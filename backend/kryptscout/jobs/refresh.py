"""Refresh job — pull latest results and update every sport engine.

Usage (from the backend directory):

    python -m kryptscout.jobs.refresh [--sports nba,mlb,...] [--force]

Runs each requested engine's refresh_data() sequentially (default: every
registered engine), capturing "kryptscout" log output and timing each sport.
--force deletes any MODELS_DIR pickle named after the sport first. No engine
writes one today (the trained NBA/NFL overlays were retired in November
2026), so it currently changes nothing; it stays for an engine that does.

The same runner backs POST /api/models/{sport}/refresh (one sport, full log)
and POST /api/jobs/refresh (all sports — the Electron scheduler's daily
refresh job calls that endpoint).
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from kryptscout import config
from kryptscout.core.schemas import Sport
from kryptscout.sports import base as engines

# College backfills can be chunky on a cold cache; generous but finite so one
# hung download can never wedge the (single-worker) API forever.
REFRESH_TIMEOUT_SECONDS = 600.0
_TRACEBACK_TAIL_LINES = 12

_SPORT_MODULES = (
    "nba", "nhl", "nfl", "mlb", "wnba", "ncaaf", "ncaab", "soccer", "tennis"
)


def load_engines() -> None:
    """Import every sport module and register its engine (as api/main.py does).

    A sport whose module fails to import (e.g. not built yet, missing data)
    is skipped so the rest of the refresh still runs.
    """
    for name in _SPORT_MODULES:
        try:
            module = importlib.import_module(f"kryptscout.sports.{name}")
            engines.register(module.build_engine())
        except Exception as exc:  # noqa: BLE001
            # exc_info: an import that only fails inside a frozen bundle needs
            # the traceback to be diagnosable from a user's log export.
            logging.getLogger("kryptscout").warning(
                "engine %s unavailable: %s", name, exc, exc_info=True
            )


# ------------------------------------------------------------ artifact summaries


def state_path(sport: Sport) -> Path:
    """Where every engine persists its replayed state (see sports/*.py)."""
    return config.DATA_DIR / f"{sport.value}_state.json"


def sport_pickles(sport: Sport) -> list[Path]:
    """MODELS_DIR pickles named after this sport — none, for today's engines."""
    return sorted(config.MODELS_DIR.glob(f"{sport.value}*.pkl"))


# Artifacts written by engines that no longer exist. MODELS_DIR lives in
# userData on a packaged build, so these outlive the update that retired the
# code which read them — and /api/models would go on listing a "model pickle"
# for two engines that are pure Elo.
RETIRED_ARTIFACT_GLOBS = ("nba_xgb_*.pkl", "nfl_xgb_*.pkl")


def purge_retired_artifacts() -> list[str]:
    """Delete leftover artifacts of retired engines; return the names removed.

    Best-effort: a file that is locked or already gone is simply skipped.
    """
    removed: list[str] = []
    for pattern in RETIRED_ARTIFACT_GLOBS:
        for path in sorted(config.MODELS_DIR.glob(pattern)):
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(path.name)
    return removed


def file_summary(path: Path) -> dict:
    """{exists, file, size_kb, updated_at} for one on-disk artifact."""
    if not path.exists():
        return {"exists": False, "file": path.name, "size_kb": None, "updated_at": None}
    stat = path.stat()
    return {
        "exists": True,
        "file": path.name,
        "size_kb": round(stat.st_size / 1024.0, 1),
        "updated_at": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
    }


def backtest_metadata(sport: Sport) -> dict | None:
    """Static backtest metrics persisted in the sport's state json, if any.

    nfl/mlb/ncaaf/ncaab save a walk-forward "metrics" dict and tennis saves
    per-tour metrics under "backtest"; those come back verbatim. nba/wnba
    keep "backtest" as a raw per-game log (a list — not metadata), and
    nhl/soccer persist no metrics at all: all of those return None.
    """
    path = state_path(sport)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for key in ("metrics", "backtest"):
        value = state.get(key)
        if isinstance(value, dict) and value:
            return value
    return None


# ------------------------------------------------------------------- the runner


class _ListHandler(logging.Handler):
    """Append every formatted record to a plain list of lines."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__(logging.DEBUG)
        self.lines = lines

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001 — log capture must never break the run
            pass


def _state_line(sport: Sport, state: dict, rewritten: bool = True) -> str:
    """One human summary line about the refreshed state file.

    `rewritten=False` means the engine left the file untouched this run —
    an incremental engine already caught up to its history window (nba after
    the Finals, say) legitimately has nothing to replay. Saying "state
    written" there reads as success while the panel's "updated N days ago"
    keeps climbing, so the two are worded apart.
    """
    if not state["exists"]:
        return f"no state file written ({state['file']} missing)"
    if not rewritten:
        return (
            f"no new results · {state['file']} already current "
            f"(unchanged since {str(state['updated_at'])[:10]})"
        )
    line = f"state written · {state['file']} {state['size_kb']} KB"
    try:
        payload = json.loads(state_path(sport).read_text(encoding="utf-8"))
        ratings = payload.get("ratings")
        if isinstance(ratings, dict):
            line += f" · {len(ratings)} ratings"
    except (OSError, ValueError):
        pass
    return line


async def refresh_sport(
    sport: Sport,
    force_retrain: bool = False,
    timeout: float = REFRESH_TIMEOUT_SECONDS,
) -> dict:
    """Run one engine's refresh_data() with log capture and a timeout guard.

    Raises KeyError when no engine is registered (the API maps that to 404).
    Engine exceptions never propagate: they come back as ok=False with the
    traceback tail in `log`. `force_retrain` deletes any MODELS_DIR pickle
    named after the sport first (no engine writes one today).

    Log capture attaches a temporary handler to the "kryptscout" logger —
    every engine logs on children of it — raising it to INFO for the
    duration; fine for our single-worker uvicorn. Returns
    {sport, ok, seconds, log, state (file_summary), summary (one line)}.
    """
    engine = engines.get_engine(sport)
    log: list[str] = []
    if force_retrain:
        for pickle in sport_pickles(sport):
            pickle.unlink()
            log.append(f"deleted {pickle.name} (force_retrain)")

    before = file_summary(state_path(sport))
    logger = logging.getLogger("kryptscout")
    handler = _ListHandler(log)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    previous_level = logger.level
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    started = time.perf_counter()
    ok = True
    try:
        # refresh_data() does blocking work between awaits (pandas replays),
        # so it runs on its own event loop in a worker thread;
        # wait_for caps how long the request can hang on it. (On timeout the
        # thread itself is orphaned, not killed — acceptable as a guard.)
        await asyncio.wait_for(
            asyncio.to_thread(asyncio.run, engine.refresh_data()), timeout=timeout
        )
    except Exception:  # noqa: BLE001 — report, never 500
        ok = False
        log.extend(traceback.format_exc().splitlines()[-_TRACEBACK_TAIL_LINES:])
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    seconds = round(time.perf_counter() - started, 2)
    state = file_summary(state_path(sport))
    rewritten = state["updated_at"] != before["updated_at"]
    if ok:
        state_line = _state_line(sport, state, rewritten)
        log.append(state_line)
        summary = f"refreshed in {seconds}s · {state_line}"
    else:
        summary = log[-1] if log else "refresh failed"
    return {
        "sport": sport.value,
        "ok": ok,
        "seconds": seconds,
        "log": log,
        "state": state,
        "summary": summary,
    }


async def refresh_all(
    sports: list[Sport] | None = None, force_retrain: bool = False
) -> dict[str, dict]:
    """Refresh every requested sport sequentially (default: all registered).

    Shared by the CLI below and POST /api/jobs/refresh. One failing (or
    unregistered) engine never sinks the rest — its entry simply reports
    ok=False. Returns {sport value: refresh_sport() result}.
    """
    available = {e.sport for e in engines.all_engines()}
    requested = sports or [e.sport for e in engines.all_engines()]
    results: dict[str, dict] = {}
    for sport in requested:
        if sport not in available:
            results[sport.value] = {
                "sport": sport.value,
                "ok": False,
                "seconds": 0.0,
                "log": [],
                "state": file_summary(state_path(sport)),
                "summary": "no engine available, skipped",
            }
            continue
        results[sport.value] = await refresh_sport(sport, force_retrain=force_retrain)
    return results


# -------------------------------------------------------------------------- CLI


async def run(sports: list[Sport] | None, force_retrain: bool) -> None:
    results = await refresh_all(sports, force_retrain=force_retrain)
    for sport, result in results.items():
        status = "ok" if result["ok"] else "FAILED"
        print(f"{sport}: {status} in {result['seconds']}s — {result['summary']}")
        if not result["ok"]:
            for line in result["log"]:
                print(f"    {line}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--sports",
        help="comma-separated sports (default: every registered engine)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete the sport's model pickle(s) first so staleness checks retrain",
    )
    args = parser.parse_args()
    sports = (
        [Sport(s.strip().lower()) for s in args.sports.split(",") if s.strip()]
        if args.sports
        else None
    )
    load_engines()
    asyncio.run(run(sports, force_retrain=args.force))


if __name__ == "__main__":
    main()

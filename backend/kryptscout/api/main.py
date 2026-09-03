"""Krypt Scout API — serves the terminal UI.

Endpoints deliver GameCards: game + model prediction + market odds + computed
edges, uniformly across sports.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from kryptscout import config, license as license_mod, logsetup
from kryptscout.autobet import engine as autobet_engine
from kryptscout.autobet import polymarket_auth
from kryptscout.core import arb as arb_mod
from kryptscout.core import fair as fair_mod
from kryptscout.core import forecast as forecast_mod
from kryptscout.core import lean as lean_mod
from kryptscout.core import sgp as sgp_mod
from kryptscout.core import totals as totals_mod
from kryptscout.core.schemas import Edge, GameCard, MoneylineOdds, Prediction, Sport
from kryptscout.datasources import injuries as injuries_source
from kryptscout.datasources import sharpmoney as sharpmoney_source
from kryptscout.datasources import weather as weather_source
from kryptscout.datasources.injuries import InjuryReport
from kryptscout.datasources.weather import GameWeather
from kryptscout.jobs import backfill as backfill_job
from kryptscout.jobs import capture as capture_job
from kryptscout.jobs import refresh as refresh_job
from kryptscout.jobs import settle as settle_job
from kryptscout.odds import providers as odds_providers
from kryptscout.sports import base as engines
from kryptscout.store import backtest as backtest_store
from kryptscout.store import db, roibacktest, tracker
from kryptscout.store import picks as picks_mod

# Install the rotating backend.log sink as early as possible — at module
# import, before uvicorn starts serving — so the file captures the full
# startup (engine imports, first-run model builds) and any crash during it.
logsetup.setup_logging()
logger = logging.getLogger("kryptscout")


def _log_startup() -> None:
    """One banner line so every backend.log opens with the context a support
    request needs: version, where data/logs live, and the log file itself."""
    version = os.environ.get("KRYPT_APP_VERSION", "dev")
    logger.info(
        "Krypt Scout backend starting — version=%s userdata=%s log=%s",
        version, config._BASE_DIR, logsetup.LOG_FILE,
    )
    if not _API_TOKEN and getattr(sys, "frozen", False):
        # A packaged backend is always launched by the Electron supervisor,
        # which mints a token every time. Missing means either the env didn't
        # propagate (a bug worth seeing in the log) or someone started this
        # binary by hand — either way the local API is answering unauthenticated.
        logger.warning(
            "no KRYPT_SCOUT_API_TOKEN in the environment — the local API is "
            "accepting unauthenticated requests"
        )


def _load_engines() -> None:
    """Import every sport module and register its engine.

    Each module in kryptscout.sports exposes build_engine(); a sport whose
    module fails to import (e.g. missing data on first run) is skipped so the
    rest of the board still serves.

    Runs on a worker thread — see _start_engine_load for why.
    """
    import importlib
    import logging

    for name in ("nba", "nhl", "nfl", "mlb", "wnba", "ncaaf", "ncaab", "soccer", "tennis"):
        try:
            module = importlib.import_module(f"kryptscout.sports.{name}")
            engines.register(module.build_engine())
        except Exception as exc:  # noqa: BLE001
            # exc_info: the message alone is useless for the imports that only
            # fail inside a frozen bundle on one platform. A bare
            # "'NoneType' object has no attribute 'replace'" from a mac build
            # cost us a release to chase; the traceback names the file.
            logging.getLogger("kryptscout").warning(
                "engine %s unavailable: %s", name, exc, exc_info=True
            )


# Engine registration is the single most expensive thing this process does:
# importing the sports modules pulls numpy/pandas/sklearn/scipy/xgboost, which
# on a cold disk takes ~25-35s (measured on macOS arm64, first launch after
# install). While a uvicorn startup handler is running NOTHING is served — not
# even /api/health — so doing this inline made the Electron supervisor's
# startup health probe time out and SIGKILL a backend that was working fine,
# restarting into an even colder start. Load on a worker thread instead: the
# app is serving within milliseconds and the engine-backed endpoints await
# _engines_ready below. Nothing else in the process touches the model stack.
_engines_ready = asyncio.Event()
_engine_load_task: asyncio.Task | None = None

# How long an engine-backed request waits for the warm-up before giving up.
# Comfortably above the worst measured cold load; a breach means the import
# is genuinely wedged, not slow.
ENGINE_WARMUP_TIMEOUT_S = 180.0


async def _start_engine_load() -> None:
    global _engine_load_task

    async def _run() -> None:
        try:
            await asyncio.to_thread(_load_engines)
        finally:
            # Set even on failure: a wedged load must not hang every request
            # until the timeout. _load_engines already swallows per-sport
            # errors, so reaching here means the registry is as full as it
            # will get.
            _engines_ready.set()

    # Held in a module global: a bare create_task() reference is weak, and a
    # garbage-collected task cancels the load silently.
    _engine_load_task = asyncio.create_task(_run())


async def _await_engines() -> None:
    """Block until the sports registry is populated. 503 if the load is stuck."""
    if _engines_ready.is_set():
        return
    try:
        await asyncio.wait_for(_engines_ready.wait(), ENGINE_WARMUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(
            503, "sport engines are still loading", headers={"Retry-After": "10"}
        )


app = FastAPI(
    title="Krypt Scout",
    version="0.1.0",
    on_startup=[_log_startup, _start_engine_load],
)

# App-wide license gate. Krypt Scout is licensed software: without an active
# license the API serves nothing but health + the license endpoints themselves,
# so the app can't be used by bypassing the renderer and hitting 127.0.0.1
# directly. is_licensed() is a compiled, offline check (kryptscout.license).
# Settlement still happens on Polymarket on-chain regardless, so locking the app
# never traps funds — see HARDENING.md / the license design.
_LICENSE_OPEN_PREFIXES = ("/api/health", "/api/license")


# sha256 of the raw Ed25519 public key that kryptscout.license verifies with.
# Duplicated here deliberately — see _license_module_intact.
# Rotating the signing key means updating this constant as well as
# license._PUBLIC_KEY_B64, or every install 402s. There is a test that fails
# loudly if the two drift (test_license_gate.py).
_LICENSE_PUBKEY_SHA256 = "2a15d4a1c0498bb563ad21fa92d60a11e9d8c92e8ad1a8df53287711c7293983"


def _license_module_intact() -> bool:
    """Whether `kryptscout.license` is the real module rather than a stand-in.

    The 2026-08-08 crack did not defeat a single check — it replaced the file
    holding it. Python resolves `from kryptscout import license` by filename at
    runtime out of a directory the user owns, so a twenty-line stub exporting
    `is_licensed = lambda: True` satisfied every caller, compiled or not. No
    machine code was patched and nothing had to be understood.

    So the gate stops taking the module's word for it. This asks for things a
    stub author has no reason to carry: the full internal surface, and the real
    verifying key. Reproducing the key means shipping the genuine article, at
    which point the stub is doing real signature checks and is no longer a stub.

    It lives here, in a compiled module, and not in license.py — a module that
    has been replaced cannot be asked to confirm it hasn't been.

    This is a cost, not a wall. Someone who reads this function can satisfy it.
    The point is that they now have to read it first."""
    try:
        for attr in ("_verify_token", "compute_hwid", "_public_key", "_read_store", "PRODUCT"):
            if not hasattr(license_mod, attr):
                return False
        if license_mod.PRODUCT != "scout":
            return False
        return license_mod.public_key_fingerprint() == _LICENSE_PUBKEY_SHA256
    except Exception:  # noqa: BLE001 — a module that throws here is not the real one
        return False


def _licensed_now() -> bool:
    """The real gate: a valid token AND a genuine module to have checked it.

    Two independent conditions rather than one boolean. `is_licensed()` alone
    was the whole of it, which is why one replaced file was the whole of the
    crack."""
    return _license_module_intact() and license_mod.is_licensed()


@app.middleware("http")
async def _license_gate(request, call_next):
    from starlette.responses import JSONResponse
    path = request.url.path
    if (
        request.method == "OPTIONS"
        or not path.startswith("/api")
        or path.startswith(_LICENSE_OPEN_PREFIXES)
        or _licensed_now()
    ):
        return await call_next(request)
    return JSONResponse(
        {"detail": "License required", "licenseRequired": True},
        status_code=402,
    )


# Local API token. Electron mints a fresh random token on every backend launch
# and hands it to this process through the environment; the renderer reads it
# over IPC and sends it as X-Scout-Token on every request. That closes the
# "anything on this box can curl 127.0.0.1:<port>" hole — another local
# process, a browser tab, or a hand-rolled client can no longer drive the
# backend just by finding the port.
#
# What this does NOT do: stop someone who controls the machine. They can read
# the token out of the process environment, or just spawn the backend binary
# themselves with a token of their own choosing. This raises the floor; it is
# not a boundary against the machine's owner. See HARDENING.md.
#
# Unset (pytest, a hand-run `python -m uvicorn`, plain `vite` dev) means auth
# is off, so the dev loop and the test suite need no ceremony.
_API_TOKEN = os.environ.get("KRYPT_SCOUT_API_TOKEN") or ""

# Health stays open: it is the supervisor's liveness probe, it carries no data,
# and it must keep answering even if token plumbing breaks.
_AUTH_OPEN_PREFIXES = ("/api/health",)


# Registered AFTER _license_gate, which in Starlette means it wraps it — an
# unauthenticated request is rejected before the license check ever runs.
@app.middleware("http")
async def _api_token_gate(request, call_next):
    from starlette.responses import JSONResponse
    path = request.url.path
    if (
        not _API_TOKEN
        or request.method == "OPTIONS"
        or not path.startswith("/api")
        or path.startswith(_AUTH_OPEN_PREFIXES)
    ):
        return await call_next(request)
    sent = request.headers.get("x-scout-token") or ""
    if hmac.compare_digest(sent, _API_TOKEN):
        return await call_next(request)
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


app.add_middleware(
    CORSMiddleware,
    # Any localhost port (dev servers move: 5173, 5183, ...) plus "null" (the
    # packaged renderer loads from file://). Non-simple requests (JSON POST,
    # DELETE) preflight — a mismatched origin here 400s the OPTIONS and the
    # browser silently kills the request. Remote web origins stay blocked.
    allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|null)$",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/sports")
async def sports() -> list[dict]:
    await _await_engines()
    return [
        {"sport": e.sport.value, "model": e.model_name}
        for e in engines.all_engines()
    ]


@app.get("/api/board/{sport}", response_model=list[GameCard])
async def board(sport: Sport, with_odds: bool = True) -> list[GameCard]:
    """The main terminal board for one sport: slate + predictions + edges."""
    await _await_engines()
    try:
        engine = engines.get_engine(sport)
    except KeyError:
        raise HTTPException(404, f"No engine registered for {sport.value}")

    games = await engine.upcoming_games()
    # Predictions more than a week out are stale guesses (rosters, lines and
    # ratings all move) — cap the board at 7 days and serve soonest-first.
    horizon = datetime.now(timezone.utc) + timedelta(days=7)

    def _start_utc(g) -> datetime:
        st = g.start_time
        return st if st.tzinfo else st.replace(tzinfo=timezone.utc)

    games = sorted((g for g in games if _start_utc(g) <= horizon), key=_start_utc)
    predictions = {p.game_id: p for p in await engine.predict(games)}

    # Every registered provider (ESPN lines, prediction markets, The Odds API
    # when keyed) merged per game id; provider failures are isolated inside
    # merged_odds so the board still renders without market columns.
    odds_by_game: dict[str, list[MoneylineOdds]] = {}
    if with_odds:
        odds_by_game = await odds_providers.merged_odds(sport, games)

    cards: list[GameCard] = []
    for game in games:
        odds = odds_by_game.get(game.id, [])
        # No-information predictions (tennis unknown players etc.) adopt the
        # devigged market consensus instead of showing a fake 50/50.
        pred = picks_mod.apply_market_fallback(predictions.get(game.id), odds)
        # The sharp fair comes first: value edges are priced against it.
        fair = fair_mod.sharp_fair(game.id, sport, odds, game.start_time)
        edges = _compute_edges(game.id, sport, pred, odds, game.start_time, fair) if pred else []
        card = GameCard(game=game, prediction=pred, odds=odds, edges=edges, fair=fair)
        card.totals_edges = totals_mod.totals_edges(card)
        # Model-free signals: a locked-in cross-venue position, and the
        # exchange-lead reference. Neither touches picks or autobet.
        card.arb = arb_mod.find_arb(game.id, sport, odds, game.start_time)
        card.lean = lean_mod.find_lean(game.id, sport, odds, game.start_time)
        card.sgp = sgp_mod.sgp_fair(card)
        card.forecast = forecast_mod.display_forecast(card)
        cards.append(card)

    try:
        tracker.record_cards(cards)
    except Exception:  # noqa: BLE001 — recording must never break the board
        logger.exception("record_cards failed for %s", sport.value)
    return cards


def _compute_edges(
    game_id: str,
    sport: Sport,
    pred: Prediction,
    odds_list,
    starts_at: datetime | None = None,
    fair=None,
) -> list[Edge]:
    """Shared with the capture job — see store.picks.compute_edges."""
    return picks_mod.compute_edges(game_id, sport, pred, odds_list, starts_at, fair)


@app.get("/api/injuries/{sport}", response_model=list[InjuryReport])
async def injuries(sport: Sport) -> list[InjuryReport]:
    """League-wide injury reports; empty list for sports without a feed."""
    return await injuries_source.injuries(sport)


@app.get("/api/weather/{game_id}")
async def weather(game_id: str) -> GameWeather | None:
    """Game-time forecast at the home venue, or JSON null when unavailable.

    The game is looked up in the store (games recorded by the board / capture
    job); an unknown game_id is a 404. Null covers domes, indoor sports, and
    starts beyond the forecast horizon — see datasources.weather.
    """
    conn = db.connect()
    try:
        found = db.rows(
            conn,
            "SELECT sport, home, start_time FROM games WHERE game_id = ?",
            (game_id,),
        )
    finally:
        conn.close()
    if not found:
        raise HTTPException(404, f"Unknown game_id {game_id}")
    game = found[0]
    return await weather_source.game_weather(
        Sport(game["sport"]),
        game["home"],
        datetime.fromisoformat(game["start_time"]),
    )


@app.get("/api/sharpmoney/{sport}")
async def sharpmoney(sport: Sport, on: date | None = None) -> dict:
    """Sharp-money slate intelligence — whale flow, top holders, and top-100
    sports-leaderboard wallet positioning per game on today's (or `on`'s)
    slate. Display-only context from the public Polymarket Data API;
    best-effort per game and never a 500 (see datasources/sharpmoney.py).
    """
    try:
        return _json_safe(await sharpmoney_source.slate_report(sport, on))
    except KeyError:
        raise HTTPException(404, f"No engine registered for {sport.value}")
    except Exception as exc:  # noqa: BLE001 — undocumented upstream API
        logger.warning("sharpmoney %s failed: %s", sport.value, exc)
        return {"sport": sport.value, "games": [], "error": str(exc)[:200]}


@app.get("/api/performance")
async def performance(
    sport: Sport | None = None,
    model: str | None = None,
    since: date | None = None,
) -> dict:
    """Model accuracy over settled games — tracker.performance() passthrough."""
    return _json_safe(tracker.performance(sport=sport, model=model, since=since))


@app.get("/api/clv")
async def clv(sport: Sport | None = None, since: date | None = None) -> dict:
    """Closing-line value of model picks — overall + by_sport (argmax side),
    by_tier (STRONG/LEAN/MODEL, scored on each pick's own side), and
    vs_close (scored off the book's declared open/close, backfillable).

    `by_tier` reconstructs endpoints from odds_snapshots, so it only covers
    games this install happened to be polling early AND near kickoff.
    `vs_close` reads line_endpoints, which jobs/lines.py can fill in after the
    fact — same question, far more of the ledger answered.
    """
    base = tracker.clv(sport=sport, since=since)
    tiers = tracker.clv_by_tier(sport=sport, since=since)
    endpoints = tracker.clv_vs_close(sport=sport, since=since)
    lean = tracker.lean_clv(sport=sport, since=since)
    return _json_safe({**base, **tiers, "vs_close": endpoints, "lean": lean})


@app.get("/api/benchmark")
async def benchmark(sport: Sport | None = None, since: date | None = None) -> dict:
    """Scout vs ESPN's predictor vs the market — tracker.espn_benchmark()."""
    return _json_safe(tracker.espn_benchmark(sport=sport, since=since))


@app.get("/api/trackers")
async def trackers() -> dict:
    """Schedule-shaped trackers vs the market — tracker.context_trackers()."""
    return _json_safe(tracker.context_trackers())


@app.get("/api/lean")
async def lean(sport: Sport | None = None, since: date | None = None) -> dict:
    """Same-book CLV of the exchange-lead reference — tracker.lean_clv()."""
    return _json_safe(tracker.lean_clv(sport=sport, since=since))


@app.get("/api/arbs")
async def arbs(limit: int = 50) -> dict:
    """Every true-arb sighting logged by the board/capture, newest first."""
    conn = db.connect()
    try:
        rows = db.rows(
            conn,
            """
            SELECT a.game_id, a.sport, a.seen_at, a.legs, a.margin_pct,
                   a.net_margin_pct, g.home, g.away, g.start_time
            FROM arb_sightings a LEFT JOIN games g ON g.game_id = a.game_id
            ORDER BY a.seen_at DESC LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        )
        total = conn.execute("SELECT COUNT(*) FROM arb_sightings").fetchone()[0]
    finally:
        conn.close()
    for row in rows:
        try:
            row["legs"] = json.loads(row["legs"])
        except (TypeError, ValueError):
            row["legs"] = []
    return _json_safe({"n": total, "recent": rows})


@app.get("/api/movement/{game_id}")
async def movement(game_id: str) -> dict:
    """Odds-snapshot series per book — tracker.line_movement() passthrough."""
    return tracker.line_movement(game_id)


@app.get("/api/roi")
async def roi(sport: Sport | None = None, since: date | None = None) -> dict:
    """Pick-level ROI over recorded picks — tracker.roi() passthrough."""
    return _json_safe(tracker.roi(sport=sport, since=since))


@app.get("/api/picklog")
async def picklog(sport: Sport | None = None, since: date | None = None) -> dict:
    """Every value pick the bot has recommended + the bot's own P&L —
    tracker.pick_log() passthrough.

    The tracker page's BOT PICKS view: pending and graded STRONG/LEAN picks
    (newest kickoff first) with a pnl block shaped exactly like /api/mybets
    ({n, graded, units, roi_pct, record, daily}) at the flat 1u stake the
    settle job grades picks with.
    """
    return _json_safe(tracker.pick_log(sport=sport, since=since))


@app.get("/api/picks")
async def picks(
    on: date | None = Query(None, alias="date"),
    sport: Sport | None = None,
    tiers: str | None = None,
) -> dict:
    """One day's recorded picks — tracker.list_picks() passthrough.

    `date` (YYYY-MM-DD; malformed → 422) selects games by the UTC date of
    their start_time and defaults to today UTC — the Electron caller passes
    its local date explicitly to avoid the UTC-midnight rollover. `tiers` is
    a comma list (e.g. STRONG,LEAN). Returns {"date": ..., "picks": [rows
    with matchup fields + a `kickoff` ISO timestamp]}.
    """
    on = on or datetime.now(timezone.utc).date()
    tier_list = (
        [t.strip() for t in tiers.split(",") if t.strip()] if tiers else None
    )
    return _json_safe(
        {
            "date": on.isoformat(),
            "picks": tracker.list_picks(on=on, sport=sport, tiers=tier_list),
        }
    )


class MyBetRequest(BaseModel):
    """POST /api/mybets body — one of the user's own bets to log."""

    game_id: str
    side: Literal["home", "away", "draw", "over", "under"]
    price: int
    book: str | None = None
    stake_units: float = 1.0
    note: str | None = None
    # Required for over/under sides — the O/U line the bet grades against.
    line: float | None = None

    @field_validator("price")
    @classmethod
    def _american_price(cls, value: int) -> int:
        if abs(value) < 100:
            raise ValueError("price must be American odds (|price| >= 100)")
        return value


@app.post("/api/mybets")
async def create_mybet(body: MyBetRequest) -> dict:
    """Log one of the user's own bets — tracker.log_bet() passthrough.

    The game must already be recorded (the board records games on serve);
    an unknown game_id is a 404. sport/team resolve server-side from the
    game row + side, stake_units clamps to 0.1-100. Returns the stored row;
    an exact ungraded duplicate (same game/side/book/price) returns the
    existing row with "duplicate": true instead of inserting.
    """
    try:
        row = tracker.log_bet(
            body.game_id, body.side, body.price,
            book=body.book, stake_units=body.stake_units, note=body.note,
            line=body.line,
        )
    except KeyError:
        raise HTTPException(404, f"Unknown game_id {body.game_id}")
    except ValueError as exc:  # totals bet without a line
        raise HTTPException(422, str(exc))
    return _json_safe(row)


class ParlayLegRequest(BaseModel):
    """One leg of POST /api/mybets/parlay — a side of a recorded game."""

    game_id: str
    side: Literal["home", "away", "draw"]
    price: int
    book: str | None = None

    @field_validator("price")
    @classmethod
    def _american_price(cls, value: int) -> int:
        if abs(value) < 100:
            raise ValueError("price must be American odds (|price| >= 100)")
        return value


class ParlayRequest(BaseModel):
    """POST /api/mybets/parlay body — a parlay slip to track."""

    legs: list[ParlayLegRequest] = Field(min_length=2)
    stake_units: float = 1.0
    note: str | None = None


@app.post("/api/mybets/parlay")
async def create_parlay(body: ParlayRequest) -> dict:
    """Track a parlay the user built — tracker.log_parlay() passthrough.

    Every leg's game must already be recorded (404 for an unknown game_id);
    same-game legs are rejected (422, they're correlated — the builder blocks
    them too). sport/team resolve server-side per leg, stake_units clamps to
    0.1-100, and the stored combined price is the product of the legs'
    decimal prices. Returns the stored parlay with its legs; an exact
    ungraded duplicate (same leg set) returns the existing parlay with
    "duplicate": true instead of inserting.
    """
    try:
        row = tracker.log_parlay(
            [leg.model_dump() for leg in body.legs],
            stake_units=body.stake_units,
            note=body.note,
        )
    except KeyError as exc:
        raise HTTPException(404, f"Unknown game_id {exc.args[0]}")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _json_safe(row)


@app.delete("/api/mybets/parlay/{parlay_id}")
async def delete_parlay(parlay_id: int) -> dict:
    """Remove an ungraded parlay; graded parlays are immutable history (409)."""
    deleted = tracker.delete_parlay(parlay_id)
    if deleted is None:
        raise HTTPException(404, f"Unknown parlay id {parlay_id}")
    if not deleted:
        raise HTTPException(409, "Parlay already graded — cannot delete")
    return {"deleted": parlay_id}


@app.delete("/api/mybets/{bet_id}")
async def delete_mybet(bet_id: int) -> dict:
    """Remove an ungraded user bet; graded bets are immutable history (409)."""
    deleted = tracker.delete_bet(bet_id)
    if deleted is None:
        raise HTTPException(404, f"Unknown bet id {bet_id}")
    if not deleted:
        raise HTTPException(409, "Bet already graded — cannot delete")
    return {"deleted": bet_id}


@app.get("/api/mybets")
async def mybets(since: date | None = None) -> dict:
    """The user's logged bets + parlays + P&L — tracker.my_bets() passthrough.

    Returns {"bets": [rows joined with games, newest first], "parlays":
    [parlay rows each with "legs"], "pnl": {n, graded, units, roi_pct
    (stake-weighted), record "W-L-P", daily [{date, units, cum_units}]}} —
    a parlay counts as one bet at its own stake; see tracker.my_bets.
    """
    return _json_safe(tracker.my_bets(since=since))


@app.get("/api/mybets/clv")
async def mybets_clv(since: date | None = None) -> dict:
    """CLV coaching over the user's own bets — tracker.user_clv passthrough."""
    return _json_safe(tracker.user_clv(since=since))


@app.get("/api/models")
async def models() -> dict:
    """The MODELS panel: one artifact/performance summary per registered engine.

    Read-only and cheap — no engine calls, just file stats, the state json,
    and one tracker.performance() query. Per engine:
      state     file_summary of DATA_DIR/{sport}_state.json ({exists, file,
                size_kb, updated_at}; exists=False with nulls when missing)
      pickle    same shape for the sport's MODELS_DIR pickle (nfl_xgb_v3.pkl
                etc.), or null when the engine keeps no pickle
      live      this sport's settled-game metrics {n, accuracy, log_loss,
                brier} from tracker.performance(), or null with no history
      backtest  static walk-forward metrics persisted in the state json
                (see jobs.refresh.backtest_metadata), or null
    """
    await _await_engines()
    overall = tracker.performance()["overall"]
    rows = []
    for engine in engines.all_engines():
        sport_key = engine.sport.value
        perf = next(
            (r for r in overall
             if r["sport"] == sport_key and r["model"] == engine.model_name),
            None,
        ) or next((r for r in overall if r["sport"] == sport_key), None)
        live = (
            {k: perf[k] for k in ("n", "accuracy", "log_loss", "brier")}
            if perf is not None
            else None
        )
        pickles = refresh_job.sport_pickles(engine.sport)
        rows.append(
            {
                "sport": sport_key,
                "model": engine.model_name,
                "state": refresh_job.file_summary(
                    refresh_job.state_path(engine.sport)
                ),
                "pickle": refresh_job.file_summary(pickles[0]) if pickles else None,
                "live": live,
                "backtest": refresh_job.backtest_metadata(engine.sport),
            }
        )
    return _json_safe({"models": rows})


@app.get("/api/backtest")
async def backtest_all() -> dict:
    """Model-skill backtest for every registered engine.

    Read-only: each report is derived from the sport's persisted walk-forward
    log (DATA_DIR/{sport}_state.json), never a live engine call. Returns
    {"reports": [backtest_store.report(...) per engine]} — see
    store.backtest.report for the per-sport envelope (kind log/summary/none,
    calibration curve + confidence sweep on "log" sports).
    """
    await _await_engines()
    reports = [
        backtest_store.report(engine.sport, model=engine.model_name)
        for engine in engines.all_engines()
    ]
    return _json_safe({"reports": reports})


@app.get("/api/backtest/{sport}")
async def backtest_one(sport: Sport) -> dict:
    """One sport's model-skill backtest — store.backtest.report passthrough."""
    await _await_engines()
    engine = next((e for e in engines.all_engines() if e.sport == sport), None)
    model = engine.model_name if engine is not None else None
    return _json_safe(backtest_store.report(sport, model=model))


@app.get("/api/backtest/{sport}/roi")
async def backtest_roi(
    sport: Sport,
    offset_minutes: int = 60,
    min_volume: float = 1000.0,
    min_ev: float = 0.04,
    min_win_prob: float = 0.0,
    min_edge: float = 0.0,
    min_prob: float = 0.0,
    max_prob: float = 0.85,
    stake_mode: Literal["fixed", "percent", "confidence", "kelly"] = "fixed",
    stake_usd: float = 10.0,
    stake_pct: float = 2.0,
    stake_pct_min: float = 2.0,
    stake_pct_max: float = 8.0,
    bankroll_usd: float = 1000.0,
    daily_stop_loss_usd: float = 0.0,
    daily_stop_profit_usd: float = 0.0,
    stop_loss_usd: float = 0.0,
    stop_profit_usd: float = 0.0,
) -> dict:
    """Dollar-ROI strategy backtest over backfilled Polymarket history.

    Joins the sport's walk-forward model log with pm_markets price snapshots
    (jobs/backfill.py must have run), then replays the strategy at three fill
    assumptions — see store/roibacktest.py. Same knobs as the live autobet
    engine so the AUTO-BET strategy can be tested verbatim.
    """
    return _json_safe(roibacktest.report(
        sport.value, offset_minutes=offset_minutes, min_volume=min_volume,
        min_ev=min_ev, min_win_prob=min_win_prob, min_edge=min_edge,
        min_prob=min_prob, max_prob=max_prob, stake_mode=stake_mode, stake_usd=stake_usd,
        stake_pct=stake_pct, stake_pct_min=stake_pct_min,
        stake_pct_max=stake_pct_max, bankroll_usd=bankroll_usd,
        daily_stop_loss_usd=daily_stop_loss_usd,
        daily_stop_profit_usd=daily_stop_profit_usd,
        stop_loss_usd=stop_loss_usd, stop_profit_usd=stop_profit_usd,
    ))


@app.get("/api/backtest/{sport}/roi/optimize")
async def backtest_roi_optimize(
    sport: Sport,
    offset_minutes: int = 60,
    min_volume: float = 5000.0,
    stake_mode: Literal["fixed", "percent", "confidence", "kelly"] = "fixed",
) -> dict:
    """Grid-search gate thresholds over backfilled history with a 70/30
    chronological tune/validation split — see store.roibacktest.optimize."""
    return _json_safe(roibacktest.optimize(
        sport.value, offset_minutes=offset_minutes, min_volume=min_volume,
        stake_mode=stake_mode,
    ))


class ModelRefreshRequest(BaseModel):
    """Optional POST /api/models/{sport}/refresh body."""

    force_retrain: bool = False


@app.post("/api/models/{sport}/refresh")
async def models_refresh(sport: Sport, body: ModelRefreshRequest | None = None) -> dict:
    """Run one engine's refresh_data() and report what happened.

    force_retrain deletes the sport's MODELS_DIR pickle(s) first so pickle
    staleness logic (nfl, nba) retrains. Returns {sport, ok, seconds, log
    (captured "kryptscout" logging + runner summary lines), state (updated
    state-file summary), summary}. Engine failures are ok=false with the
    traceback tail in log — never a 500; see jobs.refresh.refresh_sport
    (which also guards with a ~600s timeout).
    """
    force = body.force_retrain if body is not None else False
    try:
        result = await refresh_job.refresh_sport(sport, force_retrain=force)
    except KeyError:
        raise HTTPException(404, f"No engine registered for {sport.value}")
    return _json_safe(result)


@app.post("/api/jobs/refresh")
async def jobs_refresh() -> dict:
    """Run the refresh job over HTTP (Electron scheduler's daily entry point).

    Same logic as `python -m kryptscout.jobs.refresh`: every registered
    engine's refresh_data(), sequentially, one failure never sinking the
    rest. Returns {"results": {sport: {ok, seconds, summary}}}.
    """
    results = await refresh_job.refresh_all()
    return _json_safe(
        {
            "results": {
                sport: {key: result[key] for key in ("ok", "seconds", "summary")}
                for sport, result in results.items()
            }
        }
    )


class CaptureRequest(BaseModel):
    """Optional POST /api/jobs/capture body; None sports = every engine."""

    sports: list[Sport] | None = None


@app.post("/api/jobs/capture")
async def jobs_capture(body: CaptureRequest | None = None) -> dict:
    """Run the capture job over HTTP (Electron scheduler entry point).

    Same logic as `python -m kryptscout.jobs.capture`: build boards for the
    requested sports (all registered engines when the body is omitted),
    including market odds from every configured provider, and record them.
    When autobet is enabled, a sweep runs over the freshly recorded picks
    right after the capture (its summary rides along; a sweep failure never
    fails the capture). Returns {"results": {sport: counts or {"error":
    message}}, "autobet": sweep summary | None}.
    """
    sports = body.sports if body is not None else None
    results = await capture_job.capture_all(sports)
    autobet_summary = None
    if autobet_engine.load_settings()["enabled"]:
        try:
            autobet_summary = await autobet_engine.sweep()
        except Exception as exc:  # noqa: BLE001 — capture must still succeed
            logger.warning("autobet sweep after capture failed: %s", exc)
            autobet_summary = {"error": str(exc)}
    return {"results": results, "autobet": _json_safe(autobet_summary)}


# ------------------------------------------------------------------- autobet
# Polymarket auto-betting (kryptscout/autobet). Real-money surface: the
# engine defaults to disabled + dry-run; wallet material lives in the
# encrypted credential store (polymarket_auth), never in these responses.


class AutobetSettingsRequest(BaseModel):
    """POST /api/autobet/settings body — partial update, every field optional."""

    enabled: bool | None = None
    dry_run: bool | None = None
    stake_mode: Literal["fixed", "percent", "confidence", "kelly"] | None = None
    stake_usd: float | None = None
    stake_pct: float | None = None
    stake_pct_min: float | None = None
    stake_pct_max: float | None = None
    kelly_mult: float | None = None
    kelly_cap_pct: float | None = None
    unit_usd: float | None = None
    min_ev: float | None = None
    min_win_prob: float | None = None
    min_edge: float | None = None
    tiers: list[str] | None = None
    sports: list[str] | None = None
    best_edge_only: bool | None = None
    max_bets_per_day: int | None = None
    min_prob: float | None = None
    max_prob: float | None = None
    lead_minutes: int | None = None
    max_days_ahead: int | None = None
    max_hours_ahead: int | None = None
    daily_stop_loss_usd: float | None = None
    daily_stop_profit_usd: float | None = None
    stop_loss_usd: float | None = None
    stop_profit_usd: float | None = None


class AutobetWalletRequest(BaseModel):
    """POST /api/autobet/wallet body. `funder` is the Polymarket deposit-wallet
    address for email/Magic accounts (key exported from Polymarket)."""

    private_key: str
    funder: str | None = None


@app.get("/api/autobet")
async def autobet_status() -> dict:
    """Settings + wallet presence + today's activity. Local-only (no network);
    balance / trading-readiness go through POST /api/autobet/wallet/test."""
    return _json_safe(autobet_engine.get_status())


@app.post("/api/autobet/settings")
async def autobet_settings(body: AutobetSettingsRequest) -> dict:
    """Merge a partial settings update (engine._sanitize clamps every knob)."""
    patch = body.model_dump(exclude_none=True)
    logger.info("autobet settings update: %s", patch)
    saved = autobet_engine.save_settings(patch)
    # The two switches that gate real money deserve an explicit, greppable line.
    logger.info(
        "autobet now: enabled=%s mode=%s", saved["enabled"],
        "LIVE" if not saved["dry_run"] else "paper",
    )
    return saved


class AutobetProfileRequest(BaseModel):
    """POST /api/autobet/profiles body. `settings` defaults to the live
    strategy when omitted (save-current); `apply` merges the named strategy
    into the live settings instead of saving."""

    name: str
    apply: bool = False


@app.get("/api/autobet/profiles")
async def autobet_profiles() -> dict:
    """Built-in presets + user-saved strategy profiles (name + full rules)."""
    return _json_safe(autobet_engine.list_strategies())


@app.post("/api/autobet/profiles")
async def autobet_save_profile(body: AutobetProfileRequest) -> dict:
    """Save the current strategy under a name, or (apply=true) load a named
    preset/profile into the live settings.

    Save returns {"strategies": {...}}; apply returns {"settings": {...}} — the
    new full settings, so the UI refreshes the form the same way it does on a
    settings patch.
    """
    if body.apply:
        try:
            saved = autobet_engine.apply_strategy(body.name)
        except KeyError:
            raise HTTPException(404, f"No strategy named {body.name!r}")
        logger.info("autobet strategy applied: %s", body.name)
        return _json_safe({"settings": saved})
    try:
        strategies = autobet_engine.save_profile(body.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    logger.info("autobet profile saved: %s", body.name)
    return _json_safe({"strategies": strategies})


@app.delete("/api/autobet/profiles/{name}")
async def autobet_delete_profile(name: str) -> dict:
    """Delete a user profile (built-in presets can't be deleted)."""
    strategies = autobet_engine.delete_profile(name)
    logger.info("autobet profile deleted: %s", name)
    return _json_safe({"strategies": strategies})


@app.post("/api/autobet/wallet")
async def autobet_wallet(body: AutobetWalletRequest) -> dict:
    """Save the trading wallet (encrypted at rest) and probe readiness.

    Returns {"wallet": status, "check": {ready, balance} | {"error": ...}} —
    the check needs the network, so its failure still leaves the key saved.
    """
    try:
        polymarket_auth.save_credentials(body.private_key, funder=body.funder)
    except ValueError as exc:
        logger.warning("autobet wallet save rejected: %s", exc)
        raise HTTPException(422, str(exc))
    logger.info("autobet wallet saved (encrypted); running readiness probe")
    try:
        check = await autobet_engine.wallet_check()
    except Exception as exc:  # noqa: BLE001 — key is saved; report the probe
        logger.warning("autobet wallet readiness probe failed: %s", exc)
        check = {"error": str(exc)}
    return _json_safe({"wallet": polymarket_auth.credentials_status(), "check": check})


@app.post("/api/autobet/wallet/test")
async def autobet_wallet_test() -> dict:
    """Trading-readiness + balance probe for the saved wallet (network)."""
    if not polymarket_auth.credentials_present():
        raise HTTPException(404, "No wallet configured")
    try:
        return _json_safe(await autobet_engine.wallet_check())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"wallet check failed: {exc}")


@app.delete("/api/autobet/wallet")
async def autobet_wallet_clear() -> dict:
    """Delete the saved wallet key + derived API credentials."""
    polymarket_auth.clear_credentials()
    logger.info("autobet wallet cleared")
    return {"cleared": True}


@app.post("/api/autobet/run")
async def autobet_run() -> dict:
    """Sweep now (the RUN NOW button). Runs even while disabled, but honors
    dry_run and every money rule — see engine.sweep(force=True)."""
    logger.info("autobet RUN NOW requested")
    return _json_safe(await autobet_engine.sweep(force=True))


@app.get("/api/autobet/drift")
async def autobet_drift() -> dict:
    """Live-vs-backtest drift monitor — engine.drift passthrough (advisory)."""
    return _json_safe(autobet_engine.drift())


@app.get("/api/mybets/guardian")
async def mybets_guardian() -> dict:
    """Slip guardian: injury warnings on games carrying the user's PENDING
    bets. For each ungraded bet on a game that hasn't started, the sport's
    injury feed is checked for OUT/doubtful players on either team. Best
    effort — sports without a feed simply contribute no warnings.
    """
    now = datetime.now(timezone.utc)
    conn = db.connect()
    try:
        pend = db.rows(
            conn,
            """
            SELECT b.id, b.sport, b.side, b.team, b.line, g.home, g.away,
                   g.start_time, g.game_id
            FROM user_bets b JOIN games g ON g.game_id = b.game_id
            WHERE b.result IS NULL AND g.start_time > ?
            """,
            (db.iso(now),),
        )
    finally:
        conn.close()
    warnings: list[dict] = []
    for sport_key in sorted({b["sport"] for b in pend}):
        try:
            reports = await injuries_source.injuries(Sport(sport_key))
        except Exception:  # noqa: BLE001 — advisory, never a 500
            continue
        bad = [
            r for r in reports
            if any(k in (r.status or "").lower()
                   for k in ("out", "doubtful", "injured reserve"))
        ]
        if not bad:
            continue
        for bet in (b for b in pend if b["sport"] == sport_key):
            teams = {bet["home"], bet["away"]}
            for r in bad:
                if r.team in teams:
                    warnings.append({
                        "bet_id": bet["id"], "game_id": bet["game_id"],
                        "bet_team": bet["team"], "home": bet["home"],
                        "away": bet["away"], "kickoff": bet["start_time"],
                        "player": r.player, "injured_team": r.team,
                        "status": r.status,
                    })
    return _json_safe({"pending_bets": len(pend), "warnings": warnings})


@app.get("/api/autobet/log")
async def autobet_log(limit: int = 50) -> dict:
    """Newest auto-bet attempts (dry/placed/unmatched/error), games joined."""
    return _json_safe({"attempts": autobet_engine.recent_log(limit)})


_backfill_task: asyncio.Task | None = None


@app.post("/api/jobs/backfill")
async def jobs_backfill() -> dict:
    """Start the Polymarket history harvest in the background (idempotent).

    ~36k polite requests on first run (an hour or two); both passes are
    resumable, so re-triggering after an interruption continues where it
    left off. GET /api/backtest/{sport}/roi's join counts show progress.
    """
    global _backfill_task
    if _backfill_task is not None and not _backfill_task.done():
        return {"started": False, "running": True}
    logger.info("backfill harvest starting (background task)")
    _backfill_task = asyncio.create_task(backfill_job.run())
    return {"started": True, "running": True}


@app.post("/api/jobs/settle")
async def jobs_settle() -> dict:
    """Run the settle job over HTTP (Electron scheduler entry point).

    Same logic as `python -m kryptscout.jobs.settle`: settle every sport
    (grading frozen picks along the way), then report. Returns
    {"results": {sport: settle summary or {"error": message}},
     "performance": ..., "roi": ...}.
    """
    return _json_safe(await settle_job.settle_all())


def _json_safe(value):
    """Replace non-finite floats (tracker means over zero games) with None."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


# ------------------------------------------------------------------- license
# Krypt Scout licensing (kryptscout/license — compiled). Live auto-betting is
# gated on an active license; everything else (board, MY BETS, settlement,
# dry-run) is never gated. Status is local/offline; activate/deactivate talk to
# the license server.


class LicenseActivateRequest(BaseModel):
    key: str


@app.get("/api/license")
async def license_status() -> dict:
    """Local license state (no network) — licensed flag, expiry, seats, hwid."""
    return _json_safe(license_mod.status())


@app.post("/api/license/activate")
async def license_activate(body: LicenseActivateRequest) -> dict:
    """Bind this machine to a license key; store the signed token."""
    try:
        return _json_safe(await license_mod.activate(body.key))
    except license_mod.LicenseError as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/license/deactivate")
async def license_deactivate() -> dict:
    """Free this machine's seat and clear the local license."""
    return _json_safe(await license_mod.deactivate())


@app.post("/api/license/heartbeat")
async def license_heartbeat() -> dict:
    """Renew the token / enforce revocation now (also runs on a schedule)."""
    ok = await license_mod.heartbeat()
    return {"ok": ok, **_json_safe(license_mod.status())}


@app.get("/api/health")
async def health() -> dict:
    # Answers immediately, warm or cold: enginesReady distinguishes "serving,
    # still importing the model stack" from "fully up". Never awaits the
    # warm-up — this is the probe the supervisor kills the process over.
    return {
        "ok": True,
        "time": datetime.now(timezone.utc).isoformat(),
        "enginesReady": _engines_ready.is_set(),
        "sports": len(engines.all_engines()),
    }

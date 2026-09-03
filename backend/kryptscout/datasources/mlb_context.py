"""MLB home-plate umpire and lineup postings, with first-seen timestamps.

One MLB Stats API schedule call per capture, hydrated with `officials` and
`lineups` (verified live 2026-09-02: both keys appear on the schedule row
once posted, hours before first pitch). The umpire is recorded once per game
with the time it was first seen; lineups get a first-seen stamp when both
sides have a batting order. Both are schedule facts the market prices —
they are tracked so tracker.context_trackers() can score the umpire's
totals residual against the DraftKings close and measure how early the
postings land relative to line moves. No forecast uses them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from kryptscout.core.schemas import Game, GameCard, Sport
from kryptscout.store import db

logger = logging.getLogger("kryptscout")

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
TIMEOUT = 20.0


@dataclass(frozen=True)
class SlateContext:
    umpire_id: Optional[int]
    umpire_name: Optional[str]
    lineups_posted: bool


def parse_schedule(payload: dict) -> dict[tuple[str, str], SlateContext]:
    """{(officialDate, home team name): context} from a hydrated schedule."""
    out: dict[tuple[str, str], SlateContext] = {}
    for day in payload.get("dates", []) or []:
        for game in day.get("games", []) or []:
            when = game.get("officialDate") or day.get("date")
            home = ((game.get("teams") or {}).get("home") or {}).get("team", {}).get("name")
            if not when or not home:
                continue
            ump_id = ump_name = None
            for official in game.get("officials") or []:
                if official.get("officialType") == "Home Plate":
                    person = official.get("official") or {}
                    ump_id = person.get("id")
                    ump_name = person.get("fullName")
                    break
            lineups = game.get("lineups") or {}
            posted = bool(lineups.get("homePlayers")) and bool(lineups.get("awayPlayers"))
            out[(when, str(home))] = SlateContext(ump_id, ump_name, posted)
    return out


async def fetch_slate_context(dates: list[str]) -> dict[tuple[str, str], SlateContext]:
    if not dates:
        return {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                SCHEDULE_URL,
                params={
                    "sportId": 1, "startDate": min(dates), "endDate": max(dates),
                    "hydrate": "officials,lineups",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — best-effort context feed
        logger.debug("mlb context fetch failed: %s", exc)
        return {}
    return parse_schedule(payload if isinstance(payload, dict) else {})


def _canon(name: str) -> str:
    from kryptscout.sports.mlb import _canon as canon  # local: sports imports datasources

    return canon(name)


def _candidate_dates(game: Game) -> list[str]:
    d = game.start_time.date()
    return [d.isoformat(), (d - timedelta(days=1)).isoformat()]


async def capture(cards: list[GameCard]) -> int:
    """Record umpire and lineup first-seen for upcoming MLB games; rows written."""
    games = [c.game for c in cards if c.game.sport is Sport.MLB and not c.game.completed]
    if not games:
        return 0
    dates = sorted({d for g in games for d in _candidate_dates(g)})
    slate = await fetch_slate_context(dates)
    if not slate:
        return 0
    now = datetime.now(timezone.utc)
    written = 0
    conn = db.connect()
    try:
        with conn:
            for game in games:
                home = _canon(game.home)
                ctx = next((slate[(d, home)] for d in _candidate_dates(game) if (d, home) in slate), None)
                if ctx is None or (ctx.umpire_id is None and not ctx.lineups_posted):
                    continue
                db.upsert_game_context(
                    conn, game.id, game.sport.value, now,
                    umpire_id=ctx.umpire_id, umpire_name=ctx.umpire_name,
                    lineups_posted=ctx.lineups_posted,
                )
                written += 1
    except Exception:  # noqa: BLE001 — never into the capture job
        logger.exception("mlb context capture failed")
        return written
    finally:
        conn.close()
    return written

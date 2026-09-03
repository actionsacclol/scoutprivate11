"""Schedule-shaped game context — recorded, scored, never modelled.

The information-source sweep (2026-09-02) found nothing that adds to the
closing line as a forecast feature. One cell stood out and needs a season of
tracking before it means anything: in soccer, a side on three days' rest or
less facing a rested side beat the Pinnacle close by 7–11 probability
points (z 3.8, n=478) — but +23% / +8% / −7% ROI by season and no log-loss
gain, so it is either a 2026-27 confirmation at n≈300 or gone. Two more
cost nothing to record and score against the DraftKings close the app
already backfills: the MLB home-plate umpire (a tenths-of-a-run effect on
totals in the literature, partly priced) and, once the season and a
verified feed exist, the NBA referee crew.

This module computes REST DAYS from the store's own games table for every
recorded card. The MLB umpire and lineup first-seen timestamps come from
datasources/mlb_context. Scoring lives in tracker.context_trackers().
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from kryptscout.core.schemas import GameCard, Sport
from kryptscout.store import db

# A previous game further back than this is not "rest", it is a break
# (international window, All-Star, season start): rest is unknown.
MAX_REST_DAYS = 21.0
# "Short rest": the soccer cell that measured large — three days or fewer
# between matches, against an opponent on four or more.
SHORT_REST_DAYS = 3.0
RESTED_DAYS = 4.0
SKIP = frozenset({Sport.TENNIS})


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def rest_days(conn: sqlite3.Connection, sport: str, team: str, start_time: datetime) -> Optional[float]:
    """Days since the team's previous recorded game, or None when unknown."""
    start = _aware(start_time)
    floor = start - timedelta(days=MAX_REST_DAYS)
    row = conn.execute(
        """
        SELECT MAX(start_time) FROM games
        WHERE sport = ? AND (home = ? OR away = ?)
          AND start_time < ? AND start_time > ?
        """,
        (sport, team, team, db.iso(start), db.iso(floor)),
    ).fetchone()
    if not row or not row[0]:
        return None
    previous = _aware(datetime.fromisoformat(row[0]))
    return round((start - previous).total_seconds() / 86400.0, 2)


def record_rest(conn: sqlite3.Connection, cards: list[GameCard], now: datetime) -> int:
    """Upsert rest days for every recorded card; returns rows written."""
    written = 0
    for card in cards:
        g = card.game
        if g.sport in SKIP or g.completed:
            continue
        home_rest = rest_days(conn, g.sport.value, g.home, g.start_time)
        away_rest = rest_days(conn, g.sport.value, g.away, g.start_time)
        if home_rest is None and away_rest is None:
            continue
        db.upsert_game_context(
            conn, g.id, g.sport.value, now, rest_home=home_rest, rest_away=away_rest,
        )
        written += 1
    return written


def short_rest_side(rest_home: Optional[float], rest_away: Optional[float]) -> Optional[str]:
    """'home' / 'away' when exactly one side is on short rest, else None."""
    if rest_home is None or rest_away is None:
        return None
    if rest_home <= SHORT_REST_DAYS and rest_away >= RESTED_DAYS:
        return "home"
    if rest_away <= SHORT_REST_DAYS and rest_home >= RESTED_DAYS:
        return "away"
    return None

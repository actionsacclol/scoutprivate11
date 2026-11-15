"""The record of the daily card: what was actually posted, and how it did.

The picks table holds every pick the app ever derived — a hundred and fifty
on a college Saturday. The record a reader cares about is the handful that
were SENT. `save` writes those down when a post goes out; `summary` grades
them from the final scores in the games table.

Grading is the game's result, nothing else: the picked side won, lost, or
tied. A tie is a push, except in soccer, where the draw is its own outcome
and a team that drew did not win. It does not depend on any pick row
surviving.

Units are one unit staked on every pick at the price a reader could really
have paid: the cheapest exchange after its fee, as shown in the post. A pick
no exchange listed is staked at its fair price (the probability shown), which
is what "no edge claimed" means in money. A win at cost c pays 1/c - 1.

A game is recorded once. Sending twice in a day adds the new games and leaves
the first record of the others alone, so a later price cannot rewrite it.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from kryptscout.store import db

# The most rows one post can record (a card is at most 12).
MAX_ROWS = 25


def _unit(value: Any) -> Optional[float]:
    """A probability or share price strictly between 0 and 1, else None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 < number < 1.0 else None


def _row(pick: dict) -> Optional[tuple]:
    """One card row as stored, or None when it cannot be graded later."""
    game_id, side = pick.get("game_id"), pick.get("side")
    if not game_id or side not in ("home", "away"):
        return None
    venue = pick.get("best")
    quote = (pick.get("exchanges") or {}).get(venue) if venue else None
    quote = quote if isinstance(quote, dict) else {}
    return (
        str(game_id),
        str(pick.get("sport") or ""),
        side,
        str(pick.get("team") or ""),
        str(pick.get("tier") or ""),
        _unit(pick.get("prob")),
        str(venue) if venue and quote else None,
        _unit(quote.get("price")),
        _unit(quote.get("cost")),
    )


def save(card_date: date, picks: list[dict], now: Optional[datetime] = None) -> int:
    """Record the picks of a card that was posted on `card_date` (the
    sender's local date). Returns how many were new."""
    posted_at = db.iso(now or datetime.now(timezone.utc))
    rows = [r for r in (_row(p) for p in picks[:MAX_ROWS] if isinstance(p, dict)) if r]
    if not rows:
        return 0
    conn = db.connect()
    try:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO card_picks
                (game_id, sport, side, team, tier, prob, venue, price, cost,
                 card_date, posted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(*r, card_date.isoformat(), posted_at) for r in rows],
        )
        conn.commit()
        return conn.total_changes - before
    finally:
        conn.close()


def _outcome(row: dict) -> Optional[str]:
    """'win' | 'loss' | 'push', or None while the game is not final."""
    if not row.get("completed") or row.get("home_score") is None or row.get("away_score") is None:
        return None
    home, away = int(row["home_score"]), int(row["away_score"])
    if home == away:
        return "loss" if row.get("sport") == "soccer" else "push"
    return "win" if (home > away) == (row["side"] == "home") else "loss"


def _tally(rows: list[dict]) -> dict:
    out = {"wins": 0, "losses": 0, "pushes": 0, "pending": 0, "units": 0.0}
    for row in rows:
        outcome = _outcome(row)
        if outcome is None:
            out["pending"] += 1
        elif outcome == "push":
            out["pushes"] += 1
        elif outcome == "win":
            out["wins"] += 1
            paid = row.get("cost") or row.get("prob")
            out["units"] += (1.0 / paid - 1.0) if paid else 0.0
        else:
            out["losses"] += 1
            out["units"] -= 1.0
    out["units"] = round(out["units"], 2)
    return out


def summary(today: Optional[date] = None) -> dict:
    """{"overall": {...}, "last": {...} | None}.

    `overall` tallies every recorded card pick, with `since` the first card's
    date. `last` is the most recent card day BEFORE `today` on which at least
    one pick has been graded — "yesterday", or Friday's on a Monday.
    """
    today = today or datetime.now(timezone.utc).date()
    conn = db.connect()
    try:
        rows = db.rows(
            conn,
            """
            SELECT c.game_id, c.sport, c.side, c.prob, c.cost, c.card_date,
                   g.home_score, g.away_score, g.completed
            FROM card_picks c
            LEFT JOIN games g ON g.game_id = c.game_id
            ORDER BY c.card_date
            """,
        )
    finally:
        conn.close()
    overall = _tally(rows)
    overall["since"] = rows[0]["card_date"] if rows else None
    last = None
    days = sorted({r["card_date"] for r in rows if r["card_date"] < today.isoformat()}, reverse=True)
    for day in days:
        tally = _tally([r for r in rows if r["card_date"] == day])
        if tally["wins"] + tally["losses"] + tally["pushes"] > 0:
            last = {"date": day, **tally}
            break
    return {"overall": overall, "last": last}

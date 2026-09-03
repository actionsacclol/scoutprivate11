"""Backfill ESPN's pre-game predictor for settled games.

ESPN serves the predictor document for completed events, frozen before
kickoff, so the benchmark on the PERFORMANCE page can be built over history
instead of waiting for it. Same shape as jobs/lines.py: newest first,
batched, idempotent (one row per game, replaced on re-fetch).

    python -m kryptscout.jobs.predictor                # every settled game
    python -m kryptscout.jobs.predictor --sport wnba --limit 200
    python -m kryptscout.jobs.predictor --refresh      # re-fetch existing rows
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from kryptscout.core.schemas import Game, Sport
from kryptscout.datasources import espn_predictor
from kryptscout.store import db

logger = logging.getLogger("kryptscout")

BATCH = 100


def _pending(conn, sport: str | None, refresh: bool, limit: int | None) -> list[Game]:
    where = ["g.completed = 1"]
    params: list = []
    if sport:
        where.append("g.sport = ?")
        params.append(sport)
    if not refresh:
        where.append("NOT EXISTS (SELECT 1 FROM espn_predictions e WHERE e.game_id = g.game_id)")
    sql = f"""
        SELECT g.game_id, g.sport, g.league, g.start_time, g.home, g.away
        FROM games g
        WHERE {' AND '.join(where)}
        ORDER BY g.start_time DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    out: list[Game] = []
    for rec in db.rows(conn, sql, tuple(params)):
        try:
            game = Game(
                id=rec["game_id"], sport=Sport(rec["sport"]), league=rec["league"] or "",
                start_time=rec["start_time"], home=rec["home"], away=rec["away"], completed=True,
            )
        except (ValueError, KeyError):
            continue
        if game.sport in espn_predictor.SUPPORTED:
            out.append(game)
    return out


def backfill(sport: str | None = None, refresh: bool = False, limit: int | None = None) -> dict[str, int]:
    """Returns {"considered": n, "fetched": n, "pre_game": n}."""
    conn = db.connect()
    try:
        games = _pending(conn, sport, refresh, limit)
        considered = len(games)
        fetched = pre_game = 0
        for start in range(0, len(games), BATCH):
            chunk = games[start:start + BATCH]
            found = asyncio.run(espn_predictor.fetch_predictors(chunk))
            with conn:
                for pred in found.values():
                    db.upsert_espn_prediction(conn, pred)
                    fetched += 1
                    pre_game += int(pred.pre_game)
            logger.info("espn predictor: %s/%s games processed", min(start + BATCH, considered), considered)
        return {"considered": considered, "fetched": fetched, "pre_game": pre_game}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", help="restrict to one sport key (e.g. mlb)")
    parser.add_argument("--refresh", action="store_true", help="re-fetch games that already have a row")
    parser.add_argument("--limit", type=int, help="newest N games only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(backfill(sport=args.sport, refresh=args.refresh, limit=args.limit))


if __name__ == "__main__":
    main()

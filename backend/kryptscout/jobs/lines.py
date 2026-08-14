"""Backfill each game's opening and closing moneyline from ESPN.

Closing-line value is the only measurement that separates a real edge from a
hot streak, and until now this app could only compute it from `odds_snapshots`
— which requires that the app happened to be running for that game, on that
book, both early and again near kickoff. It usually wasn't: of 283 recorded
picks, 11 had the snapshot coverage to be scored.

ESPN publishes the book's own declared open and close in the same odds
document the board already reads, and keeps serving it long after the game
settles. So CLV does not have to accrue — it can be backfilled over every
game already in the store, and over any past season worth walking.

    python -m kryptscout.jobs.lines                    # every settled game
    python -m kryptscout.jobs.lines --picks-only       # only games we picked
    python -m kryptscout.jobs.lines --sport mlb --limit 200
    python -m kryptscout.jobs.lines --refresh          # re-fetch existing rows

Idempotent: rows are keyed (game_id, book) and replaced, so re-running only
costs requests. ESPN is unauthenticated and unmetered, but be a good citizen —
the fetcher caps concurrency and the job batches.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from kryptscout.core.schemas import Game, Sport
from kryptscout.odds import espn_odds
from kryptscout.store import db

logger = logging.getLogger("kryptscout")

BATCH = 100


def _pending(
    conn, sport: str | None, picks_only: bool, refresh: bool, limit: int | None
) -> list[Game]:
    """Settled games that still need endpoints, newest first.

    Newest first because ESPN's retention is the binding constraint: if the
    archive thins out at some age, we want to have banked the recent seasons
    before finding the floor.
    """
    where = ["g.completed = 1"]
    params: list = []
    if sport:
        where.append("g.sport = ?")
        params.append(sport)
    if picks_only:
        where.append("EXISTS (SELECT 1 FROM picks k WHERE k.game_id = g.game_id)")
    if not refresh:
        where.append(
            "NOT EXISTS (SELECT 1 FROM line_endpoints e WHERE e.game_id = g.game_id)"
        )
    sql = f"""
        SELECT g.game_id, g.sport, g.league, g.start_time, g.home, g.away
        FROM games g
        WHERE {' AND '.join(where)}
        ORDER BY g.start_time DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    for rec in db.rows(conn, sql, tuple(params)):
        try:
            out.append(
                Game(
                    id=rec["game_id"],
                    sport=Sport(rec["sport"]),
                    league=rec["league"] or "",
                    start_time=rec["start_time"],
                    home=rec["home"],
                    away=rec["away"],
                )
            )
        except (ValueError, KeyError):  # a sport we no longer model
            continue
    return out


def backfill(
    sport: str | None = None,
    picks_only: bool = False,
    refresh: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Fetch and store open/close for every qualifying game.

    Returns {"considered": n, "fetched": n, "rows": n} — `fetched` counts games
    ESPN priced, `rows` counts (game, book) pairs written.
    """
    conn = db.connect()
    try:
        games = _pending(conn, sport, picks_only, refresh, limit)
        considered = len(games)
        fetched = rows = 0
        for start in range(0, len(games), BATCH):
            chunk = games[start:start + BATCH]
            found = asyncio.run(espn_odds.fetch_line_endpoints(chunk))
            by_id = {g.id: g for g in chunk}
            with conn:
                for game_id, endpoints in found.items():
                    game = by_id.get(game_id)
                    if game is None:
                        continue
                    fetched += 1
                    for e in endpoints:
                        db.upsert_line_endpoints(
                            conn,
                            game_id=game_id,
                            sport=game.sport.value,
                            book=e.book,
                            fetched_at=e.fetched_at,
                            open_home_ml=e.open_home_ml,
                            open_away_ml=e.open_away_ml,
                            open_draw_ml=e.open_draw_ml,
                            close_home_ml=e.close_home_ml,
                            close_away_ml=e.close_away_ml,
                            close_draw_ml=e.close_draw_ml,
                        )
                        rows += 1
            logger.info(
                "line endpoints: %s/%s games processed", min(start + BATCH, considered),
                considered,
            )
        return {"considered": considered, "fetched": fetched, "rows": rows}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", help="restrict to one sport key (e.g. mlb)")
    parser.add_argument("--picks-only", action="store_true",
                        help="only games that carry a recorded pick")
    parser.add_argument("--refresh", action="store_true",
                        help="re-fetch games that already have endpoints")
    parser.add_argument("--limit", type=int, help="stop after N games")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = backfill(
        sport=args.sport, picks_only=args.picks_only,
        refresh=args.refresh, limit=args.limit,
    )
    print(
        f"line_endpoints: {result['considered']} games considered, "
        f"{result['fetched']} priced by ESPN, {result['rows']} (game, book) rows written"
    )


if __name__ == "__main__":
    main()

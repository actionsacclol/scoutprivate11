"""Backfill job — harvest CLOSED Polymarket sports markets + pre-kick prices.

Usage (from the backend directory):

    python -m kryptscout.jobs.backfill [--sports mlb,nba,...] [--enumerate-only]

Two resumable passes, both polite (~5 req/s) and safe to re-run:

1. ENUMERATE — Gamma /markets?closed=true&sports_market_types=moneyline per
   sport tag, cursored on start_date_min (page size is capped at 100 and
   offsets 422 above ~2046, so the cursor walks startDate ascending). Each
   market upserts into pm_markets with outcomes, clobTokenIds, kickoff,
   resolution (outcomePrices "1"/"0" → winner) and volume.
2. PRICES — for every pm_markets row with prices_fetched=0, one CLOB
   /prices-history call (fidelity=1, window kickoff-25h → kickoff+5m) and
   store the last trade at-or-before each of the T-minus offsets
   (15m/1h/6h/24h) into pm_price_snapshots. interval=max returns EMPTY for
   resolved markets — explicit startTs/endTs is mandatory. The trade's own
   timestamp rides along so consumers can gate on staleness.

This history is the market side of the dollar-ROI strategy backtest; the
model side comes from each engine's walk-forward log (see store/backtest.py).
Sports taker fees when simulating fills: 0.03 before 2026-07-10, 0.05 after
(autobet.engine.SPORTS_TAKER_FEE_RATE tracks the current rate).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from kryptscout.store import db

logger = logging.getLogger("kryptscout")

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
PRICES_URL = "https://clob.polymarket.com/prices-history"

# Gamma tag ids per Scout sport key (verified 2026-07-16 via /tags/slug/*).
# ncaab: only March Madness exists on Gamma (regular seasons absent).
SPORT_TAGS: dict[str, list[int]] = {
    "mlb": [100381],
    "nba": [745],
    "nfl": [450],
    "nhl": [899],
    "wnba": [100254],
    "ncaaf": [100351],
    "ncaab": [100149],  # march-madness
    "tennis": [864],
    "soccer": [306, 780, 1494, 102070, 100100, 102448, 102648],
}

SNAPSHOT_OFFSETS_MIN = (15, 60, 360, 1440)
PAGE_SIZE = 100
CONCURRENCY = 5
_RETRIES = 3


async def _get_json(client: httpx.AsyncClient, url: str, params: dict):
    for attempt in range(_RETRIES):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 429:
                await asyncio.sleep(3.0 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception:  # noqa: BLE001 — retry, then give up quietly
            if attempt == _RETRIES - 1:
                return None
            await asyncio.sleep(1.5 * (attempt + 1))
    return None


def _parse_ts(raw: str | None) -> datetime | None:
    """Gamma timestamps come in several shapes ('...Z', '...+00', '...+00:00');
    Python 3.10 fromisoformat only takes the last — normalize before parsing."""
    if not raw:
        return None
    s = raw.strip().replace("Z", "+00:00")
    if len(s) >= 3 and s[-3] in "+-" and ":" not in s[-3:]:
        s += ":00"  # bare '+00' offset
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _loads(value):
    """Gamma double-encodes list fields (outcomes, clobTokenIds) as JSON strings."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _parse_market(sport: str, m: dict) -> dict | None:
    outcomes = _loads(m.get("outcomes"))
    tokens = _loads(m.get("clobTokenIds"))
    prices = _loads(m.get("outcomePrices"))
    if not (isinstance(outcomes, list) and isinstance(tokens, list)
            and len(outcomes) == 2 and len(tokens) == 2):
        return None
    winner = None
    if isinstance(prices, list) and len(prices) == 2:
        try:
            pa, pb = float(prices[0]), float(prices[1])
            winner = "a" if pa > 0.5 else ("b" if pb > 0.5 else None)
        except (TypeError, ValueError):
            winner = None
    start = m.get("gameStartTime") or m.get("endDate")
    return {
        "condition_id": m.get("conditionId"),
        "sport": sport,
        "slug": m.get("slug"),
        "game_start": start,
        "outcome_a": str(outcomes[0]),
        "outcome_b": str(outcomes[1]),
        "token_a": str(tokens[0]),
        "token_b": str(tokens[1]),
        "winner": winner,
        "volume": m.get("volumeNum"),
        "question": m.get("question"),
    }


async def enumerate_sport(client: httpx.AsyncClient, conn, sport: str) -> int:
    """Cursor every closed moneyline market for a sport into pm_markets."""
    total = 0
    for tag_id in SPORT_TAGS[sport]:
        cursor = "2024-01-01T00:00:00Z"
        while True:
            page = await _get_json(client, GAMMA_URL, {
                "tag_id": tag_id, "closed": "true",
                "sports_market_types": "moneyline",
                "limit": PAGE_SIZE, "order": "startDate", "ascending": "true",
                "start_date_min": cursor,
            })
            if not page:
                break
            rows = [p for p in (_parse_market(sport, m) for m in page) if p]
            with conn:
                for r in rows:
                    if not r["condition_id"]:
                        continue
                    conn.execute(
                        """
                        INSERT INTO pm_markets (condition_id, sport, slug,
                            game_start, outcome_a, outcome_b, token_a, token_b,
                            winner, volume, question)
                        VALUES (:condition_id, :sport, :slug, :game_start,
                                :outcome_a, :outcome_b, :token_a, :token_b,
                                :winner, :volume, :question)
                        ON CONFLICT (condition_id) DO UPDATE SET
                            winner = COALESCE(excluded.winner, pm_markets.winner),
                            volume = COALESCE(excluded.volume, pm_markets.volume),
                            question = COALESCE(excluded.question, pm_markets.question)
                        """,
                        r,
                    )
            total += len(rows)
            if len(page) < PAGE_SIZE:
                break
            # Advance the cursor past this page; startDate ordering makes the
            # max startDate on the page the next lower bound.
            starts = [m.get("startDate") for m in page if m.get("startDate")]
            if not starts or max(starts) == cursor:
                break
            cursor = max(starts)
            await asyncio.sleep(0.2)
    logger.info("backfill enumerate %s: %s markets", sport, total)
    return total


async def fetch_prices(client: httpx.AsyncClient, conn, sport: str | None = None,
                       limit: int | None = None) -> dict:
    """Snapshot pass over pm_markets rows not yet priced. Resumable."""
    where = "prices_fetched = 0 AND game_start IS NOT NULL"
    params: tuple = ()
    if sport:
        where += " AND sport = ?"
        params = (sport,)
    sql = f"SELECT * FROM pm_markets WHERE {where} ORDER BY game_start"
    if limit:
        sql += f" LIMIT {int(limit)}"
    markets = db.rows(conn, sql, params)
    sem = asyncio.Semaphore(CONCURRENCY)
    done = empty = 0

    async def one(m: dict) -> None:
        nonlocal done, empty
        async with sem:
            kick = _parse_ts(m["game_start"])
            if kick is None:
                empty += 1
                return
            start = int((kick - timedelta(hours=25)).timestamp())
            end = int((kick + timedelta(minutes=5)).timestamp())
            payload = await _get_json(client, PRICES_URL, {
                "market": m["token_a"], "startTs": start, "endTs": end,
                "fidelity": 1,
            })
            history = (payload or {}).get("history") or []
            snaps = []
            for offset in SNAPSHOT_OFFSETS_MIN:
                target = int((kick - timedelta(minutes=offset)).timestamp())
                at = [h for h in history if h.get("t") is not None and h["t"] <= target]
                if at:
                    last = at[-1]
                    snaps.append((m["condition_id"], offset, float(last["p"]),
                                  datetime.fromtimestamp(last["t"], tz=timezone.utc).isoformat()))
            with conn:
                for row in snaps:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO pm_price_snapshots
                            (condition_id, offset_minutes, price_a, at_ts)
                        VALUES (?, ?, ?, ?)
                        """,
                        row,
                    )
                conn.execute(
                    "UPDATE pm_markets SET prices_fetched = 1 WHERE condition_id = ?",
                    (m["condition_id"],),
                )
            done += 1
            if not snaps:
                empty += 1
            await asyncio.sleep(0.05)

    await asyncio.gather(*(one(m) for m in markets))
    logger.info("backfill prices%s: %s fetched (%s empty) of %s pending",
                f" [{sport}]" if sport else "", done, empty, len(markets))
    return {"pending": len(markets), "fetched": done, "empty": empty}


async def run(sports: list[str] | None = None, enumerate_only: bool = False) -> dict:
    """Both passes for the requested sports (default: every mapped sport)."""
    wanted = sports or list(SPORT_TAGS)
    conn = db.connect()
    summary: dict = {"enumerated": {}, "prices": {}}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            for sport in wanted:
                if sport not in SPORT_TAGS:
                    continue
                summary["enumerated"][sport] = await enumerate_sport(client, conn, sport)
            if not enumerate_only:
                for sport in wanted:
                    if sport not in SPORT_TAGS:
                        continue
                    summary["prices"][sport] = await fetch_prices(client, conn, sport)
    finally:
        conn.close()
    return summary


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sports", type=str, default=None,
                        help="comma list (default: all mapped sports)")
    parser.add_argument("--enumerate-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sports = args.sports.split(",") if args.sports else None
    summary = asyncio.run(run(sports, enumerate_only=args.enumerate_only))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    _main()

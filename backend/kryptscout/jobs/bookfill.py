"""Backfill REAL pre-kickoff order books from the pmxt public archive.

Where jobs/backfill.py stores the LAST TRADE at kickoff-minus-offset (a
guess at the executable price), this job stores the actual top-of-book:
best bid/ask (+ top-3 depth when available), letting store/roibacktest.py
price fills at the true ask (fill="book") instead of last-trade + guessed
tick padding.

Source: the pmxt hourly Parquet archive (CC BY 4.0, attribution in the app's
About page) at https://r2v2.pmxt.dev/polymarket_orderbook_{YYYY-MM-DDTHH}.parquet
— raw CLOB V2 websocket events; coverage starts 2026-04-13T19 (verified by
probe: 404 at T18, 200 at T19). Two event shapes carry top-of-book, and BOTH
are needed (verified live 2026-07-17):

  book          full bids/asks JSON arrays sorted AWAY from the touch (best
                bid = max price, best ask = min price); the parquet
                best_bid/best_ask columns are NULL on these rows. Dense in
                recent months (~28/hour), nearly absent in April–May files.
  price_change  best_bid/best_ask columns POPULATED (no depth); tens of
                thousands per busy token-hour — the only usable source in
                the early archive era.

The last at-or-before-target event of either shape wins; depth arrays are
stored only when that event was a `book`.

Cost reality (measured): each hourly file scan transfers ~50 MiB no matter
how narrow the predicate — the files are time-ordered event streams, so
asset_id pushdown barely prunes. The job therefore groups work by hour-file
(each file scanned ONCE for every token that needs it, reduced to ≤1 row per
target INSIDE duckdb) and refuses big sports without --i-know-the-cost:
WNBA+NBA ≈ ~20 GB transferred once, MLB ≈ 100-150 GB, tennis/soccer
TB-scale. Storage is KBs (SQLite rows only).

Interruption-safe: snapshots upsert after EVERY file and a journal
(DATA_DIR/bookfill_journal.json) records which hour-files have been scanned
for which tokens, so a killed run resumes losing at most one file's work.
Markets are marked pm_markets.books_fetched=1 only when their whole plan has
been scanned.

Run from backend/:  python -m kryptscout.jobs.bookfill
    [--sports wnba,nba] [--offsets 60,15] [--limit N] [--i-know-the-cost]

duckdb is imported lazily and is NOT part of the PyInstaller bundle — this
is a dev-side harvest tool, never a runtime dependency.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from kryptscout.config import DATA_DIR
from kryptscout.store import db

logger = logging.getLogger("kryptscout")

ARCHIVE_URL = "https://r2v2.pmxt.dev/polymarket_orderbook_{stamp}.parquet"
JOURNAL_FILE = DATA_DIR / "bookfill_journal.json"
# First hour with reliable CLOB V2 events (space-separated to match the
# pm_markets.game_start storage format for lexical comparison).
V2_START = "2026-04-13 19:00:00"
DEFAULT_OFFSETS = (60, 15)
# Sports whose whole post-V2 history fits a desktop transfer budget.
CHEAP_SPORTS = ("wnba", "nba")
TOP_LEVELS = 3


def _parse_start(s: str) -> datetime | None:
    """pm_markets.game_start ('2026-07-15 17:00:00+00') → aware datetime."""
    s = (s or "").strip().replace("Z", "+00:00")
    if len(s) >= 3 and s[-3] in "+-" and ":" not in s[-3:]:
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hour_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def _files_for_target(target: datetime) -> list[str]:
    """Hour files that can hold the last event at-or-before `target`.

    The target's own hour usually suffices — except near the top of the hour
    (kickoffs are usually on the hour, putting T-60 exactly at a file
    boundary), where the previous file holds the at-or-before row.
    """
    files = [_hour_stamp(target)]
    if (target.minute * 60 + target.second) < 600:  # first 10 minutes
        files.append(_hour_stamp(target - timedelta(hours=1)))
    return files


def _top_of_book(bids_json: str, asks_json: str) -> tuple[
    Optional[float], Optional[float], Optional[str], Optional[str]
]:
    """(best_bid, best_ask, top-3 bids JSON, top-3 asks JSON) from a `book`
    event's arrays. Arrays are sorted away from the touch; levels are
    [price, size] string pairs."""
    def _levels(raw: str) -> list[tuple[float, float]]:
        try:
            return [(float(p), float(s)) for p, s in json.loads(raw or "[]")]
        except (ValueError, TypeError):
            return []

    bids = sorted(_levels(bids_json), key=lambda x: x[0], reverse=True)
    asks = sorted(_levels(asks_json), key=lambda x: x[0])
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    return (
        best_bid,
        best_ask,
        json.dumps(bids[:TOP_LEVELS]) if bids else None,
        json.dumps(asks[:TOP_LEVELS]) if asks else None,
    )


def _load_journal() -> dict[str, list[str]]:
    """{hour-stamp: [token, ...]} — files already scanned, per token set."""
    try:
        raw = json.loads(JOURNAL_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_journal(journal: dict[str, list[str]]) -> None:
    tmp = JOURNAL_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(journal), encoding="utf-8")
    tmp.replace(JOURNAL_FILE)


def _plan(conn, sports: list[str], offsets: list[int], limit: int) -> tuple[
    dict[str, list[dict]], list[str]
]:
    """(hour-stamp → [{condition_id, token, target, offset}], market ids).

    Selects unfetched post-V2 markets for `sports` and maps every
    (market, offset) to the hour file(s) that can serve it.
    """
    marks = ",".join("?" for _ in sports)
    markets = db.rows(
        conn,
        f"""
        SELECT condition_id, token_a, game_start FROM pm_markets
        WHERE sport IN ({marks}) AND books_fetched = 0
          AND game_start >= ? AND game_start IS NOT NULL
        ORDER BY game_start LIMIT ?
        """,
        (*sports, V2_START, limit),
    )
    by_file: dict[str, list[dict]] = {}
    ids: list[str] = []
    for m in markets:
        kick = _parse_start(m["game_start"])
        if kick is None or not m["token_a"]:
            continue
        ids.append(m["condition_id"])
        for offset in offsets:
            target = kick - timedelta(minutes=offset)
            want = {
                "condition_id": m["condition_id"], "token": m["token_a"],
                "target": target, "offset": offset,
            }
            for stamp in _files_for_target(target):
                by_file.setdefault(stamp, []).append(want)
    return by_file, ids


# Reduce inside duckdb: for each (market, offset) target, the single latest
# at-or-before event that carries top-of-book — either a full `book` event
# (depth arrays, NULL best columns) or a `price_change` (populated best
# columns, no depth). Shipping raw rows to Python would be ~67k rows per busy
# token-hour; this returns ≤ 1 row per target.
_FILE_QUERY = """
WITH targets(cid, tok, off, target_ts) AS (VALUES {values}),
ev AS (
    SELECT asset_id, timestamp_received, event_type, bids, asks,
           best_bid, best_ask
    FROM read_parquet(?)
    WHERE asset_id IN (SELECT DISTINCT tok FROM targets)
      AND (event_type = 'book'
           OR (event_type = 'price_change' AND best_bid IS NOT NULL
               AND best_ask IS NOT NULL))
),
ranked AS (
    SELECT t.cid, t.off, e.event_type, e.timestamp_received, e.bids, e.asks,
           e.best_bid, e.best_ask,
           row_number() OVER (PARTITION BY t.cid, t.off
                              ORDER BY e.timestamp_received DESC) AS rn
    FROM targets t
    JOIN ev e ON e.asset_id = t.tok
       AND e.timestamp_received <= CAST(t.target_ts AS TIMESTAMPTZ)
)
SELECT cid, off, event_type, timestamp_received, bids, asks, best_bid, best_ask
FROM ranked WHERE rn = 1
"""


def _scan_file(duck, url: str, wants: list[dict]) -> dict[tuple[str, int], dict]:
    """One archive file → {(condition_id, offset): snapshot-fields}."""
    values = ",".join("(?, ?, ?, ?)" for _ in wants)
    params: list = []
    for w in wants:
        params.extend([
            w["condition_id"], w["token"], w["offset"],
            w["target"].astimezone(timezone.utc).isoformat(),
        ])
    rows = duck.execute(
        _FILE_QUERY.format(values=values), [*params, url]
    ).fetchall()
    out: dict[tuple[str, int], dict] = {}
    for cid, off, event_type, ts, bids, asks, best_bid, best_ask in rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if event_type == "book":
            bb, ba, top_bids, top_asks = _top_of_book(bids, asks)
        else:  # price_change — top-of-book columns, no depth
            bb = float(best_bid) if best_bid is not None else None
            ba = float(best_ask) if best_ask is not None else None
            top_bids = top_asks = None
        if bb is None and ba is None:
            continue
        out[(cid, int(off))] = {
            "at": ts.astimezone(timezone.utc), "best_bid": bb, "best_ask": ba,
            "top_bids": top_bids, "top_asks": top_asks,
        }
    return out


def run(
    sports: list[str],
    offsets: list[int],
    limit: int = 100_000,
    acknowledged_cost: bool = False,
) -> dict:
    """Harvest book snapshots for every unfetched market of `sports`.

    Returns {markets, snapshots, files_scanned, files_skipped, missing}.
    """
    expensive = [s for s in sports if s not in CHEAP_SPORTS]
    if expensive and not acknowledged_cost:
        raise SystemExit(
            f"{','.join(expensive)}: measured transfer is ~50 MiB per hour-file "
            "— a full harvest of this sport is tens to hundreds of GB. "
            "Re-run with --i-know-the-cost to proceed."
        )
    try:
        import duckdb  # dev-side dependency — deliberately not bundled
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "duckdb is required for the book harvest: pip install duckdb"
        ) from exc

    conn = db.connect()
    try:
        by_file, market_ids = _plan(conn, sports, offsets, limit)
        n_targets = len({(w["condition_id"], w["offset"])
                         for ws in by_file.values() for w in ws})
        if not market_ids:
            logger.info("bookfill: nothing to do (all fetched or pre-V2)")
            return {"markets": 0, "snapshots": 0, "files_scanned": 0,
                    "files_skipped": 0, "missing": 0}
        journal = _load_journal()
        logger.info(
            "bookfill: %s markets / %s targets over %s hour-files "
            "(%s already journaled; ~%s MiB to transfer)",
            len(market_ids), n_targets, len(by_file),
            sum(1 for s, ws in by_file.items()
                if {w["token"] for w in ws} <= set(journal.get(s, []))),
            sum(50 for s, ws in by_file.items()
                if not {w["token"] for w in ws} <= set(journal.get(s, []))),
        )

        duck = duckdb.connect()
        duck.execute("INSTALL httpfs; LOAD httpfs;")
        # best-so-far per (condition_id, offset); files ascend chronologically
        # so a later file's at-or-before candidate always supersedes.
        best: dict[tuple[str, int], dict] = {}
        scanned = skipped = 0
        for i, stamp in enumerate(sorted(by_file), 1):
            wants = by_file[stamp]
            tokens = sorted({w["token"] for w in wants})
            if set(tokens) <= set(journal.get(stamp, [])):
                skipped += 1
                continue
            url = ARCHIVE_URL.format(stamp=stamp)
            try:
                found = _scan_file(duck, url, wants)
            except Exception as exc:  # noqa: BLE001 — a missing hour never aborts
                logger.warning("bookfill: %s failed: %s", stamp, str(exc)[:160])
                continue
            scanned += 1
            best.update(found)  # chronological order → later always fresher
            # Persist after EVERY file: upserts are idempotent, so a killed
            # run resumes (via the journal) losing at most this one file.
            for (cid, off), snap in found.items():
                db.upsert_book_snapshot(
                    conn, condition_id=cid, offset_minutes=off,
                    best_bid_a=snap["best_bid"], best_ask_a=snap["best_ask"],
                    bid_depth_a=snap["top_bids"], ask_depth_a=snap["top_asks"],
                    at_ts=snap["at"].isoformat(),
                )
            conn.commit()
            journal[stamp] = sorted(set(journal.get(stamp, [])) | set(tokens))
            _save_journal(journal)
            if scanned % 10 == 0 or found:
                logger.info(
                    "bookfill: %s/%s files · %s → %s targets hit · %s/%s "
                    "satisfied so far",
                    i, len(by_file), stamp, len(found), len(best), n_targets,
                )

        # Attempted = done, found or not: the archive won't grow new rows for
        # a past hour, so retrying missing targets forever is pure waste.
        conn.executemany(
            "UPDATE pm_markets SET books_fetched = 1 WHERE condition_id = ?",
            [(cid,) for cid in market_ids],
        )
        conn.commit()
        # Journal entries only matter for the finished plan — reset so it
        # can't grow unbounded across future sports/offsets.
        _save_journal({})
        n_written = db.rows(
            conn, "SELECT COUNT(*) AS n FROM pm_book_snapshots"
        )[0]["n"]
        missing = n_targets - len(best)
        logger.info(
            "bookfill done: %s markets · %s targets hit this run · %s missing "
            "· %s snapshot rows total",
            len(market_ids), len(best), missing, n_written,
        )
        return {
            "markets": len(market_ids), "snapshots": len(best),
            "files_scanned": scanned, "files_skipped": skipped,
            "missing": missing,
        }
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sports", default=",".join(CHEAP_SPORTS),
                    help="comma-separated Scout sport keys (default: wnba,nba)")
    ap.add_argument("--offsets", default=",".join(str(o) for o in DEFAULT_OFFSETS),
                    help="kickoff-minus offsets in minutes (default: 60,15)")
    ap.add_argument("--limit", type=int, default=100_000,
                    help="max markets this run (resume later runs)")
    ap.add_argument("--i-know-the-cost", action="store_true",
                    help="required for sports beyond wnba/nba (huge transfers)")
    args = ap.parse_args()
    sports = [s.strip().lower() for s in args.sports.split(",") if s.strip()]
    offsets = [int(o) for o in args.offsets.split(",") if o.strip()]
    result = run(sports, offsets, limit=args.limit,
                 acknowledged_cost=args.i_know_the_cost)
    print(json.dumps(result))


if __name__ == "__main__":
    main()

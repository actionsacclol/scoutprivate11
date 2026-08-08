"""One-off maintenance: collapse historical odds_snapshots into time buckets.

`GET /api/board/{sport}` records on every read, and the renderer polls all
nine sports every 60s — so before db.insert_snapshot bucketed its writes, a
single game accrued a snapshot per book per MINUTE (measured on a real store:
1,313 rows for one MLB game, 41,943 rows across 284 games). The writer no
longer does that, but rows already on disk stay until something collapses
them. That's this job.

It keeps the FRESHEST row in each (game, book, SNAPSHOT_MIN_GAP_MINUTES)
bucket — exactly the row the current writer would have left — and deletes the
rest, then VACUUMs to return the space to the filesystem. Buckets are the same
fixed wall-clock windows db.insert_snapshot uses, so running this leaves the
store in precisely the state it would have been in had the bucketing always
been there.

Line movement and CLV both read this table. Neither loses anything meaningful:
CLV takes the first and last PREGAME snapshot per book, and bucketing only
drops intra-bucket duplicates — the endpoints of the series survive.

Idempotent: a second run deletes nothing.

    python -m kryptscout.jobs.compact [--dry-run] [--no-vacuum]
"""
from __future__ import annotations

import argparse

from kryptscout.store import db

# Rows are partitioned by the same floor(epoch / gap) bucket the writer uses,
# so "what this job keeps" and "what the writer would have kept" are the same
# rule expressed twice — in SQL here, in Python there.
_KEEP_NEWEST_PER_BUCKET = """
SELECT id FROM (
    SELECT id, ROW_NUMBER() OVER (
        PARTITION BY game_id, book,
                     CAST(strftime('%s', fetched_at) AS INTEGER) / :gap
        ORDER BY fetched_at DESC, id DESC
    ) AS rn
    FROM odds_snapshots
) WHERE rn = 1
"""


def compact(dry_run: bool = False, vacuum: bool = True) -> dict[str, int]:
    """Collapse odds_snapshots to one row per (game, book, bucket).

    Returns {"before": n, "kept": n, "deleted": n}. `dry_run` reports the
    counts without touching a row.
    """
    gap = int(db.SNAPSHOT_MIN_GAP_MINUTES * 60)
    conn = db.connect()
    try:
        before = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
        kept = conn.execute(
            f"SELECT COUNT(*) FROM ({_KEEP_NEWEST_PER_BUCKET})", {"gap": gap}
        ).fetchone()[0]
        deleted = before - kept
        if dry_run or deleted <= 0:
            return {"before": before, "kept": kept, "deleted": 0 if dry_run else deleted}
        with conn:
            conn.execute(
                f"DELETE FROM odds_snapshots WHERE id NOT IN ({_KEEP_NEWEST_PER_BUCKET})",
                {"gap": gap},
            )
        if vacuum:
            # Outside the transaction — sqlite refuses to VACUUM inside one.
            conn.execute("VACUUM")
        return {"before": before, "kept": kept, "deleted": deleted}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be removed, change nothing")
    parser.add_argument("--no-vacuum", action="store_true",
                        help="skip the VACUUM (leaves the file size unchanged)")
    args = parser.parse_args()

    result = compact(dry_run=args.dry_run, vacuum=not args.no_vacuum)
    verb = "would remove" if args.dry_run else "removed"
    print(
        f"odds_snapshots: {result['before']} rows -> {result['kept']} kept "
        f"({verb} {result['before'] - result['kept']})"
    )


if __name__ == "__main__":
    main()

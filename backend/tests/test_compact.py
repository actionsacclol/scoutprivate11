"""odds_snapshots compaction — must leave exactly what the writer would have."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from kryptscout.jobs import compact
from kryptscout.store import db

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")


def _bucket_base() -> datetime:
    gap = db.SNAPSHOT_MIN_GAP_MINUTES * 60.0
    return datetime.fromtimestamp(
        math.floor(NOW.timestamp() / gap) * gap, tz=timezone.utc
    )


def _raw_insert(conn, game_id: str, book: str, at: datetime, home_ml: int) -> None:
    """Bypass insert_snapshot's bucketing to recreate a pre-fix store."""
    conn.execute(
        """
        INSERT INTO odds_snapshots
            (game_id, sport, book, fetched_at, home_ml, away_ml)
        VALUES (?, 'nba', ?, ?, ?, 110)
        """,
        (game_id, book, db.iso(at), home_ml),
    )


def test_compact_keeps_freshest_row_per_bucket():
    base = _bucket_base()
    conn = db.connect()
    with conn:
        # 20 per-minute polls spanning two buckets, plus a second book.
        for i in range(20):
            _raw_insert(conn, "nba-1", "dk", base + timedelta(minutes=i), -130 - i)
        _raw_insert(conn, "nba-1", "fanduel", base, -125)
    conn.close()

    result = compact.compact()
    assert result["before"] == 21
    # dk spans buckets 0 (minutes 0-14) and 1 (15-19) -> 2 rows; fanduel -> 1.
    assert result["kept"] == 3
    assert result["deleted"] == 18

    conn = db.connect()
    rows = db.rows(
        conn,
        "SELECT book, fetched_at, home_ml FROM odds_snapshots ORDER BY book, fetched_at",
    )
    conn.close()
    dk = [r for r in rows if r["book"] == "dk"]
    # Freshest in each bucket: minute 14 (-144) and minute 19 (-149).
    assert [r["home_ml"] for r in dk] == [-144, -149]
    assert [r["book"] for r in rows].count("fanduel") == 1


def test_compact_is_idempotent():
    base = _bucket_base()
    conn = db.connect()
    with conn:
        for i in range(10):
            _raw_insert(conn, "nba-2", "dk", base + timedelta(minutes=i), -130)
    conn.close()

    first = compact.compact()
    assert first["deleted"] == 9
    second = compact.compact()
    assert second["deleted"] == 0 and second["kept"] == first["kept"]


def test_dry_run_changes_nothing():
    base = _bucket_base()
    conn = db.connect()
    with conn:
        for i in range(5):
            _raw_insert(conn, "nba-3", "dk", base + timedelta(minutes=i), -130)
    conn.close()

    result = compact.compact(dry_run=True)
    assert result["before"] == 5 and result["kept"] == 1 and result["deleted"] == 0
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 5
    conn.close()


def test_compaction_matches_what_the_writer_would_have_kept():
    """The job's SQL bucket and insert_snapshot's Python bucket are the same
    rule written twice — this pins them together."""
    base = _bucket_base()
    stamps = [base + timedelta(minutes=i) for i in range(0, 40, 3)]

    # Path A: the writer, bucketing as it goes.
    conn = db.connect()
    with conn:
        for i, at in enumerate(stamps):
            db.insert_snapshot(conn, "nba-writer", "nba", "dk", at, home_ml=-130 - i)
    written = db.rows(
        conn,
        "SELECT fetched_at, home_ml FROM odds_snapshots WHERE game_id='nba-writer'"
        " ORDER BY fetched_at",
    )
    # Path B: raw appends, then compaction.
    with conn:
        for i, at in enumerate(stamps):
            _raw_insert(conn, "nba-raw", "dk", at, -130 - i)
    conn.close()

    compact.compact(vacuum=False)

    conn = db.connect()
    compacted = db.rows(
        conn,
        "SELECT fetched_at, home_ml FROM odds_snapshots WHERE game_id='nba-raw'"
        " ORDER BY fetched_at",
    )
    conn.close()
    assert compacted == written

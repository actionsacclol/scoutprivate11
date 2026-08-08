"""Bookfill harvest unit tests — the pure parts only (no network, no duckdb):
top-of-book derivation from pmxt's away-from-touch JSON arrays, hour-file
targeting around file boundaries, and the game_start parser.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from kryptscout.jobs import bookfill


def test_top_of_book_derives_from_away_sorted_arrays():
    # Real pmxt shape: bids ascend from 0.01, asks descend from 0.99 —
    # best bid is the MAX bid price, best ask the MIN ask price.
    bids = json.dumps([["0.01", "28534.1"], ["0.40", "1671.07"], ["0.44", "8240.56"]])
    asks = json.dumps([["0.99", "763492"], ["0.50", "100270"], ["0.46", "3500"]])
    best_bid, best_ask, top_bids, top_asks = bookfill._top_of_book(bids, asks)
    assert best_bid == 0.44 and best_ask == 0.46
    # Top levels are re-sorted best-first.
    assert json.loads(top_bids)[0] == [0.44, 8240.56]
    assert json.loads(top_asks)[0] == [0.46, 3500.0]


def test_top_of_book_tolerates_garbage():
    assert bookfill._top_of_book("not json", "[]") == (None, None, None, None)


def test_files_for_target_adds_previous_hour_near_boundary():
    # Kickoff on the hour puts T-60 exactly at a file boundary — the
    # at-or-before book row lives in the PREVIOUS hour's file.
    on_boundary = datetime(2026, 7, 15, 16, 0, 0, tzinfo=timezone.utc)
    assert bookfill._files_for_target(on_boundary) == [
        "2026-07-15T16", "2026-07-15T15",
    ]
    mid_hour = datetime(2026, 7, 15, 16, 45, 0, tzinfo=timezone.utc)
    assert bookfill._files_for_target(mid_hour) == ["2026-07-15T16"]


def test_parse_start_handles_store_format():
    # pm_markets stores '2026-07-15 17:00:00+00' (space separator, short tz).
    dt = bookfill._parse_start("2026-07-15 17:00:00+00")
    assert dt == datetime(2026, 7, 15, 17, 0, 0, tzinfo=timezone.utc)
    assert bookfill._parse_start("garbage") is None

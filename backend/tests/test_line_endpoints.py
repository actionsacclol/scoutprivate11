"""ESPN open/close endpoints, and the CLV they make measurable.

The confound this file exists to pin: our value picks are priced at Polymarket
and Kalshi, ESPN's close is DraftKings, and an exchange beats a sportsbook's
vigged close on essentially every bet ever placed. Compared naively that reads
as +6.1% closing-line value at a 12-of-12 beat rate — a fake edge with a
t-statistic of 4.6, produced entirely by the spread between two venues. The
corrected cross-venue comparison on the same picks gives +1.5% +/- 1.3%.

If `test_cross_venue_pick_never_counts_as_price_clv` ever fails, that number
has come back.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kryptscout.core.schemas import Sport
from kryptscout.odds import espn_odds
from kryptscout.store import db, tracker

NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")


def _item(open_home="-118", close_home="-117", open_away="+100", close_away="+101",
          provider="DraftKings"):
    def side(o, c):
        out = {}
        if o is not None:
            out["open"] = {"moneyLine": {"american": o, "decimal": 1.5}}
        if c is not None:
            out["close"] = {"moneyLine": {"american": c, "decimal": 1.5}}
        return out
    return {
        "provider": {"name": provider},
        "homeTeamOdds": side(open_home, close_home),
        "awayTeamOdds": side(open_away, close_away),
    }


# ------------------------------------------------------------------- parsing


def test_parses_signed_american_strings():
    (row,) = espn_odds.parse_line_endpoints([_item()], NOW)
    assert row.book == "draftkings"
    assert (row.open_home_ml, row.close_home_ml) == (-118, -117)
    assert (row.open_away_ml, row.close_away_ml) == (100, 101)


def test_even_is_plus_one_hundred():
    (row,) = espn_odds.parse_line_endpoints([_item(open_home="EVEN")], NOW)
    assert row.open_home_ml == 100


def test_half_a_pair_is_dropped():
    """A game still in progress publishes open + current but no close. Half a
    pair measures nothing, so it must not reach the table as a partial row."""
    assert espn_odds.parse_line_endpoints([_item(close_home=None)], NOW) == []


def test_in_play_feed_is_skipped():
    items = [_item(provider="DraftKings Live Odds")]
    assert espn_odds.parse_line_endpoints(items, NOW) == []


def test_garbage_price_does_not_raise():
    assert espn_odds.parse_line_endpoints([_item(open_home="n/a")], NOW) == []


# ------------------------------------------------------------------- storage


def test_upsert_replaces_a_provisional_close():
    conn = db.connect()
    for close in (-110, -125):
        db.upsert_line_endpoints(
            conn, game_id="mlb-1", sport="mlb", book="draftkings", fetched_at=NOW,
            open_home_ml=-105, open_away_ml=-105,
            close_home_ml=close, close_away_ml=105,
        )
    rows = db.rows(conn, "SELECT * FROM line_endpoints")
    conn.close()
    assert len(rows) == 1 and rows[0]["close_home_ml"] == -125


# ----------------------------------------------------------------------- CLV


def _seed(conn, *, entry_book: str, price: int, side: str = "home"):
    db.upsert_game(
        conn, game_id="mlb-1", sport="mlb", league="", start_time=NOW,
        home="Tigers", away="Guardians", home_score=5, away_score=2, completed=True,
    )
    conn.execute(
        "UPDATE games SET settled_at = ? WHERE game_id = 'mlb-1'", (db.iso(NOW),)
    )
    conn.execute(
        """INSERT INTO picks (game_id, sport, model, side, team, book, price, ev,
                              kelly, tier, created_at, result)
           VALUES ('mlb-1','mlb','m',?,'Tigers',?,?,0.05,0.02,'STRONG',?, 'W')""",
        (side, entry_book, price, db.iso(NOW)),
    )
    db.upsert_line_endpoints(
        conn, game_id="mlb-1", sport="mlb", book="draftkings", fetched_at=NOW,
        open_home_ml=-110, open_away_ml=-110,
        close_home_ml=-130, close_away_ml=110,
    )
    conn.commit()


def test_same_book_pick_scores_as_price_clv():
    conn = db.connect()
    _seed(conn, entry_book="draftkings", price=-110)
    conn.close()
    strong = tracker.clv_vs_close()["by_tier"]["STRONG"]
    assert strong["n_same_book"] == 1 and strong["n_cross_venue"] == 0
    # Took -110, closed -130: bought the same side cheaper than the market did.
    assert strong["price_clv_pct"] > 0
    assert strong["beat_close_rate"] == 1.0


def test_cross_venue_pick_never_counts_as_price_clv():
    """The regression guard. A Polymarket entry against a DraftKings close is
    NOT price improvement — the closing book's vig has to come out first, or
    every exchange bet ever placed reads as a winner."""
    conn = db.connect()
    _seed(conn, entry_book="polymarket", price=-110)
    conn.close()
    strong = tracker.clv_vs_close()["by_tier"]["STRONG"]
    assert strong["n_same_book"] == 0, "cross-venue pick leaked into price_clv"
    assert strong["price_clv_pct"] is None
    assert strong["n_cross_venue"] == 1 and strong["fair_ev_pct"] is not None


def test_cross_venue_ev_is_measured_against_the_devigged_close():
    """-110 on a market that closes -130/+110 is +EV once the vig is removed,
    but by far less than the raw price gap suggests."""
    conn = db.connect()
    _seed(conn, entry_book="polymarket", price=-110)
    conn.close()
    strong = tracker.clv_vs_close()["by_tier"]["STRONG"]
    raw_gap = (tracker._decimal(-110) / tracker._decimal(-130) - 1.0) * 100.0
    assert 0 < strong["fair_ev_pct"] < raw_gap


def test_drift_uses_the_devigged_market_at_both_ends():
    conn = db.connect()
    _seed(conn, entry_book="polymarket", price=-110)
    conn.close()
    strong = tracker.clv_vs_close()["by_tier"]["STRONG"]
    # Home went -110 -> -130 against a symmetric open: the market came to us.
    assert strong["drift_prob_pts"] > 0


def test_away_side_indexes_correctly():
    """_side_index counts non-None prices before the pick's side; getting it
    wrong silently scores the opponent's line."""
    conn = db.connect()
    _seed(conn, entry_book="polymarket", price=120, side="away")
    conn.close()
    strong = tracker.clv_vs_close()["by_tier"]["STRONG"]
    # Away drifted -110 -> +110, i.e. lengthened: drift on our side is negative.
    assert strong["drift_prob_pts"] < 0


def test_sport_filter_applies():
    conn = db.connect()
    _seed(conn, entry_book="polymarket", price=-110)
    conn.close()
    assert tracker.clv_vs_close(sport=Sport.NHL)["overall"]["n"] == 0
    assert tracker.clv_vs_close(sport=Sport.MLB)["overall"]["n"] == 1

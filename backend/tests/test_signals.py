"""Model-free signals: true arbs (core/arb) and the exchange-lead reference
(core/lean), their recording, and the same-book CLV that scores the lean.

Both exist because the models measured zero information beyond the closing
line (2026-09-02). The arb needs no forecast at all; the lean is the one
measured signal — Kalshi/Polymarket leading DraftKings by hours — and it is
tracked, not traded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kryptscout.core import arb as arb_mod
from kryptscout.core import lean as lean_mod
from kryptscout.core.schemas import Game, GameCard, MoneylineOdds, Prediction, Sport
from kryptscout.store import db, tracker

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=6)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")


def _q(book, home, away, draw=None, at=None):
    return MoneylineOdds(book=book, fetched_at=at or NOW, home_ml=home, away_ml=away, draw_ml=draw)


def _game(game_id="mlb-1", start=START, sport=Sport.MLB, completed=False, hs=None, as_=None):
    return Game(
        id=game_id, sport=sport, league="", start_time=start, home="Tigers",
        away="Guardians", completed=completed, home_score=hs, away_score=as_,
    )


# ------------------------------------------------------------------------ arb


def test_arb_found_only_when_fee_inclusive_cost_is_below_one():
    # DK home -105 (0.512) + Polymarket away +115 (0.465): gross 2.3%, and
    # still positive after the 0.05·p(1-p) taker fee on the exchange leg.
    odds = [_q("draftkings", -105, -115), _q("polymarket", -130, 115)]
    arb = arb_mod.find_arb("g", Sport.MLB, odds, START)
    assert arb is not None
    assert {(leg.side, leg.book) for leg in arb.legs} == {("home", "draftkings"), ("away", "polymarket")}
    assert arb.implied_sum < 1.0 and arb.cost_sum < 1.0
    assert arb.net_margin_pct < arb.margin_pct  # fees only ever cost
    assert sum(leg.stake_fraction for leg in arb.legs) == pytest.approx(1.0)


def test_arb_that_only_exists_before_fees_is_not_one():
    # Gross sum 0.993 (+0.7%), but the exchange leg's fee costs ~1.25%.
    odds = [_q("draftkings", -105, -115), _q("polymarket", -125, 108)]
    assert arb_mod.find_arb("g", Sport.MLB, odds, START) is None


def test_arb_ignores_in_play_quotes():
    live = _q("polymarket", -130, 115, at=START + timedelta(minutes=30))
    odds = [_q("draftkings", -105, -115), live]
    assert arb_mod.find_arb("g", Sport.MLB, odds, START) is None


def test_arb_three_way_needs_every_side_from_each_book():
    # A two-way Kalshi book cannot cover a soccer market, however good its price.
    odds = [
        _q("draftkings", 150, 200, draw=260),
        _q("polymarket", 140, 230, draw=250),
        _q("kalshi", 300, 300),  # no draw: excluded
    ]
    arb = arb_mod.find_arb("g", Sport.SOCCER, odds, START)
    assert arb is not None
    assert len(arb.legs) == 3
    assert all(leg.book != "kalshi" for leg in arb.legs)


# ----------------------------------------------------------------------- lean


def test_lean_prefers_kalshi_and_points_at_the_underpriced_side():
    odds = [_q("draftkings", -110, -110), _q("kalshi", -120, 112)]
    lean = lean_mod.find_lean("g", Sport.MLB, odds, START)
    assert lean is not None
    assert (lean.side, lean.ref_book, lean.price, lean.agree) == ("home", "kalshi", -110, False)
    assert lean.gap_pts >= lean_mod.MIN_GAP_PTS
    assert lean.ref_prob > lean.book_prob


def test_lean_needs_both_exchanges_to_agree_when_both_quote():
    odds = [_q("draftkings", -110, -110), _q("kalshi", -120, 112), _q("polymarket", 112, -120)]
    assert lean_mod.find_lean("g", Sport.MLB, odds, START) is None
    agreeing = [_q("draftkings", -110, -110), _q("kalshi", -120, 112), _q("polymarket", -118, 110)]
    lean = lean_mod.find_lean("g", Sport.MLB, agreeing, START)
    assert lean is not None and lean.agree is True


def test_lean_ignores_a_thin_reference_and_uses_the_microprice():
    thin = _q("kalshi", -120, 112)
    thin.microprice_home, thin.home_depth_usd, thin.away_depth_usd, thin.spread_pts = 0.545, 30.0, 30.0, 1.0
    assert lean_mod.find_lean("g", Sport.MLB, [_q("draftkings", -110, -110), thin], START) is None
    deep = _q("polymarket", -118, 110)
    deep.microprice_home, deep.home_depth_usd, deep.away_depth_usd, deep.spread_pts = 0.531, 4_000.0, 4_000.0, 1.0
    lean = lean_mod.find_lean("g", Sport.MLB, [_q("draftkings", -110, -110), thin, deep], START)
    assert lean is not None
    assert (lean.ref_book, lean.side) == ("polymarket", "home")
    assert lean.ref_prob == pytest.approx(0.531)


def test_lean_needs_a_real_gap():
    odds = [_q("draftkings", -110, -110), _q("polymarket", -105, -105)]
    assert lean_mod.find_lean("g", Sport.MLB, odds, START) is None


def test_lean_skips_three_way_and_in_play_target():
    soccer = [_q("draftkings", 150, 200, draw=260), _q("kalshi", 140, 210)]
    assert lean_mod.find_lean("g", Sport.SOCCER, soccer, START) is None
    live_dk = [_q("draftkings", -110, -110, at=START + timedelta(minutes=5)), _q("kalshi", -120, 112)]
    assert lean_mod.find_lean("g", Sport.MLB, live_dk, START) is None


# ------------------------------------------------------------ store + scoring


def _card(game, odds, arb=None, lean=None):
    pred = Prediction(
        game_id=game.id, sport=game.sport, model="m", generated_at=NOW,
        home_win_prob=0.55, away_win_prob=0.45,
    )
    return GameCard(game=game, prediction=pred, odds=odds, arb=arb, lean=lean)


def test_lean_is_frozen_at_first_sight_and_scored_against_the_same_book_close():
    # record_cards stamps entries with the wall clock, so the game must start
    # after the real "now" for the entry to count as pre-game.
    real_now = datetime.now(timezone.utc)
    start = real_now + timedelta(hours=6)
    g = _game(start=start)
    first = [_q("draftkings", -110, -110, at=real_now), _q("kalshi", -120, 112, at=real_now)]
    lean = lean_mod.find_lean(g.id, g.sport, first, g.start_time)
    counts = tracker.record_cards([_card(g, first, lean=lean)])
    assert counts["leans"] == 1

    # The line moves toward the lean; the current state refreshes, the entry
    # does not.
    moved = [_q("draftkings", -118, 100, at=real_now), _q("kalshi", -125, 116, at=real_now)]
    lean2 = lean_mod.find_lean(g.id, g.sport, moved, g.start_time)
    tracker.record_cards([_card(g, moved, lean=lean2)])
    conn = db.connect()
    [row] = db.rows(conn, "SELECT * FROM lean_signals")
    assert (row["home_ml"], row["entry_home_ml"], row["entry_side"]) == (-118, -110, "home")
    assert row["locked_at"] is None

    # At kickoff the row freezes: a later state is never written.
    with conn:
        assert db.upsert_lean(conn, lean2, start, start + timedelta(hours=1)) is None
    [row] = db.rows(conn, "SELECT * FROM lean_signals")
    assert row["locked_at"] is not None and row["home_ml"] == -118

    # Settle the game and backfill DraftKings' close: -125 on the lean side.
    with conn:
        db.upsert_game(
            conn, g.id, g.sport.value, g.league, start, g.home, g.away, 5, 2, True,
        )
        db.upsert_line_endpoints(
            conn, game_id=g.id, sport="mlb", book="draftkings",
            fetched_at=start + timedelta(hours=4),
            open_home_ml=-110, open_away_ml=-110, close_home_ml=-125, close_away_ml=105,
        )
    conn.close()
    report = tracker.lean_clv()
    overall = report["overall"]
    assert overall["n"] == 1 and overall["n_moved"] == 1
    # Took -110 at first sight, the book closed -125: same book, vig cancels.
    expected = (tracker._decimal(-110) / tracker._decimal(-125) - 1.0) * 100.0
    assert overall["clv_pct"] == pytest.approx(expected)
    assert overall["hit_rate"] == 1.0 and overall["beat_close_rate"] == 1.0
    assert overall["roi_pct"] == pytest.approx((tracker._decimal(-110) - 1.0) * 100.0)
    assert report["by_reference"][0]["ref_book"] == "kalshi"
    assert overall["significant"] is False


def test_lean_first_seen_after_kickoff_is_not_stored():
    g = _game(start=datetime.now(timezone.utc) - timedelta(hours=1))
    odds = [_q("draftkings", -110, -110, at=g.start_time - timedelta(hours=2)),
            _q("kalshi", -120, 112, at=g.start_time - timedelta(hours=2))]
    lean = lean_mod.find_lean(g.id, g.sport, odds, g.start_time)
    assert lean is not None
    counts = tracker.record_cards([_card(g, odds, lean=lean)])
    assert "leans" not in counts
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM lean_signals").fetchone()[0] == 0
    conn.close()


def test_arb_sightings_log_once_per_bucket():
    g = _game()
    odds = [_q("draftkings", -105, -115), _q("polymarket", -130, 115)]
    arb = arb_mod.find_arb(g.id, g.sport, odds, g.start_time)
    assert arb is not None
    assert tracker.record_cards([_card(g, odds, arb=arb)])["arbs"] == 1
    assert "arbs" not in tracker.record_cards([_card(g, odds, arb=arb)])
    conn = db.connect()
    [row] = db.rows(conn, "SELECT * FROM arb_sightings")
    conn.close()
    assert row["net_margin_pct"] == pytest.approx(arb.net_margin_pct)
    assert '"polymarket"' in row["legs"]

"""Totals (over/under) tests — probability math, edges, tracking, grading."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kryptscout.core import totals
from kryptscout.core.schemas import Game, GameCard, MoneylineOdds, Prediction, Sport
from kryptscout.store import db, tracker

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")


# ------------------------------------------------------------- probability


def test_poisson_half_line_probs():
    # NHL total ~ Poisson(6.2) vs 5.5: P(over) = P(N >= 6).
    p_over, p_under, p_push = totals.outcome_probs(Sport.NHL, 6.2, 5.5)
    assert p_push == 0.0
    assert p_over + p_under == pytest.approx(1.0)
    assert p_over > 0.5  # mean above the line


def test_poisson_integer_line_pushes():
    p_over, p_under, p_push = totals.outcome_probs(Sport.NHL, 6.0, 6.0)
    assert p_push > 0.05  # P(N == 6) is material
    assert p_over + p_under + p_push == pytest.approx(1.0)


def test_mlb_uses_normal_with_measured_sigma():
    # MLB runs are overdispersed vs Poisson (measured 2.27x) — Normal(4.5).
    p_over, p_under, p_push = totals.outcome_probs(Sport.MLB, 8.9, 8.5)
    assert p_push == 0.0
    assert 0.5 < p_over < 0.6  # mean barely above a half line, sigma 4.5


def test_normal_line_probs():
    # NBA total ~ N(228, 19) vs 224.5 -> over is favored but not extreme.
    p_over, p_under, p_push = totals.outcome_probs(Sport.NBA, 228.0, 224.5)
    assert p_push == 0.0
    assert 0.5 < p_over < 0.7
    # Symmetric check: line at the mean is a coin flip.
    p_over2, p_under2, _ = totals.outcome_probs(Sport.NBA, 224.5, 224.5)
    assert p_over2 == pytest.approx(0.5, abs=0.01)


def test_unknown_sport_returns_none():
    assert totals.outcome_probs(Sport.TENNIS, 22.0, 21.5) is None


# ------------------------------------------------------------------- edges


def _card(total_pred: float, line: float, over=-110, under=-110) -> GameCard:
    g = Game(id="nba-1", sport=Sport.NBA, league="", start_time=NOW,
             home="H", away="A")
    pred = Prediction(game_id=g.id, sport=g.sport, model="m", generated_at=NOW,
                      home_win_prob=0.5, away_win_prob=0.5,
                      predicted_total=total_pred)
    # Quoted an hour before the listed kickoff: totals_edges drops anything
    # observed at/after it as an in-play line (core.schemas.is_in_play).
    o = MoneylineOdds(book="draftkings", fetched_at=NOW - timedelta(hours=1),
                      home_ml=-110, away_ml=-110, total_line=line,
                      over_price=over, under_price=under)
    return GameCard(game=g, prediction=pred, odds=[o], edges=[])


def test_totals_edges_lean_matches_model():
    card = _card(total_pred=232.0, line=224.5)
    edges = totals.totals_edges(card)
    assert len(edges) == 2
    assert edges[0].side == "over"  # model total well above the line
    assert edges[0].ev_per_dollar > edges[1].ev_per_dollar
    assert 0 < edges[0].model_prob < 1


def test_small_disagreement_shows_no_edge():
    """Validated 2026-09-02: the board flagged an edge on 66% of games while
    the model sat inside the line's own typical move. A 1.5-point gap on an
    NBA total (floor 3.0) is noise, not an edge."""
    assert totals.totals_edges(_card(total_pred=226.0, line=224.5)) == []
    assert len(totals.totals_edges(_card(total_pred=228.0, line=224.5))) == 2


def test_totals_edges_empty_without_quote_or_total():
    card = _card(232.0, 224.5)
    card.odds[0].total_line = None
    assert totals.totals_edges(card) == []
    card2 = _card(232.0, 224.5)
    card2.prediction.predicted_total = None
    assert totals.totals_edges(card2) == []


# --------------------------------------------------------- track + grading


def _seed_game(game_id="nba-9001", hs=None, as_=None, settled=False):
    conn = db.connect()
    with conn:
        db.upsert_game(conn, game_id, "nba", "", NOW - timedelta(hours=5),
                       "Home Team", "Away Team", hs, as_, settled)
        if settled:
            db.mark_settled(conn, game_id, NOW, hs, as_, True)
    conn.close()


def test_log_and_grade_totals_bet():
    _seed_game()
    row = tracker.log_bet("nba-9001", "over", -110, book="draftkings",
                          stake_units=2.0, line=224.5)
    assert row["team"] == "OVER 224.5" and row["line"] == 224.5
    # Settle at 230 total: over wins.
    _seed_game(hs=118, as_=112, settled=True)
    conn = db.connect()
    graded = tracker._grade_user_bets(conn, Sport.NBA, NOW)
    bets = db.rows(conn, "SELECT * FROM user_bets WHERE id = ?", (row["id"],))
    conn.close()
    assert graded == 1
    assert bets[0]["result"] == "W"


def test_totals_bet_pushes_on_exact_line():
    _seed_game(game_id="nba-9002")
    row = tracker.log_bet("nba-9002", "under", -110, line=230.0)
    _seed_game(game_id="nba-9002", hs=115, as_=115, settled=True)  # total 230
    conn = db.connect()
    tracker._grade_user_bets(conn, Sport.NBA, NOW)
    bets = db.rows(conn, "SELECT result, units FROM user_bets WHERE id = ?", (row["id"],))
    conn.close()
    assert bets[0]["result"] == "P" and bets[0]["units"] == 0.0


def test_totals_bet_requires_line():
    _seed_game(game_id="nba-9003")
    with pytest.raises(ValueError):
        tracker.log_bet("nba-9003", "over", -110)


# -------------------------------------------------------------- CLV coaching


def _raw_bet(conn, game_id, side, price, book, created_at, line=None):
    conn.execute(
        """INSERT INTO user_bets (game_id, sport, side, team, price, book,
           stake_units, created_at, line) VALUES (?, 'nba', ?, 'X', ?, ?, 1, ?, ?)""",
        (game_id, side, price, book, created_at, line),
    )


def _raw_snap(conn, game_id, fetched_at, home_ml=None, away_ml=None,
              total_line=None, book="draftkings"):
    conn.execute(
        """INSERT INTO odds_snapshots (game_id, sport, book, fetched_at,
           home_ml, away_ml, total_line) VALUES (?, 'nba', ?, ?, ?, ?, ?)""",
        (game_id, book, fetched_at, home_ml, away_ml, total_line),
    )


def test_user_clv_beats_and_misses_the_close():
    _seed_game(game_id="nba-9100", hs=110, as_=100, settled=True)
    start = db.iso(NOW - timedelta(hours=5))
    bet_t = db.iso(NOW - timedelta(hours=10))
    close_t = db.iso(NOW - timedelta(hours=6))
    conn = db.connect()
    with conn:
        # Bet home at -105; same book closes -120 -> positive CLV.
        _raw_bet(conn, "nba-9100", "home", -105, "draftkings", bet_t)
        _raw_snap(conn, "nba-9100", close_t, home_ml=-120, away_ml=100)
        # Totals: bet over 224.5, line closes 226.5 -> +2.0 pts in our favor.
        _raw_bet(conn, "nba-9100", "over", -110, "draftkings", bet_t, line=224.5)
        # Totals-only capture (no ML quote) — the ML close must fall back to
        # the freshest snapshot that actually quotes home_ml (-120 above).
        _raw_snap(conn, "nba-9100", db.iso(NOW - timedelta(hours=5, minutes=30)),
                  total_line=226.5)
    conn.close()
    r = tracker.user_clv()
    assert r["overall"]["n_evaluated"] == 2
    ml = next(e for e in r["recent"] if e["side"] == "home")
    # dec(-105)=1.952, dec(-120)=1.833 -> +6.5%
    assert ml["clv_pct"] == pytest.approx(6.5, abs=0.2)
    tot = next(e for e in r["recent"] if e["side"] == "over")
    assert tot["line_move_pts"] == pytest.approx(2.0)
    assert r["overall"]["beat_close"] == 2


def test_user_clv_skips_bets_without_pregame_snapshot():
    _seed_game(game_id="nba-9101", hs=100, as_=90, settled=True)
    conn = db.connect()
    with conn:
        _raw_bet(conn, "nba-9101", "home", -110, None,
                 db.iso(NOW - timedelta(hours=10)))
    conn.close()
    r = tracker.user_clv()
    assert r["overall"]["n_evaluated"] == 0

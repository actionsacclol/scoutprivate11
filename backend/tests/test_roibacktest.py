"""Tests for store.roibacktest — the join and the strategy simulator.

The join runs against a tmp SQLite (pm_markets/pm_price_snapshots) plus a
synthetic {sport}_state.json; the simulator is exercised with hand-built rows
so every dollar figure is verifiable by hand.
"""
from __future__ import annotations

import json

import pytest

from kryptscout import config
from kryptscout.store import db, roibacktest


@pytest.fixture(autouse=True)
def _tmp_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)


def _write_log(tmp_path, sport, rows):
    (tmp_path / f"{sport}_state.json").write_text(
        json.dumps({"model": f"{sport}-x", "backtest": rows}), encoding="utf-8"
    )


def _seed_market(condition_id, sport, game_start, a, b, winner, volume,
                 price_a, at_ts, offset=60):
    conn = db.connect()
    with conn:
        conn.execute(
            """
            INSERT INTO pm_markets (condition_id, sport, slug, game_start,
                outcome_a, outcome_b, token_a, token_b, winner, volume,
                prices_fetched)
            VALUES (?, ?, 'slug', ?, ?, ?, 'ta', 'tb', ?, ?, 1)
            """,
            (condition_id, sport, game_start, a, b, winner, volume),
        )
        conn.execute(
            """
            INSERT INTO pm_price_snapshots (condition_id, offset_minutes,
                price_a, at_ts)
            VALUES (?, ?, ?, ?)
            """,
            (condition_id, offset, price_a, at_ts),
        )
    conn.close()


# ---------------------------------------------------------------------- join


def test_join_matches_by_eastern_date_and_names(tmp_path):
    # Kickoff 2026-06-11 00:10 UTC = 2026-06-10 evening Eastern.
    _write_log(tmp_path, "mlb", [
        ["2026-06-10", 0.58, 1, "New York Yankees", "Boston Red Sox"],
    ])
    _seed_market("0xc1", "mlb", "2026-06-11 00:10:00+00",
                 "New York Yankees", "Boston Red Sox", "a", 50_000.0,
                 0.55, "2026-06-10T23:05:00+00:00")
    out = roibacktest.joined_rows("mlb")
    assert out["stats"]["joined"] == 1
    (row,) = out["rows"]
    assert row["model_home_prob"] == pytest.approx(0.58)
    assert row["price_home"] == pytest.approx(0.55)  # outcome_a is home
    assert row["home_won"] is True


def test_join_orients_flipped_outcomes(tmp_path):
    # Market lists the away side first: price of home = 1 - price_a.
    _write_log(tmp_path, "mlb", [
        ["2026-06-10", 0.58, 0, "New York Yankees", "Boston Red Sox"],
    ])
    _seed_market("0xc2", "mlb", "2026-06-11 00:10:00+00",
                 "Boston Red Sox", "New York Yankees", "a", 50_000.0,
                 0.47, "2026-06-10T23:05:00+00:00")
    out = roibacktest.joined_rows("mlb")
    (row,) = out["rows"]
    assert row["price_home"] == pytest.approx(0.53)
    assert row["home_won"] is False  # winner 'a' = Red Sox = away... home lost


def test_join_gates_thin_stale_and_mismatched(tmp_path):
    _write_log(tmp_path, "mlb", [
        ["2026-06-10", 0.58, 1, "New York Yankees", "Boston Red Sox"],
        ["2026-06-10", 0.61, 1, "Athletics", "Seattle Mariners"],
        ["2026-06-10", 0.52, 0, "Chicago Cubs", "St. Louis Cardinals"],
    ])
    # thin: volume below the floor
    _seed_market("0xt", "mlb", "2026-06-11 00:10:00+00",
                 "New York Yankees", "Boston Red Sox", "a", 200.0,
                 0.55, "2026-06-10T23:05:00+00:00")
    # stale: last trade 3h before the snapshot instant
    _seed_market("0xs", "mlb", "2026-06-11 00:10:00+00",
                 "Athletics", "Seattle Mariners", "a", 50_000.0,
                 0.60, "2026-06-10T20:10:00+00:00")
    # outcome mismatch: market says winner b (Cardinals) but log says... log
    # outcome 0 = home (Cubs) lost = Cardinals won -> market winner 'b' AGREES
    # when outcome_a is home; make it DISAGREE by claiming winner 'a'.
    _seed_market("0xm", "mlb", "2026-06-11 00:10:00+00",
                 "Chicago Cubs", "St. Louis Cardinals", "a", 50_000.0,
                 0.50, "2026-06-10T23:05:00+00:00")
    out = roibacktest.joined_rows("mlb")
    assert out["stats"]["thin"] == 1
    assert out["stats"]["stale"] == 1
    assert out["stats"]["outcome_mismatch"] == 1
    assert out["stats"]["joined"] == 0


def test_join_matches_nicknames(tmp_path):
    _write_log(tmp_path, "wnba", [
        ["2026-06-10", 0.64, 1, "Minnesota Lynx", "Dallas Wings"],
    ])
    _seed_market("0xn", "wnba", "2026-06-11 00:00:00+00",
                 "Lynx", "Wings", "a", 9_000.0,
                 0.62, "2026-06-10T23:10:00+00:00")
    out = roibacktest.joined_rows("wnba")
    assert out["stats"]["joined"] == 1


# ----------------------------------------------------------------- simulator


def _row(date, model_home, price_home, home_won):
    return {"date": date, "home": "H", "away": "A",
            "model_home_prob": model_home, "price_home": price_home,
            "home_won": home_won, "volume": 50_000.0}


def test_simulate_basic_pnl_and_fees():
    # One winning home bet post fee change (rate 0.05). Realistic fill: +1 tick.
    rows = [_row("2026-07-12", 0.60, 0.50, True)]
    out = roibacktest.simulate(rows, min_ev=0.02, stake_mode="fixed",
                               stake_usd=10.0, fill="realistic")
    assert out["n_bets"] == 1 and out["wins"] == 1
    q = 0.51
    shares = 10.0 / q
    fee = shares * 0.05 * q * (1 - q)
    assert out["pnl_usd"] == pytest.approx(shares - 10.0 - fee, abs=0.01)


def test_simulate_fee_era_split():
    # Same bet before the fee change uses rate 0.03 -> higher pnl.
    early = roibacktest.simulate([_row("2026-05-01", 0.60, 0.50, True)],
                                 min_ev=0.02, fill="realistic")
    late = roibacktest.simulate([_row("2026-07-12", 0.60, 0.50, True)],
                                min_ev=0.02, fill="realistic")
    assert early["pnl_usd"] > late["pnl_usd"]


def test_simulate_gates_and_side_selection():
    # Model favors AWAY (home prob .35 -> away .65 at away price .55): the
    # simulator must bet the away side.
    rows = [_row("2026-07-12", 0.35, 0.45, False)]  # away won
    out = roibacktest.simulate(rows, min_ev=0.02, fill="optimistic")
    assert out["n_bets"] == 1 and out["wins"] == 1
    # min_win_prob above the away prob kills it
    gated = roibacktest.simulate(rows, min_ev=0.02, min_win_prob=0.70,
                                 fill="optimistic")
    assert gated["n_bets"] == 0 and gated["skipped"]["gates"] == 1


def test_simulate_min_prob_gates_longshot_prices():
    # Effective fill 0.46 (optimistic): below a 50c floor, above a 40c one.
    rows = [_row("2026-07-12", 0.60, 0.46, True)]
    gated = roibacktest.simulate(rows, min_ev=0.0, min_prob=0.50, fill="optimistic")
    assert gated["n_bets"] == 0 and gated["skipped"]["gates"] == 1
    open_ = roibacktest.simulate(rows, min_ev=0.0, min_prob=0.40, fill="optimistic")
    assert open_["n_bets"] == 1


def test_simulate_book_fill_uses_real_ask():
    # Last trade 0.50 but the real book asks 0.54 for home — the book fill
    # must pay the ask, not the padded last trade.
    row = _row("2026-07-12", 0.65, 0.50, True)
    row["ask_home"], row["bid_home"] = 0.54, 0.52
    out = roibacktest.simulate([row], min_ev=0.0, fill="book")
    assert out["n_bets"] == 1 and out["wins"] == 1
    q = 0.54
    shares = 10.0 / q
    fee = shares * 0.05 * q * (1 - q)
    assert out["pnl_usd"] == pytest.approx(shares - 10.0 - fee, abs=0.01)


def test_simulate_book_fill_skips_uncovered_rows():
    covered = _row("2026-07-12", 0.65, 0.50, True)
    covered["ask_home"], covered["bid_home"] = 0.54, 0.52
    bare = _row("2026-07-13", 0.65, 0.50, True)  # no harvested book
    out = roibacktest.simulate([covered, bare], min_ev=0.0, fill="book")
    assert out["n_bets"] == 1
    assert out["skipped"]["no_book"] == 1


def test_simulate_book_fill_away_side_pays_complement_of_bid():
    # Model favors away; away ask = 1 − home bid = 1 − 0.40 = 0.60.
    row = _row("2026-07-12", 0.30, 0.45, False)  # away won
    row["ask_home"], row["bid_home"] = 0.44, 0.40
    out = roibacktest.simulate([row], min_ev=0.0, fill="book")
    assert out["n_bets"] == 1 and out["wins"] == 1
    q = 0.60
    shares = 10.0 / q
    fee = shares * 0.05 * q * (1 - q)
    assert out["pnl_usd"] == pytest.approx(shares - 10.0 - fee, abs=0.01)


def test_simulate_pessimistic_fill_is_worse():
    rows = [_row("2026-07-12", 0.62, 0.55, True)] * 3
    res = {f: roibacktest.simulate(rows, min_ev=0.0, fill=f)["pnl_usd"]
           for f in ("optimistic", "realistic", "pessimistic")}
    assert res["optimistic"] > res["realistic"] > res["pessimistic"]


def test_simulate_daily_stop_loss_halts_day():
    rows = [
        _row("2026-07-12", 0.70, 0.50, False),  # loses ~$10
        _row("2026-07-12", 0.70, 0.50, True),   # would win — but day is halted
        _row("2026-07-13", 0.70, 0.50, True),   # next day trades again
    ]
    out = roibacktest.simulate(rows, min_ev=0.0, daily_stop_loss_usd=5.0)
    assert out["n_bets"] == 2
    assert out["skipped"]["stopped"] == 1


def test_simulate_confidence_sizing_scales():
    lo = roibacktest.simulate([_row("2026-07-12", 0.55, 0.45, True)],
                              min_ev=0.0, stake_mode="confidence",
                              bankroll_usd=1000.0)
    hi = roibacktest.simulate([_row("2026-07-12", 0.85, 0.70, True)],
                              min_ev=0.0, stake_mode="confidence",
                              bankroll_usd=1000.0, max_prob=0.9)
    assert hi["staked_usd"] > lo["staked_usd"] * 2

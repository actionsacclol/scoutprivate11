"""Kalman margin filter (core/kalman) and temperature scaling (core/calibrate)."""
from __future__ import annotations

import math

import pytest

from kryptscout.core.calibrate import temper
from kryptscout.core.kalman import KalmanBook, KalmanConfig

CFG = KalmanConfig(sigma=15.0, q=3.0, hfa=2.5, init_var=150.0, season_var=30.0)


def test_fresh_teams_split_on_home_advantage_only():
    book = KalmanBook(CFG)
    p = book.expected("A", "B")
    expected = 0.5 * (1 + math.erf((2.5 / math.sqrt(150 + 150 + 225)) / math.sqrt(2)))
    assert p == pytest.approx(expected)
    assert book.expected("A", "B", neutral=True) == pytest.approx(0.5)


def test_update_moves_both_sides_by_their_share_of_variance():
    book = KalmanBook(CFG)
    book.means["A"], book.vars["A"] = 0.0, 300.0  # uncertain team
    book.means["B"], book.vars["B"] = 0.0, 30.0  # well-known team
    pre = book.update("A", "B", 35, 7)  # A wins by 28 at home
    assert pre == pytest.approx(KalmanBook(CFG, {"A": 0.0, "B": 0.0}, {"A": 300.0, "B": 30.0}).expected("A", "B"))
    # A moved far more than B: it had ten times the variance to spend.
    assert book.means["A"] > 10 and abs(book.means["B"]) < 3
    assert book.means["B"] < 0
    # Both sides are more certain than before the game.
    assert book.vars["A"] < 300.0 and book.vars["B"] < 30.0 + CFG.q


def test_blowouts_carry_information_and_variance_shrinks_with_games():
    book = KalmanBook(CFG)
    for _ in range(6):
        book.update("A", "B", 42, 3)
    assert book.expected("A", "B") > 0.95
    assert book.vars["A"] < CFG.init_var / 3


def test_season_boundary_shrinks_and_widens():
    book = KalmanBook(CFG)
    book.means["A"], book.vars["A"] = 20.0, 40.0
    book.new_season()
    assert book.means["A"] == pytest.approx(17.0)
    assert book.vars["A"] == pytest.approx(70.0)


def test_state_roundtrip():
    book = KalmanBook(CFG)
    book.update("A", "B", 21, 20)
    again = KalmanBook.from_state(CFG, book.to_state())
    assert again.means == book.means and again.vars == book.vars
    assert KalmanBook.from_state(CFG, None).means == {}


def test_temperature_scaling_pulls_toward_a_half():
    assert temper(0.80, 1.0) == pytest.approx(0.80)
    assert 0.5 < temper(0.80, 0.7) < 0.80
    assert temper(0.20, 0.7) > 0.20
    assert temper(0.5, 0.7) == pytest.approx(0.5)
    assert temper(0.80, 0.7) + temper(0.20, 0.7) == pytest.approx(1.0)

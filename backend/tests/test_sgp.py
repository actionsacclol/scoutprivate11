"""Soccer same-game grid (core/sgp): the eight test vectors from the archive
fit (2026-09-02), marginal recovery, and the card-level wiring."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kryptscout.core import sgp
from kryptscout.core.schemas import FairPrices, Game, GameCard, MoneylineOdds, Sport

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=6)

# (home, draw, away, over 2.5) -> λh, λa, ρ, then joints. From dc_fit3.py.
VECTORS = [
    ((0.36, 0.28, 0.36, 0.50), (1.3172, 1.3172, -0.0158),
     {"home|over2.5": 0.2082, "home|under2.5": 0.1518, "draw|over2.5": 0.0837, "draw|under2.5": 0.1963,
      "away|over2.5": 0.2082, "away|under2.5": 0.1518, "btts": 0.5469, "1X|under2.5": 0.3482,
      "X2|over2.5": 0.2918, "over1.5": 0.7461, "over3.5": 0.2856}),
    ((0.72, 0.17, 0.11, 0.62), (2.3783, 0.7961, 0.0563),
     {"home|over2.5": 0.5008, "home|under2.5": 0.2192, "draw|over2.5": 0.0595, "draw|under2.5": 0.1105,
      "away|over2.5": 0.0597, "away|under2.5": 0.0503, "btts": 0.5005, "1X|under2.5": 0.3297,
      "X2|over2.5": 0.1192, "over1.5": 0.8234, "over3.5": 0.4001}),
    ((0.12, 0.19, 0.69, 0.58), (0.7857, 2.2028, 0.0256),
     {"home|over2.5": 0.0634, "home|under2.5": 0.0566, "draw|over2.5": 0.0587, "draw|under2.5": 0.1313,
      "away|over2.5": 0.4578, "away|under2.5": 0.2322, "btts": 0.4889, "1X|under2.5": 0.1878,
      "X2|over2.5": 0.5166, "over1.5": 0.7996, "over3.5": 0.3590}),
    ((0.34, 0.33, 0.33, 0.38), (1.0993, 1.0793, -0.1037),
     {"home|over2.5": 0.1635, "home|under2.5": 0.1765, "draw|over2.5": 0.0584, "draw|under2.5": 0.2716,
      "away|over2.5": 0.1582, "away|under2.5": 0.1718, "btts": 0.4615, "1X|under2.5": 0.4482,
      "X2|over2.5": 0.2165, "over1.5": 0.6588, "over3.5": 0.1875}),
    ((0.55, 0.22, 0.23, 0.68), (2.1376, 1.3225, 0.0389),
     {"home|over2.5": 0.4112, "home|under2.5": 0.1388, "draw|over2.5": 0.1095, "draw|under2.5": 0.1105,
      "away|over2.5": 0.1593, "away|under2.5": 0.0707, "btts": 0.6526, "1X|under2.5": 0.2493,
      "X2|over2.5": 0.2688, "over1.5": 0.8600, "over3.5": 0.4685}),
    ((0.45, 0.27, 0.28, 0.52), (1.5481, 1.1686, -0.0149),
     {"home|over2.5": 0.2738, "home|under2.5": 0.1762, "draw|over2.5": 0.0845, "draw|under2.5": 0.1855,
      "away|over2.5": 0.1618, "away|under2.5": 0.1182, "btts": 0.5533, "1X|under2.5": 0.3618,
      "X2|over2.5": 0.2462, "over1.5": 0.7609, "over3.5": 0.3034}),
    ((0.30, 0.30, 0.40, 0.42), (1.0600, 1.2670, -0.0361),
     {"home|over2.5": 0.1488, "home|under2.5": 0.1512, "draw|over2.5": 0.0655, "draw|under2.5": 0.2345,
      "away|over2.5": 0.2057, "away|under2.5": 0.1943, "btts": 0.4821, "1X|under2.5": 0.3857,
      "X2|over2.5": 0.2712, "over1.5": 0.6849, "over3.5": 0.2182}),
    ((0.44, 0.26, 0.30, 0.54), (1.5576, 1.2411, 0.0212),
     {"home|over2.5": 0.2724, "home|under2.5": 0.1676, "draw|over2.5": 0.0900, "draw|under2.5": 0.1700,
      "away|over2.5": 0.1776, "away|under2.5": 0.1224, "btts": 0.5679, "1X|under2.5": 0.3376,
      "X2|over2.5": 0.2676, "over1.5": 0.7711, "over3.5": 0.3222}),
]


@pytest.mark.parametrize("inputs,params,expected", VECTORS)
def test_grid_reproduces_the_archive_fit(inputs, params, expected):
    grid = sgp.build_grid(*inputs)
    assert grid is not None and grid.exact
    assert (grid.lambda_home, grid.lambda_away, grid.rho) == pytest.approx(params, abs=2e-3)
    for key, value in expected.items():
        parts = key.split("|")
        assert grid.prob(*parts) == pytest.approx(value, abs=3e-4), key
    # The marginals come back exactly: that is what the third parameter buys.
    h, d, a, o = inputs
    assert grid.prob("home") == pytest.approx(h, abs=1e-6)
    assert grid.prob("draw") == pytest.approx(d, abs=1e-6)
    assert grid.prob("over2.5") == pytest.approx(o, abs=1e-6)


def test_draw_and_under_is_far_above_the_independent_product():
    """The reason the grid exists: draw & under 2.5 runs ~50% above the
    product of its marginals in the archive, and the grid says so."""
    grid = sgp.build_grid(0.44, 0.26, 0.30, 0.54)
    joint = grid.prob("draw", "under2.5")
    independent = grid.prob("draw") * grid.prob("under2.5")
    assert joint / independent - 1 > 0.35
    assert grid.prob("draw", "over2.5") / (grid.prob("draw") * grid.prob("over2.5")) - 1 < -0.35


def test_extra_pins_from_the_fit():
    even = sgp.build_grid(0.36, 0.28, 0.36, 0.50)
    assert even.grid[0, 0] == pytest.approx(0.0723, abs=3e-4)
    assert even.grid[1, 1] == pytest.approx(0.1240, abs=3e-4)
    assert even.grid[2, 2] == pytest.approx(0.0688, abs=3e-4)
    assert even.prob("12", "over2.5") == pytest.approx(0.4163, abs=3e-4)
    fav = sgp.build_grid(0.72, 0.17, 0.11, 0.62)
    assert fav.grid[0, 0] == pytest.approx(0.0368, abs=3e-4)
    assert fav.prob("home", "over3.5") == pytest.approx(0.3155, abs=3e-4)


def _card(with_total=True, line=2.5, source="polymarket"):
    g = Game(id="soc-1", sport=Sport.SOCCER, league="EPL", start_time=START, home="Arsenal", away="Chelsea")
    odds = [
        MoneylineOdds(book="polymarket", fetched_at=NOW, home_ml=127, away_ml=233, draw_ml=285),
        MoneylineOdds(
            book="draftkings", fetched_at=NOW, home_ml=120, away_ml=220, draw_ml=260,
            total_line=line if with_total else None, over_price=-115, under_price=-105,
        ),
    ]
    fair = FairPrices(
        game_id=g.id, sport=Sport.SOCCER, source=source, sharp=True,
        home=0.44, draw=0.26, away=0.30, fetched_at=NOW, sources={},
    )
    return GameCard(game=g, odds=odds, fair=fair)


def test_card_wiring_needs_a_real_over_under_quote():
    out = sgp.sgp_fair(_card())
    assert out is not None
    assert out.over_book == "draftkings" and out.total_line == 2.5 and out.exact
    assert out.singles["home"] == pytest.approx(0.44, abs=1e-6)
    assert set(out.joints) == {f"{r}|{t}" for r in sgp.RESULTS for t in sgp.TOTALS}
    assert out.joints["draw|under2.5"] > out.singles["draw"] * out.singles["under2.5"]
    assert sgp.sgp_fair(_card(with_total=False)) is None
    assert sgp.sgp_fair(_card(line=3.5)) is None  # the total is never inferred

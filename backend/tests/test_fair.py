"""Sharp fair price (core/fair): the exchange-devigged probability a boost or
promo is scored against, with a flagged sportsbook fallback."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kryptscout.core import fair as fair_mod
from kryptscout.core.oddsmath import remove_vig, remove_vig_power
from kryptscout.core.schemas import MoneylineOdds, Sport

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=6)


def _q(book, home, away, draw=None, at=None):
    return MoneylineOdds(book=book, fetched_at=at or NOW, home_ml=home, away_ml=away, draw_ml=draw)


def test_kalshi_is_preferred_and_flagged_sharp():
    odds = [_q("draftkings", -130, 110), _q("polymarket", -118, 110), _q("kalshi", -120, 112)]
    fair = fair_mod.sharp_fair("g", Sport.MLB, odds, START)
    assert fair is not None
    assert (fair.source, fair.sharp) == ("kalshi", True)
    assert fair.home == pytest.approx(remove_vig(-120, 112)[0])
    assert fair.home + fair.away == pytest.approx(1.0)
    assert set(fair.sources) == {"draftkings", "polymarket", "kalshi"}


def test_sportsbook_fallback_uses_power_devig_and_is_not_sharp():
    odds = [_q("draftkings", -300, 240)]
    fair = fair_mod.sharp_fair("g", Sport.MLB, odds, START)
    assert fair is not None
    assert (fair.source, fair.sharp) == ("draftkings", False)
    assert fair.away == pytest.approx(remove_vig_power(-300, 240)[1])
    assert fair.away < remove_vig(-300, 240)[1]  # the dog gets less, on purpose


def test_in_play_and_partial_quotes_are_ignored():
    live = _q("kalshi", -400, 320, at=START + timedelta(minutes=10))
    half = MoneylineOdds(book="polymarket", fetched_at=NOW, home_ml=-118)
    odds = [live, half, _q("draftkings", -130, 110)]
    fair = fair_mod.sharp_fair("g", Sport.MLB, odds, START)
    assert fair is not None and fair.source == "draftkings"


def test_three_way_match_needs_a_three_way_quote():
    odds = [_q("polymarket", 140, 230), _q("draftkings", 150, 200, draw=260)]
    fair = fair_mod.sharp_fair("g", Sport.SOCCER, odds, START)
    assert fair is not None
    assert (fair.source, fair.sharp) == ("draftkings", False)
    assert fair.draw is not None
    assert fair.home + fair.away + fair.draw == pytest.approx(1.0)


def _book(book, home, away, micro_home, depth, spread=1.0):
    q = _q(book, home, away)
    q.microprice_home = micro_home
    q.home_depth_usd = depth
    q.away_depth_usd = depth
    q.spread_pts = spread
    return q


def test_microprice_beats_devigged_asks_and_thin_book_is_demoted():
    """A deep Polymarket book outranks a thin Kalshi one even though Kalshi is
    first in preference; the fair is the microprice, not the ask devig."""
    thin_kalshi = _book("kalshi", -120, 112, micro_home=0.545, depth=40.0)
    deep_pm = _book("polymarket", -118, 110, micro_home=0.531, depth=5_000.0)
    fair = fair_mod.sharp_fair("g", Sport.MLB, [thin_kalshi, deep_pm, _q("draftkings", -130, 110)], START)
    assert fair is not None
    assert (fair.source, fair.sharp, fair.thin) == ("polymarket", True, False)
    assert fair.home == pytest.approx(0.531)
    assert fair.depth_usd == pytest.approx(5_000.0)
    # Kalshi still reports its own number for context.
    assert fair.sources["kalshi"] == pytest.approx(0.545)


def test_all_exchanges_thin_is_flagged_not_hidden():
    thin = _book("kalshi", -120, 112, micro_home=0.545, depth=40.0)
    fair = fair_mod.sharp_fair("g", Sport.MLB, [thin, _q("draftkings", -130, 110)], START)
    assert fair is not None
    assert (fair.source, fair.thin) == ("kalshi", True)
    wide = _book("polymarket", -118, 110, micro_home=0.531, depth=5_000.0, spread=4.5)
    assert fair_mod.is_thin(wide) is True


def test_nothing_usable_returns_none():
    assert fair_mod.sharp_fair("g", Sport.MLB, [], START) is None
    only_half = [MoneylineOdds(book="kalshi", fetched_at=NOW, away_ml=100)]
    assert fair_mod.sharp_fair("g", Sport.MLB, only_half, START) is None

"""The displayed forecast (core/forecast): market where one exists, else the
model labelled as a pre-line estimate with its measured error."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kryptscout.core import forecast as forecast_mod
from kryptscout.core.schemas import FairPrices, Game, GameCard, Prediction, Sport

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=6)


def _card(sport=Sport.MLB, with_fair=True, with_pred=True, source="kalshi", sharp=True, thin=False):
    g = Game(id="g", sport=sport, league="", start_time=START, home="H", away="A")
    pred = Prediction(
        game_id="g", sport=sport, model="mlb-elo-v3", generated_at=NOW,
        home_win_prob=0.62, away_win_prob=0.38,
    ) if with_pred else None
    fair = FairPrices(
        game_id="g", sport=sport, source=source, sharp=sharp, thin=thin,
        home=0.55, away=0.45, fetched_at=NOW,
    ) if with_fair else None
    return GameCard(game=g, prediction=pred, fair=fair)


def test_market_is_the_forecast_and_the_model_is_context():
    f = forecast_mod.display_forecast(_card())
    assert f is not None
    assert (f.kind, f.source, f.sharp, f.home) == ("market", "kalshi", True, 0.55)
    assert f.model_home == 0.62 and f.error_pts is None


def test_book_fallback_is_still_a_market_but_not_sharp():
    f = forecast_mod.display_forecast(_card(source="draftkings", sharp=False))
    assert (f.kind, f.sharp) == ("market", False)


def test_model_is_labelled_pre_line_with_its_error_band():
    f = forecast_mod.display_forecast(_card(with_fair=False))
    assert (f.kind, f.source, f.home) == ("model", "mlb-elo-v3", 0.62)
    assert f.error_pts == forecast_mod.PRELINE_ERROR_PTS[Sport.MLB]
    assert forecast_mod.display_forecast(_card(sport=Sport.NCAAF, with_fair=False)).error_pts == 16.0


def test_nothing_to_show_without_either():
    assert forecast_mod.display_forecast(_card(with_fair=False, with_pred=False)) is None

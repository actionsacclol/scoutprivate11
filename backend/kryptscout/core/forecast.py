"""The displayed forecast — the best number the app can show for a game.

Measured 2026-09-02 (the forecast audit): wherever an exchange quotes a
game, its depth-weighted probability beats every engine here, every variant
tried, and ESPN's own predictor, and no blend with the model survives out of
sample (the model's coefficient beside the market is −0.10, z −0.4). Even a
sportsbook's devigged open beats the model in every sport with a sample. So:

  market   any full-market fair (core/fair.py) — Kalshi, then Polymarket,
           then DraftKings flagged — is the forecast; the model is context.
  model    before any venue quotes, the engine's (recalibrated) probability
           is shown as a PROVISIONAL pre-line estimate carrying its measured
           error against the eventual open, so the reader knows how much to
           trust it. Never a pick.

Switching the displayed number to the market cut held-out log loss by 0.013
(MLB), 0.028 (WNBA) and 0.067 (tennis) versus the raw model; the exchange is
available for roughly 70–85% of games, from ~7h out in the daily leagues.
"""
from __future__ import annotations

from typing import Optional

from kryptscout.core.schemas import Forecast, GameCard, Sport

# Mean |model − eventual DraftKings open| in probability points, measured on
# the live store where the sport had a sample (MLB 3.5, WNBA 8.4, soccer 10.1,
# NFL 9.6 preseason, NCAAF 16.2 weeks out). Unmeasured sports carry the
# nearest measured sibling's band; revisit when their seasons produce rows.
PRELINE_ERROR_PTS: dict[Sport, float] = {
    Sport.MLB: 4.0,
    Sport.WNBA: 8.0,
    Sport.NBA: 8.0,
    Sport.SOCCER: 10.0,
    Sport.NFL: 10.0,
    Sport.NCAAF: 16.0,
    Sport.NCAAB: 16.0,
    Sport.NHL: 4.0,
    Sport.TENNIS: 10.0,
}


def display_forecast(card: GameCard) -> Optional[Forecast]:
    """Market where one exists, else the model labelled as pre-line; None with neither."""
    fair = card.fair
    pred = card.prediction
    model_home = pred.home_win_prob if pred else None
    model_away = pred.away_win_prob if pred else None
    model_draw = pred.draw_prob if pred else None
    if fair is not None:
        return Forecast(
            game_id=card.game.id,
            sport=card.game.sport,
            kind="market",
            source=fair.source,
            sharp=fair.sharp,
            thin=fair.thin,
            home=fair.home,
            away=fair.away,
            draw=fair.draw,
            model_home=model_home,
            model_away=model_away,
            model_draw=model_draw,
        )
    if pred is not None:
        return Forecast(
            game_id=card.game.id,
            sport=card.game.sport,
            kind="model",
            source=pred.model,
            sharp=False,
            thin=False,
            home=pred.home_win_prob,
            away=pred.away_win_prob,
            draw=pred.draw_prob,
            error_pts=PRELINE_ERROR_PTS.get(card.game.sport),
            model_home=model_home,
            model_away=model_away,
            model_draw=model_draw,
        )
    return None

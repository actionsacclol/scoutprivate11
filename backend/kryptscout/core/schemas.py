"""Unified schemas shared by every sport engine.

Every sport — NBA, NHL, NFL, soccer, tennis — reduces to the same contract:
a scheduled event between two sides, a model-estimated win probability for
each side, an optional predicted total score, and the market's moneyline.
The terminal renders these uniformly regardless of sport.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class Sport(str, enum.Enum):
    NBA = "nba"
    NHL = "nhl"
    NFL = "nfl"
    MLB = "mlb"
    WNBA = "wnba"
    NCAAF = "ncaaf"
    NCAAB = "ncaab"
    SOCCER = "soccer"
    TENNIS = "tennis"


class Game(BaseModel):
    """A scheduled event. `home`/`away` map to player1/player2 for tennis."""

    id: str
    sport: Sport
    league: str = ""  # e.g. "EPL" for soccer, "ATP" for tennis; blank for US majors
    start_time: datetime
    home: str
    away: str
    # Bowls, March Madness, Super Bowl, international games: engines zero
    # their home-advantage term when set.
    neutral_site: bool = False
    # Set post-game for model-accuracy tracking
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    completed: bool = False


class MoneylineOdds(BaseModel):
    """One book's moneyline snapshot for a game (American odds)."""

    book: str
    fetched_at: datetime
    home_ml: Optional[int] = None
    away_ml: Optional[int] = None
    draw_ml: Optional[int] = None  # soccer three-way markets
    total_line: Optional[float] = None  # over/under points line
    over_price: Optional[int] = None
    under_price: Optional[int] = None


def _aware(moment: datetime) -> datetime:
    """Naive datetimes are UTC by convention everywhere in this codebase."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def is_in_play(quote: MoneylineOdds, starts_at: Optional[datetime] = None) -> bool:
    """True when a quote reflects a game already under way.

    Every model probability this app produces is strictly PRE-GAME. Comparing
    one against a live price does not measure market inefficiency — it
    measures the game state the model cannot see. A side down 20 in the third
    quarter quotes -1000 while the model still holds the opening coin flip,
    and the gap reads as an enormous "edge" that is really just the score.

    Live prices reach us two ways, and both are common:

    * **Dedicated in-play feeds.** ESPN lists a book's live feed as its own
      provider ("DraftKings Live Odds"), which lands as its own book key. In
      our own capture every row from `draftkings_live_odds`/`draftkings_live`
      was post-kickoff, and same-minute home lines diverged from the pre-game
      feed by as much as -380 vs -100000.
    * **Ordinary books quoted after kickoff.** Polymarket and Kalshi trade
      straight through a game, so any snapshot of theirs taken after the
      first pitch is an in-play price with no special name to give it away.

    `starts_at` is the game's kickoff; pass it whenever it is known. Without
    it only the named-feed check applies.
    """
    if "live" in quote.book.lower():
        return True
    if starts_at is None:
        return False
    return _aware(quote.fetched_at) >= _aware(starts_at)


class Prediction(BaseModel):
    """Model output for one game. Probabilities sum to 1 (incl. draw if set)."""

    game_id: str
    sport: Sport
    model: str  # engine identifier, e.g. "nba-xgb-v1"
    generated_at: datetime
    home_win_prob: float = Field(ge=0.0, le=1.0)
    away_win_prob: float = Field(ge=0.0, le=1.0)
    draw_prob: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # Score / totals predictions (None when a model doesn't produce them)
    predicted_home_score: Optional[float] = None
    predicted_away_score: Optional[float] = None
    predicted_total: Optional[float] = None
    over_prob: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class Edge(BaseModel):
    """Model vs. market: where our probability beats the vig-free implied line."""

    game_id: str
    sport: Sport
    side: str  # "home" | "away" | "draw" | "over" | "under"
    book: str
    price: int  # American odds offered
    model_prob: float
    market_prob: float  # vig-removed implied probability
    ev_per_dollar: float  # expected profit per $1 staked at model_prob
    kelly_fraction: float  # full-Kelly stake fraction (cap at 0.25-0.5x in practice)


class GameCard(BaseModel):
    """Everything the terminal needs to render one row: game + model + market."""

    game: Game
    prediction: Optional[Prediction] = None
    odds: list[MoneylineOdds] = []
    edges: list[Edge] = []
    # Over/under edges (core/totals.py) — separate from `edges` on purpose:
    # totals have no walk-forward validation yet, so they never feed
    # picks/autobet until they earn it. Display + manual tracking only.
    totals_edges: list[Edge] = []

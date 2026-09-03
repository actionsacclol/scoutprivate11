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
    # Exchange top-of-book context (odds/polymarket, odds/kalshi), live only
    # and never persisted. `microprice_home` is the size-weighted fair
    # probability of HOME read off the touch — (ask·bid_size + bid·ask_size)
    # / (bid_size + ask_size) — and `*_depth_usd` is the notional resting at
    # each side's executable price. core/fair.py prefers a deep microprice
    # over a devigged ask pair and flags a thin book instead of trusting it.
    microprice_home: Optional[float] = None
    home_depth_usd: Optional[float] = None
    away_depth_usd: Optional[float] = None
    spread_pts: Optional[float] = None  # ask − bid, probability points


class LineEndpoints(BaseModel):
    """A book's OPENING and CLOSING moneyline for one game.

    Distinct from MoneylineOdds on purpose: a snapshot is "the price at time
    t", and two of those are only the endpoints of a series if we happened to
    poll at the right moments. These are the book's own declared open and
    close, which is what closing-line value is actually defined against.

    Why this exists: CLV is the only measurement that can tell a real edge
    from a hot streak, and computing it from our own `odds_snapshots` requires
    the app to have been running — polling that game, on that book, both early
    and near kickoff. It usually wasn't. ESPN publishes both endpoints per
    event and keeps serving them long after the game finishes, so this turns
    CLV from "start collecting and wait months" into a backfill.
    """

    book: str
    fetched_at: datetime
    open_home_ml: Optional[int] = None
    open_away_ml: Optional[int] = None
    open_draw_ml: Optional[int] = None
    close_home_ml: Optional[int] = None
    close_away_ml: Optional[int] = None
    close_draw_ml: Optional[int] = None

    def complete(self) -> bool:
        """Both endpoints present on both sides — the minimum for a CLV row."""
        return None not in (
            self.open_home_ml, self.open_away_ml,
            self.close_home_ml, self.close_away_ml,
        )


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
    ev_per_dollar: float  # expected profit per $1 staked at the reference probability
    kelly_fraction: float  # full-Kelly stake fraction (cap at 0.25-0.5x in practice)
    # Value edges (store/picks.compute_edges) are priced against the SHARP
    # FAIR, not the model: `fair_prob` is the exchange's probability the EV
    # is measured against, `ref_book` which exchange, `gap_pts` how far the
    # book's price sits below it, `agree` whether the other exchange (when
    # quoting) pointed the same way. `model_prob` stays the model's own
    # number, for context. Totals edges leave these None.
    fair_prob: Optional[float] = None
    ref_book: Optional[str] = None
    gap_pts: Optional[float] = None
    agree: Optional[bool] = None


class ArbLeg(BaseModel):
    """One side of a locked-in position: the best price on it, and its cost."""

    side: str  # "home" | "away" | "draw"
    book: str
    price: int  # American odds
    implied: float  # raw implied probability of the price
    cost: float  # dollars per $1 of payout, taker fee included
    stake_fraction: float  # share of the total stake that goes on this leg


class Arb(BaseModel):
    """True arbitrage across venues (core/arb.py): every outcome bought for
    less than $1 of payout. Deterministic — no model involved."""

    game_id: str
    sport: Sport
    legs: list[ArbLeg]
    implied_sum: float  # sum of raw implied probabilities (< 1 gross)
    cost_sum: float  # sum of fee-inclusive costs (< 1 means a real arb)
    margin_pct: float  # gross return %, before exchange taker fees
    net_margin_pct: float  # locked-in return % after fees — the one that counts


class Lean(BaseModel):
    """Exchange lead (core/lean.py): the DraftKings side the exchanges say is
    underpriced. A measured reference, tracked against the close — not a
    pick, and never fed to autobet."""

    game_id: str
    sport: Sport
    side: str  # "home" | "away"
    book: str  # the target book (DraftKings)
    price: int  # the target book's American price on `side`
    home_ml: int  # the target book's full pair at this moment
    away_ml: int
    book_prob: float  # target book's devigged (power) probability of `side`
    ref_book: str  # "kalshi" | "polymarket"
    ref_prob: float  # the reference's devigged probability of `side`
    gap_pts: float  # ref_prob − book_prob, probability points
    agree: bool  # both exchanges were quoting and pointed the same way


class FairPrices(BaseModel):
    """Sharp fair probabilities per side (core/fair.py): the exchange-devigged
    price — the reference that led DraftKings' close 64% of the time — with a
    sportsbook's power-devigged pair as a flagged fallback. What an odds boost
    or promo is scored against. Never a pick."""

    game_id: str
    sport: Sport
    source: str  # book the fair came from
    sharp: bool  # True when `source` is an exchange (kalshi / polymarket)
    home: float
    away: float
    draw: Optional[float] = None
    fetched_at: datetime
    # Every full-market book's devigged HOME probability, for context.
    sources: dict[str, float] = {}
    # Order-book context of the source when it is an exchange: `thin` means
    # the touch held less than core.fair.THIN_DEPTH_USD or the spread was
    # wider than WIDE_SPREAD_PTS — a stale or empty book, not a reference.
    thin: bool = False
    depth_usd: Optional[float] = None
    spread_pts: Optional[float] = None


class SgpFair(BaseModel):
    """Soccer same-game joint probabilities (core/sgp.py): result × total from
    the sharp fair 1X2 and a real over/under 2.5 quote, Dixon-Coles low-score
    dependence plus a high-score diagonal. Keeps an SGP fair honest; never a
    pick."""

    game_id: str
    sport: Sport
    source: str  # the 1X2 fair's book
    over_book: str  # the book whose O/U 2.5 quote set the total
    total_line: float
    fair_over: float
    lambda_home: float
    lambda_away: float
    rho: float
    delta: float
    exact: bool
    # Singles by mask name (home/draw/away/1X/X2/12, over/under 1.5/2.5/3.5,
    # btts/no_btts) and every result|total pair.
    singles: dict[str, float] = {}
    joints: dict[str, float] = {}


class Forecast(BaseModel):
    """The probability the app DISPLAYS for a game (core/forecast.py).

    `kind` is "market" when a full-market fair exists (Kalshi, Polymarket,
    or DraftKings flagged not-sharp) — measured 2026-09-02 to beat every
    engine, every variant tried and ESPN's predictor, with no blend
    surviving out of sample — and "model" before any venue quotes, where the
    engine's probability is shown as a provisional pre-line estimate with
    its measured error band. The model's own numbers ride along as context.
    """

    game_id: str
    sport: Sport
    kind: str  # "market" | "model"
    source: str  # book key, or the engine's model name
    sharp: bool = False
    thin: bool = False
    home: float
    away: float
    draw: Optional[float] = None
    error_pts: Optional[float] = None  # model kind: mean |model − eventual open|
    model_home: Optional[float] = None
    model_away: Optional[float] = None
    model_draw: Optional[float] = None


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
    # Model-free signals. `arb` is a locked-in cross-venue position (rare);
    # `lean` is the exchange-lead reference. Both are display + tracking.
    arb: Optional[Arb] = None
    lean: Optional[Lean] = None
    # Sharp fair per side for the boost evaluator (core/fair.py).
    fair: Optional[FairPrices] = None
    # Soccer same-game joint table (core/sgp.py).
    sgp: Optional[SgpFair] = None
    # What the terminal shows as the win probability (core/forecast.py).
    forecast: Optional[Forecast] = None

"""Pick derivation — Python port of the frontend's src/picks.ts.

A "pick" is the single recommended bet for one game: the highest-EV moneyline
edge when live odds exist (a VALUE pick, tiered STRONG/LEAN by EV), otherwise
the model's argmax-probability side (a MODEL pick, no book or price attached).

This mirrors derivePick() in src/picks.ts exactly so the picks the backend
records for ROI tracking are the same picks users see in the terminal:
  - completed games and games without a prediction yield no pick;
  - over/under edges are excluded — picks answer "what team wins", and
    totals don't resolve to a team;
  - ties break the same way (first-seen edge / side wins on equal EV/prob,
    matching the TS reduce semantics).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from kryptscout.core import oddsmath
from kryptscout.core.schemas import (
    Edge,
    GameCard,
    MoneylineOdds,
    Prediction,
    Sport,
    is_in_play,
)

# EV threshold (per $1) separating STRONG from LEAN value picks (picks.ts).
STRONG_EV = 0.04
# EV per $1 above which an "edge" is treated as a model artifact, not value.
MAX_PLAUSIBLE_EV = 0.40
# Claimed EV above this demotes a value pick STRONG → LEAN: the live graded
# record (7/12–7/21) shows claimed EV anti-selects — picks under 10% EV ran
# +6.3u (52% win) while every bucket above lost money (EV≥0.30 won 30%). A
# huge claimed edge against a liquid market is evidence the MODEL is wrong.
SUSPECT_EV = 0.15
# Sports whose model has measured NEGATIVE alpha vs the market (tennis:
# −18.2% walk-forward ROI, −13.4u live 7/12–7/21, CLV ≈ 0, probabilities
# ~11pp overconfident). Their picks never carry a value tier — the argmax
# MODEL pick is all the model has honestly earned.
NO_VALUE_SPORTS = frozenset({"tennis"})

_ML_SIDES = ("home", "away", "draw")


@dataclass(frozen=True)
class Pick:
    """The store-relevant slice of a derived pick (see src/picks.ts Pick)."""

    game_id: str
    sport: str
    model: str
    side: str  # "home" | "away" | "draw"
    team: str  # resolved display name; "DRAW" for soccer three-ways
    tier: str  # "STRONG" | "LEAN" | "MODEL"
    model_prob: float
    book: Optional[str] = None  # value picks only
    price: Optional[int] = None  # American odds at that book (value picks only)
    ev: Optional[float] = None  # expected profit per $1 (value picks only)
    kelly: Optional[float] = None  # full-Kelly fraction (value picks only)


def side_name(side: str, card: GameCard) -> str:
    """Resolve a pick side to its display name (picks.ts pickSideName)."""
    if side == "home":
        return card.game.home
    if side == "away":
        return card.game.away
    return "DRAW"


def derive_pick(card: GameCard) -> Optional[Pick]:
    """The single recommended bet for one game, or None (picks.ts derivePick).

    Value pick: the highest-EV moneyline edge across all books; STRONG at
    ev >= STRONG_EV, LEAN otherwise (edges are +EV by construction).
    Model pick: the argmax-probability side, tier MODEL, no book/price.
    """
    game, prediction = card.game, card.prediction
    if game.completed or prediction is None:
        return None

    # ---- value pick: best moneyline edge across all books -------------------
    # Edges above MAX_PLAUSIBLE_EV are almost always model artifacts (e.g. a
    # promoted club priced +900 against fallback probabilities), never real
    # market inefficiency — treat them as no edge and fall through to MODEL.
    ml_edges = [
        e for e in card.edges
        if e.side in _ML_SIDES and e.ev_per_dollar <= MAX_PLAUSIBLE_EV
    ] if game.sport.value not in NO_VALUE_SPORTS else []
    if ml_edges:
        best = max(ml_edges, key=lambda e: e.ev_per_dollar)  # first wins ties
        return Pick(
            game_id=game.id,
            sport=game.sport.value,
            model=prediction.model,
            side=best.side,
            team=side_name(best.side, card),
            tier="STRONG" if STRONG_EV <= best.ev_per_dollar <= SUSPECT_EV else "LEAN",
            model_prob=best.model_prob,
            book=best.book,
            price=best.price,
            ev=best.ev_per_dollar,
            kelly=best.kelly_fraction,
        )

    # ---- model pick: argmax probability, no book attached --------------------
    probs = [("home", prediction.home_win_prob), ("away", prediction.away_win_prob)]
    if prediction.draw_prob is not None:
        probs.append(("draw", prediction.draw_prob))
    side, prob = max(probs, key=lambda sp: sp[1])  # first wins ties
    return Pick(
        game_id=game.id,
        sport=game.sport.value,
        model=prediction.model,
        side=side,
        team=side_name(side, card),
        tier="MODEL",
        model_prob=prob,
    )


def compute_edges(
    game_id: str,
    sport: Sport,
    pred: Prediction,
    odds_list: list[MoneylineOdds],
    starts_at: datetime | None = None,
) -> list[Edge]:
    """Model-vs-market +EV edges for one game, sorted by EV descending.

    The single edge computation behind both the board (api/main) and the
    capture job, so picks derived from either path see identical edges: per
    book, devig the full moneyline market (draw included when both the book
    and the model price it) and keep every side where the model's probability
    makes the offered price +EV.

    In-play quotes are dropped (core.schemas.is_in_play): our probabilities
    are strictly pre-game, so an edge measured against a live price is the
    scoreboard, not market inefficiency. Pass `starts_at` — the game's
    kickoff — wherever it is known; a game already under way then yields no
    value edges at all and falls through to a MODEL pick, which is the honest
    answer once the price the model was arguing with no longer exists.
    """
    edges: list[Edge] = []
    for ml in odds_list:
        if ml.home_ml is None or ml.away_ml is None:
            continue
        if is_in_play(ml, starts_at):
            continue
        prices = [ml.home_ml, ml.away_ml]
        sides = [("home", pred.home_win_prob), ("away", pred.away_win_prob)]
        if ml.draw_ml is not None and pred.draw_prob is not None:
            prices.append(ml.draw_ml)
            sides.append(("draw", pred.draw_prob))
        fair = oddsmath.remove_vig(*prices)
        for (side, model_prob), price, market_prob in zip(sides, prices, fair):
            ev = oddsmath.expected_value(model_prob, price)
            # Implausible EV = model artifact, not market inefficiency — never
            # display it as an edge anywhere (VALUE board, cards, drawers).
            # The 1e-9 floor keeps float dust (market-consensus fills devig to
            # exactly the market) from minting zero-EV edges.
            if ev <= 1e-9 or ev > MAX_PLAUSIBLE_EV:
                continue
            edges.append(
                Edge(
                    game_id=game_id,
                    sport=sport,
                    side=side,
                    book=ml.book,
                    price=price,
                    model_prob=model_prob,
                    market_prob=market_prob,
                    ev_per_dollar=ev,
                    kelly_fraction=oddsmath.kelly_fraction(model_prob, price),
                )
            )
    edges.sort(key=lambda e: e.ev_per_dollar, reverse=True)
    return edges


MARKET_MODEL = "market-consensus"
# Engines emit this confidence sentinel when they have NO information (e.g.
# tennis unknown-player 0.5/0.5). At or below it, the market speaks instead.
NO_INFO_CONFIDENCE = 0.1


def market_consensus_probs(
    odds_list: list[MoneylineOdds], want_draw: bool
) -> tuple[float, float, float | None] | None:
    """Devigged (home, away, draw) probabilities from the best available book.

    Polymarket first (deep, ~vig-free — the general consensus), then Kalshi,
    then any book quoting both sides. None when nothing usable is quoted.
    """
    ranked = sorted(
        (ml for ml in odds_list if ml.home_ml is not None and ml.away_ml is not None),
        key=lambda ml: {"polymarket": 0, "kalshi": 1}.get(ml.book, 2),
    )
    for ml in ranked:
        if want_draw and ml.draw_ml is not None:
            h, a, d = oddsmath.remove_vig(ml.home_ml, ml.away_ml, ml.draw_ml)
            return h, a, d
        if not want_draw:
            h, a = oddsmath.remove_vig(ml.home_ml, ml.away_ml)
            return h, a, None
    return None


def apply_market_fallback(
    pred: Prediction | None, odds_list: list[MoneylineOdds]
) -> Prediction | None:
    """Replace a no-information prediction with the market consensus.

    A model that doesn't rate the participants (confidence <= the sentinel)
    must not display a 50/50 shrug next to a -1700 market price — that reads
    as wrong AND manufactures huge fake edges on the longshot side. Instead:
    adopt the devigged consensus (Polymarket preferred), relabeled
    "market-consensus" so tracking grades it separately from the real model.
    With no odds to lean on, the prediction is dropped entirely (an honest
    blank beats a fake coin flip).
    """
    if pred is None:
        return None
    if pred.confidence is None or pred.confidence > NO_INFO_CONFIDENCE:
        return pred
    probs = market_consensus_probs(odds_list, want_draw=pred.draw_prob is not None)
    if probs is None:
        return None
    home, away, draw = probs
    return pred.model_copy(
        update={
            "model": MARKET_MODEL,
            "home_win_prob": home,
            "away_win_prob": away,
            "draw_prob": draw,
            "confidence": 0.15,
        }
    )


def derive_picks(cards: list[GameCard]) -> list[Pick]:
    """Picks for a slate; cards without a derivable pick are skipped."""
    picks = []
    for card in cards:
        pick = derive_pick(card)
        if pick is not None:
            picks.append(pick)
    return picks

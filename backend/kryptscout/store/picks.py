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
from kryptscout.core.fair import EXCHANGES, is_thin
from kryptscout.core.schemas import (
    Edge,
    FairPrices,
    GameCard,
    MoneylineOdds,
    Prediction,
    Sport,
    is_in_play,
)

# VALUE PICKS ARE PRICED AGAINST THE SHARP FAIR, NOT THE MODEL (2026-09-02).
# Every test this repo has run says the model's disagreement with the market
# carries no information: claimed +9% EV measured +0.55% (median 0) against
# the Kalshi fair, and tailing model-vs-market picks returns the vig. The one
# replicating signal is that the exchanges lead DraftKings. This exact rule,
# replayed capture-by-capture over 45 days of the live store (656 games, 475
# with both a DraftKings and an exchange quote): 59 fires, 1.4 a day, 12% of
# eligible games; the same-book close then moved toward the pick by
# +1.75 ± 0.45 pts (59% toward, 10% flat, 31% against; Kalshi-led +1.85,
# Polymarket-led +1.34). Realized ROI on the 51 graded: −14.6%, which at
# n=51 is noise (σ ≈ 13 pts) but is NOT evidence of profit — the edge sits
# under DraftKings' vig. So a value edge is a SPORTSBOOK price sitting at
# least MIN_GAP_PTS below the sharp fair; exchanges never qualify against
# their own fair; STRONG means the reference was Kalshi (the stronger lead),
# LEAN Polymarket alone. The tier is honest about being a closing-line
# signal, not a profit.
MIN_GAP_PTS = 1.0
# EV per $1 above which an "edge" is treated as a model artifact, not value.
MAX_PLAUSIBLE_EV = 0.40
# Claimed EV above this demotes a value pick STRONG → LEAN: the live graded
# record (7/12–7/21) shows claimed EV anti-selects — picks under 10% EV ran
# +6.3u (52% win) while every bucket above lost money (EV≥0.30 won 30%). A
# huge claimed edge against a liquid market is evidence the MODEL is wrong.
SUSPECT_EV = 0.15
# Sports whose model has measured NEGATIVE alpha vs the market. Their picks
# never carry a value tier — the argmax MODEL pick is all the model has
# honestly earned. Mirrored in src/picks.ts.
#   tennis  −18.2% walk-forward ROI, −13.4u live 7/12–7/21, CLV ≈ 0,
#           probabilities ~11pp overconfident.
#   soccer  STRONG −11.0% walk-forward vs football-data closes (n=2,129, CI
#           entirely below zero, 2026-07-27); live through 2026-09-02 the
#           value picks ran −4.2% at midpoint prices (n=166) with same-book
#           CLV −0.8% ± 0.6 (n=75), and capping a slate to its best edges
#           returned the same ROI at every cap — a zero-information ranker.
NO_VALUE_SPORTS = frozenset({"tennis", "soccer"})

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
    model_prob: float  # the MODEL's probability of the pick side (context)
    book: Optional[str] = None  # value picks only
    price: Optional[int] = None  # American odds at that book (value picks only)
    ev: Optional[float] = None  # expected profit per $1 at the fair (value picks only)
    kelly: Optional[float] = None  # full-Kelly fraction at the fair (value picks only)
    fair_prob: Optional[float] = None  # the sharp fair the value edge is priced against
    ref_book: Optional[str] = None  # which exchange set that fair
    prob: Optional[float] = None  # the headline probability shown (fair, else forecast)


def side_name(side: str, card: GameCard) -> str:
    """Resolve a pick side to its display name (picks.ts pickSideName)."""
    if side == "home":
        return card.game.home
    if side == "away":
        return card.game.away
    return "DRAW"


def derive_pick(card: GameCard) -> Optional[Pick]:
    """The single recommended bet for one game, or None (picks.ts derivePick).

    Value pick: the best moneyline edge — a sportsbook price beating the
    sharp fair (compute_edges) — STRONG when Kalshi set the fair, LEAN when
    Polymarket did; a claimed EV above SUSPECT_EV demotes to LEAN (a huge
    gap to an exchange is a stale exchange, not a gift).
    Model pick: the displayed forecast's favourite (the market's where one
    exists, else the model's), tier MODEL, no book/price.
    """
    game, prediction = card.game, card.prediction
    if game.completed or prediction is None:
        return None
    side_probs = {
        "home": prediction.home_win_prob,
        "away": prediction.away_win_prob,
        "draw": prediction.draw_prob,
    }

    # ---- value pick: best sharp-fair edge across the sportsbooks ------------
    ml_edges = [
        e for e in card.edges
        if e.side in _ML_SIDES and e.ev_per_dollar <= MAX_PLAUSIBLE_EV and e.fair_prob is not None
    ] if game.sport.value not in NO_VALUE_SPORTS else []
    if ml_edges:
        best = max(ml_edges, key=lambda e: e.ev_per_dollar)  # first wins ties
        strong = best.ref_book == "kalshi" and best.ev_per_dollar <= SUSPECT_EV
        return Pick(
            game_id=game.id,
            sport=game.sport.value,
            model=prediction.model,
            side=best.side,
            team=side_name(best.side, card),
            tier="STRONG" if strong else "LEAN",
            model_prob=best.model_prob,
            book=best.book,
            price=best.price,
            ev=best.ev_per_dollar,
            kelly=best.kelly_fraction,
            fair_prob=best.fair_prob,
            ref_book=best.ref_book,
            prob=best.fair_prob,
        )

    # ---- model pick: the displayed forecast's favourite, no book attached ---
    forecast = card.forecast
    if forecast is not None:
        shown = {"home": forecast.home, "away": forecast.away, "draw": forecast.draw}
        ref = forecast.source if forecast.kind == "market" else None
    else:
        shown = side_probs
        ref = None
    probs = [(s, p) for s, p in shown.items() if p is not None]
    side, prob = max(probs, key=lambda sp: sp[1])  # first wins ties
    return Pick(
        game_id=game.id,
        sport=game.sport.value,
        model=prediction.model,
        side=side,
        team=side_name(side, card),
        tier="MODEL",
        model_prob=side_probs.get(side) if side_probs.get(side) is not None else prob,
        fair_prob=prob if ref else None,
        ref_book=ref,
        prob=prob,
    )


def compute_edges(
    game_id: str,
    sport: Sport,
    pred: Prediction,
    odds_list: list[MoneylineOdds],
    starts_at: datetime | None = None,
    fair: FairPrices | None = None,
) -> list[Edge]:
    """Sportsbook-vs-sharp-fair edges for one game, sorted by EV descending.

    The single edge computation behind both the board (api/main) and the
    capture job. A value edge needs: a SHARP fair (an exchange, not thin —
    core/fair.py), a pre-game quote from a SPORTSBOOK that prices the full
    market (an exchange can never beat its own fair), and the fair sitting at
    least MIN_GAP_PTS above the price's implied probability. EV and Kelly are
    computed at the fair; the model's own probability rides along as context.
    When both exchanges quote a two-way market they must agree the price is
    above fair, else there is no edge (a disagreement is no reference);
    `agree` is True when a second exchange confirmed, None when only one
    exchange quoted.

    In-play quotes are dropped (core.schemas.is_in_play): a live price
    against a pre-game reference is the scoreboard, not inefficiency.
    """
    if fair is None or not fair.sharp or fair.thin or sport.value in NO_VALUE_SPORTS:
        return []
    fair_probs = {"home": fair.home, "away": fair.away, "draw": fair.draw}
    model_probs = {"home": pred.home_win_prob, "away": pred.away_win_prob, "draw": pred.draw_prob}
    others = [b for b in fair.sources if b in EXCHANGES and b != fair.source]
    edges: list[Edge] = []
    for ml in odds_list:
        if ml.book in EXCHANGES or ml.home_ml is None or ml.away_ml is None:
            continue
        if is_in_play(ml, starts_at):
            continue
        prices = [ml.home_ml, ml.away_ml]
        sides = ["home", "away"]
        if fair.draw is not None:
            if ml.draw_ml is None:
                continue  # a 3-way fair needs a 3-way book price
            prices.append(ml.draw_ml)
            sides.append("draw")
        try:
            book_fair = oddsmath.remove_vig_power(*prices)
        except (ValueError, ZeroDivisionError):
            continue
        for side, price, book_prob in zip(sides, prices, book_fair):
            p = fair_probs.get(side)
            if p is None:
                continue
            implied = oddsmath.implied_prob(price)
            gap = (p - implied) * 100.0
            if gap < MIN_GAP_PTS:
                continue
            ev = oddsmath.expected_value(p, price)
            if ev <= 1e-9 or ev > MAX_PLAUSIBLE_EV:
                continue
            agree = True
            for other in others:
                other_home = fair.sources[other]
                other_p = other_home if side == "home" else (1.0 - other_home if fair.draw is None else None)
                if other_p is not None and other_p <= implied:
                    agree = False
            if not agree:
                continue
            edges.append(
                Edge(
                    game_id=game_id,
                    sport=sport,
                    side=side,
                    book=ml.book,
                    price=price,
                    model_prob=model_probs.get(side) if model_probs.get(side) is not None else p,
                    market_prob=book_prob,
                    ev_per_dollar=ev,
                    kelly_fraction=oddsmath.kelly_fraction(p, price),
                    fair_prob=p,
                    ref_book=fair.source,
                    gap_pts=gap,
                    agree=True if others else None,
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

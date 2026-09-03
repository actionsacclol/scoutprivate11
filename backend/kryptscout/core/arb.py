"""True arbitrage — the best price on every side of a market sums below $1.

The one edge check in this app that needs no forecast. If buying every
outcome at the best available price costs less than the $1 the market pays
out, the position is locked in before kickoff, whoever wins. Measured
2026-08-17: DraftKings holds ~4.45% overround while Polymarket and Kalshi hold
~0, so the seam between a sportsbook and the exchanges is where these live —
and they are rare, small, and gone in minutes. This module finds them, the
board shows them, and `tracker.record_cards` logs every sighting so "did we
ever actually see one?" has an answer.

Two honesty rules:

* Pre-game quotes only (core.schemas.is_in_play). An in-play price against a
  pre-game one is the scoreboard, not an arb.
* Exchange legs are costed at the executable ask (odds/polymarket and
  odds/kalshi quote asks since 2026-09-02) AND net of the taker fee —
  Polymarket's sports taker fee is 0.05·p(1−p) per share
  (autobet.engine.SPORTS_TAKER_FEE_RATE), Kalshi's 0.07·p(1−p) per contract;
  sportsbooks charge nothing beyond the price. An "arb" that only exists
  before fees is not one, so only the NET margin can flag it. The gross
  margin is reported for context.

A quote that does not price the whole market (a two-way exchange book on a
three-way soccer match, a one-sided Kalshi event) cannot contribute a leg:
an arb needs every outcome covered.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from kryptscout.core.oddsmath import american_to_decimal, implied_prob
from kryptscout.core.schemas import Arb, ArbLeg, MoneylineOdds, Sport, is_in_play

# Taker fee rate per book: fee per $1 share = rate · p · (1 − p). Books not
# listed charge nothing beyond the quoted price.
TAKER_FEE_RATE: dict[str, float] = {"polymarket": 0.05, "kalshi": 0.07}

_SIDES_2 = ("home", "away")
_SIDES_3 = ("home", "away", "draw")


def leg_cost(book: str, price: int) -> float:
    """Dollars to buy $1 of payout on `price` at `book`, taker fee included."""
    p = implied_prob(price)
    return p + TAKER_FEE_RATE.get(book, 0.0) * p * (1.0 - p)


def find_arb(
    game_id: str,
    sport: Sport,
    odds_list: list[MoneylineOdds],
    starts_at: Optional[datetime] = None,
) -> Optional[Arb]:
    """The locked-in position across `odds_list`, or None when there isn't one.

    Best (highest-paying) price per side across every pre-game quote that
    prices the full market; an arb exists when the fee-inclusive cost of $1
    of payout on every side sums below $1. Stakes are proportional to each
    leg's cost, so every outcome returns the same 1 / cost_sum.
    """
    quotes = [q for q in odds_list if not is_in_play(q, starts_at)]
    sides = _SIDES_3 if any(q.draw_ml is not None for q in quotes) else _SIDES_2
    full = [
        q for q in quotes
        if all(getattr(q, f"{side}_ml") is not None for side in sides)
    ]
    if not full:
        return None
    best: dict[str, tuple[str, int]] = {}
    for side in sides:
        book, price = max(
            ((q.book, getattr(q, f"{side}_ml")) for q in full),
            key=lambda bp: american_to_decimal(bp[1]),
        )
        best[side] = (book, price)
    implied_sum = sum(implied_prob(price) for _, price in best.values())
    cost_sum = sum(leg_cost(book, price) for book, price in best.values())
    if cost_sum >= 1.0:
        return None
    legs = [
        ArbLeg(
            side=side,
            book=book,
            price=price,
            implied=implied_prob(price),
            cost=leg_cost(book, price),
            stake_fraction=leg_cost(book, price) / cost_sum,
        )
        for side, (book, price) in best.items()
    ]
    return Arb(
        game_id=game_id,
        sport=sport,
        legs=legs,
        implied_sum=implied_sum,
        cost_sum=cost_sum,
        margin_pct=(1.0 / implied_sum - 1.0) * 100.0,
        net_margin_pct=(1.0 / cost_sum - 1.0) * 100.0,
    )

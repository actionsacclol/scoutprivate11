"""Odds conversion and value-bet math.

American odds convention: -150 means stake 150 to win 100; +130 means stake
100 to win 130. All EV/Kelly math works in decimal odds internally.
"""
from __future__ import annotations


def american_to_decimal(american: int) -> float:
    if american == 0:
        raise ValueError("American odds of 0 are undefined")
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def decimal_to_american(decimal: float) -> int:
    if decimal <= 1.0:
        raise ValueError("Decimal odds must exceed 1.0")
    if decimal >= 2.0:
        return round((decimal - 1.0) * 100)
    return round(-100.0 / (decimal - 1.0))


def implied_prob(american: int) -> float:
    """Raw implied probability including the book's vig."""
    return 1.0 / american_to_decimal(american)


def remove_vig(*american_prices: int) -> list[float]:
    """Vig-free probabilities for a full market (2-way or 3-way).

    Uses proportional (multiplicative) devigging: normalize raw implied
    probabilities so they sum to 1.
    """
    raw = [implied_prob(p) for p in american_prices]
    total = sum(raw)
    return [r / total for r in raw]


def remove_vig_power(*american_prices: int) -> list[float]:
    """Vig-free probabilities by the POWER method: each raw implied
    probability is raised to the exponent k that makes the market sum to 1.

    `remove_vig` (multiplicative) shaves the same proportion off every side,
    which hands longshots more probability than they deserve — the
    favourite-longshot bias. Measured 2026-09-02 on 277 exchange picks scored
    against the DraftKings close: multiplicative devig reported +0.90% ± 0.20
    of "closing-line value", concentrated entirely in underdogs (+1.8% on
    dogs, 0.0% on favourites); under this method the same picks read
    +0.16% ± 0.20 and the dog/favourite split flipped. The edge was the devig.

    So this is the conservative reference for any claimed edge on a dog, and
    the one `tracker.clv_vs_close` reports alongside the multiplicative
    figure. Bisection on k: the sum is monotone decreasing in k, and 60 halvings
    of [0.05, 20] pin it well past float precision for 2- and 3-way markets.
    An arb market (raw sum < 1) solves to k < 1 rather than raising.
    """
    raw = [implied_prob(p) for p in american_prices]
    if not raw or any(not 0.0 < r < 1.0 for r in raw):
        raise ValueError("Every price must imply a probability strictly in (0, 1)")
    lo, hi = 0.05, 20.0
    for _ in range(60):
        k = (lo + hi) / 2.0
        if sum(r ** k for r in raw) > 1.0:
            lo = k  # still over 1: push probabilities down harder
        else:
            hi = k
    k = (lo + hi) / 2.0
    out = [r ** k for r in raw]
    total = sum(out)  # residual float dust only
    return [v / total for v in out]


def microprice(bid: float, ask: float, bid_size: float, ask_size: float) -> float:
    """Size-weighted fair probability at the touch.

    The microprice leans toward the side with LESS resting size — a thin ask
    is about to be lifted, so fair sits nearer the ask — and it is the
    standard depth-aware midpoint in market microstructure. With equal sizes
    it is the plain mid; with no size information callers should fall back
    to the mid.
    """
    total = bid_size + ask_size
    if total <= 0:
        return (bid + ask) / 2.0
    return (ask * bid_size + bid * ask_size) / total


def expected_value(model_prob: float, american: int) -> float:
    """Expected profit per $1 staked, given our probability and the offered price."""
    dec = american_to_decimal(american)
    return model_prob * (dec - 1.0) - (1.0 - model_prob)


def kelly_fraction(model_prob: float, american: int) -> float:
    """Full-Kelly optimal stake fraction; 0 when there is no edge."""
    dec = american_to_decimal(american)
    b = dec - 1.0
    if b <= 0:
        return 0.0
    f = (model_prob * (b + 1.0) - 1.0) / b
    return max(0.0, f)


def prob_to_american(prob: float) -> int:
    """Fair (no-vig) American price for a probability — the model's 'line'."""
    if not 0.0 < prob < 1.0:
        raise ValueError("Probability must be strictly between 0 and 1")
    return decimal_to_american(1.0 / prob)

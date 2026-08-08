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

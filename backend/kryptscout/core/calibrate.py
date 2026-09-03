"""Post-hoc probability calibration shared by the engines.

Logit scaling: the model's log-odds are multiplied by one constant. Below 1
the probabilities move toward 0.5 (an overconfident model), above 1 away
from it. Fitted on a held-out slice of each engine's own walk-forward log
(2026-09-02) as the slope b of logit(P(win)) = a + b·logit(p): NBA's and
WNBA's Elo overstated favourites by 6–8 points (slopes 0.82 / 0.78, fitted
scale 0.69 for WNBA), the NFL blend by ~3 (0.86 → 0.84); MLB, NHL, soccer
and tennis were already calibrated and take none. This is the reciprocal of
the "divide by T" temperature convention; the engines' constants are the
multiplier. The engines own their constants; this is the mechanism.
"""
from __future__ import annotations

import math

_EPS = 1e-6


def temper(p: float, scale: float) -> float:
    """σ(scale · logit(p)): scale < 1 pulls toward 0.5, scale = 1 is the identity."""
    if scale <= 0 or scale == 1.0:
        return p
    q = min(1.0 - _EPS, max(_EPS, p))
    z = math.log(q / (1.0 - q)) * scale
    return 1.0 / (1.0 + math.exp(-z))

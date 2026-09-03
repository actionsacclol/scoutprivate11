"""Soccer same-game joint distribution — result × total, from the sharp fair.

Why this exists: same-game legs are correlated, and in soccer the
correlation is structural and large. On 7,249 archive matches priced at
Pinnacle's close, draw & under 2.5 happens 49% MORE often than the product
of its marginals, favourite & over 2.5 18% more, draw & over 2.5 40% less.
A naive product misprices every soccer same-game parlay by tens of percent;
the NFL, by contrast, is within ±2% of independent and needs no grid.

What it is NOT: an edge. Every book's same-game engine already prices this
dependence and holds 15–25% on top of it. The grid's job is to keep the
app's own "fair" honest — so an SGP quote or an SGP promo is scored against
the true joint probability rather than an overstated independent one.

Model (fitted 2026-09-02, train 2023-25 → validate 2025-26, scripts in the
session notes): per-match EXACT solve of (λ_home, λ_away, ρ) on the sharp
home / draw / over-2.5 probabilities — two Poisson rates cannot hit 1X2 and
O/U together (the draw price is the casualty, up to 5 pts off), so the
Dixon-Coles low-score parameter ρ is the third unknown — plus a global
high-score diagonal inflation δ = 0.30 (profile 95% CI [0.15, 0.30]) that
the market's own λs need to reproduce 2-2/3-3 draws. Bivariate Poisson was
rejected (shared rate → 0). On validation every result × O/U-2.5 pair sits
inside the observed 95% interval, in all 8 leagues and all 3 favourite-
strength bins. Known biases, documented not hidden: BTTS under-predicted by
~2.5 pts, over 1.5 by ~1 pt, draw & over by ~1.5 pts for favourites above
60%.

Requires a real over/under 2.5 quote: the total is never inferred.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from kryptscout.core.oddsmath import remove_vig_power
from kryptscout.core.schemas import GameCard, SgpFair, Sport, is_in_play

MAX_GOALS = 10
DELTA = 0.30
RHO_INIT = -0.05
LAMBDA_INIT = (1.4, 1.1)
TOL = 1e-9
MAX_ITER = 80
CLIP = (0.01, 0.98)
TOTAL_LINE = 2.5

_K = np.arange(MAX_GOALS + 1)
_LOG_FACT = np.array([math.lgamma(k + 1) for k in _K])
_X = _K[:, None]  # home goals
_Y = _K[None, :]  # away goals

# Every single the app prices, as a boolean mask over the (home, away) grid.
MASKS: dict[str, np.ndarray] = {
    "home": _X > _Y,
    "draw": _X == _Y,
    "away": _X < _Y,
    "1X": _X >= _Y,
    "X2": _X <= _Y,
    "12": _X != _Y,
    "over1.5": _X + _Y >= 2,
    "under1.5": _X + _Y <= 1,
    "over2.5": _X + _Y >= 3,
    "under2.5": _X + _Y <= 2,
    "over3.5": _X + _Y >= 4,
    "under3.5": _X + _Y <= 3,
    "btts": (_X >= 1) & (_Y >= 1),
    "no_btts": ~((_X >= 1) & (_Y >= 1)),
}
RESULTS = ("home", "draw", "away", "1X", "X2", "12")
TOTALS = ("over1.5", "under1.5", "over2.5", "under2.5", "over3.5", "under3.5", "btts", "no_btts")


def _pois(lam: float) -> np.ndarray:
    return np.exp(-lam + _K * math.log(lam) - _LOG_FACT)


def score_grid(lam_home: float, lam_away: float, rho: float, delta: float = DELTA) -> np.ndarray:
    """P(home goals = i, away goals = j) on 0..MAX_GOALS, normalised."""
    grid = np.outer(_pois(lam_home), _pois(lam_away))
    grid[0, 0] *= 1.0 - lam_home * lam_away * rho
    grid[1, 0] *= 1.0 + lam_away * rho
    grid[0, 1] *= 1.0 + lam_home * rho
    grid[1, 1] *= 1.0 - rho
    for i in range(2, MAX_GOALS + 1):
        grid[i, i] *= 1.0 + delta
    return grid / grid.sum()


def rho_bounds(lam_home: float, lam_away: float) -> tuple[float, float]:
    """Dixon-Coles validity: every τ factor must stay positive."""
    return max(-1.0 / lam_home, -1.0 / lam_away), min(1.0 / (lam_home * lam_away), 1.0)


@dataclass
class SgpGrid:
    lambda_home: float
    lambda_away: float
    rho: float
    delta: float
    exact: bool  # False when ρ had to be clamped and the marginals are approximate
    grid: np.ndarray

    def prob(self, *keys: str) -> float:
        """Probability that every named single happens together."""
        mask = np.ones_like(self.grid, dtype=bool)
        for key in keys:
            mask &= MASKS[key]
        return float(self.grid[mask].sum())


def _targets_from(h: float, d: float, a: float, over25: float) -> tuple[float, float, float, float]:
    lo, hi = CLIP
    vals = [min(hi, max(lo, v)) for v in (h, d, a, over25)]
    total = vals[0] + vals[1] + vals[2]
    return vals[0] / total, vals[1] / total, vals[2] / total, vals[3]


def _residuals3(v: np.ndarray, h: float, d: float, o: float) -> np.ndarray:
    grid = score_grid(math.exp(v[0]), math.exp(v[1]), v[2])
    return np.array([
        grid[MASKS["home"]].sum() - h,
        grid[MASKS["draw"]].sum() - d,
        grid[MASKS["over2.5"]].sum() - o,
    ])


def _residuals2(v: np.ndarray, rho: float, diff: float, o: float) -> np.ndarray:
    grid = score_grid(math.exp(v[0]), math.exp(v[1]), rho)
    return np.array([
        grid[MASKS["home"]].sum() - grid[MASKS["away"]].sum() - diff,
        grid[MASKS["over2.5"]].sum() - o,
    ])


def _newton(fn, v0: np.ndarray, clamp=None) -> tuple[np.ndarray, np.ndarray]:
    """Damped Newton with a numeric Jacobian; stdlib + numpy only."""
    v = v0.copy()
    r = fn(v)
    for _ in range(MAX_ITER):
        if np.max(np.abs(r)) < TOL:
            break
        jac = np.empty((len(r), len(v)))
        for j in range(len(v)):
            bump = v.copy()
            bump[j] += 1e-6
            jac[:, j] = (fn(bump) - r) / 1e-6
        try:
            step = np.linalg.solve(jac, -r)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(jac, -r, rcond=None)[0]
        t = 1.0
        while True:
            v_new = v + t * step
            if clamp is not None:
                v_new = clamp(v_new)
            r_new = fn(v_new)
            if np.linalg.norm(r_new) < np.linalg.norm(r) or t < 1e-4:
                break
            t /= 2.0
        v, r = v_new, r_new
    return v, r


def _clamp_rho(v: np.ndarray) -> np.ndarray:
    lo, hi = rho_bounds(math.exp(v[0]), math.exp(v[1]))
    out = v.copy()
    out[2] = min(hi - 1e-6, max(lo + 1e-6, v[2]))
    return out


def build_grid(fair_home: float, fair_draw: float, fair_away: float, fair_over25: float) -> Optional[SgpGrid]:
    """Solve the match's (λ_home, λ_away, ρ) from the sharp fair and return the grid.

    Exact when the three-parameter solve converges with ρ inside the
    Dixon-Coles bounds (100% of the archive). Otherwise ρ is clamped and
    (λ_home, λ_away) re-solved on the home−away gap and the over — the
    draw is then approximate and `exact` is False.
    """
    try:
        h, d, a, o = _targets_from(fair_home, fair_draw, fair_away, fair_over25)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    v0 = np.array([math.log(LAMBDA_INIT[0]), math.log(LAMBDA_INIT[1]), RHO_INIT])
    v, r = _newton(lambda x: _residuals3(x, h, d, o), v0, clamp=_clamp_rho)
    lam_h, lam_a, rho = math.exp(v[0]), math.exp(v[1]), float(v[2])
    exact = bool(np.max(np.abs(r)) < 1e-6)
    if not exact:
        lo, hi = rho_bounds(lam_h, lam_a)
        rho = min(hi - 1e-6, max(lo + 1e-6, rho))
        v2, r2 = _newton(lambda x: _residuals2(x, rho, h - a, o), np.array([v[0], v[1]]))
        if not np.all(np.isfinite(r2)) or np.max(np.abs(r2)) > 1e-4:
            rho = 0.0
            v2, r2 = _newton(lambda x: _residuals2(x, 0.0, h - a, o), np.array([v[0], v[1]]))
            if not np.all(np.isfinite(r2)) or np.max(np.abs(r2)) > 1e-3:
                return None
        lam_h, lam_a = math.exp(v2[0]), math.exp(v2[1])
    return SgpGrid(
        lambda_home=lam_h, lambda_away=lam_a, rho=rho, delta=DELTA, exact=exact,
        grid=score_grid(lam_h, lam_a, rho),
    )


def sgp_fair(card: GameCard) -> Optional[SgpFair]:
    """The card's same-game joint table, or None without a 3-way fair and a
    pre-game over/under 2.5 quote from a book that prices both sides."""
    fair = card.fair
    if fair is None or fair.draw is None or card.game.sport != Sport.SOCCER:
        return None
    quotes = [
        q for q in card.odds
        if q.total_line is not None and abs(float(q.total_line) - TOTAL_LINE) < 1e-9
        and q.over_price and q.under_price and not is_in_play(q, card.game.start_time)
    ]
    if not quotes:
        return None
    quote = next((q for q in quotes if q.book == fair.source), quotes[0])
    try:
        p_over = remove_vig_power(int(quote.over_price), int(quote.under_price))[0]
    except (ValueError, ZeroDivisionError):
        return None
    grid = build_grid(fair.home, fair.draw, fair.away, p_over)
    if grid is None:
        return None
    singles = {key: grid.prob(key) for key in MASKS}
    joints = {f"{res}|{tot}": grid.prob(res, tot) for res in RESULTS for tot in TOTALS}
    return SgpFair(
        game_id=card.game.id,
        sport=card.game.sport,
        source=fair.source,
        over_book=quote.book,
        total_line=TOTAL_LINE,
        fair_over=p_over,
        lambda_home=grid.lambda_home,
        lambda_away=grid.lambda_away,
        rho=grid.rho,
        delta=grid.delta,
        exact=grid.exact,
        singles=singles,
        joints=joints,
    )

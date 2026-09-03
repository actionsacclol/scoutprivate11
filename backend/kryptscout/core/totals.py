"""Totals (over/under) edges — model predicted total vs the market line.

Converting a point-estimate total into P(over) needs a scoring distribution:

  Poisson    low-scoring count sports (NHL, soccer, MLB): total goals/runs ~
             Poisson(mu = predicted_total). Exact pushes at integer lines.
  Normal     high-scoring sports (basketball, football): total ~
             Normal(mu, sigma) with a per-sport sigma from public totals
             research (roughly: NBA ~19, WNBA ~16, NCAAB ~17, NFL ~13.5,
             NCAAF ~15.5). Discretized so integer lines can push.

These are deliberately simple first-cut distributions. VALIDATED 2026-09-02
on the live store, strictly pre-game: on the one sport with a sample (soccer,
n=108) the model total matches DraftKings' closing total on accuracy but its
P(over) adds nothing beyond the market (log loss 0.688 vs 0.679; edges −2.8%
[−26, +20]); pooled, the model's residual coefficient is negative (z −1.3),
and where it disagrees with the market by 10+ probability points the market
is right in both directions. The board had been flagging an edge on 66% of
games — the model sits ~1.9 points from the early line while the line itself
moves ~1.5 — which is a distribution problem, not information. Hence
MIN_DISAGREEMENT below, and totals stay display-only, never picks/autobet.
EV accounts for pushes (refunds): EV = p_win*b - p_lose at net decimal payout b.
"""
from __future__ import annotations

import math
from typing import Optional

from kryptscout.core.oddsmath import american_to_decimal, remove_vig
from kryptscout.core.schemas import Edge, GameCard, Sport, is_in_play

# Normal-total sigma per sport; None = Poisson. MEASURED (2026-07-16) as the
# walk-forward residual std of the engines' rolling-total prediction over the
# cached score history: NBA 18.99 (n=2,553), WNBA 17.65 (n=705), NFL 13.81
# (n=7,174), MLB 4.49 (n=11,272). MLB runs are OVERDISPERSED vs Poisson
# (residual var / mean = 2.27) so it gets a Normal; NHL is near-perfectly
# Poisson (0.87). College sigmas are unmeasured estimates scaled from the
# measured pro numbers.
SIGMA: dict[Sport, Optional[float]] = {
    Sport.NBA: 19.0,
    Sport.WNBA: 17.5,
    Sport.NCAAB: 17.0,
    Sport.NFL: 13.8,
    Sport.NCAAF: 15.5,
    Sport.MLB: 4.5,
    Sport.NHL: None,
    Sport.SOCCER: None,
    # Tennis has no meaningful points total on our boards.
}


# Minimum distance between the model total and the book's line, in the
# line's own units (goals for soccer/NHL), before an over/under edge is
# shown at all. Roughly each sport's typical pre-game move of the total:
# provisional floors, to be retuned once the totals sample is in the
# hundreds. Below the floor the disagreement is inside the line's own noise.
MIN_DISAGREEMENT: dict[Sport, float] = {
    Sport.NBA: 3.0,
    Sport.WNBA: 3.0,
    Sport.NCAAB: 3.0,
    Sport.NFL: 2.0,
    Sport.NCAAF: 2.5,
    Sport.MLB: 0.75,
    Sport.NHL: 0.5,
    Sport.SOCCER: 0.35,
}


def _poisson_cdf(k: int, mu: float) -> float:
    """P(N <= k) for N ~ Poisson(mu)."""
    if k < 0:
        return 0.0
    term = math.exp(-mu)
    total = term
    for i in range(1, k + 1):
        term *= mu / i
        total += term
    return min(total, 1.0)


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def outcome_probs(sport: Sport, mu: float, line: float) -> Optional[tuple[float, float, float]]:
    """(p_over, p_under, p_push) for the total against `line`.

    Half-point lines never push. Integer lines push exactly (Poisson) or on
    the discretized cell (Normal).
    """
    sigma = SIGMA.get(sport, "missing")
    if sigma == "missing" or mu <= 0:
        return None
    integral = float(line).is_integer()
    if sigma is None:  # Poisson counts
        if integral:
            k = int(line)
            p_under = _poisson_cdf(k - 1, mu)
            p_push = _poisson_cdf(k, mu) - p_under
        else:
            k = math.floor(line)
            p_under = _poisson_cdf(k, mu)
            p_push = 0.0
        p_over = max(1.0 - p_under - p_push, 0.0)
    else:
        s = float(sigma)
        if integral:
            p_under = _phi((line - 0.5 - mu) / s)
            p_push = _phi((line + 0.5 - mu) / s) - p_under
        else:
            p_under = _phi((line - mu) / s)
            p_push = 0.0
        p_over = max(1.0 - p_under - p_push, 0.0)
    return p_over, p_under, p_push


def _ev(p_win: float, p_lose: float, american: int) -> float:
    """Expected profit per $1 with pushes refunded."""
    b = american_to_decimal(american) - 1.0
    return p_win * b - p_lose


def totals_edges(card: GameCard) -> list[Edge]:
    """Over/under Edge rows at the best-priced book carrying a full O/U quote.

    Kept OUT of card.edges — totals have no walk-forward validation yet, so
    they must never flow into picks/autobet until they earn it. Sorted best
    EV first; empty when the model has no total or no book quotes one.
    """
    pred = card.prediction
    if pred is None or pred.predicted_total is None:
        return []
    quotes = [
        o for o in card.odds
        if o.total_line is not None and o.over_price and o.under_price
        # In-game books move the line with the score — comparing a live line
        # to a PREGAME model total fabricates huge edges. Pregame books only.
        # Kickoff-aware: a named "live" feed is not the only way a live price
        # arrives (Polymarket and Kalshi trade through the game). See
        # core.schemas.is_in_play.
        and not is_in_play(o, card.game.start_time)
    ]
    if not quotes:
        return []
    edges: list[Edge] = []
    for o in quotes:
        gap = abs(float(pred.predicted_total) - float(o.total_line))
        if gap < MIN_DISAGREEMENT.get(card.game.sport, 0.0):
            continue  # inside the line's own noise: no edge to show
        probs = outcome_probs(card.game.sport, float(pred.predicted_total), float(o.total_line))
        if probs is None:
            continue
        p_over, p_under, _push = probs
        fair = remove_vig(o.over_price, o.under_price)
        for side, p_win, p_lose, price, market_p in (
            ("over", p_over, p_under, o.over_price, fair[0]),
            ("under", p_under, p_over, o.under_price, fair[1]),
        ):
            edges.append(Edge(
                game_id=card.game.id,
                sport=card.game.sport,
                side=side,
                book=o.book,
                price=int(price),
                model_prob=p_win,
                market_prob=market_p,
                ev_per_dollar=_ev(p_win, p_lose, int(price)),
                kelly_fraction=0.0,  # not staked automatically; see docstring
            ))
    edges.sort(key=lambda e: e.ev_per_dollar, reverse=True)
    # One row per side at its best book keeps the payload lean.
    best: dict[str, Edge] = {}
    for e in edges:
        best.setdefault(e.side, e)
    return sorted(best.values(), key=lambda e: e.ev_per_dollar, reverse=True)

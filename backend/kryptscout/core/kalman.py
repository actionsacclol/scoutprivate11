"""Kalman margin filter — a state-space team rating on score margins.

Each team's strength is a Gaussian (mean, variance) in POINTS. A game is an
observation of the margin:

    margin = s_home − s_away + hfa + ε,   ε ~ N(0, σ²)

and the filter does the textbook update: add process noise q to both sides,
compute the innovation against the predicted margin, and move each team by
its share of the total variance. Win probability is the mass of the
predictive margin distribution above zero (pushes ignored). Between seasons
strengths shrink toward zero and variance is added, so early-season games
move ratings fast and a long, consistent season pins them.

Why it exists: measured 2026-09-02 on the engines' own archives, walk-forward
with paired bootstrap, it beats the shipped Elo by −0.048 [−0.056, −0.039]
of log loss in NCAAF (n=3,829) and −0.023 [−0.028, −0.017] in NCAAB
(n=5,319) — 9% and 4% of the number. Elo's margin-of-victory multiplier
compresses blowouts that, in college, carry most of the information; a
margin filter uses them at face value with an honest noise model. In every
pro sport the same filter was within noise of Elo, so it ships only where
it won, and college is also where the exchanges quote almost nothing — the
standalone forecast IS the displayed number there.

Lower-division opponents (2026-11-14). A side can be passed as a PRIOR —
a fixed (mean, variance) that is used for the game and never written back.
That is how an FCS team (football) or a non-D1 guest (basketball) is priced:
as one generic lower-division opponent, the Kalman counterpart of the Elo
book's flat 1250 / 1200. Before this, such a side was simply missing from
the book and priced as (0, init_var) — an AVERAGE rated team — so the live
board gave an FBS host about 62% against an FCS visitor it beats 95% of the
time (held-out 2025-26: log loss 0.514 on those games, 0.155 with the prior).

Season roll-over can regress toward per-team TARGETS (the mean of the
team's conference) instead of zero; see `conference_targets`.

Tuned constants live in each engine (KALMAN_CONFIG); this module is the
mechanism only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Hashable, Mapping, Optional

Prior = Optional[tuple[float, float]]  # (mean, variance) of a side that is not in the book


@dataclass(frozen=True)
class KalmanConfig:
    sigma: float  # sd of the game margin given true strengths, points
    q: float  # process variance added to each side per game played
    hfa: float  # home advantage, points; zero at a neutral site
    init_var: float  # prior variance of an unseen team's strength
    season_var: float  # variance added at every season boundary
    carry: float = 0.85  # strength kept at a season boundary


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def conference_targets(
    means: Mapping[str, float], conference_of: Mapping[str, Hashable], weight: float = 1.0
) -> dict[str, float]:
    """Roll-over targets: `weight` × the mean strength of each team's conference.

    `conference_of` maps a team to its conference in the NEW season; the mean
    is taken over the teams already in the book (their end-of-season
    strengths), so a team that changed conference regresses toward where it
    is going. Teams without a conference get no entry (they regress to 0).
    """
    sums: dict[Hashable, float] = {}
    counts: dict[Hashable, int] = {}
    for team, conf in conference_of.items():
        if conf is None or conf != conf or team not in means:  # None / NaN / unrated
            continue
        sums[conf] = sums.get(conf, 0.0) + means[team]
        counts[conf] = counts.get(conf, 0) + 1
    conf_mean = {conf: sums[conf] / counts[conf] for conf in sums}
    return {
        team: weight * conf_mean[conf]
        for team, conf in conference_of.items()
        if conf in conf_mean
    }


@dataclass
class KalmanBook:
    config: KalmanConfig
    means: dict[str, float] = field(default_factory=dict)
    vars: dict[str, float] = field(default_factory=dict)

    def get(self, team: str) -> tuple[float, float]:
        return self.means.get(team, 0.0), self.vars.get(team, self.config.init_var)

    def margin(
        self,
        home: str,
        away: str,
        neutral: bool = False,
        home_prior: Prior = None,
        away_prior: Prior = None,
    ) -> tuple[float, float]:
        """(mean, sd) of the predictive home-minus-away margin."""
        mh, vh = home_prior if home_prior is not None else self.get(home)
        ma, va = away_prior if away_prior is not None else self.get(away)
        mu = mh - ma + (0.0 if neutral else self.config.hfa)
        sd = math.sqrt(vh + va + self.config.sigma ** 2)
        return mu, sd

    def expected(
        self,
        home: str,
        away: str,
        neutral: bool = False,
        home_prior: Prior = None,
        away_prior: Prior = None,
    ) -> float:
        """P(home wins) before the game."""
        mu, sd = self.margin(home, away, neutral, home_prior, away_prior)
        return _phi(mu / sd)

    def update(
        self,
        home: str,
        away: str,
        home_score: int,
        away_score: int,
        neutral: bool = False,
        home_prior: Prior = None,
        away_prior: Prior = None,
    ) -> float:
        """One result through the filter; returns the pre-game P(home).

        A side passed as a prior takes part in the update (so the rated side
        learns from the game) but is never stored.
        """
        p_home = self.expected(home, away, neutral, home_prior, away_prior)
        mh, vh = home_prior if home_prior is not None else self.get(home)
        ma, va = away_prior if away_prior is not None else self.get(away)
        vh += self.config.q
        va += self.config.q
        mu = mh - ma + (0.0 if neutral else self.config.hfa)
        total = vh + va + self.config.sigma ** 2
        innovation = (home_score - away_score) - mu
        if home_prior is None:
            self.means[home] = mh + vh / total * innovation
            self.vars[home] = vh - vh * vh / total
        if away_prior is None:
            self.means[away] = ma - va / total * innovation
            self.vars[away] = va - va * va / total
        return p_home

    def new_season(self, targets: Mapping[str, float] | None = None) -> None:
        """Season boundary: add variance, shrink every strength toward its
        target (0 when none is given — the original behaviour)."""
        carry = self.config.carry
        for team in list(self.vars):
            self.vars[team] += self.config.season_var
            target = targets.get(team, 0.0) if targets else 0.0
            self.means[team] = carry * self.means[team] + (1.0 - carry) * target

    # -- persistence ---------------------------------------------------------

    def to_state(self) -> dict:
        return {"means": dict(self.means), "vars": dict(self.vars)}

    @classmethod
    def from_state(cls, config: KalmanConfig, state: dict | None) -> "KalmanBook":
        book = cls(config=config)
        if isinstance(state, dict):
            book.means = {k: float(v) for k, v in (state.get("means") or {}).items()}
            book.vars = {k: float(v) for k, v in (state.get("vars") or {}).items()}
        return book

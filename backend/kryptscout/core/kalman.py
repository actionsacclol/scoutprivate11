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

Tuned constants live in each engine (KALMAN_CONFIG); this module is the
mechanism only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


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


@dataclass
class KalmanBook:
    config: KalmanConfig
    means: dict[str, float] = field(default_factory=dict)
    vars: dict[str, float] = field(default_factory=dict)

    def get(self, team: str) -> tuple[float, float]:
        return self.means.get(team, 0.0), self.vars.get(team, self.config.init_var)

    def margin(self, home: str, away: str, neutral: bool = False) -> tuple[float, float]:
        """(mean, sd) of the predictive home-minus-away margin."""
        mh, vh = self.get(home)
        ma, va = self.get(away)
        mu = mh - ma + (0.0 if neutral else self.config.hfa)
        sd = math.sqrt(vh + va + self.config.sigma ** 2)
        return mu, sd

    def expected(self, home: str, away: str, neutral: bool = False) -> float:
        """P(home wins) before the game."""
        mu, sd = self.margin(home, away, neutral)
        return _phi(mu / sd)

    def update(
        self, home: str, away: str, home_score: int, away_score: int, neutral: bool = False
    ) -> float:
        """One result through the filter; returns the pre-game P(home)."""
        p_home = self.expected(home, away, neutral)
        mh, vh = self.get(home)
        ma, va = self.get(away)
        vh += self.config.q
        va += self.config.q
        mu = mh - ma + (0.0 if neutral else self.config.hfa)
        total = vh + va + self.config.sigma ** 2
        innovation = (home_score - away_score) - mu
        self.means[home] = mh + vh / total * innovation
        self.means[away] = ma - va / total * innovation
        self.vars[home] = vh - vh * vh / total
        self.vars[away] = va - va * va / total
        return p_home

    def new_season(self) -> None:
        for team in list(self.vars):
            self.vars[team] += self.config.season_var
            self.means[team] *= self.config.carry

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

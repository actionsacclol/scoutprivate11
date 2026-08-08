"""Generic Elo rating engine, shared across sport engines.

Standard Elo with configurable K-factor, home advantage, and margin-of-victory
scaling. Every reference implementation we studied (NBA, tennis, soccer)
converges on Elo as the dominant feature, so it lives in core.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class EloConfig:
    k: float = 32.0
    initial: float = 1500.0
    home_advantage: float = 0.0  # rating points added to home side pre-match
    mov_scaling: bool = False  # margin-of-victory multiplier (538-style)
    # Between-season regression toward the mean: new = mean + carry*(old - mean)
    season_carry: float = 0.75


@dataclass
class EloBook:
    config: EloConfig = field(default_factory=EloConfig)
    ratings: dict[str, float] = field(default_factory=dict)

    def get(self, team: str) -> float:
        return self.ratings.get(team, self.config.initial)

    def expected(self, home: str, away: str) -> float:
        """P(home wins) before the match."""
        diff = self.get(home) + self.config.home_advantage - self.get(away)
        return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))

    def update(
        self,
        home: str,
        away: str,
        home_won: bool,
        margin: int | None = None,
        draw: bool = False,
    ) -> None:
        exp_home = self.expected(home, away)
        actual = 0.5 if draw else (1.0 if home_won else 0.0)
        k = self.config.k
        if self.config.mov_scaling and margin is not None and margin > 0:
            elo_diff = self.get(home) - self.get(away)
            if not home_won:
                elo_diff = -elo_diff
            k *= math.log(margin + 1) * (2.2 / (elo_diff * 0.001 + 2.2))
        delta = k * (actual - exp_home)
        self.ratings[home] = self.get(home) + delta
        self.ratings[away] = self.get(away) - delta

    def new_season(self) -> None:
        mean = self.config.initial
        carry = self.config.season_carry
        for team in self.ratings:
            self.ratings[team] = mean + carry * (self.ratings[team] - mean)

"""SportEngine — the contract every per-sport predictor implements.

Each engine owns its own data ingestion, feature pipeline, and model, but
exposes the same three operations so the API layer and terminal UI never
need sport-specific code.
"""
from __future__ import annotations

import abc
from datetime import date

from kryptscout.core.schemas import Game, Prediction, Sport


class SportEngine(abc.ABC):
    sport: Sport
    model_name: str

    @abc.abstractmethod
    async def upcoming_games(self, on: date | None = None) -> list[Game]:
        """Fetch the slate of scheduled games (today by default)."""

    @abc.abstractmethod
    async def predict(self, games: list[Game]) -> list[Prediction]:
        """Produce win probabilities (and totals where supported) for games."""

    @abc.abstractmethod
    async def refresh_data(self) -> None:
        """Pull latest results/stats and retrain or update ratings as needed."""


_REGISTRY: dict[Sport, SportEngine] = {}


def register(engine: SportEngine) -> None:
    _REGISTRY[engine.sport] = engine


def get_engine(sport: Sport) -> SportEngine:
    return _REGISTRY[sport]


def all_engines() -> list[SportEngine]:
    return list(_REGISTRY.values())

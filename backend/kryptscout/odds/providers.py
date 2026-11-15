"""Odds provider registry — multiple free sources feed one odds table.

Each provider maps a slate of Games to MoneylineOdds entries keyed by game id.
Providers are independent and best-effort: one failing (or being unconfigured,
like The Odds API without a key) never blocks the others. The board merges
every provider's entries into GameCard.odds, so ESPN lines, prediction-market
prices (Polymarket/Kalshi), and traditional books all line-shop together.
"""
from __future__ import annotations

import abc
import asyncio
import importlib
import logging

from kryptscout.core.schemas import Game, MoneylineOdds, Sport

logger = logging.getLogger("kryptscout")

# Imported lazily on first use; a module that fails to import (missing dep,
# syntax error mid-development) is skipped with a warning.
PROVIDER_MODULES = [
    "kryptscout.odds.espn_odds",
    "kryptscout.odds.polymarket",
    "kryptscout.odds.kalshi",
]


class OddsProvider(abc.ABC):
    name: str  # book key used in MoneylineOdds.book, e.g. "espn_bet", "polymarket"

    @abc.abstractmethod
    async def odds_for(
        self, sport: Sport, games: list[Game]
    ) -> dict[str, list[MoneylineOdds]]:
        """Return {game.id: [MoneylineOdds, ...]} for games it can price.

        Missing games are simply absent. Must not raise for routine problems
        (no market found, network hiccup) — log and return what it has.
        """


_registry: list[OddsProvider] = []
_loaded = False


def register(provider: OddsProvider) -> None:
    _registry.append(provider)


def all_providers() -> list[OddsProvider]:
    """Load provider modules once, then return everything registered."""
    global _loaded
    if not _loaded:
        _loaded = True
        for mod in PROVIDER_MODULES:
            try:
                module = importlib.import_module(mod)
                build = getattr(module, "build_provider", None)
                if build is not None:
                    provider = build()
                    if provider is not None:  # None = unconfigured (e.g. no key)
                        register(provider)
            except Exception as exc:  # noqa: BLE001
                logger.warning("odds provider %s unavailable: %s", mod, exc)
    return list(_registry)


# Ceiling on any one provider's share of a board build. Providers are
# independent, so a venue that hangs costs its own column, not the board: the
# renderer abandons a board request after BOARD_TIMEOUT_MS (src/api.ts), and
# before this cap one slow venue could push the whole response past it.
PROVIDER_TIMEOUT_S = 12.0


async def merged_odds(sport: Sport, games: list[Game]) -> dict[str, list[MoneylineOdds]]:
    """All providers' entries merged per game id, provider errors isolated.

    Providers run CONCURRENTLY — they share nothing, and awaiting them one
    after another made a board cost the sum of every venue's latency (NFL:
    ESPN + Polymarket + Kalshi ≈ 3s on a fast line, most of an 8s budget on a
    slow one). Results are merged in registry order regardless of which venue
    answered first, because downstream tie-breaks are first-seen-wins.
    """
    providers = all_providers()
    if not games or not providers:
        return {}

    async def _one(provider: OddsProvider) -> dict[str, list[MoneylineOdds]]:
        return await asyncio.wait_for(
            provider.odds_for(sport, games), PROVIDER_TIMEOUT_S
        )

    results = await asyncio.gather(
        *(_one(p) for p in providers), return_exceptions=True
    )
    merged: dict[str, list[MoneylineOdds]] = {}
    for provider, result in zip(providers, results):
        if isinstance(result, BaseException):
            reason = (
                f"timed out after {PROVIDER_TIMEOUT_S:.0f}s"
                if isinstance(result, asyncio.TimeoutError)
                else result
            )
            logger.warning(
                "odds provider %s failed for %s: %s", provider.name, sport.value, reason
            )
            continue
        for game_id, entries in result.items():
            merged.setdefault(game_id, []).extend(entries)
    return merged

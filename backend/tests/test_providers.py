"""merged_odds — providers run concurrently, isolated, and merge in registry order."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from kryptscout.core.schemas import Game, MoneylineOdds, Sport
from kryptscout.odds import providers

GAME = Game(
    id="nfl-1",
    sport=Sport.NFL,
    start_time=datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc),
    home="Home",
    away="Away",
)


class FakeProvider(providers.OddsProvider):
    def __init__(self, name: str, delay: float = 0.0, fail: bool = False) -> None:
        self.name = name
        self.delay = delay
        self.fail = fail
        self.calls = 0

    async def odds_for(self, sport, games):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError(f"{self.name} is down")
        return {
            g.id: [MoneylineOdds(book=self.name, fetched_at=datetime.now(timezone.utc), home_ml=-110, away_ml=100)]
            for g in games
        }


@pytest.fixture()
def registry(monkeypatch):
    def install(*fakes: FakeProvider) -> None:
        monkeypatch.setattr(providers, "all_providers", lambda: list(fakes))

    return install


def _books(merged) -> list[str]:
    return [o.book for o in merged.get(GAME.id, [])]


def test_providers_run_concurrently(registry) -> None:
    """Three venues at 0.2s each must cost about 0.2s, not 0.6s."""
    registry(FakeProvider("a", 0.2), FakeProvider("b", 0.2), FakeProvider("c", 0.2))
    started = time.perf_counter()
    merged = asyncio.run(providers.merged_odds(Sport.NFL, [GAME]))
    elapsed = time.perf_counter() - started
    assert _books(merged) == ["a", "b", "c"]
    assert elapsed < 0.45


def test_merge_order_is_registry_order_not_arrival_order(registry) -> None:
    """Downstream tie-breaks are first-seen-wins, so the slowest venue being
    first in the registry must still come first in the merged list."""
    registry(FakeProvider("slow", 0.15), FakeProvider("fast", 0.0))
    merged = asyncio.run(providers.merged_odds(Sport.NFL, [GAME]))
    assert _books(merged) == ["slow", "fast"]


def test_a_failing_provider_costs_only_its_own_column(registry, caplog) -> None:
    registry(FakeProvider("a"), FakeProvider("broken", fail=True), FakeProvider("c"))
    with caplog.at_level("WARNING", logger="kryptscout"):
        merged = asyncio.run(providers.merged_odds(Sport.NFL, [GAME]))
    assert _books(merged) == ["a", "c"]
    assert "odds provider broken failed for nfl" in caplog.text


def test_a_hung_provider_is_cut_off_at_the_timeout(registry, monkeypatch, caplog) -> None:
    monkeypatch.setattr(providers, "PROVIDER_TIMEOUT_S", 0.1)
    registry(FakeProvider("a"), FakeProvider("hung", delay=30.0))
    started = time.perf_counter()
    with caplog.at_level("WARNING", logger="kryptscout"):
        merged = asyncio.run(providers.merged_odds(Sport.NFL, [GAME]))
    assert time.perf_counter() - started < 2.0
    assert _books(merged) == ["a"]
    assert "odds provider hung failed for nfl: timed out" in caplog.text


def test_no_games_asks_nobody(registry) -> None:
    fake = FakeProvider("a")
    registry(fake)
    assert asyncio.run(providers.merged_odds(Sport.NFL, [])) == {}
    assert fake.calls == 0

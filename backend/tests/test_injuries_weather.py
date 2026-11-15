"""No-network tests for the injuries, stadiums and weather datasources.

ESPN and Open-Meteo payloads are canned and injected by monkeypatching each
module's private fetch seam (`_fetch` / `_fetch_forecast`).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kryptscout.core.schemas import Sport
from kryptscout.datasources import injuries as injuries_mod
from kryptscout.datasources import stadiums, weather as weather_mod

# ---------------------------------------------------------------------------
# injuries
# ---------------------------------------------------------------------------

ESPN_INJURIES_PAYLOAD = {
    "timestamp": "2026-07-10T21:00Z",
    "status": "success",
    "injuries": [
        {
            "id": "12",
            "displayName": "Kansas City Chiefs",
            "injuries": [
                {
                    "id": "1",
                    "status": "Questionable",
                    "date": "2026-07-09T18:30Z",
                    "shortComment": "Mahomes was limited in practice.",
                    "longComment": "Mahomes was limited in practice all week.",
                    "athlete": {
                        "displayName": "Patrick Mahomes",
                        "position": {"abbreviation": "QB"},
                    },
                    "details": {
                        "type": "Ankle",
                        "detail": "Sprain",
                        "side": "Right",
                        "returnDate": "2026-07-13",
                    },
                },
                {
                    # Minimal item: no details block, no comment (NFL feed
                    # frequently omits `details`).
                    "id": "2",
                    "status": "Out",
                    "athlete": {"displayName": "John Backup"},
                },
            ],
        },
        {
            "id": "30",
            "displayName": "Tampa Bay Rays",
            "injuries": [
                {
                    "id": "3",
                    "status": "10-Day-IL",
                    "date": "2026-07-01T12:00Z",
                    "shortComment": "On the IL with a hamstring strain.",
                    "athlete": {
                        "displayName": "Sample Player",
                        "position": {"abbreviation": "LF"},
                    },
                    "details": {"type": "Hamstring", "detail": "Strain"},
                }
            ],
        },
    ],
}


@pytest.fixture(autouse=True)
def _fresh_injury_cache():
    injuries_mod.clear_cache()
    yield
    injuries_mod.clear_cache()


def test_injuries_parses_canned_espn_payload(monkeypatch) -> None:
    async def fake_fetch(path: str) -> dict:
        assert path == "football/nfl"
        return ESPN_INJURIES_PAYLOAD

    monkeypatch.setattr(injuries_mod, "_fetch", fake_fetch)
    reports = asyncio.run(injuries_mod.injuries(Sport.NFL))
    assert len(reports) == 3

    mahomes = reports[0]
    assert mahomes.team == "Kansas City Chiefs"
    assert mahomes.player == "Patrick Mahomes"
    assert mahomes.position == "QB"
    assert mahomes.status == "Questionable"
    assert mahomes.detail == "Ankle - Sprain (Right)"
    assert mahomes.comment == "Mahomes was limited in practice."
    assert mahomes.date == datetime(2026, 7, 9, 18, 30, tzinfo=timezone.utc)

    backup = reports[1]  # sparse item must not crash
    assert backup.player == "John Backup"
    assert backup.position == ""
    assert backup.detail == ""
    assert backup.date is None

    rays = reports[2]  # no `side` -> no parenthetical
    assert rays.status == "10-Day-IL"
    assert rays.detail == "Hamstring - Strain"


def test_injuries_cached_for_15_minutes(monkeypatch) -> None:
    calls = []

    async def fake_fetch(path: str) -> dict:
        calls.append(path)
        return ESPN_INJURIES_PAYLOAD

    monkeypatch.setattr(injuries_mod, "_fetch", fake_fetch)
    first = asyncio.run(injuries_mod.injuries(Sport.NFL))
    second = asyncio.run(injuries_mod.injuries(Sport.NFL))
    assert calls == ["football/nfl"]  # second call served from cache
    assert second == first
    # Expired entries refetch.
    ts, reports = injuries_mod._cache[Sport.NFL]
    injuries_mod._cache[Sport.NFL] = (ts - injuries_mod.CACHE_TTL_SECONDS - 1, reports)
    asyncio.run(injuries_mod.injuries(Sport.NFL))
    assert calls == ["football/nfl", "football/nfl"]


def test_injuries_unsupported_sports_return_empty(monkeypatch) -> None:
    async def boom(path: str) -> dict:  # pragma: no cover - must not run
        raise AssertionError("no fetch expected for soccer/tennis")

    monkeypatch.setattr(injuries_mod, "_fetch", boom)
    assert asyncio.run(injuries_mod.injuries(Sport.SOCCER)) == []
    assert asyncio.run(injuries_mod.injuries(Sport.TENNIS)) == []


# ---------------------------------------------------------------------------
# stadiums
# ---------------------------------------------------------------------------


def test_stadium_tables_complete_and_valid() -> None:
    assert len(stadiums.NFL_STADIUMS) == 32
    assert len(stadiums.MLB_STADIUMS) == 30
    for table in (stadiums.NFL_STADIUMS, stadiums.MLB_STADIUMS):
        for team, venue in table.items():
            assert venue.roof in ("open", "dome", "retractable"), team
            # Continental North America sanity box (Toronto is the far NE).
            assert 24.0 < venue.lat < 50.0, team
            assert -125.0 < venue.lon < -70.0, team


def test_stadium_known_roofs_and_2026_relocations() -> None:
    assert stadiums.NFL_STADIUMS["Los Angeles Rams"].roof == "dome"  # SoFi fixed roof
    assert stadiums.NFL_STADIUMS["Dallas Cowboys"].roof == "retractable"
    assert stadiums.NFL_STADIUMS["Green Bay Packers"].roof == "open"
    assert stadiums.MLB_STADIUMS["Tampa Bay Rays"].roof == "dome"  # Tropicana, 2026
    # Athletics (ESPN displayName has no city) at Sutter Health Park, Sacramento.
    athletics = stadiums.MLB_STADIUMS["Athletics"]
    assert athletics.roof == "open"
    assert abs(athletics.lat - 38.58) < 0.1 and abs(athletics.lon + 121.51) < 0.1


def test_venue_for_lookup() -> None:
    assert stadiums.venue_for(Sport.NFL, "Chicago Bears") is not None
    assert stadiums.venue_for(Sport.NFL, "Chicago Cubs") is None  # wrong league
    assert stadiums.venue_for(Sport.NBA, "Chicago Bulls") is None  # no table
    assert stadiums.venue_for(Sport.MLB, "Nowhere Nine") is None


# ---------------------------------------------------------------------------
# weather
# ---------------------------------------------------------------------------

OPEN_METEO_PAYLOAD = {
    "latitude": 41.95,
    "longitude": -87.65,
    "hourly": {
        "time": [
            "2026-07-10T18:00",
            "2026-07-10T19:00",
            "2026-07-10T20:00",
            "2026-07-10T21:00",
        ],
        "temperature_2m": [24.0, 23.0, 22.4, 21.0],
        "precipitation_probability": [10, 20, 30, 40],
        "wind_speed_10m": [10.0, 12.0, 14.3, 16.0],
        "wind_direction_10m": [180, 190, 200, 210],
    },
}


def test_weather_picks_nearest_hour(monkeypatch) -> None:
    seen = {}

    async def fake_forecast(lat, lon, start_date, end_date):
        seen.update(lat=lat, lon=lon, start_date=start_date, end_date=end_date)
        return OPEN_METEO_PAYLOAD

    monkeypatch.setattr(weather_mod, "_fetch_forecast", fake_forecast)
    # 19:40 UTC is nearest to the 20:00 slot.
    start = datetime(2026, 7, 10, 19, 40, tzinfo=timezone.utc)
    wx = asyncio.run(weather_mod.game_weather(Sport.MLB, "Chicago Cubs", start))
    assert wx is not None
    assert seen["start_date"] == "2026-07-10"
    assert wx.temp_c == 22.4
    assert wx.temp_f == pytest.approx(72.32)
    assert wx.wind_kph == 14.3
    assert wx.wind_dir_deg == 200
    assert wx.precip_prob_pct == 30
    assert wx.is_dome is False
    assert wx.retractable is False
    assert wx.summary == "22°C, 14 kph wind, 30% rain"
    # Wrigley coordinates were passed through.
    assert seen["lat"] == pytest.approx(41.9484)
    assert seen["lon"] == pytest.approx(-87.6553)


def test_weather_retractable_flagged(monkeypatch) -> None:
    async def fake_forecast(lat, lon, start_date, end_date):
        return OPEN_METEO_PAYLOAD

    monkeypatch.setattr(weather_mod, "_fetch_forecast", fake_forecast)
    start = datetime(2026, 7, 10, 20, 5, tzinfo=timezone.utc)
    wx = asyncio.run(weather_mod.game_weather(Sport.MLB, "Milwaukee Brewers", start))
    assert wx is not None
    assert wx.retractable is True
    assert "retractable roof" in wx.summary


def test_weather_short_circuits_without_fetch(monkeypatch) -> None:
    async def boom(lat, lon, start_date, end_date):  # pragma: no cover
        raise AssertionError("no forecast fetch expected")

    monkeypatch.setattr(weather_mod, "_fetch_forecast", boom)
    now = datetime.now(timezone.utc)
    run = lambda *a: asyncio.run(weather_mod.game_weather(*a))
    # Dome venues.
    assert run(Sport.MLB, "Tampa Bay Rays", now) is None
    assert run(Sport.NFL, "Minnesota Vikings", now) is None
    # Unknown team / non-weather sports.
    assert run(Sport.MLB, "Springfield Isotopes", now) is None
    assert run(Sport.NBA, "Boston Celtics", now) is None
    assert run(Sport.SOCCER, "Arsenal", now) is None
    # Beyond the 16-day forecast horizon.
    assert run(Sport.MLB, "Chicago Cubs", now + timedelta(days=17)) is None


def test_weather_naive_start_time_treated_as_utc(monkeypatch) -> None:
    async def fake_forecast(lat, lon, start_date, end_date):
        return OPEN_METEO_PAYLOAD

    monkeypatch.setattr(weather_mod, "_fetch_forecast", fake_forecast)
    wx = asyncio.run(
        weather_mod.game_weather(
            Sport.MLB, "Chicago Cubs", datetime(2026, 7, 10, 18, 20)
        )
    )
    assert wx is not None
    assert wx.temp_c == 24.0  # snapped to the 18:00 slot


# ------------------------------------------------------------ forecast cache


def test_forecast_is_fetched_once_per_venue_and_day(monkeypatch) -> None:
    """The board is rebuilt every refresh; the forecast must not be re-fetched
    for every game on every build."""
    calls = []

    async def fake_forecast(lat, lon, start_date, end_date):
        calls.append((lat, lon, start_date))
        return OPEN_METEO_PAYLOAD

    monkeypatch.setattr(weather_mod, "_fetch_forecast", fake_forecast)
    start = datetime(2026, 7, 10, 19, 40, tzinfo=timezone.utc)
    first = asyncio.run(weather_mod.game_weather(Sport.MLB, "Chicago Cubs", start))
    second = asyncio.run(weather_mod.game_weather(Sport.MLB, "Chicago Cubs", start))
    assert first == second and first is not None
    assert len(calls) == 1
    # A different venue is its own key.
    asyncio.run(weather_mod.game_weather(Sport.MLB, "Boston Red Sox", start))
    assert len(calls) == 2


def test_forecast_cache_expires(monkeypatch) -> None:
    calls = []

    async def fake_forecast(lat, lon, start_date, end_date):
        calls.append(start_date)
        return OPEN_METEO_PAYLOAD

    clock = [1_000.0]
    monkeypatch.setattr(weather_mod, "_fetch_forecast", fake_forecast)
    monkeypatch.setattr(weather_mod.time, "monotonic", lambda: clock[0])
    start = datetime(2026, 7, 10, 19, 40, tzinfo=timezone.utc)
    asyncio.run(weather_mod.game_weather(Sport.MLB, "Chicago Cubs", start))
    clock[0] += weather_mod.FORECAST_TTL_S - 1
    asyncio.run(weather_mod.game_weather(Sport.MLB, "Chicago Cubs", start))
    assert len(calls) == 1
    clock[0] += 2
    asyncio.run(weather_mod.game_weather(Sport.MLB, "Chicago Cubs", start))
    assert len(calls) == 2


def test_failed_forecast_is_not_cached(monkeypatch) -> None:
    """A venue whose forecast failed once must be retried on the next build,
    not served "no weather" for half an hour."""
    attempts = []

    async def flaky(lat, lon, start_date, end_date):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("open-meteo timed out")
        return OPEN_METEO_PAYLOAD

    monkeypatch.setattr(weather_mod, "_fetch_forecast", flaky)
    start = datetime(2026, 7, 10, 19, 40, tzinfo=timezone.utc)
    try:
        first = asyncio.run(weather_mod.game_weather(Sport.MLB, "Chicago Cubs", start))
    except RuntimeError:
        first = None
    assert first is None
    second = asyncio.run(weather_mod.game_weather(Sport.MLB, "Chicago Cubs", start))
    assert second is not None and len(attempts) == 2

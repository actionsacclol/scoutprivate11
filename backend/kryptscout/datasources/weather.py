"""Open-Meteo game-time weather for outdoor NFL/MLB home venues.

api.open-meteo.com is free and keyless. We request hourly temperature,
precipitation probability and wind for the venue's coordinates and pick the
forecast hour nearest kickoff/first pitch.

Timezone choice: we query Open-Meteo with timezone=UTC and compare against
the game's start_time converted to UTC (naive datetimes are assumed UTC,
matching espn.py which always emits UTC). Venue-local conversion is
unnecessary because we match on the absolute instant, not the wall clock.

Returns None when weather can't matter or can't be known:
  * dome venues (fixed roof)   * unknown teams
  * sports other than NFL/MLB  * start_time beyond the ~16-day horizon
Retractable-roof venues DO get a forecast, flagged via `retractable` and a
"(retractable roof)" note in the summary — the roof may be open.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
from pydantic import BaseModel

from kryptscout.core.schemas import Sport
from kryptscout.datasources.stadiums import venue_for

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARS = (
    "temperature_2m,precipitation_probability,wind_speed_10m,wind_direction_10m"
)
FORECAST_HORIZON_DAYS = 16
# A forecast for a venue and day is reused for this long. The NFL engine asks
# for one per outdoor game on every board build, and the renderer rebuilds the
# board every minute: uncached, that was ~14 Open-Meteo round-trips a minute,
# awaited one after another, inside a request the renderer times out. Hourly
# forecasts do not move on that scale.
FORECAST_TTL_S = 30 * 60
REQUEST_TIMEOUT_S = 8.0

# (lat, lon, start_date, end_date) -> (monotonic fetch time, payload)
_forecast_cache: dict[tuple[float, float, str, str], tuple[float, dict]] = {}


def clear_cache() -> None:
    """Forget every cached forecast (tests; a manual refresh)."""
    _forecast_cache.clear()


async def _cached_forecast(
    lat: float, lon: float, start_date: str, end_date: str
) -> dict:
    """_fetch_forecast behind a TTL cache. Failures are never cached."""
    key = (lat, lon, start_date, end_date)
    now = time.monotonic()
    hit = _forecast_cache.get(key)
    if hit is not None and now - hit[0] < FORECAST_TTL_S:
        return hit[1]
    payload = await _fetch_forecast(lat, lon, start_date, end_date)
    if len(_forecast_cache) > 256:  # a season of venues is ~60 keys
        for stale in [k for k, v in _forecast_cache.items() if now - v[0] >= FORECAST_TTL_S]:
            del _forecast_cache[stale]
    _forecast_cache[key] = (now, payload)
    return payload


class GameWeather(BaseModel):
    """Forecast for the hour nearest a game's start time."""

    temp_c: float
    temp_f: float
    wind_kph: float
    wind_dir_deg: float
    precip_prob_pct: float
    is_dome: bool = False  # dome games return None, so this stays False
    retractable: bool = False  # roof may be closed on game day
    summary: str  # e.g. "22°C, 14 kph wind, 30% rain"


async def game_weather(
    sport: Sport, home_team: str, start_time: datetime
) -> GameWeather | None:
    """Weather at the home venue nearest `start_time`, or None (see module doc)."""
    if sport not in (Sport.NFL, Sport.MLB):
        return None
    venue = venue_for(sport, home_team)
    if venue is None or venue.roof == "dome":
        return None
    if start_time.tzinfo is None:
        start_utc = start_time.replace(tzinfo=timezone.utc)
    else:
        start_utc = start_time.astimezone(timezone.utc)
    if start_utc > datetime.now(timezone.utc) + timedelta(days=FORECAST_HORIZON_DAYS):
        return None

    # Fetch the game's UTC date plus the next day so a near-midnight start
    # can still snap to the closest hour.
    start_date = start_utc.date().isoformat()
    end_date = (start_utc + timedelta(hours=1)).date().isoformat()
    payload = await _cached_forecast(venue.lat, venue.lon, start_date, end_date)
    hourly = payload.get("hourly", {})
    times: list[str] = hourly.get("time", [])
    if not times:
        return None
    idx = min(
        range(len(times)),
        key=lambda i: abs(
            datetime.fromisoformat(times[i]).replace(tzinfo=timezone.utc) - start_utc
        ),
    )

    temp_c = _at(hourly, "temperature_2m", idx)
    wind_kph = _at(hourly, "wind_speed_10m", idx)
    wind_dir = _at(hourly, "wind_direction_10m", idx)
    precip = _at(hourly, "precipitation_probability", idx)
    retractable = venue.roof == "retractable"
    summary = f"{temp_c:.0f}°C, {wind_kph:.0f} kph wind, {precip:.0f}% rain"
    if retractable:
        summary += " (retractable roof)"
    return GameWeather(
        temp_c=temp_c,
        temp_f=temp_c * 9 / 5 + 32,
        wind_kph=wind_kph,
        wind_dir_deg=wind_dir,
        precip_prob_pct=precip,
        retractable=retractable,
        summary=summary,
    )


def _at(hourly: dict, key: str, idx: int) -> float:
    """Hourly value at idx; Open-Meteo uses null for unavailable hours."""
    values = hourly.get(key) or []
    value = values[idx] if idx < len(values) else None
    return float(value) if value is not None else 0.0


async def _fetch_forecast(
    lat: float, lon: float, start_date: str, end_date: str
) -> dict:
    """GET the raw Open-Meteo hourly forecast (test seam)."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": HOURLY_VARS,
        "start_date": start_date,
        "end_date": end_date,
        "timezone": "UTC",
        "wind_speed_unit": "kmh",
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        return resp.json()

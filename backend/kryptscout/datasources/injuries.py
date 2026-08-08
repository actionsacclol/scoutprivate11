"""ESPN public injuries API — league-wide injury reports for team sports.

site.api.espn.com serves /{sport}/{league}/injuries with the SAME shape for
every US major league we cover (verified live 2026-07-10, all HTTP 200):

    NFL   football/nfl/injuries
    NBA   basketball/nba/injuries
    WNBA  basketball/wnba/injuries
    MLB   baseball/mlb/injuries
    NHL   hockey/nhl/injuries

Payload: {"injuries": [{"id", "displayName", "injuries": [item, ...]}, ...]}
where each item carries status, date, short/longComment, athlete{displayName,
position} and (for most sports) details{type, detail, side, returnDate}.

Soccer and tennis have no ESPN league-wide injuries feed — those sports
return [] here. (The core API at sports.core.api.espn.com/v2/.../athletes/
{id}/injuries exists per-athlete but is impractical league-wide.)

Responses are cached in-process for 15 minutes per sport.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

import httpx
from pydantic import BaseModel

from kryptscout.core.schemas import Sport

BASE = "https://site.api.espn.com/apis/site/v2/sports"

INJURY_PATHS: dict[Sport, str] = {
    Sport.NFL: "football/nfl",
    Sport.NBA: "basketball/nba",
    Sport.WNBA: "basketball/wnba",
    Sport.MLB: "baseball/mlb",
    Sport.NHL: "hockey/nhl",
    # Sport.SOCCER / Sport.TENNIS: no ESPN league-wide injuries feed.
}

CACHE_TTL_SECONDS = 15 * 60
# sport -> (monotonic timestamp, parsed reports)
_cache: dict[Sport, tuple[float, list[InjuryReport]]] = {}


class InjuryReport(BaseModel):
    """One player's injury line, flattened from ESPN's team-nested feed."""

    team: str  # ESPN team displayName, e.g. "Kansas City Chiefs"
    player: str
    position: str = ""  # position abbreviation, e.g. "QB"
    status: str  # Out / Doubtful / Questionable / Day-To-Day / 10-Day-IL / ...
    detail: str = ""  # structured, e.g. "Hamstring - Strain (Right)"
    comment: str = ""  # ESPN shortComment (beat-writer note)
    date: Optional[datetime] = None  # when the report was filed


async def injuries(sport: Sport) -> list[InjuryReport]:
    """League-wide injury reports; [] for sports without a feed.

    Results are cached in-process for 15 minutes.
    """
    path = INJURY_PATHS.get(sport)
    if path is None:
        return []
    hit = _cache.get(sport)
    if hit is not None and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    payload = await _fetch(path)
    reports = _parse(payload)
    _cache[sport] = (time.monotonic(), reports)
    return reports


def clear_cache() -> None:
    """Drop all cached injury reports (mainly for tests)."""
    _cache.clear()


async def _fetch(path: str) -> dict:
    """GET the raw injuries payload for one league path (test seam)."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{BASE}/{path}/injuries")
        resp.raise_for_status()
        return resp.json()


def _parse(payload: dict) -> list[InjuryReport]:
    reports: list[InjuryReport] = []
    for team_block in payload.get("injuries", []):
        team = team_block.get("displayName", "")
        for item in team_block.get("injuries", []):
            athlete = item.get("athlete") or {}
            position = (athlete.get("position") or {}).get("abbreviation") or ""
            reports.append(
                InjuryReport(
                    team=team,
                    player=athlete.get("displayName", ""),
                    position=position,
                    status=item.get("status") or "",
                    detail=_format_detail(item.get("details") or {}),
                    comment=item.get("shortComment") or "",
                    date=_parse_date(item.get("date")),
                )
            )
    return reports


def _format_detail(details: dict) -> str:
    """'Hamstring - Strain (Right)' from ESPN's details block."""
    parts = [p for p in (details.get("type"), details.get("detail")) if p]
    text = " - ".join(parts)
    side = details.get("side")
    if side:
        text = f"{text} ({side})" if text else f"({side})"
    return text


def _parse_date(raw: str | None) -> datetime | None:
    """ESPN dates look like '2026-07-10T20:37Z' (minute precision)."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None

"""ESPN public scoreboard API — uniform schedules/results for team sports.

site.api.espn.com needs no API key and serves every league we cover with the
same JSON shape. Used for daily slates and for backfilling results that feed
Elo updates. Tennis uses its own source (see sports/tennis.py).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import httpx

from kryptscout.core.schemas import Game, Sport

BASE = "https://site.api.espn.com/apis/site/v2/sports"

LEAGUE_PATHS: dict[Sport, list[tuple[str, str]]] = {
    Sport.NBA: [("basketball/nba", "NBA")],
    Sport.NHL: [("hockey/nhl", "NHL")],
    Sport.NFL: [("football/nfl", "NFL")],
    Sport.MLB: [("baseball/mlb", "MLB")],
    Sport.WNBA: [("basketball/wnba", "WNBA")],
    Sport.NCAAF: [("football/college-football", "NCAAF")],
    Sport.NCAAB: [("basketball/mens-college-basketball", "NCAAB")],
    Sport.SOCCER: [
        ("soccer/eng.1", "EPL"),
        ("soccer/esp.1", "La Liga"),
        ("soccer/ger.1", "Bundesliga"),
        ("soccer/ita.1", "Serie A"),
        ("soccer/fra.1", "Ligue 1"),
        ("soccer/eng.2", "Championship"),
        ("soccer/ned.1", "Eredivisie"),
        ("soccer/por.1", "Primeira Liga"),
        ("soccer/usa.1", "MLS"),
        ("soccer/bra.1", "Brasileirão"),
        ("soccer/mex.1", "Liga MX"),
    ],
}


# ESPN's college scoreboards default to Top-25 only — the groups param widens
# them to all FBS (80) / all D1 (50); limit raises the 50-event default cap.
EXTRA_PARAMS: dict[str, dict[str, str]] = {
    "football/college-football": {"groups": "80", "limit": "400"},
    "basketball/mens-college-basketball": {"groups": "50", "limit": "400"},
}


async def scoreboard(sport: Sport, on: date | None = None) -> list[Game]:
    """Games for a date (today by default), with scores when finished."""
    params = {"dates": on.strftime("%Y%m%d")} if on else {}
    games: list[Game] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for path, league in LEAGUE_PATHS[sport]:
            resp = await client.get(
                f"{BASE}/{path}/scoreboard",
                params={**params, **EXTRA_PARAMS.get(path, {})},
            )
            resp.raise_for_status()
            for event in resp.json().get("events", []):
                game = _parse_event(event, sport, league)
                if game:
                    games.append(game)
    return games


def _parse_event(event: dict, sport: Sport, league: str) -> Game | None:
    comp = event.get("competitions", [{}])[0]
    competitors = comp.get("competitors", [])
    if len(competitors) != 2:
        return None
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    status = comp.get("status", {}).get("type", {})
    completed = bool(status.get("completed"))
    start = datetime.fromisoformat(event["date"].replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
    return Game(
        id=f"{sport.value}-{event['id']}",
        sport=sport,
        league=league,
        start_time=start,
        home=home["team"]["displayName"],
        away=away["team"]["displayName"],
        home_score=int(home["score"]) if completed and home.get("score") else None,
        away_score=int(away["score"]) if completed and away.get("score") else None,
        completed=completed,
        neutral_site=bool(comp.get("neutralSite")),
    )

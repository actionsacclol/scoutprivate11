"""ESPN public scoreboard API — uniform schedules/results for team sports.

site.api.espn.com needs no API key and serves every league we cover with the
same JSON shape. Used for daily slates and for backfilling results that feed
Elo updates. Tennis uses its own source (see sports/tennis.py).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

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

# Extra days pulled past today when the default scoreboard lags (see
# _lags_today) — one day of forward slate, so tomorrow's card and its opening
# lines are on the board without multiplying per-game odds requests.
CATCHUP_DAYS = 1


def _eastern_tz():
    """ESPN keys its slates to the US Eastern day."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001 — Windows without tzdata package
        return timezone(timedelta(hours=-4))  # EDT; the day-boundary slack absorbs it


_EASTERN = _eastern_tz()


def _lags_today(payload: dict, today: date) -> bool:
    """True when a daily scoreboard is still serving a day before today.

    ESPN's daily leagues (MLB, NBA, NHL, WNBA, NCAAB) hold the previous day's
    slate until that day's games are near — well past noon ET for MLB — so an
    undated request during the morning returns only yesterday's finals and the
    board renders empty. Week-based scoreboards (NFL, NCAAF) carry no "day"
    key, and leagues idling between fixtures (soccer, an off-season league)
    report a future day; neither is missing anything, so neither triggers a
    second fetch.
    """
    day = (payload.get("day") or {}).get("date")
    if not day:
        return False
    try:
        return date.fromisoformat(day) < today
    except ValueError:  # unexpected shape — leave the default response alone
        return False


async def scoreboard(sport: Sport, on: date | None = None) -> list[Game]:
    """Games for a date (today by default), with scores when finished.

    Undated, this is ESPN's own idea of the current slate, plus today and the
    next CATCHUP_DAYS days whenever that idea has fallen behind the Eastern
    date. Yesterday's finals stay in the result either way, so results keep
    settling. Duplicates across the two responses collapse on game id.
    """
    params = {"dates": on.strftime("%Y%m%d")} if on else {}
    today = datetime.now(_EASTERN).date()
    games: dict[str, Game] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for path, league in LEAGUE_PATHS[sport]:
            extra = EXTRA_PARAMS.get(path, {})
            payloads = [await _fetch(client, path, {**params, **extra})]
            if on is None and _lags_today(payloads[0], today):
                end = today + timedelta(days=CATCHUP_DAYS)
                payloads.append(
                    await _fetch(
                        client,
                        path,
                        {"dates": f"{today:%Y%m%d}-{end:%Y%m%d}", **extra},
                    )
                )
            for payload in payloads:
                for event in payload.get("events", []):
                    game = _parse_event(event, sport, league)
                    if game:
                        games[game.id] = game
    return list(games.values())


async def _fetch(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    resp = await client.get(f"{BASE}/{path}/scoreboard", params=params)
    resp.raise_for_status()
    return resp.json()


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

"""ESPN public scoreboard API — uniform schedules/results for team sports.

site.api.espn.com needs no API key and serves every league we cover with the
same JSON shape. Used for daily slates and for backfilling results that feed
Elo updates. Tennis uses its own source (see sports/tennis.py).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from kryptscout.core.schemas import Game, Sport

logger = logging.getLogger("kryptscout")

BASE = "https://site.api.espn.com/apis/site/v2/sports"

# Per-request budget. The board is polled by a renderer that gives up after
# BOARD_TIMEOUT_MS (src/api.ts), so a request that hangs for 30s is a board
# that already failed; fail fast and let the retry take the second swing.
REQUEST_TIMEOUT_S = 12.0
# One retry on the failures that are ESPN's, not ours: 429, any 5xx (the edge
# returns 504 in bursts), and transport errors. A 4xx is a request we built
# wrong and will be wrong again.
FETCH_ATTEMPTS = 2
RETRY_BACKOFF_S = 0.6

# How long a sport's last good slate may stand in for one that failed to load.
# Long enough to ride out an ESPN blip without the board going blank, short
# enough that a finished game cannot sit there looking scheduled.
STALE_SLATE_MAX_AGE_S = 15 * 60

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


# Last good undated slate per sport: (monotonic time, games). See scoreboard().
_last_slate: dict[Sport, tuple[float, list[Game]]] = {}


async def scoreboard(sport: Sport, on: date | None = None) -> list[Game]:
    """Games for a date (today by default), with scores when finished.

    Undated, this is ESPN's own idea of the current slate, plus today and the
    next CATCHUP_DAYS days whenever that idea has fallen behind the Eastern
    date. Yesterday's finals stay in the result either way, so results keep
    settling. Duplicates across the responses collapse on game id.

    Leagues are fetched concurrently and fail independently: one soccer league
    erroring costs that league, not the other ten. Only when EVERY league of
    the sport fails does this raise — and for the undated (board) call, a
    slate fetched within STALE_SLATE_MAX_AGE_S is served instead, so an ESPN
    blip leaves the board standing rather than blank.
    """
    params = {"dates": on.strftime("%Y%m%d")} if on else {}
    today = datetime.now(_EASTERN).date()
    leagues = LEAGUE_PATHS[sport]
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        results = await asyncio.gather(
            *(_league_payloads(client, path, params, on, today) for path, _ in leagues),
            return_exceptions=True,
        )

    games: dict[str, Game] = {}
    errors: list[BaseException] = []
    for (path, league), result in zip(leagues, results):
        if isinstance(result, BaseException):
            errors.append(result)
            logger.warning("espn: %s scoreboard failed: %s", path, _describe(result))
            continue
        for payload in result:
            for event in payload.get("events", []):
                game = _parse_event(event, sport, league)
                if game:
                    games[game.id] = game

    if errors and len(errors) == len(leagues):
        cached = _last_slate.get(sport) if on is None else None
        if cached and time.monotonic() - cached[0] <= STALE_SLATE_MAX_AGE_S:
            logger.warning(
                "espn: %s unavailable — serving the slate from %.0fs ago",
                sport.value, time.monotonic() - cached[0],
            )
            return list(cached[1])
        raise errors[0]

    slate = list(games.values())
    # Only a COMPLETE slate is worth standing in later: one built while some
    # leagues were down would hide their games for the whole cache window.
    if on is None and not errors:
        _last_slate[sport] = (time.monotonic(), slate)
    return slate


async def _league_payloads(
    client: httpx.AsyncClient,
    path: str,
    params: dict,
    on: date | None,
    today: date,
) -> list[dict]:
    """One league's scoreboard responses: the default slate, plus one request
    per catch-up day when that slate lags today.

    The catch-up is ONE REQUEST PER DAY on purpose. ESPN stopped accepting the
    `dates=YYYYMMDD-YYYYMMDD` range form in November 2026 — every league
    answers 400 "Failed to get events endpoint" — and because the range was
    only requested while the default slate lagged, the failure looked random:
    NBA/NHL/NCAAB boards died each morning until ESPN rolled its day, and
    MLB/WNBA/soccer died for as long as they sat between fixtures. Single
    days are the form ESPN's own site uses.

    The default slate must load (its failure is this league's failure). The
    catch-up days are best-effort: without them the board still shows what
    ESPN considers current, which beats showing nothing.
    """
    extra = EXTRA_PARAMS.get(path, {})
    first = await _fetch(client, path, {**params, **extra})
    if on is not None or not _lags_today(first, today):
        return [first]
    days = [today + timedelta(days=n) for n in range(CATCHUP_DAYS + 1)]
    caught = await asyncio.gather(
        *(_fetch(client, path, {"dates": f"{d:%Y%m%d}", **extra}) for d in days),
        return_exceptions=True,
    )
    payloads = [first]
    for day, result in zip(days, caught):
        if isinstance(result, BaseException):
            logger.warning(
                "espn: %s catch-up for %s failed: %s", path, day, _describe(result)
            )
            continue
        payloads.append(result)
    return payloads


def _describe(exc: BaseException) -> str:
    """A log-sized reason. httpx's own message for a status error is three
    lines with a documentation link; a timeout's is empty."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return f"{type(exc).__name__}: {exc}".rstrip(": ")


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError)


async def _fetch(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    for attempt in range(FETCH_ATTEMPTS):
        try:
            resp = await client.get(f"{BASE}/{path}/scoreboard", params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — re-raised unless retryable
            if attempt + 1 >= FETCH_ATTEMPTS or not _retryable(exc):
                raise
            await asyncio.sleep(RETRY_BACKOFF_S * (attempt + 1))
    raise RuntimeError("unreachable")  # the loop returns or raises


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

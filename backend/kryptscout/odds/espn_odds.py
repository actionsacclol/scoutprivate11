"""ESPN's public per-event odds — free sportsbook lines, no API key.

sports.core.api.espn.com serves an odds document per event with one item per
sportsbook provider (DraftKings, ESPN BET, ...): moneylines under
homeTeamOdds/awayTeamOdds (`moneyLine`, American), totals as overUnder +
overOdds/underOdds, and drawOdds.moneyLine for soccer three-way markets.
Our game ids embed the ESPN event id ("mlb-401816100"), so each game maps
straight to its odds URL. Events without published odds (far-future games,
out-of-season leagues) return 404 or empty items and are skipped.

Tennis is unsupported: ESPN's core API keys tennis events by tournament, not
match, so there is no per-match odds document to fetch.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from kryptscout.core.schemas import Game, MoneylineOdds, Sport
from kryptscout.datasources.espn import LEAGUE_PATHS
from kryptscout.odds.providers import OddsProvider

logger = logging.getLogger("kryptscout")

CORE = "https://sports.core.api.espn.com/v2/sports"

# Core-API league path per sport. Soccer resolves per game via SOCCER_LEAGUES;
# tennis is deliberately absent (see module docstring).
SPORT_PATHS: dict[Sport, str] = {
    Sport.NBA: "basketball/leagues/nba",
    Sport.NHL: "hockey/leagues/nhl",
    Sport.NFL: "football/leagues/nfl",
    Sport.MLB: "baseball/leagues/mlb",
    Sport.WNBA: "basketball/leagues/wnba",
    Sport.NCAAF: "football/leagues/college-football",
    Sport.NCAAB: "basketball/leagues/mens-college-basketball",
}

# Our Game.league labels -> ESPN league codes ("EPL" -> "eng.1"), derived from
# datasources/espn.LEAGUE_PATHS so the two maps can never drift apart.
SOCCER_LEAGUES: dict[str, str] = {
    label: path.split("/", 1)[1] for path, label in LEAGUE_PATHS[Sport.SOCCER]
}

CACHE_TTL = 600.0  # seconds — board polls must not refetch ESPN constantly
MAX_CONCURRENCY = 8
TIMEOUT = 10.0


# ESPN spells the same book more than one way across events ("DraftKings" and
# "Draft Kings" both appear in our capture). Left alone they become separate
# book keys, which inflates the line shop: `lineShop`/`bestPrice` take the best
# price across books, so one book listed twice fakes both the breadth of the
# shop and, when one spelling is staler than the other, the price at the top.
BOOK_ALIASES = {"draft_kings": "draftkings"}


def _normalize(provider_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", provider_name.lower()).strip("_")


def is_in_play_provider(provider_name: str) -> bool:
    """True for a book's live/in-play feed ("DraftKings Live Odds").

    ESPN publishes these alongside the pre-game feed as ordinary providers,
    so without this check they enter the odds table as if they were separate
    pre-game books. They are not: every row we captured from one was after
    kickoff. See core.schemas.is_in_play for why that ruins the edge math.
    """
    key = _normalize(provider_name)
    return "live" in key.split("_") or "inplay" in key or "in_play" in key


def book_key(provider_name: str) -> str:
    """Normalize an ESPN provider name to a book key: "ESPN BET" -> "espn_bet".

    Known alias spellings collapse onto one canonical key so a single book is
    counted once by the line shop.
    """
    key = _normalize(provider_name)
    return BOOK_ALIASES.get(key, key)


def _price(value) -> int | None:
    """American odds arrive as int or float (e.g. -104.0); keep None as None.

    ESPN publishes 0 as a placeholder on some items (e.g. games in progress);
    American odds of 0 are undefined, so 0 maps to None as well.
    """
    if value is None:
        return None
    try:
        return int(round(float(value))) or None
    except (TypeError, ValueError):
        return None


def parse_odds_items(items: list[dict], fetched_at: datetime) -> list[MoneylineOdds]:
    """One MoneylineOdds per provider item that carries usable American lines.

    Some providers (e.g. Bet 365) publish only fractional summaries with no
    moneyLine/overUnder fields — those items are skipped, as are in-play
    feeds (see is_in_play_provider).
    """
    odds: list[MoneylineOdds] = []
    for item in items:
        name = (item.get("provider") or {}).get("name", "")
        if not name or is_in_play_provider(name):
            continue
        over_under = item.get("overUnder")
        ml = MoneylineOdds(
            book=book_key(name),
            fetched_at=fetched_at,
            home_ml=_price((item.get("homeTeamOdds") or {}).get("moneyLine")),
            away_ml=_price((item.get("awayTeamOdds") or {}).get("moneyLine")),
            draw_ml=_price((item.get("drawOdds") or {}).get("moneyLine")),
            total_line=float(over_under) if over_under else None,
            over_price=_price(item.get("overOdds")),
            under_price=_price(item.get("underOdds")),
        )
        if (
            ml.home_ml is None
            and ml.away_ml is None
            and ml.draw_ml is None
            and ml.total_line is None
        ):
            continue
        odds.append(ml)
    return odds


def event_id_for(game: Game) -> str | None:
    """Our ids embed ESPN's event id: "mlb-401816100" -> "401816100"."""
    prefix = f"{game.sport.value}-"
    if not game.id.startswith(prefix):
        return None
    event_id = game.id[len(prefix):]
    return event_id if event_id.isdigit() else None


def league_path_for(game: Game) -> str | None:
    """Core-API "{sport}/leagues/{league}" path, or None when unsupported."""
    if game.sport is Sport.SOCCER:
        code = SOCCER_LEAGUES.get(game.league)
        return f"soccer/leagues/{code}" if code else None
    return SPORT_PATHS.get(game.sport)


class EspnOddsProvider(OddsProvider):
    name = "espn"

    def __init__(self, ttl: float = CACHE_TTL) -> None:
        self._ttl = ttl
        # game id -> (fetched_at monotonic, parsed odds); empty lists are
        # cached too so games without odds aren't refetched every poll.
        self._cache: dict[str, tuple[float, list[MoneylineOdds]]] = {}

    async def odds_for(
        self, sport: Sport, games: list[Game]
    ) -> dict[str, list[MoneylineOdds]]:
        result: dict[str, list[MoneylineOdds]] = {}
        stale: list[Game] = []
        for game in games:
            cached = self._cache.get(game.id)
            if cached is not None and time.monotonic() - cached[0] < self._ttl:
                if cached[1]:
                    result[game.id] = list(cached[1])
            else:
                stale.append(game)

        if stale:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                fetched = await asyncio.gather(
                    *(self._fetch_game(client, sem, game) for game in stale)
                )
            for game, odds in zip(stale, fetched):
                if odds is None:
                    continue  # transient failure — retry on the next poll
                self._cache[game.id] = (time.monotonic(), odds)
                if odds:
                    result[game.id] = list(odds)
        return result

    async def _fetch_game(
        self, client: httpx.AsyncClient, sem: asyncio.Semaphore, game: Game
    ) -> list[MoneylineOdds] | None:
        """Parsed odds for one game; [] = none available, None = try later."""
        event_id = event_id_for(game)
        path = league_path_for(game)
        if not event_id or not path:
            return []  # unsupported sport/league or foreign id — cache the miss
        url = f"{CORE}/{path}/events/{event_id}/competitions/{event_id}/odds"
        async with sem:
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                logger.warning("espn odds fetch failed for %s: %s", game.id, exc)
                return None
        if resp.status_code == 404:
            return []  # no odds published — common for far-future games
        if resp.status_code != 200:
            logger.warning("espn odds %s: HTTP %s", game.id, resp.status_code)
            return None
        return parse_odds_items(
            resp.json().get("items", []), datetime.now(timezone.utc)
        )


def build_provider() -> EspnOddsProvider:
    return EspnOddsProvider()

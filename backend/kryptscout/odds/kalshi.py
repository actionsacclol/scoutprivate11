"""Kalshi prediction-market moneylines via the public trade API.

api.elections.kalshi.com/trade-api/v2 needs no auth for market data. Game
winners live in per-sport series (KXMLBGAME, KXWNBAGAME, ...): each game is
one event (e.g. ``KXMLBGAME-26JUL121610TORSD``) holding one binary market
per team whose Yes side is that team winning. Prices arrive as dollar
strings ("0.4900") on current payloads or integer cents (49) on older ones;
both mean probability 0.49. We convert the yes bid/ask midpoint to American
odds with core.oddsmath.prob_to_american.

These are ~vig-free real-money probabilities — no bookmaker margin, just
the bid/ask spread — often the sharpest free moneyline signal available.
Kalshi sports books are newer and thinner than Polymarket's, so expect
partial coverage and occasionally one-sided (single-team) pricing.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from kryptscout.core.oddsmath import prob_to_american
from kryptscout.core.schemas import Game, MoneylineOdds, Sport
from kryptscout.odds.providers import OddsProvider

logger = logging.getLogger("kryptscout")

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Game-winner series per sport (verified live: KXMLBGAME, KXWNBAGAME,
# KXNFLGAME; NBA/NHL follow the same pattern and simply return no open
# markets out of season).
SERIES_TICKERS: dict[Sport, str] = {
    Sport.MLB: "KXMLBGAME",
    Sport.WNBA: "KXWNBAGAME",
    Sport.NBA: "KXNBAGAME",
    Sport.NFL: "KXNFLGAME",
    Sport.NHL: "KXNHLGAME",
}

REQUEST_TIMEOUT = 10.0
PAGE_SIZE = 200
MAX_PAGES = 5
CACHE_TTL = 600.0  # one series fetch per sport per 10 min
MAX_SPREAD = 0.15
START_TOLERANCE = timedelta(hours=2)  # when the ticker carries a start time
# The two team markets price complementary outcomes; when both mids exist
# they must roughly sum to 1 or the book is stale/one-sided garbage.
MAX_COMPLEMENT_DRIFT = 0.12

# Event ticker date segment, Eastern time: 26JUL121610TORSD -> 2026-07-12
# 16:10 ET; WNBA omits the HHMM (26JUL12INDLV).
_TICKER_DATE_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{4})?(?=[A-Z])")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_series_cache: dict[Sport, tuple[float, list["KalshiEvent"]]] = {}


def _eastern_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001 — Windows without tzdata package
        return timezone(timedelta(hours=-4))  # EDT; all covered leagues run Mar-Nov


_EASTERN = _eastern_tz()


@dataclass
class KalshiEvent:
    """One game: the per-team win markets grouped by event ticker."""

    event_ticker: str
    game_date: Optional[date]  # Eastern date from the ticker
    start: Optional[datetime]  # UTC, when the ticker carries HHMM
    team_probs: list[tuple[str, float]]  # (team name, yes-win probability)


# ---------------------------------------------------------------------------
# Team-name matching (ESPN displayNames vs. Kalshi city/short names)
# ---------------------------------------------------------------------------

_STOPWORDS = {"the"}

# Market shorthand -> ESPN-style tokens (Kalshi: "A's vs Chicago WS Winner?").
_ALIASES: dict[str, tuple[str, ...]] = {
    "as": ("athletics",),  # "A's" with the apostrophe folded
    "ws": ("white", "sox"),
    "la": ("los", "angeles"),
}


def _tokens(name: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("'", ""))
    tokens: list[str] = []
    for tok in cleaned.split():
        if not tok or tok in _STOPWORDS:
            continue
        tokens.extend(_ALIASES.get(tok, (tok,)))
    return tokens


def _match_score(market_name: str, team_name: str) -> int:
    """3 = strong (token subset / joined containment), 1 = nickname-only, 0 = none."""
    mt, gt = _tokens(market_name), _tokens(team_name)
    if not mt or not gt:
        return 0
    ms, gs = set(mt), set(gt)
    if ms <= gs or gs <= ms:
        return 3
    mjoin, gjoin = "".join(mt), "".join(gt)
    if len(mjoin) >= 5 and (mjoin in gjoin or gjoin in mjoin):
        return 3
    if gt[-1] in ms:  # nickname hit, e.g. "LA Dodgers" vs "Los Angeles Dodgers"
        return 1
    return 0


def pick_side(market_name: str, game: Game) -> Optional[str]:
    """"home"/"away" when the name maps to exactly one side; None if ambiguous.

    Kalshi labels teams by city ("Toronto", "Las Vegas"); a bare "New York"
    during a Yankees-Mets game ties both sides and must be skipped — a wrong
    match is worse than a missing one.
    """
    home, away = _match_score(market_name, game.home), _match_score(market_name, game.away)
    if home > away:
        return "home"
    if away > home:
        return "away"
    return None


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def _price_prob(market: dict, base: str) -> Optional[float]:
    """Read `base` as probability: integer cents (49) or dollar string ("0.4900")."""
    cents = market.get(base)
    if isinstance(cents, (int, float)):
        return float(cents) / 100.0
    dollars = market.get(f"{base}_dollars")
    try:
        if dollars is None or dollars == "":
            return None
        return float(dollars)
    except (TypeError, ValueError):
        return None


def market_prob(market: dict) -> Optional[float]:
    """Yes-side probability from the order book; None when illiquid.

    Mid of yes bid/ask when the book is real; a degenerate book (bid ~0 and
    ask ~1) is skipped; a wide book falls back to the last trade price.
    """
    bid = _price_prob(market, "yes_bid")
    ask = _price_prob(market, "yes_ask")
    last = _price_prob(market, "last_price")
    if bid is not None and ask is not None:
        if bid <= 0.01 and ask >= 0.99:
            return None  # nobody quoting either side
        if 0.0 < bid < ask < 1.0 and (ask - bid) <= MAX_SPREAD:
            return (bid + ask) / 2.0
    if last is not None and 0.0 < last < 1.0:
        return last
    return None


def parse_ticker_datetime(event_ticker: str) -> tuple[Optional[date], Optional[datetime]]:
    """(eastern game date, UTC start) from e.g. KXMLBGAME-26JUL121610TORSD."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None, None
    match = _TICKER_DATE_RE.match(parts[1])
    if not match:
        return None, None
    yy, mon, dd, hhmm = match.groups()
    month = _MONTHS.get(mon)
    if month is None:
        return None, None
    try:
        game_date = date(2000 + int(yy), month, int(dd))
    except ValueError:
        return None, None
    start = None
    if hhmm:
        try:
            local = datetime(
                game_date.year, game_date.month, game_date.day,
                int(hhmm[:2]), int(hhmm[2:]), tzinfo=_EASTERN,
            )
            start = local.astimezone(timezone.utc)
        except ValueError:
            start = None
    return game_date, start


def parse_markets(raw_markets: list[dict]) -> list[KalshiEvent]:
    """Group a series' markets by event and price each team's yes side."""
    grouped: dict[str, KalshiEvent] = {}
    for market in raw_markets:
        ticker = market.get("event_ticker", "")
        if not ticker or market.get("status") not in (None, "active", "open"):
            continue
        team = str(market.get("yes_sub_title") or "").strip()
        if not team:
            continue
        prob = market_prob(market)
        if prob is None or not 0.01 <= prob <= 0.99:
            continue
        event = grouped.get(ticker)
        if event is None:
            game_date, start = parse_ticker_datetime(ticker)
            event = KalshiEvent(ticker, game_date, start, [])
            grouped[ticker] = event
        event.team_probs.append((team, prob))
    return list(grouped.values())


# ---------------------------------------------------------------------------
# Fetching (cached per sport)
# ---------------------------------------------------------------------------

async def _fetch_raw_markets(sport: Sport) -> list[dict]:
    """All open markets in the sport's game series, cursor-paginated."""
    markets: list[dict] = []
    cursor = ""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "series_ticker": SERIES_TICKERS[sport],
                "status": "open",
                "limit": PAGE_SIZE,
            }
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(f"{BASE_URL}/markets", params=params)
            resp.raise_for_status()
            payload = resp.json()
            batch = payload.get("markets", [])
            markets.extend(batch)
            cursor = payload.get("cursor") or ""
            if not cursor or not batch:
                break
    return markets


async def _slate(sport: Sport) -> list[KalshiEvent]:
    cached = _series_cache.get(sport)
    if cached and time.monotonic() - cached[0] < CACHE_TTL:
        return cached[1]
    events = parse_markets(await _fetch_raw_markets(sport))
    _series_cache[sport] = (time.monotonic(), events)
    return events


# ---------------------------------------------------------------------------
# Matching games to events
# ---------------------------------------------------------------------------

def _sides_for(event: KalshiEvent, game: Game) -> Optional[dict[str, float]]:
    """{"home": prob, "away": prob} (either may be missing); None if ambiguous."""
    sides: dict[str, float] = {}
    for team, prob in event.team_probs:
        side = pick_side(team, game)
        if side is None or side in sides:
            return None  # unresolvable or duplicate side
        sides[side] = prob
    return sides or None


def match_game(game: Game, events: list[KalshiEvent], now: datetime) -> Optional[MoneylineOdds]:
    """Exactly-one-event matching on Eastern game date (+ start time if known)."""
    game_date_et = game.start_time.astimezone(_EASTERN).date()
    hits: list[dict[str, float]] = []
    for event in events:
        if event.game_date is not None and event.game_date != game_date_et:
            continue
        if event.start is not None and abs(event.start - game.start_time) > START_TOLERANCE:
            continue
        sides = _sides_for(event, game)
        if sides is not None:
            hits.append(sides)
    if len(hits) != 1:
        if len(hits) > 1:
            logger.debug(
                "kalshi: %s vs %s matched %d events, skipping",
                game.away, game.home, len(hits),
            )
        return None
    sides = hits[0]
    home_prob, away_prob = sides.get("home"), sides.get("away")
    if (
        home_prob is not None
        and away_prob is not None
        and abs(home_prob + away_prob - 1.0) > MAX_COMPLEMENT_DRIFT
    ):
        logger.debug(
            "kalshi: %s vs %s books inconsistent (%.2f + %.2f), skipping",
            game.away, game.home, away_prob, home_prob,
        )
        return None
    return MoneylineOdds(
        book="kalshi",
        fetched_at=now,
        home_ml=prob_to_american(home_prob) if home_prob is not None else None,
        away_ml=prob_to_american(away_prob) if away_prob is not None else None,
    )


class KalshiProvider(OddsProvider):
    name = "kalshi"

    async def odds_for(
        self, sport: Sport, games: list[Game]
    ) -> dict[str, list[MoneylineOdds]]:
        if sport not in SERIES_TICKERS or not games:
            return {}
        try:
            events = await _slate(sport)
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("kalshi: fetch failed for %s: %s", sport.value, exc)
            return {}
        now = datetime.now(timezone.utc)
        result: dict[str, list[MoneylineOdds]] = {}
        for game in games:
            if game.completed:
                continue
            try:
                entry = match_game(game, events, now)
            except Exception as exc:  # noqa: BLE001 — never let one game kill the slate
                logger.warning("kalshi: match failed for %s: %s", game.id, exc)
                continue
            if entry is not None:
                result[game.id] = [entry]
        return result


def build_provider() -> KalshiProvider:
    return KalshiProvider()

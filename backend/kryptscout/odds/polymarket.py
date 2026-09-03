"""Polymarket prediction-market moneylines via the public Gamma API.

gamma-api.polymarket.com needs no API key for market data. Coverage
(structures verified live 2026-07-11):

* US team sports (MLB/WNBA/NBA/NFL/NHL, college via ``cfb``/``cbb`` tags):
  single-game events use slugs like ``mlb-tor-sd-2026-07-11`` (away-home-date)
  and carry a ``moneyline`` market whose ``outcomes`` are the two team display
  names and whose ``outcomePrices`` are string probabilities in [0, 1].
* Tennis: tag ``tennis``, event slugs ``atp-sinner-zverev-2026-07-12`` /
  ``wta-...`` (tour-surname-surname-date, surnames truncated to ~7 chars).
  The ``moneyline`` market's outcomes are the two FULL player names
  ("Jannik Sinner", "Alexander Zverev"). Doubles events insert a ``doubles``
  token and ITF events use the ``itf`` prefix — both excluded by the slug
  pattern. Matched to ESPN names by surname + first-initial keys.
* Soccer: per-league tags (``epl``, ``mls``, ``mex``, ``brazil-serie-a``,
  ...). A match is a negRisk event (``mls-laf-rsl-2026-07-22``) holding THREE
  binary Yes/No ``moneyline`` markets — home win, away win, and a draw market
  whose ``groupItemTitle`` starts with "Draw". Team names live in
  ``groupItemTitle``. Mapped to home_ml/draw_ml/away_ml.

We convert the EXECUTABLE price to American odds with
core.oddsmath.prob_to_american: buying outcome 0 costs its best ask, buying
outcome 1 costs (1 − best bid of outcome 0), since the two tokens' books
mirror each other. The two prices sum to 1 + spread, exactly like a
sportsbook's pair sums to 1 + vig; `remove_vig` on the pair recovers the
midpoint for market-consensus purposes, while EV and every displayed edge are
measured against what a buyer actually pays. Until 2026-09-02 the quote was
the bid/ask MIDPOINT, which nobody can trade — every exchange EV on the board
overstated by roughly half the spread (~1% of stake on a 1c book), and the
"edges" that vanish under this change were never real.

Even at the ask these are ~vig-free real-money probabilities — no bookmaker
margin beyond the spread — which makes them one of the sharpest free
moneyline references available.

Day-ahead note: Gamma's /events default ordering is oldest-first, so on tags
with more open events than we page through (soccer today, NBA/NFL props in
season) the most recently created events — exactly the upcoming games — were
silently dropped. We now request ``order=id&ascending=false`` (newest first)
so day-ahead game events always land in the first pages. Separately, events
created ~a week out have empty order books and a placeholder 0.5/0.5 quote;
those are skipped (a fake even line is worse than no line) and picked up
automatically once real quotes appear, typically 1-2 days before start.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from kryptscout.core.oddsmath import microprice, prob_to_american
from kryptscout.core.schemas import Game, MoneylineOdds, Sport
from kryptscout.odds.providers import OddsProvider

logger = logging.getLogger("kryptscout")

GAMMA_BASE = "https://gamma-api.polymarket.com"
# Public CLOB order books — no key. One GET per matched two-way market per
# poll, cached BOOK_TTL seconds; a failure just leaves the quote without
# depth (core/fair.py then devigs the asks as before).
CLOB_BASE = "https://clob.polymarket.com"
BOOK_TTL = 90.0
BOOK_TIMEOUT = 5.0
BOOK_CONCURRENCY = 5
_book_cache: dict[str, tuple[float, Optional[dict]]] = {}

# Gamma tag slugs for two-way US team sports. College tags verified against
# last season's game events (cfb-mia-ind-2026-01-19, cbb-uf-hou-2025-04-07);
# off-season they serve futures only, which the slug pattern filters out.
TAG_SLUGS: dict[Sport, str] = {
    Sport.MLB: "mlb",
    Sport.WNBA: "wnba",
    Sport.NBA: "nba",
    Sport.NFL: "nfl",
    Sport.NHL: "nhl",
    Sport.NCAAF: "cfb",
    Sport.NCAAB: "cbb",
}

TENNIS_TAG = "tennis"

# Game.league -> tennis event slug prefix. ITF/Challenger markets exist on
# Gamma too but ESPN slates only carry the main tours; gating on the prefix
# keeps a Challenger player from ever matching a tour name.
TOUR_PREFIXES: dict[str, str] = {"ATP": "atp", "WTA": "wta"}

# Per-league Gamma tag -> our ESPN league label (datasources/espn.py). Querying
# league tags instead of the firehose "soccer" tag (~2000 open events) keeps
# pagination trivial and gives the league hint for matching. Eredivisie has no
# discoverable Gamma tag as of 2026-07; it simply contributes no candidates.
SOCCER_TAG_LEAGUES: dict[str, str] = {
    "epl": "EPL",
    "la-liga": "La Liga",
    "bundesliga": "Bundesliga",
    "serie-a": "Serie A",
    "ligue-1": "Ligue 1",
    "efl-championship": "Championship",
    "primeira-liga": "Primeira Liga",
    "mls": "MLS",
    "brazil-serie-a": "Brasileirão",
    "mex": "Liga MX",
}

# Single-game event slugs: "<tag>-<away>-<home>-<yyyy-mm-dd>". Aux events
# (first-five, exact-score, ...) append a suffix after the date and miss.
_GAME_SLUG_RE = re.compile(r"^[a-z]+-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$")
# Tennis singles: tour prefix required; doubles insert a "-doubles-" token
# (4 name parts) and never match.
_TENNIS_SLUG_RE = re.compile(r"^(?:atp|wta)-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$")
# Soccer league prefixes may carry digits (bra2, j2100).
_SOCCER_SLUG_RE = re.compile(r"^[a-z0-9]+-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$")

_DRAW_TITLE_RE = re.compile(r"^draw\b", re.IGNORECASE)
_SOCCER_QUESTION_RE = re.compile(r"^Will (.+?) win on \d{4}-\d{2}-\d{2}\?$")

REQUEST_TIMEOUT = 10.0
PAGE_SIZE = 100
MAX_PAGES = 10
CACHE_TTL = 600.0  # one slate discovery fetch per sport per 10 min
MAX_SPREAD = 0.15  # wider bid/ask books fall back to last trade price
# Book quoted (nearly) 0-to-1 on day-of-creation placeholder markets — treat
# as unpriced rather than trust the 0.5/0.5 default quote.
EMPTY_BID = 0.05
EMPTY_ASK = 0.95
# Market start must sit within this window of our game's start time; tight
# enough that doubleheader games don't both qualify.
START_TOLERANCE = timedelta(hours=2)
# Tennis start times are estimates (matches follow the previous one on
# court); the (player, player, date) triple is already unique so a wider
# window is safe.
TENNIS_START_TOLERANCE = timedelta(hours=8)
# Three independent binary books should roughly sum to 1; placeholder or
# half-dead soccer books don't, and get skipped.
SOCCER_SUM_RANGE = (0.85, 1.15)

_slate_cache: dict[Sport, tuple[float, list]] = {}


@dataclass
class Candidate:
    """One two-way game-level moneyline market, priced at discovery time."""

    slug: str
    outcomes: list[str]  # two team/player display names, market's own order
    prob0: float  # executable BUY price of outcomes[0] (its best ask)
    start: Optional[datetime]  # gameStartTime, UTC
    # Executable buy price of outcomes[1] = 1 − best bid of outcomes[0].
    # None (legacy / last-trade fallback) means the complement of prob0.
    prob1: Optional[float] = None
    # Gamma clobTokenIds, index-aligned with outcomes; "" when absent. The
    # first is the token whose CLOB book prices this market.
    token_ids: tuple[str, str] = ("", "")
    # Gamma conditionId — the CLOB trading key (autobet). "" when absent;
    # outcomes[0] is the market's YES token, outcomes[1] the NO token.
    condition_id: str = ""


@dataclass
class SoccerCandidate:
    """One three-way soccer match: two team win markets plus the draw."""

    slug: str
    league: str  # ESPN league label from SOCCER_TAG_LEAGUES
    teams: list[tuple[str, float]]  # exactly two (display name, win prob)
    draw_prob: float
    start: Optional[datetime]
    # Gamma conditionIds for trading (autobet): one binary Yes/No market per
    # team (aligned with `teams`) plus the draw market. "" when absent; the
    # tradable side of each is always its YES token.
    team_condition_ids: tuple[str, str] = ("", "")
    draw_condition_id: str = ""


@dataclass
class MarketRef:
    """The tradable Polymarket market backing one side of one game."""

    condition_id: str
    clob_side: str  # "yes" | "no" — the outcome token that backs the side
    prob: float  # market probability of that side at discovery time
    slug: str


# ---------------------------------------------------------------------------
# Team-name matching (ESPN displayNames vs. market outcome names)
# ---------------------------------------------------------------------------

_STOPWORDS = {"the"}

# Market shorthand -> ESPN-style tokens ("A's vs Chicago WS", "LA Dodgers").
_ALIASES: dict[str, tuple[str, ...]] = {
    "as": ("athletics",),  # "A's" with the apostrophe folded
    "ws": ("white", "sox"),
    "la": ("los", "angeles"),
}


# Letters NFKD cannot decompose — dropped entirely by the ascii fold unless
# translated first ("Møller" would otherwise become "Mller").
_SPECIAL_LETTERS = str.maketrans(
    {"ø": "o", "Ø": "O", "æ": "ae", "Æ": "Ae", "ß": "ss", "đ": "d", "Đ": "D",
     "ł": "l", "Ł": "L", "œ": "oe", "Œ": "Oe", "þ": "th", "Þ": "Th"}
)


def _fold_ascii(name: str) -> str:
    """Accent-stripped ASCII ("MŠK Žilina" -> "MSK Zilina")."""
    decomposed = unicodedata.normalize("NFKD", name.translate(_SPECIAL_LETTERS))
    return decomposed.encode("ascii", "ignore").decode("ascii")


def _tokens(name: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", _fold_ascii(name).lower().replace("'", ""))
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
        return 3  # e.g. Gamma title glitches like "PortlandFire"
    if gt[-1] in ms:  # nickname hit, e.g. "LA Dodgers" vs "Los Angeles Dodgers"
        return 1
    return 0


def pick_side(market_name: str, game: Game) -> Optional[str]:
    """"home"/"away" when the name maps to exactly one side; None if ambiguous.

    Ambiguity (e.g. "New York" during a Yankees-Mets game) must skip: a wrong
    match is worse than a missing one.
    """
    home, away = _match_score(market_name, game.home), _match_score(market_name, game.away)
    if home > away:
        return "home"
    if away > home:
        return "away"
    return None


# ---------------------------------------------------------------------------
# Tennis player-name matching (surname + first initial, like sports/tennis.py)
# ---------------------------------------------------------------------------

def _player_tokens(name: str) -> list[str]:
    folded = _fold_ascii(name).replace("-", " ").replace(".", " ").replace("'", "")
    tokens = []
    for tok in folded.lower().split():
        # Polymarket transliterates ö/ø/ü/ä as oe/ue/ae where ESPN uses the
        # bare vowel ("Elmer Moeller" vs "Elmer Møller"/"Moller"): collapse
        # the digraphs so both spellings key identically.
        for digraph, vowel in (("oe", "o"), ("ue", "u"), ("ae", "a")):
            tok = tok.replace(digraph, vowel)
        if tok:
            tokens.append(tok)
    return tokens


def player_key(name: str) -> Optional[str]:
    """``"sinner|j"`` from "Jannik Sinner"; None for single-token names."""
    tokens = _player_tokens(name)
    if len(tokens) < 2:
        return None
    return f"{tokens[-1]}|{tokens[0][0]}"


def _surname(name: str) -> Optional[str]:
    tokens = _player_tokens(name)
    return tokens[-1] if tokens else None


def tennis_side(market_name: str, game: Game) -> Optional[str]:
    """"home"/"away" for a market outcome that names exactly one player.

    Exact surname+initial key first; if the initial disagrees (nicknames) or
    the outcome is surname-only, fall back to the surname alone — but only
    when the two players' surnames differ, so sibling matchups can't cross.
    """
    home_key, away_key = player_key(game.home), player_key(game.away)
    if home_key is None or away_key is None or home_key == away_key:
        return None
    market_key = player_key(market_name)
    if market_key == home_key:
        return "home"
    if market_key == away_key:
        return "away"
    m_sur, h_sur, a_sur = _surname(market_name), _surname(game.home), _surname(game.away)
    if m_sur is None or h_sur == a_sur:
        return None
    if m_sur == h_sur:
        return "home"
    if m_sur == a_sur:
        return "away"
    return None


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_game_start(raw: Any) -> Optional[datetime]:
    """Gamma serves e.g. "2026-07-12 00:40:00+00" — normalize for fromisoformat."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip().replace(" ", "T").replace("Z", "+00:00")
    if re.search(r"[+-]\d{2}$", text):
        text += ":00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_list(raw: Any) -> list:
    """Gamma encodes outcomes/outcomePrices as JSON strings inside JSON."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
            return value if isinstance(value, list) else []
        except ValueError:
            return []
    return []


def extract_buy_prices(
    bid: Optional[float],
    ask: Optional[float],
    last: Optional[float],
    fallback: Optional[float],
) -> Optional[tuple[float, float]]:
    """(buy price of outcome 0, buy price of outcome 1), best-source-first.

    A real book prices outcome 0 at its ASK and outcome 1 at 1 − BID: those
    are the numbers a taker pays, and they sum to 1 + spread. A wide book
    (> MAX_SPREAD) or a missing book falls back to the last trade, then the
    quoted price, as a mid with a zero spread — the best information there
    is, honestly labelled by prob1 = 1 − prob0.

    An (almost) empty book — bid pinned near 0 AND ask near 1 — means nobody
    is quoting; day-ahead events are created with exactly this placeholder
    book and a 0.5/0.5 quote. Skip rather than serve a fake even line.
    """
    if bid is not None and ask is not None:
        if bid <= EMPTY_BID and ask >= EMPTY_ASK:
            return None  # placeholder/empty book: no real market yet
        if 0.0 < bid < ask < 1.0 and (ask - bid) <= MAX_SPREAD:
            return (ask, 1.0 - bid)
    for prob in (last, fallback):
        if prob is not None and 0.0 < prob < 1.0:
            return (prob, 1.0 - prob)
    return None


def extract_prob(
    bid: Optional[float],
    ask: Optional[float],
    last: Optional[float],
    fallback: Optional[float],
) -> Optional[float]:
    """Midpoint view of `extract_buy_prices` — a reference, never a price."""
    pair = extract_buy_prices(bid, ask, last, fallback)
    if pair is None:
        return None
    return (pair[0] + (1.0 - pair[1])) / 2.0


def _market_buy_prices(market: dict) -> Optional[tuple[float, float]]:
    """Executable buy prices of the market's two outcomes, best-source-first."""
    prices = [_as_float(p) for p in _json_list(market.get("outcomePrices"))]
    quoted0 = prices[0] if len(prices) == 2 else None
    return extract_buy_prices(
        _as_float(market.get("bestBid")),
        _as_float(market.get("bestAsk")),
        _as_float(market.get("lastTradePrice")),
        quoted0,
    )


def _token_pair(market: dict) -> tuple[str, str]:
    ids = [str(t) for t in _json_list(market.get("clobTokenIds"))]
    return (ids[0] if len(ids) > 0 else "", ids[1] if len(ids) > 1 else "")


TopOfBook = tuple[float, float, float, float]  # bid, ask, bid_size, ask_size


def top_of_book(book: Optional[dict]) -> Optional[TopOfBook]:
    """(best bid, best ask, size at bid, size at ask) from a CLOB /book payload.

    Levels arrive sorted AWAY from the touch (best bid = max price, best ask
    = min price); sizes are shares. None for an empty, crossed or degenerate
    book — the placeholder 0.01/0.99 books of unseeded events included.
    """
    if not isinstance(book, dict):
        return None
    try:
        bids = [(float(l["price"]), float(l["size"])) for l in book.get("bids") or []]
        asks = [(float(l["price"]), float(l["size"])) for l in book.get("asks") or []]
    except (KeyError, TypeError, ValueError):
        return None
    if not bids or not asks:
        return None
    bid, bid_size = max(bids, key=lambda level: level[0])
    ask, ask_size = min(asks, key=lambda level: level[0])
    if not (0.0 < bid < ask < 1.0) or (bid <= EMPTY_BID and ask >= EMPTY_ASK):
        return None
    return bid, ask, bid_size, ask_size


async def _fetch_book(token_id: str) -> Optional[dict]:
    """The CLOB book for one token, cached BOOK_TTL seconds; None on any failure."""
    if not token_id:
        return None
    cached = _book_cache.get(token_id)
    if cached and time.monotonic() - cached[0] < BOOK_TTL:
        return cached[1]
    payload: Optional[dict] = None
    try:
        async with httpx.AsyncClient(timeout=BOOK_TIMEOUT) as client:
            resp = await client.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
            resp.raise_for_status()
            data = resp.json()
            payload = data if isinstance(data, dict) else None
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
        logger.debug("polymarket: book fetch failed for %s: %s", token_id[:12], exc)
    _book_cache[token_id] = (time.monotonic(), payload)
    return payload


def attach_depth(entry: MoneylineOdds, tob: TopOfBook, side0: str) -> None:
    """Write microprice/depth/spread onto `entry` from outcomes[0]'s book.

    Buying outcomes[0] lifts its ask (depth = ask_size·ask dollars); buying
    outcomes[1] hits outcomes[0]'s bid from the other side (depth =
    bid_size·(1 − bid)). `side0` says which of home/away outcomes[0] is.
    """
    bid, ask, bid_size, ask_size = tob
    micro0 = microprice(bid, ask, bid_size, ask_size)
    depth0 = ask_size * ask
    depth1 = bid_size * (1.0 - bid)
    if side0 == "home":
        entry.microprice_home = micro0
        entry.home_depth_usd, entry.away_depth_usd = depth0, depth1
    else:
        entry.microprice_home = 1.0 - micro0
        entry.home_depth_usd, entry.away_depth_usd = depth1, depth0
    entry.spread_pts = (ask - bid) * 100.0


def _single_hit(sport: Sport, game: Game, candidates: list) -> Optional[tuple[Candidate, str]]:
    """The exactly-one two-way candidate behind a matched game, if any."""
    if sport == Sport.SOCCER:
        return None  # three binary books per match — depth not wired for soccer
    hits = _tennis_hits(game, candidates) if sport == Sport.TENNIS else _game_hits(game, candidates)
    return hits[0] if len(hits) == 1 else None


def _prob1(cand: "Candidate") -> float:
    """Buy price of outcomes[1]; the complement when only a mid was known."""
    return cand.prob1 if cand.prob1 is not None else 1.0 - cand.prob0


def _open_moneylines(event: dict) -> list[dict]:
    return [
        m
        for m in event.get("markets", [])
        if m.get("sportsMarketType") == "moneyline"
        and not m.get("closed")
        and m.get("active") is not False
    ]


def parse_events(raw_events: list[dict], slug_re: re.Pattern = _GAME_SLUG_RE) -> list[Candidate]:
    """Reduce a Gamma /events page to priced two-way moneyline candidates."""
    candidates: list[Candidate] = []
    for event in raw_events:
        slug = event.get("slug", "")
        if not slug_re.match(slug):
            continue  # futures, props, first-five, doubles, etc.
        for market in _open_moneylines(event):
            outcomes = [str(o) for o in _json_list(market.get("outcomes"))]
            if len(outcomes) != 2 or "Yes" in outcomes:
                continue  # binary Yes/No shapes are not two-way team markets
            pair = _market_buy_prices(market)
            if pair is None or not all(0.01 <= p <= 0.99 for p in pair):
                continue
            candidates.append(
                Candidate(
                    slug=slug,
                    outcomes=outcomes,
                    prob0=pair[0],
                    prob1=pair[1],
                    start=_parse_game_start(market.get("gameStartTime")),
                    condition_id=str(market.get("conditionId") or ""),
                    token_ids=_token_pair(market),
                )
            )
            break  # one moneyline market per game event
    return candidates


def _soccer_league(event: dict) -> Optional[str]:
    for tag in event.get("tags", []) or []:
        league = SOCCER_TAG_LEAGUES.get((tag or {}).get("slug", ""))
        if league:
            return league
    return None


def parse_soccer_events(raw_events: list[dict]) -> list[SoccerCandidate]:
    """Three-binary negRisk soccer events -> priced 3-way candidates.

    Each match event carries three Yes/No ``moneyline`` markets: one per team
    (``groupItemTitle`` = team display name) and one draw ("Draw (...)" /
    "...end in a draw?"). All three must price and roughly sum to 1.
    """
    candidates: list[SoccerCandidate] = []
    seen: set[str] = set()
    for event in raw_events:
        slug = event.get("slug", "")
        if not _SOCCER_SLUG_RE.match(slug) or slug in seen:
            continue
        league = _soccer_league(event)
        if league is None:
            continue
        teams: list[tuple[str, float]] = []
        team_cids: list[str] = []
        draw_prob: Optional[float] = None
        draw_cid = ""
        start: Optional[datetime] = None
        ok = True
        for market in _open_moneylines(event):
            outcomes = _json_list(market.get("outcomes"))
            if outcomes and outcomes[0] != "Yes":
                ok = False  # unexpected shape — don't guess
                break
            question = str(market.get("question") or "")
            title = str(market.get("groupItemTitle") or "")
            # Each binary market is bought on its YES side: its ask.
            pair = _market_buy_prices(market)
            prob = pair[0] if pair is not None else None
            if prob is None:
                ok = False
                break
            start = start or _parse_game_start(market.get("gameStartTime"))
            cid = str(market.get("conditionId") or "")
            if _DRAW_TITLE_RE.match(title) or "end in a draw" in question.lower():
                if draw_prob is not None:
                    ok = False
                    break
                draw_prob = prob
                draw_cid = cid
            else:
                name = title
                if not name:
                    q_match = _SOCCER_QUESTION_RE.match(question)
                    name = q_match.group(1) if q_match else ""
                if not name:
                    ok = False
                    break
                teams.append((name, prob))
                team_cids.append(cid)
        if not ok or draw_prob is None or len(teams) != 2:
            continue
        total = teams[0][1] + teams[1][1] + draw_prob
        if not SOCCER_SUM_RANGE[0] <= total <= SOCCER_SUM_RANGE[1]:
            continue  # independent books disagree — placeholder or one-sided
        seen.add(slug)
        candidates.append(
            SoccerCandidate(
                slug=slug, league=league, teams=teams, draw_prob=draw_prob, start=start,
                team_condition_ids=(team_cids[0], team_cids[1]),
                draw_condition_id=draw_cid,
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# Fetching (cached per sport)
# ---------------------------------------------------------------------------

async def _fetch_tag_events(client: httpx.AsyncClient, tag: str) -> list[dict]:
    """All open events for one tag, newest first.

    ``order=id&ascending=false`` matters: Gamma's default is oldest-first, so
    tags holding more open events than MAX_PAGES covers used to drop the most
    recently created events — i.e. tomorrow's games (the day-ahead gap).
    """
    events: list[dict] = []
    for page in range(MAX_PAGES):
        resp = await client.get(
            f"{GAMMA_BASE}/events",
            params={
                "tag_slug": tag,
                "closed": "false",
                "order": "id",
                "ascending": "false",
                "limit": PAGE_SIZE,
                "offset": page * PAGE_SIZE,
            },
        )
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        events.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return events


async def _fetch_raw_events(sport: Sport) -> list[dict]:
    """All open events for the sport's tag(s), paginated."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        if sport == Sport.SOCCER:
            events: list[dict] = []
            for tag in SOCCER_TAG_LEAGUES:
                try:
                    events.extend(await _fetch_tag_events(client, tag))
                except httpx.HTTPError as exc:
                    # partial coverage beats none: one league 404ing must not
                    # blank every league's lines
                    logger.warning("polymarket: soccer tag %s failed: %s", tag, exc)
            return events
        if sport == Sport.TENNIS:
            return await _fetch_tag_events(client, TENNIS_TAG)
        return await _fetch_tag_events(client, TAG_SLUGS[sport])


async def _slate(sport: Sport) -> list:
    cached = _slate_cache.get(sport)
    if cached and time.monotonic() - cached[0] < CACHE_TTL:
        return cached[1]
    raw = await _fetch_raw_events(sport)
    if sport == Sport.SOCCER:
        candidates: list = parse_soccer_events(raw)
    elif sport == Sport.TENNIS:
        candidates = parse_events(raw, _TENNIS_SLUG_RE)
    else:
        candidates = parse_events(raw)
    _slate_cache[sport] = (time.monotonic(), candidates)
    return candidates


# ---------------------------------------------------------------------------
# Matching games to markets
# ---------------------------------------------------------------------------

def _game_hits(game: Game, candidates: list[Candidate]) -> list[tuple[Candidate, str]]:
    """(candidate, side of outcomes[0]) for every candidate whose teams map
    1-1 to the game and whose start time agrees."""
    hits: list[tuple[Candidate, str]] = []
    for cand in candidates:
        if cand.start is None or abs(cand.start - game.start_time) > START_TOLERANCE:
            continue
        side0 = pick_side(cand.outcomes[0], game)
        side1 = pick_side(cand.outcomes[1], game)
        if side0 is None or side1 is None or side0 == side1:
            continue
        hits.append((cand, side0))
    return hits


def match_game(game: Game, candidates: list[Candidate], now: datetime) -> Optional[MoneylineOdds]:
    """Exactly-one-candidate matching: teams map 1-1 and start times agree."""
    hits = _game_hits(game, candidates)
    if len(hits) != 1:
        if len(hits) > 1:
            logger.debug(
                "polymarket: %s vs %s matched %d markets, skipping",
                game.away, game.home, len(hits),
            )
        return None
    cand, side0 = hits[0]
    home_prob, away_prob = _home_away(cand, side0)
    if not (0.01 <= home_prob <= 0.99 and 0.01 <= away_prob <= 0.99):
        return None
    return MoneylineOdds(
        book="polymarket",
        fetched_at=now,
        home_ml=prob_to_american(home_prob),
        away_ml=prob_to_american(away_prob),
    )


def _home_away(cand: Candidate, side0: str) -> tuple[float, float]:
    """(home buy price, away buy price) given which side outcomes[0] is."""
    p0, p1 = cand.prob0, _prob1(cand)
    return (p0, p1) if side0 == "home" else (p1, p0)


def _tennis_hits(game: Game, candidates: list[Candidate]) -> list[tuple[Candidate, str]]:
    """Tennis (candidate, side of outcomes[0]) hits: tour prefix + player keys."""
    prefix = TOUR_PREFIXES.get(game.league)
    if prefix is None:
        return []
    hits: list[tuple[Candidate, str]] = []
    for cand in candidates:
        if not cand.slug.startswith(prefix + "-"):
            continue
        if cand.start is None or abs(cand.start - game.start_time) > TENNIS_START_TOLERANCE:
            continue
        side0 = tennis_side(cand.outcomes[0], game)
        side1 = tennis_side(cand.outcomes[1], game)
        if side0 is None or side1 is None or side0 == side1:
            continue
        hits.append((cand, side0))
    return hits


def match_tennis(game: Game, candidates: list[Candidate], now: datetime) -> Optional[MoneylineOdds]:
    """Tennis: tour prefix + player-key matching, exactly-one-candidate."""
    hits = _tennis_hits(game, candidates)
    if len(hits) != 1:
        if len(hits) > 1:
            logger.debug(
                "polymarket: %s vs %s matched %d tennis markets, skipping",
                game.away, game.home, len(hits),
            )
        return None
    cand, side0 = hits[0]
    home_prob, away_prob = _home_away(cand, side0)
    if not (0.01 <= home_prob <= 0.99 and 0.01 <= away_prob <= 0.99):
        return None
    return MoneylineOdds(
        book="polymarket",
        fetched_at=now,
        home_ml=prob_to_american(home_prob),
        away_ml=prob_to_american(away_prob),
    )


def _soccer_hits(
    game: Game, candidates: list[SoccerCandidate]
) -> list[tuple[SoccerCandidate, bool]]:
    """(candidate, teams[0]-is-home) hits: league hint + team-name matching."""
    hits: list[tuple[SoccerCandidate, bool]] = []
    for cand in candidates:
        if game.league and cand.league != game.league:
            continue
        if cand.start is None or abs(cand.start - game.start_time) > START_TOLERANCE:
            continue
        (name0, _), (name1, _) = cand.teams
        side0 = pick_side(name0, game)
        side1 = pick_side(name1, game)
        if side0 is None or side1 is None or side0 == side1:
            continue
        hits.append((cand, side0 == "home"))
    return hits


def match_soccer(
    game: Game, candidates: list[SoccerCandidate], now: datetime
) -> Optional[MoneylineOdds]:
    """Soccer: league hint + team-name matching over the two win markets."""
    hits = _soccer_hits(game, candidates)
    if len(hits) != 1:
        if len(hits) > 1:
            logger.debug(
                "polymarket: %s vs %s matched %d soccer markets, skipping",
                game.away, game.home, len(hits),
            )
        return None
    cand, home_first = hits[0]
    (_, prob0), (_, prob1) = cand.teams
    home_prob = prob0 if home_first else prob1
    away_prob = prob1 if home_first else prob0
    draw_prob = cand.draw_prob
    if not all(0.01 <= p <= 0.99 for p in (home_prob, draw_prob, away_prob)):
        return None
    return MoneylineOdds(
        book="polymarket",
        fetched_at=now,
        home_ml=prob_to_american(home_prob),
        away_ml=prob_to_american(away_prob),
        draw_ml=prob_to_american(draw_prob),
    )


# ---------------------------------------------------------------------------
# Trading resolution (autobet)
# ---------------------------------------------------------------------------

async def resolve_market(sport: Sport, game: Game, side: str) -> Optional[MarketRef]:
    """The tradable market backing one side of a game, or None.

    Same discovery cache and exactly-one-candidate matching rules as the odds
    path — if the odds path wouldn't quote the game, this won't trade it.
    Two-way markets trade the matched outcome's token ("yes" = outcomes[0]);
    soccer sides trade the YES token of that side's own binary market. A
    candidate recorded before conditionIds were captured resolves to None.
    """
    supported = sport in TAG_SLUGS or sport in (Sport.TENNIS, Sport.SOCCER)
    if not supported or side not in ("home", "away", "draw"):
        return None
    try:
        candidates = await _slate(sport)
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("polymarket: resolve fetch failed for %s: %s", sport.value, exc)
        return None

    if sport == Sport.SOCCER:
        soccer_hits = _soccer_hits(game, candidates)
        if len(soccer_hits) != 1:
            return None
        cand, home_first = soccer_hits[0]
        if side == "draw":
            cid, prob = cand.draw_condition_id, cand.draw_prob
        else:
            idx = 0 if (side == "home") == home_first else 1
            cid, prob = cand.team_condition_ids[idx], cand.teams[idx][1]
        if not cid:
            return None
        return MarketRef(condition_id=cid, clob_side="yes", prob=prob, slug=cand.slug)

    if side == "draw":
        return None  # two-way markets have no draw
    hits = _tennis_hits(game, candidates) if sport == Sport.TENNIS else _game_hits(game, candidates)
    if len(hits) != 1:
        return None
    cand2, side0 = hits[0]
    if not cand2.condition_id:
        return None
    idx = 0 if side0 == side else 1
    prob = cand2.prob0 if idx == 0 else _prob1(cand2)
    return MarketRef(
        condition_id=cand2.condition_id,
        clob_side="yes" if idx == 0 else "no",
        prob=prob,
        slug=cand2.slug,
    )


def _match_for_sport(sport: Sport, game: Game, candidates: list, now: datetime):
    if sport == Sport.TENNIS:
        return match_tennis(game, candidates, now)
    if sport == Sport.SOCCER:
        return match_soccer(game, candidates, now)
    return match_game(game, candidates, now)


class PolymarketProvider(OddsProvider):
    name = "polymarket"

    async def odds_for(
        self, sport: Sport, games: list[Game]
    ) -> dict[str, list[MoneylineOdds]]:
        supported = sport in TAG_SLUGS or sport in (Sport.TENNIS, Sport.SOCCER)
        if not supported or not games:
            return {}
        try:
            candidates = await _slate(sport)
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("polymarket: fetch failed for %s: %s", sport.value, exc)
            return {}
        now = datetime.now(timezone.utc)
        result: dict[str, list[MoneylineOdds]] = {}
        wants: list[tuple[MoneylineOdds, Candidate, str]] = []
        for game in games:
            if game.completed:
                continue
            try:
                entry = _match_for_sport(sport, game, candidates, now)
            except Exception as exc:  # noqa: BLE001 — never let one game kill the slate
                logger.warning("polymarket: match failed for %s: %s", game.id, exc)
                continue
            if entry is not None:
                result[game.id] = [entry]
                hit = _single_hit(sport, game, candidates)
                if hit is not None and hit[0].token_ids[0]:
                    wants.append((entry, hit[0], hit[1]))
        await _attach_books(wants)
        return result


async def _attach_books(wants: list[tuple[MoneylineOdds, "Candidate", str]]) -> None:
    """Best-effort depth for every matched two-way market, BOOK_CONCURRENCY at a time."""
    if not wants:
        return
    gate = asyncio.Semaphore(BOOK_CONCURRENCY)

    async def one(entry: MoneylineOdds, cand: Candidate, side0: str) -> None:
        async with gate:
            tob = top_of_book(await _fetch_book(cand.token_ids[0]))
        if tob is not None:
            attach_depth(entry, tob, side0)

    await asyncio.gather(*(one(*w) for w in wants), return_exceptions=True)


def build_provider() -> PolymarketProvider:
    return PolymarketProvider()

"""Polymarket US trading client — just what auto-bet needs to buy a game winner.

Polymarket US (polymarket.us) is the CFTC-regulated exchange: an API key pair
instead of a wallet, dollars instead of USDC, one book per market instead of a
YES and a NO token. It is a different venue from polymarket.com in every way
that matters to an order, so it gets its own client. A focused port of Krypt
PolyBot US's network layer; the payload translation is that bot's own module,
vendored whole as pmus_wire.py.

What carries over from the bot, because each one is there for a reason found
on a real account:

* Auth is three X-PM-* headers; the signed string is `timestamp + METHOD +
  path` with no query and no body, Ed25519, 30 s of clock skew allowed — so
  the clock is corrected from the gateway's Date header.
* Order prices are always on the YES side. Buying the second team is buying
  NO (short the one instrument); pmus_wire mirrors the price on the way out.
* Every order is a LIMIT with immediate-or-cancel, so the cap holds on a thin
  book. The exchange is asked to answer synchronously so a fill is known here.
* The API has no idempotency key. A placement is sent ONCE; a lost reply is an
  unknown outcome (OrderOutcomeUnknown), never a clean failure and never a
  retry.
* The rulebook (5.2(i)) requires automated systems to carry a price collar.
  Here the collar is the order itself: the engine reads the live book first
  and the limit can never sit above the price it just saw clear its floor; a
  book that cannot be read sends nothing.

Game markets: an event `nba-lal-sa-2026-11-27` holds one winner market
`aec-nba-lal-sa-2026-11-27` whose `marketSides` name both teams; the side with
`long: true` is YES. The event list ignores every filter, so a game is found
through search by team name and matched on league, both teams and start time —
exactly one hit or nothing.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from kryptscout.autobet import pmus_wire as us
from kryptscout.autobet import polymarket_auth as secrets
from kryptscout.core.schemas import Game, Sport
from kryptscout.odds.kalshi import pick_side

logger = logging.getLogger("kryptscout")

GATEWAY = us.GATEWAY
API_BASE = us.API_BASE
USER_AGENT = "krypt-scout"
REQUEST_TIMEOUT = 12.0
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5

# Polymarket US league key per Scout sport (GET /v1/sports).
LEAGUES: dict[Sport, str] = {
    Sport.NBA: "nba", Sport.NFL: "nfl", Sport.NHL: "nhl", Sport.MLB: "mlb",
    Sport.WNBA: "wnba", Sport.NCAAB: "cbb", Sport.NCAAF: "cfb",
}
START_TOLERANCE = timedelta(hours=3)
SEARCH_TTL = 600.0
MONEYLINE = "SPORTS_MARKET_TYPE_MONEYLINE"


class PolymarketUSError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = int(status or 0)
        self.body = body
        super().__init__(f"Polymarket US {self.status}: {str(body)[:300]}")


class OrderOutcomeUnknown(Exception):
    """The order may or may not have reached the exchange."""


# ------------------------------------------------------------------ credentials

_KEY_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _key_file() -> Path:
    return secrets._credentials_dir() / "polymarket_us.key"


_cached: Optional[dict] = None
_cached_for: Optional[Path] = None
_time_offset = 0.0


def reset_cache() -> None:
    global _cached, _cached_for
    _cached = None
    _cached_for = None


def _decode_secret(secret_key: str) -> bytes:
    """The 32-byte Ed25519 seed. The developer page issues base64; a 64-byte
    keypair form is accepted too, whose first 32 bytes are the seed."""
    try:
        raw = base64.b64decode((secret_key or "").strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("The secret key is not valid base64.") from exc
    if len(raw) == 64:
        raw = raw[:32]
    if len(raw) != 32:
        raise ValueError(
            f"The secret key decodes to {len(raw)} bytes; a Polymarket US key is 32 or 64.")
    return raw


def save_credentials(key_id: str, secret_key: str) -> None:
    key_id = (key_id or "").strip()
    secret_key = (secret_key or "").strip()
    if not _KEY_ID_RE.match(key_id):
        raise ValueError("The key ID should look like 1a2b3c4d-0000-0000-0000-000000000000.")
    _decode_secret(secret_key)
    path = _key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    secrets._write_secret_bytes(
        path, json.dumps({"keyId": key_id, "secretKey": secret_key}).encode("utf-8"))
    reset_cache()


def clear_credentials() -> None:
    try:
        _key_file().unlink()
    except OSError:
        pass
    reset_cache()


def credentials_present() -> bool:
    return _key_file().exists()


def _load() -> dict:
    global _cached, _cached_for
    path = _key_file()
    if _cached is not None and _cached_for == path:
        return _cached
    if not path.exists():
        raise PolymarketUSError(401, "No Polymarket US API key is saved.")
    try:
        data = json.loads(secrets._read_secret_bytes(path).decode("utf-8"))
        creds = {"keyId": str(data["keyId"]), "secretKey": str(data["secretKey"])}
    except Exception as exc:  # noqa: BLE001 — a corrupt file reads as "no key"
        raise PolymarketUSError(
            401, f"The saved Polymarket US key could not be read ({type(exc).__name__}).") from exc
    _cached, _cached_for = creds, path
    return creds


def credentials_status() -> dict:
    info: dict[str, Any] = {"connected": credentials_present(), "keyPreview": ""}
    if info["connected"]:
        try:
            key_id = _load()["keyId"]
            info["keyPreview"] = f"{key_id[:8]}…{key_id[-4:]}"
        except PolymarketUSError as exc:
            info["error"] = str(exc.body)
    return info


def sign_message(secret_key: str, message: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(_decode_secret(secret_key))
    return base64.b64encode(key.sign(message.encode("utf-8"))).decode("ascii")


def auth_headers(method: str, path: str, *, now_ms: Optional[int] = None,
                 creds: Optional[dict] = None) -> dict:
    path = path.split("?", 1)[0]
    c = creds or _load()
    ts = str(now_ms if now_ms is not None else int((time.time() + _time_offset) * 1000))
    return {
        "X-PM-Access-Key": c["keyId"],
        "X-PM-Timestamp": ts,
        "X-PM-Signature": sign_message(c["secretKey"], f"{ts}{method.upper()}{path}"),
    }


def verify_signer() -> str:
    """Offline selftest: the signature verifies over exactly the documented
    message with the matching public key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    creds = {"keyId": "00000000-0000-0000-0000-000000000000",
             "secretKey": base64.b64encode(bytes(range(32))).decode("ascii")}
    headers = auth_headers("get", "/v1/portfolio/positions", now_ms=1700000000000, creds=creds)
    Ed25519PrivateKey.from_private_bytes(bytes(range(32))).public_key().verify(
        base64.b64decode(headers["X-PM-Signature"]), b"1700000000000GET/v1/portfolio/positions")
    return "ed25519 ok"


# --------------------------------------------------------------------- requests


async def _get(url: str, params: Optional[dict] = None, *, timeout: float = REQUEST_TIMEOUT) -> Any:
    """Public gateway read; None on any failure (callers treat it as unknown)."""
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("polymarket us: GET %s failed: %s", url, exc)
        return None


async def _sync_clock() -> None:
    global _time_offset
    try:
        async with httpx.AsyncClient(timeout=4.0, headers={"User-Agent": USER_AGENT}) as client:
            stamp = (await client.get(f"{GATEWAY}/v1/sports")).headers.get("date")
        if stamp:
            offset = parsedate_to_datetime(stamp).timestamp() - time.time()
            _time_offset = offset if abs(offset) > 2.0 else 0.0
    except Exception as exc:  # noqa: BLE001
        logger.debug("polymarket us: clock sync skipped: %s", exc)


async def _authed(method: str, path: str, *, json_body: Any = None,
                  params: Optional[dict] = None, idempotent: bool = True) -> Any:
    """Signed request. `idempotent=False` (placing an order) is sent once."""
    method = method.upper()
    attempts = MAX_RETRIES if idempotent else 1
    last: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for attempt in range(1, attempts + 1):
            headers = auth_headers(method, path)
            headers["Content-Type"] = "application/json"
            headers["User-Agent"] = USER_AGENT
            try:
                resp = await client.request(method, f"{API_BASE}{path}", json=json_body,
                                            params=params, headers=headers)
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                last = exc
                if attempt < attempts:
                    await asyncio.sleep(RETRY_BACKOFF * attempt)
                    continue
                raise
            if resp.status_code < 400:
                try:
                    return resp.json() if resp.text else {}
                except ValueError:
                    return {}
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text[:300]
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < attempts:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            raise PolymarketUSError(resp.status_code, body)
    if last:
        raise last
    raise PolymarketUSError(0, "request failed")


def error_text(exc: Exception) -> str:
    if isinstance(exc, PolymarketUSError):
        body = exc.body
        if isinstance(body, dict):
            message = body.get("message") or body.get("error") or body.get("detail")
            if message:
                return f"Polymarket US: {message}"[:300]
        return f"Polymarket US {exc.status}: {str(body)[:250]}"
    return f"{type(exc).__name__}: {exc}"[:300]


# -------------------------------------------------------------------- discovery

_search_cache: dict[str, tuple[float, list]] = {}
_market_cache: dict[str, dict] = {}


async def _search(query: str) -> list[dict]:
    hit = _search_cache.get(query)
    if hit and time.monotonic() - hit[0] < SEARCH_TTL:
        return hit[1]
    data = await _get(f"{GATEWAY}/v1/search", {"query": query, "limit": 20})
    events = data.get("events") if isinstance(data, dict) else None
    events = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
    if len(_search_cache) > 400:
        _search_cache.clear()
    _search_cache[query] = (time.monotonic(), events)
    return events


_COLLEGE_WORD = {Sport.NCAAF: "football", Sport.NCAAB: "basketball"}


def _drop_last(name: str) -> str:
    """A team name without its last word: the school of "Georgia Bulldogs"."""
    parts = name.split()
    return " ".join(parts[:-1]) if len(parts) > 1 else name


def search_queries(sport: Sport, game: Game) -> list[str]:
    """The searches to try for a game, most likely first; the caller stops at
    the first that matches.

    The search ranks by text and returns twenty rows, and it titles games its
    own way ("BKN Nets vs. MEM Grizzlies", "Missouri vs. Georgia"). A full
    home name is therefore a poor query: "Memphis Grizzlies" returns Memphis
    college basketball, and a team's other fixtures and futures crowd the
    game out. Measured on one evening's boards (2026-11-14): the full home
    name found 0 of 5 NBA games and 0 of 6 NHL games; both nicknames found
    all eleven. College games are titled by school, so those ask for the two
    schools and the sport (6 of 8 football games, against none).

    The query only decides what is fetched. Whether a row IS this game is
    match_market's call — league, both teams, start time — so a loose query
    costs a request, never a wrong market.
    """
    word = _COLLEGE_WORD.get(sport)
    if word is not None:
        first = f"{_drop_last(game.away)} {_drop_last(game.home)} {word}"
    else:
        first = f"{game.home.split()[-1]} {game.away.split()[-1]}"
    queries: list[str] = []
    for query in (first, game.home, game.away):
        query = query.strip()
        if query and query not in queries:
            queries.append(query)
    return queries


def _start_of(row: dict) -> Optional[datetime]:
    stamp = row.get("gameStartTime") or row.get("startTime") or row.get("startDate")
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def match_market(game: Game, events: list[dict], league: str) -> Optional[tuple[dict, dict[str, str]]]:
    """(raw winner market, {"home"/"away": "yes"/"no"}) for the one event that
    is this game — same league, both teams, start within START_TOLERANCE — or
    None when zero or several qualify. Pure."""
    hits: list[tuple[dict, dict[str, str]]] = []
    for event in events:
        if event.get("closed") or event.get("archived"):
            continue
        for market in event.get("markets") or []:
            if not isinstance(market, dict) or market.get("sportsMarketTypeV2") != MONEYLINE:
                continue
            if market.get("closed") or str(market.get("status") or "") != "MARKET_STATUS_OPEN":
                continue
            start = _start_of(market) or _start_of(event)
            if start is None or abs(start - game.start_time) > START_TOLERANCE:
                continue
            yes_side, no_side = us.market_sides(market)
            sides: dict[str, str] = {}
            for clob_side, row in (("yes", yes_side), ("no", no_side)):
                team = (row or {}).get("team") if isinstance((row or {}).get("team"), dict) else {}
                if str(team.get("league") or "").lower() != league:
                    break
                side = pick_side(str(team.get("name") or ""), game)
                if side is None or side in sides:
                    break
                sides[side] = clob_side
            else:
                if set(sides) == {"home", "away"} and us.valid_slug(market.get("slug")):
                    hits.append((market, sides))
    if len(hits) != 1:
        if len(hits) > 1:
            logger.debug("polymarket us: %s vs %s matched %d markets, skipping",
                         game.away, game.home, len(hits))
        return None
    return hits[0]


def side_ask(market: dict, clob_side: str) -> Optional[float]:
    """What a buyer of `clob_side` pays at the touch, from the market row."""
    bid, ask = us.parse_market_quote(market)
    _, side_a = us.quote_for_side(clob_side, bid, ask)
    return side_a if side_a is not None and 0.0 < side_a < 1.0 else None


async def resolve(sport: Sport, game: Game, side: str) -> Optional[dict]:
    """{slug, clob_side, prob} for one side of a game, or None."""
    league = LEAGUES.get(sport)
    if league is None or side not in ("home", "away"):
        return None
    matched = None
    for query in search_queries(sport, game):
        matched = match_market(game, await _search(query), league)
        if matched is not None:
            break
    if matched is None:
        return None
    market, sides = matched
    slug = str(market["slug"])
    _market_cache[slug] = market
    return {"slug": slug, "clob_side": sides[side], "prob": side_ask(market, sides[side])}


async def market_meta(slug: str) -> Optional[dict]:
    """{"fee_schedule", "min_size", "tick_size"} — from the row discovery saw,
    else one read by slug."""
    raw = _market_cache.get(slug)
    if raw is None and us.valid_slug(slug):
        data = await _get(f"{GATEWAY}/v1/market/slug/{slug}")
        raw = data.get("market") if isinstance(data, dict) else None
        if isinstance(raw, dict):
            _market_cache[slug] = raw
    if not isinstance(raw, dict):
        return None
    return {
        "fee_schedule": us.fee_schedule(raw),
        "min_size": us.to_float(raw.get("minimumTradeQty"), 1.0) or 1.0,
        "tick_size": us.to_float(raw.get("orderPriceMinTickSize"), 0.01) or 0.01,
    }


async def ask_depth(slug: str, clob_side: str, max_price_cents: int) -> Optional[dict]:
    """Same shape as polymarket_api.ask_depth. None when the book is unreadable
    or shows no offer at all."""
    if not us.valid_slug(slug):
        return None
    data = await _get(f"{GATEWAY}/v1/markets/{slug}/book")
    if not isinstance(data, dict):
        return None
    asks = us.book_for_side(us.parse_book(data.get("marketData")), clob_side)["asks"]
    asks = sorted(((px, qty) for px, qty in asks if 0.0 < px < 1.0), key=lambda r: r[0])
    if not asks:
        return None
    cap = max_price_cents / 100.0
    crossable = [[px, qty] for px, qty in asks if px <= cap + 1e-9]
    return {
        "best_ask_cents": us.ask_cents(asks[0][0]),
        "depth_shares": sum(qty for _, qty in crossable),
        "levels": crossable,
    }


# ---------------------------------------------------------------------- account


async def get_balance_usd() -> float:
    """Spendable cash. A reply without a USD row RAISES, so a read failure
    never looks like a $0 balance."""
    view = us.balance_view(await _authed("GET", "/v1/account/balances"))
    if view is None:
        raise RuntimeError("balance response had no USD balance")
    return round(float(view["cash"]), 2)


async def get_order(order_id: str) -> Optional[dict]:
    data = await _authed("GET", f"/v1/order/{order_id}")
    order = data.get("order") if isinstance(data, dict) else None
    return us.order_view(order) if isinstance(order, dict) else None


async def fills_for_order(order_id: str) -> Optional[tuple[float, float]]:
    """(shares, average price of OUR side) filled on one order; (0.0, 0.0)
    when the order ended with nothing filled; None when unknown. The order's
    own cumQuantity / avgPx are the truth: trade activity carries no order id
    on this exchange."""
    try:
        view = await get_order(order_id)
    except PolymarketUSError:
        return None
    if not view:
        return None
    shares, price = float(view.get("size_matched") or 0), float(view.get("price") or 0)
    if shares <= 0 and view.get("status") == "canceled":
        return 0.0, 0.0  # ended with nothing filled — a definite no-trade
    if shares <= 0 or not (0.0 < price < 1.0):
        return None
    return round(shares, 4), round(price, 4)


def _snap(price: float, tick: float) -> float:
    """DOWN to the tick grid: a buy cap must never round up past its floor."""
    tick = tick if tick and tick > 0 else 0.01
    steps = int(price / tick + 1e-9)
    return max(tick, min(1.0 - tick, round(steps * tick, 4)))


async def buy_ioc(slug: str, clob_side: str, shares: int, price_cents: int) -> dict:
    """Buy up to `shares` of one side at or under `price_cents`, immediately;
    the rest is killed. Returns {order_id, filled, avg_prob}; an empty
    order_id is a clean no-trade.

    Raises OrderOutcomeUnknown when no definite answer came back, and
    PolymarketUSError for a refusal."""
    if clob_side not in ("yes", "no"):
        raise ValueError("clob_side must be yes|no")
    if shares <= 0 or not (0 < price_cents < 100):
        raise ValueError("bad order size or price")
    if not us.valid_slug(slug):
        raise PolymarketUSError(404, f"not a market slug: {slug!r}")
    meta = await market_meta(slug)
    if meta is None:
        raise PolymarketUSError(404, f"no market {slug}")
    if shares < meta["min_size"]:
        raise PolymarketUSError(400, f"order is below this market's minimum of {meta['min_size']:g}")
    price = _snap(price_cents / 100.0, meta["tick_size"])
    body = us.order_body(slug=slug, side=clob_side, action="buy", count=int(shares),
                         price=price, order_type="FAK")
    try:
        resp = await _authed("POST", "/v1/orders", json_body=body, idempotent=False)
    except PolymarketUSError as exc:
        if us.is_no_liquidity(exc.body):
            return {"order_id": "", "filled": 0.0, "avg_prob": None}
        if "global rate limit exceeded" in str(exc.body).lower():
            # Not processed within 5 s and rejected: a clean refusal.
            raise PolymarketUSError(429, "The exchange was too busy and rejected the order.") from exc
        if exc.status >= 500:
            raise OrderOutcomeUnknown(error_text(exc)) from exc
        raise
    except httpx.TransportError as exc:
        raise OrderOutcomeUnknown(f"no answer from Polymarket US ({type(exc).__name__})") from exc
    parsed = us.parse_place_response(resp, "FAK")
    if parsed["reject"]:
        if us.is_no_liquidity(parsed["reject"]):
            return {"order_id": "", "filled": 0.0, "avg_prob": None}
        raise PolymarketUSError(400, parsed["reject"])
    if not parsed["order_id"]:
        return {"order_id": "", "filled": 0.0, "avg_prob": None}
    filled, avg = float(parsed["filled"] or 0.0), None
    execs = [e for e in (resp.get("executions") or []) if isinstance(e, dict)] if isinstance(resp, dict) else []
    order = execs[-1].get("order") if execs and isinstance(execs[-1].get("order"), dict) else None
    if order and filled > 0:
        view_price = float(us.order_view(order).get("price") or 0)
        avg = round(view_price, 4) if 0.0 < view_price < 1.0 else None
    if filled > 0 and avg is not None and parsed["status"] != "live":
        return {"order_id": parsed["order_id"], "filled": filled, "avg_prob": avg}
    return await _await_final(parsed["order_id"], filled, avg)


FINAL_POLLS = 4
FINAL_POLL_S = 0.6


async def _await_final(order_id: str, filled: float, avg: Optional[float]) -> dict:
    """The exchange accepted an immediate order without saying how it ended.
    Ask the order itself, briefly. A kill with nothing filled is a clean
    no-trade (empty id); anything still unsettled is reported with
    `filled: None` — placed, fill unknown — and is reconciled later rather
    than graded as a full fill."""
    for _ in range(FINAL_POLLS):
        await asyncio.sleep(FINAL_POLL_S)
        try:
            view = await get_order(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("polymarket us: order read failed for %s: %s", order_id, exc)
            continue
        if not view:
            continue
        done = float(view.get("size_matched") or 0.0)
        price = float(view.get("price") or 0.0)
        if view.get("status") == "live":
            continue
        if done <= 0:
            return {"order_id": "", "filled": 0.0, "avg_prob": None}
        return {"order_id": order_id, "filled": done,
                "avg_prob": round(price, 4) if 0.0 < price < 1.0 else None}
    if filled > 0 and avg is not None:
        return {"order_id": order_id, "filled": filled, "avg_prob": avg}
    return {"order_id": order_id, "filled": None, "avg_prob": None}


async def check() -> dict:
    """One signed read with the saved key: is this account usable?"""
    if not credentials_present():
        return {"ok": False, "error": "No Polymarket US API key is saved."}
    await _sync_clock()
    try:
        return {"ok": True, "balance_usd": await get_balance_usd()}
    except PolymarketUSError as exc:
        hint = " Check the key ID and secret." if exc.status in (401, 403) else ""
        return {"ok": False, "error": error_text(exc) + hint}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not reach Polymarket US ({type(exc).__name__})."}

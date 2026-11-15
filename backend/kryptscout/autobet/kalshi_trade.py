"""Kalshi trading client — just what auto-bet needs to buy a game winner.

A focused port of Krypt Trader's kalshi_auth / kalshi_api (the Kalshi bot in
this suite, where every shape below was proven on a real account):

* Signing. `timestamp + METHOD + path` (no query string), RSA-PSS/SHA-256 or
  Ed25519 — Kalshi's key page creates Ed25519 by default now, so both load.
  The timestamp uses Kalshi's own clock (Date header), because a consumer PC
  20-30 s off gets every signed request refused.
* Orders go to the v2 endpoint as a bid/ask on the YES book with the price in
  dollars. A YES buy is a bid; auto-bet only ever buys YES of a team's own
  market, and only as immediate-or-cancel: it crosses what the book offers at
  or under the cap and the rest is killed. Nothing rests.
* One `client_order_id` per order, minted before the first attempt, so a retry
  after a lost reply cannot place a second order.
* Collateral lives per exchange shard (since August 2026) and game markets are
  not on the shard a deposit lands on. `ensure_collateral` moves the shortfall
  of ONE order between the user's own shards before a buy — never retried (the
  endpoint has no idempotency key), never more than the source holds, whole
  cents rounded down — and says so in its return value. Money never leaves the
  account. On anything it cannot read it does nothing and lets Kalshi decide.

Public market data (books, series fees) needs no key, so paper mode works with
nothing connected.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import time
import uuid
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from kryptscout.autobet import polymarket_auth as secrets

logger = logging.getLogger("kryptscout")

HOST = "https://api.elections.kalshi.com"
PATH_PREFIX = "/trade-api/v2"
PUBLIC_BASE = f"{HOST}{PATH_PREFIX}"
ORDERS_PATH = "/portfolio/events/orders"

REQUEST_TIMEOUT = 15.0
ORDER_TIMEOUT = httpx.Timeout(connect=4.0, read=8.0, write=8.0, pool=8.0)
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5

# Taker fee per contract = 0.07 x fee_multiplier x p x (1 - p), rounded UP to
# the cent per order. The multiplier comes from the market's series.
BASE_TAKER_RATE = 0.07
# On top of the limit price when checking collateral: the fee peaks at
# 0.07 x 0.25 = 1.75c a contract, so two cents always covers it.
FEE_ALLOWANCE_CENTS = 2
# A transfer is accepted before the destination can spend it (seen on a real
# account: the order sent right behind a move was refused, the money showed a
# moment later). Wait, briefly and bounded, until the shard shows it.
LAND_TIMEOUT_S = 4.0
LAND_POLL_S = 0.4
_CENTICENTS_PER_CENT = 100


class KalshiError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = int(status or 0)
        self.body = body
        super().__init__(f"Kalshi {self.status}: {str(body)[:300]}")


class OrderOutcomeUnknown(Exception):
    """The order may or may not have reached the exchange (timeout, 5xx)."""


# ------------------------------------------------------------------ credentials


def _key_file() -> Path:
    return secrets._credentials_dir() / "kalshi.key"


_cached: Optional[tuple[str, Any]] = None
_cached_for: Optional[Path] = None


def reset_cache() -> None:
    global _cached, _cached_for
    _cached = None
    _cached_for = None


def _load_signing_key(pem: bytes):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
        raise ValueError("That file is not an RSA or Ed25519 private key.")
    return key


def normalize_pem(text: str) -> str:
    """A pasted key often loses its line breaks. Rebuild a PEM from whatever
    survived, as long as the header and footer are still there."""
    raw = (text or "").strip().replace("\\n", "\n")
    if "-----BEGIN" not in raw or "-----END" not in raw:
        raise ValueError(
            "That does not look like a Kalshi private key. Paste the whole file, "
            "including the -----BEGIN ... PRIVATE KEY----- and -----END lines."
        )
    head, _, rest = raw.partition("-----BEGIN")
    label, _, rest = rest.partition("-----")
    body, _, _tail = rest.partition("-----END")
    body = "".join(body.split())
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    label = label.strip()
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def save_credentials(key_id: str, private_key_pem: str) -> None:
    key_id = (key_id or "").strip()
    if not key_id or len(key_id) > 100 or any(c.isspace() for c in key_id):
        raise ValueError("Enter the API key ID from Kalshi (the short id shown beside the key).")
    pem = normalize_pem(private_key_pem)
    try:
        _load_signing_key(pem.encode("utf-8"))
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — cryptography raises several types
        raise ValueError(f"Kalshi could not read that private key ({type(exc).__name__}).") from exc
    path = _key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    secrets._write_secret_bytes(path, json.dumps({"keyId": key_id, "pem": pem}).encode("utf-8"))
    reset_cache()


def clear_credentials() -> None:
    try:
        _key_file().unlink()
    except OSError:
        pass
    reset_cache()


def credentials_present() -> bool:
    return _key_file().exists()


def _load() -> tuple[str, Any]:
    global _cached, _cached_for
    path = _key_file()
    if _cached is not None and _cached_for == path:
        return _cached
    if not path.exists():
        raise KalshiError(401, "No Kalshi API key is saved.")
    try:
        data = json.loads(secrets._read_secret_bytes(path).decode("utf-8"))
        loaded = (str(data["keyId"]), _load_signing_key(str(data["pem"]).encode("utf-8")))
    except Exception as exc:  # noqa: BLE001 — a corrupt file reads as "no key"
        raise KalshiError(401, f"The saved Kalshi key could not be read ({type(exc).__name__}).") from exc
    _cached, _cached_for = loaded, path
    return loaded


def credentials_status() -> dict:
    """For the UI — never returns secret material."""
    info: dict[str, Any] = {"connected": credentials_present(), "keyPreview": "", "keyType": ""}
    if not info["connected"]:
        return info
    try:
        key_id, key = _load()
        info["keyPreview"] = f"{key_id[:6]}…{key_id[-4:]}" if len(key_id) > 12 else key_id
        info["keyType"] = "Ed25519" if type(key).__name__.startswith("Ed25519") else "RSA"
    except KalshiError as exc:
        info["error"] = str(exc.body)
    return info


# ---------------------------------------------------------------------- signing

_offset_ms = 0
_last_sync = 0.0
_RESYNC_SEC = 300.0


def sync_server_time(force: bool = False) -> int:
    """Kalshi's clock minus ours, in ms. Blocking — call via asyncio.to_thread."""
    global _offset_ms, _last_sync
    now = time.time()
    if not force and now - _last_sync < _RESYNC_SEC:
        return _offset_ms
    _last_sync = now  # back off on failure too, or every request would re-try it
    try:
        with httpx.Client(timeout=5.0) as client:
            stamp = client.head(f"{PUBLIC_BASE}/exchange/status").headers.get("date")
        if stamp:
            _offset_ms = int((parsedate_to_datetime(stamp).timestamp() - now) * 1000) - 750
    except Exception as exc:  # noqa: BLE001
        logger.debug("kalshi clock sync failed: %s", exc)
    return _offset_ms


def sign(private_key: Any, message: bytes) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding

    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        signature = private_key.sign(message)
    else:
        signature = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
    return base64.b64encode(signature).decode("ascii")


def sign_headers(method: str, path: str, *, now_ms: Optional[int] = None,
                 creds: Optional[tuple[str, Any]] = None) -> dict[str, str]:
    """`path` is the full signed path (/trade-api/v2/...), without a query."""
    key_id, key = creds or _load()
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000) + _offset_ms)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign(key, (ts + method.upper() + path).encode("utf-8")),
    }


def verify_signer() -> str:
    """Offline selftest: an Ed25519 signature over the documented message
    verifies with the matching public key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    headers = sign_headers("get", f"{PATH_PREFIX}/portfolio/balance",
                           now_ms=1700000000000, creds=("test-key", key))
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000000GET/trade-api/v2/portfolio/balance",
    )
    return "ed25519 ok"


# --------------------------------------------------------------------- requests


async def _public_get(path: str, params: Optional[dict] = None) -> Any:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(f"{PUBLIC_BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


async def _signed(method: str, path: str, *, json_body: Optional[dict] = None,
                  params: Optional[dict] = None, retry: bool = True,
                  timeout: Any = REQUEST_TIMEOUT) -> Any:
    """Signed request. `retry=False` for a write with no dedupe token of its
    own: every retry re-signs, so Kalshi sees independent valid requests."""
    signed_path = f"{PATH_PREFIX}{path}"
    method = method.upper()
    attempts = MAX_RETRIES if retry else 1
    if time.time() - _last_sync >= _RESYNC_SEC:
        await asyncio.to_thread(sync_server_time)
    last: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=timeout, base_url=HOST) as client:
        for attempt in range(1, attempts + 1):
            headers = sign_headers(method, signed_path)
            try:
                resp = await client.request(method, signed_path, headers=headers,
                                            json=json_body, params=params)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last = exc
                if attempt < attempts:
                    await asyncio.sleep(RETRY_BACKOFF * attempt)
                    continue
                raise
            if resp.status_code == 429 and attempt < attempts:
                await asyncio.sleep(min(float(resp.headers.get("retry-after", 2.0)), 10.0))
                continue
            if 500 <= resp.status_code < 600 and attempt < attempts:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text[:300]
            if resp.status_code == 401 and attempt < attempts and "timestamp" in json.dumps(body).lower():
                await asyncio.to_thread(sync_server_time, True)
                continue
            if resp.status_code >= 400:
                raise KalshiError(resp.status_code, body)
            return body
    if last:
        raise last
    raise KalshiError(0, "request failed")


def error_text(exc: Exception) -> str:
    """The exchange's own words for a refusal, for the bet log."""
    if isinstance(exc, KalshiError):
        body = exc.body
        if isinstance(body, dict):
            err = body.get("error") if isinstance(body.get("error"), dict) else body
            message = err.get("message") or err.get("details") or err.get("code")
            if message:
                return f"Kalshi: {message}"[:300]
        return f"Kalshi {exc.status}: {str(body)[:260]}"
    return f"{type(exc).__name__}: {exc}"[:300]


# ------------------------------------------------------------------ market data

_market_cache: dict[str, tuple[float, dict]] = {}
_series_cache: dict[str, tuple[float, dict]] = {}
_META_TTL = 600.0


async def market(ticker: str) -> Optional[dict]:
    hit = _market_cache.get(ticker)
    if hit and time.monotonic() - hit[0] < _META_TTL:
        return hit[1]
    try:
        data = await _public_get(f"/markets/{ticker}")
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("kalshi: market read failed for %s: %s", ticker, exc)
        return None
    row = data.get("market") if isinstance(data, dict) else None
    if not isinstance(row, dict):
        return None
    _market_cache[ticker] = (time.monotonic(), row)
    return row


async def _series(series_ticker: str) -> Optional[dict]:
    hit = _series_cache.get(series_ticker)
    if hit and time.monotonic() - hit[0] < _META_TTL:
        return hit[1]
    try:
        data = await _public_get(f"/series/{series_ticker}")
    except (httpx.HTTPError, ValueError):
        return None
    row = data.get("series") if isinstance(data, dict) else None
    if not isinstance(row, dict):
        return None
    _series_cache[series_ticker] = (time.monotonic(), row)
    return row


async def fee_rate(ticker: str) -> Optional[float]:
    """Taker rate r in fee = r x p(1-p) per contract, or None when the series
    could not be read (the caller then uses its fallback — never zero)."""
    series = await _series((ticker or "").split("-")[0])
    if not series:
        return None
    fee_type = str(series.get("fee_type") or "")
    if fee_type and not fee_type.startswith("quadratic"):
        return None  # a fee shape this formula does not describe
    try:
        multiplier = float(series.get("fee_multiplier"))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= multiplier <= 5.0):
        return None
    return BASE_TAKER_RATE * multiplier


def fee_usd(shares: float, prob: float, rate: float = BASE_TAKER_RATE) -> float:
    """What one order pays: rounded UP to the cent, per order."""
    raw = shares * rate * prob * (1.0 - prob)
    return math.ceil(raw * 100.0 - 1e-9) / 100.0 if raw > 0 else 0.0


def yes_asks(orderbook: Any) -> list[tuple[float, float]]:
    """The YES ask ladder, best (cheapest) first, from an orderbook payload.

    Kalshi publishes two BID ladders. A YES buyer lifts NO bids, so every NO
    bid at price q is YES offered at 1 - q. Dollar strings on current
    payloads (`orderbook_fp.no_dollars`), integer cents on legacy ones.
    """
    if not isinstance(orderbook, dict):
        return []
    fp = orderbook.get("orderbook_fp") or {}
    raw, scale = fp.get("no_dollars"), 1.0
    if not raw:
        raw, scale = (orderbook.get("orderbook") or {}).get("no"), 0.01
    asks: list[tuple[float, float]] = []
    for level in raw or []:
        try:
            price, size = float(level[0]) * scale, float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0.0 < price < 1.0 and size > 0:
            asks.append((round(1.0 - price, 4), size))
    asks.sort(key=lambda level: level[0])
    return asks


async def ask_depth(ticker: str, max_price_cents: int) -> Optional[dict]:
    """Same shape as polymarket_api.ask_depth: what a YES buy capped at
    `max_price_cents` could take right now. None when the book is unreadable."""
    try:
        data = await _public_get(f"/markets/{ticker}/orderbook")
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("kalshi: orderbook read failed for %s: %s", ticker, exc)
        return None
    asks = yes_asks(data)
    if not asks:
        return None
    cap = max_price_cents / 100.0
    crossable = [[px, sz] for px, sz in asks if px <= cap + 1e-9]
    return {
        "best_ask_cents": int(round(asks[0][0] * 100)),
        "depth_shares": sum(sz for _, sz in crossable),
        "levels": crossable,
    }


# ---------------------------------------------------------------------- account


def _shard_balances(data: Any) -> dict[int, float]:
    """{exchange_index: dollars}. The breakdown is fixed-point DOLLAR strings,
    unlike the top-level `balance`, which is cents."""
    out: dict[int, float] = {}
    for row in (data.get("balance_breakdown") or []) if isinstance(data, dict) else []:
        try:
            out[int(row.get("exchange_index"))] = float(row.get("balance"))
        except (TypeError, ValueError, AttributeError):
            continue
    return out


async def get_balance() -> dict:
    """{"usd": account cash across shards, "shards": {index: usd}}."""
    data = await _signed("GET", "/portfolio/balance")
    shards = _shard_balances(data)
    if shards:
        usd = sum(shards.values())
    else:
        usd = float((data or {}).get("balance") or 0) / 100.0
    return {"usd": round(usd, 2), "shards": shards}


async def transfer_between_shards(amount_usd: float, source: int, destination: int) -> dict:
    """Move collateral between two of the user's own exchange shards. Whole
    cents, rounded DOWN (through Decimal: int(2.01 * 100) is 200). Never
    retried — a retry after a lost reply would move the money twice."""
    cents = int(Decimal(str(amount_usd)) * 100)
    if cents <= 0 or source == destination:
        raise ValueError("nothing to move")
    return await _signed(
        "POST", "/portfolio/intra_exchange_instance_transfer",
        json_body={
            "source": "event_contract", "destination": "event_contract",
            "source_exchange_shard": int(source),
            "destination_exchange_shard": int(destination),
            "amount": cents * _CENTICENTS_PER_CENT,
        },
        retry=False, timeout=ORDER_TIMEOUT,
    )


_collateral_lock = asyncio.Lock()


async def ensure_collateral(ticker: str, shares: int, price_cents: int, *,
                            allow_move: bool = True) -> Optional[str]:
    """Make sure the market's shard can pay for one buy. Returns a note when
    money was moved or is short; None when nothing needed doing or nothing
    could be read (fail open: Kalshi then decides)."""
    row = await market(ticker)
    try:
        shard = int((row or {}).get("exchange_index"))
    except (TypeError, ValueError):
        return None
    need = round(shares * (price_cents + FEE_ALLOWANCE_CENTS) / 100.0, 2)
    async with _collateral_lock:
        try:
            shards = (await get_balance())["shards"]
        except Exception as exc:  # noqa: BLE001
            logger.debug("kalshi: balance unreadable before an order: %s", exc)
            return None
        if not shards:
            return None  # a key with no per-shard view: nothing to reason about
        have = shards.get(shard, 0.0)
        short = round(need - have, 2)
        if short <= 0:
            return None
        donors = sorted(((usd, idx) for idx, usd in shards.items() if idx != shard and usd > 0),
                        reverse=True)
        if not allow_move or sum(usd for usd, _ in donors) < short:
            return (f"needs ${need:.2f} on Kalshi exchange {shard}, which holds ${have:.2f}"
                    + ("" if allow_move else " (moving funds between Kalshi exchanges is off)"))
        moved = 0.0
        for usd, idx in donors:
            take = min(usd, round(short - moved, 2))
            if take < 0.01:
                break
            await transfer_between_shards(take, idx, shard)
            moved = round(moved + take, 2)
            logger.info("kalshi: moved $%.2f from exchange %s to %s for %s", take, idx, shard, ticker)
        deadline = time.monotonic() + LAND_TIMEOUT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(LAND_POLL_S)
            try:
                if (await get_balance())["shards"].get(shard, 0.0) >= need - 0.005:
                    break
            except Exception:  # noqa: BLE001
                continue
        return f"moved ${moved:.2f} to Kalshi exchange {shard} for this order"


# ----------------------------------------------------------------------- orders


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_order(resp: Any) -> dict:
    """{order_id, filled, avg_prob} from a place-order reply. Current payloads
    carry fixed-point strings (`fill_count_fp`, `taker_fill_cost_dollars`);
    legacy ones integer counts and cents."""
    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    if not isinstance(order, dict):
        return {"order_id": "", "filled": 0.0, "avg_prob": None, "status": "", "fill_known": False}
    fill_known = True
    if order.get("fill_count_fp") is not None:
        filled = _num(order.get("fill_count_fp"))
        cost = _num(order.get("taker_fill_cost_dollars")) + _num(order.get("maker_fill_cost_dollars"))
    elif order.get("taker_fill_count") is not None or order.get("maker_fill_count") is not None:
        filled = _num(order.get("taker_fill_count")) + _num(order.get("maker_fill_count"))
        cost = (_num(order.get("taker_fill_cost")) + _num(order.get("maker_fill_cost"))) / 100.0
    else:
        # The reply names the order and says nothing about fills. That is not
        # "nothing filled" — the caller asks the fills feed before deciding.
        filled, cost, fill_known = 0.0, 0.0, False
    avg = round(cost / filled, 4) if filled > 0 and cost > 0 else None
    return {
        "order_id": str(order.get("order_id") or ""), "filled": filled, "avg_prob": avg,
        "status": str(order.get("status") or "").lower(), "fill_known": fill_known,
    }


async def buy_yes_ioc(ticker: str, shares: int, price_cents: int) -> dict:
    """Buy up to `shares` YES at or under `price_cents`, immediately; the rest
    is cancelled. Returns parse_order's dict; `filled == 0` is a clean no-trade.

    Raises OrderOutcomeUnknown when the request may have reached the exchange
    but no answer came back, and KalshiError for a definite refusal."""
    if shares <= 0:
        raise ValueError("shares must be positive")
    if not (1 <= int(price_cents) <= 99):
        raise ValueError("price_cents must be 1..99")
    client_order_id = str(uuid.uuid4())
    body = {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": "bid",                       # a YES buy on the YES book
        "count": f"{int(shares)}.00",
        "price": f"{int(price_cents) / 100:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "exchange_index": -1,                # auto-route to the market's shard
    }
    try:
        resp = await _signed("POST", ORDERS_PATH, json_body=body, timeout=ORDER_TIMEOUT)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        # Retried under one client_order_id, so at most one order exists — but
        # whether it does is not known here. Ask before calling it a failure.
        found = await find_order(client_order_id, ticker)
        if found is None:
            raise OrderOutcomeUnknown(f"no answer from Kalshi ({type(exc).__name__})") from exc
        return await _settle_fill(found)
    except KalshiError as exc:
        if exc.status >= 500:
            found = await find_order(client_order_id, ticker)
            if found is None:
                raise OrderOutcomeUnknown(error_text(exc)) from exc
            return await _settle_fill(found)
        raise
    return await _settle_fill(parse_order(resp))


async def _settle_fill(parsed: dict) -> dict:
    """An immediate order's reply should carry its fill. When it does not, or
    carries a fill with no cost, the fills feed is the truth — a real fill
    must never be booked as "nothing happened"."""
    if not parsed["order_id"]:
        return parsed
    if parsed["fill_known"] and (parsed["filled"] <= 0 or parsed["avg_prob"] is not None):
        return parsed
    try:
        fill = await fills_for_order(parsed["order_id"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("kalshi: fills read failed for %s: %s", parsed["order_id"], exc)
        fill = None
    if fill is not None:
        return {**parsed, "filled": fill[0], "avg_prob": fill[1], "fill_known": True}
    if parsed["fill_known"]:
        return parsed
    if parsed["status"] in ("canceled", "cancelled"):
        return {**parsed, "filled": 0.0, "fill_known": True}
    # Accepted, fill not yet visible anywhere: report it as placed with an
    # unknown fill (filled None) so it holds its slot and is reconciled later.
    return {**parsed, "filled": None}


async def find_order(client_order_id: str, ticker: str) -> Optional[dict]:
    """The order carrying this client id, if the exchange has one. None means
    "could not tell", never "does not exist"."""
    try:
        data = await _signed("GET", "/portfolio/orders",
                             params={"ticker": ticker, "limit": 50})
    except Exception as exc:  # noqa: BLE001
        logger.debug("kalshi: order lookup failed: %s", exc)
        return None
    for order in (data.get("orders") if isinstance(data, dict) else None) or []:
        if isinstance(order, dict) and order.get("client_order_id") == client_order_id:
            return parse_order({"order": order})
    return None


async def fills_for_order(order_id: str) -> Optional[tuple[float, float]]:
    """(contracts, average YES price) actually filled on one order, or None."""
    try:
        data = await _signed("GET", "/portfolio/fills", params={"order_id": order_id, "limit": 200})
    except KalshiError as exc:
        if exc.status == 404:
            return None
        raise
    total = cost = 0.0
    for fill in (data.get("fills") if isinstance(data, dict) else None) or []:
        if not isinstance(fill, dict):
            continue
        count = _num(fill.get("count_fp")) or _num(fill.get("count"))
        if fill.get("yes_price_dollars") is not None:
            price = _num(fill.get("yes_price_dollars"))
        else:
            price = _num(fill.get("yes_price")) / 100.0
        if count > 0 and 0.0 < price < 1.0:
            total += count
            cost += count * price
    if total <= 0:
        return None
    return round(total, 4), round(cost / total, 4)


async def order_fill(order_id: str) -> Optional[tuple[float, float]]:
    """What one order ended up filling: (contracts, average YES price);
    (0.0, 0.0) when the exchange says it ended with NOTHING filled; None when
    that cannot be told yet."""
    fill = await fills_for_order(order_id)
    if fill is not None:
        return fill
    try:
        data = await _signed("GET", f"/portfolio/orders/{order_id}")
    except KalshiError:
        return None
    parsed = parse_order(data)
    if parsed["fill_known"] and parsed["filled"] <= 0 and parsed["status"] in ("canceled", "cancelled"):
        return 0.0, 0.0
    return None


async def check() -> dict:
    """One signed read with the saved key: is this account usable?"""
    if not credentials_present():
        return {"ok": False, "error": "No Kalshi API key is saved."}
    await asyncio.to_thread(sync_server_time, True)
    try:
        balance = await get_balance()
    except KalshiError as exc:
        hint = " Check the key ID and that the private key belongs to it." if exc.status in (401, 403) else ""
        return {"ok": False, "error": error_text(exc) + hint}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not reach Kalshi ({type(exc).__name__})."}
    return {"ok": True, "balance_usd": balance["usd"],
            "shards": {str(k): v for k, v in balance["shards"].items()}}

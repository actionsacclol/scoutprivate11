"""Polymarket API client — public reads + authenticated CLOB trading.

Three independent Polymarket services are used:

  Gamma   (gamma-api.polymarket.com)  — market/event metadata, discovery,
                                         tags/categories, resolution status.
  Data    (data-api.polymarket.com)   — the public trades feed (whale
                                         tracking), wallet positions, value.
  CLOB    (clob.polymarket.com)        — order books, live prices, and all
                                         authenticated trading (orders/fills).

To keep the rest of the bot (scanner, trader) almost unchanged, the read
functions here return dicts keyed the way that engine code already expects:

  market identifier  -> `ticker`            (we use the on-chain conditionId)
  prices             -> `*_dollars` (0..1 fraction)
  sizes / volume     -> `*_fp`
  taker direction    -> `taker_side` ("yes"/"no")

A binary Polymarket market has two outcomes (index 0 = "yes/up/first",
index 1 = "no/down/second"), each an ERC1155 token. The token ids live in
the Gamma `clobTokenIds` array (index-aligned with `outcomes`). Trading a
"yes" position = BUY of token[0]; "no" = BUY of token[1].

Public market data is read from the production endpoints (no auth). Signed
trading calls use the L1/L2 scheme in `polymarket_auth`.

VENDORED from krypt-polybot (python/polymarket_api.py). Keep diffs against
upstream minimal — only the `polymarket_auth` import path differs; the
money-critical order/auth code is otherwise identical to the battle-tested
PolyBot original. Scout's autobet engine (kryptscout/autobet/engine.py)
drives a small surface — check_trading_ready, get_balance,
place_limit_order(ticker=<conditionId>, ...) with conditionIds resolved by
Scout's own game↔market matcher (kryptscout/odds/polymarket.py) — but the
whole module is kept intact so future upstream syncs stay a straight copy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any, Optional

import httpx

from kryptscout.autobet import polymarket_auth as auth

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Public Polygon RPCs (fallback list) for on-chain reads. Used to read the
# pUSD collateral held by a deposit wallet (signatureType 3) — the CLOB
# balance-allowance endpoint does not surface deposit-wallet collateral
# reliably yet, so we read the ERC-20 balanceOf directly on-chain.
# Several independent public Polygon RPC providers, tried in turn. A balance
# read only fails ("all Polygon RPCs unreachable") if EVERY one is down/blocked,
# so we keep a wide, diverse set — a single provider's outage or regional block
# (users hit this) shouldn't take out the on-chain balance read.
POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://polygon.gateway.tenderly.co",
    "https://polygon.api.onfinality.io/public",
    # Extra providers (all verified live against the real balanceOf call) so a
    # provider outage / regional block can't take out the on-chain balance read.
    "https://1rpc.io/matic",
    "https://polygon-pokt.nodies.app",
    "https://rpc-mainnet.matic.quiknode.pro",
]

# Non-pUSD stablecoins on Polygon. Probed ONLY for diagnostics when the pUSD
# balance is 0, so the readiness check can distinguish "funds are sitting in
# plain USDC, not pUSD" (→ convert on Polymarket) from "wallet is genuinely
# empty / wrong address". Both are 6-decimal like pUSD.
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"      # bridged USDC.e
NATIVE_USDC_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cc03d5c3359"  # native USDC

# Per-request timeout. Kept tight (12s) on purpose: the engines run in one
# serialized loop, so a hung endpoint blocks stop-loss / settlement / the other
# engines for timeout × retries. 12s is well above the typical <2s response, so
# legitimate calls still succeed, but a pathological hang now caps the loop stall
# at ~36s instead of ~79s. (Polymarket APIs normally answer in well under 2s.)
REQUEST_TIMEOUT = 12.0
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5

_pub_client: Optional[httpx.AsyncClient] = None

# Poisoned-pool self-heal. A transient network event (Wi-Fi drop, sleep/wake,
# ISP blip) can leave the shared client's pooled keepalive connections
# permanently dead on Windows — the Proactor loop keeps handing out half-open
# sockets, so EVERY host (Gamma, Data, CLOB, *and* all Polygon RPCs) then fails
# at once with empty errors and stays that way until the PROCESS is restarted.
# The user's symptom — "have to restart to fix RPC/connection errors" — is
# exactly this. We detect it with a consecutive *cross-endpoint* failure streak
# and rebuild the client (fresh connection pool), which is what a manual restart
# did, minus the restart. The streak resets on ANY successful response, so an
# endpoint-specific outage (e.g. Polygon RPCs regionally blocked while Polymarket
# is fine) keeps resetting it and never triggers a recycle — only a genuine
# "nothing gets through" pool poison does.
_net_fail_streak = 0
_NET_FAIL_RECYCLE = 5            # consecutive terminal net failures → rebuild pool
_last_client_recycle = 0.0
_CLIENT_RECYCLE_MIN_GAP = 20.0   # never rebuild more than ~once per 20s


async def _get_client() -> httpx.AsyncClient:
    global _pub_client
    if _pub_client is None or _pub_client.is_closed:
        _pub_client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={
                "Accept": "application/json",
                "User-Agent": "KryptPolyBot/1.0",
            },
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
        )
    return _pub_client


def _note_net_ok() -> None:
    """A response (any status) came back, so the transport/pool is healthy —
    clear the failure streak."""
    global _net_fail_streak
    _net_fail_streak = 0


async def _note_net_fail() -> None:
    """Record a terminal network failure. Once the streak shows every endpoint is
    failing (pool wedged), rebuild the shared client's connection pool — the
    in-process equivalent of the restart users currently do by hand."""
    global _net_fail_streak, _last_client_recycle
    _net_fail_streak += 1
    if _net_fail_streak < _NET_FAIL_RECYCLE:
        return
    now = time.monotonic()
    if now - _last_client_recycle < _CLIENT_RECYCLE_MIN_GAP:
        return
    _last_client_recycle = now
    _net_fail_streak = 0
    logger.warning(
        "network failures across all endpoints — rebuilding HTTP client "
        "(fresh connection pool) to clear a poisoned pool"
    )
    await _recycle_client()


# Other modules keep their own httpx pools (crypto15m's spot/indicator
# client), which poison the same way ours does — a Windows sleep/wake that
# wedges this pool wedges theirs too. They register a recycle callback here
# so ONE streak detector heals every pool at once; before this, crypto15m's
# client was only recycled by the watchdog after a ≥240s loop stall, and a
# fast-fail poison (no stall) left spot/indicators dead until app restart.
_extra_recycle_hooks: list = []


def register_recycle_hook(fn) -> None:
    """fn: async callable that closes/renews that module's HTTP client."""
    if fn not in _extra_recycle_hooks:
        _extra_recycle_hooks.append(fn)


async def _recycle_client() -> None:
    """Swap in a fresh client on the next request, then close the old one.
    Swapping to None FIRST means concurrent callers immediately get a clean
    client; the dead one is closed in the background (errors swallowed)."""
    global _pub_client
    old, _pub_client = _pub_client, None
    if old is not None and not old.is_closed:
        try:
            await old.aclose()
        except Exception:
            pass
    for hook in list(_extra_recycle_hooks):
        try:
            await hook()
        except Exception:
            pass


async def close_clients() -> None:
    global _net_fail_streak
    _net_fail_streak = 0
    await _recycle_client()


class PolymarketAPIError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


# Set when the CLOB rejects an order with its region-restriction 403; read by
# the status snapshot so the UI can show a persistent "trading geoblocked"
# banner. Auto-clears after 30 min without a fresh rejection (a successful
# region change stops refreshing it).
_geoblocked_at = 0.0


def geoblock_active() -> bool:
    return (time.time() - _geoblocked_at) < 1800.0


def _to_float(v, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _json_list(v) -> list:
    """Gamma returns `outcomes`/`outcomePrices`/`clobTokenIds` as JSON-encoded
    strings. Decode tolerantly; pass through real lists."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            out = json.loads(v)
            return out if isinstance(out, list) else []
        except Exception:
            return []
    return []


# ───────── generic GET with retry ──────────────────────────────────────


async def _get(
    url: str, params: dict | None = None,
    *, timeout: float | None = None, max_retries: int | None = None,
) -> Any:
    """GET with retry. `timeout`/`max_retries` override the module defaults for
    latency-sensitive callers (e.g. the crypto snapshot, which has its own hard
    per-asset budget and must fail FAST rather than burn it on retries — the
    difference between one card briefly showing 'no market' and the whole board
    hard-timing-out to red)."""
    retries = max_retries if max_retries is not None else MAX_RETRIES
    for attempt in range(1, retries + 1):
        try:
            client = await _get_client()
            # NOTE: httpx treats timeout=None as "disable timeout", so only pass
            # it through when an override was actually given; otherwise let the
            # client default (REQUEST_TIMEOUT) apply.
            if timeout is not None:
                resp = await client.get(url, params=params, timeout=timeout)
            else:
                resp = await client.get(url, params=params)
            _note_net_ok()  # a response came back → transport/pool is healthy
            if resp.status_code == 429:
                wait = float(resp.headers.get("retry-after", 2))
                await asyncio.sleep(min(wait, 10))
                continue
            if resp.status_code >= 500 and attempt < retries:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            try:
                return resp.json() if resp.status_code < 400 else None
            except Exception:
                return None
        except (httpx.NetworkError, httpx.TimeoutException) as e:
            if attempt < retries:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            await _note_net_fail()  # terminal: count toward poisoned-pool recycle
            # Include the exception TYPE: httpx network errors often stringify
            # to "" (thousands of reasonless 'GET failed' lines in user logs).
            logger.warning(f"GET failed {url}: {type(e).__name__}: {e}")
            return None
    return None


# ───────── category from event tags ────────────────────────────────────

# Map Polymarket tag slugs → the bot's internal category buckets (which the
# scoring/edge tables in categorize.py key off).
_TAG_CATEGORY = {
    "crypto": "crypto", "bitcoin": "crypto", "ethereum": "crypto",
    "sports": "sports", "soccer": "sports", "nba": "sports", "nfl": "sports",
    "mlb": "sports", "nhl": "sports", "tennis": "sports", "ufc": "sports",
    # Tournament/league tags Gamma actually stamps (2026-07-05: World Cup
    # events carried none of the mapped slugs, fell through to the title
    # keyword scan, and "Will Jordan win on 2026-06-22?" landed in "world" —
    # which hid a +13.8c/ct soccer whale edge inside the "world" bucket).
    "fifa-world-cup": "sports", "world-cup": "sports", "football": "sports",
    "epl": "sports", "premier-league": "sports", "champions-league": "sports",
    "la-liga": "sports", "serie-a": "sports", "bundesliga": "sports",
    "wimbledon": "sports", "atp": "sports", "wta": "sports",
    "boxing": "sports", "mma": "sports", "esports": "sports",
    "golf": "sports", "f1": "sports", "formula-1": "sports",
    "cricket": "sports", "rugby": "sports", "olympics": "sports",
    "politics": "politics", "elections": "politics", "us-politics": "politics",
    "geopolitics": "politics", "world": "world",
    "economy": "economics", "economics": "economics", "business": "economics",
    "finance": "economics", "fed": "economics",
    "pop-culture": "entertainment", "entertainment": "entertainment",
    "culture": "entertainment", "mentions": "entertainment",
    "weather": "climate", "climate": "climate",
    "tech": "world", "science": "world", "ai": "world",
}


def _category_from_tags(tags: list) -> str:
    for t in tags or []:
        slug = (t.get("slug") if isinstance(t, dict) else str(t)).lower()
        if slug in _TAG_CATEGORY:
            return _TAG_CATEGORY[slug]
    return ""


# ───────── market normalisation + meta cache ───────────────────────────

# conditionId -> {yes_token, no_token, neg_risk, tick_size, min_size, slug,
#                 event_slug, question}. Populated whenever we normalise a
# market; used by the trading path to resolve a (ticker, side) into a token
# id without an extra round-trip.
_meta: dict[str, dict] = {}
_META_MAX = 8000  # cap so the 24/7 process can't grow this cache unbounded


def _fee_schedule_from_gamma(raw: dict) -> dict | None:
    """Per-market taker-fee descriptor Gamma stamps on fee-enabled markets
    (verified live 2026-07-03: `feeType: crypto_fees_v2`, `feeSchedule:
    {rate: 0.07, exponent: 1, takerOnly: true}` on the crypto up/down
    windows). Fee per share = rate × (p·(1−p))^exponent, takers only.
    Returns {"enabled": False} for an explicitly fee-free market, a
    {"enabled", "rate", "exponent"} dict when Gamma provides the schedule,
    or None when Gamma says nothing (caller falls back to category defaults).
    The CLOB's maker_base_fee/taker_base_fee=1000 fields are legacy protocol
    caps, NOT the operative rate — don't read fees from them."""
    fs = raw.get("feeSchedule")
    if "feesEnabled" not in raw and not isinstance(fs, dict):
        return None
    if not raw.get("feesEnabled"):
        return {"enabled": False}
    if not isinstance(fs, dict):
        return None
    try:
        return {
            "enabled": True,
            "rate": float(fs.get("rate") or 0.0),
            "exponent": float(fs.get("exponent") or 1.0),
        }
    except (TypeError, ValueError):
        return None


def _normalize_market(raw: dict) -> dict | None:
    """Map a Gamma market object into the dict shape the scanner/trader read
    (internal dict keys), and cache its trading meta."""
    if not isinstance(raw, dict):
        return None
    condition_id = raw.get("conditionId") or raw.get("condition_id") or ""
    if not condition_id:
        return None
    outcomes = _json_list(raw.get("outcomes"))
    prices = _json_list(raw.get("outcomePrices"))
    token_ids = _json_list(raw.get("clobTokenIds"))
    yes_token = str(token_ids[0]) if len(token_ids) > 0 else ""
    no_token = str(token_ids[1]) if len(token_ids) > 1 else ""
    yes_price = _to_float(prices[0]) if len(prices) > 0 else 0.0
    no_price = _to_float(prices[1]) if len(prices) > 1 else (1.0 - yes_price)

    # category from the parent event's tags (markets carry no category).
    category = ""
    events = raw.get("events") or []
    if isinstance(events, list) and events:
        category = _category_from_tags(events[0].get("tags") or [])
    event_slug = ""
    if isinstance(events, list) and events:
        event_slug = events[0].get("slug") or events[0].get("ticker") or ""
    if not event_slug:
        event_slug = raw.get("eventSlug") or ""

    closed = bool(raw.get("closed"))
    uma = (raw.get("umaResolutionStatus") or "").lower()
    result = ""
    settlement = None
    if closed and len(prices) >= 2:
        if prices[0] in ("1", "1.0", 1, 1.0):
            result, settlement = "yes", 1.0
        elif prices[1] in ("1", "1.0", 1, 1.0):
            result, settlement = "no", 0.0
        elif uma == "resolved":
            # snap to nearest extreme
            result = "yes" if yes_price >= 0.5 else "no"
            settlement = 1.0 if result == "yes" else 0.0

    status = "open"
    if closed:
        status = "settled" if (result or uma == "resolved") else "closed"
    elif not raw.get("active", True):
        status = "inactive"

    neg_risk = bool(raw.get("negRisk") or raw.get("neg_risk"))
    tick = _to_float(raw.get("orderPriceMinTickSize"), 0.01) or 0.01
    min_size = _to_float(raw.get("orderMinSize"), 5.0) or 5.0
    slug = raw.get("slug") or ""

    _meta[condition_id] = {
        "yes_token": yes_token,
        "no_token": no_token,
        "neg_risk": neg_risk,
        "tick_size": tick,
        "min_size": min_size,
        "slug": slug,
        "event_slug": event_slug,
        "question": raw.get("question") or "",
        "outcomes": outcomes,
        # Per-market taker-fee descriptor — the live source of truth for the
        # engine's EV gating (Polymarket mutates feeSchedule in place when
        # rates change; a hardcoded rate goes silently stale).
        "fee_schedule": _fee_schedule_from_gamma(raw),
    }
    if len(_meta) > _META_MAX:
        # FIFO-evict the oldest entries. _meta is a re-derivable cache
        # (get_market_meta re-fetches on a miss), so dropping cold markets only
        # costs an occasional round-trip — never a correctness issue.
        for _k in list(_meta.keys())[: len(_meta) - _META_MAX]:
            _meta.pop(_k, None)

    return {
        "ticker": condition_id,
        "condition_id": condition_id,
        "event_ticker": event_slug,
        "series_ticker": "",
        "title": raw.get("question") or slug or condition_id,
        "yes_sub_title": outcomes[0] if outcomes else "",
        "category": category,
        "status": status,
        "close_time": raw.get("endDate") or raw.get("endDateIso") or "",
        "slug": slug,
        "neg_risk": neg_risk,
        "yes_token": yes_token,
        "no_token": no_token,
        "tick_size": tick,
        "fee_schedule": _fee_schedule_from_gamma(raw),
        # Internal numeric keys the scanner/trader already read.
        "volume_fp": _to_float(raw.get("volumeNum"), _to_float(raw.get("volume"))),
        "volume_24h_fp": _to_float(raw.get("volume24hr")),
        "open_interest_fp": _to_float(raw.get("liquidityNum"), _to_float(raw.get("liquidity"))),
        "yes_bid_dollars": yes_price,
        "yes_ask_dollars": yes_price,
        "last_price_dollars": yes_price,
        "no_price_dollars": no_price,
        "result": result,
        "settlement_value_dollars": settlement,
    }


async def get_market_meta(ticker: str) -> dict | None:
    """Trading metadata for a conditionId (token ids, neg_risk, tick).
    Cached; fetches from Gamma on a miss."""
    m = _meta.get(ticker)
    if m and m.get("yes_token"):
        return m
    await fetch_market(ticker)
    return _meta.get(ticker)


# ───────── Gamma: markets / events ──────────────────────────────────────


async def fetch_markets(
    status: str = "open",
    limit: int = 500,
    cursor: str = "",
    series_ticker: str = "",
) -> tuple[list, str]:
    """Active, tradable markets (normalised). `cursor` is a numeric offset
    string (Gamma paginates via offset)."""
    params: dict = {
        "limit": min(int(limit), 500),
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        params["offset"] = int(cursor) if cursor else 0
    except (TypeError, ValueError):
        params["offset"] = 0
    data = await _get(f"{GAMMA_BASE}/markets", params=params)
    if not isinstance(data, list):
        return [], ""
    out = [m for m in (_normalize_market(x) for x in data) if m]
    next_cursor = (
        str(params["offset"] + len(data)) if len(data) >= params["limit"] else ""
    )
    return out, next_cursor


async def fetch_all_open_markets(max_pages: int = 10) -> list:
    out: list = []
    cursor = ""
    for _ in range(max_pages):
        m, cursor = await fetch_markets(status="open", limit=500, cursor=cursor)
        if not m:
            break
        out.extend(m)
        if not cursor:
            break
        await asyncio.sleep(0.2)
    return out


async def fetch_market(ticker: str) -> dict | None:
    """Look up a single market by conditionId (normalised).

    Gamma's /markets defaults to closed=false, so a plain condition_ids lookup
    returns NOTHING once a market has resolved — which silently stranded
    settled positions (we could never read their outcome). So if the default
    query is empty, retry including closed markets to pick up resolved ones."""
    if not ticker:
        return None
    data = await _get(f"{GAMMA_BASE}/markets", params={"condition_ids": ticker})
    if not (isinstance(data, list) and data):
        data = await _get(
            f"{GAMMA_BASE}/markets",
            params={"condition_ids": ticker, "closed": "true"},
        )
    if isinstance(data, list) and data:
        return _normalize_market(data[0])
    return None


async def fetch_markets_map(tickers, concurrency: int = 8) -> dict[str, dict]:
    """Fetch many markets concurrently → {ticker: market}, skipping misses.

    Replaces the serial `for t in tickers: await fetch_market(t)` chains in the
    resolution paths, where one hung endpoint could stall the whole pass
    (N tickers × the per-request timeout). A semaphore caps the in-flight burst
    so even a large unresolved backlog stays friendly to Gamma's rate limits
    (and fetch_market's own 429 backoff still applies per request)."""
    uniq = [t for t in dict.fromkeys(tickers) if t]
    if not uniq:
        return {}
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(tk: str):
        async with sem:
            try:
                return tk, await fetch_market(tk)
            except Exception:
                return tk, None

    pairs = await asyncio.gather(*[_one(t) for t in uniq])
    return {tk: m for tk, m in pairs if m}


_CRYPTO_INTERVAL_SEC = {"5m": 300, "15m": 900, "hourly": 3600}


async def fetch_crypto_updown(
    asset: str, interval: str = "15m", *, fast: bool = False
) -> list[dict]:
    """Active recurring "Up or Down" crypto markets for an asset.

    `fast=True` (used by the live snapshot) caps each Gamma lookup at one quick
    attempt so a slow/blocked Gamma degrades to "no market" inside the snapshot's
    per-asset budget instead of retrying ~40s and hard-timing-out the whole tab.

    Polymarket mints these on a rolling schedule with slugs like
    `btc-updown-15m-1782261900` where the trailing unix timestamp is the
    WINDOW START. We compute the current/adjacent window slugs directly
    (cheap, exact) and fall back to a crypto-tag scan if the naming differs.

    Each returned market is augmented with the REAL window timing derived
    from the slug — the Gamma `endDate`/close_time on these is a far-future
    placeholder and must NOT be used.
    """
    import time as _time
    sec = _CRYPTO_INTERVAL_SEC.get(interval, 900)
    asset_l = asset.lower()
    out: list[dict] = []
    seen: set[str] = set()
    # Snapshot path: fail fast (one short attempt) so a slow/blocked Gamma can't
    # blow the per-asset snapshot budget; the normal path keeps full retries.
    g_timeout = 4.0 if fast else None
    g_retries = 1 if fast else None

    def _augment(norm: dict, ws: int) -> dict:
        from datetime import datetime, timezone
        norm = dict(norm)
        norm["window_start_epoch"] = ws
        norm["window_close_epoch"] = ws + sec
        norm["interval_sec"] = sec
        norm["close_time"] = datetime.fromtimestamp(
            ws + sec, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        return norm

    if interval in ("5m", "15m"):
        now = int(_time.time())
        base = (now // sec) * sec
        # The four candidate windows (current, next, previous, after-next) are
        # independent Gamma lookups. Fetch them concurrently instead of one
        # after another — this serial chain of 4 round-trips per asset (×7
        # assets) was the dominant latency in every 15m-crypto snapshot.
        windows = (base, base + sec, base - sec, base + 2 * sec)
        datas = await asyncio.gather(*[
            _get(f"{GAMMA_BASE}/markets",
                 params={"slug": f"{asset_l}-updown-{interval}-{ws}"},
                 timeout=g_timeout, max_retries=g_retries)
            for ws in windows
        ])
        # Process in the original window order so dedup priority is unchanged.
        for ws, data in zip(windows, datas):
            if isinstance(data, list) and data:
                norm = _normalize_market(data[0])
                if norm and norm["ticker"] not in seen:
                    seen.add(norm["ticker"])
                    out.append(_augment(norm, ws))

    if not out:
        # Fallback: scan the crypto tag and match this asset+interval by slug.
        import re as _re
        pat = _re.compile(
            rf"^{asset_l}-updown-{interval}-(\d+)$", _re.IGNORECASE
        )
        data = await _get(
            f"{GAMMA_BASE}/markets",
            params={
                "tag_id": 21, "active": "true", "closed": "false",
                "limit": 500, "order": "startDate", "ascending": "false",
            },
            timeout=g_timeout, max_retries=g_retries,
        )
        for raw in (data if isinstance(data, list) else []):
            slug = raw.get("slug") or ""
            mobj = pat.match(slug)
            if not mobj:
                continue
            norm = _normalize_market(raw)
            if norm and norm["ticker"] not in seen:
                seen.add(norm["ticker"])
                out.append(_augment(norm, int(mobj.group(1))))
    return out


async def fetch_events(
    status: str = "open", limit: int = 200, cursor: str = ""
) -> tuple[list, str]:
    params: dict = {
        "limit": min(int(limit), 200),
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        params["offset"] = int(cursor) if cursor else 0
    except (TypeError, ValueError):
        params["offset"] = 0
    data = await _get(f"{GAMMA_BASE}/events", params=params)
    if not isinstance(data, list):
        return [], ""
    out = []
    for e in data:
        out.append({
            "event_ticker": e.get("slug") or e.get("ticker") or str(e.get("id") or ""),
            "series_ticker": "",
            "title": e.get("title") or "",
            "sub_title": "",
            "category": _category_from_tags(e.get("tags") or []),
            "status": "open",
        })
    next_cursor = str(params["offset"] + len(data)) if len(data) >= params["limit"] else ""
    return out, next_cursor


async def fetch_series(series_ticker: str) -> dict | None:
    """Polymarket has no series concept; category comes from event tags
    on each market. Kept as a stub for call-site compatibility."""
    return None


# ───────── Data API: public trades feed (whale tracking) ───────────────


async def fetch_recent_trades(limit: int = 1000) -> list:
    """Recent public taker trades, normalised to the internal shape the
    scanner reads. For each trade we resolve the direction the taker went
    LONG and express its cost as yes/no fractions.

    BUY of outcome i  → long that outcome at `price`.
    SELL of outcome i → long the OPPOSITE outcome at `1 - price`.
    """
    data = await _get(
        f"{DATA_BASE}/trades",
        params={"takerOnly": "true", "limit": min(int(limit), 1000)},
    )
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for t in data:
        try:
            price = _to_float(t.get("price"))
            size = _to_float(t.get("size"))
            idx = int(t.get("outcomeIndex") or 0)
            side = str(t.get("side") or "BUY").upper()
            if side == "BUY":
                long_idx = idx
                long_price = price
            else:  # SELL outcome i == long the opposite at (1 - price)
                long_idx = 1 - idx
                long_price = 1.0 - price
            taker_side = "yes" if long_idx == 0 else "no"
            yes_price = long_price if long_idx == 0 else (1.0 - long_price)
            no_price = 1.0 - yes_price
            tx = t.get("transactionHash") or ""
            asset = t.get("asset") or ""
            trade_id = f"{tx}-{asset}-{int(round(price*1000))}-{int(round(size))}"
            out.append({
                "trade_id": trade_id,
                "ticker": t.get("conditionId") or "",
                "event_ticker": t.get("eventSlug") or "",
                "slug": t.get("slug") or "",
                "title": t.get("title") or "",
                "yes_sub_title": t.get("outcome") or "",
                "count_fp": size,
                "yes_price_dollars": yes_price,
                "no_price_dollars": no_price,
                "taker_side": taker_side,
                "created_time": "",
                "timestamp": int(t.get("timestamp") or 0),
            })
        except Exception:
            continue
    return out


# ───────── Web (browser) URL ────────────────────────────────────────────


async def web_market_url(
    *, event_ticker: str = "", ticker: str = "", env: str = "mainnet",
) -> str:
    """polymarket.com URL for a market/event."""
    slug = (event_ticker or "").strip()
    if not slug and ticker:
        meta = _meta.get(ticker) or await get_market_meta(ticker)
        if meta:
            slug = meta.get("event_slug") or meta.get("slug") or ""
    if slug:
        return f"https://polymarket.com/event/{slug}"
    return "https://polymarket.com/"


# ───────── CLOB: order books / quotes ──────────────────────────────────


async def _clob_price(token_id: str, side: str) -> Optional[float]:
    """Best price for a token+side. side BUY → best ask, SELL → best bid."""
    if not token_id:
        return None
    data = await _get(
        f"{CLOB_BASE}/price", params={"token_id": token_id, "side": side.upper()}
    )
    if isinstance(data, dict) and data.get("price") not in (None, ""):
        try:
            return float(data["price"])
        except (TypeError, ValueError):
            return None
    return None


async def get_quote(ticker: str, side: str) -> dict:
    """{'bid_cents', 'ask_cents'} for the (yes/no) token of a market."""
    meta = await get_market_meta(ticker)
    if not meta:
        return {"bid_cents": None, "ask_cents": None}
    token = meta["yes_token"] if side.lower() == "yes" else meta["no_token"]
    # Fetch both sides concurrently — these are on the hot entry/exit path and
    # were two sequential round-trips, doubling the latency of every quote.
    ask, bid = await asyncio.gather(
        _clob_price(token, "BUY"), _clob_price(token, "SELL"),
    )
    return {
        "bid_cents": int(round(bid * 100)) if bid is not None else None,
        "ask_cents": int(round(ask * 100)) if ask is not None else None,
    }


async def updown_arb_edge(ticker: str) -> Optional[dict]:
    """Up + Down ≠ $1 arbitrage check for a binary market.

    Owning one Up token AND one Down token pays exactly $1 at resolution (one
    side settles to $1, the other to $0). So if the two best ASKS sum to LESS
    than $1, you can buy both for under a dollar and lock the difference — a
    market-neutral edge. Returns {upAsk, downAsk, sum, edgeCents} (all 0..1
    except edgeCents; edgeCents > 0 = a buyable arb, GROSS of fees), or None if
    either side has no ask. Fees still apply, so a real edge must clear them —
    this only DETECTS the gap.
    """
    meta = await get_market_meta(ticker)
    if not meta:
        return None
    yt, nt = meta.get("yes_token"), meta.get("no_token")
    if not yt or not nt:
        return None
    up, down = await asyncio.gather(_clob_price(yt, "BUY"), _clob_price(nt, "BUY"))
    if up is None or down is None or up <= 0 or down <= 0:
        return None
    s = up + down
    return {
        "upAsk": round(up, 4), "downAsk": round(down, 4),
        "sum": round(s, 4), "edgeCents": round((1.0 - s) * 100, 1),
    }


async def book_imbalance(token_id: str, levels: int = 3) -> Optional[float]:
    """Top-of-book order-flow imbalance for a token over the best `levels`
    price levels: (bidVol − askVol) / (bidVol + askVol), in [-1, 1]. +1 = all
    resting buy interest (buying pressure), −1 = all sell interest, 0 = balanced.
    None if the book has no two-sided depth."""
    if not token_id:
        return None
    data = await _get(f"{CLOB_BASE}/book", params={"token_id": token_id})
    if not isinstance(data, dict):
        return None
    n = max(1, int(levels or 1))

    def _top_vol(rows, *, best_high: bool) -> float:
        parsed: list[tuple[float, float]] = []
        for r in (rows or []):
            try:
                parsed.append((float(r.get("price", 0)), float(r.get("size", 0))))
            except (TypeError, ValueError):
                continue
        parsed.sort(key=lambda x: x[0], reverse=best_high)  # best price level first
        return sum(sz for _, sz in parsed[:n])

    bid_v = _top_vol(data.get("bids"), best_high=True)   # best bid = highest price
    ask_v = _top_vol(data.get("asks"), best_high=False)  # best ask = lowest price
    tot = bid_v + ask_v
    if tot <= 0:
        return None
    return round((bid_v - ask_v) / tot, 4)


async def ask_depth(
    ticker: str, side: str, max_price_cents: int
) -> Optional[dict]:
    """Crossable ask-side liquidity for a (market, yes/no side) up to a limit
    price: how many shares a marketable BUY capped at `max_price_cents` could
    actually take right now.

    Returns {"best_ask_cents": int, "depth_shares": float, "levels":
    [[price_dollars, size], ...]} (levels = the crossable asks, best first)
    or None when the book/token can't be read (live callers treat None as
    "ungated" — a transient CLOB hiccup must never block the money path;
    dry-run callers fail closed instead). Note the existing get_orderbook
    helper parses BIDS only — don't reuse it here."""
    meta = await get_market_meta(ticker)
    if not meta:
        return None
    token = meta.get("yes_token") if side == "yes" else meta.get("no_token")
    if not token:
        return None
    data = await _get(f"{CLOB_BASE}/book", params={"token_id": token})
    if not isinstance(data, dict):
        return None
    asks: list[tuple[float, float]] = []
    for lvl in data.get("asks") or []:
        try:
            asks.append((float(lvl["price"]), float(lvl["size"])))
        except (TypeError, ValueError, KeyError):
            continue
    if not asks:
        return None
    asks.sort(key=lambda x: x[0])  # best ask (lowest price) first
    cap = max_price_cents / 100.0
    crossable = [[px, sz] for px, sz in asks if px <= cap + 1e-9]
    return {
        "best_ask_cents": int(round(asks[0][0] * 100)),
        "depth_shares": sum(sz for _, sz in crossable),
        "levels": crossable,
    }


async def get_orderbook(ticker: str) -> dict:
    """Normalised {'yes':[[cents,size],...], 'no':[...]} built from
    the two token books' bids. Best-effort."""
    meta = await get_market_meta(ticker)
    out: dict = {"yes": [], "no": []}
    if not meta:
        return out
    for side, token in (("yes", meta["yes_token"]), ("no", meta["no_token"])):
        if not token:
            continue
        data = await _get(f"{CLOB_BASE}/book", params={"token_id": token})
        if isinstance(data, dict):
            for lvl in data.get("bids") or []:
                try:
                    out[side].append(
                        [int(round(float(lvl["price"]) * 100)), float(lvl["size"])]
                    )
                except (TypeError, ValueError, KeyError):
                    continue
    return out


# ───────── authenticated CLOB (L2) ─────────────────────────────────────


def _is_stale_creds_error(body: Any) -> bool:
    """True if a CLOB error body indicates the L2 API creds are stale/invalid for
    the current signer — recoverable by re-deriving. Most importantly the
    post-key-change "the order signer address has to be the address of the api
    key", plus generic api-key/unauthorized rejections."""
    s = (body if isinstance(body, str) else str(body)).lower()
    return (
        "signer address has to be the address of the api key" in s
        or "invalid api key" in s
        or "api key" in s and ("unauthorized" in s or "not found" in s or "invalid" in s)
        or "unauthorized" in s
    )


def _is_signer_mismatch_error(body: Any) -> bool:
    """The CLOB's 'order signer address has to be the address of the api key'
    rejection specifically. Unlike generic stale creds, re-deriving CANNOT fix
    this: it means the imported key isn't the authorized signer of the configured
    deposit wallet, so the api key (derived from that key) will never match the
    order's funder/signer. A user-config problem, not a transient one."""
    s = (body if isinstance(body, str) else str(body)).lower()
    return "signer address has to be the address of the api key" in s


def _is_maker_not_allowed_error(body: Any) -> bool:
    """The CLOB's 'maker address not allowed, please use the deposit wallet flow'
    rejection: the order's `maker` is a plain EOA. Polymarket only matches orders
    whose maker is a Polymarket deposit (proxy) wallet."""
    s = (body if isinstance(body, str) else str(body)).lower()
    return "maker address not allowed" in s


# Surfaced verbatim when an order is rejected for a key↔deposit-wallet mismatch.
# Kept ≤200 chars so it survives the executor's str(e)[:200] truncation intact.
_SIGNER_MISMATCH_HELP = (
    "Order rejected: your imported key isn't the authorized signer for the "
    "configured deposit wallet. In API Keys, paste your Polymarket Profile/"
    "deposit address AND import the key that controls it."
)

# Same budget: this rides through PolymarketAPIError's "HTTP {status}: " prefix
# and the executors' str(e)[:200] truncation.
_NO_DEPOSIT_WALLET_HELP = (
    "No deposit wallet set, so orders are signed with your raw wallet as maker "
    "and Polymarket rejects them. In API Keys, paste your Polymarket Profile/"
    "deposit address (no need to re-import your key)."
)


async def _reset_api_creds() -> None:
    """Drop the cached/derived L2 creds and re-derive them for the CURRENT signer."""
    global _creds_derived, _allowance_synced
    _creds_derived = False
    _allowance_synced = False  # new signer/session → its server cache may be stale too
    try:
        auth.clear_api_creds()
    except Exception:
        pass
    await ensure_api_creds()


async def _authed_request(
    method: str, path: str, *, json_body: dict | list | None = None,
    params: dict | None = None, idempotent: bool = True,
) -> Any:
    """Authenticated CLOB request. Serialises the body once, signs that exact
    string (HMAC L2), and sends it as raw content. On a stale/invalid-creds
    rejection it re-derives the L2 creds once and retries, so a wallet/key change
    self-heals instead of the user having to hit Wallet → Test.

    `idempotent=False` (used for POST /order) disables the automatic retry on an
    AMBIGUOUS failure — a network timeout or a 5xx — because the request may have
    already mutated server state (the order could have reached the CLOB before the
    response was lost). Re-firing it would risk a double fill, so we surface the
    error and let the order poller reconcile what actually landed. 429s and
    auth-refresh retries are still safe (the order was rejected before matching)."""
    await ensure_api_creds()
    method = method.upper()
    body_str = ""
    if json_body is not None:
        body_str = json.dumps(json_body, separators=(",", ":"))
    last_exc: Optional[Exception] = None
    creds_refreshed = False
    for attempt in range(1, MAX_RETRIES + 1):
        headers = auth.l2_headers(method, path, body_str)
        headers["Content-Type"] = "application/json"
        client = await _get_client()
        try:
            resp = await client.request(
                method, f"{CLOB_BASE}{path}",
                headers=headers,
                content=body_str if body_str else None,
                params=params,
                # Never follow redirects on an AUTHED call: httpx doesn't strip our
                # custom POLY_API_KEY/POLY_PASSPHRASE headers on a cross-host
                # redirect, so a redirect could forward the API secret off-Polymarket.
                follow_redirects=False,
            )
            _note_net_ok()  # a response came back → transport/pool is healthy
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_exc = e
            # A non-idempotent mutation (POST /order) must NOT be silently re-fired
            # on an ambiguous network failure — the order may already have reached
            # the CLOB, so a blind retry risks a DOUBLE FILL. Surface it instead and
            # let the order poller reconcile whether it actually rested.
            if idempotent and attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            await _note_net_fail()  # terminal: count toward poisoned-pool recycle
            raise
        if resp.status_code == 429:
            await asyncio.sleep(min(float(resp.headers.get("retry-after", 2)), 10))
            continue
        # Same idempotency guard: a 5xx after a POST /order is ambiguous (the order
        # may have been accepted before the server erred), so don't re-fire a
        # non-idempotent mutation — fall through to raise and reconcile.
        if 500 <= resp.status_code < 600 and idempotent and attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_BACKOFF * attempt)
            continue
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        # Self-heal stale/invalid L2 creds (e.g. after a wallet/key change): clear
        # them, re-derive for the current signer, and retry ONCE. Without this the
        # bot stays "could not connect" until a manual Wallet → Test.
        # NOTE: this deliberately retries on a signer/api-key mismatch too. That
        # string is ambiguous: it also fires when the CACHED creds belong to a
        # previous signer (a key change), which re-deriving DOES fix. The retry is
        # bounded to once by creds_refreshed, and a genuinely mismatched
        # key↔deposit pairing falls through to _SIGNER_MISMATCH_HELP below.
        if (
            not creds_refreshed
            and (resp.status_code == 401
                 or (resp.status_code in (400, 403) and _is_stale_creds_error(body)))
        ):
            creds_refreshed = True
            logger.warning("CLOB rejected creds — re-deriving for the current signer and retrying")
            try:
                await _reset_api_creds()
            except Exception as e:
                logger.warning(f"creds re-derive failed: {e}")
            continue
        if resp.status_code >= 400:
            # A key↔deposit-wallet mismatch can't be self-healed by re-deriving
            # (we already tried above), and the raw CLOB string is opaque to the
            # user. Surface the actual fix; keep the raw body in the log for us.
            if _is_signer_mismatch_error(body):
                logger.warning(f"CLOB signer/api-key mismatch (raw): {body}")
                raise PolymarketAPIError(resp.status_code, _SIGNER_MISMATCH_HELP)
            if _is_maker_not_allowed_error(body):
                logger.warning(f"CLOB rejected the order's EOA maker (raw): {body}")
                raise PolymarketAPIError(resp.status_code, _NO_DEPOSIT_WALLET_HELP)
            if "restricted in your region" in str(body).lower():
                # Geoblock: market data still flows, so the app looks healthy
                # while EVERY order placement bounces with this 403 — observed
                # live 2026-07-09 (user traded fine for weeks, then their
                # IP/VPN changed and the bot went silently orderless). Flag it
                # so the status snapshot / UI can show a persistent banner
                # instead of burying it in per-position error rows.
                global _geoblocked_at
                _geoblocked_at = time.time()
                logger.warning(
                    "Polymarket CLOB geoblock: trading is restricted from this "
                    "IP/region — orders will keep failing until the connection "
                    "exits the blocked region (e.g. VPN)"
                )
            raise PolymarketAPIError(resp.status_code, body)
        return body
    if last_exc:
        raise last_exc
    raise RuntimeError("exhausted retries without response")


_creds_derived = False
# Throttle for the "wallet key not configured" api-cred warning (see
# ensure_api_creds) — a persistent setup state, not a transient error.
_last_nokey_log_t = 0.0


async def _sync_clock_offset() -> None:
    """Fetch the CLOB server time and record the local↔server offset (via
    auth.set_time_offset) so signed L1/L2 timestamps compensate for a wrong system
    clock. Polymarket validates request-timestamp freshness, so a skewed clock
    otherwise rejects every signed request as stale — an opaque 'stale creds' loop
    the re-derive can't fix. Best-effort: on any failure the offset is unchanged."""
    try:
        client = await _get_client()
        resp = await client.get(f"{CLOB_BASE}/time", timeout=5.0)
        if resp.status_code >= 400:
            return
        server_ts = float((resp.text or "").strip().strip('"'))
        offset = server_ts - time.time()
        auth.set_time_offset(offset)
        if abs(offset) > 30:
            logger.warning(
                f"system clock is {offset:+.0f}s off the Polymarket server — "
                "compensating for signing; enable automatic time sync on this machine"
            )
    except Exception as e:
        logger.debug(f"clock offset sync failed (non-fatal): {e}")


_allowance_synced = False


async def refresh_balance_allowance(force: bool = False) -> bool:
    """Ask the CLOB to refresh its server-side cached balance/allowance for
    this account (GET /balance-allowance/update, L2-authed). The operator
    validates orders against that cache, and a stale zero after a deposit,
    redemption, or fresh session is the most common cause of silent
    'not enough balance / allowance' V2 rejections. Fired once per session
    (re-armed on a creds reset); `force=True` refreshes again on demand.
    Best-effort — a failure never blocks the caller."""
    global _allowance_synced
    if _allowance_synced and not force:
        return True
    try:
        await _authed_request(
            "GET", "/balance-allowance/update",
            params={
                "asset_type": "COLLATERAL",
                "signature_type": str(auth.get_signature_type()),
            },
        )
        _allowance_synced = True
        logger.info("CLOB balance/allowance server cache refreshed")
        return True
    except Exception as e:
        logger.debug(f"balance-allowance refresh failed (non-fatal): {e}")
        return False


async def ensure_api_creds() -> None:
    """Derive (or create) L2 API credentials from the wallet key if we don't
    have them yet. Idempotent. On the first pass of a session it also nudges
    the CLOB to refresh its cached balance/allowance (see
    refresh_balance_allowance) so the first order isn't rejected on a stale
    server-side zero."""
    global _creds_derived
    if _creds_derived or auth.has_api_creds():
        first_pass = not _creds_derived
        _creds_derived = True
        if first_pass:
            # Safe to call here: _creds_derived is already True, so the nested
            # ensure_api_creds() inside _authed_request returns immediately.
            await refresh_balance_allowance()
        return
    # Correct our clock to the server BEFORE signing L1 headers below — a wrong
    # system clock would otherwise make the derive request reject as stale.
    await _sync_clock_offset()
    client = await _get_client()
    # 1) Try to derive existing creds (GET, L1-signed).
    global _last_nokey_log_t
    for path, verb in (("/auth/derive-api-key", "GET"), ("/auth/api-key", "POST")):
        try:
            headers = auth.l1_headers(nonce=0)
            # follow_redirects=False: this L1-signed call carries POLY_SIGNATURE +
            # POLY_ADDRESS; don't let a cross-host redirect forward them.
            resp = await client.request(
                verb, f"{CLOB_BASE}{path}", headers=headers, follow_redirects=False,
            )
            data = resp.json() if resp.status_code < 400 else None
        except Exception as e:
            # "wallet key not configured" is a stable setup state, not a
            # transient fault — it re-fails identically every poll (~2,900
            # log lines/day for a user who simply hasn't connected a wallet;
            # the UI already shows Not Connected). Say it every 10min, not
            # every attempt. Real (transient) errors keep full verbosity.
            if "not configured" in str(e):
                if time.time() - _last_nokey_log_t >= 600:
                    _last_nokey_log_t = time.time()
                    logger.warning(
                        f"api-cred {verb} {path} failed: {e} "
                        "(connect a wallet in Settings; muted for 10min)"
                    )
            else:
                logger.warning(f"api-cred {verb} {path} failed: {e}")
            continue
        if isinstance(data, dict) and data.get("apiKey"):
            auth.set_api_creds({
                "apiKey": data["apiKey"],
                "secret": data.get("secret") or data.get("apiSecret") or "",
                "passphrase": data.get("passphrase") or data.get("apiPassphrase") or "",
            })
            _creds_derived = True
            logger.info("Polymarket API credentials ready")
            await refresh_balance_allowance()
            return
    raise RuntimeError("could not derive Polymarket API credentials from wallet key")


_BALANCE_READ_TIMEOUT = 8.0  # hard cap on the whole on-chain balance read


async def _erc20_balance_usd(token: str, address: str) -> Optional[float]:
    """On-chain 6-decimal ERC-20 balanceOf(address) at `token` → dollars.

    Races ALL public Polygon RPC fallbacks CONCURRENTLY and takes the first
    valid result, under a hard overall timeout. Parallel (not sequential) is the
    point: the read is as fast as the QUICKEST healthy RPC (~1-2s), and adding
    fallbacks only HELPS. A sequential loop over slow/dead endpoints took
    len(RPCs) × per-timeout (tens of seconds) — which blew the 30s `crypto15m:
    status` RPC (it reads the balance for the sizing preview). None only if every
    endpoint fails within the timeout."""
    addr = (address or "").strip()
    tok = (token or "").strip()
    if not addr.startswith("0x") or len(addr) != 42 or not tok.startswith("0x"):
        return None
    data = "0x70a08231" + "0" * 24 + addr[2:].lower()  # balanceOf(addr)
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": tok, "data": data}, "latest"],
    }
    client = await _get_client()

    async def _one(rpc: str) -> Optional[float]:
        resp = await client.post(rpc, json=body, timeout=_BALANCE_READ_TIMEOUT)
        res = resp.json().get("result")
        if isinstance(res, str) and res.startswith("0x"):
            return int(res, 16) / 1e6
        return None

    tasks = [asyncio.create_task(_one(rpc)) for rpc in POLYGON_RPCS]
    result: Optional[float] = None
    try:
        for fut in asyncio.as_completed(tasks, timeout=_BALANCE_READ_TIMEOUT):
            try:
                val = await fut
            except Exception as e:
                logger.debug(f"polygon rpc failed: {e}")
                continue
            if val is not None:
                result = val
                break
    except asyncio.TimeoutError:
        logger.debug("polygon balance read timed out (all RPCs slow/unreachable)")
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    # Feed the poisoned-pool detector: one healthy RPC clears the streak; ALL of
    # them failing is a strong "nothing gets through" signal. (A genuine
    # RPC-only block stays harmless — successful Gamma/CLOB/Data calls in the
    # same loop keep resetting the streak, so a recycle only fires when every
    # endpoint is down.)
    if result is not None:
        _note_net_ok()
    else:
        await _note_net_fail()
    return result


async def _pusd_balance_usd(address: str) -> Optional[float]:
    """On-chain pUSD balanceOf(address) → dollars. None if every RPC fails."""
    return await _erc20_balance_usd(auth.COLLATERAL_PUSD_ADDRESS, address)


async def _rpc_first(method: str, params: list) -> Optional[str]:
    """One JSON-RPC call, racing all Polygon fallbacks, first valid result wins."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    client = await _get_client()

    async def _one(rpc: str) -> Optional[str]:
        resp = await client.post(rpc, json=body, timeout=_BALANCE_READ_TIMEOUT)
        res = resp.json().get("result")
        return res if isinstance(res, str) else None

    tasks = [asyncio.create_task(_one(rpc)) for rpc in POLYGON_RPCS]
    try:
        for fut in asyncio.as_completed(tasks, timeout=_BALANCE_READ_TIMEOUT):
            try:
                val = await fut
            except Exception:
                continue
            if val is not None:
                return val
    except asyncio.TimeoutError:
        pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return None


# EIP-1167 minimal-proxy runtime prefix; the 20 bytes after it are the impl addr.
_EIP1167_MARKER = "363d3d373d3d3d363d73"
# Polymarket's deposit wallets are NOT EIP-1167: their runtime is
# `363d3d373d3d363d7f<slot>54 5af43d…` — PUSH32 <slot>; SLOAD — i.e. the
# implementation address lives in the standard EIP-1967 storage slot
# (keccak256("eip1967.proxy.implementation") - 1) rather than inline in the code.
_EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
_SIG_ISVALIDSIGNATURE = "1626ba7e"   # EIP-1271 magic selector (present iff type 3)
_SEL_GET_OWNERS = "0xa0e67e2b"       # Gnosis Safe getOwners()


async def _proxy_impl_address(funder: str, code: str) -> str:
    """The implementation address behind `funder`, or "" if it isn't a proxy we
    recognise. Handles both the inline EIP-1167 form and the EIP-1967 storage
    slot that Polymarket's own deposit wallets use."""
    if _EIP1167_MARKER in code:
        i = code.index(_EIP1167_MARKER) + len(_EIP1167_MARKER)
        return "0x" + code[i:i + 40]
    try:
        raw = await _rpc_first(
            "eth_getStorageAt", [funder, _EIP1967_IMPL_SLOT, "latest"])
        if raw and int(raw, 16) != 0:
            return "0x" + raw[-40:]
    except Exception:
        pass
    return ""


async def detect_wallet_signature_type() -> Optional[int]:
    """Best-effort on-chain detection of the deposit wallet's Polymarket
    signature scheme, so orders are signed the way the CLOB expects instead of
    blindly assuming POLY_1271.

    Polymarket hands different account types different deposit wallets:
      • Gmail/email  → an EIP-1967-slot proxy over a 1271 impl → POLY_1271 (3)
      • MetaMask     → a Gnosis Safe                           → POLY_GNOSIS_SAFE (2)
      • some proxies → an impl with no EIP-1271 entrypoint     → POLY_PROXY (1)
      • raw key      → the EOA itself (no funder)              → EOA (0)

    The overwhelmingly common case is the Gmail/email signup we tell users to
    make, and it is type 3 — do not "correct" it. Returns a SignatureType int
    ONLY on positive evidence and None otherwise, so the caller keeps its stored
    default (3) whenever the chain doesn't clearly say something else. Read only;
    places no orders and moves no funds."""
    funder = (auth.get_funder() or "").strip()
    if not funder:
        return auth.SIGNATURE_TYPE_EOA
    try:
        eoa = (auth.get_address() or "").strip()
    except Exception:
        eoa = ""
    if eoa and funder.lower() == eoa.lower():
        return auth.SIGNATURE_TYPE_EOA
    code = (await _rpc_first("eth_getCode", [funder, "latest"]) or "").lower()
    if len(code) <= 2:
        return None  # no bytecode (undeployed/wrong address) — don't override
    # Gnosis Safe? getOwners() returns an address[]; the EOA must be an owner.
    if eoa:
        owners_res = (await _rpc_first(
            "eth_call", [{"to": funder, "data": _SEL_GET_OWNERS}, "latest"]) or "")
        if len(owners_res) > 2:
            try:
                b = owners_res[2:]
                cnt = int(b[64:128], 16)
                owners = {"0x" + b[128 + i * 64 + 24: 128 + (i + 1) * 64].lower()
                          for i in range(cnt)}
                if eoa.lower() in owners:
                    return auth.SIGNATURE_TYPE_POLY_GNOSIS_SAFE
            except Exception:
                pass
    # EIP-1271? Check the wallet's own code, and (if it's a proxy) the code of the
    # implementation it delegates to. Presence of isValidSignature ⇒ POLY_1271.
    impl_code = code
    impl = await _proxy_impl_address(funder, code)
    if impl:
        impl_code = (await _rpc_first("eth_getCode", [impl, "latest"]) or "").lower()
        if len(impl_code) <= 2:
            # Implementation-code read failed transiently (_rpc_first returns None,
            # not raise, on an RPC blip). We CANNOT tell a real POLY_PROXY (impl
            # lacks EIP-1271) apart from a POLY_1271 whose impl we simply failed to
            # read — so ABSTAIN (return None) and keep the caller's stored type.
            # Falling through to POLY_PROXY here would persist type 1 on a working
            # type-3 wallet and get every order rejected until a later clean read.
            return None
    if _SIG_ISVALIDSIGNATURE in (impl_code or ""):
        return auth.SIGNATURE_TYPE_POLY_1271
    if impl:
        # We resolved a real implementation and it has no EIP-1271 entrypoint:
        # positive evidence of the Magic/email proxy.
        return auth.SIGNATURE_TYPE_POLY_PROXY
    # A deployed contract that is not a Safe, not a proxy we recognise, and shows
    # no EIP-1271 selector. That is an ABSENCE of evidence, not evidence of
    # POLY_PROXY — concluding type 1 here downgraded working POLY_1271 deposit
    # wallets (whose isValidSignature lives in an implementation we failed to
    # resolve) and got every one of their orders rejected. Abstain.
    return None


# DepositWallet (the current-generation Polymarket wallet, impl 0x58ca52eb…) view
# selectors. keccak("owner()")[:4] and keccak("sessionSignerAuthorizedUntil(address)")[:4].
_SEL_OWNER = "0x8da5cb5b"
_SEL_SESSION_SIGNER_UNTIL = "0xb9ac71d6"


async def check_deposit_wallet_authorization() -> dict:
    """Is the imported key allowed to sign for the configured deposit wallet?

    The current-generation deposit wallet exposes `owner()`, so we can answer this
    with one eth_call — offline, free, no order placed, no balance required. The
    legacy EIP-1167 proxy exposes no owner getter, so we abstain there rather than
    guess (the live 1¢ probe remains the fallback).

    Returns {ok, tested, reason, owner}. ok=False ONLY on positive proof of a
    mismatch — a failed read is always {ok: True, tested: False}, never a reject.
    """
    out = {"ok": True, "tested": False, "reason": "", "owner": ""}
    try:
        funder = auth.get_funder()
        signer = (auth.get_address() or "").strip()
    except Exception as e:
        out["reason"] = f"inconclusive ({type(e).__name__})"
        return out
    if not funder or not signer:
        return out
    raw = await _rpc_first("eth_call", [{"to": funder, "data": _SEL_OWNER}, "latest"])
    if not raw or len(raw) < 42:
        # Legacy proxy (no owner()) or an RPC blip — cannot prove anything.
        out["reason"] = "wallet exposes no owner() — not checked"
        return out
    owner = "0x" + raw[-40:]
    if int(owner, 16) == 0:
        out["reason"] = "owner() returned the zero address — not checked"
        return out
    out["owner"] = owner
    out["tested"] = True
    if owner.lower() == signer.lower():
        out["reason"] = "imported key owns this deposit wallet"
        return out
    # Not the owner — Polymarket can also authorize a time-boxed session signer.
    until = 0
    sess = await _rpc_first("eth_call", [
        {"to": funder, "data": _SEL_SESSION_SIGNER_UNTIL + signer[2:].lower().rjust(64, "0")},
        "latest",
    ])
    try:
        until = int(sess, 16) if sess and sess != "0x" else 0
    except (TypeError, ValueError):
        until = 0
    if until > time.time():
        out["reason"] = "imported key is an authorized session signer"
        return out
    out["ok"] = False
    out["reason"] = (
        f"Deposit wallet {funder[:6]}…{funder[-4:]} is owned by {owner[:6]}…{owner[-4:]}, "
        f"not by your imported key {signer[:6]}…{signer[-4:]}. Import the key for THAT "
        "wallet, or paste the deposit wallet that belongs to this key."
    )
    return out


async def get_balance() -> dict:
    """{'balance': cash_cents, 'portfolio_value': portfolio_cents}.

    Cash = collateral balance. For a deposit wallet (signatureType 3) this is
    the pUSD held on-chain by the funder address; for a raw EOA it comes from
    the CLOB balance-allowance endpoint (1e6 units → cents). Portfolio = sum of
    position market value from the Data API /value endpoint, keyed by whichever
    address actually holds the positions.
    """
    cash_cents = 0
    funder = auth.get_funder()
    if funder:
        # Deposit-wallet mode: read pUSD held by the deposit wallet on-chain.
        usd = await _pusd_balance_usd(funder)
        if usd is None:
            # Genuine read failure (every Polygon RPC unreachable). RAISE so the
            # caller's balance cache (trader.refresh_balance) serves the
            # last-known value instead of flashing the displayed balance to $0.
            raise RuntimeError("deposit-wallet balance read failed (all Polygon RPCs unreachable)")
        cash_cents = int(round(usd * 100))
    else:
        # Raw-EOA mode: collateral from the CLOB (needs L2). Mirror the deposit
        # path: on a read FAILURE (or a response missing the balance field) RAISE
        # so the caller (trader.refresh_balance) serves the last-known value
        # instead of caching a false $0 — which halts trading (the `balance < $5`
        # skip) and flashes $0 on a fully-funded wallet during a transient blip.
        # Only an affirmative numeric balance (a real 0 included) is trusted.
        try:
            data = await _authed_request(
                "GET", "/balance-allowance",
                params={"asset_type": "COLLATERAL", "signature_type": "0"},
            )
        except Exception as e:
            raise RuntimeError(f"raw-EOA balance-allowance read failed: {e}")
        bal = data.get("balance") if isinstance(data, dict) else None
        if bal is None:
            raise RuntimeError("raw-EOA balance-allowance response missing 'balance'")
        cash_cents = int(round(float(bal) / 1e6 * 100))
    # Portfolio value — public, keyed by the address holding the positions.
    # A genuine read FAILURE returns None (= "unknown") so the caller keeps the
    # last-known portfolio instead of letting total collapse to cash-only — which
    # would make open positions look like a realized loss right after a fill
    # (cash already debited on-chain, /value not yet caught up or transiently
    # down). An empty list is a real "no open positions" → 0.
    port_value: Optional[int] = None
    try:
        addr = funder or auth.get_address()
        data = await _get(f"{DATA_BASE}/value", params={"user": addr})
        if isinstance(data, list):
            port_value = int(round(float(data[0].get("value") or 0) * 100)) if data else 0
        else:
            logger.debug(f"/value unexpected shape: {type(data).__name__}")
    except Exception as e:
        logger.debug(f"/value failed: {e}")
    return {"balance": cash_cents, "portfolio_value": port_value}


def _extract_allowance(data) -> Optional[float]:
    """Pull a collateral allowance out of the CLOB balance-allowance
    response, tolerating field-name variations across CLOB versions."""
    if not isinstance(data, dict):
        return None
    for k in ("allowance", "allowances"):
        v = data.get(k)
        if v is None:
            continue
        if isinstance(v, dict):
            try:
                return max((float(x) for x in v.values()), default=0.0)
            except (TypeError, ValueError):
                return None
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


# Live signer-check cache keyed by (signer, funder). The key↔deposit-wallet
# authorization can't spontaneously break, so once a pairing is proven we never
# re-place a test order for it — only an actual wallet change (new key/funder,
# i.e. a new cache key) re-runs the probe. Only DEFINITIVE verdicts are cached; a
# transient/inconclusive run is left uncached so it's retried next time.
_signer_check_cache: dict[tuple[str, str], dict] = {}


async def verify_order_signer_live() -> dict:
    """Prove the imported key is the authorized signer of the configured deposit
    wallet by submitting ONE tiny, non-crossing FAK order and reading the CLOB's
    verdict. Returns {ok, tested, reason}.

    The supported setup is a Polymarket Gmail/email account, signing as
    signatureType 3 (POLY_1271) with the funder = the Profile/deposit address and
    the api key derived from the exported EOA. That pairing can't be proven
    offline (the CLOB validates the key↔deposit-wallet authorization on-chain), so
    `check_trading_ready`'s "approvals look OK" can pass while EVERY real order is
    rejected with "the order signer address has to be the address of the API KEY"
    — which means THIS wallet's imported key isn't the signer of THIS deposit
    address (a mismatched pairing). This live probe catches that in one click.

    Safety: a BUY at 1¢ crosses nothing (nothing sells at 1¢) and FAK kills the
    unfilled remainder instantly — nothing rests, nothing fills, no cancel needed.
    The CLOB only reaches "no liquidity to match" AFTER it has accepted (i.e.
    signature/signer-validated) the order, so a non-crossing FAK is a reliable
    probe: a bad pairing is rejected at validation; a good one returns "unmatched".

    ok=False is returned ONLY on a proven mismatch. ok=True with tested=False
    means no conclusive probe ran (no market / transient / too little balance) —
    callers must NOT treat that as a failure."""
    # Raw-EOA (type 0) never reaches the signer check: Polymarket rejects the EOA
    # `maker` first. Probing would just place a doomed order, so report the real
    # verdict offline. Every deposit-wallet scheme (POLY_PROXY 1, GNOSIS_SAFE 2,
    # POLY_1271 3) CAN mispair with the imported key, so those get the live probe.
    if auth.get_signature_type() == auth.SIGNATURE_TYPE_EOA:
        return {"ok": False, "tested": True, "reason": _NO_DEPOSIT_WALLET_HELP}
    signer = (auth.get_address() or "").strip().lower()
    funder = (auth.get_funder() or "").strip().lower()
    cached = _signer_check_cache.get((signer, funder))
    if cached is not None:
        return {**cached, "tested": True}
    # Pick the most-liquid open market where a 1¢ buy is unambiguously non-crossing.
    try:
        markets, _ = await fetch_markets(limit=50)
    except Exception as e:
        return {"ok": True, "tested": False, "reason": f"no test market ({type(e).__name__})"}
    target = next(
        (m for m in markets
         if m.get("yes_token") and m.get("status") == "open"
         and 0.10 <= _to_float(m.get("yes_bid_dollars")) <= 0.90),
        None,
    )
    if not target:
        return {"ok": True, "tested": False, "reason": "no suitable test market"}
    meta = await get_market_meta(target["ticker"]) or {}
    # Size to clear BOTH the per-order minimum and ~$1 notional (Polymarket's
    # floor) at 1¢, so the CLOB reaches the signer check rather than short-
    # circuiting on min-size/notional. 110 shares × 1¢ = $1.10.
    count = max(int(math.ceil(_to_float(meta.get("min_size"), 5.0))), 110)
    try:
        await place_limit_order(
            ticker=target["ticker"], side="yes", action="buy",
            count=count, price_cents=1, order_type="FAK",
        )
    except PolymarketAPIError as e:
        # _authed_request translates the raw CLOB string to _SIGNER_MISMATCH_HELP,
        # so match BOTH the translated help text and the raw string.
        if _is_signer_mismatch_error(e.body) or str(e.body) == _SIGNER_MISMATCH_HELP:
            verdict = {"ok": False, "reason": _SIGNER_MISMATCH_HELP}
            _signer_check_cache[(signer, funder)] = verdict
            return {**verdict, "tested": True}
        # Any other rejection (balance / min-size / market state) is NOT a signer
        # verdict — report inconclusive rather than false-failing the wallet.
        return {"ok": True, "tested": False, "reason": f"inconclusive (CLOB {e.status})"}
    except Exception as e:
        return {"ok": True, "tested": False, "reason": f"inconclusive ({type(e).__name__})"}
    # Accepted (returned unmatched/filled, not a mismatch) ⇒ the key IS the
    # authorized signer of the deposit wallet.
    verdict = {"ok": True, "reason": "signer verified"}
    _signer_check_cache[(signer, funder)] = verdict
    return {**verdict, "tested": True}


async def check_trading_ready() -> dict:
    """Preflight before live trading: confirm the wallet can actually place
    CLOB orders. Derives API creds, checks USDC collateral balance, and
    best-effort checks that the on-chain trading approvals (allowances) are
    set. Returns a dict for the UI/logs; never raises.

    `approvalsOk` is True/False when the CLOB reports an allowance, or None
    when it can't be determined (then a real order is the only sure test)."""
    issues: list[str] = []
    address = ""
    balance_usd = 0.0
    approvals_ok: Optional[bool] = None
    try:
        funder = auth.get_funder()
        sig_type = auth.get_signature_type()
    except auth.WalletMetaError as e:
        return {
            "ok": False, "address": "", "balanceUsd": 0.0, "approvalsOk": None,
            "issues": [f"deposit-wallet setting unreadable ({e}) — re-save it in API Keys"],
        }
    try:
        await ensure_api_creds()
        address = auth.trading_address()
    except Exception as e:
        return {
            "ok": False, "address": "", "balanceUsd": 0.0, "approvalsOk": None,
            "issues": [f"could not derive Polymarket API credentials: {e}"],
        }

    # Deposit-wallet sanity: the funder must be the Polymarket proxy/Profile
    # address, NEVER the signer key's own address. If a user pastes their signer
    # (EOA) address into the deposit-wallet field, every POLY_1271 order is
    # rejected with "the order signer address has to be the address of the api
    # key". This is the one such mismatch we can prove offline (the rest — wrong
    # key / wrong proxy — only surface as that CLOB error on a real order, which
    # we now translate to a plain-English fix). 100% reliable, no false reject.
    signer_addr = ""
    try:
        signer_addr = auth.get_address()
    except Exception:
        pass
    if funder and signer_addr and funder.strip().lower() == signer_addr.strip().lower():
        issues.append(
            "deposit wallet equals your signer (wallet) address — paste your "
            "Polymarket Profile/deposit address there instead, not your wallet's "
            "own address (otherwise every order is rejected: signer ≠ api key)"
        )
    # No deposit wallet at all ⇒ orders carry the signer EOA as `maker`, and
    # Polymarket refuses to match those ("maker address not allowed, please use
    # the deposit wallet flow"). Readiness must not go green: `place_limit_order`
    # now blocks these before signing, so the engines would silently no-op.
    elif not funder:
        issues.append(
            "no deposit wallet configured — Polymarket rejects orders signed by a "
            "raw wallet address, so no order can fill. Paste your Polymarket "
            "Profile/deposit address into the Deposit wallet field."
        )
    # Auto-correct the signature scheme on-chain. The installer defaults ANY
    # deposit wallet to POLY_1271 (type 3), but Polymarket Magic/email accounts
    # are POLY_PROXY (1) and MetaMask are Gnosis-Safe (2) — signing those as
    # 1271 gets EVERY order rejected ("signer ≠ api key") even though the key +
    # deposit address are correct. Detection reads chain state only and overrides
    # solely on positive evidence, so it can't regress a working 1271 wallet.
    if funder:
        try:
            detected = await detect_wallet_signature_type()
            # detected == EOA with a funder configured means the user pasted their
            # own signer address. Persisting {funder, type 0} would be a
            # self-contradictory meta; the issue above already tells them to fix it.
            if detected == auth.SIGNATURE_TYPE_EOA:
                detected = None
            if detected is not None and int(detected) != int(sig_type):
                auth.set_wallet_meta(funder=funder, signature_type=int(detected))
                _names = {0: "EOA", 1: "POLY_PROXY", 2: "POLY_GNOSIS_SAFE", 3: "POLY_1271"}
                logger.info(
                    f"[wallet] corrected signatureType {sig_type}→{detected} "
                    f"({_names.get(int(detected), detected)}) for deposit {funder}"
                )
                sig_type = int(detected)
        except Exception as e:
            logger.debug(f"[wallet] signature-type detection skipped: {e}")
    balance_read_failed = False
    # Prove the imported key can actually sign for this deposit wallet. The
    # current-generation wallet answers owner() directly, so a mismatched
    # key↔wallet pairing is caught here instead of as an opaque CLOB rejection.
    if funder:
        try:
            authz = await check_deposit_wallet_authorization()
            if authz.get("tested") and not authz.get("ok"):
                issues.append(authz["reason"])
        except Exception as e:
            logger.debug(f"[wallet] deposit-wallet owner check skipped: {e}")

    try:
        bal = await get_balance()
        balance_usd = int(bal.get("balance") or 0) / 100.0
    except Exception as e:
        balance_read_failed = True
        issues.append(f"could not read balance: {e}")
    # Only an AFFIRMATIVE on-chain zero means "empty wallet". When every RPC is
    # unreachable balance_usd is still 0.0, and telling a funded user their
    # deposit wallet "holds no pUSD" sends them to re-fund a wallet that already
    # has money. Say nothing about the balance we failed to read.
    if balance_usd <= 0 and not balance_read_failed:
        # pUSD is the only collateral the CLOB trades in. A wallet can hold a
        # real balance in plain USDC (USDC.e / native) and still read 0 pUSD —
        # then the fix is "convert to pUSD on Polymarket", NOT "fund the wallet".
        # Probe is best-effort and only runs on the 0-balance path.
        usdc_usd = 0.0
        for _tok in (USDC_E_ADDRESS, NATIVE_USDC_ADDRESS):
            try:
                v = await _erc20_balance_usd(_tok, address)
                if v:
                    usdc_usd = max(usdc_usd, v)
            except Exception:
                pass
        short = f"{address[:6]}…{address[-4:]}" if address else "your wallet"
        if usdc_usd > 0:
            issues.append(
                f"found ${usdc_usd:.2f} USDC (not pUSD) at {short} — Polymarket trades in pUSD, "
                "so convert/deposit it to pUSD on Polymarket before the bot can trade"
            )
        elif funder:
            issues.append(
                f"deposit wallet {short} holds no pUSD — confirm it's the Polygon (0x) address "
                "from your Polymarket deposit screen (not the Solana one) and that it's funded"
            )
        else:
            issues.append(
                "no pUSD collateral on this raw wallet — if your funds are in a Polymarket "
                "account (email/Google login), paste your deposit-wallet (Polygon 0x) address "
                "into the Deposit wallet field, then reconnect"
            )
    # Minimum-funding floor. Below this the bot can't place its smallest order AND
    # (crucially) the sub-$1.50 range also SKIPS the live signer probe below — so a
    # barely-funded, mispaired wallet would otherwise show a FALSE GREEN and then
    # reject every real order (the worst first-run experience). $5 covers the
    # default 5-contract minimum even at a ~$1/contract price. Flag it as an issue
    # so readiness isn't green until the wallet can actually trade + be verified.
    MIN_TRADE_USD = 5.0
    if 0 < balance_usd < MIN_TRADE_USD:
        issues.append(
            f"balance is ${balance_usd:.2f} — add at least ${MIN_TRADE_USD:.0f} so the bot can "
            "place its minimum 5-contract order (and fully verify this wallet is set up to trade)"
        )
    if funder or sig_type == auth.SIGNATURE_TYPE_POLY_1271:
        # Deposit-wallet mode: on-chain trading approvals are provisioned by
        # Polymarket's relayer at onboarding, not by this bot. A funded deposit
        # wallet is effectively approved; the live order smoke test is the real
        # confirmation, so we don't emit a false "allowance is 0" warning here.
        approvals_ok = True if balance_usd > 0 else None
    else:
        try:
            data = await _authed_request(
                "GET", "/balance-allowance",
                params={"asset_type": "COLLATERAL", "signature_type": "0"},
            )
            allow = _extract_allowance(data)
            if allow is not None:
                approvals_ok = allow > 0
                if not approvals_ok:
                    issues.append(
                        "collateral allowance is 0 — enable trading approvals for "
                        "this wallet on polymarket.com before going live"
                    )
        except Exception as e:
            logger.debug(f"allowance check failed: {e}")

    # Live signer probe — the ONE mismatch we can't prove offline. Run it only
    # for a deposit wallet (POLY_1271) that cleared the offline checks and has
    # enough balance to back the $1.10 test order; the verdict is cached per wallet
    # config, so this places at most one tiny (instantly-killed) order per distinct
    # key/deposit pairing — not once per Test. This catches a mismatched
    # key↔deposit pairing in one click instead of after a wall of rejects.
    signer_ok: Optional[bool] = None
    if (
        sig_type == auth.SIGNATURE_TYPE_POLY_1271
        and balance_usd >= 1.5
        and not any("deposit wallet equals your signer" in i for i in issues)
    ):
        try:
            probe_sig = await verify_order_signer_live()
            if probe_sig.get("tested"):
                signer_ok = bool(probe_sig.get("ok"))
                if not signer_ok:
                    issues.append(probe_sig.get("reason") or _SIGNER_MISMATCH_HELP)
        except Exception as e:
            logger.debug(f"live signer probe failed: {e}")

    # Market-data reachability: the engines + Crypto tab read Polymarket's Gamma
    # API, which is Cloudflare-fronted and geo-restricted. A wallet can be fully
    # ready yet show an all-red board if Gamma is unreachable from the user's
    # network/region — diagnose it HERE (at Test) instead of as a cryptic timeout
    # later. SPOT prices use a different host, so they still load even when this
    # fails. One quick attempt so a healthy setup isn't slowed.
    try:
        probe = await _get(
            f"{GAMMA_BASE}/markets", params={"limit": 1},
            timeout=4.0, max_retries=1,
        )
    except Exception:
        probe = None
    if not probe:
        issues.append(
            "Polymarket market data (Gamma API) is unreachable from your network "
            "— the Crypto markets may be unavailable in your region"
        )

    return {
        "ok": len(issues) == 0, "address": address, "balanceUsd": balance_usd,
        "approvalsOk": approvals_ok, "signerOk": signer_ok, "issues": issues,
    }


async def get_positions(
    limit: int = 1000, *, settlement_status: str | None = None,
    paginate: bool = True, user: str | None = None,
) -> list[dict]:
    """Wallet positions from the Data API, mapped to the internal shape
    the reconciler reads (signed `position_fp`, `market_exposure_dollars`).

    `user` overrides whose wallet to read — used by the copy-trading engine to
    poll a FOLLOWED address. Default (None) reads our own wallet: the funder
    (deposit wallet) when one is configured, else the EOA."""
    if user:
        addr = user
    else:
        try:
            addr = auth.trading_address()
        except Exception:
            return []
    # PAGINATE to completion (when paginate=True). A single capped fetch dropped
    # the tail for wallets with many positions — which, for the copy engine, made
    # a still-held position look gone and triggered a spurious exit. `limit` is
    # the page size; we walk offsets until a short page, with a hard page cap.
    page_size = min(500, max(1, int(limit)))
    raw: list = []
    offset = 0
    for _ in range(20):   # safety cap: up to 10k positions
        data = await _get(
            f"{DATA_BASE}/positions",
            params={"user": addr, "limit": page_size, "offset": offset,
                    "sizeThreshold": 0.1},
        )
        if not isinstance(data, list):
            # _get returns None on EVERY failure (4xx, 5xx-after-retries,
            # network) — it never raises. Returning [] here made an API
            # outage indistinguishable from a genuinely flat wallet, and the
            # copy engine's exit logic treats "not in holdings" as "the whale
            # sold": a Data-API 403/429 stretch mass-dumped the whole copy
            # book at the bid. Every caller already wraps this in try/except
            # expecting failures to raise — so raise. A mid-pagination
            # failure must raise too: a silently truncated tail looks like
            # the wallet closed those positions.
            raise RuntimeError(
                f"positions fetch failed (offset {offset}) for {addr or 'own wallet'}"
            )
        if not data:
            break
        raw.extend(data)
        if not paginate or len(data) < page_size:
            break
        offset += page_size
    out: list[dict] = []
    for p in raw:
        try:
            size = _to_float(p.get("size"))
            if size == 0:
                continue
            idx = int(p.get("outcomeIndex") or 0)
            signed = size if idx == 0 else -size
            avg = _to_float(p.get("avgPrice"))
            out.append({
                "ticker": p.get("conditionId") or "",
                "event_ticker": p.get("eventSlug") or "",
                "title": p.get("title") or "",
                "category": "",
                "position_fp": signed,
                "market_exposure_dollars": abs(size * avg),
                "fees_paid_dollars": 0.0,
                "redeemable": bool(p.get("redeemable")),
                "cur_price": _to_float(p.get("curPrice")),
            })
        except Exception:
            continue
    return out


async def get_settled_positions(limit: int = 1000) -> list[dict]:
    pos = await get_positions(limit=limit)
    return [p for p in pos if p.get("redeemable")]


async def get_activity(limit: int = 500, *, user: str | None = None) -> list[dict]:
    """Recent on-chain activity (TRADE / REDEEM / SPLIT / MERGE) for a wallet,
    from the public Data API. Used to detect positions the user exited DIRECTLY
    on Polymarket — a SELL or REDEEM the order/position reconcile can't see,
    because the position simply vanishes from /positions. Each event carries
    `type`, `side` (BUY/SELL), `conditionId`, `outcomeIndex`, `usdcSize` (cash
    leg), `size` (shares), and `timestamp`. Default reads our own trading
    wallet (deposit-wallet funder when set, else the EOA)."""
    if user:
        addr = user
    else:
        try:
            addr = auth.trading_address()
        except Exception:
            return []
    out: list[dict] = []
    page = min(500, max(1, int(limit)))
    offset = 0
    for _ in range(10):   # hard page cap
        data = await _get(
            f"{DATA_BASE}/activity",
            params={"user": addr, "limit": page, "offset": offset},
        )
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < page or len(out) >= limit:
            break
        offset += page
    return out


# ───────── orders / fills ──────────────────────────────────────────────


async def get_order(order_id: str) -> dict:
    """Single open order by id → {'order': {...}}."""
    data = await _authed_request("GET", f"/data/order/{order_id}")
    return {"order": data if isinstance(data, dict) else {}}


async def get_fills_for_order(order_id: str, limit: int = 200) -> list[dict]:
    """Trades for one of our orders, mapped to {count, side, price fields}.

    The `/data/trades` endpoint is L2-authenticated, so it already returns ONLY
    the authed account's trades — as MAKER or TAKER. We must NOT add a
    `maker_address` filter: that restricts the response to trades where we were
    the maker and silently drops every TAKER fill (a marketable/crossing order,
    or the crypto15m maker→taker escalation). That dropped-taker bug made filled
    orders look unfilled, so they were written off as 'canceled' while the
    contracts sat in the wallet. We match our order_id client-side against both
    the taker_order_id and the maker_orders list instead."""
    try:
        data = await _authed_request("GET", "/data/trades")
    except PolymarketAPIError:
        return []
    rows = data.get("data") if isinstance(data, dict) else data
    out: list[dict] = []
    for t in rows or []:
        oid = str(t.get("taker_order_id") or "")
        maker_oids = [str(x) for x in (t.get("maker_orders") or [])]
        if str(order_id) not in ([oid] + maker_oids):
            continue
        price = _to_float(t.get("price"))
        out.append({
            "order_id": str(order_id),
            "action": "buy",
            "count_fp": _to_float(t.get("size")),
            "price_cents": int(round(price * 100)),  # price of the bought token
            "yes_price_dollars": price,
            "no_price_dollars": 1.0 - price,
            "side": "",  # let the caller use the position's known direction
        })
    return out


async def get_fills_since(after_ts_unix: int, limit: int = 200) -> list[dict]:
    return []


def _deposit_wallet_issue() -> str:
    """Non-empty when the configured wallet would sign orders with an EOA `maker`
    — i.e. no funder, or a funder that is just the signer's own address. Both
    make every order a guaranteed CLOB reject. Returns the user-facing fix."""
    try:
        funder = auth.get_funder()
    except auth.WalletMetaError as e:
        return f"Deposit-wallet setting unreadable ({e}). Re-save it in API Keys."
    if not funder:
        return _NO_DEPOSIT_WALLET_HELP
    try:
        signer = (auth.get_address() or "").strip()
    except Exception:
        return ""  # can't compare; let the CLOB be the judge
    if signer and funder.strip().lower() == signer.lower():
        return _NO_DEPOSIT_WALLET_HELP
    return ""


def _assert_maker_is_deposit_wallet() -> None:
    issue = _deposit_wallet_issue()
    if issue:
        raise PolymarketAPIError(400, issue)


def _snap_price_to_tick(
    price: float, tick: float, *, direction: str = "nearest"
) -> float:
    """Round a 0..1 price to the market's tick size and clamp to
    [tick, 1-tick]. Polymarket rejects orders whose price isn't an exact
    multiple of the market's tick (0.1 / 0.01 / 0.001 / 0.0001).

    `direction` controls which way a non-multiple moves. "nearest" is the
    historical behaviour. A BUY passes "down" and a SELL "up", so snapping
    can only ever improve the caller's price: the requested price is a limit
    (the autobet engine derives it as the exact point where net EV falls to
    its floor), and rounding a buy UP to the next tick silently pays above
    that cap — on a 0.1-tick market a 0.87 limit would submit at 0.90."""
    if not tick or tick <= 0:
        tick = 0.01
    dp = max(0, -int(round(math.log10(tick))))
    # Nudge before flooring/ceiling so float dust (0.85 / 0.01 == 84.999…)
    # can't drop an already-exact multiple a whole tick.
    steps = price / tick
    if direction == "down":
        snapped = math.floor(steps + 1e-9) * tick
    elif direction == "up":
        snapped = math.ceil(steps - 1e-9) * tick
    else:
        snapped = round(steps) * tick
    snapped = round(snapped, dp)
    return min(round(1 - tick, dp), max(tick, snapped))


# CLOB V2 order types (py_clob_client_v2.clob_types.OrderType). GTC rests on the
# book; FOK/FAK are marketable (match immediately against resting liquidity, the
# unfilled remainder is killed — never rests). `price` is the worst-price limit
# (slippage cap) for marketable orders.
# GTD is intentionally NOT accepted: place_limit_order never threads an
# expiration through to create_signed_order, so a GTD order would be signed with
# expiration=0 — an immediately-invalid/undefined order. Nothing in the app uses
# GTD; reject it loudly rather than silently sign a broken order. Re-add it here
# only alongside an `expiration` parameter that actually reaches the signer.
_VALID_ORDER_TYPES = ("GTC", "FOK", "FAK")


def _is_no_match_error(msg: str) -> bool:
    """True for the CLOB's 'marketable order found no liquidity' rejection — a
    FOK/FAK that crossed nothing. That's a clean zero-fill, not a real failure."""
    return "no orders found to match" in (msg or "").lower()


async def place_limit_order(
    *,
    ticker: str,
    side: str,
    action: str,
    count: int,
    price_cents: int,
    client_order_id: Optional[str] = None,
    order_type: str = "GTC",
) -> dict:
    """Sign + submit a CLOB order for the (yes/no) outcome token.

    The main engine only opens positions (`action='buy'`); the crypto
    executor also sells to stop out (`action='sell'`). A "no" position is a
    trade of the NO token — buying NO to open, selling NO to close.

    `order_type` (CLOB V2): "GTC" (default) rests on the book; "FAK"/"FOK" are
    marketable — they cross and fill immediately against resting liquidity at a
    price at-or-better than `price_cents` (the slippage cap) and KILL the
    unfilled remainder instead of resting it. Marketable orders carry no
    `postOnly` (the CLOB rejects post-only FOK/FAK). The signed order struct is
    identical across types — only the submission `orderType` differs.

    The requested `price_cents` is snapped to the market's tick size and the
    `count` is raised to the market's minimum order size (for buys) so the
    CLOB doesn't reject the order with INVALID_ORDER_MIN_TICK_SIZE /
    INVALID_ORDER_MIN_SIZE."""
    side = side.lower()
    action = (action or "buy").lower()
    order_type = (order_type or "GTC").upper()
    if order_type not in _VALID_ORDER_TYPES:
        raise ValueError(f"order_type must be one of {_VALID_ORDER_TYPES}, got {order_type}")
    if side not in ("yes", "no"):
        raise ValueError(f"side must be yes|no, got {side}")
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be buy|sell, got {action}")
    if not (1 <= price_cents <= 99):
        raise ValueError(f"price_cents must be 1..99, got {price_cents}")
    if count <= 0:
        raise ValueError(f"count must be positive, got {count}")

    # The signed order's `maker` is the funder, falling back to the signer EOA
    # when no deposit wallet is configured. Polymarket refuses to match an EOA
    # maker ("maker address not allowed, please use the deposit wallet flow"), so
    # such an order is a guaranteed reject. Catch it here — the one path every
    # engine's orders go through — instead of letting each engine burn its trade
    # window and write an error position row per attempt.
    _assert_maker_is_deposit_wallet()

    meta = await get_market_meta(ticker)
    if not meta:
        raise PolymarketAPIError(404, f"no market meta for {ticker}")
    token_id = meta["yes_token"] if side == "yes" else meta["no_token"]
    if not token_id:
        raise PolymarketAPIError(404, f"no {side} token for {ticker}")

    tick = float(meta.get("tick_size") or 0.01)
    min_size = float(meta.get("min_size") or 0.0)
    # Snap AWAY from crossing: a buy's price is a slippage cap, so it rounds
    # down; a sell's is a floor, so it rounds up. Never past the caller's limit.
    price = _snap_price_to_tick(
        price_cents / 100.0, tick, direction="down" if action == "buy" else "up",
    )
    size = int(count)
    # Respect the user's size: NEVER silently buy more than requested. If the
    # request is under the market's enforced minimum, refuse rather than
    # inflate the position (Polymarket rejects sub-minimum orders anyway, and
    # inflating would blow past the user's risk limits). The caller surfaces
    # this so the user can raise their size to trade the market.
    if action == "buy" and min_size > 0 and size < min_size:
        raise PolymarketAPIError(
            400,
            f"requested size {size} is below this market's minimum of "
            f"{int(math.ceil(min_size))} shares — raise your order size to trade "
            f"here (not auto-inflating, to protect your risk limits)",
        )

    order = auth.create_signed_order(
        token_id=token_id,
        side="BUY" if action == "buy" else "SELL",
        price=price,
        size=size,
        neg_risk=bool(meta.get("neg_risk")),
    )
    # V2 /order body requires `owner` = the API key the order is placed under.
    await ensure_api_creds()
    creds = auth.get_api_creds() or {}
    body = {
        "order": order,
        "owner": creds.get("apiKey", ""),
        "orderType": order_type,
        "deferExec": False,
        # postOnly must be false for marketable (FOK/FAK) orders — the CLOB
        # rejects post-only marketable orders. We never rest as post-only.
        "postOnly": False,
    }
    try:
        # idempotent=False: never blindly re-fire a placed order on a timeout/5xx
        # (double-fill guard). The order poller reconciles an ambiguous result.
        resp = await _authed_request("POST", "/order", json_body=body, idempotent=False)
    except PolymarketAPIError as e:
        # A marketable (FOK/FAK) order with NO crossable liquidity is rejected by
        # the CLOB ("no orders found to match with FAK order"). That's the
        # fill-and-kill contract doing its job — a clean ZERO fill, NOT an error.
        # Report it as unmatched so the caller skips cleanly instead of logging a
        # failure (the spurious 'error' rows on thin/late books).
        if order_type in ("FOK", "FAK") and _is_no_match_error(str(e)):
            return {"order": {"order_id": "", "status": "unmatched"}, "raw": {"errorMsg": str(e)}}
        if "invalid order payload" in str(e).lower():
            # The CLOB's reject is generic — dump the exact (non-secret) order
            # params so a user log can show WHAT it considered malformed.
            logger.warning(
                f"[order-payload] rejected: {ticker} {side}/{action} "
                f"type={order_type} price={price} size={size} tick={tick} "
                f"neg_risk={bool(meta.get('neg_risk'))} token={token_id} "
                f"sig_type={auth.get_signature_type()}"
            )
        raise
    oid = ""
    status = ""
    if isinstance(resp, dict):
        oid = resp.get("orderID") or resp.get("orderId") or ""
        status = resp.get("status") or ""
        # The CLOB returns HTTP 200 with {"success": false, "errorMsg": ...} on a
        # business rejection (e.g. min-tick, not-enough-balance). _authed_request
        # only raises on HTTP >= 400, so without this a rejected order was booked
        # as a live position and the signal marked "traded". Surface it as an
        # error so the caller records a failure, not a phantom fill.
        err = resp.get("errorMsg") or resp.get("error") or ""
        if resp.get("success") is False or err:
            # Same no-liquidity carve-out if it comes back 200 + success:false.
            if order_type in ("FOK", "FAK") and _is_no_match_error(err):
                return {"order": {"order_id": "", "status": "unmatched"}, "raw": resp}
            raise PolymarketAPIError(400, err or f"order rejected: {str(resp)[:160]}")
    # A marketable (FOK/FAK) order NEVER rests: an "unmatched" result means it was
    # killed with zero fill. Report no order_id in that case so the caller takes
    # its clean no-fill path instead of polling a non-existent order — a terminal
    # marketable order 404s on the open-order endpoint, which the crypto poll
    # treats as "fully matched" and would otherwise book as a phantom full fill.
    # A matched / partially-matched FAK keeps its id (the fill is real).
    if order_type in ("FOK", "FAK") and str(status).lower() in (
        "unmatched", "killed", "canceled", "cancelled"
    ):
        oid = ""
    return {"order": {"order_id": oid, "status": status}, "raw": resp}


async def cancel_order(order_id: str) -> dict:
    return await _authed_request("DELETE", "/order", json_body={"orderID": order_id})

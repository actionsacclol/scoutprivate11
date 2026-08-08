"""Sharp-money intelligence — who's positioned where on tonight's slate.

Free, unauthenticated Polymarket Data API (data-api.polymarket.com — public
but undocumented, so every call is best-effort and a dead endpoint degrades
to an empty panel, never an error):

  /holders?market=<conditionId>       top holders per outcome token
  /trades?market=<cid>&filterType=CASH&filterAmount=N   whale taker prints
  /v1/leaderboard?category=sports     top sports wallets by realized P&L

Per slate game the report aggregates: 24h whale BUY flow per side, the top
holder per side, and how many top-100 sports-leaderboard wallets hold each
side. DISPLAY-ONLY intelligence: the profit-concentration research says top
wallets earn on execution as much as picks, so this is context next to the
model's number — never an autobet input until walk-forward validated.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from kryptscout.core.schemas import Game, Sport
from kryptscout.odds import polymarket
from kryptscout.sports import base as engines

logger = logging.getLogger("kryptscout")

DATA_API = "https://data-api.polymarket.com"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
WHALE_MIN_CASH = 500.0  # $ per print to count as whale flow
_LEADERBOARD_TTL = 6 * 3600
_CONCURRENCY = 4

_leaderboard_cache: tuple[float, frozenset[str], dict[str, str]] = (0.0, frozenset(), {})


async def _get(client: httpx.AsyncClient, url: str, params: dict):
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception:  # noqa: BLE001 — undocumented API; degrade quietly
        return None


async def _top_wallets(client: httpx.AsyncClient) -> tuple[frozenset[str], dict[str, str]]:
    """Top-100 sports wallets by P&L, cached ~6h: (addresses, address->name)."""
    global _leaderboard_cache
    at, wallets, names = _leaderboard_cache
    if wallets and time.time() - at < _LEADERBOARD_TTL:
        return wallets, names
    rows = await _get(client, f"{DATA_API}/v1/leaderboard", {
        "category": "sports", "timePeriod": "all", "orderBy": "pnl", "limit": 100,
    })
    if isinstance(rows, list) and rows:
        wallets = frozenset(str(r.get("proxyWallet", "")).lower() for r in rows)
        names = {str(r.get("proxyWallet", "")).lower(): str(r.get("userName") or "")
                 for r in rows}
        _leaderboard_cache = (time.time(), wallets, names)
    return _leaderboard_cache[1], _leaderboard_cache[2]


def _side_of(outcome: str, game: Game) -> str | None:
    """Map an outcome display name to 'home'/'away' by (nick)name match."""
    def norm(s: str) -> str:
        return "".join(c for c in s.lower() if c.isalnum())
    o = norm(outcome)
    for side, team in (("home", game.home), ("away", game.away)):
        t = norm(team)
        if o == t or t.endswith(o) or o.endswith(t):
            return side
        if outcome.split()[-1].lower() == team.split()[-1].lower():
            return side
    return None


async def _game_report(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                       sport: Sport, game: Game,
                       top: frozenset[str], names: dict[str, str]) -> dict | None:
    async with sem:
        ref = await polymarket.resolve_market(sport, game, "home")
        if ref is None:
            return None
        cid = ref.condition_id
        gm = await _get(client, GAMMA_MARKETS, {"condition_ids": cid})
        market = gm[0] if isinstance(gm, list) and gm else None
        if not market:
            return None
        import json as _json
        outcomes = market.get("outcomes")
        tokens = market.get("clobTokenIds")
        outcomes = _json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        tokens = _json.loads(tokens) if isinstance(tokens, str) else tokens
        if not (isinstance(outcomes, list) and isinstance(tokens, list)
                and len(outcomes) == 2 and len(tokens) == 2):
            return None
        token_side = {}
        for outcome, token in zip(outcomes, tokens):
            side = _side_of(str(outcome), game)
            if side:
                token_side[str(token)] = side
        if len(token_side) != 2:
            return None

        holders_raw, trades = await asyncio.gather(
            _get(client, f"{DATA_API}/holders", {"market": cid, "limit": 10}),
            _get(client, f"{DATA_API}/trades", {
                "market": cid, "filterType": "CASH",
                "filterAmount": WHALE_MIN_CASH, "limit": 200,
            }),
        )

        flow = {"home": 0.0, "away": 0.0}
        whales = {"home": 0, "away": 0}
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()
        for t in trades or []:
            side = token_side.get(str(t.get("asset")))
            if side is None or float(t.get("timestamp") or 0) < cutoff:
                continue
            usd = float(t.get("size") or 0) * float(t.get("price") or 0)
            if str(t.get("side")).upper() == "BUY":
                flow[side] += usd
                whales[side] += 1
            else:
                flow[side] -= usd

        sharp = {"home": [], "away": []}
        top_holder = {"home": None, "away": None}
        for entry in holders_raw or []:
            token = str(entry.get("token", ""))
            side = token_side.get(token)
            if side is None:
                continue
            for h in entry.get("holders") or []:
                wallet = str(h.get("proxyWallet", "")).lower()
                amount = float(h.get("amount") or 0)
                if top_holder[side] is None or amount > top_holder[side]["shares"]:
                    top_holder[side] = {
                        "name": h.get("name") or h.get("pseudonym") or wallet[:8],
                        "shares": amount,
                    }
                if wallet in top:
                    sharp[side].append(names.get(wallet) or wallet[:8])

        net = flow["home"] - flow["away"]
        lean = "home" if net > 0 else ("away" if net < 0 else None)
        if lean is None and (sharp["home"] or sharp["away"]):
            lean = "home" if len(sharp["home"]) > len(sharp["away"]) else "away"
        return {
            "game_id": game.id,
            "home": game.home,
            "away": game.away,
            "kickoff": game.start_time.isoformat(),
            "slug": market.get("slug"),
            "flow_home_usd": round(flow["home"], 0),
            "flow_away_usd": round(flow["away"], 0),
            "whale_trades": whales["home"] + whales["away"],
            "lean": lean,
            "sharp_home": sharp["home"][:5],
            "sharp_away": sharp["away"][:5],
            "top_holder_home": top_holder["home"],
            "top_holder_away": top_holder["away"],
        }


async def slate_report(sport: Sport, on: date | None = None) -> dict:
    """Sharp-money rows for one sport's slate. Best-effort per game."""
    engine = engines.get_engine(sport)
    games = await engine.upcoming_games(on)
    if not games:
        return {"sport": sport.value, "games": []}
    sem = asyncio.Semaphore(_CONCURRENCY)
    async with httpx.AsyncClient(timeout=30) as client:
        top, names = await _top_wallets(client)
        rows = await asyncio.gather(*(
            _game_report(client, sem, sport, g, top, names) for g in games
        ))
    return {
        "sport": sport.value,
        "games": [r for r in rows if r is not None],
        "leaderboard_loaded": bool(top),
    }

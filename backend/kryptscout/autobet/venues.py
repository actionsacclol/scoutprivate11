"""The places auto-bet can buy a game winner, behind one interface.

Three exchanges, three different mechanics — a crypto wallet and outcome
tokens (polymarket.com), signed REST with per-shard collateral (Kalshi), an
API key and one two-sided instrument (polymarket.us) — and one thing the
engine wants from each: "what does this side of this game cost right now,
after your fee, and can I buy N shares at or under a cap, immediately".

Every venue answers in the same shapes:

    resolve(sport, game, side)   -> VenueMarket | None
    meta(market)                 -> {"fee_schedule": {...}, "min_size": float} | None
    ask_depth(market, cap_cents) -> {"best_ask_cents", "depth_shares", "levels"} | None
    buy(market, shares, cents)   -> {"order_id", "filled", "avg_prob", "note", "pending"}
    fill(order_id, market_id)    -> (shares, avg_prob) | (0, 0) = ended unfilled | None
    balance_usd()                -> float

Orders are always immediate: they take what the book offers at or under the
cap and the rest is killed. Nothing rests, so nothing needs cancelling and no
position is ever larger than the engine decided on.

`buy` returning an empty order_id is a clean no-trade. `pending` means the
exchange accepted the order and has not yet said how it ended: the engine
holds the game and confirms the fill before it counts the bet. It raises
OrderOutcomeUnknown when the exchange may have the order but did not say —
the engine then holds the game rather than risk buying it twice.

Reading a book needs no account on any of the three, so paper mode works with
nothing connected.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from kryptscout.autobet import kalshi_trade, pmus_trade
from kryptscout.autobet import polymarket_api as pm_api
from kryptscout.autobet import polymarket_auth as pm_auth
from kryptscout.core.schemas import Game, Sport
from kryptscout.odds import kalshi as kalshi_odds
from kryptscout.odds import polymarket as pm_odds

logger = logging.getLogger("kryptscout")


class OrderOutcomeUnknown(Exception):
    """An order was sent and no definite answer came back."""


@dataclass
class VenueMarket:
    """The tradable market backing one side of one game on one venue."""

    venue: str
    condition_id: str  # Polymarket conditionId / Kalshi market ticker / Polymarket US slug
    clob_side: str  # "yes" | "no" — which side of that market is our team
    prob: Optional[float]  # what a buyer pays at the touch, at discovery time
    slug: Optional[str] = None


class Venue:
    key = ""
    label = ""
    # The book key this venue's quote carries on a board (core.schemas
    # MoneylineOdds.book), or None when the board does not quote it.
    book: Optional[str] = None
    # Taker fee per share = rate x p x (1 - p) on all three. Used only when the
    # market's own schedule cannot be read: an unknown fee is never a zero fee.
    fallback_fee_rate = 0.05
    # Live orders without a readable book: polymarket.com goes anyway (the cap
    # is the safety, and its record was built that way); the two regulated
    # venues need a price collar, and the book read IS the collar.
    live_needs_book = True
    account_noun = "API key"

    def connected(self) -> bool:
        raise NotImplementedError

    def credentials_status(self) -> dict:
        raise NotImplementedError

    async def resolve(self, sport: Sport, game: Game, side: str) -> Optional[VenueMarket]:
        raise NotImplementedError

    async def meta(self, market: VenueMarket) -> Optional[dict]:
        raise NotImplementedError

    async def ask_depth(self, market: VenueMarket, max_price_cents: int) -> Optional[dict]:
        raise NotImplementedError

    async def balance_usd(self) -> float:
        raise NotImplementedError

    async def buy(self, market: VenueMarket, shares: int, price_cents: int) -> dict:
        raise NotImplementedError

    async def fill(self, order_id: str, market_id: str = "") -> Optional[tuple[float, float]]:
        raise NotImplementedError

    async def check(self) -> dict:
        raise NotImplementedError

    def fee_usd(self, shares: float, prob: float, rate: float) -> float:
        return shares * rate * prob * (1.0 - prob)

    def error_text(self, exc: Exception) -> str:
        return str(exc)[:300]


# ---------------------------------------------------------------- polymarket.com


class PolymarketVenue(Venue):
    key = "polymarket"
    label = "Polymarket"
    book = "polymarket"
    fallback_fee_rate = 0.05  # sports_fees_v2; the market's Gamma feeSchedule wins
    live_needs_book = False
    account_noun = "wallet"

    def connected(self) -> bool:
        return pm_auth.credentials_present()

    def credentials_status(self) -> dict:
        try:
            status = pm_auth.credentials_status()
        except Exception as exc:  # noqa: BLE001 — status must never 500
            return {"connected": False, "error": str(exc)}
        return {
            "connected": bool(status.get("hasWalletKey")),
            "keyPreview": status.get("addressPreview") or "",
            "detail": status,
        }

    async def resolve(self, sport: Sport, game: Game, side: str) -> Optional[VenueMarket]:
        ref = await pm_odds.resolve_market(sport, game, side)
        if ref is None:
            return None
        return VenueMarket(self.key, ref.condition_id, ref.clob_side,
                           getattr(ref, "prob", None), ref.slug)

    async def meta(self, market: VenueMarket) -> Optional[dict]:
        return await pm_api.get_market_meta(market.condition_id)

    async def ask_depth(self, market: VenueMarket, max_price_cents: int) -> Optional[dict]:
        return await pm_api.ask_depth(market.condition_id, market.clob_side, max_price_cents)

    async def balance_usd(self) -> float:
        balance = await pm_api.get_balance()
        return float(balance.get("balance") or 0) / 100.0

    async def buy(self, market: VenueMarket, shares: int, price_cents: int) -> dict:
        result = await pm_api.place_limit_order(
            ticker=market.condition_id, side=market.clob_side, action="buy",
            count=shares, price_cents=price_cents, order_type="FAK",
        )
        order_id = ((result or {}).get("order") or {}).get("order_id") or ""
        return {"order_id": order_id, "filled": None, "avg_prob": None, "note": None}

    async def fill(self, order_id: str, market_id: str = "") -> Optional[tuple[float, float]]:
        fills = await pm_api.get_fills_for_order(order_id)
        total = sum(float(f.get("count_fp") or 0.0) for f in fills)
        if total <= 0:
            return None
        avg = sum(
            float(f.get("count_fp") or 0.0) * float(f.get("yes_price_dollars") or 0.0)
            for f in fills
        ) / total
        return round(total, 4), round(avg, 4)

    async def check(self) -> dict:
        return await pm_api.check_trading_ready()


# ------------------------------------------------------------------------ kalshi


class KalshiVenue(Venue):
    key = "kalshi"
    label = "Kalshi"
    book = "kalshi"
    fallback_fee_rate = kalshi_trade.BASE_TAKER_RATE
    # Set by the engine from settings before a sweep: may the app move cash
    # between the user's own Kalshi exchange shards to fund an order?
    auto_move_funds = True

    def connected(self) -> bool:
        return kalshi_trade.credentials_present()

    def credentials_status(self) -> dict:
        return kalshi_trade.credentials_status()

    async def resolve(self, sport: Sport, game: Game, side: str) -> Optional[VenueMarket]:
        if sport not in kalshi_odds.SERIES_TICKERS or side not in ("home", "away"):
            return None
        try:
            events = await kalshi_odds._slate(sport)
        except Exception as exc:  # noqa: BLE001 — discovery is best-effort
            logger.warning("kalshi: resolve fetch failed for %s: %s", sport.value, exc)
            return None
        matched = kalshi_odds._match_event(game, events)
        if matched is None:
            return None
        event, sides = matched
        ticker = kalshi_odds._side_tickers(event, game).get(side)
        if not ticker:
            return None
        # Each team has its own market and its YES is that team winning, so a
        # Kalshi bet is always a YES buy.
        return VenueMarket(self.key, ticker, "yes", sides.get(side), event.event_ticker)

    async def meta(self, market: VenueMarket) -> Optional[dict]:
        rate = await kalshi_trade.fee_rate(market.condition_id)
        if rate is None:
            return {"fee_schedule": None, "min_size": 1.0}
        schedule = {"enabled": rate > 0, "rate": rate, "exponent": 1.0}
        return {"fee_schedule": schedule, "min_size": 1.0}

    async def ask_depth(self, market: VenueMarket, max_price_cents: int) -> Optional[dict]:
        return await kalshi_trade.ask_depth(market.condition_id, max_price_cents)

    async def balance_usd(self) -> float:
        return float((await kalshi_trade.get_balance())["usd"])

    async def buy(self, market: VenueMarket, shares: int, price_cents: int) -> dict:
        note = await kalshi_trade.ensure_collateral(
            market.condition_id, shares, price_cents, allow_move=self.auto_move_funds)
        try:
            result = await kalshi_trade.buy_yes_ioc(market.condition_id, shares, price_cents)
        except kalshi_trade.OrderOutcomeUnknown as exc:
            raise OrderOutcomeUnknown(str(exc)) from exc
        if result["filled"] is not None and result["filled"] <= 0:
            return {"order_id": "", "filled": 0.0, "avg_prob": None, "note": note}
        return {"order_id": result["order_id"], "filled": result["filled"],
                "avg_prob": result["avg_prob"], "note": note,
                "pending": result["filled"] is None}

    async def fill(self, order_id: str, market_id: str = "") -> Optional[tuple[float, float]]:
        return await kalshi_trade.order_fill(order_id)

    async def check(self) -> dict:
        return await kalshi_trade.check()

    def fee_usd(self, shares: float, prob: float, rate: float) -> float:
        return kalshi_trade.fee_usd(shares, prob, rate)

    def error_text(self, exc: Exception) -> str:
        return kalshi_trade.error_text(exc)


# ----------------------------------------------------------------- polymarket.us


class PolymarketUSVenue(Venue):
    key = "polymarket_us"
    label = "Polymarket US"
    book = None  # not an odds source on the board; priced from its own book here
    fallback_fee_rate = 0.095  # what its game markets stamp (feeCoefficient)

    def connected(self) -> bool:
        return pmus_trade.credentials_present()

    def credentials_status(self) -> dict:
        return pmus_trade.credentials_status()

    async def resolve(self, sport: Sport, game: Game, side: str) -> Optional[VenueMarket]:
        found = await pmus_trade.resolve(sport, game, side)
        if found is None:
            return None
        return VenueMarket(self.key, found["slug"], found["clob_side"], found["prob"], found["slug"])

    async def meta(self, market: VenueMarket) -> Optional[dict]:
        return await pmus_trade.market_meta(market.condition_id)

    async def ask_depth(self, market: VenueMarket, max_price_cents: int) -> Optional[dict]:
        return await pmus_trade.ask_depth(market.condition_id, market.clob_side, max_price_cents)

    async def balance_usd(self) -> float:
        return await pmus_trade.get_balance_usd()

    async def buy(self, market: VenueMarket, shares: int, price_cents: int) -> dict:
        try:
            result = await pmus_trade.buy_ioc(
                market.condition_id, market.clob_side, shares, price_cents)
        except pmus_trade.OrderOutcomeUnknown as exc:
            raise OrderOutcomeUnknown(str(exc)) from exc
        return {**result, "note": None,
                "pending": bool(result["order_id"]) and result["filled"] is None}

    async def fill(self, order_id: str, market_id: str = "") -> Optional[tuple[float, float]]:
        return await pmus_trade.fills_for_order(order_id)

    async def check(self) -> dict:
        return await pmus_trade.check()

    def error_text(self, exc: Exception) -> str:
        return pmus_trade.error_text(exc)


# ---------------------------------------------------------------------- registry

VENUES: dict[str, Venue] = {
    venue.key: venue for venue in (PolymarketVenue(), KalshiVenue(), PolymarketUSVenue())
}
VENUE_KEYS: tuple[str, ...] = tuple(VENUES)
# Rows written before the venue column existed are polymarket.com bets.
LEGACY_VENUE = "polymarket"


def get(key: Optional[str]) -> Venue:
    return VENUES.get(key or LEGACY_VENUE) or VENUES[LEGACY_VENUE]


def status() -> list[dict[str, Any]]:
    """Per-venue connection state for the UI. Local only — no network."""
    out = []
    for venue in VENUES.values():
        try:
            creds = venue.credentials_status()
        except Exception as exc:  # noqa: BLE001 — status must never 500
            creds = {"connected": False, "error": str(exc)}
        out.append({
            "key": venue.key, "label": venue.label,
            "connected": bool(creds.get("connected")),
            "keyPreview": creds.get("keyPreview") or "",
            "error": creds.get("error"),
            "accountNoun": venue.account_noun,
            "onBoard": venue.book is not None,
        })
    return out

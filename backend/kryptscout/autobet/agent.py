"""Bets asked for by an AI agent (the MCP tools get_bet_prices / place_bet).

The rule engine (engine.py) decides from prices alone and needs no AI. This is
the second driver: an agent the user connected reads the boards, forms its own
view, and asks for ONE bet at a time. It goes through the same venue layer and
lands in the same ledger, so the two drivers cannot bet the same game twice
and the same stops halt both.

What the user controls (AI AGENTS page; all three live in autobet.json):

    agent_mode               off (default) | paper | live
    agent_max_usd            the most one agent bet may spend
    agent_max_bets_per_day   agent bets per UTC day

What an agent cannot do, whatever it sends:

* bet while the mode is off, or live while it is paper;
* spend more than the cap, or more bets than the day allows — a request over
  the cap is REFUSED, never trimmed, so splitting an order gains nothing;
* bet a game that already carries a bet (its own, the rule engine's, or an
  order that was sent and never answered);
* pay more than the price its own stated probability supports after the
  exchange's fee, more than its own limit, or more than two cents through the
  best offer (a thin book cannot be walked);
* send anything without a readable order book, a connected account and an
  active license (live), or once a stop has been hit.

Every refusal says why in words the agent can relay. Nothing here retries.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from kryptscout.autobet import engine
from kryptscout.autobet import venues as venues_mod
from kryptscout.core.oddsmath import prob_to_american
from kryptscout.core.schemas import Game, Sport
from kryptscout.store import db, tracker
from kryptscout.store.picks import NO_VALUE_SPORTS

logger = logging.getLogger("kryptscout")

TIER = "AGENT"
MODES = ("off", "paper", "live")
# How far through the best offer an agent's order may reach.
SLIPPAGE_CENTS = 2
# The agent's own probability may support a high price; nothing is bought
# above this regardless.
MAX_PRICE_CENTS = 95
COMMITTED = ("placed", "dry", "unknown")


class AgentRefused(Exception):
    """A bet the rules do not allow. The message is for the agent to relay."""


def limits(settings: Optional[dict] = None, conn=None) -> dict:
    """What an agent may do right now, for get_status / get_bet_prices."""
    settings = settings or engine.load_settings()
    own = conn is None
    conn = conn or db.connect()
    try:
        used = _bets_today(conn)
    finally:
        if own:
            conn.close()
    return {
        "mode": settings["agent_mode"],
        "max_usd_per_bet": settings["agent_max_usd"],
        "max_bets_per_day": settings["agent_max_bets_per_day"],
        "bets_left_today": max(0, settings["agent_max_bets_per_day"] - used),
        "venues": [
            {"venue": v.key, "name": v.label, "connected": v.connected()}
            for v in venues_mod.VENUES.values()
        ],
    }


def _bets_today(conn) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    rows = db.rows(
        conn,
        f"""
        SELECT COUNT(*) AS n FROM auto_bets
        WHERE tier = ? AND date(created_at) = ?
          AND status IN ({",".join("?" * len(COMMITTED))})
        """,
        (TIER, today, *COMMITTED),
    )
    return int(rows[0]["n"]) if rows else 0


def _game(conn, game_id: str) -> tuple[dict, Game]:
    found = db.rows(
        conn,
        "SELECT game_id, sport, league, start_time, home, away, completed "
        "FROM games WHERE game_id = ?",
        (str(game_id),),
    )
    if not found:
        raise AgentRefused(
            f"No game {game_id!r} is on a board the app has served. Call get_board first "
            "and use a game_id from it."
        )
    row = found[0]
    start = datetime.fromisoformat(row["start_time"])
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    game = Game(
        id=row["game_id"], sport=Sport(row["sport"]), league=row["league"] or "",
        start_time=start, home=row["home"], away=row["away"],
    )
    return row, game


def _crossable(book: dict, limit_cents: int) -> list[list[float]]:
    cap = limit_cents / 100.0 + 1e-9
    out = []
    for level in book.get("levels") or []:
        try:
            px, size = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if px <= cap and size > 0:
            out.append([px, size])
    return sorted(out, key=lambda level: level[0])


async def _fee_rate(venue, market) -> tuple[float, Optional[dict]]:
    try:
        meta = await venue.meta(market)
    except Exception:  # noqa: BLE001 — an unreadable fee is the standing rate, never zero
        meta = None
    return engine._fee_rate_from_meta(meta, venue.fallback_fee_rate), meta


async def prices(game_id: str) -> dict:
    """What each exchange would charge for each side of one game right now,
    read off its live order book, next to the sharp fair when there is one."""
    conn = db.connect()
    try:
        row, game = _game(conn, game_id)
        settings = engine.load_settings()
        allowed = limits(settings, conn)
    finally:
        conn.close()
    card = None
    try:
        cards = await engine._load_cards([game.sport])
        card = next((c for c in cards if c.game.id == game.id), None)
    except Exception as exc:  # noqa: BLE001 — the fair is context, not a requirement
        logger.debug("agent prices: board unavailable for %s: %s", game.sport.value, exc)
    out: list[dict] = []
    for venue in venues_mod.VENUES.values():
        reference = engine._reference(card, venue) if card is not None else None
        for side in ("home", "away"):
            try:
                market = await venue.resolve(game.sport, game, side)
            except Exception as exc:  # noqa: BLE001
                logger.debug("agent prices: %s resolve failed: %s", venue.key, exc)
                market = None
            if market is None:
                continue
            fee_rate, _meta = await _fee_rate(venue, market)
            try:
                book = await venue.ask_depth(market, 99)
            except Exception:  # noqa: BLE001
                book = None
            ask = book["best_ask_cents"] / 100.0 if book else None
            entry: dict[str, Any] = {
                "venue": venue.key, "side": side,
                "team": game.home if side == "home" else game.away,
                "best_ask_cents": book["best_ask_cents"] if book else None,
                "shares_at_best_ask": (
                    round(float(book["levels"][0][1]), 2) if book and book.get("levels") else None
                ),
                "fee_rate": round(fee_rate, 4),
                "connected": venue.connected(),
            }
            if ask and 0.0 < ask < 1.0:
                # The lowest win probability at which this price breaks even
                # after the fee: ask × (1 + fee_rate × (1 − ask)).
                entry["breakeven_prob"] = round(ask * (1.0 + fee_rate * (1.0 - ask)), 4)
                if reference is not None:
                    ref_book, probs, thin = reference
                    fair = float(probs[side])
                    entry["fair_prob"] = round(fair, 4)
                    entry["fair_from"] = ref_book
                    entry["fair_is_thin"] = bool(thin)
                    entry["net_ev_vs_fair"] = round(engine._net_ev(fair, ask, fee_rate), 4)
            out.append(entry)
    started = game.start_time <= datetime.now(timezone.utc)
    return {
        "game_id": game.id, "sport": game.sport.value, "home": game.home, "away": game.away,
        "starts_at": game.start_time.isoformat(), "started": started or bool(row["completed"]),
        "prices": out,
        "agent_betting": allowed,
        "note": (
            "best_ask_cents is what a buyer pays now. breakeven_prob is how often the side "
            "must win for that price to break even after the exchange fee. net_ev_vs_fair "
            "compares the price with ANOTHER exchange's probability; it is usually negative, "
            "and a positive number has not been a profit in this app's own tracking."
        ),
    }


async def place(*, game_id: str, side: str, venue: str, usd: float, max_price_cents: int,
                win_prob: float, rationale: str = "", client: str = "agent",
                paper: bool = False) -> dict:
    """One agent bet. Raises AgentRefused with a reason; otherwise returns what
    happened: {"status": paper | placed | no_fill | unknown, ...}."""
    settings = engine.load_settings()
    mode = settings["agent_mode"]
    if mode not in ("paper", "live"):
        raise AgentRefused(
            "Agent betting is switched off. The user turns it on in Krypt Scout on the "
            "AI AGENTS page (paper first)."
        )
    if side not in ("home", "away"):
        raise AgentRefused("side must be home or away.")
    target = venues_mod.VENUES.get(venue)
    if target is None:
        raise AgentRefused(f"venue must be one of: {', '.join(venues_mod.VENUE_KEYS)}.")
    try:
        usd, win_prob, max_price_cents = float(usd), float(win_prob), int(max_price_cents)
    except (TypeError, ValueError):
        raise AgentRefused("usd, win_prob and max_price_cents must be numbers.") from None
    if not (0.01 <= win_prob <= 0.99):
        raise AgentRefused("win_prob is your own probability that this side wins, 0.01 to 0.99.")
    if not (1 <= max_price_cents <= 99):
        raise AgentRefused("max_price_cents is the most you will pay per share, 1 to 99.")
    if usd < 1.0:
        raise AgentRefused("The smallest bet is $1.")
    if usd > settings["agent_max_usd"] + 1e-9:
        raise AgentRefused(
            f"${usd:.2f} is over the user's cap of ${settings['agent_max_usd']:.2f} per agent "
            "bet. Ask for less; do not split the bet to get under the cap."
        )
    live = mode == "live" and not paper
    async with engine._sweep_lock:
        return await _place_locked(
            settings=settings, game_id=game_id, side=side, venue=target, usd=usd,
            max_price_cents=max_price_cents, win_prob=win_prob, client=client, live=live,
        )


async def _place_locked(*, settings: dict, game_id: str, side: str, venue, usd: float,
                        max_price_cents: int, win_prob: float, client: str, live: bool) -> dict:
    now = datetime.now(timezone.utc)
    conn = db.connect()
    try:
        game_row, game = _game(conn, game_id)
        if game.sport.value in NO_VALUE_SPORTS:
            raise AgentRefused(f"{game.sport.value} is not available for auto-betting.")
        if game_row["completed"] or game.start_time <= now + timedelta(
                minutes=settings["lead_minutes"]):
            raise AgentRefused(
                "That game has started or is about to: bets close "
                f"{settings['lead_minutes']} minutes before the start."
            )
        if _bets_today(conn) >= settings["agent_max_bets_per_day"]:
            raise AgentRefused(
                f"The user allows {settings['agent_max_bets_per_day']} agent bets a day and "
                "they are used."
            )
        stop = engine._stop_reason(settings, engine.realized_pnl(conn))
        if stop is not None:
            raise AgentRefused(f"Betting is halted by the user's {stop.replace('_', ' ')}.")
        _attempts, committed = engine._attempts(conn, game.id, side)
        if committed:
            raise AgentRefused(
                "This game already has a bet (one bet per game, whoever placed it)."
            )
        if live:
            if not venue.connected():
                raise AgentRefused(
                    f"No {venue.label} account is connected in Krypt Scout. "
                    "Pass paper=true to record it as a paper bet instead."
                )
            if not engine._live_license_ok():
                raise AgentRefused("Live betting needs an active Krypt Scout license.")

        try:
            market = await venue.resolve(game.sport, game, side)
        except Exception as exc:  # noqa: BLE001
            logger.debug("agent bet: %s resolve failed: %s", venue.key, exc)
            market = None
        if market is None:
            raise AgentRefused(f"{venue.label} has no market for that game right now.")
        fee_rate, meta = await _fee_rate(venue, market)
        own_cap = engine._limit_cents(win_prob, 0.0, MAX_PRICE_CENTS / 100.0, fee_rate=fee_rate)
        if own_cap is None:
            raise AgentRefused("That probability supports no price worth paying.")
        try:
            book = await venue.ask_depth(market, 99)
        except Exception:  # noqa: BLE001
            book = None
        if book is None:
            raise AgentRefused(
                f"{venue.label}'s order book could not be read, so nothing was sent."
            )
        best = int(book["best_ask_cents"])
        cents = min(max_price_cents, own_cap, best + SLIPPAGE_CENTS, MAX_PRICE_CENTS)
        if cents < best:
            raise AgentRefused(
                f"No trade: the best offer is {best}¢. Your limit is {max_price_cents}¢ and, "
                f"after {venue.label}'s {fee_rate * 100:.1f}% fee, a {win_prob * 100:.1f}% "
                f"probability supports at most {own_cap}¢."
            )
        levels = _crossable(book, cents)
        depth = sum(size for _, size in levels)
        limit_prob = cents / 100.0
        shares = min(int(usd / limit_prob), int(depth))
        min_size = float((meta or {}).get("min_size") or 0.0)
        if shares < 1 or (min_size > 0 and shares < min_size):
            raise AgentRefused("Too small to trade at that price (under one share or the "
                               "exchange's minimum).")
        cost = round(shares * limit_prob, 2)
        row = {
            "game_id": game.id, "sport": game.sport.value, "side": side,
            "team": game.home if side == "home" else game.away, "tier": TIER,
            "ev": win_prob / (best / 100.0) - 1.0, "venue": venue.key,
            "ref_book": f"agent:{client}"[:60],
        }
        common = dict(limit_prob=limit_prob, shares=shares, model_prob=win_prob, now=now,
                      best_ask_cents=best, book_depth=depth, fee_rate=fee_rate)
        described = {
            "venue": venue.key, "team": row["team"], "shares": shares,
            "limit_cents": cents, "fee_rate": round(fee_rate, 4),
        }

        if not live:
            fill_prob = round(engine._paper_fill_prob({"levels": levels, "best_ask_cents": best},
                                                      shares, limit_prob), 4)
            bet_id = engine._record(conn, row, market, usd=round(shares * fill_prob, 2),
                                    order_id=None, status="dry", error=None, **common)
            with conn:
                db.set_auto_bet_fill(conn, bet_id, shares, fill_prob)
            return {**described, "status": "paper", "bet_id": bet_id,
                    "fill_cents": round(fill_prob * 100, 2),
                    "usd": round(shares * fill_prob, 2),
                    "message": "Recorded as a PAPER bet at the price the live book would have "
                               "filled. No order was sent and no money moved."}

        try:
            result = await venue.buy(market, shares, cents)
        except venues_mod.OrderOutcomeUnknown as exc:
            logger.error("agent bet: %s order for %s got no answer (%s)", venue.label, game.id, exc)
            bet_id = engine._record(conn, row, market, usd=cost, order_id=None,
                                    status="unknown", error=str(exc)[:300], **common)
            return {**described, "status": "unknown", "bet_id": bet_id, "usd": cost,
                    "message": f"The order went to {venue.label} and no answer came back. It "
                               "may or may not be live. Tell the user to check their account; "
                               "the game is held and no second order will be sent."}
        except Exception as exc:  # noqa: BLE001
            reason = venue.error_text(exc)
            logger.warning("agent bet: %s order failed for %s: %s", venue.label, game.id, reason)
            engine._record(conn, row, market, usd=cost, order_id=None, status="error",
                           error=reason, **common)
            raise AgentRefused(f"{venue.label} refused the order: {reason}") from exc

        order_id = (result or {}).get("order_id") or ""
        if not order_id:
            engine._record(conn, row, market, usd=cost, order_id=None, status="unmatched",
                           error=None, **common)
            return {**described, "status": "no_fill", "usd": 0.0,
                    "message": "Nothing filled at or under the limit; no money was spent."}
        pending = bool(result.get("pending"))
        bet_id = engine._record(conn, row, market, usd=cost, order_id=order_id, status="placed",
                                error=engine.PENDING_NOTE if pending else None, **common)
        fill = None
        if pending:
            pass
        elif result.get("filled") and result.get("avg_prob"):
            fill = (round(float(result["filled"]), 4), round(float(result["avg_prob"]), 4))
            with conn:
                db.set_auto_bet_fill(conn, bet_id, fill[0], fill[1])
        else:
            fill = await engine._reconcile_fill(conn, bet_id, order_id, venue.key)
        spent = round(fill[0] * fill[1], 2) if fill else cost
        logger.info("agent bet PLACED on %s %s %s: %s shares @ %s¢ ($%.2f) order=%s by %s",
                    venue.label, game.id, side, shares, cents, spent, order_id, client)
        if not pending:
            _log_to_tracker(game.id, side, cents, spent, settings, venue)
        return {
            **described, "status": "placed", "bet_id": bet_id, "order_id": order_id,
            "filled_shares": fill[0] if fill else None,
            "fill_cents": round(fill[1] * 100, 2) if fill else None,
            "usd": spent, "note": result.get("note"),
            "message": ("A REAL order was placed." if fill else
                        "A REAL order was accepted; the fill is being confirmed."),
        }
    finally:
        conn.close()


def _log_to_tracker(game_id: str, side: str, cents: int, spent_usd: float, settings: dict,
                    venue) -> None:
    try:
        tracker.log_bet(
            game_id, side, prob_to_american(cents / 100.0), book=venue.key,
            stake_units=max(0.1, round(spent_usd / settings["unit_usd"], 2)),
            note=f"AGENT · {venue.label.lower()}",
        )
    except Exception as exc:  # noqa: BLE001 — the order is already live
        logger.warning("agent bet: tracker log failed for %s: %s", game_id, exc)

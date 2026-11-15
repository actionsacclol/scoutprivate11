"""What each MCP tool does, and the one dispatcher every call goes through.

A tool never reimplements a screen. Each one calls the same endpoint function
the app's own UI calls (kryptscout.api.main), then trims the answer to what an
agent needs: a board row is ~4 KB of nested models as the UI gets it and a few
hundred bytes here. The numbers are the same numbers.

`call` is the only door: it checks arguments against the tool's schema,
counts the call, audits writes and refusals, turns every failure into a
sentence, and caps the size of what goes back.
"""
from __future__ import annotations

import contextvars
import json
import logging
import math
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from . import catalog, store

logger = logging.getLogger("kryptscout.mcp")

# A tool result is text the model has to read. Past this it is cut, with a
# note saying so, rather than flooding the client's context.
MAX_RESULT_CHARS = 60_000
DEFAULT_BOARD_LIMIT = 40
EXCHANGES = ("kalshi", "polymarket")


class ToolError(Exception):
    """A refusal with a sentence the agent can act on."""


# ------------------------------------------------------------------ helpers


def _main():
    from kryptscout.api import main
    return main


def _sport(args: dict, required: bool = True):
    from kryptscout.core.schemas import Sport
    raw = args.get("sport")
    if raw is None:
        if required:
            raise ToolError("sport is required: one of " + ", ".join(catalog.SPORTS))
        return None
    try:
        return Sport(str(raw).strip().lower())
    except ValueError:
        raise ToolError(f"unknown sport {raw!r}: use one of " + ", ".join(catalog.SPORTS))


def _date(args: dict, key: str) -> Optional[date]:
    raw = args.get(key)
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise ToolError(f"{key} must be a date like 2026-11-13, got {raw!r}")


def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    raw = args.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != int(raw):
        raise ToolError(f"{key} must be a whole number")
    return max(low, min(int(raw), high))


def _r(value: Any, places: int = 4) -> Any:
    if isinstance(value, float):
        return round(value, places) if math.isfinite(value) else None
    return value


def _iso(moment: Optional[datetime]) -> Optional[str]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _drop_none(row: dict) -> dict:
    return {k: v for k, v in row.items() if v is not None}


async def _endpoint(fn: Callable[..., Awaitable[Any]], *args, **kwargs) -> Any:
    """Call one of the API's endpoint functions; an HTTP error becomes a
    sentence instead of a status code."""
    from fastapi import HTTPException
    try:
        return await fn(*args, **kwargs)
    except HTTPException as exc:
        raise ToolError(str(exc.detail))


# ----------------------------------------------------------- board trimming


def _edge(edge) -> dict:
    return _drop_none({
        "side": edge.side,
        "book": edge.book,
        "price": edge.price,
        "fair_prob": _r(edge.fair_prob),
        "ref_book": edge.ref_book,
        "gap_pts": _r(edge.gap_pts, 2),
        "ev_per_dollar": _r(edge.ev_per_dollar),
        "exchanges_agree": edge.agree,
    })


def trim_card(card, now: Optional[datetime] = None) -> dict:
    """One board row, as an agent needs it."""
    from kryptscout.core.schemas import is_in_play
    from kryptscout.store import picks as picks_mod

    now = now or datetime.now(timezone.utc)
    game = card.game
    start = game.start_time if game.start_time.tzinfo else game.start_time.replace(tzinfo=timezone.utc)
    row: dict[str, Any] = {
        "game_id": game.id,
        "start": _iso(start),
        "started": start <= now,
        "home": game.home,
        "away": game.away,
    }
    if game.league:
        row["league"] = game.league
    if game.neutral_site:
        row["neutral_site"] = True
    if game.completed:
        row["final"] = {"home": game.home_score, "away": game.away_score}

    forecast = card.forecast
    if forecast is not None:
        row["forecast"] = _drop_none({
            "kind": forecast.kind,
            "source": forecast.source,
            "sharp": forecast.sharp,
            "thin_book": forecast.thin or None,
            "home": _r(forecast.home),
            "away": _r(forecast.away),
            "draw": _r(forecast.draw),
            "error_pts": _r(forecast.error_pts, 2),
        })
    pred = card.prediction
    if pred is not None:
        row["model"] = _drop_none({
            "name": pred.model,
            "home": _r(pred.home_win_prob),
            "away": _r(pred.away_win_prob),
            "draw": _r(pred.draw_prob),
            "total": _r(pred.predicted_total, 1),
        })
    fair = card.fair
    if fair is not None:
        row["fair"] = _drop_none({
            "source": fair.source,
            "sharp": fair.sharp,
            "thin_book": fair.thin or None,
            "home": _r(fair.home),
            "away": _r(fair.away),
            "draw": _r(fair.draw),
            "depth_usd": _r(fair.depth_usd, 0),
            "spread_pts": _r(fair.spread_pts, 2),
        })

    prices = []
    for quote in card.odds:
        prices.append(_drop_none({
            "book": quote.book,
            "exchange": True if quote.book in EXCHANGES else None,
            "home": quote.home_ml,
            "away": quote.away_ml,
            "draw": quote.draw_ml,
            "total": quote.total_line,
            "over": quote.over_price,
            "under": quote.under_price,
            "live": True if is_in_play(quote, start) else None,
        }))
    if prices:
        row["prices"] = prices

    pick = picks_mod.derive_pick(card)
    if pick is not None and pick.tier in ("STRONG", "LEAN"):
        row["value"] = _drop_none({
            "tier": pick.tier,
            "side": pick.side,
            "team": pick.team,
            "book": pick.book,
            "price": pick.price,
            "fair_prob": _r(pick.fair_prob),
            "ref_book": pick.ref_book,
            "ev_per_dollar": _r(pick.ev),
        })
    ml_edges = [_edge(e) for e in card.edges if e.fair_prob is not None]
    if ml_edges:
        row["value_edges"] = ml_edges
    if card.totals_edges:
        # Totals are unvalidated (core/totals.py): shown, never picked.
        row["totals_edges_unvalidated"] = [
            _drop_none({
                "side": e.side, "book": e.book, "price": e.price,
                "model_prob": _r(e.model_prob), "market_prob": _r(e.market_prob),
            })
            for e in card.totals_edges
        ]
    if card.arb is not None:
        row["arb"] = {
            "net_margin_pct": _r(card.arb.net_margin_pct, 3),
            "gross_margin_pct": _r(card.arb.margin_pct, 3),
            "legs": [
                {"side": leg.side, "book": leg.book, "price": leg.price,
                 "stake_fraction": _r(leg.stake_fraction)}
                for leg in card.arb.legs
            ],
        }
    if card.lean is not None:
        lean = card.lean
        row["lean"] = {
            "side": lean.side, "book": lean.book, "price": lean.price,
            "ref_book": lean.ref_book, "gap_pts": _r(lean.gap_pts, 2),
            "exchanges_agree": lean.agree,
        }
    return row


_BOARD_FILTERS: dict[str, Callable[[dict], bool]] = {
    "all": lambda row: True,
    "upcoming": lambda row: not row["started"],
    "priced": lambda row: bool(row.get("prices")),
    "value": lambda row: "value" in row,
    "arb": lambda row: "arb" in row,
    "lean": lambda row: "lean" in row,
}


# -------------------------------------------------------------------- tools


async def get_status(args: dict) -> dict:
    main = _main()
    from kryptscout.autobet import agent as autobet_agent
    from kryptscout.autobet import engine as autobet_engine
    from kryptscout.sports import base as engines

    ready = main._engines_ready.is_set()
    auto = autobet_engine.get_status()
    settings = auto.get("settings") or {}
    return {
        "app": catalog.SERVER_TITLE,
        "version": store.app_version(),
        "time": _iso(datetime.now(timezone.utc)),
        "engines_ready": ready,
        "engines": [{"sport": e.sport.value, "model": e.model_name} for e in engines.all_engines()],
        "autobet": {
            "enabled": bool(settings.get("enabled")),
            "dry_run": bool(settings.get("dry_run", True)),
            "wallet_connected": bool((auto.get("wallet") or {}).get("hasWalletKey")),
            "stopped": auto.get("stopped"),
            "today": auto.get("today"),
        },
        "agent_betting": autobet_agent.limits(settings),
        "note": "Probabilities are pre-game. `forecast` is the market's number wherever a market exists.",
    }


async def get_board(args: dict) -> dict:
    sport = _sport(args)
    only = str(args.get("only") or "all")
    if only not in _BOARD_FILTERS:
        raise ToolError("only must be one of " + ", ".join(_BOARD_FILTERS))
    limit = _int(args, "limit", DEFAULT_BOARD_LIMIT, 1, 200)
    cards = await _endpoint(_main().board, sport, True)
    now = datetime.now(timezone.utc)
    rows = [trim_card(card, now) for card in cards]
    kept = [row for row in rows if _BOARD_FILTERS[only](row)]
    out: dict[str, Any] = {
        "sport": sport.value,
        "as_of": _iso(now),
        "games_on_slate": len(rows),
        "games": kept[:limit],
    }
    if len(kept) > limit:
        out["more"] = f"{len(kept) - limit} more games match; raise limit (max 200) to see them."
    if not rows:
        out["note"] = "No games scheduled in the next 7 days."
    return out


async def get_game(args: dict) -> dict:
    sport = _sport(args)
    game_id = str(args.get("game_id") or "").strip()
    if not game_id:
        raise ToolError("game_id is required (it comes from get_board)")
    main = _main()
    cards = await _endpoint(main.board, sport, True)
    card = next((c for c in cards if c.game.id == game_id), None)
    if card is None:
        raise ToolError(
            f"No game {game_id!r} on the {sport.value} board. A game leaves the board "
            "once it is more than 7 days out or its day has passed; call get_board for current ids."
        )
    out = trim_card(card)
    try:
        out["line_movement"] = await _endpoint(main.movement, game_id)
    except ToolError:
        out["line_movement"] = None
    try:
        weather = await _endpoint(main.weather, game_id)
        out["weather"] = weather.model_dump(mode="json") if weather is not None else None
    except Exception:  # noqa: BLE001 — weather is context, never the reason a game fails
        out["weather"] = None
    if card.sgp is not None:
        out["same_game_fair"] = {
            "total_line": card.sgp.total_line,
            "fair_over": _r(card.sgp.fair_over),
            "singles": {k: _r(v) for k, v in card.sgp.singles.items()},
        }
    return out


async def get_picks(args: dict) -> dict:
    sport = _sport(args, required=False)
    tiers = args.get("tiers")
    if tiers is not None and (not isinstance(tiers, list) or not all(isinstance(t, str) for t in tiers)):
        raise ToolError("tiers must be a list such as [\"STRONG\", \"LEAN\"]")
    return await _endpoint(
        _main().picks, _date(args, "date"), sport, ",".join(tiers) if tiers else None
    )


async def get_pick_record(args: dict) -> dict:
    from kryptscout.store import tracker

    main = _main()
    sport, since = _sport(args, required=False), _date(args, "since")
    log = tracker.pick_log(sport=sport, since=since)
    picks = log.get("picks") if isinstance(log, dict) else None
    return main._json_safe({
        "pnl": log.get("pnl") if isinstance(log, dict) else None,
        "roi": tracker.roi(sport=sport, since=since),
        "clv_by_tier": tracker.clv_by_tier(sport=sport, since=since),
        "clv_vs_close": tracker.clv_vs_close(sport=sport, since=since),
        "recent_picks": (picks or [])[:25],
        "note": "Flat 1-unit stakes. CLV is measured against the same book's own close.",
    })


async def get_model_record(args: dict) -> dict:
    main = _main()
    sport, since = _sport(args, required=False), _date(args, "since")
    return {
        "engines": await _endpoint(main.performance, sport, None, since),
        "vs_espn_and_market": await _endpoint(main.benchmark, sport, since),
        "note": "Settled games only. The market row is the benchmark the engines are measured against.",
    }


async def get_arbs(args: dict) -> dict:
    return await _endpoint(_main().arbs, _int(args, "limit", 25, 1, 200))


async def get_line_movement(args: dict) -> dict:
    game_id = str(args.get("game_id") or "").strip()
    if not game_id:
        raise ToolError("game_id is required (it comes from get_board)")
    return await _endpoint(_main().movement, game_id)


async def get_injuries(args: dict) -> dict:
    sport = _sport(args)
    reports = await _endpoint(_main().injuries, sport)
    needle = str(args.get("team") or "").strip().lower()
    by_team: dict[str, list[dict]] = {}
    for report in reports:
        if needle and needle not in report.team.lower():
            continue
        by_team.setdefault(report.team, []).append(_drop_none({
            "player": report.player,
            "position": report.position or None,
            "status": report.status,
            "detail": report.detail or None,
            "reported": _iso(report.date),
        }))
    out: dict[str, Any] = {"sport": sport.value, "teams": by_team}
    if not reports:
        out["note"] = "No injury feed for this sport, or nothing reported."
    return out


async def get_my_bets(args: dict) -> dict:
    main = _main()
    since = _date(args, "since")
    bets = await _endpoint(main.mybets, since)
    return {**bets, "clv": await _endpoint(main.mybets_clv, since)}


async def log_bet(args: dict) -> dict:
    main = _main()
    price = args.get("price")
    if isinstance(price, bool) or not isinstance(price, int):
        raise ToolError("price must be whole American odds, e.g. -110 or 135")
    try:
        body = main.MyBetRequest(
            game_id=str(args.get("game_id") or ""),
            side=args.get("side"),
            price=price,
            book=args.get("book"),
            stake_units=args.get("stake_units", 1.0),
            note=args.get("note"),
            line=args.get("line"),
        )
    except Exception as exc:  # noqa: BLE001 — pydantic.ValidationError, said plainly
        errors = getattr(exc, "errors", None)
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}" for e in errors()
        ) if callable(errors) else str(exc)
        raise ToolError(f"That bet could not be logged: {detail}")
    row = await _endpoint(main.create_mybet, body)
    return {"logged": row, "note": "Recorded in the user's bet log. Nothing was placed anywhere."}


async def delete_bet(args: dict) -> dict:
    bet_id = args.get("bet_id")
    if isinstance(bet_id, bool) or not isinstance(bet_id, int):
        raise ToolError("bet_id must be a whole number (it comes from get_my_bets)")
    return await _endpoint(_main().delete_mybet, bet_id)


async def get_autobet(args: dict) -> dict:
    from kryptscout.autobet import agent as autobet_agent
    from kryptscout.autobet import engine as autobet_engine

    limit = _int(args, "log_limit", 20, 0, 200)
    status = autobet_engine.get_status()
    wallet = status.get("wallet") or {}
    out = {
        "settings": status.get("settings"),
        "wallet": {
            "connected": bool(wallet.get("hasWalletKey")),
            "address": wallet.get("address"),
        },
        "exchanges": [
            {"venue": v.get("key"), "name": v.get("label"), "connected": bool(v.get("connected"))}
            for v in status.get("venues") or []
        ],
        "agent_betting": autobet_agent.limits(status.get("settings")),
        "today": status.get("today"),
        "pnl": status.get("pnl"),
        "stopped": status.get("stopped"),
        "last_sweep": status.get("last_sweep"),
        "recent_attempts": autobet_engine.recent_log(limit) if limit else [],
    }
    return _main()._json_safe(out)


# Who is calling, for the handlers that record it (set by call()).
_caller: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_caller", default="agent")


async def get_bet_prices(args: dict) -> dict:
    from kryptscout.autobet import agent

    game_id = str(args.get("game_id") or "").strip()
    if not game_id:
        raise ToolError("game_id is required (from get_board).")
    try:
        return await agent.prices(game_id)
    except agent.AgentRefused as exc:
        raise ToolError(str(exc)) from exc


async def place_bet(args: dict) -> dict:
    from kryptscout.autobet import agent

    missing = [k for k in ("game_id", "side", "venue", "usd", "max_price_cents", "win_prob")
               if args.get(k) in (None, "")]
    if missing:
        raise ToolError(f"missing: {', '.join(missing)}.")
    try:
        return await agent.place(
            game_id=str(args["game_id"]).strip(), side=str(args["side"]),
            venue=str(args["venue"]), usd=args["usd"],
            max_price_cents=args["max_price_cents"], win_prob=args["win_prob"],
            rationale=str(args.get("rationale") or "")[:300], client=_caller.get(),
            paper=args.get("paper") is True,
        )
    except agent.AgentRefused as exc:
        raise ToolError(str(exc)) from exc


HANDLERS: dict[str, Callable[[dict], Awaitable[Any]]] = {
    "get_status": get_status,
    "get_board": get_board,
    "get_game": get_game,
    "get_picks": get_picks,
    "get_pick_record": get_pick_record,
    "get_model_record": get_model_record,
    "get_arbs": get_arbs,
    "get_line_movement": get_line_movement,
    "get_injuries": get_injuries,
    "get_my_bets": get_my_bets,
    "log_bet": log_bet,
    "delete_bet": delete_bet,
    "get_autobet": get_autobet,
    "get_bet_prices": get_bet_prices,
    "place_bet": place_bet,
}

_SCHEMAS = {t["name"]: t["inputSchema"] for t in catalog.TOOLS}


# --------------------------------------------------------------- dispatcher


def _unknown_args(name: str, args: dict) -> Optional[str]:
    """Every tool's schema is closed (additionalProperties false). A misspelt
    argument is refused rather than ignored, or an agent that wrote `sports`
    would be handed the default and never know."""
    schema = _SCHEMAS[name]
    known = set(schema.get("properties") or {})
    extra = sorted(set(args) - known)
    if extra:
        allowed = ", ".join(sorted(known)) or "none"
        return f"unknown argument(s) {', '.join(extra)} for {name}. It takes: {allowed}."
    missing = [key for key in schema.get("required") or [] if args.get(key) in (None, "")]
    if missing:
        return f"{name} needs: {', '.join(missing)}."
    return None


def _audit_detail(name: str, args: dict, payload: Any) -> str:
    """What the user reads in the app's agent log for one accepted write. A
    bet says what happened to it, not just what was asked for."""
    if name == "place_bet" and isinstance(payload, dict):
        line = (
            f"{str(payload.get('status', '')).upper()} · {payload.get('team')} on "
            f"{payload.get('venue')} · {payload.get('shares')} @ {payload.get('limit_cents')}¢ "
            f"(${float(payload.get('usd') or 0.0):.2f}) · said "
            f"{float(args.get('win_prob') or 0.0) * 100:.0f}%"
        )
        why = str(args.get("rationale") or "").strip()
        return (f"{line} · {why}" if why else line)[:300]
    return _text(args)[:160]


def _text(payload: Any) -> str:
    text = json.dumps(payload, default=str, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_RESULT_CHARS:
        text = (
            text[:MAX_RESULT_CHARS]
            + f'\n…[cut at {MAX_RESULT_CHARS} characters of {len(text)}; '
              "ask for less — a smaller limit, one sport, or a `since` date]"
        )
    return text


def _result(payload: Any, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": payload if isinstance(payload, str) else _text(payload)}],
        "isError": is_error,
    }


async def call(name: str, args: dict, session: Optional[dict] = None) -> dict:
    """Run one tool → an MCP tool result. Never raises."""
    client = (session or {}).get("client") or "unknown client"
    writes = name in catalog.WRITE_TOOLS
    handler = HANDLERS.get(name)
    if handler is None:
        store.audit(client, name, "refused", "unknown tool")
        return _result(f"unknown tool: {name}", is_error=True)
    store.note_call(client, name)
    problem = _unknown_args(name, args)
    if problem:
        store.audit(client, name, "refused", problem)
        return _result(problem, is_error=True)
    _caller.set(str(client)[:40])
    try:
        payload = await handler(args)
    except ToolError as exc:
        store.audit(client, name, "refused", str(exc))
        return _result(str(exc), is_error=True)
    except Exception as exc:  # noqa: BLE001 — a tool must answer, whatever broke
        logger.exception("mcp tool %s failed", name)
        store.audit(client, name, "error", f"{type(exc).__name__}: {exc}")
        return _result(
            f"{name} failed inside Krypt Scout ({type(exc).__name__}). "
            "The app's log has the details; try again in a moment.",
            is_error=True,
        )
    if writes:
        store.audit(client, name, "accepted", _audit_detail(name, args, payload))
    return _result(payload)

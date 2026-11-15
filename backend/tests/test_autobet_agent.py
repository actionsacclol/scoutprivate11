"""Bets asked for by an AI agent (autobet/agent.py, MCP get_bet_prices /
place_bet). No network: the board and the Kalshi venue are stubbed, the other
two venues list nothing. Everything on disk lives under tmp_path.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kryptscout import config
from kryptscout.autobet import agent, engine
from kryptscout.autobet import venues as venues_mod
from kryptscout.core.schemas import FairPrices, Game, GameCard, MoneylineOdds, Prediction, Sport
from kryptscout.mcp import store, tools
from kryptscout.store import db, tracker

NOW = datetime.now(timezone.utc)
BOARD: list[GameCard] = []
# What the stubbed Kalshi book offers: 50 shares at 42c, 100 more at 44c.
BOOK = {"levels": [[0.42, 50.0], [0.44, 100.0]]}
CALLS: list[tuple] = []


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.setattr(engine, "SETTINGS_FILE", tmp_path / "autobet.json")
    monkeypatch.setattr(engine, "PROFILES_FILE", tmp_path / "autobet_profiles.json")
    monkeypatch.setattr(engine, "_last_sweep", None)
    monkeypatch.setattr(engine, "_live_license_ok", lambda: True)
    monkeypatch.setattr(engine.engines, "_REGISTRY",
                        {sport: SimpleNamespace(sport=sport) for sport in Sport})
    BOARD.clear()
    CALLS.clear()
    BOOK["levels"] = [[0.42, 50.0], [0.44, 100.0]]

    async def board(sports):
        return [c for c in BOARD if c.game.sport in sports]

    async def nothing(self, *args, **kwargs):
        return None

    async def resolve(self, sport, game, side):
        return venues_mod.VenueMarket("kalshi", f"KX-{game.id}-{side}", "yes", 0.42, "KX")

    async def meta(self, market):
        return {"fee_schedule": {"enabled": True, "rate": 0.07, "exponent": 1.0}, "min_size": 1.0}

    async def ask_depth(self, market, max_price_cents):
        levels = [lv for lv in BOOK["levels"] if lv[0] <= max_price_cents / 100.0 + 1e-9]
        if not levels:
            return None
        return {"best_ask_cents": round(levels[0][0] * 100),
                "depth_shares": sum(lv[1] for lv in levels), "levels": levels}

    async def buy(self, market, shares, price_cents):
        CALLS.append((market.condition_id, shares, price_cents))
        return {"order_id": "k-1", "filled": float(shares), "avg_prob": 0.42,
                "note": None, "pending": False}

    monkeypatch.setattr(engine, "_load_cards", board)
    monkeypatch.setattr(venues_mod.PolymarketVenue, "resolve", nothing)
    monkeypatch.setattr(venues_mod.PolymarketUSVenue, "resolve", nothing)
    monkeypatch.setattr(venues_mod.PolymarketVenue, "connected", lambda self: False)
    monkeypatch.setattr(venues_mod.PolymarketUSVenue, "connected", lambda self: False)
    monkeypatch.setattr(venues_mod.KalshiVenue, "resolve", resolve)
    monkeypatch.setattr(venues_mod.KalshiVenue, "meta", meta)
    monkeypatch.setattr(venues_mod.KalshiVenue, "ask_depth", ask_depth)
    monkeypatch.setattr(venues_mod.KalshiVenue, "buy", buy)
    monkeypatch.setattr(venues_mod.KalshiVenue, "connected", lambda self: True)
    store.reset_for_tests()
    yield
    store.reset_for_tests()


def _game(game_id: str = "nba-1", hours_out: float = 3.0, sport: Sport = Sport.NBA,
          fair_home: float = 0.45) -> Game:
    game = Game(id=game_id, sport=sport, league="", start_time=NOW + timedelta(hours=hours_out),
                home="Boston Celtics", away="Miami Heat")
    card = GameCard(
        game=game,
        prediction=Prediction(game_id=game.id, sport=sport, model="nba-elo-v1", generated_at=NOW,
                              home_win_prob=0.5, away_win_prob=0.5),
        odds=[MoneylineOdds(book="kalshi", fetched_at=NOW, home_ml=138, away_ml=-150)],
        fair=FairPrices(game_id=game.id, sport=sport, source="polymarket", sharp=True,
                        home=fair_home, away=1.0 - fair_home, fetched_at=NOW,
                        sources={"polymarket": fair_home}),
    )
    tracker.record_cards([card])
    BOARD.append(card)
    return game


def _place(**over) -> dict:
    args = dict(game_id="nba-1", side="home", venue="kalshi", usd=10, max_price_cents=50,
                win_prob=0.5, client="claude")
    args.update(over)
    return asyncio.run(agent.place(**args))


def _rows() -> list[dict]:
    conn = db.connect()
    try:
        return db.rows(conn, "SELECT * FROM auto_bets ORDER BY id")
    finally:
        conn.close()


# ------------------------------------------------------------------ the switch


def test_agent_betting_is_off_until_the_user_turns_it_on():
    _game()
    assert engine.load_settings()["agent_mode"] == "off"
    with pytest.raises(agent.AgentRefused, match="switched off"):
        _place()
    assert _rows() == [] and CALLS == []


def test_settings_clamp_the_agent_knobs():
    saved = engine.save_settings({"agent_mode": "live", "agent_max_usd": 99999,
                                  "agent_max_bets_per_day": 0})
    assert saved["agent_mode"] == "live"
    assert saved["agent_max_usd"] == 1000.0 and saved["agent_max_bets_per_day"] == 1
    # A value that is not a mode leaves the saved one alone.
    assert engine.save_settings({"agent_mode": "yolo"})["agent_mode"] == "live"
    # The rule engine's own switches do not turn agents on, or off.
    assert engine.save_settings({"enabled": True, "dry_run": False})["agent_mode"] == "live"
    assert engine._sanitize({})["agent_mode"] == "off"


# ------------------------------------------------------------------ paper


def test_paper_bet_fills_off_the_book_and_sends_nothing():
    _game()
    engine.save_settings({"agent_mode": "paper"})
    out = _place()
    # A 50% view supports 48c after Kalshi's 7% fee; two cents through the 42c
    # offer is the tighter cap.
    assert out["status"] == "paper" and out["limit_cents"] == 44
    assert out["shares"] == int(10 / 0.44) == 22
    assert out["fill_cents"] == 42.0 and out["usd"] == pytest.approx(9.24)
    assert CALLS == []
    (row,) = _rows()
    assert (row["status"], row["tier"], row["venue"]) == ("dry", "AGENT", "kalshi")
    assert row["ref_book"] == "agent:claude" and row["model_prob"] == 0.5
    assert row["fill_shares"] == 22 and row["fill_prob"] == 0.42
    assert tracker.my_bets()["bets"] == []


def test_paper_fill_walks_the_levels_it_would_cross():
    _game()
    BOOK["levels"] = [[0.42, 10.0], [0.44, 100.0]]
    engine.save_settings({"agent_mode": "paper"})
    out = _place()
    assert out["shares"] == 22
    assert out["fill_cents"] == pytest.approx((10 * 42 + 12 * 44) / 22, abs=0.01)


def test_live_mode_can_still_be_asked_for_a_paper_bet(monkeypatch):
    _game()
    engine.save_settings({"agent_mode": "live"})
    monkeypatch.setattr(venues_mod.KalshiVenue, "connected", lambda self: False)
    with pytest.raises(agent.AgentRefused, match="No Kalshi account is connected"):
        _place()
    assert _place(paper=True)["status"] == "paper"
    assert CALLS == []


# ------------------------------------------------------------------ refusals


def test_over_the_cap_is_refused_not_trimmed():
    _game()
    engine.save_settings({"agent_mode": "paper", "agent_max_usd": 10})
    with pytest.raises(agent.AgentRefused, match="over the user's cap"):
        _place(usd=10.5)
    assert _rows() == []


def test_a_price_the_stated_probability_does_not_support_is_refused():
    _game()
    engine.save_settings({"agent_mode": "live"})
    # 42c needs better than 42% before fees; 41% supports 39c.
    with pytest.raises(agent.AgentRefused, match="supports at most 39"):
        _place(win_prob=0.41)
    # The agent's own limit binds the same way.
    with pytest.raises(agent.AgentRefused, match="Your limit is 40"):
        _place(max_price_cents=40)
    assert _rows() == [] and CALLS == []


def test_one_bet_per_game_whoever_placed_it():
    _game()
    engine.save_settings({"agent_mode": "paper"})
    _place()
    with pytest.raises(agent.AgentRefused, match="already has a bet"):
        _place(side="away")
    assert len(_rows()) == 1


def test_the_daily_allowance():
    _game("nba-1")
    _game("nba-2")
    engine.save_settings({"agent_mode": "paper", "agent_max_bets_per_day": 1})
    _place(game_id="nba-1")
    with pytest.raises(agent.AgentRefused, match="1 agent bets a day"):
        _place(game_id="nba-2")
    assert agent.limits()["bets_left_today"] == 0


def test_agent_bets_do_not_use_the_rule_engines_allowance():
    _game("nba-1")
    # nba-2: Kalshi home quoted +138 (42c) against a Polymarket fair of 46%.
    _game("nba-2", fair_home=0.46)
    engine.save_settings({"agent_mode": "paper", "enabled": True, "max_bets_per_day": 1,
                          "venues": ["kalshi"], "tiers": ["STRONG", "LEAN"], "min_ev": 0.04})
    _place(game_id="nba-1")
    summary = asyncio.run(engine.sweep())
    assert summary["dry"] == 1
    assert [(r["game_id"], r["tier"]) for r in _rows()] == [("nba-1", "AGENT"), ("nba-2", "LEAN")]
    # ...and the engine leaves the game the agent already holds alone.
    assert summary["skipped"]["already_bet"] == 1


def test_started_unlisted_and_excluded_games():
    _game("nba-late", hours_out=0.05)
    _game("soc-1", sport=Sport.SOCCER)
    engine.save_settings({"agent_mode": "paper"})
    with pytest.raises(agent.AgentRefused, match="started or is about to"):
        _place(game_id="nba-late")
    with pytest.raises(agent.AgentRefused, match="not available"):
        _place(game_id="soc-1")
    with pytest.raises(agent.AgentRefused, match="Call get_board first"):
        _place(game_id="nope")
    assert _rows() == []


def test_no_readable_book_sends_nothing():
    _game()
    BOOK["levels"] = []
    engine.save_settings({"agent_mode": "live"})
    with pytest.raises(agent.AgentRefused, match="order book could not be read"):
        _place()
    assert _rows() == [] and CALLS == []


def test_a_stop_halts_agents_too(monkeypatch):
    _game()
    engine.save_settings({"agent_mode": "live", "stop_loss_usd": 50})
    monkeypatch.setattr(engine, "realized_pnl", lambda conn: {"today_usd": 0.0, "total_usd": -60.0})
    with pytest.raises(agent.AgentRefused, match="halted by the user's stop loss"):
        _place()
    assert CALLS == []


def test_live_needs_the_license(monkeypatch):
    _game()
    engine.save_settings({"agent_mode": "live"})
    monkeypatch.setattr(engine, "_live_license_ok", lambda: False)
    with pytest.raises(agent.AgentRefused, match="license"):
        _place()
    assert CALLS == []


# ------------------------------------------------------------------ live


def test_live_bet_goes_to_the_exchange_once_and_is_logged():
    _game()
    engine.save_settings({"agent_mode": "live", "unit_usd": 10})
    out = _place()
    assert CALLS == [("KX-nba-1-home", 22, 44)]
    assert out["status"] == "placed" and out["order_id"] == "k-1"
    assert out["filled_shares"] == 22.0 and out["fill_cents"] == 42.0
    assert out["usd"] == pytest.approx(9.24)
    (row,) = _rows()
    assert (row["status"], row["tier"], row["order_id"]) == ("placed", "AGENT", "k-1")
    (bet,) = tracker.my_bets()["bets"]
    assert bet["book"] == "kalshi" and bet["note"] == "AGENT · kalshi"


def test_an_unanswered_order_holds_the_game(monkeypatch):
    _game()
    engine.save_settings({"agent_mode": "live"})

    async def lost(self, market, shares, price_cents):
        CALLS.append("sent")
        raise venues_mod.OrderOutcomeUnknown("no reply from Kalshi")

    monkeypatch.setattr(venues_mod.KalshiVenue, "buy", lost)
    out = _place()
    assert out["status"] == "unknown" and "check their account" in out["message"]
    with pytest.raises(agent.AgentRefused, match="already has a bet"):
        _place()
    assert CALLS == ["sent"]
    assert [r["status"] for r in _rows()] == ["unknown"]
    assert tracker.my_bets()["bets"] == []


def test_a_refused_order_is_recorded_and_frees_the_game(monkeypatch):
    _game()
    engine.save_settings({"agent_mode": "live"})

    async def refused(self, market, shares, price_cents):
        raise RuntimeError("insufficient balance")

    monkeypatch.setattr(venues_mod.KalshiVenue, "buy", refused)
    with pytest.raises(agent.AgentRefused, match="Kalshi refused the order"):
        _place()
    assert [r["status"] for r in _rows()] == ["error"]
    assert agent.limits()["bets_left_today"] == 3


def test_nothing_filled_is_no_fill(monkeypatch):
    _game()
    engine.save_settings({"agent_mode": "live"})

    async def empty(self, market, shares, price_cents):
        return {"order_id": "", "filled": 0.0, "avg_prob": 0.0, "note": None, "pending": False}

    monkeypatch.setattr(venues_mod.KalshiVenue, "buy", empty)
    out = _place()
    assert out["status"] == "no_fill" and out["usd"] == 0.0
    assert [r["status"] for r in _rows()] == ["unmatched"]


# ------------------------------------------------------------------ prices + MCP


def test_prices_show_the_book_the_breakeven_and_the_other_exchanges_fair():
    _game()
    out = asyncio.run(agent.prices("nba-1"))
    assert out["agent_betting"]["mode"] == "off"
    home, away = out["prices"]
    assert (home["venue"], home["side"], home["best_ask_cents"]) == ("kalshi", "home", 42)
    assert home["shares_at_best_ask"] == 50.0 and home["fee_rate"] == 0.07
    assert home["breakeven_prob"] == pytest.approx(0.42 * (1 + 0.07 * 0.58), abs=1e-4)
    assert home["fair_from"] == "polymarket" and home["fair_prob"] == 0.45
    assert home["net_ev_vs_fair"] == pytest.approx(0.45 / 0.42 - 1 - 0.07 * 0.58, abs=1e-4)
    assert away["team"] == "Miami Heat"


def _call(name: str, **arguments):
    result = asyncio.run(tools.call(name, arguments, {"client": "claude-desktop"}))
    text = result["content"][0]["text"]
    try:
        return json.loads(text), result["isError"]
    except ValueError:
        return text, result["isError"]


def test_mcp_place_bet_refuses_in_a_sentence_and_audits_both_ways():
    _game()
    payload, is_error = _call("place_bet", game_id="nba-1", side="home", venue="kalshi", usd=10,
                              max_price_cents=50, win_prob=0.5)
    assert is_error and "switched off" in payload
    engine.save_settings({"agent_mode": "paper"})
    payload, is_error = _call("place_bet", game_id="nba-1", side="home", venue="kalshi", usd=10,
                              max_price_cents=50, win_prob=0.5, rationale="Tatum is back.")
    assert not is_error and payload["status"] == "paper"
    assert _rows()[0]["ref_book"] == "agent:claude-desktop"
    accepted, refused = store.audit_log()[:2]
    assert (refused["outcome"], accepted["outcome"]) == ("refused", "accepted")
    assert accepted["detail"].startswith("PAPER · Boston Celtics on kalshi · 22 @ 44¢ ($9.24)")
    assert "said 50%" in accepted["detail"] and accepted["detail"].endswith("Tatum is back.")

    payload, is_error = _call("place_bet", game_id="nba-1", side="home", venue="kalshi")
    assert is_error and "needs: usd, max_price_cents, win_prob" in payload
    status, _ = _call("get_bet_prices", game_id="nba-1")
    assert status["agent_betting"]["mode"] == "paper"
    assert status["agent_betting"]["bets_left_today"] == 2

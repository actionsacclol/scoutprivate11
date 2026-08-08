"""Autobet engine tests — settings sanitization, pick→order math, the sweep's
money rules (EV floor, dedupe, daily cap, dry-run vs live), and the API
endpoints. No network: resolve_market and place_limit_order are injected.
Everything runs against a tmp_path SQLite database + settings file.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from kryptscout.api import main
from kryptscout.autobet import engine
from kryptscout.autobet import polymarket_auth as auth
from kryptscout.core.schemas import Edge, GameCard, Game, Prediction, Sport
from kryptscout.odds.polymarket import MarketRef
from kryptscout.store import db, tracker

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _tmp_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.setattr(engine, "SETTINGS_FILE", tmp_path / "autobet.json")
    monkeypatch.setattr(engine, "PROFILES_FILE", tmp_path / "autobet_profiles.json")
    monkeypatch.setattr(engine, "_last_sweep", None)

    # No network: meta/fills lookups are fail-open None/[] stubs. The ask
    # book defaults to a deep book AT the limit price — dry mode fails
    # closed without a readable book, and a fill at the limit preserves the
    # sizing math the assertions expect. Tests override per-case.
    async def _no_meta(ticker):
        return None

    async def _deep_book(ticker, side, max_price_cents):
        return {
            "best_ask_cents": max_price_cents,
            "depth_shares": 1_000_000.0,
            "levels": [[max_price_cents / 100.0, 1_000_000.0]],
        }

    async def _no_fills(order_id, limit=200):
        return []

    monkeypatch.setattr(engine.api, "get_market_meta", _no_meta)
    monkeypatch.setattr(engine.api, "ask_depth", _deep_book)
    monkeypatch.setattr(engine.api, "get_fills_for_order", _no_fills)
    # Licensed by default — the license GATE is exercised by its own test.
    monkeypatch.setattr(engine.license, "is_licensed", lambda *a, **k: True)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main.app.router, "on_startup", [])
    with TestClient(main.app) as test_client:
        yield test_client


def _game(game_id: str, start: datetime) -> Game:
    return Game(
        id=game_id, sport=Sport.NBA, league="", start_time=start,
        home="Boston Celtics", away="Miami Heat",
    )


def _prediction(game: Game, home_prob: float = 0.6) -> Prediction:
    return Prediction(
        game_id=game.id, sport=game.sport, model="nba-elo-v1", generated_at=NOW,
        home_win_prob=home_prob, away_win_prob=1.0 - home_prob,
    )


def _edge(game: Game, ev: float = 0.06, price: int = 130) -> Edge:
    return Edge(
        game_id=game.id, sport=game.sport, side="home", book="polymarket",
        price=price, model_prob=0.55, market_prob=0.5,
        ev_per_dollar=ev, kelly_fraction=0.05,
    )


def _seed_pick(game_id: str = "nba-7001", hours_out: float = 3.0,
               ev: float = 0.06, price: int = 130) -> Game:
    g = _game(game_id, NOW + timedelta(hours=hours_out))
    tracker.record_cards(
        [GameCard(game=g, prediction=_prediction(g), edges=[_edge(g, ev, price)])]
    )
    return g


def _ref(cid: str = "0xcond1") -> MarketRef:
    return MarketRef(condition_id=cid, clob_side="yes", prob=0.44, slug="nba-mia-bos")


def _resolver(ref: MarketRef | None):
    async def resolve(sport, game, side):
        return ref

    return resolve


def _sweep(**kwargs) -> dict:
    return asyncio.run(engine.sweep(**kwargs))


def _auto_rows() -> list[dict]:
    conn = db.connect()
    try:
        return db.rows(conn, "SELECT * FROM auto_bets ORDER BY id")
    finally:
        conn.close()


# ------------------------------------------------------------------ settings


def test_settings_defaults_and_sanitize_clamps():
    s = engine.load_settings()
    assert s == engine.DEFAULT_SETTINGS
    saved = engine.save_settings(
        {"enabled": True, "dry_run": False, "stake_usd": 99999,
         "min_ev": -3, "tiers": ["LEAN", "MODEL", "bogus"],
         "max_bets_per_day": 0.5, "max_prob": 2, "unknown_knob": 1}
    )
    assert saved["enabled"] is True and saved["dry_run"] is False
    assert saved["stake_usd"] == 1000.0  # clamped
    assert saved["min_ev"] == 0.0
    assert saved["tiers"] == ["LEAN"]  # MODEL/bogus dropped
    assert saved["max_bets_per_day"] == 1
    assert saved["max_prob"] == 0.99
    assert "unknown_knob" not in saved
    assert engine.load_settings() == saved  # round-trips through the file


def test_settings_partial_patch_keeps_other_knobs():
    engine.save_settings({"stake_usd": 25})
    saved = engine.save_settings({"min_ev": 0.08})
    assert saved["stake_usd"] == 25.0 and saved["min_ev"] == 0.08


def test_settings_stake_mode_and_stops_sanitize():
    saved = engine.save_settings(
        {"stake_mode": "percent", "stake_pct": 200, "daily_stop_loss_usd": -5,
         "stop_loss_usd": 100, "stop_profit_usd": 250}
    )
    assert saved["stake_mode"] == "percent"
    assert saved["stake_pct"] == 50.0  # clamped
    assert saved["daily_stop_loss_usd"] == 0.0  # negative -> off
    assert saved["stop_loss_usd"] == 100.0
    assert saved["stop_profit_usd"] == 250.0
    # Bogus mode falls back to the saved value's default handling.
    assert engine.save_settings({"stake_mode": "yolo"})["stake_mode"] == "percent"


# ---------------------------------------------------------------- pick math


def test_model_prob_recovers_from_price_and_ev():
    # +130 (d=2.3), ev 0.05 -> p = 1.05/2.3
    assert engine._model_prob(130, 0.05) == pytest.approx(1.05 / 2.3)
    # -150 (d=1.6667), ev 0.04 -> p = 1.04/1.6667 = 0.624
    assert engine._model_prob(-150, 0.04) == pytest.approx(1.04 / (1 + 100 / 150))
    assert engine._model_prob(0, 0.05) is None  # degenerate price


def test_limit_cents_is_the_ev_floor_price():
    # Gross formula (fee_rate=0): p=0.52, min_ev 0.04 -> 0.52/1.04 = 0.5 -> 50c
    assert engine._limit_cents(0.52, 0.04, 0.85, fee_rate=0.0) == 50
    # capped by max_prob
    assert engine._limit_cents(0.99, 0.0, 0.85, fee_rate=0.0) == 85
    # floored to whole cents, never rounded up past the floor
    assert engine._limit_cents(0.4567, 0.0, 0.85, fee_rate=0.0) == 45
    # sub-1c prices are unusable
    assert engine._limit_cents(0.004, 0.0, 0.85, fee_rate=0.0) is None


def test_limit_cents_nets_the_sports_taker_fee():
    # Default fee_rate=0.05 (sports_fees_v2). Same pick pays LESS than gross:
    # r*q^2 - (1+min_ev+r)*q + m = 0 with m=0.52, min_ev=0.04 -> q=0.48795 -> 48c
    assert engine._limit_cents(0.52, 0.04, 0.85) == 48
    # m=0.4567, min_ev=0: q=0.44435 -> 44c (gross was 45c)
    assert engine._limit_cents(0.4567, 0.0, 0.85) == 44
    # Net EV at the returned cap really is >= min_ev.
    q = engine._limit_cents(0.52, 0.04, 0.85) / 100.0
    net_ev = 0.52 / q - 1.0 - 0.05 * (1.0 - q)
    assert net_ev >= 0.04
    # max_prob cap still binds
    assert engine._limit_cents(0.99, 0.0, 0.85) == 85


# ------------------------------------------------------------------- sweeps


def test_sweep_disabled_without_force_does_nothing(monkeypatch):
    _seed_pick()
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    summary = _sweep()
    assert summary["skipped"] == {"disabled": 1}
    assert _auto_rows() == []


def test_dry_run_records_would_be_bet_and_dedupes(monkeypatch):
    _seed_pick(ev=0.06, price=130)  # model p = 1.06/2.3 = 0.4609
    engine.save_settings({"enabled": True})  # dry_run stays default ON
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    summary = _sweep()
    assert summary["dry"] == 1 and summary["placed"] == 0
    (row,) = _auto_rows()
    assert row["status"] == "dry" and row["order_id"] is None
    # model p = 0.4609; fee-aware limit (rate 0.05, min_ev 0.04) -> 43c;
    # shares = floor(10/0.43) = 23
    assert row["limit_prob"] == pytest.approx(0.43)
    assert row["shares"] == 23
    assert row["condition_id"] == "0xcond1"
    # Dry runs must never reach the user's bet log.
    assert tracker.my_bets()["bets"] == []

    again = _sweep()
    assert again["dry"] == 0 and again["skipped"].get("already_bet") == 1
    assert len(_auto_rows()) == 1


def test_live_sweep_places_fak_order_and_logs_tracker_bet(monkeypatch):
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True, "dry_run": False, "stake_usd": 20,
                          "unit_usd": 10})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.auth, "credentials_present", lambda env=None: True)
    calls = []

    async def fake_place(**kwargs):
        calls.append(kwargs)
        return {"order": {"order_id": "0xdeadbeef", "status": "matched"}}

    monkeypatch.setattr(engine.api, "place_limit_order", fake_place)

    summary = _sweep()
    assert summary["placed"] == 1 and summary["errors"] == 0
    (call,) = calls
    assert call["ticker"] == "0xcond1" and call["side"] == "yes"
    assert call["action"] == "buy" and call["order_type"] == "FAK"
    assert call["price_cents"] == 43  # fee-aware EV floor (rate 0.05)
    assert call["count"] == int(20 / 0.43)  # 46 shares
    (row,) = _auto_rows()
    assert row["status"] == "placed" and row["order_id"] == "0xdeadbeef"

    # Mirrored into the user's bet log at what the order ACTUALLY cost /
    # unit_usd — not the requested stake. 46 shares is the most $20 buys at
    # 43¢, so the real spend is 46 × 0.43 = $19.78 → 1.98u. Logging the
    # requested $20 (2.0u) would overstate every order, and the gap widens
    # whenever the depth gate shrinks the size or a FAK only partially fills.
    (bet,) = tracker.my_bets()["bets"]
    assert bet["book"] == "polymarket" and bet["note"] == "AUTO · polymarket"
    assert bet["stake_units"] == pytest.approx(1.98)
    assert bet["side"] == "home"


def test_live_sweep_without_wallet_skips(monkeypatch):
    _seed_pick()
    engine.save_settings({"enabled": True, "dry_run": False})
    monkeypatch.setattr(engine.auth, "credentials_present", lambda env=None: False)
    summary = _sweep()
    assert summary["skipped"] == {"no_wallet": 1}
    assert _auto_rows() == []


def test_unmatched_fak_retries_then_gives_up(monkeypatch):
    _seed_pick()
    engine.save_settings({"enabled": True, "dry_run": False})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.auth, "credentials_present", lambda env=None: True)

    async def no_fill(**kwargs):
        return {"order": {"order_id": "", "status": "unmatched"}}

    monkeypatch.setattr(engine.api, "place_limit_order", no_fill)
    for _ in range(engine.MAX_ATTEMPTS):
        assert _sweep()["unmatched"] == 1
    summary = _sweep()
    assert summary["unmatched"] == 0
    assert summary["skipped"].get("max_attempts") == 1
    assert len(_auto_rows()) == engine.MAX_ATTEMPTS
    assert tracker.my_bets()["bets"] == []  # zero fills never hit the log


def test_order_exception_records_error_row(monkeypatch):
    _seed_pick()
    engine.save_settings({"enabled": True, "dry_run": False})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.auth, "credentials_present", lambda env=None: True)

    async def boom(**kwargs):
        raise engine.api.PolymarketAPIError(400, "requested size 3 is below minimum")

    monkeypatch.setattr(engine.api, "place_limit_order", boom)
    summary = _sweep()
    assert summary["errors"] == 1
    (row,) = _auto_rows()
    assert row["status"] == "error" and "below minimum" in row["error"]


def test_live_sweep_requires_license(monkeypatch):
    """Live betting is gated on an active license — but ONLY live. Dry-run must
    still work (funds-safety carve-out: never block viewing/settling/paper)."""
    _seed_pick()
    engine.save_settings({"enabled": True, "dry_run": False})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.auth, "credentials_present", lambda env=None: True)
    monkeypatch.setattr(engine.license, "is_licensed", lambda *a, **k: False)

    async def _should_not_run(**kwargs):
        raise AssertionError("live order placed without a license")

    monkeypatch.setattr(engine.api, "place_limit_order", _should_not_run)
    summary = _sweep()
    assert summary["skipped"].get("unlicensed") == 1
    assert summary["placed"] == 0 and summary["dry"] == 0
    assert _auto_rows() == []

    # Same slate in DRY-RUN is unaffected by licensing — paper trading is free.
    engine.save_settings({"dry_run": True})
    summary2 = _sweep()
    assert summary2["dry"] == 1 and "unlicensed" not in summary2["skipped"]


def test_sweep_filters_ev_window_market_and_caps(monkeypatch):
    engine.save_settings({"enabled": True, "min_ev": 0.07, "max_bets_per_day": 1})
    # ev 0.05 is STRONG tier (>= 0.04) so it reaches the sweep — and fails
    # the engine's own, stricter min_ev.
    _seed_pick("nba-7101", hours_out=3, ev=0.05)         # below min_ev
    _seed_pick("nba-7102", hours_out=0.05, ev=0.08)      # inside lead window
    _seed_pick("nba-7103", hours_out=3, ev=0.08)         # blocked by daily cap
    _seed_pick("nba-7104", hours_out=4, ev=0.09)         # best EV -> dry first
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    summary = _sweep()
    assert summary["dry"] == 1
    assert summary["skipped"].get("below_min_ev") == 1
    assert summary["skipped"].get("out_of_window") == 1
    assert summary["skipped"].get("daily_cap") == 1


def test_max_days_ahead_caps_how_far_out_the_engine_bets(monkeypatch):
    engine.save_settings({"enabled": True, "max_days_ahead": 3})
    _seed_pick("nba-7201", hours_out=2 * 24, ev=0.08)   # 2 days out -> bets
    _seed_pick("nba-7202", hours_out=10 * 24, ev=0.09)  # 10 days out -> never scanned
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    summary = _sweep()
    assert summary["dry"] == 1
    assert summary["scanned"] == 1  # the far pick is outside the scan window
    (row,) = _auto_rows()
    assert row["game_id"] == "nba-7201"


def test_sweep_skips_unresolved_markets(monkeypatch):
    _seed_pick()
    engine.save_settings({"enabled": True})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(None))
    summary = _sweep()
    assert summary["skipped"].get("no_market") == 1
    assert _auto_rows() == []


def test_game_level_dedupe_blocks_the_other_side(monkeypatch):
    g = _seed_pick()
    engine.save_settings({"enabled": True})
    conn = db.connect()
    with conn:
        db.insert_auto_bet(
            conn, game_id=g.id, sport="nba", side="away", team="Miami Heat",
            tier="STRONG", ev=0.05, model_prob=0.5, condition_id="0xother",
            clob_side="no", slug="s", limit_prob=0.5, shares=20, usd=10.0,
            order_id="0xlive", status="placed", error=None, created_at=NOW,
        )
    conn.close()
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    summary = _sweep()
    # The home pick must NOT trade — the game already carries a live away bet.
    assert summary["skipped"].get("already_bet") == 1
    assert summary["dry"] == 0


def test_game_level_dedupe_holds_within_one_sweep(monkeypatch):
    """Two models disagreeing about ONE game must not both get bet.

    picks are stored per (game, model), so a second model produces a second
    pick row on the same game. Both are un-committed when the sweep's
    eligibility pass scores them — only the money pass writes rows — so the
    dedupe has to hold inside a single sweep, not just across sweeps.

    On a two-way market the two sides are the complementary YES/NO tokens of
    the SAME condition_id: buying both crosses the ask twice (cost 1 + spread
    against a $1 payout) and pays two taker fees.
    """
    start = NOW + timedelta(hours=3)
    g = _game("nba-7100", start)

    def _card(model: str, side: str) -> GameCard:
        pred = Prediction(
            game_id=g.id, sport=g.sport, model=model, generated_at=NOW,
            home_win_prob=0.6, away_win_prob=0.4,
        )
        edge = Edge(
            game_id=g.id, sport=g.sport, side=side, book="polymarket",
            price=130, model_prob=0.55, market_prob=0.5,
            ev_per_dollar=0.06, kelly_fraction=0.05,
        )
        return GameCard(game=g, prediction=pred, edges=[edge])

    tracker.record_cards([_card("nba-elo-v1", "home")])
    tracker.record_cards([_card("nba-gbm-v2", "away")])
    engine.save_settings({"enabled": True})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    summary = _sweep()
    assert summary["scanned"] == 2  # both picks were seen...
    assert summary["dry"] == 1  # ...but only one funded
    assert summary["skipped"].get("already_bet") == 1
    committed = [r for r in _auto_rows() if r["status"] in ("placed", "dry")]
    assert len(committed) == 1
    assert {r["game_id"] for r in committed} == {g.id}


def test_best_edge_ranking_picks_the_sharper_side_of_a_contested_game(monkeypatch):
    """When one game carries two opposing picks, the higher-EDGE one wins the
    slot — the dedupe must not just take whichever the board listed first."""
    start = NOW + timedelta(hours=3)
    g = _game("nba-7101", start)

    def _card(model: str, side: str, price: int) -> GameCard:
        pred = Prediction(
            game_id=g.id, sport=g.sport, model=model, generated_at=NOW,
            home_win_prob=0.6, away_win_prob=0.4,
        )
        edge = Edge(
            game_id=g.id, sport=g.sport, side=side, book="polymarket",
            price=price, model_prob=0.55, market_prob=0.5,
            ev_per_dollar=0.06, kelly_fraction=0.05,
        )
        return GameCard(game=g, prediction=pred, edges=[edge])

    # Same claimed EV, different prices → the shorter price carries more EDGE
    # (edge = model_prob − market-implied prob), so 'away' must win the slot.
    tracker.record_cards([_card("nba-elo-v1", "home", 250)])
    tracker.record_cards([_card("nba-gbm-v2", "away", 110)])
    engine.save_settings({"enabled": True, "best_edge_only": True})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    summary = _sweep()
    assert summary["dry"] == 1
    committed = [r for r in _auto_rows() if r["status"] in ("placed", "dry")]
    assert [r["side"] for r in committed] == ["away"]


def _seed_settled_auto_bet(game_id: str, side: str, home_score: int,
                           away_score: int, usd: float, shares: int,
                           created: datetime | None = None) -> None:
    """A PLACED auto bet on an already-settled game — realized P&L fodder."""
    g = _game(game_id, NOW - timedelta(hours=5))
    conn = db.connect()
    with conn:
        db.upsert_game(conn, g.id, g.sport.value, g.league, g.start_time,
                       g.home, g.away, home_score, away_score, True)
        db.mark_settled(conn, g.id, NOW, home_score, away_score, True)
        db.insert_auto_bet(
            conn, game_id=g.id, sport="nba", side=side, team="X", tier="STRONG",
            ev=0.05, model_prob=0.5, condition_id="0xc", clob_side="yes",
            slug="s", limit_prob=usd / shares, shares=shares, usd=usd,
            order_id="0xlive", status="placed", error=None,
            created_at=created or NOW,
        )
    conn.close()


def test_realized_pnl_from_settled_auto_bets():
    # Taker fee (rate 0.05 post 2026-07-10) is charged win or lose.
    # Home bet wins: 25 shares @ 44c pay $25, cost $11, fee 25*.05*.44*.56.
    _seed_settled_auto_bet("nba-8001", "home", 110, 100, usd=11.0, shares=25)
    # Away bet, home wins: -8 - fee(16*.05*.5*.5).
    _seed_settled_auto_bet("nba-8002", "away", 99, 90, usd=8.0, shares=16)
    conn = db.connect()
    pnl = engine.realized_pnl(conn)
    conn.close()
    fee1 = 25 * 0.05 * 0.44 * 0.56
    fee2 = 16 * 0.05 * 0.5 * 0.5
    assert pnl["total_usd"] == pytest.approx(14.0 - fee1 - 8.0 - fee2, abs=0.01)
    assert pnl["today_usd"] == pnl["total_usd"]  # created today
    assert pnl["open_usd"] == 0.0


def test_daily_stop_loss_halts_the_sweep(monkeypatch):
    _seed_settled_auto_bet("nba-8010", "away", 99, 90, usd=12.0, shares=24)  # -12 today
    _seed_pick()
    engine.save_settings({"enabled": True, "daily_stop_loss_usd": 10})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    summary = _sweep()
    assert summary["skipped"] == {"daily_stop_loss": 1}
    # -12 stake - 0.30 taker fee (24 shares @ 50c, rate 0.05)
    assert summary["pnl"]["today_usd"] == pytest.approx(-12.3)
    assert summary["scanned"] == 0  # halted before scanning anything


def test_stop_profit_halts_the_sweep(monkeypatch):
    _seed_settled_auto_bet("nba-8020", "home", 110, 100, usd=10.0, shares=40)  # +30
    _seed_pick()
    engine.save_settings({"enabled": True, "stop_profit_usd": 25})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    summary = _sweep()
    assert summary["skipped"] == {"stop_profit": 1}


def test_lifetime_stop_loss_counts_older_days(monkeypatch):
    # A loss booked YESTERDAY: outside the daily stop, inside the lifetime one.
    _seed_settled_auto_bet("nba-8030", "away", 99, 90, usd=30.0, shares=60,
                           created=NOW - timedelta(days=1))
    _seed_pick()
    engine.save_settings({"enabled": True, "daily_stop_loss_usd": 20,
                          "stop_loss_usd": 25})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    summary = _sweep()
    assert summary["skipped"] == {"stop_loss": 1}
    assert summary["pnl"]["today_usd"] == 0.0  # yesterday's loss isn't today's


def test_percent_stake_sizes_off_live_balance(monkeypatch):
    _seed_pick(ev=0.06, price=130)  # limit 44c, as elsewhere
    engine.save_settings({"enabled": True, "stake_mode": "percent", "stake_pct": 4})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    async def fake_balance():
        return {"balance": 50_000, "portfolio_value": None}  # $500 cash

    monkeypatch.setattr(engine.api, "get_balance", fake_balance)
    summary = _sweep()
    assert summary["stake_usd"] == pytest.approx(20.0)  # 4% of $500
    (row,) = _auto_rows()
    assert row["shares"] == int(20 / 0.43)  # 46 (fee-aware 43c limit)


def test_percent_stake_without_balance_halts_live(monkeypatch):
    _seed_pick()
    engine.save_settings({"enabled": True, "dry_run": False, "stake_mode": "percent"})
    monkeypatch.setattr(engine.auth, "credentials_present", lambda env=None: True)
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    async def no_balance():
        raise RuntimeError("all Polygon RPCs unreachable")

    monkeypatch.setattr(engine.api, "get_balance", no_balance)
    summary = _sweep()
    assert summary["skipped"] == {"no_balance": 1}
    assert _auto_rows() == []


def test_percent_stake_without_balance_uses_paper_bankroll_in_dry_run(monkeypatch):
    # Walletless installs must keep paper-trading: the sweep sizes off the
    # fixed paper bankroll instead of halting with no_balance.
    _seed_pick(ev=0.06, price=130)  # limit 43c (fee-aware), as elsewhere
    engine.save_settings({"enabled": True, "stake_mode": "percent", "stake_pct": 4})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    async def no_balance():
        raise RuntimeError("Polymarket wallet key not configured")

    monkeypatch.setattr(engine.api, "get_balance", no_balance)
    summary = _sweep()
    assert summary["skipped"] == {}
    assert summary["dry"] == 1
    assert summary["stake_usd"] == pytest.approx(
        engine.PAPER_BANKROLL_USD * 0.04
    )
    (row,) = _auto_rows()
    assert row["status"] == "dry"


# -------------------------------------------------- new strategy knobs/gates


def test_new_knobs_sanitize_and_confidence_mode():
    saved = engine.save_settings({
        "stake_mode": "confidence", "stake_pct_min": 12, "stake_pct_max": 3,
        "min_win_prob": 1.5, "min_edge": -1, "daily_stop_profit_usd": 40,
        "max_hours_ahead": 999,
    })
    assert saved["stake_mode"] == "confidence"
    # band clamps (0.1..50) and reorders low <= high
    assert saved["stake_pct_min"] == 3.0 and saved["stake_pct_max"] == 12.0
    assert saved["min_win_prob"] == 0.99  # clamped to <1
    assert saved["min_edge"] == 0.0       # negative -> off
    assert saved["daily_stop_profit_usd"] == 40.0
    assert saved["max_hours_ahead"] == 720  # clamped


def test_confidence_pct_interpolates():
    assert engine._confidence_pct(0.50, 2, 8) == pytest.approx(2.0)
    assert engine._confidence_pct(0.70, 2, 8) == pytest.approx(5.0)
    assert engine._confidence_pct(0.90, 2, 8) == pytest.approx(8.0)
    assert engine._confidence_pct(0.99, 2, 8) == pytest.approx(8.0)  # saturates


def test_min_win_prob_gate(monkeypatch):
    # price 130, ev 0.06 -> recovered model_prob 0.4609 (< 0.55 floor)
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True, "min_win_prob": 0.55})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    summary = _sweep()
    assert summary["skipped"].get("below_min_winprob") == 1
    assert _auto_rows() == []


def test_min_edge_gate(monkeypatch):
    # price 130 -> implied 0.4348, model_prob 0.4609 -> edge ~0.026 (< 0.05)
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True, "min_edge": 0.05})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    assert _sweep()["skipped"].get("below_min_edge") == 1
    # A looser floor lets it through (as a dry row).
    engine.save_settings({"min_edge": 0.02})
    assert _sweep()["dry"] == 1


def _depth(best_ask_cents: int, depth_shares: float):
    async def fake(ticker, side, max_price_cents):
        return {"best_ask_cents": best_ask_cents, "depth_shares": depth_shares}

    return fake


def test_depth_gate_shrinks_and_skips(monkeypatch):
    # $50 at ~48c limit wants ~104 shares; a 40-share book shrinks the order.
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True, "stake_usd": 50})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.api, "ask_depth", _depth(43, 40.0))
    assert _sweep()["dry"] == 1
    (row,) = _auto_rows()
    assert row["shares"] == 40
    assert row["usd"] == pytest.approx(40 * row["limit_prob"], abs=0.01)
    assert row["best_ask_cents"] == 43 and row["book_depth"] == 40.0


def test_depth_gate_empty_book_skips(monkeypatch):
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.api, "ask_depth", _depth(43, 0.0))
    assert _sweep()["skipped"].get("no_depth") == 1
    assert _auto_rows() == []


def test_depth_gate_fails_open_live(monkeypatch):
    # LIVE orders: a book-fetch error must never block the order (the FAK
    # price cap is the real safety mechanism).
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True, "dry_run": False})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.auth, "credentials_present", lambda env=None: True)

    async def fake_place(**kwargs):
        return {"order": {"order_id": "0xlive", "status": "matched"}}

    async def boom(ticker, side, max_price_cents):
        raise RuntimeError("book unavailable")

    monkeypatch.setattr(engine.api, "place_limit_order", fake_place)
    monkeypatch.setattr(engine.api, "ask_depth", boom)
    assert _sweep()["placed"] == 1
    (row,) = _auto_rows()
    assert row["best_ask_cents"] is None and row["book_depth"] is None


def test_dry_run_fails_closed_without_book(monkeypatch):
    # Paper mode: no readable book means no honest fill price — the pick is
    # skipped instead of recording a fictional fill at the limit cap.
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    async def boom(ticker, side, max_price_cents):
        raise RuntimeError("book unavailable")

    monkeypatch.setattr(engine.api, "ask_depth", boom)
    assert _sweep()["skipped"].get("no_book") == 1
    assert _auto_rows() == []


def test_dry_run_skips_uncrossable_market(monkeypatch):
    # The reported case: limit ~43c but the market is ASKING 86c — a real
    # FAK would zero-fill, so the dry run must not record a bet.
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.api, "ask_depth", _depth(86, 0.0))
    assert _sweep()["skipped"].get("no_depth") == 1
    assert _auto_rows() == []


def test_dry_run_fills_at_executable_ask(monkeypatch):
    # Limit 43c but the book offers 40c: the paper fill records the price a
    # real FAK would have paid, not the cap.
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True, "stake_usd": 10})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    async def book(ticker, side, max_price_cents):
        return {"best_ask_cents": 40, "depth_shares": 100.0,
                "levels": [[0.40, 100.0]]}

    monkeypatch.setattr(engine.api, "ask_depth", book)
    assert _sweep()["dry"] == 1
    (row,) = _auto_rows()
    assert row["shares"] == 23  # sized at the 43c worst case
    assert row["fill_shares"] == 23
    assert row["fill_prob"] == pytest.approx(0.40)
    assert row["usd"] == pytest.approx(23 * 0.40)
    assert row["best_ask_cents"] == 40


def test_paper_fill_walks_the_book(monkeypatch):
    # 23 shares across 10 @ 40c + 13 @ 42c -> share-weighted average fill.
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True, "stake_usd": 10})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    async def book(ticker, side, max_price_cents):
        return {"best_ask_cents": 40, "depth_shares": 110.0,
                "levels": [[0.40, 10.0], [0.42, 100.0]]}

    monkeypatch.setattr(engine.api, "ask_depth", book)
    assert _sweep()["dry"] == 1
    (row,) = _auto_rows()
    vwap = (10 * 0.40 + 13 * 0.42) / 23
    assert row["fill_prob"] == pytest.approx(vwap, abs=1e-4)


def test_dry_bets_grade_in_the_log(monkeypatch):
    # A settled game grades its dry row exactly like a placed one — result
    # and paper P&L (fees included, on the saved executable fill) appear in
    # the log, while the real-money stops stay untouched.
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True, "stake_usd": 10})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    async def book(ticker, side, max_price_cents):
        return {"best_ask_cents": 40, "depth_shares": 100.0,
                "levels": [[0.40, 100.0]]}

    monkeypatch.setattr(engine.api, "ask_depth", book)
    assert _sweep()["dry"] == 1
    (open_row,) = engine.recent_log()
    assert open_row["result"] is None and open_row["pnl_usd"] is None

    conn = db.connect()
    with conn:
        db.mark_settled(conn, "nba-7001", NOW, 110, 100, True)
    conn.close()
    (row,) = engine.recent_log()
    assert row["result"] == "W"
    # 23 shares pay $23, cost 23*0.40 = 9.20, fee = 23*rate*0.40*0.60.
    fee = 23 * row["fee_rate"] * 0.40 * 0.60
    assert row["pnl_usd"] == pytest.approx(23 - 9.20 - fee, abs=0.01)
    conn = db.connect()
    try:
        assert engine.realized_pnl(conn)["total_usd"] == 0.0  # dry ≠ stops
    finally:
        conn.close()


def test_every_candidate_logs_a_book_observation(monkeypatch):
    # Even a candidate the depth gate kills leaves a ground-truth row.
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.api, "ask_depth", _depth(43, 0.0))
    _sweep()
    conn = db.connect()
    try:
        (obs,) = db.rows(conn, "SELECT * FROM book_observations")
    finally:
        conn.close()
    assert obs["best_ask_cents"] == 43 and obs["depth_shares"] == 0.0
    assert obs["pick_prob"] == pytest.approx(1 / 2.3, abs=1e-4)  # +130 implied


def _meta_with_fee(rate, enabled=True, exponent=1.0, min_size=0.0):
    async def fake(ticker):
        return {
            "yes_token": "t1", "no_token": "t2", "min_size": min_size,
            "fee_schedule": {"enabled": enabled, "rate": rate,
                             "exponent": exponent},
        }

    return fake


def test_fee_schedule_overrides_constant(monkeypatch):
    # Fee-free market prices at the gross formula: p=0.52/min_ev=0.04 -> 50c
    # (the 5% default would give 48c — see test_limit_cents_nets_the_fee).
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.api, "get_market_meta",
                        _meta_with_fee(0.0, enabled=False))
    assert _sweep()["dry"] == 1
    (row,) = _auto_rows()
    assert row["fee_rate"] == 0.0
    # model_prob for +130/ev .06 is ~0.4609 -> gross limit .4609/1.04 = 44c
    assert int(round(row["limit_prob"] * 100)) == 44


def test_fee_schedule_unknown_exponent_falls_back(monkeypatch):
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.api, "get_market_meta",
                        _meta_with_fee(0.10, exponent=2.0))
    assert _sweep()["dry"] == 1
    (row,) = _auto_rows()
    assert row["fee_rate"] == engine.SPORTS_TAKER_FEE_RATE


def test_min_size_preempts_clob_rejection(monkeypatch):
    # $10 at ~43c limit is ~23 shares — below a 50-share market minimum.
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True, "stake_usd": 10})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.api, "get_market_meta",
                        _meta_with_fee(0.05, min_size=50))
    assert _sweep()["skipped"].get("below_min_size") == 1
    assert _auto_rows() == []


def test_fill_reconciliation_grades_partials(monkeypatch):
    # 20 requested, only 8 filled at 45c: P&L must grade the REAL fill.
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True, "dry_run": False, "stake_usd": 10})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    monkeypatch.setattr(engine.auth, "credentials_present", lambda env=None: True)

    async def fake_place(**kwargs):
        return {"order": {"order_id": "0xpartial", "status": "matched"}}

    async def fake_fills(order_id, limit=200):
        assert order_id == "0xpartial"
        return [{"order_id": order_id, "action": "buy", "count_fp": 8.0,
                 "price_cents": 45, "yes_price_dollars": 0.45,
                 "no_price_dollars": 0.55, "side": ""}]

    monkeypatch.setattr(engine.api, "place_limit_order", fake_place)
    monkeypatch.setattr(engine.api, "get_fills_for_order", fake_fills)
    assert _sweep()["placed"] == 1
    (row,) = _auto_rows()
    assert row["fill_shares"] == 8 and row["fill_prob"] == pytest.approx(0.45)

    # Settle the game as a WIN and check realized P&L uses the real fill:
    # 8 shares pay $8, cost 8*0.45=3.60, fee = 8*rate*q*(1-q).
    conn = db.connect()
    try:
        db.mark_settled(conn, "nba-7001", NOW, 110, 100, True)
        conn.commit()
        pnl = engine.realized_pnl(conn)
    finally:
        conn.close()
    fee = 8 * row["fee_rate"] * 0.45 * 0.55
    assert pnl["total_usd"] == pytest.approx(8 - 3.60 - fee, abs=0.01)


def test_min_prob_gate_filters_longshots(monkeypatch):
    # price 130 -> market implied prob 0.4348: below a 50c floor, above a 40c one.
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True, "min_prob": 0.50})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    assert _sweep()["skipped"].get("below_min_price") == 1
    assert _auto_rows() == []
    engine.save_settings({"min_prob": 0.40})
    assert _sweep()["dry"] == 1


def test_confidence_sizing_scales_with_model_prob(monkeypatch):
    # Low-prob pick (p≈0.46 → low band) vs high-prob pick (p≈0.84 → high band).
    _seed_pick(game_id="nba-lo", ev=0.06, price=130)
    _seed_pick(game_id="nba-hi", ev=0.05, price=-400)
    engine.save_settings({
        "enabled": True, "stake_mode": "confidence",
        "stake_pct_min": 2, "stake_pct_max": 8, "max_bets_per_day": 10,
    })
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    async def fake_balance():
        return {"balance": 50_000, "portfolio_value": None}  # $500 cash

    monkeypatch.setattr(engine.api, "get_balance", fake_balance)
    summary = _sweep()
    assert summary["dry"] == 2
    rows = {r["game_id"]: r for r in _auto_rows()}
    # The higher-confidence pick stakes materially more of the same balance.
    assert rows["nba-hi"]["usd"] > rows["nba-lo"]["usd"] * 2


def test_live_market_edge_gate(monkeypatch):
    # model_prob 0.4609 vs live market prob: 0.44 passes a 2pt floor
    # (edge 2.09pts), 0.45 fails it — the gate uses the VENUE's live price.
    _seed_pick(ev=0.06, price=130)
    engine.save_settings({"enabled": True, "min_edge": 0.02})
    monkeypatch.setattr(engine.polymarket, "resolve_market",
                        _resolver(MarketRef(condition_id="0xc", clob_side="yes",
                                            prob=0.45, slug="s")))
    assert _sweep()["skipped"].get("below_live_edge") == 1
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    assert _sweep()["dry"] == 1


def test_consensus_picks_never_bet(monkeypatch):
    g = _game("nba-7009", NOW + timedelta(hours=3))
    tracker.record_cards([GameCard(
        game=g, prediction=_prediction(g), edges=[_edge(g, 0.06, 130)],
    )])
    conn = db.connect()
    with conn:
        conn.execute("UPDATE picks SET model = 'market-consensus'")
    conn.close()
    engine.save_settings({"enabled": True})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    assert _sweep()["skipped.consensus" if False else "skipped"].get("consensus_pick") == 1


def test_implausible_claimed_ev_skipped(monkeypatch):
    # A "30% EV" pick is demoted STRONG → LEAN at derivation (SUSPECT_EV), so
    # tail LEAN here to prove the engine's own gate also refuses it.
    _seed_pick(ev=0.30, price=130)  # "30% EV" = model artifact
    engine.save_settings({"enabled": True, "tiers": ["STRONG", "LEAN"]})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    assert _sweep()["skipped"].get("ev_implausible") == 1


def test_percent_sizing_decrements_within_sweep(monkeypatch):
    # Two picks at 40% of balance each: the second sizes off the REMAINING
    # cash, so the sweep can never commit more than it started with.
    _seed_pick(game_id="nba-7101", ev=0.06, price=130)
    _seed_pick(game_id="nba-7102", ev=0.06, price=130)
    engine.save_settings({"enabled": True, "stake_mode": "percent",
                          "stake_pct": 40, "max_bets_per_day": 10})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))

    async def fake_balance():
        return {"balance": 10_000, "portfolio_value": None}  # $100 cash

    monkeypatch.setattr(engine.api, "get_balance", fake_balance)
    summary = _sweep()
    assert summary["dry"] == 2
    rows = _auto_rows()
    total = sum(r["usd"] for r in rows)
    assert total <= 100.0
    assert rows[1]["usd"] < rows[0]["usd"]  # second bet sized off remainder


def test_validated_wnba_preset_exists():
    s = engine.apply_strategy("Validated WNBA")
    assert s["sports"] == ["wnba"] and s["min_ev"] == 0.08
    assert s["min_edge"] == 0.08 and s["max_hours_ahead"] == 6


def test_sport_allowlist_filters_picks(monkeypatch):
    _seed_pick()  # NBA pick
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    engine.save_settings({"enabled": True, "sports": ["mlb", "wnba"]})
    assert _sweep()["skipped"].get("sport_filtered") == 1
    # Empty allowlist = every sport.
    engine.save_settings({"sports": []})
    assert _sweep()["dry"] == 1


def test_daily_take_profit_halts_the_sweep(monkeypatch):
    _seed_settled_auto_bet("nba-8040", "home", 110, 100, usd=10.0, shares=40)  # +30 today
    _seed_pick()
    engine.save_settings({"enabled": True, "daily_stop_profit_usd": 25})
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    summary = _sweep()
    assert summary["skipped"] == {"daily_stop_profit": 1}


# ---------------------------------------------------------------- best edge


def _seed_edge_pair() -> None:
    """Two STRONG picks where EV order and EDGE order disagree.

    edge = model_prob − implied = ev / decimal, so a longer price carries
    more claimed EV per dollar at LESS probability edge:
      nba-9101  ev 0.10 @ +130 (decimal 2.3) -> edge 0.043  (board lists 1st)
      nba-9102  ev 0.08 @ -200 (decimal 1.5) -> edge 0.053  (sharper)
    """
    _seed_pick("nba-9101", hours_out=3, ev=0.10, price=130)
    _seed_pick("nba-9102", hours_out=3, ev=0.08, price=-200)


def test_one_slot_goes_to_top_ev_without_best_edge(monkeypatch):
    """Baseline: the board's own STRONG-then-EV order wins the slot."""
    _seed_edge_pair()
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    engine.save_settings({"enabled": True, "max_bets_per_day": 1,
                          "best_edge_only": False})
    summary = _sweep()
    assert summary["dry"] == 1
    assert summary["skipped"].get("daily_cap") == 1
    assert [r["game_id"] for r in _auto_rows()] == ["nba-9101"]


def test_best_edge_only_spends_the_cap_on_the_sharpest_pick(monkeypatch):
    """Same slate, same cap — ranked by edge, so the sharper (lower-EV)
    pick takes the slot and the rest report not_best_edge, not daily_cap."""
    _seed_edge_pair()
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    engine.save_settings({"enabled": True, "max_bets_per_day": 1,
                          "best_edge_only": True})
    summary = _sweep()
    assert summary["dry"] == 1
    assert summary["skipped"].get("not_best_edge") == 1
    assert summary["skipped"].get("daily_cap") is None
    assert [r["game_id"] for r in _auto_rows()] == ["nba-9102"]


def test_best_edge_ranking_still_honors_every_other_gate(monkeypatch):
    """Ranking reorders eligible picks; it never promotes an ineligible one.
    The sharper pick here is outside the lead window, so the cap falls
    through to the next-best edge rather than going unspent."""
    _seed_pick("nba-9111", hours_out=3, ev=0.10, price=130)      # edge 0.043
    _seed_pick("nba-9112", hours_out=0.05, ev=0.08, price=-200)  # edge 0.053, too soon
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    engine.save_settings({"enabled": True, "max_bets_per_day": 1,
                          "best_edge_only": True})
    summary = _sweep()
    assert summary["dry"] == 1
    assert summary["skipped"].get("out_of_window") == 1
    assert [r["game_id"] for r in _auto_rows()] == ["nba-9111"]


def test_best_edge_only_sanitizes_as_a_strict_bool():
    assert engine.load_settings()["best_edge_only"] is False
    assert engine.save_settings({"best_edge_only": True})["best_edge_only"] is True
    assert engine.save_settings({"best_edge_only": False})["best_edge_only"] is False
    # Strict identity, like enabled/dry_run — a truthy string never arms it.
    assert engine.save_settings({"best_edge_only": "yes"})["best_edge_only"] is False
    # Absent from a patch = untouched (a profile predating the knob can't
    # silently flip it either way).
    engine.save_settings({"best_edge_only": True})
    assert engine.save_settings({"stake_usd": 20})["best_edge_only"] is True


def test_best_edge_preset_arms_the_ranking_and_others_clear_it():
    assert engine.apply_strategy("Best Edge")["best_edge_only"] is True
    assert engine.apply_strategy("Balanced")["best_edge_only"] is False


def test_max_hours_ahead_tightens_window(monkeypatch):
    _seed_pick(hours_out=50)  # 50h out
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    # max_days_ahead alone (7d) would allow it; 24h window rejects it.
    engine.save_settings({"enabled": True, "max_days_ahead": 7, "max_hours_ahead": 24})
    assert _sweep()["skipped"].get("out_of_window") == 1
    # Clearing the hours cap lets the day window apply again.
    engine.save_settings({"max_hours_ahead": 0})
    assert _sweep()["dry"] == 1


# ------------------------------------------------------------ presets/profiles


def test_presets_apply_without_arming():
    engine.save_settings({"enabled": True, "dry_run": False})  # arm first
    saved = engine.apply_strategy("Conservative")
    # Rules applied...
    assert saved["min_win_prob"] == 0.60 and saved["stake_mode"] == "confidence"
    # ...but the arm switches are never touched by a strategy.
    assert saved["enabled"] is True and saved["dry_run"] is False


def test_apply_unknown_strategy_raises():
    with pytest.raises(KeyError):
        engine.apply_strategy("Nope")


def test_save_apply_delete_profile_roundtrip():
    engine.save_settings({"min_ev": 0.09, "min_win_prob": 0.62})
    strategies = engine.save_profile("MyEdge")
    assert "MyEdge" in [p["name"] for p in strategies["profiles"]]
    # Change live settings, then re-apply the profile to restore its rules.
    engine.save_settings({"min_ev": 0.01, "min_win_prob": 0.0})
    restored = engine.apply_strategy("MyEdge")
    assert restored["min_ev"] == pytest.approx(0.09)
    assert restored["min_win_prob"] == pytest.approx(0.62)
    after = engine.delete_profile("MyEdge")
    assert "MyEdge" not in [p["name"] for p in after["profiles"]]


def test_profile_cannot_shadow_preset():
    with pytest.raises(ValueError):
        engine.save_profile("Balanced")
    with pytest.raises(ValueError):
        engine.save_profile("   ")


def test_profiles_persist_only_strategy_keys():
    engine.save_settings({"enabled": True, "dry_run": False, "min_ev": 0.07})
    engine.save_profile("Rules")
    saved = engine.load_profiles()["Rules"]
    assert "enabled" not in saved and "dry_run" not in saved and "unit_usd" not in saved
    assert saved["min_ev"] == pytest.approx(0.07)


# ---------------------------------------------------------------- endpoints


def test_autobet_profiles_endpoints(client, monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "PROFILES_FILE", tmp_path / "profiles.json")
    listed = client.get("/api/autobet/profiles").json()
    assert [p["name"] for p in listed["presets"]] == [
        "Validated WNBA", "Best Edge", "Conservative", "Balanced", "Aggressive",
    ]
    # apply a preset via the endpoint
    applied = client.post("/api/autobet/profiles", json={"name": "Aggressive", "apply": True})
    assert applied.status_code == 200
    assert applied.json()["settings"]["stake_mode"] == "confidence"
    # save current as a profile, then it shows up
    saved = client.post("/api/autobet/profiles", json={"name": "Mine"})
    assert "Mine" in [p["name"] for p in saved.json()["strategies"]["profiles"]]
    # shadowing a preset is a 422
    assert client.post("/api/autobet/profiles", json={"name": "Balanced"}).status_code == 422
    # delete it
    deleted = client.delete("/api/autobet/profiles/Mine")
    assert "Mine" not in [p["name"] for p in deleted.json()["strategies"]["profiles"]]


def test_autobet_status_and_settings_endpoints(client):
    body = client.get("/api/autobet").json()
    assert body["settings"]["enabled"] is False
    assert body["settings"]["dry_run"] is True
    assert body["settings"]["stake_mode"] == "fixed"
    assert "hasWalletKey" in body["wallet"]
    assert body["today"]["placed"] == 0
    assert body["pnl"] == {"today_usd": 0.0, "total_usd": 0.0, "open_usd": 0.0}
    assert body["stopped"] is None

    saved = client.post(
        "/api/autobet/settings", json={"stake_usd": 25, "tiers": ["STRONG", "LEAN"]}
    ).json()
    assert saved["stake_usd"] == 25.0 and saved["tiers"] == ["STRONG", "LEAN"]
    # Every knob the AUTO-BET form can send must survive the request model —
    # an undeclared field is dropped silently by pydantic and the toggle
    # would look like it saved while doing nothing.
    saved = client.post(
        "/api/autobet/settings", json={"best_edge_only": True, "min_prob": 0.2}
    ).json()
    assert saved["best_edge_only"] is True and saved["min_prob"] == 0.2


def test_autobet_run_endpoint_forces_sweep(client, monkeypatch):
    _seed_pick()
    monkeypatch.setattr(engine.polymarket, "resolve_market", _resolver(_ref()))
    # disabled + dry_run defaults: RUN NOW still dry-runs the pick.
    summary = client.post("/api/autobet/run").json()
    assert summary["dry"] == 1
    log = client.get("/api/autobet/log").json()["attempts"]
    assert len(log) == 1 and log[0]["status"] == "dry"
    assert log[0]["home"] == "Boston Celtics"  # joined game fields


def test_autobet_wallet_endpoints(client, monkeypatch, tmp_path):
    # Point the credential store at the tmp dir (auth resolves it per call).
    import kryptscout.config as config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    auth.reset_credential_cache()

    assert client.post("/api/autobet/wallet/test").status_code == 404  # no key
    resp = client.post("/api/autobet/wallet", json={"private_key": "not-a-key"})
    assert resp.status_code == 422

    async def fake_check():
        return {"ready": {"ok": True}, "balance": {"usd": 12.5}}

    monkeypatch.setattr(main.autobet_engine, "wallet_check", fake_check)
    good = "0x" + "11" * 32
    resp = client.post("/api/autobet/wallet", json={"private_key": good})
    assert resp.status_code == 200
    body = resp.json()
    assert body["wallet"]["hasWalletKey"] is True
    assert body["wallet"]["address"].startswith("0x")
    assert body["check"]["balance"]["usd"] == 12.5

    assert client.delete("/api/autobet/wallet").json() == {"cleared": True}
    assert client.get("/api/autobet").json()["wallet"]["hasWalletKey"] is False
    auth.reset_credential_cache()

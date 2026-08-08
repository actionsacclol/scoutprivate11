"""Pick tests — derivation parity with src/picks.ts, the freeze rule,
grading math, ROI aggregation, and the roi/jobs API endpoints.

Everything runs against a tmp_path SQLite database (db.DB_PATH is
monkeypatched); settle()'s results source and the API's engines/odds are
injected, so no test touches the network.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from kryptscout.api import main
from kryptscout.core.schemas import (
    Edge, Game, GameCard, MoneylineOdds, Prediction, Sport,
)
from kryptscout.jobs import capture as capture_job
from kryptscout.sports import base as engines
from kryptscout.store import db, picks, tracker

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")


def _game(
    game_id: str,
    start: datetime,
    home: str = "Home Team",
    away: str = "Away Team",
    sport: Sport = Sport.NBA,
    completed: bool = False,
) -> Game:
    return Game(
        id=game_id, sport=sport, league="", start_time=start,
        home=home, away=away, completed=completed,
    )


def _prediction(
    game: Game,
    home_prob: float,
    away_prob: float | None = None,
    draw_prob: float | None = None,
    model: str = "nba-elo-v1",
) -> Prediction:
    return Prediction(
        game_id=game.id,
        sport=game.sport,
        model=model,
        generated_at=NOW,
        home_win_prob=home_prob,
        away_win_prob=(1.0 - home_prob - (draw_prob or 0.0))
        if away_prob is None else away_prob,
        draw_prob=draw_prob,
    )


def _edge(game: Game, side: str, ev: float, book: str = "dk", price: int = 130) -> Edge:
    return Edge(
        game_id=game.id, sport=game.sport, side=side, book=book, price=price,
        model_prob=0.55, market_prob=0.5, ev_per_dollar=ev, kelly_fraction=0.05,
    )


def _final(game: Game, home_score: int, away_score: int) -> Game:
    return game.model_copy(
        update={"home_score": home_score, "away_score": away_score, "completed": True}
    )


def _fetcher(results: list[Game]):
    async def fetch(sport: Sport, on: date) -> list[Game]:
        return [r for r in results if r.start_time.date() == on]

    return fetch


def _pick_rows(sql_suffix: str = "") -> list[dict]:
    conn = db.connect()
    try:
        return db.rows(conn, f"SELECT * FROM picks {sql_suffix}")
    finally:
        conn.close()


# ------------------------------------------------- derivation (picks.ts parity)


def test_value_pick_takes_best_moneyline_edge_and_excludes_totals():
    g = _game("nba-1", NOW + timedelta(hours=4))
    card = GameCard(
        game=g,
        prediction=_prediction(g, 0.6),
        edges=[
            _edge(g, "over", 0.20, book="fd"),  # totals never become picks
            _edge(g, "home", 0.05, book="dk", price=-120),
            _edge(g, "away", 0.02, book="fd", price=150),
            _edge(g, "under", 0.10, book="dk"),
        ],
    )
    p = picks.derive_pick(card)
    assert (p.side, p.team, p.book, p.price) == ("home", "Home Team", "dk", -120)
    assert (p.ev, p.kelly, p.tier) == (0.05, 0.05, "STRONG")  # ev >= 0.04


def test_value_pick_lean_tier_below_strong_threshold():
    g = _game("nba-2", NOW + timedelta(hours=4))
    card = GameCard(
        game=g,
        prediction=_prediction(g, 0.6),
        edges=[_edge(g, "away", 0.039, book="fd", price=110)],
    )
    p = picks.derive_pick(card)
    assert (p.side, p.team, p.tier) == ("away", "Away Team", "LEAN")


def test_value_pick_equal_ev_keeps_first_edge_like_ts_reduce():
    g = _game("nba-3", NOW + timedelta(hours=4))
    card = GameCard(
        game=g,
        prediction=_prediction(g, 0.6),
        edges=[_edge(g, "home", 0.03, book="dk"), _edge(g, "away", 0.03, book="fd")],
    )
    assert picks.derive_pick(card).side == "home"


def test_suspect_ev_demotes_strong_to_lean():
    # Live record: claimed EV above SUSPECT_EV anti-selects — never STRONG.
    g = _game("nba-4", NOW + timedelta(hours=4))
    card = GameCard(
        game=g,
        prediction=_prediction(g, 0.6),
        edges=[_edge(g, "home", picks.SUSPECT_EV + 0.01, book="dk")],
    )
    assert picks.derive_pick(card).tier == "LEAN"


def test_no_value_sport_only_yields_model_picks():
    # Tennis has measured negative alpha: edges never become value picks.
    g = _game("tennis-9", NOW + timedelta(hours=4), sport=Sport.TENNIS)
    card = GameCard(
        game=g,
        prediction=_prediction(g, 0.6, model="tennis-elo-v1"),
        edges=[_edge(g, "home", 0.08, book="polymarket")],
    )
    p = picks.derive_pick(card)
    assert (p.tier, p.book, p.price, p.ev) == ("MODEL", None, None, None)
    assert (p.side, p.model_prob) == ("home", 0.6)


def test_draw_value_pick_resolves_team_to_draw():
    g = _game("soccer-1", NOW + timedelta(hours=4), sport=Sport.SOCCER)
    card = GameCard(
        game=g,
        prediction=_prediction(g, 0.30, draw_prob=0.40, model="soccer-elo-v1"),
        edges=[_edge(g, "draw", 0.06, book="dk", price=250)],
    )
    p = picks.derive_pick(card)
    assert (p.side, p.team, p.tier) == ("draw", "DRAW", "STRONG")


def test_model_pick_fallback_argmax_without_book_or_price():
    g = _game("soccer-2", NOW + timedelta(hours=4), sport=Sport.SOCCER)
    card = GameCard(
        game=g,
        prediction=_prediction(g, 0.30, draw_prob=0.45, model="soccer-elo-v1"),
    )
    p = picks.derive_pick(card)
    assert (p.side, p.team, p.tier) == ("draw", "DRAW", "MODEL")
    assert (p.book, p.price, p.ev, p.kelly) == (None, None, None, None)
    assert p.model_prob == 0.45


def test_model_pick_when_only_over_under_edges_exist():
    g = _game("nba-4", NOW + timedelta(hours=4))
    card = GameCard(
        game=g,
        prediction=_prediction(g, 0.55),
        edges=[_edge(g, "over", 0.08), _edge(g, "under", 0.02)],
    )
    p = picks.derive_pick(card)
    assert (p.side, p.tier, p.book) == ("home", "MODEL", None)


def test_no_pick_for_completed_game_or_missing_prediction():
    done = _game("nba-5", NOW - timedelta(hours=5), completed=True)
    assert picks.derive_pick(GameCard(game=done, prediction=_prediction(done, 0.7))) is None
    blank = _game("nba-6", NOW + timedelta(hours=5))
    assert picks.derive_pick(GameCard(game=blank)) is None


# ---------------------------------------------------------------- freeze rule


def test_pick_updates_before_start_and_freezes_after():
    g = _game("nba-10", NOW + timedelta(hours=6))

    def card(ev: float, price: int, book: str) -> GameCard:
        return GameCard(
            game=g,
            prediction=_prediction(g, 0.6),
            edges=[_edge(g, "home", ev, book=book, price=price)],
        )

    counts = tracker.record_cards([card(0.05, -120, "dk")])
    assert counts["picks"] == 1
    (first,) = _pick_rows()
    assert (first["book"], first["price"], first["tier"]) == ("dk", -120, "STRONG")
    assert first["locked_at"] is None

    # Line moved before start: same row refreshed, created_at preserved.
    counts = tracker.record_cards([card(0.02, 105, "fd")])
    assert counts["picks"] == 1
    (moved,) = _pick_rows()
    assert moved["id"] == first["id"]
    assert (moved["book"], moved["price"], moved["tier"]) == ("fd", 105, "LEAN")
    assert moved["created_at"] == first["created_at"]

    # Game started (recorded start_time now in the past): pick is frozen.
    g = g.model_copy(update={"start_time": NOW - timedelta(minutes=5)})
    counts = tracker.record_cards([card(0.09, -200, "mgm")])
    assert counts["picks"] == 0
    (frozen,) = _pick_rows()
    assert (frozen["book"], frozen["price"], frozen["tier"]) == ("fd", 105, "LEAN")
    assert frozen["locked_at"] is not None


def test_first_pick_after_start_inserts_already_locked():
    g = _game("nba-11", NOW - timedelta(hours=1))
    tracker.record_cards([GameCard(game=g, prediction=_prediction(g, 0.7))])
    (row,) = _pick_rows()
    assert row["tier"] == "MODEL" and row["locked_at"] is not None


# --------------------------------------------------------------- grading math


def _settle_value_pick(
    price: int,
    side: str = "home",
    final: tuple[int, int] = (100, 90),
    sport: Sport = Sport.NBA,
    abandoned: bool = False,
) -> dict:
    """Record one value pick on a started game, settle it, return its row."""
    g = _game(f"{sport.value}-grade", NOW - timedelta(days=1), sport=sport)
    pred = (
        _prediction(g, 0.34, draw_prob=0.33, model=f"{sport.value}-elo")
        if sport is Sport.SOCCER else _prediction(g, 0.6)
    )
    tracker.record_cards(
        [GameCard(game=g, prediction=pred,
                  edges=[_edge(g, side, 0.05, price=price)])]
    )
    if abandoned:
        # No result within lookback: settle marks the game abandoned.
        summary = asyncio.run(
            tracker.settle(sport, lookback_days=0, fetch=_fetcher([]))
        )
        assert summary["abandoned"] == 1
    else:
        summary = asyncio.run(
            tracker.settle(sport, fetch=_fetcher([_final(g, *final)]))
        )
        assert summary["settled"] == 1
    assert summary["picks_graded"] == 1
    (row,) = _pick_rows()
    return row


def test_grade_positive_price_win():
    row = _settle_value_pick(130, side="home", final=(100, 90))
    assert (row["result"], row["units"]) == ("W", pytest.approx(1.3))


def test_grade_negative_price_win():
    row = _settle_value_pick(-150, side="home", final=(100, 90))
    assert row["result"] == "W"
    assert row["units"] == pytest.approx(100.0 / 150.0)  # +0.667u


def test_grade_loss():
    row = _settle_value_pick(120, side="away", final=(100, 90))
    assert (row["result"], row["units"]) == ("L", -1.0)


def test_grade_soccer_draw_pick_wins_on_tie():
    row = _settle_value_pick(250, side="draw", final=(1, 1), sport=Sport.SOCCER)
    assert (row["result"], row["units"]) == ("W", pytest.approx(2.5))


def test_grade_soccer_side_pick_loses_to_draw():
    row = _settle_value_pick(140, side="home", final=(2, 2), sport=Sport.SOCCER)
    assert (row["result"], row["units"]) == ("L", -1.0)


def test_grade_non_soccer_tie_pushes():
    row = _settle_value_pick(120, side="home", final=(20, 20), sport=Sport.NFL)
    assert (row["result"], row["units"]) == ("P", 0.0)


def test_grade_abandoned_game_pushes():
    row = _settle_value_pick(120, side="home", abandoned=True)
    assert (row["result"], row["units"]) == ("P", 0.0)


def test_grade_model_pick_has_result_but_null_units():
    g = _game("nba-20", NOW - timedelta(days=1))
    tracker.record_cards([GameCard(game=g, prediction=_prediction(g, 0.7))])
    asyncio.run(tracker.settle(Sport.NBA, fetch=_fetcher([_final(g, 90, 100)])))
    (row,) = _pick_rows()
    assert (row["tier"], row["result"], row["units"]) == ("MODEL", "L", None)


# ------------------------------------------------------------ roi aggregation


def test_roi_aggregates_tiers_sports_and_daily_cumulative():
    day1 = NOW - timedelta(days=2)
    day2 = NOW - timedelta(days=1)
    g1 = _game("nba-31", day1)  # STRONG +130 W  -> +1.3
    g2 = _game("nba-32", day2)  # LEAN  +120 L   -> -1.0
    g3 = _game("nba-33", day2)  # STRONG -150 W  -> +2/3
    g4 = _game("nba-34", day2)  # MODEL W        -> no units
    g5 = _game("nba-35", NOW + timedelta(days=1))  # pending, ungraded
    tracker.record_cards(
        [
            GameCard(game=g1, prediction=_prediction(g1, 0.6),
                     edges=[_edge(g1, "home", 0.05, price=130)]),
            GameCard(game=g2, prediction=_prediction(g2, 0.6),
                     edges=[_edge(g2, "away", 0.02, price=120)]),
            GameCard(game=g3, prediction=_prediction(g3, 0.6),
                     edges=[_edge(g3, "home", 0.06, price=-150)]),
            GameCard(game=g4, prediction=_prediction(g4, 0.7)),
            GameCard(game=g5, prediction=_prediction(g5, 0.7)),
        ]
    )
    finals = [_final(g1, 100, 90), _final(g2, 100, 90),
              _final(g3, 100, 90), _final(g4, 100, 90)]
    asyncio.run(tracker.settle(Sport.NBA, fetch=_fetcher(finals)))

    report = tracker.roi()
    overall = report["overall"]
    expected_units = 1.3 - 1.0 + 100.0 / 150.0
    assert overall["n_picks"] == 5 and overall["n_graded"] == 4
    assert overall["units"] == pytest.approx(expected_units)
    assert overall["roi_pct"] == pytest.approx(expected_units / 3 * 100.0)
    tiers = overall["by_tier"]
    assert tiers["STRONG"]["n"] == 2
    assert tiers["STRONG"]["units"] == pytest.approx(1.3 + 100.0 / 150.0)
    assert tiers["STRONG"]["win_pct"] == pytest.approx(1.0)
    assert tiers["LEAN"] == {"n": 1, "units": -1.0, "win_pct": pytest.approx(0.0)}
    assert tiers["MODEL"] == {"n": 1, "units": None, "win_pct": pytest.approx(1.0)}

    (nba,) = report["by_sport"]
    assert nba["sport"] == "nba" and nba["n"] == 4
    assert nba["units"] == pytest.approx(expected_units)
    assert nba["win_pct"] == pytest.approx(0.75)  # 3 W / 4 decided

    daily = report["daily"]  # value picks only
    assert [d["date"] for d in daily] == [
        day1.date().isoformat(), day2.date().isoformat()
    ]
    assert daily[0]["units"] == pytest.approx(1.3)
    assert daily[0]["cum_units"] == pytest.approx(1.3)
    assert daily[1]["units"] == pytest.approx(-1.0 + 100.0 / 150.0)
    assert daily[1]["cum_units"] == pytest.approx(expected_units)

    # Filters: sport miss and future since both empty the report, NaN-free.
    empty = tracker.roi(sport=Sport.MLB)
    assert empty["overall"]["n_picks"] == 0
    assert empty["overall"]["roi_pct"] is None
    assert empty["overall"]["by_tier"]["STRONG"]["win_pct"] is None
    assert empty["daily"] == [] and empty["by_sport"] == []
    since = tracker.roi(since=day2.date())
    assert since["overall"]["n_picks"] == 4  # day1's pick filtered out


# ------------------------------------------------------------------ pick log


def test_pick_log_lists_value_picks_newest_first_with_flat_1u_pnl():
    day1 = NOW - timedelta(days=2)
    day2 = NOW - timedelta(days=1)
    g1 = _game("nba-41", day1)  # STRONG +130 W -> +1.3
    g2 = _game("nba-42", day2)  # LEAN  +120 L -> -1.0
    g3 = _game("nfl-43", day2, sport=Sport.NFL)  # tie -> P, 0.0
    g4 = _game("nba-44", day2)  # MODEL pick: excluded from the log
    g5 = _game("nba-45", NOW + timedelta(days=1))  # pending value pick
    tracker.record_cards(
        [
            GameCard(game=g1, prediction=_prediction(g1, 0.6),
                     edges=[_edge(g1, "home", 0.05, price=130)]),
            GameCard(game=g2, prediction=_prediction(g2, 0.6),
                     edges=[_edge(g2, "away", 0.02, price=120)]),
            GameCard(game=g3, prediction=_prediction(g3, 0.6),
                     edges=[_edge(g3, "home", 0.05, price=120)]),
            GameCard(game=g4, prediction=_prediction(g4, 0.7)),
            GameCard(game=g5, prediction=_prediction(g5, 0.6),
                     edges=[_edge(g5, "home", 0.05, price=-110)]),
        ]
    )
    finals = [_final(g1, 100, 90), _final(g2, 100, 90), _final(g4, 100, 90)]
    asyncio.run(tracker.settle(Sport.NBA, fetch=_fetcher(finals)))
    asyncio.run(tracker.settle(Sport.NFL, fetch=_fetcher([_final(g3, 20, 20)])))

    report = tracker.pick_log()
    rows = report["picks"]
    # MODEL pick excluded; newest kickoff first — the pending pick on top.
    assert [r["game_id"] for r in rows] == ["nba-45", "nba-42", "nfl-43", "nba-41"]

    pending = rows[0]
    assert pending["result"] is None and pending["units"] is None
    assert pending["completed"] == 0
    win = rows[-1]
    assert (win["result"], win["units"]) == ("W", pytest.approx(1.3))
    # Rows join the games table: matchup + kickoff + final score.
    assert (win["home"], win["away"]) == ("Home Team", "Away Team")
    assert win["kickoff"].startswith(day1.date().isoformat())
    assert (win["home_score"], win["away_score"]) == (100, 90)

    pnl = report["pnl"]
    assert pnl["n"] == 4 and pnl["graded"] == 3
    assert pnl["units"] == pytest.approx(0.3)  # +1.3 - 1.0 + 0.0
    assert pnl["roi_pct"] == pytest.approx(10.0)  # 0.3u on 3u staked (flat 1u)
    assert pnl["record"] == "1-1-1"
    assert [d["date"] for d in pnl["daily"]] == [
        day1.date().isoformat(), day2.date().isoformat()
    ]
    assert pnl["daily"][0]["cum_units"] == pytest.approx(1.3)
    assert pnl["daily"][1]["units"] == pytest.approx(-1.0)
    assert pnl["daily"][1]["cum_units"] == pytest.approx(0.3)

    # Filters mirror roi(): by sport, and by the game's UTC start date.
    nfl = tracker.pick_log(sport=Sport.NFL)
    assert [r["game_id"] for r in nfl["picks"]] == ["nfl-43"]
    assert nfl["pnl"]["record"] == "0-0-1"
    since = tracker.pick_log(since=day2.date())
    assert since["pnl"]["n"] == 3  # day1's winner drops; the pending stays


def test_pick_log_empty_store_is_none_safe():
    report = tracker.pick_log()
    assert report["picks"] == []
    assert report["pnl"] == {
        "n": 0, "graded": 0, "units": 0.0, "roi_pct": None,
        "record": "0-0-0", "daily": [],
    }


# ------------------------------------------------------------- api endpoints


START = NOW + timedelta(hours=6)
GAME = Game(
    id="nba-9001", sport=Sport.NBA, league="", start_time=START,
    home="Home Team", away="Away Team",
)
PREDICTION = Prediction(
    game_id=GAME.id, sport=Sport.NBA, model="nba-fake-v1", generated_at=NOW,
    home_win_prob=0.62, away_win_prob=0.38,
)
ODDS = MoneylineOdds(book="fakebook", fetched_at=NOW, home_ml=-120, away_ml=105)


class FakeEngine:
    sport = Sport.NBA
    model_name = "nba-fake-v1"

    async def upcoming_games(self, on=None) -> list[Game]:
        return [GAME]

    async def predict(self, games: list[Game]) -> list[Prediction]:
        return [PREDICTION]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main.app.router, "on_startup", [])
    monkeypatch.setattr(engines, "_REGISTRY", {Sport.NBA: FakeEngine()})
    # capture_all() re-imports every real sport module otherwise.
    monkeypatch.setattr(capture_job, "load_engines", lambda: None)

    async def fake_merged_odds(sport, games):
        return {GAME.id: [ODDS]}

    # Both the board endpoint and capture_all go through the provider registry.
    monkeypatch.setattr(main.odds_providers, "merged_odds", fake_merged_odds)
    monkeypatch.setattr(capture_job.odds_providers, "merged_odds", fake_merged_odds)
    with TestClient(main.app) as test_client:
        yield test_client


def test_roi_passthrough(client, monkeypatch):
    calls = []

    def fake_roi(sport=None, since=None):
        calls.append((sport, since))
        return {
            "overall": {"n_picks": 2, "roi_pct": float("nan")},
            "by_sport": [],
            "daily": [],
        }

    monkeypatch.setattr(tracker, "roi", fake_roi)
    resp = client.get("/api/roi?sport=nba&since=2026-07-01")
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall"]["n_picks"] == 2
    assert body["overall"]["roi_pct"] is None  # NaN sanitized to null
    assert calls == [(Sport.NBA, date(2026, 7, 1))]

    assert client.get("/api/roi").status_code == 200
    assert calls[-1] == (None, None)


def test_roi_bad_sport_422(client):
    assert client.get("/api/roi?sport=curling").status_code == 422


def test_picklog_passthrough(client, monkeypatch):
    calls = []

    def fake_pick_log(sport=None, since=None):
        calls.append((sport, since))
        return {
            "picks": [],
            "pnl": {"n": 0, "graded": 0, "units": 0.0,
                    "roi_pct": float("nan"), "record": "0-0-0", "daily": []},
        }

    monkeypatch.setattr(tracker, "pick_log", fake_pick_log)
    resp = client.get("/api/picklog?sport=nba&since=2026-07-01")
    assert resp.status_code == 200
    assert resp.json()["pnl"]["roi_pct"] is None  # NaN sanitized to null
    assert calls == [(Sport.NBA, date(2026, 7, 1))]

    assert client.get("/api/picklog").status_code == 200
    assert calls[-1] == (None, None)


def test_picklog_bad_sport_422(client):
    assert client.get("/api/picklog?sport=curling").status_code == 422


def test_jobs_capture_records_board_and_pick(client):
    resp = client.post("/api/jobs/capture", json={"sports": ["nba"]})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results == {
        "nba": {"games": 1, "predictions": 1, "snapshots": 1, "picks": 1}
    }
    # Home at -120 vs 0.62 is a +EV edge: the stored pick is a value pick.
    (row,) = _pick_rows()
    assert (row["side"], row["book"], row["price"]) == ("home", "fakebook", -120)
    assert row["tier"] == "STRONG"


def test_jobs_capture_without_body_covers_all_engines(client):
    resp = client.post("/api/jobs/capture")
    assert resp.status_code == 200
    assert list(resp.json()["results"]) == ["nba"]  # only registered engine


def test_jobs_capture_unknown_engine_reports_error(client):
    resp = client.post("/api/jobs/capture", json={"sports": ["mlb"]})
    assert resp.status_code == 200
    assert "error" in resp.json()["results"]["mlb"]


def test_jobs_settle_returns_results_performance_and_roi(client, monkeypatch):
    settled = []

    async def fake_settle(sport, lookback_days=3, fetch=None):
        settled.append(sport)
        return {"settled": 0, "abandoned": 0, "pending": 0, "picks_graded": 0, "user_bets_graded": 0}

    monkeypatch.setattr(tracker, "settle", fake_settle)
    resp = client.post("/api/jobs/settle")
    assert resp.status_code == 200
    body = resp.json()
    assert set(settled) == set(Sport)
    assert set(body) == {"results", "performance", "roi"}
    assert body["results"]["nba"]["picks_graded"] == 0
    assert body["roi"]["overall"]["n_picks"] == 0
    assert body["roi"]["overall"]["roi_pct"] is None  # NaN-safe passthrough


def test_jobs_capture_then_settle_roundtrip_grades_pick(client, monkeypatch):
    client.post("/api/jobs/capture", json={"sports": ["nba"]})
    # Time-travel: push the recorded game into the past so it settles.
    conn = db.connect()
    with conn:
        conn.execute(
            "UPDATE games SET start_time = ? WHERE game_id = ?",
            (db.iso(NOW - timedelta(days=1)), GAME.id),
        )
    conn.close()

    final = GAME.model_copy(
        update={
            "start_time": NOW - timedelta(days=1),
            "home_score": 101, "away_score": 99, "completed": True,
        }
    )
    monkeypatch.setattr(
        "kryptscout.store.tracker._default_results_fetcher",
        _fetcher([final]),
    )
    resp = client.post("/api/jobs/settle")
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"]["nba"] == {
        "settled": 1, "abandoned": 0, "pending": 0, "picks_graded": 1,
        "user_bets_graded": 0, "parlays_graded": 0,
    }
    roi = body["roi"]["overall"]
    assert roi["n_graded"] == 1
    # Home won at -120: +100/120 units on the STRONG value pick.
    assert roi["units"] == pytest.approx(100.0 / 120.0)
    assert roi["roi_pct"] == pytest.approx(100.0 / 120.0 * 100.0)


# -------------------------------------------------------------- picks listing


def _value_card(g: Game, ev: float, price: int = 120) -> GameCard:
    return GameCard(
        game=g,
        prediction=_prediction(g, 0.6),
        edges=[_edge(g, "home", ev, price=price)],
    )


def test_list_picks_filters_by_utc_date_boundary():
    day = date(2026, 1, 15)
    late = _game(  # 23:30 UTC on `day`
        "nba-51", datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    )
    early_next = _game(  # 00:30 UTC the next day
        "nba-52", datetime(2026, 1, 16, 0, 30, tzinfo=timezone.utc)
    )
    tracker.record_cards([_value_card(late, 0.05), _value_card(early_next, 0.05)])

    (row,) = tracker.list_picks(on=day)
    assert row["game_id"] == "nba-51"
    (row,) = tracker.list_picks(on=day + timedelta(days=1))
    assert row["game_id"] == "nba-52"
    assert tracker.list_picks(on=day - timedelta(days=1)) == []


def test_list_picks_tier_filter_and_ordering():
    base = datetime(2026, 1, 20, 18, 0, tzinfo=timezone.utc)
    day = base.date()
    cards = [
        _value_card(_game("nba-61", base + timedelta(hours=3)), 0.05),   # STRONG
        _value_card(_game("nba-62", base), 0.09),                        # STRONG, higher ev
        _value_card(_game("nba-63", base), 0.02),                        # LEAN
        # MODEL picks (no edges): late one recorded first to prove ordering
        # comes from start_time, not insertion.
        GameCard(game=_game("nba-64", base + timedelta(hours=5)),
                 prediction=_prediction(_game("nba-64", base), 0.7)),
        GameCard(game=_game("nba-65", base + timedelta(hours=1)),
                 prediction=_prediction(_game("nba-65", base), 0.7)),
    ]
    tracker.record_cards(cards)

    rows = tracker.list_picks(on=day)
    # STRONG by ev desc, then LEAN, then MODEL by start_time asc.
    assert [r["game_id"] for r in rows] == [
        "nba-62", "nba-61", "nba-63", "nba-65", "nba-64"
    ]
    assert [r["tier"] for r in rows] == [
        "STRONG", "STRONG", "LEAN", "MODEL", "MODEL"
    ]
    # Matchup join + kickoff ISO field on every row.
    top = rows[0]
    assert (top["home"], top["away"], top["league"]) == ("Home Team", "Away Team", "")
    assert top["kickoff"] == db.iso(base)
    assert datetime.fromisoformat(top["kickoff"]) == base

    value_only = tracker.list_picks(on=day, tiers=["STRONG", "LEAN"])
    assert [r["game_id"] for r in value_only] == ["nba-62", "nba-61", "nba-63"]
    # Tier matching is case-insensitive; sport filter narrows to nothing here.
    assert [r["tier"] for r in tracker.list_picks(on=day, tiers=["model"])] == [
        "MODEL", "MODEL"
    ]
    assert tracker.list_picks(on=day, sport=Sport.MLB) == []


def test_picks_endpoint_passthrough(client, monkeypatch):
    calls = []

    def fake_list_picks(on=None, sport=None, tiers=None):
        calls.append((on, sport, tiers))
        return [{"game_id": "nba-9001", "tier": "STRONG", "ev": float("nan")}]

    monkeypatch.setattr(tracker, "list_picks", fake_list_picks)
    resp = client.get("/api/picks?date=2026-07-09&sport=nba&tiers=STRONG,LEAN")
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2026-07-09"
    assert body["picks"][0]["game_id"] == "nba-9001"
    assert body["picks"][0]["ev"] is None  # NaN sanitized via _json_safe
    assert calls == [(date(2026, 7, 9), Sport.NBA, ["STRONG", "LEAN"])]

    # No params: date defaults to today UTC, filters pass through as None.
    resp = client.get("/api/picks")
    assert resp.status_code == 200
    today = datetime.now(timezone.utc).date()
    assert resp.json()["date"] == today.isoformat()
    assert calls[-1] == (today, None, None)


def test_picks_endpoint_bad_params_422(client):
    assert client.get("/api/picks?date=not-a-date").status_code == 422
    assert client.get("/api/picks?sport=curling").status_code == 422


# ------------------------------------------------- market-consensus fallback


def _ml(book: str, home: int, away: int) -> MoneylineOdds:
    return MoneylineOdds(book=book, fetched_at=NOW, home_ml=home, away_ml=away)


def test_market_fallback_adopts_polymarket_consensus():
    g = _game("tennis-1", NOW + timedelta(hours=4), sport=Sport.TENNIS)
    shrug = _prediction(g, 0.5, model="tennis-elo-v1").model_copy(
        update={"confidence": 0.1}
    )
    filled = picks.apply_market_fallback(
        shrug, [_ml("draftkings", -300, 250), _ml("polymarket", -441, 441)]
    )
    assert filled is not None
    assert filled.model == picks.MARKET_MODEL
    # Devigged from POLYMARKET (preferred), not draftkings.
    from kryptscout.core.oddsmath import remove_vig

    h, a = remove_vig(-441, 441)
    assert filled.home_win_prob == pytest.approx(h)
    assert filled.away_win_prob == pytest.approx(a)
    assert filled.home_win_prob > 0.75  # -441 favorite, nothing like 50/50


def test_market_fallback_drops_prediction_without_odds():
    g = _game("tennis-2", NOW + timedelta(hours=4), sport=Sport.TENNIS)
    shrug = _prediction(g, 0.5, model="tennis-elo-v1").model_copy(
        update={"confidence": 0.1}
    )
    assert picks.apply_market_fallback(shrug, []) is None


def test_market_fallback_leaves_real_predictions_alone():
    g = _game("nba-mf", NOW + timedelta(hours=4))
    real = _prediction(g, 0.62).model_copy(update={"confidence": 0.24})
    out = picks.apply_market_fallback(real, [_ml("polymarket", -200, 170)])
    assert out is real  # untouched, same object


def test_market_fallback_kills_fake_edges():
    g = _game("tennis-3", NOW + timedelta(hours=4), sport=Sport.TENNIS)
    shrug = _prediction(g, 0.5, model="tennis-elo-v1").model_copy(
        update={"confidence": 0.1}
    )
    odds = [_ml("polymarket", -1718, 1718)]
    filled = picks.apply_market_fallback(shrug, odds)
    edges = picks.compute_edges(g.id, g.sport, filled, odds)
    assert edges == []  # model == market -> no EV anywhere


def test_compute_edges_caps_implausible_ev():
    g = _game("tennis-4", NOW + timedelta(hours=4), sport=Sport.TENNIS)
    shrug_kept = _prediction(g, 0.5, model="x").model_copy(update={"confidence": 0.5})
    # 50% model vs +1718: EV ~ +8.09/$ — a model artifact, never a display edge.
    edges = picks.compute_edges(g.id, g.sport, shrug_kept, [_ml("polymarket", -1718, 1718)])
    assert all(e.ev_per_dollar <= picks.MAX_PLAUSIBLE_EV for e in edges)
    assert not any(e.side == "away" for e in edges)


# --------------------------------------------------------------- in-play guard
# Our probabilities are strictly pre-game, so an edge measured against a live
# price is the scoreboard rather than market inefficiency. See
# core.schemas.is_in_play.


def _ml_at(book: str, home: int, away: int, fetched: datetime) -> MoneylineOdds:
    return MoneylineOdds(book=book, fetched_at=fetched, home_ml=home, away_ml=away)


def test_compute_edges_ignores_named_in_play_feed():
    # ESPN publishes "DraftKings Live Odds" as its own provider; it must never
    # count as another pre-game book, even before kickoff.
    g = _game("nba-live-1", NOW + timedelta(hours=4))
    pred = _prediction(g, 0.62)
    live = _ml_at("draftkings_live_odds", 100, -120, NOW)
    assert picks.compute_edges(g.id, g.sport, pred, [live], g.start_time) == []
    # The identical prices from the pre-game feed do produce an edge.
    pregame = _ml_at("draftkings", 100, -120, NOW)
    assert picks.compute_edges(g.id, g.sport, pred, [pregame], g.start_time)


def test_compute_edges_drops_quotes_taken_after_kickoff():
    # Polymarket and Kalshi trade through the game, so a post-kickoff snapshot
    # is a live price with no "live" in its name to give it away.
    start = NOW - timedelta(hours=1)
    g = _game("nba-live-2", start)
    pred = _prediction(g, 0.62)
    in_play = _ml_at("polymarket", 100, -120, start + timedelta(minutes=30))
    assert picks.compute_edges(g.id, g.sport, pred, [in_play], start) == []
    # A quote captured before kickoff is still fair game.
    before = _ml_at("polymarket", 100, -120, start - timedelta(minutes=30))
    assert picks.compute_edges(g.id, g.sport, pred, [before], start)


def test_in_play_game_falls_through_to_model_pick():
    # A game under way keeps a pick — the model's side — but loses the value
    # tier, because the price the edge was arguing with no longer exists.
    start = NOW - timedelta(hours=1)
    g = _game("nba-live-3", start)
    pred = _prediction(g, 0.62)
    odds = [_ml_at("kalshi", 100, -120, start + timedelta(minutes=30))]
    edges = picks.compute_edges(g.id, g.sport, pred, odds, start)
    pick = picks.derive_pick(GameCard(game=g, prediction=pred, odds=odds, edges=edges))
    assert pick is not None
    assert pick.tier == "MODEL"
    assert pick.book is None and pick.ev is None


def test_compute_edges_without_kickoff_still_blocks_named_feeds():
    # starts_at is optional; the named-feed check must not depend on it.
    g = _game("nba-live-4", NOW + timedelta(hours=4))
    pred = _prediction(g, 0.62)
    live = _ml_at("draftkings_live", 100, -120, NOW)
    assert picks.compute_edges(g.id, g.sport, pred, [live]) == []

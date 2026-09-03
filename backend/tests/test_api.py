"""API endpoint tests — TestClient with fakes for engines/odds/tracker/datasources.

No network: the engine registry holds a fake engine, the merged odds lookup
and the injuries/weather datasources are monkeypatched, and the store writes
to a tmp_path SQLite database (db.DB_PATH is monkeypatched). App startup is
disabled so the real sport engines are never imported.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from kryptscout.api import main
from kryptscout.core.schemas import Game, MoneylineOdds, Prediction, Sport
from kryptscout.datasources import injuries as injuries_mod
from kryptscout.datasources import weather as weather_mod
from kryptscout.datasources.injuries import InjuryReport
from kryptscout.datasources.weather import GameWeather
from kryptscout.odds import providers as odds_providers
from kryptscout.sports import base as engines
from kryptscout.store import db, tracker

NOW = datetime.now(timezone.utc).replace(microsecond=0)
START = NOW + timedelta(hours=6)

GAME = Game(
    id="nba-1001", sport=Sport.NBA, league="", start_time=START,
    home="Home Team", away="Away Team",
)
PREDICTION = Prediction(
    game_id=GAME.id, sport=Sport.NBA, model="nba-fake-v1", generated_at=NOW,
    home_win_prob=0.62, away_win_prob=0.38,
)
ODDS = MoneylineOdds(book="fakebook", fetched_at=NOW, home_ml=-120, away_ml=105)
# Value edges are priced against the exchange fair (2026-09-02): Kalshi at
# -140/+120 devigs to ~0.56 home, and fakebook's -120 implies 0.545 — a
# 1.7-point gap, so the board carries one STRONG home edge.
KALSHI = MoneylineOdds(book="kalshi", fetched_at=NOW, home_ml=-140, away_ml=120)


class FakeEngine:
    sport = Sport.NBA
    model_name = "nba-fake-v1"

    async def upcoming_games(self, on=None) -> list[Game]:
        return [GAME]

    async def predict(self, games: list[Game]) -> list[Prediction]:
        return [PREDICTION]


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")


@pytest.fixture()
def client(monkeypatch):
    # Skip _load_engines (would import every real sport module) and serve
    # from a registry holding only the fake engine.
    monkeypatch.setattr(main.app.router, "on_startup", [])
    monkeypatch.setattr(engines, "_REGISTRY", {Sport.NBA: FakeEngine()})

    async def fake_merged_odds(sport, games):
        return {GAME.id: [ODDS, KALSHI]}

    monkeypatch.setattr(odds_providers, "merged_odds", fake_merged_odds)
    with TestClient(main.app) as test_client:
        yield test_client


def _insert_game(game: Game) -> None:
    conn = db.connect()
    try:
        with conn:
            db.upsert_game(
                conn, game.id, game.sport.value, game.league, game.start_time,
                game.home, game.away,
            )
    finally:
        conn.close()


# -------------------------------------------------------------------- board


def test_board_serves_and_records(client):
    resp = client.get("/api/board/nba")
    assert resp.status_code == 200
    cards = resp.json()
    assert len(cards) == 1
    card = cards[0]
    assert card["game"]["id"] == GAME.id
    assert card["prediction"]["model"] == "nba-fake-v1"
    assert card["odds"][0]["book"] == "fakebook"
    assert card["edges"]  # fakebook -120 sits under the Kalshi fair
    assert card["edges"][0]["ref_book"] == "kalshi" and card["edges"][0]["book"] == "fakebook"
    assert card["forecast"]["kind"] == "market" and card["forecast"]["source"] == "kalshi"

    # Serving the board recorded the game, prediction and odds snapshot.
    conn = db.connect()
    try:
        games = db.rows(conn, "SELECT game_id FROM games")
        preds = db.rows(conn, "SELECT game_id, model FROM predictions")
        snaps = db.rows(conn, "SELECT game_id, book FROM odds_snapshots")
    finally:
        conn.close()
    assert [g["game_id"] for g in games] == [GAME.id]
    assert preds == [{"game_id": GAME.id, "model": "nba-fake-v1"}]
    assert sorted(s["book"] for s in snaps) == ["fakebook", "kalshi"]


def test_board_survives_record_failure(client, monkeypatch):
    def boom(cards):
        raise RuntimeError("store on fire")

    monkeypatch.setattr(tracker, "record_cards", boom)
    resp = client.get("/api/board/nba")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_board_unknown_engine_404(client):
    assert client.get("/api/board/mlb").status_code == 404


# ----------------------------------------------------------------- injuries


def test_injuries_passthrough(client, monkeypatch):
    report = InjuryReport(
        team="Kansas City Chiefs", player="Patrick Mahomes", position="QB",
        status="Questionable", detail="Ankle - Sprain (Right)",
        comment="Limited in practice.", date=NOW,
    )

    async def fake_injuries(sport):
        assert sport is Sport.NFL
        return [report]

    monkeypatch.setattr(injuries_mod, "injuries", fake_injuries)
    resp = client.get("/api/injuries/nfl")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["player"] == "Patrick Mahomes"
    assert body[0]["status"] == "Questionable"


def test_injuries_empty_for_soccer(client, monkeypatch):
    async def no_fetch(path):  # soccer must never reach the network
        raise AssertionError("fetch called for a sport without a feed")

    monkeypatch.setattr(injuries_mod, "_fetch", no_fetch)
    resp = client.get("/api/injuries/soccer")
    assert resp.status_code == 200
    assert resp.json() == []


def test_injuries_bad_sport_422(client):
    assert client.get("/api/injuries/curling").status_code == 422


# ------------------------------------------------------------------ weather


def test_weather_unknown_game_404(client):
    assert client.get("/api/weather/nope-123").status_code == 404


def test_weather_null_when_unavailable(client):
    _insert_game(GAME)  # NBA: indoor sport, game_weather returns None
    resp = client.get("/api/weather/nba-1001")
    assert resp.status_code == 200
    assert resp.json() is None


def test_weather_found(client, monkeypatch):
    mlb_game = GAME.model_copy(
        update={"id": "mlb-2002", "sport": Sport.MLB, "home": "Boston Red Sox"}
    )
    _insert_game(mlb_game)
    forecast = GameWeather(
        temp_c=22.0, temp_f=71.6, wind_kph=14.0, wind_dir_deg=250.0,
        precip_prob_pct=30.0, summary="22°C, 14 kph wind, 30% rain",
    )
    calls = []

    async def fake_game_weather(sport, home_team, start_time):
        calls.append((sport, home_team, start_time))
        return forecast

    monkeypatch.setattr(weather_mod, "game_weather", fake_game_weather)
    resp = client.get("/api/weather/mlb-2002")
    assert resp.status_code == 200
    body = resp.json()
    assert body["temp_c"] == 22.0
    assert body["summary"].startswith("22°C")
    assert calls == [(Sport.MLB, "Boston Red Sox", START)]


# ----------------------------------------- performance / clv / movement


def test_performance_passthrough(client, monkeypatch):
    calls = []

    def fake_performance(sport=None, model=None, since=None):
        calls.append((sport, model, since))
        return {"overall": [{"sport": "mlb", "model": "m", "n": 3}], "daily": []}

    monkeypatch.setattr(tracker, "performance", fake_performance)
    resp = client.get("/api/performance?sport=mlb&model=m&since=2026-07-01")
    assert resp.status_code == 200
    assert resp.json()["overall"][0]["n"] == 3
    assert calls == [(Sport.MLB, "m", date(2026, 7, 1))]

    assert client.get("/api/performance").status_code == 200
    assert calls[-1] == (None, None, None)


def test_performance_bad_sport_422(client):
    assert client.get("/api/performance?sport=curling").status_code == 422


def test_clv_passthrough(client, monkeypatch):
    calls = []

    def fake_clv(sport=None, since=None):
        calls.append((sport, since))
        return {
            "overall": {"n": 0, "avg_clv_prob_pts": float("nan")},
            "by_sport": [],
            "games": [],
        }

    monkeypatch.setattr(tracker, "clv", fake_clv)
    resp = client.get("/api/clv?sport=nba&since=2026-06-01")
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall"]["n"] == 0
    assert body["overall"]["avg_clv_prob_pts"] is None  # NaN sanitized to null
    assert calls == [(Sport.NBA, date(2026, 6, 1))]


def test_movement_passthrough(client, monkeypatch):
    calls = []

    def fake_movement(game_id):
        calls.append(game_id)
        return {"game_id": game_id, "books": {"fakebook": [{"home_ml": -120}]}}

    monkeypatch.setattr(tracker, "line_movement", fake_movement)
    resp = client.get("/api/movement/nba-1001")
    assert resp.status_code == 200
    assert resp.json()["books"]["fakebook"][0]["home_ml"] == -120
    assert calls == ["nba-1001"]


def test_movement_roundtrip_via_board(client):
    """Serving the board records a snapshot that movement then returns."""
    client.get("/api/board/nba")
    resp = client.get("/api/movement/nba-1001")
    assert resp.status_code == 200
    books = resp.json()["books"]
    assert sorted(books) == ["fakebook", "kalshi"]
    assert books["fakebook"][0]["home_ml"] == -120

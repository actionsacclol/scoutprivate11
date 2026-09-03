"""ESPN predictor capture (datasources/espn_predictor), its store rows, the
backfill job's selection, and the benchmark that scores it."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kryptscout.core.schemas import Game, Sport
from kryptscout.datasources import espn_predictor
from kryptscout.jobs import predictor as predictor_job
from kryptscout.store import db, tracker

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=6)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")


def _game(game_id="mlb-401816563", sport=Sport.MLB, start=START, completed=False, hs=None, as_=None):
    return Game(id=game_id, sport=sport, league="", start_time=start, home="Tigers", away="Guardians",
                completed=completed, home_score=hs, away_score=as_)


def _doc(name="gameProjection", value=62.4, modified="2026-09-02T10:00Z", side="homeTeam"):
    return {"lastModified": modified, side: {"statistics": [{"name": name, "value": value, "displayValue": str(value)}]}}


# ---------------------------------------------------------------- parsing


def test_parse_reads_the_home_projection_and_stamps_pre_game():
    pred = espn_predictor.parse_predictor(_doc(), _game(), NOW)
    assert pred is not None
    assert pred.home_prob == pytest.approx(0.624)
    assert pred.pre_game is True
    assert pred.last_modified == datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)


def test_parse_accepts_wnba_stat_and_away_side_complement():
    pred = espn_predictor.parse_predictor(_doc(name="teamPredWinpct", value=41.0), _game(sport=Sport.WNBA), NOW)
    assert pred.home_prob == pytest.approx(0.41)
    away_only = espn_predictor.parse_predictor(_doc(value=30.0, side="awayTeam"), _game(), NOW)
    assert away_only.home_prob == pytest.approx(0.70)


def test_parse_flags_a_document_modified_after_kickoff():
    late = espn_predictor.parse_predictor(_doc(modified="2026-09-02T19:30Z"), _game(), NOW)
    assert late.pre_game is False
    # No lastModified at all: judged by the fetch time.
    fetched_late = espn_predictor.parse_predictor(_doc(modified=None), _game(), START + timedelta(hours=1))
    assert fetched_late.pre_game is False


def test_parse_rejects_documents_without_a_probability():
    assert espn_predictor.parse_predictor({"homeTeam": {"statistics": []}}, _game(), NOW) is None
    assert espn_predictor.parse_predictor(_doc(value=0.0), _game(), NOW) is None


# ---------------------------------------------------------------- capture + store


def test_capture_fetches_once_per_game_and_skips_unsupported(monkeypatch):
    calls: list[str] = []

    async def fake_fetch(games, concurrency=4):
        calls.extend(g.id for g in games)
        return {g.id: espn_predictor.parse_predictor(_doc(), g, NOW) for g in games}

    monkeypatch.setattr(espn_predictor, "fetch_predictors", fake_fetch)
    games = [_game(), _game("nfl-1", Sport.NFL), _game("soccer-1", Sport.SOCCER), _game("mlb-2", completed=True)]
    assert asyncio.run(espn_predictor.capture(games)) == 1
    assert calls == ["mlb-401816563"]
    # Second pass: already stored, nothing fetched.
    assert asyncio.run(espn_predictor.capture(games)) == 0
    assert calls == ["mlb-401816563"]
    conn = db.connect()
    [row] = db.rows(conn, "SELECT * FROM espn_predictions")
    conn.close()
    assert (row["game_id"], row["pre_game"]) == ("mlb-401816563", 1)
    assert row["home_prob"] == pytest.approx(0.624)


def test_backfill_selects_settled_supported_games_without_a_row():
    conn = db.connect()
    with conn:
        for gid, sport, done in (("mlb-1", "mlb", 1), ("mlb-2", "mlb", 0), ("nfl-1", "nfl", 1), ("wnba-1", "wnba", 1)):
            db.upsert_game(conn, gid, sport, "", START, "H", "A", 1 if done else None, 0 if done else None, bool(done))
        db.upsert_espn_prediction(
            conn, espn_predictor.parse_predictor(_doc(), _game("wnba-1", Sport.WNBA), NOW)
        )
    pending = predictor_job._pending(conn, None, False, None)
    conn.close()
    assert [g.id for g in pending] == ["mlb-1"]


# ---------------------------------------------------------------- benchmark


def _settle(conn, game_id, sport, hs, as_, espn_home, scout_home=None, close=None):
    start = NOW - timedelta(days=1)
    db.upsert_game(conn, game_id, sport, "", start, "H", "A", hs, as_, True)
    g = Game(id=game_id, sport=Sport(sport), league="", start_time=start, home="H", away="A")
    db.upsert_espn_prediction(
        conn,
        espn_predictor.parse_predictor(
            _doc(value=espn_home * 100, modified="2026-08-31T10:00Z"), g, NOW - timedelta(days=2)
        ),
    )
    if scout_home is not None:
        db.upsert_prediction(conn, game_id, sport, "m", start - timedelta(hours=3), scout_home, 1 - scout_home)
    if close is not None:
        db.upsert_line_endpoints(conn, game_id=game_id, sport=sport, book="draftkings", fetched_at=NOW,
                                 close_home_ml=close[0], close_away_ml=close[1])


def test_benchmark_scores_espn_scout_and_close_on_the_same_games():
    conn = db.connect()
    with conn:
        _settle(conn, "mlb-1", "mlb", 5, 2, espn_home=0.70, scout_home=0.55, close=(-150, 130))
        _settle(conn, "mlb-2", "mlb", 1, 4, espn_home=0.60, scout_home=0.40, close=(-110, -110))
        _settle(conn, "wnba-1", "wnba", 80, 70, espn_home=0.65)  # no scout row: ESPN-only
    conn.close()
    report = tracker.espn_benchmark()
    mlb = next(r for r in report["by_sport"] if r["sport"] == "mlb")
    assert mlb["n"] == 2 and mlb["scout"]["n"] == 2 and mlb["close"]["n"] == 2
    # ESPN called game 1's favourite right and game 2's wrong; Scout got both.
    assert mlb["espn"]["accuracy"] == pytest.approx(0.5)
    assert mlb["scout"]["accuracy"] == pytest.approx(1.0)
    assert mlb["scout"]["espn_accuracy"] == pytest.approx(0.5)  # ESPN re-scored on the same pair
    assert mlb["scout"]["log_loss"] < mlb["scout"]["espn_log_loss"]
    assert mlb["scout"]["beats_espn"] == pytest.approx(0.5)  # better on game 2 only
    assert report["overall"]["n"] == 3 and report["overall"]["scout"]["n"] == 2
    wnba = report["by_sport"][-1]
    assert wnba["sport"] == "wnba" and wnba["scout"] is None and wnba["espn"]["n"] == 1

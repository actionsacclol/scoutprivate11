"""Unit tests for the NBA engine (nba-elo-v1). No network — history is fed
as synthetic ESPN scoreboard payloads through the engine's replay seam."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from kryptscout.core.schemas import Game, Sport
from kryptscout.sports.nba import NbaEngine, _season_key, _season_start


def make_engine(tmp_path) -> NbaEngine:
    return NbaEngine(state_path=tmp_path / "state.json", raw_dir=tmp_path / "raw")


def event(home: str, away: str, hs: int, as_: int, season_type: int = 2) -> dict:
    """Minimal completed-game event in ESPN scoreboard shape."""
    return {
        "season": {"type": season_type},
        "competitions": [
            {
                "status": {"type": {"completed": True}},
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": home}, "score": str(hs)},
                    {"homeAway": "away", "team": {"displayName": away}, "score": str(as_)},
                ],
            }
        ],
    }


def rig_history(engine: NbaEngine, n_days: int = 30) -> None:
    """Sharks beat Minnows by 20 every night, venues alternating."""
    day = date(2025, 11, 1)
    for i in range(n_days):
        if i % 2 == 0:
            payload = {"events": [event("Boston Sharks", "Utah Minnows", 120, 100)]}
        else:
            payload = {"events": [event("Utah Minnows", "Boston Sharks", 100, 120)]}
        engine._process_day(day, payload)
        day += timedelta(days=1)


def game(home: str, away: str) -> Game:
    return Game(
        id="nba-test-1",
        sport=Sport.NBA,
        start_time=datetime(2025, 12, 25, tzinfo=timezone.utc),
        home=home,
        away=away,
    )


def test_replay_beats_coinflip_on_rigged_data(tmp_path) -> None:
    engine = make_engine(tmp_path)
    rig_history(engine)
    assert all(0.0 <= r[1] <= 1.0 for r in engine.backtest)
    metrics = engine.backtest_metrics()
    assert metrics["games"] == 30
    assert metrics["accuracy"] > 0.5
    assert metrics["log_loss"] < 0.6931  # better than always predicting 0.5
    assert engine.elo.get("Boston Sharks") > engine.elo.get("Utah Minnows") + 100


def test_predict_probabilities_and_totals(tmp_path) -> None:
    engine = make_engine(tmp_path)
    rig_history(engine)
    pred = asyncio.run(engine.predict([game("Boston Sharks", "Utah Minnows")]))[0]
    assert pred.model == "nba-elo-v1"
    assert pred.sport == Sport.NBA
    assert 0.0 <= pred.home_win_prob <= 1.0
    assert abs(pred.home_win_prob + pred.away_win_prob - 1.0) < 1e-9
    assert pred.home_win_prob > 0.5  # rigged winner at home must be favored
    # Sharks average 120 for / 100 against, Minnows the mirror image, so the
    # raw sides are 120 and 100; the Elo nudge moves points between sides but
    # must leave the total at 220.
    assert pred.predicted_total == 220.0
    assert abs(pred.predicted_home_score + pred.predicted_away_score - 220.0) < 0.11
    assert pred.predicted_home_score > pred.predicted_away_score


def test_predict_unknown_teams_falls_back_to_league_average(tmp_path) -> None:
    engine = make_engine(tmp_path)
    pred = asyncio.run(engine.predict([game("Ghost A", "Ghost B")]))[0]
    # Equal 1500 ratings: only home advantage separates the sides.
    assert 0.5 < pred.home_win_prob < 0.7
    assert pred.predicted_total is None  # no scoring history: no placeholder total (2026-09-02)


def test_preseason_allstar_and_unfinished_games_are_skipped(tmp_path) -> None:
    engine = make_engine(tmp_path)
    unfinished = event("A", "B", 50, 40)
    unfinished["competitions"][0]["status"]["type"]["completed"] = False
    # ESPN files the All-Star game under the regular season (season.type 2)
    # but marks the competition type ALLSTAR.
    allstar = event("Team Stars", "World", 160, 150)
    allstar["competitions"][0]["type"] = {"id": "4", "abbreviation": "ALLSTAR"}
    payload = {
        "events": [event("A", "B", 110, 100, season_type=1), allstar, unfinished]
    }
    engine._process_day(date(2025, 10, 5), payload)
    assert engine.backtest == []
    assert engine.elo.ratings == {}
    assert engine.last_processed == date(2025, 10, 5)


def test_season_rollover_regresses_ratings(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine._process_day(
        date(2025, 6, 10), {"events": [event("Boston Sharks", "Utah Minnows", 130, 90)]}
    )
    rating_before = engine.elo.get("Boston Sharks")
    assert rating_before > 1500.0
    assert _season_key(date(2025, 6, 10)) != _season_key(date(2025, 8, 1))
    engine._process_day(date(2025, 8, 1), {"events": []})  # crosses Aug 1
    rating_after = engine.elo.get("Boston Sharks")
    expected = 1500.0 + 0.5 * (rating_before - 1500.0)  # season_carry, retuned 2026-09-02
    assert abs(rating_after - expected) < 1e-9
    # Same-season day changes must not regress again.
    engine._process_day(date(2025, 8, 2), {"events": []})
    assert engine.elo.get("Boston Sharks") == rating_after


def test_backtest_row_is_the_pregame_probability(tmp_path) -> None:
    """Leakage guard: game N is logged at the probability held BEFORE it."""
    engine = make_engine(tmp_path)
    before = engine.elo.expected("Boston Sharks", "Utah Minnows")
    engine._process_day(
        date(2025, 11, 1), {"events": [event("Boston Sharks", "Utah Minnows", 120, 100)]}
    )
    assert engine.backtest == [
        ("2025-11-01", before, 1, "Boston Sharks", "Utah Minnows")
    ]
    # The result then moves the rating, so the rematch is priced differently.
    assert engine.elo.expected("Boston Sharks", "Utah Minnows") > before


def test_model_name_is_elo(tmp_path) -> None:
    engine = make_engine(tmp_path)
    rig_history(engine)
    assert engine.model_name == "nba-elo-v1"
    pred = asyncio.run(engine.predict([game("Boston Sharks", "Utah Minnows")]))[0]
    assert pred.model == "nba-elo-v1"
    assert pred.home_win_prob == engine.elo.expected("Boston Sharks", "Utah Minnows")


def test_season_start_tracks_the_august_boundary() -> None:
    assert _season_start(date(2026, 11, 13)) == date(2026, 8, 1)
    assert _season_start(date(2027, 3, 1)) == date(2026, 8, 1)
    assert _season_start(date(2026, 7, 31)) == date(2025, 8, 1)


def test_state_roundtrip(tmp_path) -> None:
    engine = make_engine(tmp_path)
    rig_history(engine, n_days=6)
    engine._save_state()
    reloaded = make_engine(tmp_path)
    assert reloaded.elo.ratings == engine.elo.ratings
    assert reloaded.last_processed == engine.last_processed
    assert reloaded.backtest == engine.backtest
    assert {t: list(v) for t, v in reloaded.points_for.items()} == {
        t: list(v) for t, v in engine.points_for.items()
    }


def test_state_written_by_the_xgb_era_still_loads(tmp_path) -> None:
    """A v4 state file from before the overlay was removed carries a feature
    log and rest/games-played tables. Its ratings are exactly what this
    engine computes, so it must load as-is — discarding it would force every
    existing install through a two-season replay for nothing."""
    import json

    engine = make_engine(tmp_path)
    rig_history(engine, n_days=6)
    engine._save_state()
    path = tmp_path / "state.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    old["model"] = "nba-xgb-v2"
    old["features"] = [[0.0] * 12 for _ in old["backtest"]]
    old["last_game"] = {"Boston Sharks": "2025-11-06"}
    old["games_played"] = {"Boston Sharks": 6}
    path.write_text(json.dumps(old), encoding="utf-8")

    reloaded = make_engine(tmp_path)
    assert reloaded.last_processed == engine.last_processed
    assert reloaded.elo.ratings == engine.elo.ratings
    assert reloaded.backtest == engine.backtest
    # And the next save drops the dead weight.
    reloaded._save_state()
    assert "features" not in json.loads(path.read_text(encoding="utf-8"))


def test_state_without_a_version_stamp_forces_full_replay(tmp_path) -> None:
    import json

    engine = make_engine(tmp_path)
    rig_history(engine, n_days=6)
    engine._save_state()
    legacy = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    del legacy["version"]
    (tmp_path / "state.json").write_text(json.dumps(legacy), encoding="utf-8")
    reloaded = make_engine(tmp_path)
    assert reloaded.last_processed is None  # ignored -> replay from scratch
    assert reloaded.backtest == []


def test_refresh_runs_through_yesterday_with_no_history_cap(tmp_path, monkeypatch) -> None:
    """A hard HISTORY_END once froze the engine at the 2025-26 Finals: no day
    was ever processed again, the state file was never rewritten, and the
    next season would never have been ingested. The replay must always run
    through yesterday (empty off-season days included) and persist."""
    from datetime import datetime, timezone
    engine = make_engine(tmp_path)
    engine.last_processed = date(2026, 6, 30)
    fetched: list[date] = []

    async def fake_day_json(self, client, day):
        fetched.append(day)
        return {"events": []}

    monkeypatch.setattr(type(engine), "_day_json", fake_day_json)
    asyncio.run(engine.refresh_data())
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    assert fetched and fetched[0] == date(2026, 7, 1) and fetched[-1] == yesterday
    assert engine.last_processed == yesterday
    assert (tmp_path / "state.json").exists()  # rewritten even with no games
    reloaded = make_engine(tmp_path)
    assert reloaded.last_processed == yesterday


def test_state_replayed_under_old_elo_constants_is_discarded(tmp_path) -> None:
    import json
    engine = make_engine(tmp_path)
    rig_history(engine, n_days=6)
    engine._save_state()
    old = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    old["version"] = 3  # pre-retune ratings
    (tmp_path / "state.json").write_text(json.dumps(old), encoding="utf-8")
    reloaded = make_engine(tmp_path)
    assert reloaded.last_processed is None and reloaded.elo.ratings == {}

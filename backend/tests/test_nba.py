"""Unit tests for the NBA engine (Elo core + XGB overlay). No network —
history is fed as synthetic ESPN scoreboard payloads through the engine's
replay seam, and model training runs on rigged two-season schedules."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import numpy as np

from kryptscout.core.schemas import Game, Sport
from kryptscout.sports.nba import (
    FEATURE_NAMES,
    LEAGUE_AVG_POINTS,
    PROB_FLOOR,
    REST_CAP,
    NbaEngine,
    _season_key,
)

F = {name: i for i, name in enumerate(FEATURE_NAMES)}


def make_engine(tmp_path) -> NbaEngine:
    return NbaEngine(
        state_path=tmp_path / "state.json",
        raw_dir=tmp_path / "raw",
        model_path=tmp_path / "model.pkl",
    )


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


def rig_two_seasons(engine: NbaEngine, days_per_season: int = 100) -> None:
    """Four teams of fixed strength, two seasons, two disjoint games a day.

    Scores are strength gap + home edge + deterministic pseudo-noise, so
    labels correlate with Elo but include upsets — enough signal for the
    trainer while keeping both classes present everywhere.
    """
    teams = ["Alpha", "Beta", "Gamma", "Delta"]
    strength = {"Alpha": 9, "Beta": 3, "Gamma": -3, "Delta": -9}
    day_types = [((0, 1), (2, 3)), ((0, 2), (1, 3)), ((0, 3), (1, 2))]
    game_no = 0
    for season_start in (date(2024, 11, 1), date(2025, 11, 1)):
        day = season_start
        for i in range(days_per_season):
            events = []
            for pair in day_types[i % 3]:
                h, a = pair if i % 2 == 0 else pair[::-1]
                home, away = teams[h], teams[a]
                noise = (game_no * 7) % 11 - 5  # deterministic, in [-5, 5]
                diff = strength[home] - strength[away] + 3 + noise
                if diff == 0:
                    diff = 1  # the NBA has no draws
                events.append(event(home, away, 110 + max(diff, 0), 110 - min(diff, 0)))
                game_no += 1
            engine._process_day(day, {"events": events})
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
    assert pred.predicted_total == 2 * LEAGUE_AVG_POINTS


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
    assert engine.feature_log == []
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
    expected = 1500.0 + 0.75 * (rating_before - 1500.0)
    assert abs(rating_after - expected) < 1e-9
    # Same-season day changes must not regress again.
    engine._process_day(date(2025, 8, 2), {"events": []})
    assert engine.elo.get("Boston Sharks") == rating_after


def test_feature_rows_are_strictly_pregame(tmp_path) -> None:
    """Leakage guard: game N's row reflects only games 1..N-1."""
    engine = make_engine(tmp_path)
    engine._process_day(
        date(2025, 11, 1), {"events": [event("Boston Sharks", "Utah Minnows", 120, 100)]}
    )
    first = engine.feature_log[0]
    # Nothing known before the very first game: pure sentinels everywhere.
    assert first[F["elo_diff"]] == 0.0
    for col in ("home_pf", "home_pa", "away_pf", "away_pa"):
        assert first[F[col]] == LEAGUE_AVG_POINTS
    assert first[F["net_rating_diff"]] == 0.0
    assert first[F["home_rest"]] == first[F["away_rest"]] == REST_CAP
    assert first[F["home_b2b"]] == first[F["away_b2b"]] == 0.0
    assert first[F["home_gp"]] == first[F["away_gp"]] == 0.0
    # Rematch next night: the row must contain exactly game 1, not game 2.
    engine._process_day(
        date(2025, 11, 2), {"events": [event("Boston Sharks", "Utah Minnows", 90, 130)]}
    )
    second = engine.feature_log[1]
    assert second[F["elo_diff"]] > 0.0  # Sharks won game 1
    assert second[F["home_pf"]] == 120.0  # not averaged with the 90
    assert second[F["home_pa"]] == 100.0
    assert second[F["away_pf"]] == 100.0
    assert second[F["away_pa"]] == 120.0
    assert second[F["net_rating_diff"]] == 40.0
    assert second[F["home_gp"]] == second[F["away_gp"]] == 1.0
    assert len(engine.feature_log) == len(engine.backtest)


def test_rest_days_and_back_to_back_flags(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine._process_day(date(2025, 11, 1), {"events": [event("A", "B", 110, 100)]})
    # Next night: both on a back-to-back; C debuts fully rested.
    engine._process_day(
        date(2025, 11, 2),
        {"events": [event("A", "C", 110, 100), event("B", "D", 110, 100)]},
    )
    row_ac = engine.feature_log[1]
    assert row_ac[F["home_rest"]] == 1.0 and row_ac[F["home_b2b"]] == 1.0
    assert row_ac[F["away_rest"]] == REST_CAP and row_ac[F["away_b2b"]] == 0.0
    # Three days later: A rests 3 (no b2b); a 30-day layoff caps at REST_CAP.
    engine._process_day(date(2025, 11, 5), {"events": [event("A", "B", 110, 100)]})
    row = engine.feature_log[3]
    assert row[F["home_rest"]] == 3.0 and row[F["home_b2b"]] == 0.0
    assert row[F["away_rest"]] == 3.0
    engine._process_day(date(2025, 12, 5), {"events": [event("A", "B", 110, 100)]})
    assert engine.feature_log[4][F["home_rest"]] == REST_CAP


def test_season_rollover_resets_rest_and_games_played(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine._process_day(date(2025, 6, 10), {"events": [event("A", "B", 110, 100)]})
    assert engine.games_played == {"A": 1, "B": 1}
    engine._process_day(date(2025, 10, 25), {"events": [event("A", "B", 110, 100)]})
    row = engine.feature_log[1]  # first game of the new season
    assert row[F["home_rest"]] == REST_CAP  # not 137 days, and not carried over
    assert row[F["home_gp"]] == row[F["away_gp"]] == 0.0
    assert engine.games_played == {"A": 1, "B": 1}


def test_training_calibration_and_shipping(tmp_path) -> None:
    engine = make_engine(tmp_path)
    rig_two_seasons(engine)
    bundle = engine.train_model()
    assert bundle is not None
    assert engine.model_path.exists()
    assert bundle["mode"] in ("elo", "xgb", "blend")
    assert bundle["feature_names"] == FEATURE_NAMES
    # All four report rows evaluated on the same untouched validation slice.
    val = bundle["val"]
    assert set(val) == {"elo", "xgb", "blend", "logreg"}
    n_val = val["elo"]["games"]
    assert n_val >= 50
    for row in val.values():
        assert row["games"] == n_val
        assert 0.0 <= row["accuracy"] <= 1.0
        assert np.isfinite(row["log_loss"])
    # Shipping is the log-loss winner among elo/xgb/blend.
    winner = min(("elo", "xgb", "blend"), key=lambda m: val[m]["log_loss"])
    assert bundle["mode"] == winner
    # Calibration bounds: model probabilities never touch 0/1.
    probs = engine._model_prob(np.asarray(engine.feature_log[-60:], dtype=float))
    assert np.all(probs >= PROB_FLOOR) and np.all(probs <= 1.0 - PROB_FLOOR)
    # The rigged data is Elo-friendly by construction; the trainer must at
    # least beat a coin flip with whatever it ships.
    assert val[winner]["log_loss"] < 0.6931
    # predict() honors the shipped mode end to end.
    pred = asyncio.run(engine.predict([game("Alpha", "Delta")]))[0]
    expected_name = "nba-elo-v1" if bundle["mode"] == "elo" else "nba-xgb-v2"
    assert pred.model == expected_name == engine.model_name
    assert pred.home_win_prob > 0.5  # strongest hosts weakest
    # Fresh bundle is not stale; an 8-day-old one is.
    assert not engine._model_stale()
    engine._model["trained_at"] = (
        datetime.now(timezone.utc) - timedelta(days=8)
    ).isoformat()
    assert engine._model_stale()


def test_deleted_pickle_is_stale_even_with_a_fresh_bundle_in_memory(tmp_path) -> None:
    """RETRAIN deletes the pickle, then calls refresh_data() on the SAME
    loaded engine — so staleness has to look at the disk, not just memory.
    Otherwise the force-retrain is a pure delete and the artifact stays gone
    until the backend restarts."""
    engine = make_engine(tmp_path)
    rig_two_seasons(engine)
    engine.train_model()
    assert engine.model_path.exists() and not engine._model_stale()

    engine.model_path.unlink()  # what refresh_sport(force_retrain=True) does

    assert engine._model_stale()
    asyncio.run(engine.refresh_data())
    assert engine.model_path.exists()  # rebuilt, not left deleted


def test_model_bundle_roundtrips_through_pickle(tmp_path) -> None:
    engine = make_engine(tmp_path)
    rig_two_seasons(engine)
    bundle = engine.train_model()
    engine._save_state()
    reloaded = make_engine(tmp_path)  # loads state.json + model.pkl from disk
    assert reloaded._model is not None
    assert reloaded._model["mode"] == bundle["mode"]
    assert reloaded.model_name == engine.model_name
    p1 = asyncio.run(engine.predict([game("Alpha", "Delta")]))[0]
    p2 = asyncio.run(reloaded.predict([game("Alpha", "Delta")]))[0]
    assert abs(p1.home_win_prob - p2.home_win_prob) < 1e-9


def test_missing_pickle_falls_back_to_elo(tmp_path) -> None:
    engine = make_engine(tmp_path)
    rig_history(engine)
    assert not engine.model_path.exists()
    assert engine.model_name == "nba-elo-v1"
    pred = asyncio.run(engine.predict([game("Boston Sharks", "Utah Minnows")]))[0]
    assert pred.model == "nba-elo-v1"
    assert pred.home_win_prob == engine.elo.expected("Boston Sharks", "Utah Minnows")


def test_train_model_refuses_thin_data(tmp_path) -> None:
    engine = make_engine(tmp_path)
    rig_history(engine, n_days=30)  # far below MIN_TRAINING_GAMES
    assert engine.train_model() is None
    assert not engine.model_path.exists()


def test_state_roundtrip(tmp_path) -> None:
    engine = make_engine(tmp_path)
    rig_history(engine, n_days=6)
    engine._save_state()
    reloaded = make_engine(tmp_path)
    assert reloaded.elo.ratings == engine.elo.ratings
    assert reloaded.last_processed == engine.last_processed
    assert reloaded.backtest == engine.backtest
    assert reloaded.feature_log == engine.feature_log
    assert reloaded.last_game == engine.last_game
    assert reloaded.games_played == engine.games_played
    assert {t: list(v) for t, v in reloaded.points_for.items()} == {
        t: list(v) for t, v in engine.points_for.items()
    }


def test_legacy_state_without_feature_log_forces_full_replay(tmp_path) -> None:
    engine = make_engine(tmp_path)
    rig_history(engine, n_days=6)
    engine._save_state()
    state = (tmp_path / "state.json").read_text(encoding="utf-8")
    # Simulate a v1 file: no version stamp, no feature log.
    import json

    legacy = json.loads(state)
    del legacy["version"]
    del legacy["features"]
    (tmp_path / "state.json").write_text(json.dumps(legacy), encoding="utf-8")
    reloaded = make_engine(tmp_path)
    assert reloaded.last_processed is None  # ignored -> replay from scratch
    assert reloaded.backtest == []
    assert reloaded.feature_log == []

"""Unit tests for the WNBA Elo engine. No network — history is fed as
synthetic ESPN scoreboard payloads through the engine's replay seam."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

from kryptscout.core.schemas import Game, Sport
from kryptscout.sports.wnba import (
    LEAGUE_AVG_POINTS,
    WnbaEngine,
    _in_season,
    _season_key,
)


def make_engine(tmp_path) -> WnbaEngine:
    return WnbaEngine(state_path=tmp_path / "state.json", raw_dir=tmp_path / "raw")


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


def rig_history(engine: WnbaEngine, n_days: int = 30) -> None:
    """Aces beat Sparks by 20 every night, venues alternating."""
    day = date(2025, 6, 1)
    for i in range(n_days):
        if i % 2 == 0:
            payload = {"events": [event("Las Vegas Aces", "Los Angeles Sparks", 95, 75)]}
        else:
            payload = {"events": [event("Los Angeles Sparks", "Las Vegas Aces", 75, 95)]}
        engine._process_day(day, payload)
        day += timedelta(days=1)


def game(home: str, away: str) -> Game:
    return Game(
        id="wnba-test-1",
        sport=Sport.WNBA,
        start_time=datetime(2026, 7, 10, tzinfo=timezone.utc),
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
    assert engine.elo.get("Las Vegas Aces") > engine.elo.get("Los Angeles Sparks") + 100


def test_predict_probabilities_and_totals(tmp_path) -> None:
    engine = make_engine(tmp_path)
    rig_history(engine)
    pred = asyncio.run(
        engine.predict([game("Las Vegas Aces", "Los Angeles Sparks")])
    )[0]
    assert pred.model == "wnba-elo-v1"
    assert pred.sport == Sport.WNBA
    assert 0.0 <= pred.home_win_prob <= 1.0
    assert abs(pred.home_win_prob + pred.away_win_prob - 1.0) < 1e-9
    assert pred.home_win_prob > 0.5  # rigged winner at home must be favored
    # Aces average 95 for / 75 against, Sparks the mirror image, so the raw
    # sides are 95 and 75; the Elo nudge moves points between sides but must
    # leave the total at 170.
    assert pred.predicted_total == 170.0
    assert abs(pred.predicted_home_score + pred.predicted_away_score - 170.0) < 0.11
    assert pred.predicted_home_score > pred.predicted_away_score


def test_predict_unknown_teams_falls_back_to_league_average(tmp_path) -> None:
    engine = make_engine(tmp_path)
    pred = asyncio.run(engine.predict([game("Ghost A", "Ghost B")]))[0]
    # Equal 1500 ratings: only the +80 home advantage separates the sides.
    assert 0.5 < pred.home_win_prob < 0.7
    # No scoring history: NO total. The league-average placeholder used to
    # ship as 164.0 and the board printed UNDER edges against it.
    assert pred.predicted_total is None
    assert pred.predicted_home_score is None and pred.predicted_away_score is None


def test_expansion_teams_start_at_1500_and_get_rated(tmp_path) -> None:
    engine = make_engine(tmp_path)
    for team in ("Golden State Valkyries", "Toronto Tempo", "Portland Fire"):
        assert engine.elo.get(team) == 1500.0  # unseen -> initial rating
    engine._process_day(
        date(2026, 5, 15),
        {
            "events": [
                event("Golden State Valkyries", "Toronto Tempo", 88, 80),
                event("Portland Fire", "Seattle Storm", 70, 90),
            ]
        },
    )
    for team in ("Golden State Valkyries", "Toronto Tempo", "Portland Fire"):
        assert team in engine.elo.ratings
    assert engine.elo.get("Golden State Valkyries") > 1500.0
    assert engine.elo.get("Toronto Tempo") < 1500.0
    assert engine.elo.get("Portland Fire") < 1500.0
    # Rolling scoring picked them up too.
    assert list(engine.points_for["Portland Fire"]) == [70.0]


def test_preseason_allstar_and_unfinished_games_are_skipped(tmp_path) -> None:
    engine = make_engine(tmp_path)
    unfinished = event("A", "B", 50, 40)
    unfinished["competitions"][0]["status"]["type"]["completed"] = False
    # ESPN files the All-Star game under the regular season (season.type 2)
    # but marks the competition type ALLSTAR.
    allstar = event("Team Collier", "Team Clark", 151, 131)
    allstar["competitions"][0]["type"] = {"id": "4", "abbreviation": "ALLSTAR"}
    payload = {
        "events": [event("A", "B", 90, 80, season_type=1), allstar, unfinished]
    }
    engine._process_day(date(2026, 7, 5), payload)
    assert engine.backtest == []
    assert engine.elo.ratings == {}
    assert engine.last_processed == date(2026, 7, 5)


def test_commissioners_cup_final_skipped_but_cup_pool_games_kept(tmp_path) -> None:
    engine = make_engine(tmp_path)
    cup_final = event("Minnesota Lynx", "New York Liberty", 94, 89)
    cup_final["notes"] = [{"headline": "Commissioner's Cup Championship"}]
    # Qualifying Cup games count in the standings and must be replayed.
    cup_pool = event("Indiana Fever", "Chicago Sky", 85, 79)
    cup_pool["notes"] = [{"headline": "Commissioner's Cup"}]
    engine._process_day(date(2026, 7, 1), {"events": [cup_final, cup_pool]})
    assert "Minnesota Lynx" not in engine.elo.ratings
    assert "New York Liberty" not in engine.elo.ratings
    assert engine.elo.get("Indiana Fever") > 1500.0
    assert len(engine.backtest) == 1


def test_season_rollover_regresses_ratings_at_year_boundary(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine._process_day(
        date(2025, 9, 10),
        {"events": [event("Las Vegas Aces", "Los Angeles Sparks", 101, 70)]},
    )
    rating_before = engine.elo.get("Las Vegas Aces")
    assert rating_before > 1500.0
    assert _season_key(date(2025, 9, 10)) != _season_key(date(2026, 5, 1))
    engine._process_day(date(2026, 5, 1), {"events": []})  # crosses Jan 1
    rating_after = engine.elo.get("Las Vegas Aces")
    expected = 1500.0 + 0.5 * (rating_before - 1500.0)  # ELO_CONFIG.season_carry
    assert abs(rating_after - expected) < 1e-9
    # Same-season day changes must not regress again.
    engine._process_day(date(2026, 5, 2), {"events": []})
    assert engine.elo.get("Las Vegas Aces") == rating_after


def test_in_season_window() -> None:
    assert not _in_season(date(2026, 4, 30))
    assert _in_season(date(2026, 5, 1))
    assert _in_season(date(2026, 10, 31))
    assert not _in_season(date(2026, 11, 1))
    assert not _in_season(date(2026, 1, 15))


def test_day_json_uses_cache_without_network(tmp_path) -> None:
    engine = make_engine(tmp_path)
    engine.raw_dir.mkdir(parents=True)
    payload = {"events": [event("Phoenix Mercury", "Dallas Wings", 84, 76)]}
    (engine.raw_dir / "20250601.json").write_text(json.dumps(payload), encoding="utf-8")
    # client=None proves the cache hit never touches the network.
    got = asyncio.run(engine._day_json(None, date(2025, 6, 1)))
    assert got == payload


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

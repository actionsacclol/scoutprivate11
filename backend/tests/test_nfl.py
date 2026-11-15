"""No-network unit tests for the NFL engine (nfl-elo-v2: Elo + QB/rest overlays)."""
from __future__ import annotations

import asyncio
from collections import deque
import math
from datetime import datetime, timezone

import pandas as pd
import pytest

from kryptscout.core.elo import EloBook
from kryptscout.core.calibrate import temper
from kryptscout.core.schemas import Game, Sport
from kryptscout.datasources.weather import GameWeather
from kryptscout.sports import nfl
from kryptscout.sports.nfl import (
    ABBREV_TO_NAME,
    CURRENT_ABBREVS,
    ELO_CONFIG,
    PRESEASON_DAMP,
    QB_ADJ_CAP,
    QB_PRIOR_POINTS,
    QB_SPAN,
    NflEngine,
    _compute_metrics,
    _qb_diff,
    _qb_value,
    _record_qb_start,
    _replay,
    _rest_diff,
    _upcoming_schedule,
    _weather_total_multiplier,
    build_engine,
)


def _frame(rows: list[dict]) -> pd.DataFrame:
    """Synthetic games.csv slice with the columns _replay consumes."""
    defaults = {
        "gameday": "2024-09-08",
        "home_score": None,
        "away_score": None,
        "home_moneyline": None,
        "away_moneyline": None,
        "home_rest": 7,
        "away_rest": 7,
        "home_qb_id": None,
        "away_qb_id": None,
        "home_qb_name": None,
        "away_qb_name": None,
        "location": "Home",
        "week": 1,
        "game_type": "REG",
        "div_game": 0,
        "roof": "outdoors",
        "surface": "grass",
        "temp": None,
        "wind": None,
    }
    return pd.DataFrame([{**defaults, **row} for row in rows])


def _game(
    home: str = "Kansas City Chiefs",
    away: str = "Denver Broncos",
    when: datetime | None = None,
    gid: str = "nfl-test-1",
    neutral: bool = False,
) -> Game:
    return Game(
        id=gid,
        sport=Sport.NFL,
        start_time=when or datetime(2026, 9, 13, 17, tzinfo=timezone.utc),
        home=home,
        away=away,
        neutral_site=neutral,
    )


def _pt(diff: float) -> float:
    """The served probability: Elo expectation through the calibration temperature."""
    return temper(_p(diff), nfl.CALIBRATION_TEMPERATURE)


def _p(diff: float) -> float:
    """Elo logistic: P(home) for a total pre-game rating difference."""
    return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))


def _wx(wind_kph: float, precip: float = 0.0) -> GameWeather:
    return GameWeather(
        temp_c=10.0,
        temp_f=50.0,
        wind_kph=wind_kph,
        wind_dir_deg=180.0,
        precip_prob_pct=precip,
        summary="test",
    )


def _engine(tmp_path) -> NflEngine:
    """Engine with isolated (absent) state."""
    return NflEngine(state_path=tmp_path / "nfl_state.json")


@pytest.fixture
def no_weather(monkeypatch):
    """predict() never touches the network for weather in these tests."""

    async def none_weather(sport, team, start):
        return None

    monkeypatch.setattr(nfl.weather, "game_weather", none_weather)


# -- team naming ------------------------------------------------------------


def test_all_32_current_teams_mapped():
    assert len(CURRENT_ABBREVS) == 32
    names = {ABBREV_TO_NAME[a] for a in CURRENT_ABBREVS}
    assert len(names) == 32  # every current abbrev maps, to 32 distinct teams


def test_franchise_continuity_across_relocations():
    # Historical codes must resolve to the same Elo identity as today's code.
    assert ABBREV_TO_NAME["OAK"] == ABBREV_TO_NAME["LV"] == "Las Vegas Raiders"
    assert ABBREV_TO_NAME["SD"] == ABBREV_TO_NAME["LAC"] == "Los Angeles Chargers"
    assert ABBREV_TO_NAME["STL"] == ABBREV_TO_NAME["LA"] == "Los Angeles Rams"
    assert ABBREV_TO_NAME["WAS"] == "Washington Commanders"


# -- QB rating unit math ------------------------------------------------------


def test_qb_ewma_shrinkage_and_debut_zero():
    qbs: dict = {}
    # Unknown / debut QBs carry zero value, so the adjustment is exactly 0.
    assert _qb_value(qbs, None) == 0.0
    assert _qb_value(qbs, "00-001") == 0.0
    assert _qb_diff(qbs, "00-001", "00-002") == 0.0

    # One 40-point start moves the EWMA only alpha of the way off the prior.
    _record_qb_start(qbs, "00-001", "Test QB", 40)
    alpha = 2.0 / (QB_SPAN + 1.0)
    expected = QB_PRIOR_POINTS + alpha * (40 - QB_PRIOR_POINTS)
    assert qbs["00-001"]["ewma"] == pytest.approx(expected)
    assert qbs["00-001"]["starts"] == 1
    assert _qb_value(qbs, "00-001") == pytest.approx(expected - QB_PRIOR_POINTS)


def test_qb_diff_clamped_at_cap():
    qbs = {
        "hot": {"name": "Hot", "ewma": QB_PRIOR_POINTS + 50, "starts": 99},
        "cold": {"name": "Cold", "ewma": QB_PRIOR_POINTS - 50, "starts": 99},
    }
    assert _qb_diff(qbs, "hot", "cold", k_qb=4.0) == QB_ADJ_CAP
    assert _qb_diff(qbs, "cold", "hot", k_qb=4.0) == -QB_ADJ_CAP


# -- rest differential --------------------------------------------------------


def test_rest_diff_math():
    assert _rest_diff(14, 7, per_day=1.2) == pytest.approx(8.4)  # off a bye
    assert _rest_diff(4, 7, per_day=1.2) == pytest.approx(-3.6)  # short week
    assert _rest_diff(7, 7, per_day=1.2) == 0.0
    assert _rest_diff(None, 7) == 0.0
    assert _rest_diff(float("nan"), 7) == 0.0


# -- replay -----------------------------------------------------------------


def test_replay_moves_ratings_and_tracks_rolling_points():
    df = _frame(
        [
            {"season": 2024, "gameday": "2024-09-08", "home_team": "KC",
             "away_team": "DEN", "home_score": 30, "away_score": 10},
            {"season": 2024, "gameday": "2024-09-15", "home_team": "DEN",
             "away_team": "KC", "home_score": 13, "away_score": 27},
        ]
    )
    book, rolling, qbs, team_qb, feats, metrics = _replay(df)
    kc, den = ABBREV_TO_NAME["KC"], ABBREV_TO_NAME["DEN"]
    assert book.get(kc) > ELO_CONFIG.initial > book.get(den)
    assert list(rolling[kc]) == [(30, 10), (27, 13)]
    assert list(rolling[den]) == [(10, 30), (13, 27)]
    assert len(feats) == 2  # one pre-game record per played game
    assert metrics["n_games"] == 2  # 2024+ games feed validation
    assert qbs == {} and team_qb == {}  # no QB ids in the frame


def test_replay_skips_unplayed_and_pre_2006_games():
    df = _frame(
        [
            {"season": 2005, "gameday": "2005-09-11", "home_team": "KC",
             "away_team": "DEN", "home_score": 30, "away_score": 10},
            {"season": 2024, "gameday": "2024-09-08", "home_team": "KC",
             "away_team": "DEN"},  # scheduled, no score
        ]
    )
    book, rolling, qbs, team_qb, feats, metrics = _replay(df)
    assert book.ratings == {}
    assert rolling == {}
    assert feats == []
    assert metrics == {}


def test_replay_applies_season_carry():
    df = _frame(
        [
            {"season": 2020, "gameday": "2020-09-13", "home_team": "KC",
             "away_team": "DEN", "home_score": 30, "away_score": 10},
            {"season": 2021, "gameday": "2021-09-12", "home_team": "GB",
             "away_team": "CHI", "home_score": 24, "away_score": 14},
        ]
    )
    book, *_ = _replay(df)

    # Replicate season one with the shared EloBook, then regress by hand.
    ref = EloBook(config=ELO_CONFIG)
    ref.update(ABBREV_TO_NAME["KC"], ABBREV_TO_NAME["DEN"], True, margin=20)
    expected_kc = ELO_CONFIG.initial + ELO_CONFIG.season_carry * (
        ref.get(ABBREV_TO_NAME["KC"]) - ELO_CONFIG.initial
    )
    assert book.get(ABBREV_TO_NAME["KC"]) == pytest.approx(expected_kc)


def test_replay_franchise_keeps_rating_across_relocation():
    df = _frame(
        [
            {"season": 2015, "gameday": "2015-09-13", "home_team": "SD",
             "away_team": "DET", "home_score": 33, "away_score": 28},
            {"season": 2015, "gameday": "2015-09-20", "home_team": "SD",
             "away_team": "OAK", "home_score": 30, "away_score": 20},
        ]
    )
    book, rolling, *_ = _replay(df)
    chargers = ABBREV_TO_NAME["LAC"]
    assert book.get(chargers) > ELO_CONFIG.initial
    assert len(rolling[chargers]) == 2
    assert "SD" not in book.ratings  # keyed by franchise name, never abbrev


def test_replay_tracks_qb_ewma_and_last_starter():
    df = _frame(
        [
            {"season": 2023, "gameday": "2023-09-10", "home_team": "TB",
             "away_team": "CAR", "home_score": 40, "away_score": 10,
             "home_qb_id": "vet", "home_qb_name": "Vet Starter",
             "away_qb_id": "c1", "away_qb_name": "Carolina One"},
            {"season": 2023, "gameday": "2023-09-17", "home_team": "TB",
             "away_team": "CAR", "home_score": 40, "away_score": 10,
             "home_qb_id": "vet", "home_qb_name": "Vet Starter",
             "away_qb_id": "c2", "away_qb_name": "Carolina Two"},
        ]
    )
    _, _, qbs, team_qb, _, _ = _replay(df)
    assert qbs["vet"]["starts"] == 2
    alpha = 2.0 / (QB_SPAN + 1.0)
    e1 = QB_PRIOR_POINTS + alpha * (40 - QB_PRIOR_POINTS)
    assert qbs["vet"]["ewma"] == pytest.approx(e1 + alpha * (40 - e1))
    assert team_qb[ABBREV_TO_NAME["TB"]] == "vet"
    assert team_qb[ABBREV_TO_NAME["CAR"]] == "c2"  # most recent starter wins


def test_replay_walk_forward_qb_adjustment_helps_when_vet_beats_debut():
    """A vet with a hot prior EWMA vs a debut backup shifts p the right way."""
    history = [
        {"season": 2023, "gameday": f"2023-09-{10 + 7 * i:02d}", "home_team": "TB",
         "away_team": "CAR", "home_score": 40, "away_score": 10,
         "home_qb_id": "vet", "away_qb_id": f"c{i}"}
        for i in range(3)
    ]
    target = {  # validation game: vet vs a never-seen backup, vet's team wins
        "season": 2024, "gameday": "2024-09-08", "home_team": "TB",
        "away_team": "CAR", "home_score": 27, "away_score": 13,
        "home_qb_id": "vet", "away_qb_id": "debut",
    }
    df = _frame(history + [target])
    *_, m_qb = _replay(df, k_qb=4.0, rest_per_day=0.0)
    *_, m_flat = _replay(df, k_qb=0.0, rest_per_day=0.0)
    assert m_qb["n_games"] == m_flat["n_games"] == 1
    # Home (vet) won, so the QB boost must lower the log loss.
    assert m_qb["elo_log_loss"] < m_flat["elo_log_loss"]


def test_replay_debut_qbs_produce_zero_adjustment():
    df = _frame(
        [
            {"season": 2024, "gameday": "2024-09-08", "home_team": "KC",
             "away_team": "DEN", "home_score": 30, "away_score": 10,
             "home_qb_id": "fresh1", "away_qb_id": "fresh2"},
        ]
    )
    *_, m_qb = _replay(df, k_qb=8.0, rest_per_day=0.0)
    *_, m_flat = _replay(df, k_qb=0.0, rest_per_day=0.0)
    assert m_qb["elo_log_loss"] == pytest.approx(m_flat["elo_log_loss"])


def test_replay_rest_differential_shifts_probability():
    df = _frame(
        [
            {"season": 2024, "gameday": "2024-09-08", "home_team": "KC",
             "away_team": "DEN", "home_score": 30, "away_score": 10,
             "home_rest": 14, "away_rest": 7},
        ]
    )
    *_, m = _replay(df, k_qb=0.0, rest_per_day=1.2)
    # Single validated home win: log loss is -log(p); fresh book, so p is
    # exactly the logistic of home advantage plus the rest differential.
    expected_p = _p(ELO_CONFIG.home_advantage + 1.2 * 7)
    assert m["elo_log_loss"] == pytest.approx(-math.log(expected_p))


# -- v3 feature rows ----------------------------------------------------------


def test_replay_records_are_pregame_state():
    df = _frame(
        [
            {"season": 2023, "gameday": "2023-09-10", "home_team": "KC",
             "away_team": "DEN", "home_score": 30, "away_score": 10,
             "home_moneyline": -180, "away_moneyline": 150},
            {"season": 2023, "gameday": "2023-09-17", "home_team": "DEN",
             "away_team": "KC", "home_score": 13, "away_score": 27},
        ]
    )
    *_, records, _ = _replay(df, k_qb=0.0, rest_per_day=0.0)
    assert len(records) == 2
    first, second = records

    # Game 1: fresh book, no history — only home advantage separates them.
    assert first["season"] == 2023
    assert first["elo_diff"] == pytest.approx(ELO_CONFIG.home_advantage)
    assert first["elo_diff_raw"] == 0.0
    assert first["qb_adj_diff"] == 0.0 and first["rest_diff"] == 0.0
    assert first["p"] == pytest.approx(_p(ELO_CONFIG.home_advantage))
    assert first["home_won"] is True and first["tie"] is False
    assert (first["home_ml"], first["away_ml"]) == (-180, 150)

    # Game 2: reflects exactly one prior game, read before its own update.
    assert second["elo_diff_raw"] < 0.0  # DEN (loser) now rated below KC
    assert second["home_ml"] is None and second["away_ml"] is None


def test_replay_record_difference_is_the_sum_of_its_parts():
    """elo_diff is what the probability is read from; its parts must add up."""
    df = _frame(
        [
            {"season": 2023, "gameday": "2023-09-10", "home_team": "KC",
             "away_team": "DEN", "home_score": 30, "away_score": 10,
             "home_qb_id": "vet", "away_qb_id": "rook"},
            {"season": 2023, "gameday": "2023-09-17", "home_team": "KC",
             "away_team": "DEN", "home_score": 24, "away_score": 20,
             "home_qb_id": "vet", "away_qb_id": "rook",
             "home_rest": 10, "away_rest": 6},
        ]
    )
    *_, records, _ = _replay(df)
    second = records[1]
    assert second["qb_adj_diff"] > 0.0  # the vet outscored the rookie in game 1
    assert second["rest_diff"] == pytest.approx(nfl.REST_ELO_PER_DAY * 4)
    assert second["elo_diff"] == pytest.approx(
        second["elo_diff_raw"]
        + ELO_CONFIG.home_advantage
        + second["qb_adj_diff"]
        + second["rest_diff"]
    )
    assert second["p"] == pytest.approx(_p(second["elo_diff"]))


def test_replay_records_immune_to_appended_future_games():
    """Leakage guard: a game's record must not change when later games appear."""
    base = [
        {"season": 2022, "gameday": f"2022-09-{11 + 7 * i:02d}", "home_team": h,
         "away_team": a, "home_score": s1, "away_score": s2,
         "home_qb_id": f"q{h}", "away_qb_id": f"q{a}"}
        for i, (h, a, s1, s2) in enumerate(
            [("KC", "DEN", 30, 10), ("DEN", "LV", 21, 20), ("LV", "KC", 13, 34)]
        )
    ]
    future = [
        {"season": 2023, "gameday": "2023-09-10", "home_team": "KC",
         "away_team": "LV", "home_score": 40, "away_score": 3,
         "home_qb_id": "qKC", "away_qb_id": "qLV"},
        {"season": 2023, "gameday": "2023-09-17", "home_team": "DEN",
         "away_team": "KC", "home_score": 24, "away_score": 20,
         "home_qb_id": "qDEN", "away_qb_id": "qKC"},
    ]
    *_, feats_short, _ = _replay(_frame(base))
    *_, feats_long, _ = _replay(_frame(base + future))
    assert len(feats_long) == len(feats_short) + 2
    assert feats_long[: len(feats_short)] == feats_short


def test_replay_neutral_site_zeroes_home_advantage():
    df = _frame(
        [
            {"season": 2024, "gameday": "2024-09-08", "home_team": "KC",
             "away_team": "DEN", "home_score": 30, "away_score": 10,
             "location": "Neutral"},
        ]
    )
    *_, feats, m = _replay(df, k_qb=0.0, rest_per_day=0.0)
    assert feats[0]["elo_diff"] == 0.0  # fresh book, equal rest, no HFA
    assert feats[0]["p"] == pytest.approx(0.5)
    assert m["elo_log_loss"] == pytest.approx(-math.log(0.5))


# -- upcoming schedule --------------------------------------------------------


def test_upcoming_schedule_maps_unplayed_games_with_feature_context():
    df = _frame(
        [
            {"season": 2026, "gameday": "2026-09-10", "home_team": "KC",
             "away_team": "DEN", "home_rest": 7, "away_rest": 6, "week": 1,
             "div_game": 1, "roof": "outdoors", "surface": "grass",
             "temp": None, "wind": None},  # unplayed
            {"season": 2024, "gameday": "2024-09-08", "home_team": "KC",
             "away_team": "DEN", "home_score": 30, "away_score": 10},  # played
        ]
    )
    schedule = _upcoming_schedule(df)
    assert list(schedule) == ["2026-09-10|Kansas City Chiefs|Denver Broncos"]
    entry = schedule["2026-09-10|Kansas City Chiefs|Denver Broncos"]
    assert entry == {
        "home_rest": 7, "away_rest": 6, "season": 2026, "week": 1,
        "game_type": "REG", "div_game": 1, "roof": "outdoors",
        "surface": "grass", "temp": None, "wind": None, "neutral": False,
        "home_qb_id": None, "away_qb_id": None,
    }


def test_the_listed_starter_is_priced_not_the_last_one(tmp_path):
    """games.csv names the expected starter a week or two ahead; a benched or
    injured quarterback must not keep pricing his team's next game."""
    engine = _engine(tmp_path)
    engine.qbs = {
        "star": {"name": "Star", "ewma": nfl.QB_PRIOR_POINTS + 6.0, "starts": 30},
        "backup": {"name": "Backup", "ewma": nfl.QB_PRIOR_POINTS, "starts": 0},
    }
    engine.team_qb = {"Kansas City Chiefs": "star"}
    game = _game()

    last_starter, _ = engine._qb_rest_adjustments(game, None)
    assert last_starter == pytest.approx(nfl.QB_K * 6.0)

    listed = {"home_qb_id": "backup", "away_qb_id": None, "home_rest": 7, "away_rest": 7}
    with_backup, _ = engine._qb_rest_adjustments(game, listed)
    assert with_backup == pytest.approx(0.0)

    # Nothing listed yet (games further out): fall back to the last starter.
    unlisted = {"home_qb_id": None, "away_qb_id": None, "home_rest": 7, "away_rest": 7}
    fallback, _ = engine._qb_rest_adjustments(game, unlisted)
    assert fallback == pytest.approx(last_starter)


def test_upcoming_schedule_flags_neutral_sites():
    df = _frame(
        [
            {"season": 2026, "gameday": "2026-09-10", "home_team": "KC",
             "away_team": "DEN", "location": "Neutral"},
        ]
    )
    [entry] = _upcoming_schedule(df).values()
    assert entry["neutral"] is True


# -- metrics ----------------------------------------------------------------


def test_metrics_perfect_predictor_and_market_comparison():
    records = [
        {"p": 0.9, "home_won": True, "tie": False, "home_ml": -200, "away_ml": 170},
        {"p": 0.2, "home_won": False, "tie": False, "home_ml": 150, "away_ml": -180},
        {"p": 0.7, "home_won": True, "tie": False, "home_ml": None, "away_ml": None},
        {"p": 0.5, "home_won": False, "tie": True, "home_ml": -110, "away_ml": -110},
    ]
    m = _compute_metrics(records)
    assert m["n_games"] == 3
    assert m["n_ties_excluded"] == 1
    assert m["elo_accuracy"] == 1.0
    assert m["elo_log_loss"] < 0.45
    # Market slice: only the two games with both prices; favorite won both.
    assert m["n_games_with_moneyline"] == 2
    assert m["market_accuracy"] == 1.0
    assert m["elo_accuracy_vs_market"] == 1.0
    assert 0.0 < m["market_log_loss"] < 1.0


def test_metrics_empty_records():
    assert _compute_metrics([]) == {}


# -- prediction -------------------------------------------------------------


def test_predict_probabilities_and_scores(tmp_path, no_weather):
    engine = _engine(tmp_path)
    engine.book.ratings = {"Kansas City Chiefs": 1700.0, "Denver Broncos": 1400.0}
    engine.rolling = {
        "Kansas City Chiefs": nfl.deque([(30, 20), (28, 22)], maxlen=10),
        "Denver Broncos": nfl.deque([(17, 27), (21, 31)], maxlen=10),
    }
    [pred] = asyncio.run(engine.predict([_game()]))

    assert pred.model == NflEngine.model_name
    assert pred.home_win_prob + pred.away_win_prob == pytest.approx(1.0)
    assert pred.home_win_prob > 0.8  # 300-point Elo gap plus home field
    # home = (KC PF 29 + DEN PA 29)/2 + 1.25; away = (DEN PF 19 + KC PA 21)/2 - 1.25
    assert pred.predicted_home_score == pytest.approx(30.25, abs=0.06)  # rounded to .1
    assert pred.predicted_away_score == pytest.approx(18.75, abs=0.06)
    assert pred.predicted_total == pytest.approx(49.0)
    assert pred.confidence == pytest.approx(abs(pred.home_win_prob - 0.5) * 2)


def test_predict_unknown_team_falls_back_to_league_average(tmp_path, no_weather):
    engine = _engine(tmp_path)
    [pred] = asyncio.run(engine.predict([_game(gid="nfl-test-2")]))
    assert pred.home_win_prob == pytest.approx(_pt(ELO_CONFIG.home_advantage))
    assert pred.predicted_total is None  # no scoring history: no placeholder total (2026-09-02)


def test_predict_applies_qb_and_rest_adjustments(tmp_path, no_weather):
    engine = _engine(tmp_path)
    engine.qbs = {"hot": {"name": "Hot QB", "ewma": QB_PRIOR_POINTS + 5, "starts": 30}}
    engine.team_qb = {"Kansas City Chiefs": "hot", "Denver Broncos": "unknown"}
    when = datetime(2026, 9, 13, 17, tzinfo=timezone.utc)
    engine.schedule = {
        "2026-09-13|Kansas City Chiefs|Denver Broncos": {
            "home_rest": 14,
            "away_rest": 7,
        }
    }
    [pred] = asyncio.run(engine.predict([_game(when=when)]))
    expected = _pt(
        ELO_CONFIG.home_advantage + nfl.QB_K * 5 + nfl.REST_ELO_PER_DAY * 7
    )
    assert pred.home_win_prob == pytest.approx(expected)


def test_predict_scheduled_game_is_elo_through_the_temperature(tmp_path, no_weather):
    """A regular-season game is priced by Elo and nothing else."""
    engine = _engine(tmp_path)
    engine.book.ratings = {"Kansas City Chiefs": 1600.0, "Denver Broncos": 1500.0}
    when = datetime(2026, 9, 13, 17, tzinfo=timezone.utc)
    engine.schedule = {
        "2026-09-13|Kansas City Chiefs|Denver Broncos": {"home_rest": 7, "away_rest": 7}
    }
    [pred] = asyncio.run(engine.predict([_game(when=when)]))
    assert pred.home_win_prob == pytest.approx(_pt(100 + ELO_CONFIG.home_advantage))


def test_predict_neutral_site_zeroes_home_advantage(tmp_path, no_weather):
    engine = _engine(tmp_path)  # equal (default) ratings, no schedule entry
    [neutral_pred] = asyncio.run(engine.predict([_game(neutral=True)]))
    assert neutral_pred.home_win_prob == pytest.approx(0.5)
    [home_pred] = asyncio.run(engine.predict([_game()]))
    assert home_pred.home_win_prob == pytest.approx(_pt(ELO_CONFIG.home_advantage))


def test_predict_schedule_entry_can_flag_neutral(tmp_path, no_weather):
    """games.csv knows about neutral sites even when ESPN's flag is missing."""
    engine = _engine(tmp_path)
    when = datetime(2026, 9, 13, 17, tzinfo=timezone.utc)
    engine.schedule = {
        "2026-09-13|Kansas City Chiefs|Denver Broncos": {
            "home_rest": 7, "away_rest": 7, "neutral": True,
        }
    }
    [pred] = asyncio.run(engine.predict([_game(when=when)]))
    assert pred.home_win_prob == pytest.approx(0.5)


# -- preseason damping --------------------------------------------------------


def test_preseason_damping_formula(tmp_path, no_weather):
    engine = _engine(tmp_path)
    engine.book.ratings = {"Kansas City Chiefs": 1700.0, "Denver Broncos": 1400.0}
    # August game, absent from the games.csv schedule -> preseason.
    when = datetime(2026, 8, 15, 23, tzinfo=timezone.utc)
    [pred] = asyncio.run(engine.predict([_game(when=when)]))
    p_raw = _p(300 + ELO_CONFIG.home_advantage)
    expected = 0.5 + PRESEASON_DAMP * (p_raw - 0.5)
    assert pred.home_win_prob == pytest.approx(expected)
    # Confidence is derived from the damped probability -> 60% lower too.
    assert pred.confidence == pytest.approx(abs(expected - 0.5) * 2)


def test_preseason_damping_skipped_on_schedule_or_out_of_window(tmp_path, no_weather):
    engine = _engine(tmp_path)
    engine.book.ratings = {"Kansas City Chiefs": 1700.0, "Denver Broncos": 1400.0}
    p_raw = _pt(300 + ELO_CONFIG.home_advantage)  # served: Elo through the calibration scale

    # An August date that games.csv knows about is NOT preseason.
    when = datetime(2026, 8, 15, 23, tzinfo=timezone.utc)
    engine.schedule = {
        "2026-08-15|Kansas City Chiefs|Denver Broncos": {"home_rest": 7, "away_rest": 7}
    }
    [pred] = asyncio.run(engine.predict([_game(when=when)]))
    assert pred.home_win_prob == pytest.approx(p_raw)

    # A January playoff game missing from the schedule is NOT preseason either.
    engine.schedule = {}
    when = datetime(2027, 1, 17, 23, tzinfo=timezone.utc)
    [pred] = asyncio.run(engine.predict([_game(when=when)]))
    assert pred.home_win_prob == pytest.approx(p_raw)


# -- weather totals -----------------------------------------------------------


def test_weather_multiplier_thresholds_and_cap():
    assert _weather_total_multiplier(None) == 1.0  # dome / unknown / far out
    assert _weather_total_multiplier(_wx(10.0)) == 1.0
    assert _weather_total_multiplier(_wx(30.0)) == pytest.approx(0.95)
    assert _weather_total_multiplier(_wx(45.0)) == pytest.approx(0.90)
    assert _weather_total_multiplier(_wx(10.0, precip=80.0)) == pytest.approx(0.97)
    assert _weather_total_multiplier(_wx(30.0, precip=80.0)) == pytest.approx(0.9215)
    # Worst case is capped at heavy wind x precip.
    assert _weather_total_multiplier(_wx(60.0, precip=100.0)) == pytest.approx(0.873)


def test_predict_scales_totals_for_weather(tmp_path, monkeypatch):
    async def windy(sport, team, start):
        return _wx(30.0)

    monkeypatch.setattr(nfl.weather, "game_weather", windy)
    engine = _engine(tmp_path)
    game = _game()
    for team in (game.home, game.away):  # scoring history at the league average
        engine.rolling[team] = deque([(22.0, 22.0)])
    [pred] = asyncio.run(engine.predict([game]))
    # League-average baseline is 23.25 / 20.75 (total 44); x0.95 for the wind.
    assert pred.predicted_home_score == pytest.approx(23.25 * 0.95, abs=0.06)
    assert pred.predicted_away_score == pytest.approx(20.75 * 0.95, abs=0.06)
    assert pred.predicted_total == pytest.approx(44.0 * 0.95, abs=0.06)
    # Win probability never sees weather.
    assert pred.home_win_prob == pytest.approx(
        _pt(ELO_CONFIG.home_advantage)
    )


def test_predict_degrades_silently_on_weather_failure(tmp_path, monkeypatch):
    async def broken(sport, team, start):
        raise RuntimeError("open-meteo down")

    monkeypatch.setattr(nfl.weather, "game_weather", broken)
    engine = _engine(tmp_path)
    [pred] = asyncio.run(engine.predict([_game()]))
    assert pred.predicted_total is None  # no scoring history: no placeholder total (2026-09-02)


# -- state round-trip ---------------------------------------------------------


def test_state_persists_and_reloads(tmp_path):
    path = tmp_path / "nfl_state.json"
    engine = _engine(tmp_path)
    engine.book.ratings = {"Buffalo Bills": 1650.5}
    engine.rolling = {"Buffalo Bills": nfl.deque([(31, 10)], maxlen=10)}
    engine.qbs = {"00-1": {"name": "Josh Allen", "ewma": 28.4, "starts": 120}}
    engine.team_qb = {"Buffalo Bills": "00-1"}
    engine.schedule = {"2026-09-10|Buffalo Bills|New York Jets": {
        "home_rest": 7, "away_rest": 6, "season": 2026, "week": 1,
        "game_type": "REG", "div_game": 1, "roof": "outdoors",
        "surface": "grass", "temp": None, "wind": None, "neutral": False}}
    engine.metrics = {"elo_accuracy": 0.64}
    engine._save_state()

    reloaded = NflEngine(state_path=path)
    assert reloaded.book.get("Buffalo Bills") == pytest.approx(1650.5)
    assert list(reloaded.rolling["Buffalo Bills"]) == [(31, 10)]
    assert reloaded.qbs["00-1"]["ewma"] == pytest.approx(28.4)
    assert reloaded.qbs["00-1"]["starts"] == 120
    assert reloaded.team_qb == {"Buffalo Bills": "00-1"}
    assert reloaded.schedule["2026-09-10|Buffalo Bills|New York Jets"] == {
        "home_rest": 7, "away_rest": 6, "season": 2026, "week": 1,
        "game_type": "REG", "div_game": 1, "roof": "outdoors",
        "surface": "grass", "temp": None, "wind": None, "neutral": False}
    assert reloaded.metrics["elo_accuracy"] == 0.64


def test_build_engine_contract():
    engine = build_engine()
    assert engine.sport is Sport.NFL
    assert engine.model_name == "nfl-elo-v2"

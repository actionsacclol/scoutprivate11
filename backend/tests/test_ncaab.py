"""No-network unit tests for the NCAAB Elo engine."""
from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from kryptscout.core.elo import EloBook
from kryptscout.core.schemas import Game, Sport
from kryptscout.sports import ncaab
from kryptscout.sports.ncaab import (
    ELO_CONFIG,
    NON_D1_RATING,
    PREDICT_ALIASES,
    NcaabEngine,
    _canonical_names,
    _compute_metrics,
    _current_season,
    _replay,
    _seasons,
    build_engine,
)

# Stable ESPN-style team ids for the synthetic frames.
DUKE, UNC, KANSAS, GUEST = 150, 153, 2305, 9001


def _frame(rows: list[dict]) -> pd.DataFrame:
    """Synthetic hoopR schedule slice with the columns _replay consumes."""
    defaults = {
        "season": 2026,
        "season_type": 2,
        "game_date": "2025-11-10",
        "home_id": DUKE,
        "away_id": UNC,
        "home_display_name": "Duke Blue Devils",
        "away_display_name": "North Carolina Tar Heels",
        "home_score": None,
        "away_score": None,
        "status_type_completed": True,
        "neutral_site": False,
    }
    return pd.DataFrame([{**defaults, **row} for row in rows])


def _hfa_prob(diff_extra: float = 0.0) -> float:
    """P(home) between equally rated teams, plus an extra rating gap."""
    return 1.0 / (1.0 + 10.0 ** (-(ELO_CONFIG.home_advantage + diff_extra) / 400.0))


# -- season bookkeeping -------------------------------------------------------


def test_current_season_uses_hoopr_ending_year():
    assert _current_season(date(2026, 7, 11)) == 2026  # off-season -> 2025-26
    assert _current_season(date(2026, 11, 15)) == 2027  # new season underway
    assert _seasons(date(2026, 7, 11)) == list(range(2003, 2027))
    assert _seasons(date(2026, 11, 15))[-1] == 2027


# -- replay -------------------------------------------------------------------


def test_replay_moves_ratings_and_tracks_rolling_points():
    df = _frame(
        [
            {"home_score": 80, "away_score": 70},
            {"game_date": "2025-11-15", "home_id": UNC, "away_id": DUKE,
             "home_display_name": "North Carolina Tar Heels",
             "away_display_name": "Duke Blue Devils",
             "home_score": 68, "away_score": 75},
        ]
    )
    book, rolling, metrics = _replay(df, min_d1_games=1)
    assert book.get("Duke Blue Devils") > ELO_CONFIG.initial
    assert book.get("North Carolina Tar Heels") < ELO_CONFIG.initial
    assert list(rolling["Duke Blue Devils"]) == [(80, 70), (75, 68)]
    assert list(rolling["North Carolina Tar Heels"]) == [(70, 80), (68, 75)]
    assert metrics["overall"]["games"] == 2
    assert metrics["2026"]["games"] == 2


def test_replay_skips_unplayed_and_non_competition_rows():
    df = _frame(
        [
            {"status_type_completed": False},  # scheduled/postponed
            {"season_type": 1, "home_score": 90, "away_score": 60},  # preseason
            {"home_score": 0, "away_score": 0},  # tied placeholder/forfeit
        ]
    )
    book, rolling, metrics = _replay(df, min_d1_games=1)
    assert book.ratings == {}
    assert rolling == {}
    assert metrics == {}


def test_replay_neutral_site_drops_home_advantage():
    """Pre-game prob at a neutral site is 0.5 between fresh teams."""
    home_court = _replay(_frame([{"home_score": 80, "away_score": 70}]),
                         min_d1_games=1)[2]
    neutral = _replay(_frame([{"home_score": 80, "away_score": 70,
                               "neutral_site": True}]), min_d1_games=1)[2]
    # Home won both: log loss reveals the logged pre-game probability.
    assert neutral["overall"]["log_loss"] == pytest.approx(math.log(2.0))
    assert home_court["overall"]["log_loss"] == pytest.approx(
        -math.log(_hfa_prob())
    )
    # Symmetric neutral update: winner's gain mirrors K * MOV * (1 - 0.5).
    book, _, _ = _replay(_frame([{"home_score": 80, "away_score": 70,
                                  "neutral_site": True}]), min_d1_games=1)
    ref = EloBook(config=replace(ELO_CONFIG, home_advantage=0.0))
    ref.update("h", "a", True, margin=10)
    assert book.get("Duke Blue Devils") == pytest.approx(ref.get("h"))
    assert ELO_CONFIG.home_advantage == 90.0  # shared config never mutated


def test_replay_pins_non_d1_guest_flat_and_keeps_it_out_of_the_book():
    df = _frame(
        [
            # Duke appears twice (D1 at threshold 2); the guest only once.
            {"home_score": 95, "away_score": 50, "away_id": GUEST,
             "away_display_name": "Quincy Hawks"},
            {"game_date": "2025-11-15", "home_score": 80, "away_score": 70},
            {"game_date": "2025-11-15", "home_id": UNC, "away_id": DUKE,
             "home_display_name": "North Carolina Tar Heels",
             "away_display_name": "Duke Blue Devils",
             "home_score": 60, "away_score": 62},
        ]
    )
    book, rolling, metrics = _replay(df, min_d1_games=2)
    assert "Quincy Hawks" not in book.ratings
    assert "Quincy Hawks" not in rolling
    # Duke's first update was computed against a flat-1200 opponent.
    ref = EloBook(config=replace(ELO_CONFIG))
    ref.ratings["guest"] = NON_D1_RATING
    ref.update("Duke Blue Devils", "guest", True, margin=45)
    duke_after_game1 = ref.get("Duke Blue Devils")
    assert duke_after_game1 > ELO_CONFIG.initial
    # Non-D1 games never enter the backtest: only the two D1-vs-D1 games do.
    assert metrics["overall"]["games"] == 2


def test_replay_applies_season_carry():
    df = _frame(
        [
            {"season": 2025, "game_date": "2024-11-10",
             "home_score": 80, "away_score": 70},
            {"season": 2026, "game_date": "2025-11-10",
             "home_id": KANSAS, "away_id": UNC,
             "home_display_name": "Kansas Jayhawks",
             "away_display_name": "North Carolina Tar Heels",
             "home_score": 75, "away_score": 60},
        ]
    )
    book, _, _ = _replay(df, min_d1_games=1)
    ref = EloBook(config=replace(ELO_CONFIG))
    ref.update("Duke Blue Devils", "North Carolina Tar Heels", True, margin=10)
    expected_duke = ELO_CONFIG.initial + ELO_CONFIG.season_carry * (
        ref.get("Duke Blue Devils") - ELO_CONFIG.initial
    )
    assert book.get("Duke Blue Devils") == pytest.approx(expected_duke)


# -- ESPN-name keying ---------------------------------------------------------


def test_canonical_names_prefer_the_most_recent_name_per_id():
    df = _frame(
        [
            {"season": 2024, "game_date": "2023-11-10", "home_id": 85,
             "home_display_name": "IUPUI Jaguars",
             "home_score": 70, "away_score": 60},
            {"season": 2025, "game_date": "2024-11-10", "home_id": 85,
             "home_display_name": "IU Indianapolis Jaguars",
             "home_score": 70, "away_score": 60},
        ]
    )
    assert _canonical_names(df)[85] == "IU Indianapolis Jaguars"


def test_replay_keeps_one_elo_identity_across_a_rebrand():
    df = _frame(
        [
            {"season": 2025, "game_date": "2024-11-10", "home_id": 85,
             "home_display_name": "IUPUI Jaguars",
             "home_score": 70, "away_score": 60},
            {"season": 2025, "game_date": "2024-11-15", "home_id": 85,
             "home_display_name": "IU Indianapolis Jaguars",
             "home_score": 72, "away_score": 61},
        ]
    )
    book, rolling, _ = _replay(df, min_d1_games=1)
    assert "IUPUI Jaguars" not in book.ratings  # keyed by the latest name only
    assert book.get("IU Indianapolis Jaguars") > ELO_CONFIG.initial
    assert len(rolling["IU Indianapolis Jaguars"]) == 2


# -- metrics ------------------------------------------------------------------


def test_metrics_per_season_and_overall():
    records = [
        {"season": 2025, "p": 0.9, "home_won": True},
        {"season": 2026, "p": 0.3, "home_won": True},
        {"season": 2026, "p": 0.8, "home_won": True},
    ]
    m = _compute_metrics(records)
    assert m["overall"]["games"] == 3
    assert m["overall"]["accuracy"] == pytest.approx(2 / 3)
    assert m["2025"] == {"games": 1, "accuracy": 1.0,
                         "log_loss": pytest.approx(-math.log(0.9))}
    assert m["2026"]["games"] == 2
    assert _compute_metrics([]) == {}


# -- prediction ---------------------------------------------------------------


def _game(home: str, away: str, neutral_site: bool = False) -> Game:
    return Game(
        id="ncaab-test-1",
        sport=Sport.NCAAB,
        start_time=datetime(2026, 11, 10, 23, tzinfo=timezone.utc),
        home=home,
        away=away,
        neutral_site=neutral_site,
    )


def test_predict_probabilities_and_totals(tmp_path):
    engine = NcaabEngine(state_path=tmp_path / "ncaab_state.json")
    engine.book.ratings = {"Duke Blue Devils": 1900.0, "Longwood Lancers": 1450.0}
    engine.rolling = {
        "Duke Blue Devils": ncaab.deque([(85, 65), (81, 63)], maxlen=10),
        "Longwood Lancers": ncaab.deque([(70, 78), (66, 80)], maxlen=10),
    }
    [pred] = asyncio.run(
        engine.predict([_game("Duke Blue Devils", "Longwood Lancers")])
    )
    assert pred.model == "ncaab-elo-v1"
    assert pred.home_win_prob + pred.away_win_prob == pytest.approx(1.0)
    assert pred.home_win_prob > 0.9  # 450-point Elo gap plus home court
    # home = (Duke PF 83 + LW PA 79)/2 = 81, away = (LW PF 68 + Duke PA 64)/2 = 66,
    # nudged by 0.5 * (450+90)/100 = 2.7 each way; total unchanged.
    assert pred.predicted_home_score == pytest.approx(83.7)
    assert pred.predicted_away_score == pytest.approx(63.3)
    assert pred.predicted_total == pytest.approx(147.0)
    assert pred.confidence == pytest.approx(abs(pred.home_win_prob - 0.5) * 2)


def test_predict_neutral_site_drops_home_advantage(tmp_path):
    engine = NcaabEngine(state_path=tmp_path / "ncaab_state.json")
    engine.book.ratings = {"Duke Blue Devils": 1800.0, "Kansas Jayhawks": 1700.0}

    [neutral] = asyncio.run(
        engine.predict([_game("Duke Blue Devils", "Kansas Jayhawks",
                              neutral_site=True)])
    )
    [hosted] = asyncio.run(
        engine.predict([_game("Duke Blue Devils", "Kansas Jayhawks")])
    )

    # Neutral: pure 100-point rating gap. Hosted (regression): +90 HFA on top.
    no_hfa_prob = 1.0 / (1.0 + 10.0 ** (-100.0 / 400.0))
    assert neutral.home_win_prob == pytest.approx(no_hfa_prob)
    assert hosted.home_win_prob == pytest.approx(_hfa_prob(100.0))
    assert neutral.home_win_prob < hosted.home_win_prob
    assert neutral.home_win_prob + neutral.away_win_prob == pytest.approx(1.0)
    # The score nudge also loses the HFA term on a neutral court, but the
    # nudge stays equal-and-opposite so the predicted total is unchanged.
    assert neutral.predicted_home_score < hosted.predicted_home_score
    assert neutral.predicted_away_score > hosted.predicted_away_score
    assert neutral.predicted_total == pytest.approx(hosted.predicted_total)


def test_predict_between_equals_at_neutral_site_is_a_coin_flip(tmp_path):
    engine = NcaabEngine(state_path=tmp_path / "ncaab_state.json")
    [pred] = asyncio.run(
        engine.predict([_game("Nobody State A", "Nobody State B",
                              neutral_site=True)])
    )
    assert pred.home_win_prob == pytest.approx(0.5)
    assert pred.predicted_home_score == pytest.approx(ncaab.LEAGUE_AVG_POINTS)
    assert pred.predicted_away_score == pytest.approx(ncaab.LEAGUE_AVG_POINTS)


def test_unknown_teams_rate_flat_1200(tmp_path):
    engine = NcaabEngine(state_path=tmp_path / "ncaab_state.json")
    assert engine._rating("Nobody State Fighting Nobodies") == NON_D1_RATING
    # Two unknowns cancel out: only home court separates them.
    [pred] = asyncio.run(engine.predict([_game("Nobody State A", "Nobody State B")]))
    assert pred.home_win_prob == pytest.approx(_hfa_prob())
    assert pred.predicted_total == pytest.approx(2 * ncaab.LEAGUE_AVG_POINTS)
    # A rated team crushes an unknown guest pinned at 1200.
    engine.book.ratings = {"Kansas Jayhawks": 1900.0}
    [pred] = asyncio.run(engine.predict([_game("Kansas Jayhawks", "Nobody State A")]))
    assert pred.home_win_prob == pytest.approx(_hfa_prob(700.0))


def test_predict_alias_follows_post_data_rebrand(tmp_path):
    engine = NcaabEngine(state_path=tmp_path / "ncaab_state.json")
    engine.book.ratings = {"Saint Francis Red Flash": 1234.5}
    engine.rolling = {"Saint Francis Red Flash": ncaab.deque([(60, 70)], maxlen=10)}
    assert PREDICT_ALIASES["Saint Francis Red Wolves"] == "Saint Francis Red Flash"
    assert engine._rating("Saint Francis Red Wolves") == pytest.approx(1234.5)
    assert engine._avg("Saint Francis Red Wolves", idx=0) == 60.0
    # A direct book hit wins over the alias once new-name data exists.
    engine.book.ratings["Saint Francis Red Wolves"] = 1300.0
    assert engine._rating("Saint Francis Red Wolves") == pytest.approx(1300.0)


# -- state round-trip ---------------------------------------------------------


def test_state_persists_and_reloads(tmp_path):
    path = tmp_path / "ncaab_state.json"
    engine = NcaabEngine(state_path=path)
    engine.book.ratings = {"Houston Cougars": 1876.5}
    engine.rolling = {"Houston Cougars": ncaab.deque([(75, 58)], maxlen=10)}
    engine.metrics = {"overall": {"games": 3, "accuracy": 1.0, "log_loss": 0.2}}
    engine._save_state()

    reloaded = NcaabEngine(state_path=path)
    assert reloaded.book.get("Houston Cougars") == pytest.approx(1876.5)
    assert list(reloaded.rolling["Houston Cougars"]) == [(75, 58)]
    assert reloaded.metrics["overall"]["accuracy"] == 1.0


def test_corrupt_state_is_ignored(tmp_path):
    path = tmp_path / "ncaab_state.json"
    path.write_text("{not json", encoding="utf-8")
    engine = NcaabEngine(state_path=path)
    assert engine.book.ratings == {}
    assert engine.metrics == {}


# -- contract -----------------------------------------------------------------


def test_build_engine_contract():
    engine = build_engine()
    assert engine.sport is Sport.NCAAB
    assert engine.model_name == "ncaab-elo-v1"
    assert ELO_CONFIG.k == 30.0
    assert ELO_CONFIG.home_advantage == 90.0
    assert ELO_CONFIG.season_carry == 0.55
    assert ELO_CONFIG.mov_scaling is True

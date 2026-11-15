"""No-network unit tests for the NCAAF Elo engine."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from kryptscout.core.elo import EloBook
from kryptscout.core.kalman import KalmanBook
from kryptscout.core.schemas import Game, Sport
from kryptscout.sports import ncaaf
from kryptscout.sports.ncaaf import (
    ELO_CONFIG,
    FCS_RATING,
    NcaafEngine,
    _closing_spread,
    _compute_metrics,
    _current_season,
    _expected_prob,
    _parse_teams_payload,
    _replay,
    _season_is_final,
    build_engine,
    make_resolver,
)

NAME_MAP = {
    "Georgia": "Georgia Bulldogs",
    "Ohio State": "Ohio State Buckeyes",
    "Alabama": "Alabama Crimson Tide",
    "San José State": "San José State Spartans",
}
RESOLVE = make_resolver(NAME_MAP)


def _frame(rows: list[dict]) -> pd.DataFrame:
    """Synthetic cfb_schedules slice with the columns _replay consumes."""
    defaults = {
        "game_id": 0,
        "season": 2025,
        "start_date": "2025-09-06T16:00:00.000Z",
        "completed": True,
        "neutral_site": False,
        "home_division": "fbs",
        "away_division": "fbs",
        "home_points": None,
        "away_points": None,
    }
    out = []
    for i, row in enumerate(rows):
        merged = {**defaults, **row}
        if merged["game_id"] == 0:
            merged["game_id"] = 401000000 + i
        out.append(merged)
    return pd.DataFrame(out)


# -- team-name resolution -----------------------------------------------------


def test_resolver_exact_accent_fold_and_fallback():
    assert RESOLVE("Georgia") == "Georgia Bulldogs"
    # Accent drift in either direction still joins.
    assert RESOLVE("San Jose State") == "San José State Spartans"
    accented_map = make_resolver({"San Jose State": "San Jose State Spartans"})
    assert accented_map("San José State") == "San Jose State Spartans"
    # Unknown school keeps a coherent identity: itself.
    assert RESOLVE("Directional Tech") == "Directional Tech"


def test_parse_teams_payload_maps_location_to_display_name():
    payload = {
        "sports": [{"leagues": [{"teams": [
            {"team": {"location": "Georgia", "displayName": "Georgia Bulldogs"}},
            {"team": {"displayName": "No Location Provided"}},  # skipped
        ]}]}]
    }
    assert _parse_teams_payload(payload) == {"Georgia": "Georgia Bulldogs"}


def test_a_shared_location_goes_to_the_major_program():
    # ESPN's directory lists two "Charlotte"s and two "Troy"s; last-entry-wins
    # filed the FBS programs under a small college's name, so the live board's
    # "Charlotte 49ers" never matched its rating. Lowest team id wins, in
    # either order.
    def payload(teams):
        return {"sports": [{"leagues": [{"teams": [{"team": t} for t in teams]}]}]}

    fbs = {"id": "2429", "location": "Charlotte", "displayName": "Charlotte 49ers"}
    small = {"id": "3253", "location": "Charlotte", "displayName": "Charlotte Saints"}
    assert _parse_teams_payload(payload([fbs, small])) == {"Charlotte": "Charlotte 49ers"}
    assert _parse_teams_payload(payload([small, fbs])) == {"Charlotte": "Charlotte 49ers"}


# -- season bookkeeping --------------------------------------------------------


def test_current_season_and_finality():
    assert _current_season(date(2026, 7, 11)) == 2025  # off-season -> last kickoff year
    assert _current_season(date(2026, 9, 1)) == 2026
    assert _season_is_final(2025, date(2026, 7, 11))
    assert not _season_is_final(2025, date(2025, 12, 1))  # bowls still pending


# -- replay ---------------------------------------------------------------------


def test_replay_moves_ratings_tracks_rolling_and_keys_by_display_name():
    df = _frame(
        [
            {"home_team": "Georgia", "away_team": "Alabama",
             "home_points": 31, "away_points": 17},
            {"home_team": "Alabama", "away_team": "Georgia",
             "start_date": "2025-09-13T16:00:00.000Z",
             "home_points": 20, "away_points": 24},
        ]
    )
    book, rolling, records, _ = _replay(df, RESOLVE)
    uga, bama = "Georgia Bulldogs", "Alabama Crimson Tide"
    assert book.get(uga) > ELO_CONFIG.initial > book.get(bama)
    assert "Georgia" not in book.ratings  # keyed by displayName, never school
    assert list(rolling[uga]) == [(31, 17), (24, 20)]
    assert list(rolling[bama]) == [(17, 31), (20, 24)]
    assert len(records) == 2  # 2025 FBS-vs-FBS games feed the backtest
    # Served probability is the Kalman filter's: two fresh teams, home field.
    assert records[0]["p"] == pytest.approx(KalmanBook(ncaaf.KALMAN_CONFIG).expected("h", "a"))
    assert records[0]["p_elo"] == pytest.approx(
        _expected_prob(ELO_CONFIG.initial, ELO_CONFIG.initial, ELO_CONFIG.home_advantage)
    )


def test_replay_skips_unplayed_games():
    df = _frame([{"home_team": "Georgia", "away_team": "Alabama"}])  # no score
    book, rolling, records, _ = _replay(df, RESOLVE)
    assert book.ratings == {}
    assert rolling == {}
    assert records == []


def test_replay_fcs_opponent_rated_flat_and_never_stored():
    df = _frame(
        [
            {"home_team": "Georgia", "away_team": "Mercer",
             "away_division": "fcs", "home_points": 56, "away_points": 7},
        ]
    )
    book, rolling, records, _ = _replay(df, RESOLVE)
    uga = "Georgia Bulldogs"
    # FBS side beat a 1250-rated opponent: gains a little, opponent not stored.
    assert book.get(uga) > ELO_CONFIG.initial
    assert "Mercer" not in book.ratings
    assert len(book.ratings) == 1
    # Expected prob was computed against the flat FCS rating.
    p = _expected_prob(ELO_CONFIG.initial, FCS_RATING, ELO_CONFIG.home_advantage)
    assert p > 0.85  # heavy favorite -> tiny Elo gain
    assert book.get(uga) - ELO_CONFIG.initial < 25.0
    # Buy games never enter the backtest; rolling tracks the FBS side only.
    assert records == []
    assert list(rolling[uga]) == [(56, 7)]
    assert "Mercer" not in rolling


def test_replay_skips_games_with_no_fbs_side():
    df = _frame(
        [
            {"home_team": "Mercer", "away_team": "Furman",
             "home_division": "fcs", "away_division": "fcs",
             "home_points": 28, "away_points": 21},
        ]
    )
    book, rolling, records, _ = _replay(df, RESOLVE)
    assert book.ratings == {}
    assert rolling == {}
    assert records == []


def test_replay_neutral_site_zeroes_home_advantage():
    neutral = _frame(
        [
            {"home_team": "Georgia", "away_team": "Ohio State",
             "neutral_site": True, "home_points": 24, "away_points": 21},
        ]
    )
    _, _, [record], _ = _replay(neutral, RESOLVE)
    assert record["p"] == pytest.approx(0.5)  # equal ratings, no HFA

    hosted = _frame(
        [
            {"home_team": "Georgia", "away_team": "Ohio State",
             "home_points": 24, "away_points": 21},
        ]
    )
    _, _, [hosted_record], _ = _replay(hosted, RESOLVE)
    assert hosted_record["p"] == pytest.approx(KalmanBook(ncaaf.KALMAN_CONFIG).expected("h", "a"))
    assert hosted_record["p"] > 0.5


def test_replay_applies_season_carry_at_boundary():
    df = _frame(
        [
            {"season": 2024, "start_date": "2024-09-07T16:00:00.000Z",
             "home_team": "Georgia", "away_team": "Alabama",
             "home_points": 35, "away_points": 10},
            {"season": 2025, "start_date": "2025-09-06T16:00:00.000Z",
             "home_team": "Ohio State", "away_team": "Alabama",
             "home_points": 30, "away_points": 20},
        ]
    )
    book, _, _, _ = _replay(df, RESOLVE)

    # Replicate season one by hand, then regress 40% to the mean.
    ref = EloBook(config=ELO_CONFIG)
    ref.update("Georgia Bulldogs", "Alabama Crimson Tide", True, margin=25)
    expected = ELO_CONFIG.initial + ELO_CONFIG.season_carry * (
        ref.get("Georgia Bulldogs") - ELO_CONFIG.initial
    )
    assert book.get("Georgia Bulldogs") == pytest.approx(expected)


def test_replay_backtest_records_only_backtest_season():
    df = _frame(
        [
            {"season": 2024, "start_date": "2024-09-07T16:00:00.000Z",
             "home_team": "Georgia", "away_team": "Alabama",
             "home_points": 21, "away_points": 14},
            {"season": 2025, "game_id": 401770001,
             "home_team": "Georgia", "away_team": "Alabama",
             "home_points": 28, "away_points": 3},
        ]
    )
    _, _, records, _ = _replay(df, RESOLVE)
    assert [r["game_id"] for r in records] == [401770001]
    assert records[0]["home_won"] is True


# -- metrics --------------------------------------------------------------------


def test_metrics_accuracy_log_loss_and_spread_comparison():
    records = [
        {"game_id": 1, "season": 2025, "p": 0.9, "home_won": True},
        {"game_id": 2, "season": 2025, "p": 0.3, "home_won": False},
        {"game_id": 3, "season": 2025, "p": 0.8, "home_won": False},  # elo miss
        {"game_id": 4, "season": 2025, "p": 0.6, "home_won": True},  # unpriced
    ]
    spreads = {1: -7.5, 2: 3.0, 3: -10.0, 5: -3.0}  # 5 never played; 4 unpriced
    m = _compute_metrics(records, spreads)
    assert m["n_games"] == 4
    assert m["elo_accuracy"] == pytest.approx(0.75)
    assert m["elo_log_loss"] > 0.0
    assert m["n_games_with_spread"] == 3
    assert m["elo_accuracy_vs_market"] == pytest.approx(2 / 3)
    # Market favorite: home(1) won, away(2) won, home(3) lost -> 2/3.
    assert m["market_accuracy"] == pytest.approx(2 / 3)


def test_metrics_without_spreads_and_empty():
    assert _compute_metrics([]) == {}
    m = _compute_metrics(
        [{"game_id": 1, "season": 2025, "p": 0.7, "home_won": True}], None
    )
    assert m["elo_accuracy"] == 1.0
    assert "market_accuracy" not in m


def test_closing_spread_prefers_consensus():
    lines = [
        {"provider": "DraftKings", "spread": -6.5},
        {"provider": "consensus", "spread": -7.0},
        {"provider": "Bovada", "spread": "-6.0"},
    ]
    assert _closing_spread(lines) == pytest.approx(-7.0)
    assert _closing_spread(lines[:1]) == pytest.approx(-6.5)  # first with a spread
    assert _closing_spread([{"provider": "X", "spread": None}]) is None
    assert _closing_spread([]) is None


# -- prediction -------------------------------------------------------------------


def test_predict_probabilities_and_scores(tmp_path):
    engine = NcaafEngine(state_path=tmp_path / "ncaaf_state.json")
    engine.book.ratings = {"Georgia Bulldogs": 1800.0, "Kent State Golden Flashes": 1350.0}
    engine.kalman.means = {"Georgia Bulldogs": 25.0, "Kent State Golden Flashes": -15.0}
    engine.rolling = {
        "Georgia Bulldogs": ncaaf.deque([(38, 10), (42, 14)], maxlen=8),
        "Kent State Golden Flashes": ncaaf.deque([(13, 45), (17, 41)], maxlen=8),
    }
    game = Game(
        id="ncaaf-test-1",
        sport=Sport.NCAAF,
        start_time=datetime(2026, 9, 5, 19, tzinfo=timezone.utc),
        home="Georgia Bulldogs",
        away="Kent State Golden Flashes",
    )
    [pred] = asyncio.run(engine.predict([game]))

    assert pred.model == "ncaaf-kalman-v3"
    assert pred.home_win_prob + pred.away_win_prob == pytest.approx(1.0)
    assert pred.home_win_prob > 0.9  # 40-point strength gap plus home field
    # home = (UGA PF 40 + KSU PA 43)/2 + 1.5; away = (KSU PF 15 + UGA PA 12)/2 - 1.5
    assert pred.predicted_home_score == pytest.approx(43.0)
    assert pred.predicted_away_score == pytest.approx(12.0)
    assert pred.predicted_total == pytest.approx(55.0)
    assert pred.confidence == pytest.approx(abs(pred.home_win_prob - 0.5) * 2)


def test_predict_neutral_site_drops_home_advantage(tmp_path):
    engine = NcaafEngine(state_path=tmp_path / "ncaaf_state.json")
    engine.book.ratings = {"Georgia Bulldogs": 1700.0, "Ohio State Buckeyes": 1650.0}
    engine.kalman.means = {"Georgia Bulldogs": 6.0, "Ohio State Buckeyes": 3.0}
    for team in ("Georgia Bulldogs", "Ohio State Buckeyes"):  # scoring history → a total
        engine.rolling[team] = deque([(27.5, 27.5)])
    kwargs = dict(
        sport=Sport.NCAAF,
        start_time=datetime(2026, 8, 29, 16, tzinfo=timezone.utc),
        home="Georgia Bulldogs",
        away="Ohio State Buckeyes",
    )
    [neutral] = asyncio.run(
        engine.predict([Game(id="kickoff-classic", neutral_site=True, **kwargs)])
    )
    [hosted] = asyncio.run(engine.predict([Game(id="home-game", **kwargs)]))

    # Neutral: pure rating gap, no HFA. Hosted (regression): full 65-point HFA.
    assert neutral.home_win_prob == pytest.approx(
        engine.kalman.expected("Georgia Bulldogs", "Ohio State Buckeyes", neutral=True)
    )
    assert hosted.home_win_prob == pytest.approx(
        engine.kalman.expected("Georgia Bulldogs", "Ohio State Buckeyes")
    )
    assert neutral.home_win_prob < hosted.home_win_prob
    assert neutral.home_win_prob + neutral.away_win_prob == pytest.approx(1.0)
    # The home-ward points edge is dropped at a neutral site; total unchanged.
    assert neutral.predicted_home_score == pytest.approx(
        hosted.predicted_home_score - ncaaf.HOME_POINTS_EDGE
    )
    assert neutral.predicted_away_score == pytest.approx(
        hosted.predicted_away_score + ncaaf.HOME_POINTS_EDGE
    )
    assert neutral.predicted_total == pytest.approx(hosted.predicted_total)


def test_predict_unknown_team_falls_back_to_league_average(tmp_path):
    engine = NcaafEngine(state_path=tmp_path / "ncaaf_state.json")
    game = Game(
        id="ncaaf-test-2",
        sport=Sport.NCAAF,
        start_time=datetime(2026, 9, 5, 19, tzinfo=timezone.utc),
        home="Georgia Bulldogs",
        away="Georgia Tech Yellow Jackets",
    )
    [pred] = asyncio.run(engine.predict([game]))
    assert pred.home_win_prob == pytest.approx(
        engine.kalman.expected("Georgia Bulldogs", "Georgia Tech Yellow Jackets")
    )
    assert pred.predicted_total is None  # no scoring history: no placeholder total (2026-09-02)


# -- state round-trip --------------------------------------------------------------


def test_state_persists_and_reloads(tmp_path):
    path = tmp_path / "ncaaf_state.json"
    engine = NcaafEngine(state_path=path)
    engine.book.ratings = {"Ohio State Buckeyes": 1810.25}
    engine.rolling = {"Ohio State Buckeyes": ncaaf.deque([(45, 10)], maxlen=8)}
    engine.metrics = {"n_games": 808, "elo_accuracy": 0.73}
    engine._save_state()

    reloaded = NcaafEngine(state_path=path)
    assert reloaded.book.get("Ohio State Buckeyes") == pytest.approx(1810.25)
    assert list(reloaded.rolling["Ohio State Buckeyes"]) == [(45, 10)]
    assert reloaded.rolling["Ohio State Buckeyes"].maxlen == 8
    assert reloaded.metrics["elo_accuracy"] == 0.73


def test_load_state_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "ncaaf_state.json"
    path.write_text("{not json", encoding="utf-8")
    engine = NcaafEngine(state_path=path)
    assert engine.book.ratings == {}
    assert engine.metrics == {}


def test_build_engine_contract():
    engine = build_engine()
    assert engine.sport is Sport.NCAAF
    assert engine.model_name == "ncaaf-kalman-v3"

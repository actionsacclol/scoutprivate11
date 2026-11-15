"""No-network tests for the soccer Poisson engine."""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

import pandas as pd

from kryptscout.core import oddsmath
from kryptscout.core.schemas import Game, Sport
from kryptscout.sports import soccer
from kryptscout.sports.soccer import (
    ESPN_TO_FD,
    EXTRA_LEAGUES,
    LEAGUE_NAME_OVERRIDES,
    LEAGUES,
    MAIN_LEAGUES,
    TEAM_NAME_MAP,
    SoccerEngine,
    _parse_extra_csv,
    _parse_mls_scoreboard,
    devig_decimal,
    expected_goals,
    fit_league,
    outcome_probs,
    poisson_pmf,
)


def _make_df(rows: list[tuple[str, str, str, int, int]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    df["Date"] = pd.to_datetime(df["Date"])
    df["FTR"] = ["H" if h > a else ("A" if a > h else "D") for h, a in zip(df["FTHG"], df["FTAG"])]
    return df


# --------------------------------------------------------------------------
# Poisson grid
# --------------------------------------------------------------------------

def test_poisson_pmf_sums_to_one():
    # Truncating at 10 goals leaves a tiny tail (outcome_probs renormalizes it).
    for lam in (0.3, 1.4, 2.7):
        assert math.isclose(sum(poisson_pmf(lam, k) for k in range(11)), 1.0, abs_tol=1e-3)


def test_outcome_probs_sum_to_one():
    for lam_h, lam_a in ((1.5, 1.1), (0.4, 3.0), (2.2, 2.2)):
        probs = outcome_probs(lam_h, lam_a)
        assert math.isclose(probs["home"] + probs["draw"] + probs["away"], 1.0, rel_tol=1e-9)
        assert 0.0 <= probs["over"] <= 1.0


def test_outcome_probs_symmetric_when_lambdas_equal():
    probs = outcome_probs(1.4, 1.4)
    assert math.isclose(probs["home"], probs["away"], rel_tol=1e-9)
    assert probs["draw"] > 0.2  # equal low-scoring sides draw often


def test_outcome_probs_favor_stronger_side():
    probs = outcome_probs(2.4, 0.8)
    assert probs["home"] > 0.6 > probs["away"]
    assert probs["over"] > outcome_probs(1.0, 0.8)["over"]


# --------------------------------------------------------------------------
# Decimal-odds devigging (market benchmark math)
# --------------------------------------------------------------------------

def test_devig_decimal_normalizes_implied_probs():
    probs = devig_decimal(1.5, 4.2, 7.0)
    assert math.isclose(sum(probs), 1.0, rel_tol=1e-12)
    assert probs[0] > probs[1] > probs[2]  # shorter odds -> bigger probability
    # A vig-free market round-trips exactly: 1/p == the quoted odds ratio.
    fair = devig_decimal(2.0, 2.0)
    assert math.isclose(fair[0], 0.5) and math.isclose(fair[1], 0.5)


def test_devig_decimal_matches_oddsmath_remove_vig():
    # Decimal 2.0 / 3.5 / 4.0 are exactly American +100 / +250 / +300, so the
    # proportional devig in core.oddsmath must agree to machine precision.
    ours = devig_decimal(2.0, 3.5, 4.0)
    theirs = oddsmath.remove_vig(100, 250, 300)
    for a, b in zip(ours, theirs):
        assert math.isclose(a, b, rel_tol=1e-12)


def test_devig_decimal_rejects_invalid_odds():
    import pytest

    with pytest.raises(ValueError):
        devig_decimal(1.0, 3.0, 4.0)


# --------------------------------------------------------------------------
# Fitting: strengths and decay
# --------------------------------------------------------------------------

def test_fit_league_sensible_strengths():
    # Giants pummel Minnows home and away; Mid draws everyone 1-1.
    rows = []
    for d in ("2026-01-03", "2026-01-10", "2026-01-17", "2026-01-24"):
        rows += [
            (d, "Giants", "Minnows", 4, 0),
            (d, "Minnows", "Giants", 0, 3),
            (d, "Mid", "Giants", 1, 1),
            (d, "Mid", "Minnows", 1, 1),
        ]
    state = fit_league(_make_df(rows), datetime(2026, 2, 1))
    teams = state["teams"]
    assert teams["Giants"]["attack"] > 1.0 > teams["Minnows"]["attack"]
    assert teams["Giants"]["defense"] < 1.0 < teams["Minnows"]["defense"]

    lam_h, lam_a = expected_goals(state, "Giants", "Minnows")
    probs = outcome_probs(lam_h, lam_a)
    assert lam_h > lam_a
    assert probs["home"] > 0.6

    # Unknown team falls back to league-average strength, no crash.
    lam_h, lam_a = expected_goals(state, "Newly Promoted", "Mid")
    assert lam_h > 0 and lam_a > 0


def test_decay_recent_form_outweighs_old_form():
    # TeamA: terrible two years ago, dominant last month. Same opponents.
    old, recent = [], []
    for i in range(1, 9):
        old.append((f"2024-03-{i:02d}", "TeamA", "TeamB", 0, 3))
        recent.append((f"2026-01-{i:02d}", "TeamA", "TeamB", 3, 0))
    state = fit_league(_make_df(old + recent), datetime(2026, 2, 1))
    assert state["teams"]["TeamA"]["attack"] > state["teams"]["TeamB"]["attack"]
    assert state["teams"]["TeamA"]["defense"] < state["teams"]["TeamB"]["defense"]


def test_decay_half_life():
    as_of = datetime(2026, 2, 1)
    df = _make_df(
        [
            ("2026-02-01", "A", "B", 2, 0),  # weight 1
            ("2025-08-05", "C", "D", 2, 0),  # ~180 days ago, weight ~0.5
        ]
    )
    days = (as_of - df["Date"]).dt.days
    w = 0.5 ** (days / soccer.HALF_LIFE_DAYS)
    assert math.isclose(w.iloc[0], 1.0)
    assert math.isclose(w.iloc[1], 0.5, rel_tol=0.02)


# --------------------------------------------------------------------------
# football-data "extra" file parsing (BRA/MEX schema)
# --------------------------------------------------------------------------

EXTRA_CSV = (
    "﻿Country,League,Season,Date,Time,Home,Away,HG,AG,Res,PSCH,PSCD,PSCA,AvgCH,AvgCD,AvgCA\n"
    "Brazil,Serie A,2023,12/05/2023,00:30,Flamengo RJ,Santos,2,1,H,1.5,4.0,6.0,1.55,4.1,6.2\n"
    "Brazil,Serie A,2024,14/04/2024,20:00,Santos,Bahia,1,1,D,2.0,3.3,3.9,2.05,3.4,4.0\n"
    "Brazil,Serie A,2025,03/08/2025,18:00,Gremio,Vasco,3,0,,1.8,3.5,4.5,,,\n"
    "Brazil,Serie A,2025,10/08/2025,18:00,Vasco,Remo,,,,,,,,,\n"
    "Mexico,Liga MX,2024/2025,20/07/2024,02:00,Toluca,Puebla,4,2,H,,,,,,\n"
)


def test_parse_extra_csv_filters_and_maps_schema():
    df = _parse_extra_csv(EXTRA_CSV)
    # 2023 season filtered out, goal-less row dropped, "2024/2025" kept.
    assert len(df) == 3
    assert list(df["HomeTeam"]) == ["Santos", "Toluca", "Gremio"]  # date-sorted
    assert list(df.columns[:6]) == ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"]
    # Dates are day-first; goals cast to int; FTR derived from the goals.
    assert df["Date"].iloc[0] == pd.Timestamp(2024, 4, 14)
    assert df["FTHG"].dtype.kind == "i" and df["FTAG"].dtype.kind == "i"
    assert list(df["FTR"]) == ["D", "H", "H"]


def test_parse_extra_csv_closing_odds_with_fallback():
    df = _parse_extra_csv(EXTRA_CSV)
    by_home = df.set_index("HomeTeam")
    # AvgC* preferred when complete...
    assert math.isclose(by_home.loc["Santos", "MarketH"], 2.05)
    assert math.isclose(by_home.loc["Santos", "MarketA"], 4.0)
    # ...falls back to Pinnacle closing when AvgC* is blank...
    assert math.isclose(by_home.loc["Gremio", "MarketH"], 1.8)
    assert math.isclose(by_home.loc["Gremio", "MarketD"], 3.5)
    # ...and stays NaN when no book priced the game.
    assert math.isnan(by_home.loc["Toluca", "MarketH"])


def test_parse_extra_csv_respects_min_season():
    df = _parse_extra_csv(EXTRA_CSV, min_season=2023)
    assert len(df) == 4  # the 2023 row is back in


# --------------------------------------------------------------------------
# MLS ingestion (ESPN scoreboard JSON -> result rows)
# --------------------------------------------------------------------------

def _mls_event(home: str, away: str, hs: str, as_: str, completed: bool) -> dict:
    return {
        "id": "401",
        "date": "2025-04-05T18:30Z",
        "competitions": [
            {
                "status": {"type": {"completed": completed}},
                "competitors": [
                    {"homeAway": "home", "score": hs, "team": {"displayName": home}},
                    {"homeAway": "away", "score": as_, "team": {"displayName": away}},
                ],
            }
        ],
    }


def test_parse_mls_scoreboard_keeps_completed_games_only():
    payload = {
        "events": [
            _mls_event("Charlotte FC", "Nashville SC", "2", "1", True),
            _mls_event("LA Galaxy", "LAFC", "", "", False),  # in progress/scheduled
        ]
    }
    rows = _parse_mls_scoreboard(payload)
    assert len(rows) == 1
    row = rows[0]
    assert row["HomeTeam"] == "Charlotte FC" and row["AwayTeam"] == "Nashville SC"
    assert row["FTHG"] == 2 and row["FTAG"] == 1
    # Naive UTC datetime, same convention as the football-data frames.
    assert row["Date"] == datetime(2025, 4, 5, 18, 30)
    assert row["Date"].tzinfo is None


def test_parse_mls_scoreboard_empty_payload():
    assert _parse_mls_scoreboard({}) == []


# --------------------------------------------------------------------------
# Name mapping and prediction plumbing (state injected, no network)
# --------------------------------------------------------------------------

def test_team_name_map_shape():
    # 20+20+18+21+18 clubs in the original five leagues (Serie A carries the
    # Monza comeback), plus Championship 24, Eredivisie 18, Primeira Liga 18,
    # Brasileirão 20 and Liga MX 18 (Santos Laguna lives in
    # LEAGUE_NAME_OVERRIDES, see below).
    # ...plus the 18 clubs promoted for 2026-27.
    assert len(TEAM_NAME_MAP) == 213
    # Mapping must be invertible (no two clubs share an ESPN name).
    assert len(ESPN_TO_FD) == len(TEAM_NAME_MAP)
    assert ESPN_TO_FD["Manchester United"] == "Man United"
    assert ESPN_TO_FD["Athletic Club"] == "Ath Bilbao"
    assert ESPN_TO_FD["Eintracht Frankfurt"] == "Ein Frankfurt"
    assert ESPN_TO_FD["Sheffield Wednesday"] == "Sheffield Weds"
    assert ESPN_TO_FD["Ajax Amsterdam"] == "Ajax"
    assert ESPN_TO_FD["Sporting CP"] == "Sp Lisbon"
    assert ESPN_TO_FD["Vasco da Gama"] == "Vasco"
    assert ESPN_TO_FD["Pumas UNAM"] == "UNAM Pumas"


def test_santos_name_collision_resolved_per_league():
    # ESPN calls both Brazil's Santos and Mexico's Santos Laguna "Santos".
    # Brazil owns the flat entry; Liga MX resolves through the override.
    assert TEAM_NAME_MAP["Santos"] == "Santos"
    assert "Santos Laguna" not in TEAM_NAME_MAP
    assert LEAGUE_NAME_OVERRIDES["Liga MX"]["Santos"] == "Santos Laguna"
    engine = SoccerEngine()
    assert engine._to_fd_name("Santos", "Liga MX") == "Santos Laguna"
    assert engine._to_fd_name("Santos", "Brasileirão") == "Santos"


def _synthetic_engine() -> SoccerEngine:
    engine = SoccerEngine()
    engine._state = {
        "fitted_at": "2026-02-01T00:00:00",
        "leagues": {
            "EPL": {
                "home_avg": 1.6,
                "away_avg": 1.2,
                "teams": {
                    "Man City": {"attack": 1.5, "defense": 0.7},
                    "Wolves": {"attack": 0.8, "defense": 1.2},
                },
            },
            "MLS": {
                "home_avg": 1.7,
                "away_avg": 1.2,
                "teams": {
                    "Inter Miami CF": {"attack": 1.6, "defense": 0.8},
                    "Toronto FC": {"attack": 0.7, "defense": 1.3},
                },
            },
        },
    }
    return engine


def _game(home: str, away: str, league: str = "EPL") -> Game:
    return Game(
        id="soccer-test-1",
        sport=Sport.SOCCER,
        league=league,
        start_time=datetime(2026, 2, 7, 15, tzinfo=timezone.utc),
        home=home,
        away=away,
    )


def test_predict_maps_espn_names_and_sums_to_one():
    engine = _synthetic_engine()
    preds = asyncio.run(engine.predict([_game("Manchester City", "Wolverhampton Wanderers")]))
    assert len(preds) == 1
    p = preds[0]
    assert p.model == "soccer-poisson-v3"
    assert math.isclose(p.home_win_prob + p.draw_prob + p.away_win_prob, 1.0, rel_tol=1e-9)
    assert p.home_win_prob > p.away_win_prob  # strong home side favored
    assert math.isclose(p.predicted_total, p.predicted_home_score + p.predicted_away_score)


def test_predict_mls_uses_espn_names_directly():
    # MLS is fitted on ESPN display names — no football-data mapping involved.
    engine = _synthetic_engine()
    preds = asyncio.run(engine.predict([_game("Inter Miami CF", "Toronto FC", league="MLS")]))
    assert len(preds) == 1
    p = preds[0]
    assert p.home_win_prob > 0.5 > p.away_win_prob  # strengths were found and used
    # An unknown MLS side has no fitted strengths — the game is skipped so
    # fallback probabilities can't manufacture fake edges downstream.
    neutral = asyncio.run(engine.predict([_game("Expansion FC", "Toronto FC", league="MLS")]))
    assert neutral == []


def test_predict_unmapped_team_skipped():
    # Unmapped/unfitted sides used to fall through with league-average
    # strengths; that produced huge fake EV against longshot prices, so
    # predict() now skips the game entirely.
    engine = _synthetic_engine()
    preds = asyncio.run(engine.predict([_game("Sporting Nowhere FC", "Manchester City")]))
    assert preds == []


def test_predict_unknown_league_skipped():
    engine = _synthetic_engine()
    preds = asyncio.run(engine.predict([_game("Celtic", "Rangers", league="Scottish Prem")]))
    assert preds == []


def test_league_labels_match_espn_datasource():
    from kryptscout.datasources.espn import LEAGUE_PATHS

    espn_labels = {label for _, label in LEAGUE_PATHS[Sport.SOCCER]}
    assert set(LEAGUES.values()) == espn_labels
    # LEAGUES is the union of the three ingestion paths, with no code reused.
    assert len(LEAGUES) == len(MAIN_LEAGUES) + len(EXTRA_LEAGUES) + 1  # + MLS
    assert LEAGUES["MLS"] == "MLS"

"""NHL xG path (nhl-xg-v2): MoneyPuck team game file → per-team EWMAs →
walk-forward logistic, with the Elo fallback when the file is absent."""
from __future__ import annotations

import asyncio
import csv
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kryptscout.core.calibrate import temper
from kryptscout.core.schemas import Game, Sport
from kryptscout.sports import nhl

COLS = ["team", "season", "gameId", "opposingTeam", "home_or_away", "gameDate",
        "situation", "xGoalsFor", "goalsFor", "xGoalsAgainst", "goalsAgainst", "playoffGame"]


def _write_csv(path: Path, rows: list[dict]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def _game_rows(gid, season, date, home, away, xg_h, xg_a, g_h, g_a, xg5_h=None, xg5_a=None, playoff=0):
    """Both teams' 'all' and '5on5' rows for one game, as MoneyPuck lays them out."""
    xg5_h = xg_h * 0.7 if xg5_h is None else xg5_h
    xg5_a = xg_a * 0.7 if xg5_a is None else xg5_a
    out = []
    for team, opp, side, xf, xa, gf, ga, x5f, x5a in (
        (home, away, "HOME", xg_h, xg_a, g_h, g_a, xg5_h, xg5_a),
        (away, home, "AWAY", xg_a, xg_h, g_a, g_h, xg5_a, xg5_h),
    ):
        for situation, f, a in (("all", xf, xa), ("5on5", x5f, x5a)):
            out.append(dict(team=team, season=season, gameId=gid, opposingTeam=opp, home_or_away=side,
                            gameDate=date, situation=situation, xGoalsFor=f, goalsFor=gf,
                            xGoalsAgainst=a, goalsAgainst=ga, playoffGame=playoff))
    return out


def test_loader_pairs_both_sides_and_maps_codes(tmp_path):
    rows = _game_rows(2024020001, 2024, 20241008, "TOR", "L.A", 3.1, 1.9, 4, 1)
    rows += _game_rows(2024030001, 2024, 20250420, "TOR", "BOS", 2.0, 2.0, 1, 2, playoff=1)
    games = nhl.load_moneypuck_games(_write_csv(tmp_path / "all_teams.csv", rows))
    assert len(games) == 1  # playoff game dropped
    g = games[0]
    assert (g.home, g.away) == ("Toronto Maple Leafs", "Los Angeles Kings")
    assert (g.xg_home, g.xg_away, g.goals_home, g.goals_away) == (3.1, 1.9, 4, 1)
    assert g.xg5_home == pytest.approx(3.1 * 0.7)
    assert g.date == "2024-10-08" and g.season == 2024


def test_replay_moves_the_dominant_team_up_and_regresses_at_season_end():
    games = []
    for i in range(6):
        games.append(nhl.XgGame(id=i, season=2024, date=f"2024-10-{10+i:02d}", home="Toronto Maple Leafs",
                                away="Boston Bruins", xg_home=3.5, xg_away=1.5, xg5_home=2.5, xg5_away=1.0,
                                goals_home=3, goals_away=1))
    state, rows = nhl.xg_replay(games)
    tor = state["Toronto Maple Leafs"]
    assert tor["n"] == 6 and tor["xgf"] > 0 > tor["xga"]
    # The feature row for game 6 sees five prior games: Toronto clearly ahead.
    assert rows[-1]["xg_diff"] > 0 and rows[-1]["n_min"] == 5
    # First game had no history: zero features.
    assert rows[0]["xg_diff"] == 0.0 and rows[0]["n_min"] == 0
    before = tor["xgf"]
    games.append(nhl.XgGame(id=99, season=2025, date="2025-10-08", home="Toronto Maple Leafs",
                            away="Boston Bruins", xg_home=2.8, xg_away=2.8, xg5_home=2.0, xg5_away=2.0,
                            goals_home=2, goals_away=3))
    state2, rows2 = nhl.xg_replay(games)
    # The 2025 row was priced off strengths shrunk by XG_SEASON_CARRY first.
    assert rows2[-1]["xg_diff"] == pytest.approx(nhl.XG_SEASON_CARRY * (rows[-1]["xg_diff"] + 0) , rel=0.5)
    assert rows2[-1]["season"] == 2025


def test_fit_recovers_the_direction_and_predicts_the_favourite():
    rng_rows = []
    import random
    random.seed(7)
    for i in range(3000):
        d = random.gauss(0, 0.6)
        p = 1 / (1 + math.exp(-(0.1 + 1.5 * d)))
        rng_rows.append({"season": 2016 + i % 8, "n_min": 10, "home_won": random.random() < p,
                         "xg_diff": d, "xg5_diff": d * 0.7, "g_diff": d * 0.5, "sh_diff": 0.0, "sv_diff": 0.0})
    w = nhl.fit_xg_logit(rng_rows, upto_season=2024)
    assert w is not None and len(w) == 1 + len(nhl.XG_FEATURES)
    assert w[1] > 0  # xg_diff carries the signal
    p_fav = nhl.xg_prob(w, {"xg_diff": 0.8, "xg5_diff": 0.56, "g_diff": 0.4, "sh_diff": 0.0, "sv_diff": 0.0})
    assert p_fav > 0.6
    assert nhl.fit_xg_logit(rng_rows[:3], upto_season=2024) is None  # too thin to fit


def test_engine_serves_xg_when_available_and_elo_otherwise(tmp_path, monkeypatch):
    monkeypatch.setattr(nhl, "XG_ENABLED", True)  # off by default since v3
    engine = nhl.NhlEngine(state_path=tmp_path / "state.json", raw_dir=tmp_path / "raw")
    game = Game(id="nhl-1", sport=Sport.NHL, league="", start_time=datetime(2026, 10, 8, tzinfo=timezone.utc),
                home="Toronto Maple Leafs", away="Boston Bruins")
    [pred] = asyncio.run(engine.predict([game]))
    assert pred.model == "nhl-elo-v1"
    # Now with an xG state and weights in place.
    engine._xg_weights = [0.05, 1.5, 0.0, 0.0, 0.0, 0.0]
    engine._xg_last_date = "2026-10-07"  # a current file; a stale one never serves
    engine._xg_state = {
        "Toronto Maple Leafs": {"xgf": 0.6, "xga": -0.4, "xgf5": 0.3, "xga5": -0.2, "gf": 0.3, "ga": -0.3, "sh": 0.0, "sv": 0.0, "n": 20},
        "Boston Bruins": {"xgf": -0.2, "xga": 0.3, "xgf5": -0.1, "xga5": 0.2, "gf": -0.1, "ga": 0.2, "sh": 0.0, "sv": 0.0, "n": 20},
    }
    [pred] = asyncio.run(engine.predict([game]))
    assert pred.model == "nhl-xg-v2" and engine.model_name == "nhl-xg-v2"
    xg_diff = (0.6 + 0.4) - (-0.2 - 0.3)
    raw = 1 / (1 + math.exp(-(0.05 + 1.5 * xg_diff)))
    assert pred.home_win_prob == pytest.approx(temper(raw, nhl.XG_LOGIT_SCALE))
    # An unknown team falls back to Elo for that game only.
    other = game.model_copy(update={"id": "nhl-2", "away": "Utah Mammoth"})
    [fallback] = asyncio.run(engine.predict([other]))
    assert fallback.model == "nhl-elo-v1"
    # State round-trips the xG block.
    engine._save_state()
    again = nhl.NhlEngine(state_path=tmp_path / "state.json", raw_dir=tmp_path / "raw")
    again._ensure_loaded()
    assert again._xg_weights == engine._xg_weights and again._xg_state["Toronto Maple Leafs"]["n"] == 20


def test_refresh_xg_fits_from_the_file_and_skips_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(nhl, "XG_ENABLED", True)  # off by default since v3
    engine = nhl.NhlEngine(state_path=tmp_path / "state.json", raw_dir=tmp_path / "raw")
    assert engine._refresh_xg() is False  # no folder, no file: nothing happens
    mp = tmp_path / "raw" / "moneypuck"
    mp.mkdir(parents=True)
    rows = []
    for season in (2015, 2016, 2017, 2018):
        for i in range(60):
            gid = int(f"{season}02{i + 1:04d}")  # NHL id: season + 02 (regular season) + game
            home, away = ("TOR", "BOS") if i % 2 else ("BOS", "TOR")
            strong_home = home == "TOR"
            rows += _game_rows(gid, season, int(f"{season}10{1 + i % 28:02d}"), home, away,
                               3.4 if strong_home else 1.8, 1.8 if strong_home else 3.4,
                               3 if strong_home else 1, 1 if strong_home else 3)
    _write_csv(mp / nhl.MONEYPUCK_TEAMS_FILE, rows)
    assert engine._refresh_xg() is True
    assert engine.model_name == "nhl-xg-v2"
    assert engine._xg_season == 2018 and engine._xg_weights is not None
    assert engine._xg_metrics["season"] == 2018 and engine._xg_metrics["games"] > 0
    assert engine._xg_metrics["log_loss"] < math.log(2)  # separates the rigged teams
    # Same file again: skipped by mtime.
    assert engine._refresh_xg() is False

"""Tests for the v3 changes in sports/nhl.py (proposed; add to backend/tests/)."""
from __future__ import annotations

import asyncio
import json
import math
from datetime import date, datetime, timezone

from kryptscout.core.elo import EloBook
from kryptscout.core.schemas import Game, Sport
from kryptscout.sports import nhl
from kryptscout.sports.nhl import GameRecord, NhlEngine


class TestSeasonWindow:
    def test_window_follows_the_calendar(self):
        # The bug: a literal ("20242025", "20252026") never picked up 2026-27.
        assert nhl._seasons(date(2026, 11, 14)) == ("20242025", "20252026", "20262027")
        assert nhl._seasons(date(2026, 7, 10)) == ("20232024", "20242025", "20252026")
        assert nhl._seasons(date(2027, 1, 5))[-1] == "20262027"

    def test_current_season_is_never_past(self):
        for today in (date(2026, 9, 1), date(2026, 11, 14), date(2027, 6, 1)):
            assert not nhl._is_past_season(nhl._seasons(today)[-1], today)


class TestContext:
    def test_back_to_back_is_exactly_one_day(self):
        assert nhl._is_back_to_back("2026-11-13", "2026-11-14")
        assert not nhl._is_back_to_back("2026-11-12", "2026-11-14")
        assert not nhl._is_back_to_back("2026-11-14", "2026-11-14")
        assert not nhl._is_back_to_back(None, "2026-11-14")

    def test_ctx_prob_directions(self):
        base = nhl.ctx_prob(0.55, False, False, 0.0)
        assert abs(base - 0.55) < 0.02  # near-identity on the Elo number
        assert nhl.ctx_prob(0.55, True, False, 0.0) < base  # tired home side
        assert nhl.ctx_prob(0.55, False, True, 0.0) > base  # tired visitor
        assert nhl.ctx_prob(0.55, False, False, 5.0) > base  # home outshoots
        assert 0.0 < nhl.ctx_prob(1.0, False, False, 0.0) < 1.0  # clamped logit

    def test_schedule_dates_include_unplayed_games(self):
        payload = {"games": [
            {"gameType": 2, "gameState": "FUT", "gameDate": "2026-11-13",
             "homeTeam": {"abbrev": "BOS"}, "awayTeam": {"abbrev": "TOR"}},
            {"gameType": 1, "gameState": "FINAL", "gameDate": "2026-09-20",
             "homeTeam": {"abbrev": "BOS"}, "awayTeam": {"abbrev": "TOR"}},
        ]}
        dates = nhl.schedule_dates([payload])
        assert dates["Boston Bruins"] == {"2026-11-13"}  # preseason excluded
        assert dates["Toronto Maple Leafs"] == {"2026-11-13"}

    def test_replay_builds_shots_and_carries_them_over_the_summer(self):
        def rec(i, season, day, hs, as_, sog_h, sog_a):
            return GameRecord(id=i, season=season, game_type=2, date=day, home="Boston Bruins",
                              away="Toronto Maple Leafs", home_score=hs, away_score=as_,
                              home_sog=sog_h, away_sog=sog_a)

        shots, last = {}, {}
        records = [rec(i, 20252026, f"2026-01-{i + 1:02d}", 3, 2, 35, 25) for i in range(6)]
        nhl.replay(records, EloBook(config=nhl.ELO_CONFIG), nhl._new_rolling(), shots=shots, last_played=last)
        assert shots["Boston Bruins"]["n"] == 6
        assert shots["Boston Bruins"]["sog"] > 0 > shots["Toronto Maple Leafs"]["sog"]
        before = shots["Boston Bruins"]["sog"]
        nxt = [rec(99, 20262027, "2026-10-07", 1, 2, 30, 30)]
        log = []
        nhl.replay(nxt, EloBook(config=nhl.ELO_CONFIG), nhl._new_rolling(), last_season=20252026,
                   shots=shots, last_played=last, log=log)
        # carried at 0.6 before the opener was priced, then updated toward 0
        assert shots["Boston Bruins"]["sog"] < before * nhl.CTX_SEASON_CARRY + 1e-9
        assert last["Boston Bruins"] == "2026-10-07"
        assert 0.0 < log[0][1] < 1.0

    def test_predict_uses_the_schedule_for_back_to_backs(self, tmp_path):
        engine = NhlEngine(state_path=tmp_path / "state.json", raw_dir=tmp_path / "raw")
        engine._loaded = True
        engine._shots = {"Boston Bruins": {"sog": 2.0, "n": 30}, "Toronto Maple Leafs": {"sog": -1.0, "n": 30}}
        engine._team_dates = {"Boston Bruins": {"2026-11-14"}, "Toronto Maple Leafs": {"2026-11-13", "2026-11-14"}}
        game = Game(id="nhl-1", sport=Sport.NHL, start_time=datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc),
                    home="Boston Bruins", away="Toronto Maple Leafs")
        pred = asyncio.run(engine.predict([game]))[0]
        assert pred.model == nhl.CTX_MODEL_NAME
        elo_p = nhl._expected_home_prob(engine._book, "Boston Bruins", "Toronto Maple Leafs")
        assert math.isclose(pred.home_win_prob, nhl.ctx_prob(elo_p, False, True, 3.0))

    def test_predict_falls_back_to_elo_without_shot_history(self, tmp_path):
        engine = NhlEngine(state_path=tmp_path / "state.json", raw_dir=tmp_path / "raw")
        engine._loaded = True
        game = Game(id="nhl-1", sport=Sport.NHL, start_time=datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc),
                    home="Boston Bruins", away="Toronto Maple Leafs")
        assert asyncio.run(engine.predict([game]))[0].model == nhl.ELO_MODEL_NAME

    def test_pre_v3_state_is_rebuilt(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({
            "model": "nhl-xg-v2", "last_season": 20252026, "ratings": {"Boston Bruins": 1600.0},
            "rolling": {}, "goalies": {}, "team_goalie": {}, "processed_ids": [1, 2, 3], "backtest": [],
        }), encoding="utf-8")
        engine = NhlEngine(state_path=state_path, raw_dir=tmp_path / "raw")
        engine._ensure_loaded()
        assert engine._processed == set()  # no "shots" key -> replay from the caches


class TestXgGuards:
    def test_shootout_rows_are_not_training_labels(self):
        rows = []
        for i in range(400):
            tied = i % 8 in (0, 1)  # a quarter of the games went to a shootout
            rows.append({"season": 2020, "n_min": 10, "home_won": i % 8 in (2, 3, 4), "tied": tied,
                         "xg_diff": 0.0, "xg5_diff": 0.0, "g_diff": 0.0, "sh_diff": 0.0, "sv_diff": 0.0})
        w = nhl.fit_xg_logit(rows, upto_season=2021)
        # 150 home wins of 300 decided games -> intercept 0, not log(150/250) = -0.51
        assert abs(w[0]) < 0.05

    def test_stale_file_does_not_serve(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nhl, "XG_ENABLED", True)
        engine = NhlEngine(state_path=tmp_path / "state.json", raw_dir=tmp_path / "raw")
        engine._xg_weights = [0.0] * 6
        engine._xg_last_date = "2026-04-16"
        engine._latest_result = "2026-11-14"
        assert not engine._xg_serving()
        engine._xg_last_date = "2026-11-10"
        assert engine._xg_serving()
        monkeypatch.setattr(nhl, "XG_ENABLED", False)
        assert not engine._xg_serving()

"""Unit tests for the NHL engine — no network, synthetic data only."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import json

from kryptscout.core.elo import EloBook
from kryptscout.core.schemas import Game, Sport
from kryptscout.sports import nhl
from kryptscout.sports.nhl import (
    ELO_CONFIG,
    GOALIE_ADJ_CAP,
    GOALIE_SPAN,
    LEAGUE_AVG_SAVE_PCT,
    TEAM_NAMES,
    GameRecord,
    GoalieStart,
    NhlEngine,
    _goalie_diff,
    _goalie_value,
    _new_rolling,
    _poisson_over_prob,
    _predicted_goals,
    _record_goalie_start,
    attach_goalies,
    games_from_payloads,
    replay,
    starting_goalie,
)


def _raw_game(gid: int, season: int, home: str, away: str, hs: int, as_: int, **kw):
    return {
        "id": gid,
        "season": season,
        "gameType": kw.get("game_type", 2),
        "gameDate": kw.get("date", "2024-10-10"),
        "gameState": kw.get("state", "OFF"),
        "homeTeam": {"abbrev": home, "score": hs},
        "awayTeam": {"abbrev": away, "score": as_},
    }


def _record(gid: int, home: str, away: str, hs: int, as_: int, season: int = 20242025, **kw):
    return GameRecord(
        id=gid,
        season=season,
        game_type=2,
        date=f"2024-10-{10 + gid % 19:02d}",
        home=home,
        away=away,
        home_score=hs,
        away_score=as_,
        home_goalie=kw.get("home_goalie"),
        away_goalie=kw.get("away_goalie"),
    )


def _boxscore_goalie(pid: int, name: str, toi: str, saves: int, shots: int, starter=False):
    return {
        "playerId": pid,
        "name": {"default": name},
        "position": "G",
        "toi": toi,
        "starter": starter,
        "saves": saves,
        "shotsAgainst": shots,
        "goalsAgainst": shots - saves,
    }


CANNED_BOXSCORE = {
    "id": 2024020001,
    "playerByGameStats": {
        "homeTeam": {
            "forwards": [],
            "defense": [],
            "goalies": [
                _boxscore_goalie(101, "Starter Home", "59:05", 19, 22, starter=True),
                _boxscore_goalie(102, "Backup Home", "00:00", 0, 0),
            ],
        },
        "awayTeam": {
            "forwards": [],
            "defense": [],
            "goalies": [
                # Starter pulled early; the backup carried the game (more TOI).
                _boxscore_goalie(201, "Pulled Away", "10:00", 2, 5, starter=True),
                _boxscore_goalie(202, "Relief Away", "64:35", 30, 31),
            ],
        },
    },
}


class TestTeamNames:
    def test_all_32_teams(self):
        assert len(TEAM_NAMES) == 32
        assert TEAM_NAMES["UTA"] == "Utah Mammoth"
        assert len(set(TEAM_NAMES.values())) == 32


class TestGamesFromPayloads:
    def test_dedupes_game_seen_from_both_clubs(self):
        game = _raw_game(2024020001, 20242025, "BOS", "TOR", 4, 2)
        records = games_from_payloads([{"games": [game]}, {"games": [dict(game)]}])
        assert len(records) == 1
        assert records[0].home == "Boston Bruins"
        assert records[0].away == "Toronto Maple Leafs"

    def test_filters_preseason_unfinished_and_non_nhl(self):
        payload = {
            "games": [
                _raw_game(1, 20242025, "BOS", "TOR", 4, 2, game_type=1),  # preseason
                _raw_game(2, 20242025, "BOS", "TOR", 0, 0, state="FUT"),  # scheduled
                _raw_game(3, 20242025, "BOS", "SKA", 4, 2),  # exhibition side
                _raw_game(4, 20242025, "BOS", "TOR", 3, 1),
            ]
        }
        records = games_from_payloads([payload])
        assert [r.id for r in records] == [4]

    def test_sorted_chronologically_across_seasons(self):
        payload = {
            "games": [
                _raw_game(9, 20252026, "BOS", "TOR", 2, 1, date="2025-10-08"),
                _raw_game(5, 20242025, "TOR", "BOS", 3, 2, date="2025-01-05"),
                _raw_game(4, 20242025, "BOS", "TOR", 1, 0, date="2024-11-01"),
            ]
        }
        records = games_from_payloads([payload])
        assert [r.id for r in records] == [4, 5, 9]


class TestReplay:
    def test_winner_gains_rating_and_rolling_tracks_goals(self):
        book = EloBook(config=ELO_CONFIG)
        rolling = _new_rolling()
        records = [
            _record(i, "Boston Bruins", "Toronto Maple Leafs", 5, 2) for i in range(10)
        ]
        last, _ = replay(records, book, rolling)
        assert last == 20242025
        assert book.get("Boston Bruins") > book.get("Toronto Maple Leafs")
        assert book.expected("Boston Bruins", "Toronto Maple Leafs") > 0.5
        gf, ga = nhl._team_rates(rolling, "Boston Bruins")
        assert gf == 5.0 and ga == 2.0

    def test_rolling_window_caps_at_20(self):
        rolling = _new_rolling()
        records = [_record(i, "Boston Bruins", "Toronto Maple Leafs", 3, 1) for i in range(30)]
        replay(records, EloBook(config=ELO_CONFIG), rolling)
        assert len(rolling["Boston Bruins"]) == 20

    def test_season_boundary_regresses_ratings(self):
        book = EloBook(config=ELO_CONFIG)
        s1 = [_record(i, "Boston Bruins", "Toronto Maple Leafs", 6, 1) for i in range(20)]
        replay(s1, book, _new_rolling())
        peak = book.get("Boston Bruins")
        s2 = [_record(100, "Toronto Maple Leafs", "Boston Bruins", 2, 3, season=20252026)]
        replay(s2, book, _new_rolling(), last_season=20242025)
        # First game of the new season should start from a regressed rating.
        regressed = 1500.0 + 0.7 * (peak - 1500.0)
        assert book.get("Boston Bruins") < peak
        assert abs(book.get("Boston Bruins") - regressed) < ELO_CONFIG.k * 2

    def test_eval_probs_are_pre_update_and_bounded(self):
        records = [
            _record(i, "Boston Bruins", "Toronto Maple Leafs", 4, 2) for i in range(5)
        ]
        book = EloBook(config=ELO_CONFIG)
        _, evals = replay(records, book, _new_rolling(), eval_season=20242025)
        assert len(evals) == 5
        first_prob = evals[0][0]
        # Before any games, only home ice separates the sides.
        fresh = EloBook(config=ELO_CONFIG)
        assert first_prob == fresh.expected("Boston Bruins", "Toronto Maple Leafs")
        assert all(0.0 < p < 1.0 for p, _, _ in evals)
        assert all(t > 0 for _, t, _ in evals)


class TestStartingGoalie:
    def test_most_toi_wins_even_over_starter_flag(self):
        home = starting_goalie(CANNED_BOXSCORE, "homeTeam")
        assert home == GoalieStart(id=101, name="Starter Home", saves=19, shots=22)
        # The pulled starter has the flag; the relief goalie has the TOI.
        away = starting_goalie(CANNED_BOXSCORE, "awayTeam")
        assert away.id == 202 and away.name == "Relief Away"
        assert away.saves == 30 and away.shots == 31  # OT toi "64:35" parses

    def test_empty_or_malformed_payloads(self):
        assert starting_goalie({}, "homeTeam") is None
        assert starting_goalie({"playerByGameStats": {}}, "homeTeam") is None
        no_goalies = {"playerByGameStats": {"homeTeam": {"goalies": []}}}
        assert starting_goalie(no_goalies, "homeTeam") is None
        assert nhl._toi_seconds(None) == 0
        assert nhl._toi_seconds("garbage") == 0

    def test_attach_goalies_from_cache(self, tmp_path):
        (tmp_path / "1.json").write_text(json.dumps(CANNED_BOXSCORE), encoding="utf-8")
        (tmp_path / "3.json").write_text("not json", encoding="utf-8")
        records = [
            _record(1, "Boston Bruins", "Toronto Maple Leafs", 3, 1),
            _record(2, "Boston Bruins", "Toronto Maple Leafs", 3, 1),  # uncached
            _record(3, "Boston Bruins", "Toronto Maple Leafs", 3, 1),  # corrupt
        ]
        out = attach_goalies(records, tmp_path)
        assert out[0].home_goalie.id == 101 and out[0].away_goalie.id == 202
        assert out[1].home_goalie is None and out[1].away_goalie is None
        assert out[2].home_goalie is None  # corrupt cache degrades to None


class TestGoalieRatings:
    def test_debut_and_unknown_are_zero(self):
        goalies: dict = {}
        assert _goalie_value(goalies, None) == 0.0
        assert _goalie_value(goalies, "8480045") == 0.0
        assert _goalie_diff(goalies, "8480045", None) == 0.0

    def test_ewma_shrinks_toward_league_average(self):
        goalies: dict = {}
        # One .950 start moves a rookie only alpha of the way off the prior.
        _record_goalie_start(goalies, GoalieStart(1, "Rook", 19, 20), span=GOALIE_SPAN)
        alpha = 2.0 / (GOALIE_SPAN + 1.0)
        expected = LEAGUE_AVG_SAVE_PCT + alpha * (0.95 - LEAGUE_AVG_SAVE_PCT)
        assert abs(goalies["1"]["ewma"] - expected) < 1e-12
        assert goalies["1"]["starts"] == 1
        assert 0 < _goalie_value(goalies, "1") < 0.95 - LEAGUE_AVG_SAVE_PCT

    def test_zero_shot_starts_are_skipped(self):
        goalies: dict = {}
        _record_goalie_start(goalies, GoalieStart(1, "G", 0, 0))
        _record_goalie_start(goalies, None)
        assert goalies == {}

    def test_diff_is_clamped(self):
        goalies = {
            "1": {"name": "Elite", "ewma": 0.999, "starts": 50},
            "2": {"name": "Sieve", "ewma": 0.800, "starts": 50},
        }
        assert _goalie_diff(goalies, "1", "2", k_goalie=10000.0) == GOALIE_ADJ_CAP
        assert _goalie_diff(goalies, "2", "1", k_goalie=10000.0) == -GOALIE_ADJ_CAP

    def test_replay_walk_forward_integrity(self):
        """Game 1's own save% must not touch game 1's eval probability."""
        elite = GoalieStart(1, "Elite", 40, 40)  # perfect starts for the home side
        sieve = GoalieStart(2, "Sieve", 10, 20)
        records = [
            _record(i, "Boston Bruins", "Toronto Maple Leafs", 4, 2,
                    home_goalie=elite, away_goalie=sieve)
            for i in range(3)
        ]
        run = {}
        for k in (0.0, 1500.0):
            book = EloBook(config=ELO_CONFIG)
            _, evals = replay(records, book, _new_rolling(),
                              eval_season=20242025, k_goalie=k)
            run[k] = [p for p, _, _ in evals]
            # Team Elo never sees the goalie overlay.
            elo_after = book.get("Boston Bruins")
            run[f"elo_{k}"] = elo_after
        # First game: no goalie history yet, adjustment must be exactly 0.
        assert run[1500.0][0] == run[0.0][0]
        # Later games: the elite home goalie's earned EWMA moves the number.
        assert run[1500.0][1] > run[0.0][1]
        assert run[1500.0][2] > run[0.0][2]
        assert run["elo_0.0"] == run["elo_1500.0"]

    def test_replay_tracks_most_recent_team_starter(self):
        goalies: dict = {}
        team_goalie: dict = {}
        records = [
            _record(1, "Boston Bruins", "Toronto Maple Leafs", 4, 2,
                    home_goalie=GoalieStart(1, "A", 25, 27),
                    away_goalie=GoalieStart(2, "B", 20, 24)),
            _record(2, "Boston Bruins", "Toronto Maple Leafs", 4, 2,
                    home_goalie=GoalieStart(3, "C", 25, 27),
                    away_goalie=None),  # no boxscore -> keep previous starter
        ]
        replay(records, EloBook(config=ELO_CONFIG), _new_rolling(),
               goalies=goalies, team_goalie=team_goalie)
        assert team_goalie["Boston Bruins"] == "3"
        assert team_goalie["Toronto Maple Leafs"] == "2"
        assert set(goalies) == {"1", "2", "3"}


class TestPoisson:
    def test_bounds_and_monotonicity(self):
        assert _poisson_over_prob(0.0) == 0.0
        probs = [_poisson_over_prob(lam) for lam in (3.0, 5.0, 6.5, 9.0)]
        assert all(0.0 <= p <= 1.0 for p in probs)
        assert probs == sorted(probs)

    def test_known_value(self):
        # P(Poisson(6.5) > 6.5) = 1 - CDF(6; 6.5) ~= 0.4735.
        assert abs(_poisson_over_prob(6.5) - 0.4735) < 0.001


class TestPredict:
    def _engine_with_state(self, tmp_path) -> NhlEngine:
        engine = NhlEngine(state_path=tmp_path / "state.json", raw_dir=tmp_path / "raw")
        engine._loaded = True  # skip disk load; inject synthetic state
        records = [
            _record(i, "Boston Bruins", "Toronto Maple Leafs", 4, 2) for i in range(10)
        ]
        replay(records, engine._book, engine._rolling)
        return engine

    def test_probabilities_sum_to_one_and_totals_positive(self, tmp_path):
        engine = self._engine_with_state(tmp_path)
        games = [
            Game(
                id="nhl-1",
                sport=Sport.NHL,
                start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                home="Boston Bruins",
                away="Toronto Maple Leafs",
            ),
            Game(  # both teams unseen -> home ice only, league-average total
                id="nhl-2",
                sport=Sport.NHL,
                start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                home="Seattle Kraken",
                away="Anaheim Ducks",
            ),
        ]
        preds = asyncio.run(engine.predict(games))
        assert len(preds) == 2
        for pred in preds:
            assert 0.0 < pred.home_win_prob < 1.0
            assert abs(pred.home_win_prob + pred.away_win_prob - 1.0) < 1e-9
            assert pred.draw_prob is None
            if pred.predicted_total is not None:  # unknown teams carry no total
                assert pred.predicted_total > 0
                assert 0.0 <= pred.over_prob <= 1.0
            expected_model = "nhl-elo-v2" if nhl.GOALIE_ADJ_ENABLED else "nhl-elo-v1"
            assert pred.model == expected_model
        assert preds[0].home_win_prob > 0.5  # Bruins dominated the replay
        assert preds[1].predicted_total is None  # no scoring history: no placeholder total (2026-09-02)

    def test_fallback_starter_resolution(self, tmp_path, monkeypatch):
        """Predict prices each team's most recent replay starter; unknown -> 0."""
        monkeypatch.setattr(nhl, "GOALIE_ADJ_ENABLED", True)
        engine = self._engine_with_state(tmp_path)
        engine._goalies = {
            "1": {"name": "Elite", "ewma": 0.925, "starts": 30},
            "2": {"name": "Sieve", "ewma": 0.875, "starts": 30},
        }
        engine._team_goalie = {"Boston Bruins": "1", "Toronto Maple Leafs": "2"}
        game = Game(
            id="nhl-1",
            sport=Sport.NHL,
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            home="Boston Bruins",
            away="Toronto Maple Leafs",
        )
        cold = Game(  # neither team has a known starter -> pure Elo
            id="nhl-2",
            sport=Sport.NHL,
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            home="Seattle Kraken",
            away="Anaheim Ducks",
        )
        with_goalie, pure = asyncio.run(engine.predict([game, cold]))
        assert with_goalie.home_win_prob > engine._book.expected(
            "Boston Bruins", "Toronto Maple Leafs"
        )
        assert pure.home_win_prob == engine._book.expected(
            "Seattle Kraken", "Anaheim Ducks"
        )
        # Gated off, the adjustment vanishes entirely.
        monkeypatch.setattr(nhl, "GOALIE_ADJ_ENABLED", False)
        gated, _ = asyncio.run(engine.predict([game, cold]))
        assert gated.home_win_prob == engine._book.expected(
            "Boston Bruins", "Toronto Maple Leafs"
        )

    def test_state_round_trip(self, tmp_path):
        engine = self._engine_with_state(tmp_path)
        engine._processed = {1, 2, 3}
        engine._last_season = 20242025
        engine._goalies = {"1": {"name": "Elite", "ewma": 0.921, "starts": 12}}
        engine._team_goalie = {"Boston Bruins": "1"}
        engine._save_state()

        reloaded = NhlEngine(state_path=engine._state_path, raw_dir=tmp_path / "raw")
        reloaded._ensure_loaded()
        assert reloaded._book.ratings == engine._book.ratings
        assert reloaded._processed == {1, 2, 3}
        assert reloaded._last_season == 20242025
        assert reloaded._goalies == engine._goalies
        assert reloaded._team_goalie == engine._team_goalie
        assert _predicted_goals(reloaded._rolling, "Boston Bruins", "Toronto Maple Leafs") == \
            _predicted_goals(engine._rolling, "Boston Bruins", "Toronto Maple Leafs")

    def test_pre_goalie_v1_state_is_ignored(self, tmp_path):
        """A v1 state file (no goalie keys) forces a clean rebuild."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({
            "model": "nhl-elo-v1",
            "last_season": 20242025,
            "ratings": {"Boston Bruins": 1600.0},
            "rolling": {"Boston Bruins": [[4, 2]]},
            "processed_ids": [1, 2, 3],
        }), encoding="utf-8")
        engine = NhlEngine(state_path=state_path, raw_dir=tmp_path / "raw")
        engine._ensure_loaded()
        assert engine._processed == set()  # v1 games will be replayed afresh
        assert engine._book.ratings == {}
        assert engine._goalies == {}


class TestSeasonHelpers:
    def test_past_vs_current_season(self):
        from datetime import date

        july_2026 = date(2026, 7, 10)
        assert nhl._is_past_season("20242025", july_2026)
        assert not nhl._is_past_season("20252026", july_2026)
        # After August the next season becomes current.
        sept_2026 = date(2026, 9, 1)
        assert nhl._is_past_season("20252026", sept_2026)

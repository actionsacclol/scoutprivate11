"""No-network unit tests for the MLB Elo + starting-pitcher engine."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone

import pytest

from kryptscout.core.elo import EloBook
from kryptscout.core.schemas import Game, Sport
from kryptscout.sports import mlb
from kryptscout.sports.mlb import (
    ELO_CONFIG,
    LEAGUE_AVG_RUNS,
    PITCHER_ADJ_CAP,
    MlbEngine,
    _chunk_path,
    _compute_metrics,
    _expected_home_prob,
    _fetch_chunk,
    _legacy_chunk_path,
    _parse_schedule,
    _pitcher_adj,
    _record_start,
    _replay,
    _season_months,
    build_engine,
)


def _game(pk: int, when: str, home: str, away: str, hs=None, aws=None,
          state: str = "F", game_type: str = "R",
          home_pp: tuple | None = None, away_pp: tuple | None = None) -> dict:
    """One statsapi schedule game entry, final by default.

    home_pp/away_pp are optional (id, fullName) probable-pitcher tuples,
    matching the hydrate=probablePitcher payload shape.
    """
    def side(name, score, pp):
        entry: dict = {"team": {"name": name}}
        if score is not None:
            entry["score"] = score
        if pp is not None:
            entry["probablePitcher"] = {"id": pp[0], "fullName": pp[1]}
        return entry

    return {
        "gamePk": pk,
        "gameType": game_type,
        "officialDate": when,
        "status": {"codedGameState": state},
        "teams": {
            "home": side(home, hs, home_pp),
            "away": side(away, aws, away_pp),
        },
    }


def _payload(games: list[dict]) -> dict:
    """Canned statsapi schedule payload grouping games by officialDate."""
    by_date: dict[str, list[dict]] = {}
    for g in games:
        by_date.setdefault(g["officialDate"], []).append(g)
    return {
        "dates": [{"date": d, "games": gs} for d, gs in sorted(by_date.items())]
    }


async def _no_probables(dates):
    return {}


@pytest.fixture
def offline_probables(monkeypatch):
    """predict() looks up the slate's probable pitchers — stub the network."""
    monkeypatch.setattr(mlb, "_fetch_probables", _no_probables)


# -- schedule parsing ---------------------------------------------------------


def test_parse_keeps_only_finals_of_expected_game_types():
    payload = _payload(
        [
            _game(1, "2026-04-01", "New York Yankees", "Boston Red Sox", 5, 3),
            _game(2, "2026-04-01", "Chicago Cubs", "St. Louis Cardinals",
                  state="I"),  # in progress
            _game(3, "2026-04-01", "Miami Marlins", "New York Mets",
                  state="P"),  # scheduled
            _game(4, "2026-04-01", "Texas Rangers", "Houston Astros", 7, 2,
                  game_type="S"),  # spring training sneaking through
            _game(5, "2026-10-24", "Los Angeles Dodgers", "Toronto Blue Jays",
                  4, 2, game_type="W"),  # World Series counts
        ]
    )
    rows = _parse_schedule(payload)
    assert [r["game_pk"] for r in rows] == [1, 5]
    assert rows[0] == {
        "game_pk": 1,
        "date": "2026-04-01",
        "season": 2026,
        "home": "New York Yankees",
        "away": "Boston Red Sox",
        "home_score": 5,
        "away_score": 3,
        "home_pitcher_id": None,
        "home_pitcher_name": None,
        "away_pitcher_id": None,
        "away_pitcher_name": None,
    }


def test_parse_extracts_probable_pitchers_when_hydrated():
    payload = _payload(
        [
            _game(8, "2026-04-02", "New York Yankees", "Boston Red Sox", 2, 1,
                  home_pp=(543037, "Gerrit Cole"), away_pp=(678394, "Kutter Crawford")),
        ]
    )
    [row] = _parse_schedule(payload)
    assert row["home_pitcher_id"] == 543037
    assert row["home_pitcher_name"] == "Gerrit Cole"
    assert row["away_pitcher_id"] == 678394
    assert row["away_pitcher_name"] == "Kutter Crawford"


def test_parse_canonicalizes_2024_athletics_name():
    payload = _payload(
        [_game(9, "2024-06-01", "Oakland Athletics", "Seattle Mariners", 2, 1)]
    )
    [row] = _parse_schedule(payload)
    assert row["home"] == "Athletics"  # ESPN/statsapi 2025+ name


def test_parse_skips_finals_missing_a_score():
    payload = _payload(
        [_game(7, "2026-05-01", "Detroit Tigers", "Cleveland Guardians", 3, None)]
    )
    assert _parse_schedule(payload) == []


def test_season_months_never_include_future_months():
    assert _season_months(2024, date(2026, 7, 10)) == [
        (2024, m) for m in range(3, 12)
    ]
    assert _season_months(2026, date(2026, 7, 10)) == [
        (2026, m) for m in range(3, 8)
    ]


# -- cache versioning ---------------------------------------------------------


def test_fetch_chunk_serves_v2_cache_and_deletes_legacy(tmp_path, monkeypatch):
    """A cached v2 month is served without a client; the v1 file is removed."""
    monkeypatch.setattr(mlb, "RAW_MLB_DIR", tmp_path)
    payload = _payload(
        [_game(1, "2024-05-01", "New York Yankees", "Boston Red Sox", 5, 3)]
    )
    _chunk_path(2024, 5).write_text(json.dumps(payload))
    legacy = _legacy_chunk_path(2024, 5)
    legacy.write_text(json.dumps({"dates": []}))  # v1: no pitcher hydrate

    got = asyncio.run(_fetch_chunk(None, 2024, 5, date(2026, 7, 10)))
    assert got == payload
    assert not legacy.exists()


def test_fetch_chunk_requests_pitcher_hydrate_and_writes_v2(tmp_path, monkeypatch):
    monkeypatch.setattr(mlb, "RAW_MLB_DIR", tmp_path)
    payload = _payload(
        [_game(1, "2024-05-01", "New York Yankees", "Boston Red Sox", 5, 3)]
    )

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def get(self, url, params=None):
            self.calls.append((url, params))
            return FakeResp()

    client = FakeClient()
    got = asyncio.run(_fetch_chunk(client, 2024, 5, date(2026, 7, 10)))
    assert got == payload
    [(_, params)] = client.calls
    assert params["hydrate"] == "probablePitcher"
    assert json.loads(_chunk_path(2024, 5).read_text()) == payload


# -- pitcher ratings ------------------------------------------------------------


def test_record_start_shrinks_from_league_average_prior():
    pitchers: dict[str, dict] = {}
    _record_start(pitchers, 100, "Ace Starter", 0, span=15)
    alpha = 2.0 / 16.0
    entry = pitchers["100"]
    assert entry["name"] == "Ace Starter"
    assert entry["starts"] == 1
    # One shutout start only moves the EWMA alpha of the way off the prior.
    assert entry["ewma"] == pytest.approx(LEAGUE_AVG_RUNS * (1 - alpha))

    _record_start(pitchers, 100, "Ace Starter", 8, span=15)
    assert entry["starts"] == 2
    expected = LEAGUE_AVG_RUNS * (1 - alpha)
    expected += alpha * (8 - expected)
    assert entry["ewma"] == pytest.approx(expected)


def test_pitcher_adj_sign_scale_and_missing_pitcher_zero():
    pitchers = {
        "1": {"name": "Good", "ewma": LEAGUE_AVG_RUNS - 1.0, "starts": 20},
        "2": {"name": "Bad", "ewma": LEAGUE_AVG_RUNS + 2.0, "starts": 20},
    }
    assert _pitcher_adj(pitchers, 1, k_pitcher=16.0) == pytest.approx(16.0)
    assert _pitcher_adj(pitchers, 2, k_pitcher=16.0) == pytest.approx(-32.0)
    assert _pitcher_adj(pitchers, None) == 0.0  # no probable listed
    assert _pitcher_adj(pitchers, 999) == 0.0  # never seen a start


def test_pitcher_adj_is_capped():
    pitchers: dict[str, dict] = {}
    _record_start(pitchers, 7, "Peak Pedro", 0, span=1)  # alpha=1: ewma -> 0
    assert pitchers["7"]["ewma"] == pytest.approx(0.0)
    raw = 20.0 * LEAGUE_AVG_RUNS  # 88 points, past the cap
    assert raw > PITCHER_ADJ_CAP
    assert _pitcher_adj(pitchers, 7, k_pitcher=20.0) == pytest.approx(PITCHER_ADJ_CAP)
    pitchers["7"]["ewma"] = 2 * LEAGUE_AVG_RUNS + 10.0
    assert _pitcher_adj(pitchers, 7, k_pitcher=20.0) == pytest.approx(-PITCHER_ADJ_CAP)


# -- replay -------------------------------------------------------------------


def _rows(specs: list[tuple]) -> list[dict]:
    """Bare rows without pitcher info — exercises the missing-pitcher path."""
    return [
        {
            "game_pk": pk,
            "date": when,
            "season": int(when[:4]),
            "home": home,
            "away": away,
            "home_score": hs,
            "away_score": aws,
        }
        for pk, when, home, away, hs, aws in specs
    ]


def test_replay_moves_ratings_and_tracks_rolling_runs():
    rows = _rows(
        [
            (1, "2026-04-01", "New York Yankees", "Boston Red Sox", 6, 2),
            (2, "2026-04-02", "Boston Red Sox", "New York Yankees", 3, 5),
        ]
    )
    book, rolling, pitchers, metrics = _replay(rows)
    assert book.get("New York Yankees") > ELO_CONFIG.initial
    assert book.get("Boston Red Sox") < ELO_CONFIG.initial
    assert list(rolling["New York Yankees"]) == [(6, 2), (5, 3)]
    assert list(rolling["Boston Red Sox"]) == [(2, 6), (3, 5)]
    assert pitchers == {}  # no starters listed anywhere
    assert metrics["n_games"] == 2  # 2026 games feed validation
    assert metrics["pitcher_coverage"] == 0.0


def test_replay_dedupes_resumed_games_by_game_pk():
    rows = _rows(
        [
            (1, "2026-04-01", "New York Yankees", "Boston Red Sox", 6, 2),
            (1, "2026-04-02", "New York Yankees", "Boston Red Sox", 6, 2),
        ]
    )
    book, rolling, _, metrics = _replay(rows)
    assert metrics["n_games"] == 1
    assert len(rolling["New York Yankees"]) == 1

    ref = EloBook(config=ELO_CONFIG)
    ref.update("New York Yankees", "Boston Red Sox", True, margin=4)
    assert book.get("New York Yankees") == pytest.approx(
        ref.get("New York Yankees")
    )


def test_replay_applies_season_carry_at_boundary():
    rows = _rows(
        [
            (1, "2025-09-20", "New York Yankees", "Boston Red Sox", 6, 2),
            (2, "2026-04-01", "Chicago Cubs", "Cincinnati Reds", 4, 3),
        ]
    )
    book, _, _, _ = _replay(rows)

    ref = EloBook(config=ELO_CONFIG)
    ref.update("New York Yankees", "Boston Red Sox", True, margin=4)
    expected = ELO_CONFIG.initial + ELO_CONFIG.season_carry * (
        ref.get("New York Yankees") - ELO_CONFIG.initial
    )
    assert book.get("New York Yankees") == pytest.approx(expected)


def test_replay_validation_uses_pre_update_probabilities():
    import math

    rows = _rows(
        [
            (1, "2026-04-01", "New York Yankees", "Boston Red Sox", 6, 2),
            (2, "2026-04-02", "New York Yankees", "Boston Red Sox", 6, 2),
        ]
    )
    _, _, _, metrics = _replay(rows)

    # Replicate by hand: record 1 is the cold-start probability, record 2 the
    # probability after exactly one update — never a game's own result.
    ref = EloBook(config=ELO_CONFIG)
    p1 = ref.expected("New York Yankees", "Boston Red Sox")
    ref.update("New York Yankees", "Boston Red Sox", True, margin=4)
    p2 = ref.expected("New York Yankees", "Boston Red Sox")
    assert metrics["elo_log_loss"] == pytest.approx(
        -(math.log(p1) + math.log(p2)) / 2
    )
    assert metrics["home_win_rate"] == 1.0


def _pitcher_rows(specs: list[tuple]) -> list[dict]:
    """Rows with (pk, date, home, away, hs, aws, home_pid, away_pid)."""
    rows = _rows([s[:6] for s in specs])
    for row, spec in zip(rows, specs):
        row["home_pitcher_id"] = spec[6]
        row["home_pitcher_name"] = f"Pitcher {spec[6]}" if spec[6] else None
        row["away_pitcher_id"] = spec[7]
        row["away_pitcher_name"] = f"Pitcher {spec[7]}" if spec[7] else None
    return rows


def test_replay_dominant_pitcher_lifts_walk_forward_win_prob():
    import math

    # Ace (id 100) throws four shutouts in 2025, then starts the lone 2026
    # validation game. Team Elo updates never see the pitcher overlay, so the
    # k_pitcher=0 replay is an exact baseline for the same rows.
    specs = [
        (1, "2025-05-01", "Seattle Mariners", "Texas Rangers", 5, 0, 100, None),
        (2, "2025-05-06", "Seattle Mariners", "Texas Rangers", 3, 0, 100, None),
        (3, "2025-05-11", "Seattle Mariners", "Texas Rangers", 4, 0, 100, None),
        (4, "2025-05-16", "Seattle Mariners", "Texas Rangers", 2, 0, 100, None),
        (5, "2026-04-01", "Seattle Mariners", "Texas Rangers", 1, 0, 100, None),
    ]
    rows = _pitcher_rows(specs)
    _, _, pitchers, with_adj = _replay(rows, k_pitcher=16.0, pitcher_span=15)
    _, _, _, without = _replay(rows, k_pitcher=0.0)
    assert with_adj["n_games"] == without["n_games"] == 1
    # Home team won, so a higher pre-game home prob means a lower log loss.
    p_with = math.exp(-with_adj["elo_log_loss"])
    p_without = math.exp(-without["elo_log_loss"])
    assert p_with > p_without

    # And the lift is exactly the Elo-point adjustment from PRIOR starts only:
    # four shutouts pull the EWMA down by (1 - alpha)^4 from the prior.
    alpha = 2.0 / 16.0
    ewma_before_game5 = LEAGUE_AVG_RUNS * (1 - alpha) ** 4
    adj = 16.0 * (LEAGUE_AVG_RUNS - ewma_before_game5)
    assert pitchers["100"]["starts"] == 5  # game 5's shutout folded in AFTER scoring
    assert with_adj["pitcher_coverage"] == 0.0  # away starter unknown
    assert math.log10(p_with / (1 - p_with)) - math.log10(
        p_without / (1 - p_without)
    ) == pytest.approx(adj / 400.0)


def test_replay_debut_pitcher_gets_zero_adjustment():
    # Both starters are listed but neither has a prior start: walk-forward
    # probability must equal the pitcher-less Elo expectation exactly.
    rows = _pitcher_rows(
        [(1, "2026-04-01", "New York Yankees", "Boston Red Sox", 6, 2, 41, 42)]
    )
    import math

    _, _, _, metrics = _replay(rows, k_pitcher=16.0)
    ref = EloBook(config=ELO_CONFIG)
    assert math.exp(-metrics["elo_log_loss"]) == pytest.approx(
        ref.expected("New York Yankees", "Boston Red Sox")
    )
    assert metrics["pitcher_coverage"] == 1.0


# -- metrics --------------------------------------------------------------------


def test_metrics_known_values():
    records = [
        {"p": 0.8, "home_won": True, "both_pitchers": True},
        {"p": 0.3, "home_won": False, "both_pitchers": False},
        {"p": 0.6, "home_won": False},  # pitcher flag optional
    ]
    m = _compute_metrics(records)
    assert m["n_games"] == 3
    assert m["elo_accuracy"] == pytest.approx(2 / 3)
    assert m["home_win_rate"] == pytest.approx(1 / 3)
    assert m["pitcher_coverage"] == pytest.approx(1 / 3)
    import math

    expected_ll = -(math.log(0.8) + math.log(0.7) + math.log(0.4)) / 3
    assert m["elo_log_loss"] == pytest.approx(expected_ll)


def test_metrics_empty_records():
    assert _compute_metrics([]) == {}


# -- prediction -----------------------------------------------------------------


def test_predict_probabilities_and_runs(tmp_path, offline_probables):
    engine = MlbEngine(state_path=tmp_path / "mlb_state.json")
    engine.book.ratings = {"Los Angeles Dodgers": 1580.0, "Colorado Rockies": 1420.0}
    engine.rolling = {
        "Los Angeles Dodgers": mlb.deque([(6, 3), (4, 5)], maxlen=20),
        "Colorado Rockies": mlb.deque([(3, 7), (5, 9)], maxlen=20),
    }
    game = Game(
        id="mlb-test-1",
        sport=Sport.MLB,
        start_time=datetime(2026, 7, 10, 23, tzinfo=timezone.utc),
        home="Los Angeles Dodgers",
        away="Colorado Rockies",
    )
    [pred] = asyncio.run(engine.predict([game]))

    assert pred.model == "mlb-elo-v3"
    assert pred.sport is Sport.MLB
    assert 0.0 <= pred.home_win_prob <= 1.0
    assert pred.home_win_prob + pred.away_win_prob == pytest.approx(1.0)
    # 160 Elo points plus 24 home advantage: solid but not extreme favorite.
    assert 0.5 < pred.home_win_prob < 0.9
    # home = (LAD RF 5 + COL RA 8)/2 = 6.5; away = (COL RF 4 + LAD RA 4)/2 = 4.0
    assert pred.predicted_home_score == pytest.approx(6.5)
    assert pred.predicted_away_score == pytest.approx(4.0)
    assert pred.predicted_total == pytest.approx(10.5)
    assert pred.confidence == pytest.approx(abs(pred.home_win_prob - 0.5) * 2)


def test_predict_unknown_team_falls_back_to_league_average(tmp_path, offline_probables):
    engine = MlbEngine(state_path=tmp_path / "mlb_state.json")
    game = Game(
        id="mlb-test-2",
        sport=Sport.MLB,
        start_time=datetime(2026, 7, 10, 23, tzinfo=timezone.utc),
        home="Los Angeles Dodgers",
        away="Colorado Rockies",
    )
    [pred] = asyncio.run(engine.predict([game]))
    assert pred.home_win_prob == pytest.approx(
        1.0 / (1.0 + 10.0 ** (-ELO_CONFIG.home_advantage / 400.0))
    )
    assert pred.predicted_total == pytest.approx(2 * LEAGUE_AVG_RUNS)


def test_predict_applies_probable_pitcher_adjustment(tmp_path, monkeypatch):
    engine = MlbEngine(state_path=tmp_path / "mlb_state.json")
    engine.pitchers = {
        "100": {"name": "Home Ace", "ewma": LEAGUE_AVG_RUNS - 2.0, "starts": 25},
        "200": {"name": "Away Arm", "ewma": LEAGUE_AVG_RUNS + 1.0, "starts": 25},
    }

    async def canned(dates):
        assert "2026-07-10" in dates  # slate date requested
        return {
            ("2026-07-10", "Los Angeles Dodgers"): 100,
            ("2026-07-10", "Colorado Rockies"): 200,
        }

    monkeypatch.setattr(mlb, "_fetch_probables", canned)
    game = Game(
        id="mlb-test-3",
        sport=Sport.MLB,
        start_time=datetime(2026, 7, 10, 23, tzinfo=timezone.utc),
        home="Los Angeles Dodgers",
        away="Colorado Rockies",
    )
    [pred] = asyncio.run(engine.predict([game]))
    diff = _pitcher_adj(engine.pitchers, 100) - _pitcher_adj(engine.pitchers, 200)
    assert diff > 0
    assert pred.home_win_prob == pytest.approx(
        _expected_home_prob(engine.book, game.home, game.away, diff)
    )


def test_predict_degrades_to_zero_adjustment_when_probables_unavailable(
    tmp_path, offline_probables
):
    engine = MlbEngine(state_path=tmp_path / "mlb_state.json")
    engine.pitchers = {
        "100": {"name": "Home Ace", "ewma": LEAGUE_AVG_RUNS - 2.0, "starts": 25}
    }
    game = Game(
        id="mlb-test-4",
        sport=Sport.MLB,
        start_time=datetime(2026, 7, 10, 23, tzinfo=timezone.utc),
        home="Los Angeles Dodgers",
        away="Colorado Rockies",
    )
    [pred] = asyncio.run(engine.predict([game]))
    assert pred.home_win_prob == pytest.approx(
        engine.book.expected("Los Angeles Dodgers", "Colorado Rockies")
    )


# -- state round-trip -------------------------------------------------------------


def test_state_persists_and_reloads(tmp_path):
    path = tmp_path / "mlb_state.json"
    engine = MlbEngine(state_path=path)
    engine.book.ratings = {"Milwaukee Brewers": 1571.25}
    engine.rolling = {"Milwaukee Brewers": mlb.deque([(9, 1)], maxlen=20)}
    engine.pitchers = {
        "669373": {"name": "Tarik Skubal", "ewma": 2.35, "starts": 41}
    }
    engine.metrics = {"elo_accuracy": 0.55}
    engine._save_state()

    reloaded = MlbEngine(state_path=path)
    assert reloaded.book.get("Milwaukee Brewers") == pytest.approx(1571.25)
    assert list(reloaded.rolling["Milwaukee Brewers"]) == [(9, 1)]
    assert reloaded.pitchers == {
        "669373": {"name": "Tarik Skubal", "ewma": 2.35, "starts": 41}
    }
    assert reloaded.metrics["elo_accuracy"] == 0.55


def test_state_without_pitchers_loads_empty(tmp_path):
    # A v1 state file (pre-pitcher) must load cleanly with zero pitcher adj.
    path = tmp_path / "mlb_state.json"
    path.write_text(json.dumps({"ratings": {"Athletics": 1490.0}, "rolling": {}}))
    engine = MlbEngine(state_path=path)
    assert engine.pitchers == {}
    assert _pitcher_adj(engine.pitchers, 12345) == 0.0


def test_build_engine_contract():
    engine = build_engine()
    assert engine.sport is Sport.MLB
    assert engine.model_name == "mlb-elo-v3"

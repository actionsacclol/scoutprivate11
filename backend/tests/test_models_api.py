"""MODELS panel API tests — TestClient with fake engines and tmp artifact dirs.

No network and no real engines: the registry is monkeypatched with fakes,
config.DATA_DIR / config.MODELS_DIR point into tmp_path (jobs.refresh and the
endpoints read them at call time), and tracker.performance is stubbed. App
startup is disabled so the real sport modules are never imported.
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from kryptscout import config
from kryptscout.api import main
from kryptscout.core.schemas import Sport
from kryptscout.sports import base as engines
from kryptscout.store import tracker

METRICS = {"overall": {"games": 120, "accuracy": 0.63, "log_loss": 0.641}}
PERF_ROW = {
    "sport": "wnba", "model": "wnba-fake-v1",
    "n": 12, "accuracy": 0.583, "log_loss": 0.672, "brier": 0.241,
}


class FakeEngine:
    """Duck-typed SportEngine whose refresh_data behavior is injectable."""

    def __init__(self, sport: Sport, model_name: str, refresh=None) -> None:
        self.sport = sport
        self.model_name = model_name
        self._refresh = refresh
        self.refresh_calls = 0

    async def upcoming_games(self, on=None):
        return []

    async def predict(self, games):
        return []

    async def refresh_data(self) -> None:
        self.refresh_calls += 1
        if self._refresh is not None:
            self._refresh()


def _write_state(sport: Sport, payload: dict) -> None:
    (config.DATA_DIR / f"{sport.value}_state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _refresh_ok() -> None:
    """A successful refresh: logs like an engine, then persists state."""
    logging.getLogger("kryptscout.sports.fake").info("fetched 3 new days")
    _write_state(Sport.WNBA, {"ratings": {"A": 1500.0, "B": 1520.0}})


def _refresh_boom() -> None:
    raise RuntimeError("scoreboard on fire")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    models_dir = tmp_path / "models"
    data_dir.mkdir()
    models_dir.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "MODELS_DIR", models_dir)
    # Skip _load_engines (would import every real sport module) and serve
    # from a registry holding only fakes.
    monkeypatch.setattr(main.app.router, "on_startup", [])
    monkeypatch.setattr(
        engines,
        "_REGISTRY",
        {
            Sport.WNBA: FakeEngine(Sport.WNBA, "wnba-fake-v1", _refresh_ok),
            Sport.MLB: FakeEngine(Sport.MLB, "mlb-fake-v1", _refresh_boom),
        },
    )
    monkeypatch.setattr(
        tracker,
        "performance",
        lambda sport=None, model=None, since=None: {
            "overall": [PERF_ROW], "daily": [],
        },
    )
    with TestClient(main.app) as test_client:
        yield test_client


# -------------------------------------------------------------- GET /api/models


def test_models_full_row(client):
    _write_state(Sport.WNBA, {"ratings": {"A": 1500.0}, "metrics": METRICS})
    (config.MODELS_DIR / "wnba_xgb_v9.pkl").write_bytes(b"x" * 2048)

    resp = client.get("/api/models")
    assert resp.status_code == 200
    rows = {row["sport"]: row for row in resp.json()["models"]}
    assert set(rows) == {"wnba", "mlb"}

    wnba = rows["wnba"]
    assert wnba["model"] == "wnba-fake-v1"
    assert wnba["state"]["exists"] is True
    assert wnba["state"]["file"] == "wnba_state.json"
    assert wnba["state"]["size_kb"] > 0
    assert wnba["state"]["updated_at"].startswith("20")  # ISO timestamp
    assert wnba["pickle"]["file"] == "wnba_xgb_v9.pkl"
    assert wnba["pickle"]["size_kb"] == 2.0
    assert wnba["live"] == {"n": 12, "accuracy": 0.583, "log_loss": 0.672,
                            "brier": 0.241}
    assert wnba["backtest"] == METRICS


def test_models_missing_artifacts_are_null(client):
    # MLB has no state file, no pickle, and no performance row.
    resp = client.get("/api/models")
    mlb = next(r for r in resp.json()["models"] if r["sport"] == "mlb")
    assert mlb["state"] == {
        "exists": False, "file": "mlb_state.json",
        "size_kb": None, "updated_at": None,
    }
    assert mlb["pickle"] is None
    assert mlb["live"] is None
    assert mlb["backtest"] is None


def test_models_backtest_dict_vs_raw_log(client):
    # tennis-style: "backtest" holds a metrics dict -> surfaced.
    _write_state(Sport.MLB, {"backtest": {"overall": {"accuracy": 0.55}}})
    # nba/wnba-style: "backtest" is the raw per-game log (a list) -> null.
    _write_state(Sport.WNBA, {"backtest": [["2026-07-01", 0.61, 1]]})

    rows = {r["sport"]: r for r in client.get("/api/models").json()["models"]}
    assert rows["mlb"]["backtest"] == {"overall": {"accuracy": 0.55}}
    assert rows["wnba"]["backtest"] is None


# ----------------------------------------------- POST /api/models/{sport}/refresh


def test_refresh_success_captures_logs_and_state(client):
    resp = client.post("/api/models/wnba/refresh")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sport"] == "wnba"
    assert body["ok"] is True
    assert body["seconds"] >= 0
    joined = "\n".join(body["log"])
    assert "fetched 3 new days" in joined  # captured "kryptscout" logging
    assert "state written" in joined and "2 ratings" in joined
    assert body["state"]["exists"] is True
    assert body["state"]["file"] == "wnba_state.json"
    assert engines.get_engine(Sport.WNBA).refresh_calls == 1


def test_refresh_failure_is_ok_false_not_500(client):
    resp = client.post("/api/models/mlb/refresh")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "RuntimeError: scoreboard on fire" in "\n".join(body["log"])
    assert "scoreboard on fire" in body["summary"]
    assert body["state"]["exists"] is False  # the failed refresh wrote nothing


def test_refresh_that_writes_nothing_does_not_claim_state_written(client, monkeypatch):
    """An engine already current through its history window (nba in the
    offseason) writes no state. Reporting "state written" there reads as a
    successful refresh while the panel's "updated N days ago" keeps climbing."""
    engine = engines.get_engine(Sport.WNBA)
    monkeypatch.setattr(engine, "_refresh", None)  # runs, writes nothing
    _write_state(Sport.WNBA, {"ratings": {"A": 1500.0}})

    body = client.post("/api/models/wnba/refresh").json()

    assert body["ok"] is True
    assert "state written" not in body["summary"]
    assert "no new results" in body["summary"]
    assert "already current" in body["summary"]


def test_refresh_unknown_engine_404(client):
    assert client.post("/api/models/nhl/refresh").status_code == 404


def test_refresh_bad_sport_422(client):
    assert client.post("/api/models/curling/refresh").status_code == 422


def test_force_retrain_deletes_only_this_sports_pickles(client):
    wnba_a = config.MODELS_DIR / "wnba_xgb_v1.pkl"
    wnba_b = config.MODELS_DIR / "wnba_elo_cache.pkl"
    other = config.MODELS_DIR / "mlb_xgb_v1.pkl"
    for path in (wnba_a, wnba_b, other):
        path.write_bytes(b"pickle")

    resp = client.post("/api/models/wnba/refresh", json={"force_retrain": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert not wnba_a.exists() and not wnba_b.exists()
    assert other.exists()  # another sport's pickle untouched
    joined = "\n".join(body["log"])
    assert "deleted wnba_elo_cache.pkl (force_retrain)" in joined
    assert "deleted wnba_xgb_v1.pkl (force_retrain)" in joined


def test_refresh_without_force_keeps_pickle(client):
    pickle = config.MODELS_DIR / "wnba_xgb_v1.pkl"
    pickle.write_bytes(b"pickle")
    resp = client.post("/api/models/wnba/refresh", json={"force_retrain": False})
    assert resp.status_code == 200
    assert pickle.exists()
    assert "deleted" not in "\n".join(resp.json()["log"])


# ---------------------------------------------------------- POST /api/jobs/refresh


def test_jobs_refresh_aggregates_and_isolates_failures(client):
    resp = client.post("/api/jobs/refresh")
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert set(results) == {"wnba", "mlb"}
    for result in results.values():
        assert set(result) == {"ok", "seconds", "summary"}
    assert results["wnba"]["ok"] is True
    assert "state written" in results["wnba"]["summary"]
    assert results["mlb"]["ok"] is False  # one failure never sinks the rest
    assert "scoreboard on fire" in results["mlb"]["summary"]
    assert engines.get_engine(Sport.WNBA).refresh_calls == 1


# ------------------------------------------------- retired engine artifacts


def test_purge_removes_only_retired_artifacts(client) -> None:
    """The xgboost overlays are gone; their pickles outlive the update in
    userData and must stop showing up as a "model pickle" on two Elo engines."""
    from kryptscout.jobs import refresh as refresh_job

    for name in ("nba_xgb_v2.pkl", "nfl_xgb_v3.pkl", "nfl_xgb_v2.pkl"):
        (config.MODELS_DIR / name).write_bytes(b"x")
    keep = config.MODELS_DIR / "wnba_other_v1.pkl"
    keep.write_bytes(b"x")

    assert refresh_job.purge_retired_artifacts() == [
        "nba_xgb_v2.pkl", "nfl_xgb_v2.pkl", "nfl_xgb_v3.pkl",
    ]
    assert sorted(p.name for p in config.MODELS_DIR.iterdir()) == ["wnba_other_v1.pkl"]
    assert refresh_job.purge_retired_artifacts() == []  # idempotent

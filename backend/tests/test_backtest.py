"""Tests for store.backtest — the model-skill report over persisted logs.

Every test writes a fake {sport}_state.json into a monkeypatched DATA_DIR, so
nothing touches real data files or the network.
"""
from __future__ import annotations

import json
import math

import pytest

from kryptscout import config
from kryptscout.core.schemas import Sport
from kryptscout.store import backtest


@pytest.fixture(autouse=True)
def _tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def _write_state(data_dir, sport: Sport, state: dict) -> None:
    (data_dir / f"{sport.value}_state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )


def test_log_report_metrics_and_shape(_tmp_data_dir):
    # 4 games; predicted-side correct on 3 of them (rows 1,2,4).
    rows = [
        ["2025-01-01", 0.9, 1],   # confident, right
        ["2025-01-02", 0.8, 1],   # confident, right
        ["2025-01-03", 0.7, 0],   # confident, wrong
        ["2025-01-04", 0.2, 0],   # confident underdog call, right
    ]
    _write_state(_tmp_data_dir, Sport.NBA, {"model": "nba-elo-v1", "backtest": rows})

    r = backtest.report(Sport.NBA)
    assert r["sport"] == "nba"
    assert r["model"] == "nba-elo-v1"
    assert r["kind"] == "log"
    assert r["n"] == 4
    assert r["accuracy"] == pytest.approx(0.75)
    assert r["start"] == "2025-01-01" and r["end"] == "2025-01-04"

    # Brier = mean (p - y)^2.
    expected_brier = ((0.9 - 1) ** 2 + (0.8 - 1) ** 2 + (0.7 - 0) ** 2 + (0.2 - 0) ** 2) / 4
    assert r["brier"] == pytest.approx(expected_brier)
    assert r["log_loss"] > 0

    # Calibration always has 10 bins; the populated ones account for every row.
    assert len(r["calibration"]) == 10
    assert sum(b["n"] for b in r["calibration"]) == 4

    # Sweep: every-game row (0.50) counts all 4; higher floors never grow.
    sweep = {s["threshold"]: s for s in r["sweep"]}
    assert sweep[0.50]["n"] == 4
    assert sweep[0.50]["share"] == pytest.approx(1.0)
    ns = [s["n"] for s in sorted(r["sweep"], key=lambda s: s["threshold"])]
    assert ns == sorted(ns, reverse=True)


def test_log_report_filters_malformed_rows(_tmp_data_dir):
    rows = [
        ["2025-01-01", 0.6, 1],
        ["bad"],                    # too short
        ["2025-01-02", 1.4, 1],     # prob out of range
        ["2025-01-03", "x", 0],     # non-numeric prob
        ["2025-01-04", 0.3, 2],     # outcome not 0/1
        ["2025-01-05", 0.4, 0],
    ]
    _write_state(_tmp_data_dir, Sport.WNBA, {"backtest": rows})
    r = backtest.report(Sport.WNBA)
    assert r["kind"] == "log"
    assert r["n"] == 2  # only the two well-formed rows survive


def test_summary_flat_elo_metrics(_tmp_data_dir):
    _write_state(_tmp_data_dir, Sport.NFL, {
        "model": "nfl-elo",
        "metrics": {"n_games": 569, "elo_accuracy": 0.671, "elo_log_loss": 0.62},
    })
    r = backtest.report(Sport.NFL)
    assert r["kind"] == "summary"
    assert r["n"] == 569
    assert r["accuracy"] == pytest.approx(0.671)
    assert r["log_loss"] == pytest.approx(0.62)
    assert r["brier"] is None
    assert r["calibration"] is None and r["sweep"] is None
    assert r["summary"]["n_games"] == 569


def test_summary_nested_overall(_tmp_data_dir):
    _write_state(_tmp_data_dir, Sport.NCAAB, {
        "metrics": {
            "overall": {"games": 17265, "accuracy": 0.7086, "log_loss": 0.56},
            "2025": {"games": 5777, "accuracy": 0.7166, "log_loss": 0.55},
        },
    })
    r = backtest.report(Sport.NCAAB)
    assert r["kind"] == "summary"
    assert r["n"] == 17265
    assert r["accuracy"] == pytest.approx(0.7086)


def test_summary_per_tour_aggregates(_tmp_data_dir):
    _write_state(_tmp_data_dir, Sport.TENNIS, {
        "backtest": {
            "ATP": {"n": 4000, "accuracy": 0.60, "log_loss": 0.60},
            "WTA": {"n": 6000, "accuracy": 0.70, "log_loss": 0.70},
        },
    })
    r = backtest.report(Sport.TENNIS)
    assert r["kind"] == "summary"
    assert r["n"] == 10000
    # Sample-weighted: (4000*.6 + 6000*.7) / 10000 = 0.66
    assert r["accuracy"] == pytest.approx(0.66)
    assert r["log_loss"] == pytest.approx(0.66)


def test_missing_state_is_none(_tmp_data_dir):
    r = backtest.report(Sport.NHL)
    assert r["kind"] == "none"
    assert r["n"] is None
    assert r["note"]


def test_state_without_metrics_is_none(_tmp_data_dir):
    _write_state(_tmp_data_dir, Sport.SOCCER, {"fitted_at": "2026-01-01", "leagues": {}})
    r = backtest.report(Sport.SOCCER)
    assert r["kind"] == "none"


def test_model_falls_back_to_state_then_arg(_tmp_data_dir):
    _write_state(_tmp_data_dir, Sport.NBA, {"model": "state-model", "backtest": [["2025-01-01", 0.6, 1]]})
    # Explicit arg wins.
    assert backtest.report(Sport.NBA, model="engine-model")["model"] == "engine-model"
    # Else falls back to the state file's label.
    assert backtest.report(Sport.NBA)["model"] == "state-model"


def test_all_values_json_finite(_tmp_data_dir):
    _write_state(_tmp_data_dir, Sport.NBA, {"backtest": [["2025-01-01", 0.6, 1], ["2025-01-02", 0.4, 0]]})
    r = backtest.report(Sport.NBA)
    for v in (r["accuracy"], r["log_loss"], r["brier"]):
        assert v is not None and math.isfinite(v)

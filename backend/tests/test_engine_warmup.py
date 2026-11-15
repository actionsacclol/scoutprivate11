"""Engine warm-up must never block /api/health.

Importing the sports modules pulled numpy/pandas/sklearn/scipy/xgboost, ~25-35s
cold on a fresh macOS install (the last three are gone since November 2026). Doing it in a uvicorn startup handler meant
NOTHING was served until it finished — including the health probe the Electron
supervisor uses to decide the backend is dead. It SIGKILLed a backend that was
merely slow, respawned into a colder cache, and never converged: an install
that "didn't work". These tests pin the contract that made that impossible.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from kryptscout.api import main


def _wait_ready(client, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get("/api/health").json()["enginesReady"]:
            return True
        time.sleep(0.02)
    return False


def test_health_answers_while_engines_are_still_loading(monkeypatch):
    """Startup completes — and health answers — with the load still in flight."""
    main._engines_ready.clear()
    release = threading.Event()
    finished = threading.Event()

    def slow_load() -> None:
        release.wait(10)
        finished.set()

    monkeypatch.setattr(main, "_load_engines", slow_load)

    with TestClient(main.app) as client:
        # If startup blocked on the load, we would never reach this line.
        assert not finished.is_set()

        res = client.get("/api/health")
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["enginesReady"] is False

        release.set()
        assert _wait_ready(client), "enginesReady never flipped after the load landed"


def test_engine_endpoint_503s_when_warmup_is_wedged(monkeypatch):
    """A stuck import surfaces as a retryable 503, not a hang."""
    monkeypatch.setattr(main.app.router, "on_startup", [])
    monkeypatch.setattr(main, "ENGINE_WARMUP_TIMEOUT_S", 0.05)
    main._engines_ready.clear()

    with TestClient(main.app) as client:
        res = client.get("/api/sports")
        assert res.status_code == 503
        assert res.headers["Retry-After"] == "10"


def test_engine_endpoint_serves_once_ready(monkeypatch):
    """The gate is a wait, not a permanent gate: ready means serve normally."""
    monkeypatch.setattr(main.app.router, "on_startup", [])
    monkeypatch.setattr(main, "ENGINE_WARMUP_TIMEOUT_S", 0.05)
    main._engines_ready.set()

    with TestClient(main.app) as client:
        res = client.get("/api/sports")
        assert res.status_code == 200
        assert isinstance(res.json(), list)


def test_load_engines_logs_a_traceback_for_a_broken_sport(monkeypatch, caplog):
    """A frozen-bundle import failure is only diagnosable with the stack.

    The mac builds logged a bare "'NoneType' object has no attribute 'replace'"
    with no frame, for the two sports that imported xgboost — unactionable from a
    user's log export.
    """
    import importlib

    real_import = importlib.import_module

    def boom(name, *a, **k):
        if name == "kryptscout.sports.nba":
            raise AttributeError("'NoneType' object has no attribute 'replace'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", boom)

    with caplog.at_level("WARNING", logger="kryptscout"):
        main._load_engines()

    record = next(
        r for r in caplog.records if "engine nba unavailable" in r.getMessage()
    )
    assert record.exc_info is not None, "traceback was discarded"
    assert "AttributeError" in caplog.text

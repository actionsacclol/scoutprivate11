"""Local API token gate: when Electron passes KRYPT_SCOUT_API_TOKEN, every
/api call must carry it as X-Scout-Token. See api.main._api_token_gate."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kryptscout import license
from kryptscout.api import main

TOKEN = "a" * 64
HDR = {"X-Scout-Token": TOKEN}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main.app.router, "on_startup", [])
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def tokened(monkeypatch):
    """Simulate a supervisor-launched backend (token present in the env)."""
    monkeypatch.setattr(main, "_API_TOKEN", TOKEN)


def test_no_token_header_is_rejected(tokened, client):
    r = client.get("/api/performance")
    assert r.status_code == 401


def test_wrong_token_is_rejected(tokened, client):
    r = client.get("/api/performance", headers={"X-Scout-Token": "b" * 64})
    assert r.status_code == 401


def test_correct_token_passes(tokened, client):
    assert client.get("/api/performance", headers=HDR).status_code == 200


def test_health_stays_open(tokened, client):
    """The supervisor's liveness probe must answer even without a token."""
    assert client.get("/api/health").status_code == 200


def test_token_gate_runs_before_the_license_gate(tokened, client, monkeypatch):
    """Ordering matters: an unauthenticated caller gets 401, never a 402 that
    would tell them the app is merely unlicensed."""
    monkeypatch.setattr(license, "is_licensed", lambda *a, **k: False)
    assert client.get("/api/performance").status_code == 401
    assert client.get("/api/performance", headers=HDR).status_code == 402


def test_no_token_configured_means_auth_off(client, monkeypatch):
    """pytest / hand-run uvicorn / vite dev: no env token, no ceremony."""
    monkeypatch.setattr(main, "_API_TOKEN", "")
    assert client.get("/api/performance").status_code == 200

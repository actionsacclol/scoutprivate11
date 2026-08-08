"""App-wide license gate: without a license the API serves only health +
the license endpoints; everything else is 402. See api.main._license_gate."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kryptscout import license
from kryptscout.api import main


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main.app.router, "on_startup", [])
    with TestClient(main.app) as c:
        yield c


def test_gate_blocks_data_endpoints_when_unlicensed(monkeypatch, client):
    monkeypatch.setattr(license, "is_licensed", lambda *a, **k: False)
    # Open endpoints stay reachable so the user can activate + the UI can poll.
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/license").status_code == 200
    # Everything else is locked.
    for path in ("/api/performance", "/api/roi", "/api/autobet", "/api/mybets"):
        r = client.get(path)
        assert r.status_code == 402, path
        assert r.json().get("licenseRequired") is True


def test_gate_allows_data_endpoints_when_licensed(monkeypatch, client):
    monkeypatch.setattr(license, "is_licensed", lambda *a, **k: True)
    assert client.get("/api/performance").status_code == 200
    assert client.get("/api/autobet").status_code == 200

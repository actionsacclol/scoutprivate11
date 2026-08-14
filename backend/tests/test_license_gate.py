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


def test_pubkey_fingerprints_agree_across_modules():
    """The anti-shim check is a constant duplicated into two compiled modules.

    If the signing key is rotated and only license._PUBLIC_KEY_B64 is updated,
    every install 402s and every live sweep refuses — a total outage with no
    error message pointing anywhere near the cause. This is the guard rail for
    exactly that, and it is why the duplication is safe to keep."""
    from kryptscout.autobet import engine

    real = license.public_key_fingerprint()
    assert main._LICENSE_PUBKEY_SHA256 == real, "api/main.py fingerprint is stale"
    assert engine._LICENSE_PUBKEY_SHA256 == real, "autobet/engine.py fingerprint is stale"


def test_gate_rejects_a_stand_in_license_module(monkeypatch, client):
    """A stub exporting `is_licensed = lambda: True` must not open the app.

    This is the 2026-08-08 crack, reproduced: the attacker did not defeat the
    check, they replaced the module holding it. The gate now asks for the real
    verifying key, which a stub has no reason to carry."""
    class _Shim:
        PRODUCT = "scout"

        @staticmethod
        def is_licensed(*a, **k):
            return True

    monkeypatch.setattr(main, "license_mod", _Shim)
    r = client.get("/api/performance")
    assert r.status_code == 402
    assert r.json().get("licenseRequired") is True


def test_a_faithful_module_still_passes(monkeypatch, client):
    """The integrity check must not reject the genuine module — otherwise this
    whole mechanism is an outage waiting for a release."""
    monkeypatch.setattr(license, "is_licensed", lambda *a, **k: True)
    assert main._license_module_intact() is True
    assert client.get("/api/performance").status_code == 200

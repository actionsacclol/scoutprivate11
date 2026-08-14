"""Token verification against a dishonest system clock.

Expiry used to be checked against `time.time()` alone, so winding the date back
made every token immortal — and an immortal token is never renewed, which turns
off revocation permanently. It needed no reverse engineering, no file editing
and no understanding of the app: it was cheaper than the crack in HARDENING.md.

`iat` is inside the signature, so the fix anchors to it. These tests sign real
tokens with a throwaway key to prove the anchor holds.
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kryptscout import license


#: Captured at import, before any test patches the clock. A lambda that calls
#: `time.time()` after `license.time.time` has been replaced calls ITSELF —
#: license.time IS the time module, so the patch is global.
_REAL_TIME = time.time
_REAL_IS_LICENSED = license.is_licensed


def _freeze(monkeypatch, at: float) -> None:
    """Pin the clock the license module sees to `at`."""
    monkeypatch.setattr(license.time, "time", lambda: at)


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


@pytest.fixture()
def real_is_licensed(monkeypatch):
    """Undo conftest's autouse `_licensed` patch.

    Every other test in the suite runs as if licensed so the gate does not 402
    unrelated things. These tests are about the gate itself, so they need the
    real function back."""
    monkeypatch.setattr(license, "is_licensed", _REAL_IS_LICENSED)


@pytest.fixture()
def signer(monkeypatch, tmp_path):
    """Swap the embedded public key for one we hold the private half of."""
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes_raw()
    monkeypatch.setattr(license, "_PUBLIC_KEY_B64", base64.b64encode(pub_raw).decode())
    monkeypatch.setattr(license, "_pub_key", None)  # drop the cached key object
    monkeypatch.setattr(license, "STORE_FILE", tmp_path / "license.json")

    def mint(**over) -> str:
        now = int(time.time())
        payload = {
            "v": 1, "kid": "1", "hwid": license.compute_hwid(),
            "product": "scout", "plan": "donor",
            "iat": now, "exp": now + 48 * 3600,
        }
        payload.update(over)
        p = _b64url(json.dumps(payload).encode())
        return f"{p}.{_b64url(priv.sign(p.encode('ascii')))}"

    return mint


def test_a_genuine_token_verifies(signer):
    assert license._verify_token(signer()) is not None


def test_expired_token_is_refused(signer):
    now = int(time.time())
    assert license._verify_token(signer(iat=now - 7200, exp=now - 60)) is None


def test_token_for_another_machine_is_refused(signer):
    assert license._verify_token(signer(hwid="0" * 64)) is None


def test_token_for_another_product_is_refused(signer):
    assert license._verify_token(signer(product="poker")) is None


def test_a_forged_signature_is_refused(signer):
    good = signer()
    payload, _sig = good.split(".")
    # Re-sign with a different key: the payload is untouched and still says
    # everything the app wants to hear.
    other = Ed25519PrivateKey.generate()
    forged = f"{payload}.{_b64url(other.sign(payload.encode('ascii')))}"
    assert license._verify_token(forged) is None


def test_clock_rolled_back_before_issue_is_refused(monkeypatch, signer):
    """The attack: set the date back so `exp` never arrives."""
    token = signer()
    assert license._verify_token(token) is not None, "sanity: valid at the real time"

    # A year in the past. exp is now comfortably "in the future", which is
    # exactly what made this work before.
    _freeze(monkeypatch, _REAL_TIME() - 365 * 86400)
    assert license._verify_token(token) is None


def test_small_clock_skew_is_tolerated(monkeypatch, signer):
    """A machine ten minutes slow is a dying CMOS battery, not an attacker.

    This is the half of the change that protects paying customers, and it is
    the one worth pinning — a strict comparison here would lock people out for
    ordinary clock drift."""
    token = signer()
    _freeze(monkeypatch, _REAL_TIME() - 600)
    assert license._verify_token(token) is not None


def test_rollback_buys_at_most_one_token_lifetime(monkeypatch, signer):
    """Setting the clock to just after issue is allowed — and gains nothing.

    The token still expires `exp - iat` later on the frozen clock, which is the
    same 48h a legitimate offline user gets. That is the point: the attack
    collapses into the revocation latency the design already accepts."""
    now = int(time.time())
    token = signer(iat=now, exp=now + 48 * 3600)

    _freeze(monkeypatch, now + 60)
    assert license._verify_token(token) is not None, "just after issue: still valid"

    # Wind forward past exp on the frozen clock — it dies exactly on schedule.
    _freeze(monkeypatch, now + 48 * 3600 + 1)
    assert license._verify_token(token) is None


def test_is_licensed_reflects_the_stored_token(signer, monkeypatch, real_is_licensed):
    """The public gate, end to end through the store."""
    assert license.is_licensed() is False, "no token stored yet"

    license._write_store({"key": "KRYPT-TEST", "token": signer()})
    assert license.is_licensed() is True

    _freeze(monkeypatch, _REAL_TIME() - 365 * 86400)
    assert license.is_licensed() is False, "a rolled-back clock must not license the app"

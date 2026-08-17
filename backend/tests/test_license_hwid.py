"""Machine binding: stability on macOS/Linux, and migration off the old id.

The hwid is the anti-sharing mechanism, so it has to be strict. But it also has
to be STABLE, and on macOS it was not: with no platform id of its own, macOS
fell through to `uuid.getnode()` + `platform.node()`, and the hostname half of
that is rewritten by macOS itself on a DHCP name collision. When it moved, the
server-signed token stopped matching the machine and a paying customer was sent
back to the activation wall — burning another seat when they re-activated.

These tests pin both halves of the fix: a stable id is preferred where the
platform has one, and a token bound to the OLD id keeps working (nobody can
re-sign a token client-side, so refusing it would lock out every install that
activated before this change).
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kryptscout import license


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


@pytest.fixture()
def signer(monkeypatch, tmp_path):
    """Sign tokens with a key we hold, against an isolated store."""
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(license, "_PUBLIC_KEY_B64",
                        base64.b64encode(priv.public_key().public_bytes_raw()).decode())
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


# ------------------------------------------------------------------ the ids


def test_machine_ids_are_present_and_deduped():
    ids = license._machine_ids()
    assert ids, "a machine must always yield at least the portable id"
    assert all(isinstance(i, str) and i for i in ids)
    assert len(ids) == len(set(ids))


def test_compute_hwid_is_a_stable_hash():
    a, b = license.compute_hwid(), license.compute_hwid()
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_compute_hwid_prefers_the_first_id(monkeypatch):
    monkeypatch.setattr(license, "_machine_ids", lambda: ["stable-id", "legacy-id"])
    assert license.compute_hwid() == license._hash_machine_id("stable-id")


def test_the_raw_machine_id_never_appears_in_the_hwid(monkeypatch):
    """Only the salted hash is stored or transmitted."""
    monkeypatch.setattr(license, "_machine_ids", lambda: ["MY-SERIAL-1234"])
    assert "MY-SERIAL-1234" not in license.compute_hwid()


# --------------------------------------------------------------- acceptance


def test_a_token_bound_to_the_legacy_id_still_verifies(monkeypatch, signer):
    """Migration. An install activated before the stable id existed holds a
    token bound to the portable one, and the client cannot re-sign it."""
    monkeypatch.setattr(license, "_machine_ids", lambda: ["stable-id", "legacy-id"])
    token = signer(hwid=license._hash_machine_id("legacy-id"))
    assert license._verify_token(token) is not None


def test_a_token_bound_to_the_stable_id_survives_hostname_drift(monkeypatch, signer):
    """The bug itself: macOS renames the host, the portable id moves, and a
    token bound to the stable id must not care."""
    monkeypatch.setattr(license, "_machine_ids", lambda: ["stable-id", "1234-Mini"])
    token = signer(hwid=license._hash_machine_id("stable-id"))

    monkeypatch.setattr(license, "_machine_ids", lambda: ["stable-id", "1234-Mini-2"])
    assert license._verify_token(token) is not None


def test_a_token_for_another_machine_is_still_refused(monkeypatch, signer):
    """The candidate list must not become a way in: every entry is read from
    THIS machine, so a copied license.json lands somewhere it can't match."""
    monkeypatch.setattr(license, "_machine_ids", lambda: ["stable-id", "legacy-id"])
    token = signer(hwid=license._hash_machine_id("someone-elses-machine"))
    assert license._verify_token(token) is None


def test_an_explicit_hwid_must_match_exactly(monkeypatch, signer):
    """activate() checks the token it just received against the id it sent, so
    that path deliberately does not consult the candidate list."""
    monkeypatch.setattr(license, "_machine_ids", lambda: ["stable-id", "legacy-id"])
    legacy = license._hash_machine_id("legacy-id")
    token = signer(hwid=legacy)

    assert license._verify_token(token, hwid=legacy) is not None
    assert license._verify_token(token, hwid=license._hash_machine_id("stable-id")) is None


# ------------------------------------------------------- per-platform lookups


def test_platform_lookups_are_scoped_to_their_platform():
    """Each stable-id reader answers on its own platform and stays silent
    elsewhere, so the candidate list never mixes ids across OSes."""
    import sys

    assert (license._windows_machine_guid() is None) or sys.platform == "win32"
    assert (license._darwin_platform_uuid() is None) or sys.platform == "darwin"
    assert (license._linux_machine_id() is None) or sys.platform.startswith("linux")

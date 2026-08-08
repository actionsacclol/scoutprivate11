"""Krypt Scout license verification + activation (client side).

This module is COMPILED to native code (scripts/py-harden.mjs → HARDEN_MODULES),
so the license check ships as machine code, not patchable bytecode. It pairs
with the hornbot license server (Next.js: /api/scout/activate|heartbeat|
deactivate), which signs Ed25519 tokens this module verifies OFFLINE with the
embedded public key below.

The security model (see HARDENING.md): a token is a tamper-proof, offline-
verifiable proof that a key is activated on THIS machine until `exp`. It works
with no network until it expires; the app heartbeats before then to renew, and
that heartbeat is where revocation bites (within one token TTL). Binding to a
hardware id (HWID) stops one key being shared across machines beyond its seat
cap. A determined reverser can still patch this native check — the license's
real teeth are revocation + anti-sharing, layered on the compilation.

FUNDS-SAFETY CARVE-OUT: this gates NEW LIVE bets only. Viewing positions,
settling, grading, and dry-run must never be blocked — see engine._sweep_locked.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import platform
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from kryptscout.config import DATA_DIR

logger = logging.getLogger("kryptscout")

PRODUCT = "scout"

# Ed25519 PUBLIC key (raw, base64) matching the server's SCOUT_LICENSE_PRIVATE_
# KEY_B64. Public — safe to embed. Rotating the signing key means changing this
# constant and rebuilding.
_PUBLIC_KEY_B64 = "KjYaX4WrOOPtKw/2LgxhjUcPYmTCPDyvlIgxNMVJ5ik="

# Salt folded into the HWID hash so the stored/transmitted value can't be
# reversed to the raw machine id and isn't a bare MachineGuid.
_HWID_SALT = "krypt-scout-hwid-v1"

# Where the signed token + key live (userData-aware via config.DATA_DIR).
# Tests monkeypatch this.
STORE_FILE = DATA_DIR / "license.json"

# License server base URL. Override for dev (e.g. http://localhost:3000).
DEFAULT_SERVER = "https://krypt.cc"


class LicenseError(Exception):
    """Activation/deactivation failed with a user-facing message."""


def _server() -> str:
    return (os.environ.get("KRYPT_LICENSE_SERVER") or DEFAULT_SERVER).rstrip("/")


# ------------------------------------------------------------------ HWID


def _machine_id() -> str:
    """A stable per-machine string. Windows MachineGuid survives reboots and
    most hardware/OS upgrades, changing only on a Windows reinstall — a good
    balance (strict enough to stop copying, loose enough not to lock out real
    users on a GPU/disk swap). Falls back to the MAC-derived node + hostname."""
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            )
            try:
                val, _ = winreg.QueryValueEx(key, "MachineGuid")
            finally:
                winreg.CloseKey(key)
            if val:
                return str(val)
        except Exception:  # noqa: BLE001 — fall through to the portable id
            pass
    import uuid
    return f"{uuid.getnode()}-{platform.node()}"


def compute_hwid() -> str:
    """Salted sha256 of the machine id (64 lowercase hex chars). The raw id
    never leaves the machine; only this hash is stored/transmitted."""
    return hashlib.sha256(f"{_HWID_SALT}|{_machine_id()}".encode("utf-8")).hexdigest()


# --------------------------------------------------------------- token verify


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_pub_key: Optional[Ed25519PublicKey] = None


def _public_key() -> Ed25519PublicKey:
    global _pub_key
    if _pub_key is None:
        _pub_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(_PUBLIC_KEY_B64))
    return _pub_key


def _verify_token(token: str, *, hwid: Optional[str] = None) -> Optional[dict]:
    """Return the token payload iff the Ed25519 signature is valid AND it is
    unexpired AND bound to THIS machine's hwid AND for this product. Else None.
    Pure/offline — no network."""
    try:
        p, s = token.split(".")
    except (ValueError, AttributeError):
        return None
    try:
        _public_key().verify(_b64url_decode(s), p.encode("ascii"))
    except (InvalidSignature, ValueError, TypeError):
        return None
    try:
        payload = json.loads(_b64url_decode(p))
    except (ValueError, TypeError):
        return None
    if payload.get("product") != PRODUCT:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp <= time.time():
        return None
    if payload.get("hwid") != (hwid if hwid is not None else compute_hwid()):
        return None
    return payload


# ------------------------------------------------------------------ store


def _read_store() -> dict:
    try:
        return json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_store(data: dict) -> None:
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STORE_FILE)


def _clear_token() -> None:
    data = _read_store()
    if data:
        data.pop("token", None)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_store(data)


# ------------------------------------------------------------------ public API


def is_licensed(product: str = PRODUCT) -> bool:
    """THE gate. True iff a valid, unexpired, this-machine token is stored.
    Offline and fast — safe to call on the hot autobet path."""
    token = _read_store().get("token")
    if not token:
        return False
    return _verify_token(token) is not None


def status() -> dict:
    """License state for the UI / status endpoint (local-only, no network)."""
    data = _read_store()
    token = data.get("token")
    payload = _verify_token(token) if token else None
    licensed = payload is not None
    reason = None
    if not token:
        reason = "no_license"
    elif payload is None:
        reason = "expired_or_invalid"
    return {
        "licensed": licensed,
        "product": PRODUCT,
        "reason": reason,
        "hwid": compute_hwid()[:16],  # short, for display only
        "expires_at": (
            datetime.fromtimestamp(payload["exp"], tz=timezone.utc).isoformat()
            if payload else data.get("expiresAt")
        ),
        "max_seats": data.get("maxSeats"),
        "activations": data.get("activations"),
        "server": _server(),
    }


def _err(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error") or f"HTTP {resp.status_code}")
    except Exception:  # noqa: BLE001
        return f"HTTP {resp.status_code}"


async def activate(key: str) -> dict:
    """Bind this machine to a license key and store the signed token. Raises
    LicenseError with a user-facing message on failure."""
    key = (key or "").strip()
    if not key:
        raise LicenseError("Enter your license key")
    hwid = compute_hwid()
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.post(
                f"{_server()}/api/scout/activate",
                json={"key": key, "hwid": hwid, "label": platform.node()[:64]},
            )
    except httpx.HTTPError as exc:
        raise LicenseError(f"Could not reach the license server ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise LicenseError(_err(resp))
    body = resp.json()
    if not _verify_token(body.get("token", ""), hwid=hwid):
        # The server returned a token we can't verify — refuse to store it.
        raise LicenseError("License server returned an invalid token")
    _write_store({
        "key": key, "token": body["token"], "product": body.get("product", PRODUCT),
        "plan": body.get("plan"), "maxSeats": body.get("maxSeats"),
        "activations": body.get("activations"), "expiresAt": body.get("expiresAt"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info("Krypt Scout license activated (%s seats used %s/%s)",
                body.get("product"), body.get("activations"), body.get("maxSeats"))
    return status()


async def heartbeat() -> bool:
    """Renew the token while online; enforce revocation. Best-effort — returns
    True on a successful renew. A 403 (revoked/expired license) clears the
    stored token so the app lapses cleanly. Network errors leave the current
    token intact (offline grace)."""
    data = _read_store()
    token = data.get("token")
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.post(f"{_server()}/api/scout/heartbeat", json={"token": token})
    except httpx.HTTPError:
        return False  # offline — keep the current token until it expires
    if resp.status_code == 403:
        logger.warning("Krypt Scout license revoked/expired — clearing local token")
        _clear_token()
        return False
    if resp.status_code != 200:
        return False
    body = resp.json()
    fresh = body.get("token")
    if fresh and _verify_token(fresh):
        data.update({"token": fresh, "expiresAt": body.get("expiresAt"),
                     "updated_at": datetime.now(timezone.utc).isoformat()})
        _write_store(data)
        return True
    return False


async def deactivate() -> dict:
    """Free this machine's seat and clear the local license."""
    data = _read_store()
    key = data.get("key")
    hwid = compute_hwid()
    if key:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                await client.post(f"{_server()}/api/scout/deactivate", json={"key": key, "hwid": hwid})
        except httpx.HTTPError:
            pass  # best-effort; still clear locally
    try:
        STORE_FILE.unlink()
    except OSError:
        pass
    logger.info("Krypt Scout license deactivated on this machine")
    return status()

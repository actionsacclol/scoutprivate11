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

Compiling this module stops it being READ, not being REPLACED: Python resolves
this import by filename at runtime, out of a directory the user owns, so a shim
returning True from is_licensed() defeats the file whatever it was compiled to.
Not theoretical - it is how this was cracked on 2026-08-08 (HARDENING.md,
"Confirmed bypass"). Treat the below as a speed bump plus a revocation channel,
never as enforcement.

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
import re
import subprocess
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


def _windows_machine_guid() -> Optional[str]:
    """Windows MachineGuid — survives reboots and most hardware/OS upgrades,
    changing only on a Windows reinstall. Strict enough to stop copying, loose
    enough not to lock anyone out over a GPU or disk swap."""
    if sys.platform != "win32":
        return None
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
        return str(val) or None
    except Exception:  # noqa: BLE001 — caller falls through to the portable id
        return None


def _darwin_platform_uuid() -> Optional[str]:
    """macOS IOPlatformUUID — the stable analogue of Windows MachineGuid.

    macOS had no equivalent here, so it fell all the way through to the portable
    id below, and that id DRIFTS on a Mac: `platform.node()` is the hostname,
    which macOS rewrites on its own when a DHCP name collides (`Mini` becomes
    `Mini-2`), and `uuid.getnode()` invents a random node whenever it cannot
    read a MAC. Either one moves and the hwid moves with it — at which point a
    paying customer's signed token stops matching their machine and the app
    walls them behind the activation screen, then spends another seat when they
    re-activate. IOPlatformUUID moves for nobody short of a logic-board swap."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
    return m.group(1) if m else None


def _linux_machine_id() -> Optional[str]:
    """systemd/dbus machine-id: written once at install, stable thereafter.
    Same reasoning as the macOS case — the portable fallback drifts."""
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return None
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                val = fh.read().strip()
            if val:
                return val
        except OSError:
            continue
    return None


def _portable_machine_id() -> str:
    """Last-resort id for a machine with no stable platform id. Known to drift
    (see _darwin_platform_uuid) — kept only so nobody is left with no id at all,
    and because installs activated before the stable ids existed are bound to
    it. Never preferred when a stable id is available."""
    import uuid
    return f"{uuid.getnode()}-{platform.node()}"


def _machine_ids() -> list[str]:
    """Every id this machine can legitimately claim, most stable first.

    More than one because of MIGRATION: an install activated under the old
    scheme holds a server-signed token bound to the hash of the portable id, and
    the client cannot re-sign it. Accepting any id on this list means those
    users keep working while new activations bind to the stable id at the front.

    This does NOT loosen anti-sharing: every entry is still read from THIS
    machine, so a copied license.json lands on a machine whose ids don't include
    the token's."""
    ids = [
        i for i in (_windows_machine_guid(), _darwin_platform_uuid(), _linux_machine_id())
        if i
    ]
    ids.append(_portable_machine_id())
    # Dedup, preserving order.
    return list(dict.fromkeys(ids))


def _hash_machine_id(raw: str) -> str:
    return hashlib.sha256(f"{_HWID_SALT}|{raw}".encode("utf-8")).hexdigest()


def compute_hwid() -> str:
    """Salted sha256 of the machine id (64 lowercase hex chars) — the one this
    machine PREFERS, which is what a new activation binds to. The raw id never
    leaves the machine; only this hash is stored/transmitted."""
    return _hash_machine_id(_machine_ids()[0])


def _hwid_candidates() -> list[str]:
    """Hashed ids a token may be bound to for this machine to accept it."""
    return [_hash_machine_id(i) for i in _machine_ids()]


# ------------------------------------------------------------ environment signal
#
# Reported to the server on activate/heartbeat so a human gets a Discord ping
# with the buyer's identity attached (the server already handles the alert; see
# hornbot scout-alert.ts). Telemetry, never a gate: a patched client can lie or
# omit these, so nothing here bans or blocks on them — a client-side ban only
# ever hits the honest user, since a cracker patches the check out. The value is
# a VM or attached-debugger flag sitting next to a shared-key flag on the same
# key. Compiled with the rest of this module, so it ships as native code too.

# Definite VM-vendor markers only — the "light" in light detection. A real PC,
# even one running WSL2/Docker/Hyper-V, reports its own maker (Dell, ASUS…); a
# VM reports the hypervisor's. The CPU hypervisor bit is deliberately NOT used:
# it is set on ordinary Windows 11 machines and is the main false positive.
_VM_MARKERS = (
    "vmware", "virtualbox", "innotek", "qemu", "kvm", "xen",
    "parallels", "bochs", "virtual machine",
)


def _vm_marker_from(manufacturer: str, product: str, bios: str) -> Optional[str]:
    """Pure matcher (testable without a hypervisor): the VM marker present in
    the SMBIOS strings, or None. 'Microsoft Corporation' counts only with a
    'virtual' product so a Surface — same manufacturer — never trips."""
    hay = f"{manufacturer} | {product} | {bios}".lower()
    for m in _VM_MARKERS:
        if m in hay:
            return f'{m} in "{hay.strip()}"'[:200]
    if "microsoft corporation" in manufacturer.lower() and "virtual" in product.lower():
        return f'hyper-v guest ("{manufacturer} / {product}")'[:200]
    return None


def _vm_marker() -> Optional[str]:
    """This machine's VM marker, read from the registry SMBIOS strings, or None.
    Windows-only and best-effort — a machine whose registry can't be read is
    treated as a real PC, never flagged on a failed lookup."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS",
            0, winreg.KEY_READ,
        )
        try:
            def _get(name: str) -> str:
                try:
                    val, _ = winreg.QueryValueEx(key, name)
                    return str(val)
                except OSError:
                    return ""
            return _vm_marker_from(_get("SystemManufacturer"), _get("SystemProductName"), _get("BIOSVendor"))
        finally:
            winreg.CloseKey(key)
    except OSError:
        return None


def _debugger_attached() -> bool:
    """Whether a Python tracer/debugger is attached to this process. Only
    meaningful in the frozen build — pytest and coverage set a trace function
    too, so this is guarded to the packaged app to stay false in development."""
    if not getattr(sys, "frozen", False):
        return False
    try:
        return sys.gettrace() is not None
    except Exception:  # noqa: BLE001
        return False


def _env_signals() -> dict:
    """The environment payload sent on activate/heartbeat. Omits anything not
    noteworthy so an ordinary machine sends {} and the server stays quiet."""
    env: dict[str, Any] = {}
    vm = _vm_marker()
    if vm:
        env["vm"] = vm
    if _debugger_attached():
        env["dbg"] = True
    return env


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
    unexpired AND not from the future AND bound to THIS machine's hwid AND for
    this product. Else None. Pure/offline — no network."""
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

    # Reject a clock rolled back behind the token's own issue time.
    #
    # Expiry alone was checked against the system clock, so setting the date
    # back a year made every token immortal — no reversing, no file editing,
    # just the date control panel. That is a cheaper bypass than the one in
    # HARDENING.md and it also switched off revocation permanently, because a
    # token that never expires is a token the app never needs to renew.
    #
    # `iat` is inside the signature, so it cannot be edited, and it is the one
    # piece of honest wall-clock time on the machine. Anchoring to it collapses
    # the attack to "you may set your clock to just after issue", which buys
    # exactly one TTL — the same 48h a legitimate offline user gets, and the
    # revocation latency the design already accepts.
    #
    # The hour of slack is for real clock skew. Being generous costs an hour of
    # immortality and avoids locking out anyone whose CMOS battery is dying.
    iat = payload.get("iat")
    if isinstance(iat, (int, float)) and time.time() < iat - 3600:
        return None

    # An explicit hwid (activate() checking the token it just received) must
    # match exactly. Otherwise any id this machine can claim will do — see
    # _machine_ids for why there is more than one.
    expected = [hwid] if hwid is not None else _hwid_candidates()
    if payload.get("hwid") not in expected:
        return None
    return payload


def public_key_fingerprint() -> str:
    """sha256 of the raw embedded public key, for callers that want to satisfy
    themselves this is the real module and not something wearing its name.

    A shim can return True from anything; reproducing this means shipping the
    genuine verifying key, which is the one thing a stub author has no reason
    to carry. See `_license_module_intact` in api/main.py — the check lives at
    the call site deliberately, because a module cannot vouch for itself."""
    return hashlib.sha256(base64.b64decode(_PUBLIC_KEY_B64)).hexdigest()


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


# --------------------------------------------------------------- decoys
#
# MAINTAINERS: nothing below this line is consulted by anything. Do not call it,
# do not "fix" it, do not wire it up. It exists to be found and patched.
#
# The reasoning: the first thing anyone does to a licensed app is grep the
# binary for license-shaped symbols and flip the most promising one. Every name
# here is more attractive than the real path — shorter, blunter, more obviously
# The Check — and each returns the genuine answer, so watching one under a
# debugger shows it going False on an unlicensed install and True on a licensed
# one. It looks exactly like the thing you want.
#
# Patching any of them changes nothing at all. That is the entire point: the
# cheapest and most obvious attack produces a build that appears cracked, and
# an afternoon is spent before anybody notices the gate never moved.
#
# They return the real answer rather than a constant on purpose. A function
# that returns True unconditionally is spotted in a second and skipped; one
# that tracks the real state has to be tested to be disproved.
#
# Cost of a false lead to us: nothing. Cost to a paying customer: nothing —
# no product code path reaches any of it.


def check_license() -> bool:
    """Deprecated shim retained for older call sites. (Decoy — see above.)"""
    return is_licensed()


def verify_license(product: str = PRODUCT) -> bool:
    """Whether this install is licensed for `product`. (Decoy — see above.)"""
    return is_licensed(product)


def license_valid() -> bool:
    """Fast path used by the request gate. (Decoy — see above.)"""
    return is_licensed()


def _check_activation() -> bool:
    """Internal activation check. (Decoy — see above.)"""
    return is_licensed()


#: Cached at import for the hot path. (Decoy — see above.)
LICENSE_OK: bool = False
#: Set once the license has been validated this session. (Decoy — see above.)
_LICENSE_VALIDATED: bool = False


def _refresh_license_cache() -> bool:
    """Re-read the license into the module cache. (Decoy — see above.)"""
    global LICENSE_OK, _LICENSE_VALIDATED
    LICENSE_OK = is_licensed()
    _LICENSE_VALIDATED = True
    return LICENSE_OK


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
                json={"key": key, "hwid": hwid, "label": platform.node()[:64], "env": _env_signals()},
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
            resp = await client.post(f"{_server()}/api/scout/heartbeat", json={"token": token, "env": _env_signals()})
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

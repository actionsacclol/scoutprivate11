"""Wallet + request signing for the Polymarket CLOB trading API.

Polymarket trading is on-chain (Polygon, chain id 137). Unlike a centralized REST exchange's
RSA-PSS-over-REST model, authenticating to the Polymarket CLOB has two
levels:

  L1 — Ethereum private key. An EIP-712 "ClobAuth" message signed by the
       wallet proves control of the address. Used once to *derive* (or
       create) the L2 API credentials.
  L2 — API key / secret / passphrase (an HMAC scheme on top of L1). Every
       authenticated CLOB request carries an HMAC-SHA256 signature plus the
       api-key/passphrase headers.

Orders are EIP-712 structs signed by the same wallet key and POSTed to the
CLOB.

VENDORED from krypt-polybot (python/polymarket_auth.py). Keep diffs against
upstream minimal — only the credentials directory (kryptscout.config paths)
and the keyring service name differ; the money-critical signing code is
otherwise identical to the battle-tested PolyBot original.

Krypt Scout stores credentials at:

    <userData>/credentials/wallet.mainnet.key      hex private key (0x…)
    <userData>/credentials/apicreds.mainnet.json   {apiKey, secret, passphrase}

<userData> follows kryptscout.config: KRYPT_SCOUT_USERDATA (set by the
Electron main process on backend spawn), falling back to the repo root in
dev/tests/CLI. Both files are encrypted at rest: on Windows with the user
account key (DPAPI); on macOS/Linux with a random master key held in the OS
keychain (macOS Keychain / Linux Secret Service). If no backend is usable
they fall back to plaintext-0600 and warn.

There is a single network — Polygon mainnet. The `env` argument is kept on
the public functions purely for call-site compatibility with the rest of
the bot (which threads an env string through everything); it is always
"mainnet".
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Polygon mainnet.
CHAIN_ID = 137
NETWORK = "mainnet"

# Polymarket on-chain contracts (Polygon mainnet, chainId 137). The
# verifying contract for an order's EIP-712 domain depends on whether the
# market is a "negative risk" (multi-outcome) market.
#
# Polymarket migrated to CLOB Exchange **V2** on 2026-04-28: the signed
# Order struct, exchange addresses, EIP-712 domain version ("1" → "2"), and
# the collateral token (USDC.e → pUSD) all changed. These V2 addresses are
# VERIFIED against docs.polymarket.com/resources/contracts and PolygonScan
# ("CTF Exchange V2" / "Neg Risk CTF Exchange V2"). The ClobAuth domain used
# for L1 API-key auth stays at version "1" (only the Order domain is "2").
EXCHANGE_VERSION = "2"
# V2 exchanges (the order EIP-712 `verifyingContract`).
EXCHANGE_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"
# Reference addresses for the one-time on-chain trading approvals (the user
# sets these via Polymarket's UI; the bot only checks them). pUSD is the
# 6-decimal collateral; spenders that need approval are the two exchanges +
# the neg-risk adapter + the CTF.
# CLOB v2 NegRiskCtfCollateralAdapter (docs.polymarket.com/resources/contracts).
# The v1 adapter 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296 was retired
# 2026-07-17 — sync this fix upstream to krypt-polybot.
NEG_RISK_ADAPTER_ADDRESS = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"
CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
COLLATERAL_PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + "00" * 32

# signatureType: 0=EOA (raw private key), 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE.
# A bot driving a raw private key with USDC + CTF approvals on that EOA
# uses EOA.
SIGNATURE_TYPE_EOA = 0


# ───────── at-rest encryption ─────────────────────────────────────────
# Credentials are encrypted at rest with the best backend available, in order:
#   1. Windows DPAPI — the user's account key (CryptProtectData). No key on
#      disk; another user can't decrypt.
#   2. OS keychain (macOS Keychain / Linux Secret Service, via `keyring`) — we
#      keep a random 32-byte master key in the keychain and Fernet-encrypt the
#      file with it. Like DPAPI, no usable key material sits next to the
#      ciphertext, and it's transparent (no passphrase → no onboarding change).
#   3. Plaintext-0600 fallback — only when neither is usable (e.g. a headless
#      Linux box with no Secret Service). We warn, and never block a save.
# A credential file is tagged with a marker byte-prefix so reads know which
# scheme produced it. Legacy/plaintext files are transparently upgraded to the
# best available scheme on first read.

_DPAPI_MARKER = b"#KRYPT-DPAPI-v1\n"
_KEYRING_MARKER = b"#KRYPT-KEYRING-v1\n"
_KEYRING_SERVICE = "krypt-scout"
_KEYRING_MASTER_ACCOUNT = "cred-master-key"
_warned_plaintext = False
_plaintext_error_logged = False   # gate the ERROR log to once per PROCESS (startup)
_keyring_master_cache: Optional[bytes] = None


def _dpapi_available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(data: bytes) -> "_DATA_BLOB":
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _from_blob(blob: "_DATA_BLOB") -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob.pbData)

    def _dpapi_encrypt(data: bytes) -> bytes:
        out = _DATA_BLOB()
        blob_in = _to_blob(data)
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(out)
        ):
            raise OSError("CryptProtectData failed")
        return _from_blob(out)

    def _dpapi_decrypt(data: bytes) -> bytes:
        out = _DATA_BLOB()
        blob_in = _to_blob(data)
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(out)
        ):
            raise OSError("CryptUnprotectData failed")
        return _from_blob(out)
else:  # pragma: no cover - non-Windows fallback
    def _dpapi_encrypt(data: bytes) -> bytes:
        raise OSError("DPAPI not available")

    def _dpapi_decrypt(data: bytes) -> bytes:
        raise OSError("DPAPI not available")


# ── OS keychain (macOS Keychain / Linux Secret Service via `keyring`) ──
# These wrappers swallow every failure (no backend, locked keychain, no
# D-Bus, keyring not bundled) and signal it by returning None/False, so a
# missing keychain degrades to plaintext rather than breaking credential I/O.


def _keyring_set(account: str, value: str) -> bool:
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, account, value)
        return True
    except Exception:
        return False


def _keyring_get(account: str) -> Optional[str]:
    try:
        import keyring
        return keyring.get_password(_KEYRING_SERVICE, account)
    except Exception:
        return None


def _fernet(key: bytes):
    from cryptography.fernet import Fernet
    return Fernet(key)


def _get_master_key() -> Optional[bytes]:
    """Fetch (or, on first use, create) the Fernet master key from the OS
    keychain. Returns None when no keychain backend is usable — the caller then
    falls back to plaintext. The key never touches the credential directory."""
    global _keyring_master_cache
    if _keyring_master_cache is not None:
        return _keyring_master_cache
    existing = _keyring_get(_KEYRING_MASTER_ACCOUNT)
    if existing:
        key = existing.encode("ascii")
        try:
            _fernet(key)  # validate it's a usable Fernet key before trusting it
            _keyring_master_cache = key
            return key
        except Exception:
            logger.warning("Stored keychain master key is corrupt; regenerating.")
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    if not _keyring_set(_KEYRING_MASTER_ACCOUNT, key.decode("ascii")):
        return None
    # Read-back guard: a no-op backend can accept set() yet return None on get();
    # in that case treat the keychain as unusable rather than write ciphertext we
    # could never decrypt.
    if _keyring_get(_KEYRING_MASTER_ACCOUNT) != key.decode("ascii"):
        return None
    _keyring_master_cache = key
    return key


def _keyring_available() -> bool:
    return _get_master_key() is not None


def _keyring_encrypt(data: bytes) -> bytes:
    key = _get_master_key()
    if key is None:
        raise OSError("no OS keychain backend available")
    return _fernet(key).encrypt(data)


def _keyring_decrypt(token: bytes) -> bytes:
    key = _get_master_key()
    if key is None:
        raise OSError("no OS keychain backend available")
    return _fernet(key).decrypt(token)


def _chmod_600(path: Path) -> None:
    """Best-effort owner-only perms on POSIX (no-op / not meaningful on Windows,
    where DPAPI + NTFS ACLs already scope the file to the user)."""
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _atomic_write_600(path: Path, tmp: Path, data: bytes) -> None:
    """Write `data` to `tmp` as an OWNER-ONLY file (0600 from CREATION on POSIX),
    then atomically replace `path`. Creating the temp file 0600 up front means the
    (plaintext-fallback) key is never briefly world-readable — not in the pre-chmod
    window, and no 0644 crash-residue if the process dies before the replace."""
    try:
        if tmp.exists():
            tmp.unlink()   # clear any stale / loose-perm residue from a prior crash
    except OSError:
        pass
    if os.name == "posix":
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    else:
        tmp.write_bytes(data)  # Windows: DPAPI scopes content + NTFS ACL scopes the file
    _chmod_600(tmp)
    tmp.replace(path)


def _write_secret_bytes(path: Path, data: bytes) -> None:
    """Atomically write `data`, encrypted with the best available backend
    (DPAPI → OS keychain → plaintext). Every branch writes via a 0600-from-creation
    temp file so even the plaintext fallback key is never world-readable at rest."""
    global _warned_plaintext, _plaintext_error_logged
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path.parent, 0o700)  # credentials/ owner-only (not umask ~0755)
        except OSError:
            pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    if _dpapi_available():
        try:
            _atomic_write_600(path, tmp, _DPAPI_MARKER + base64.b64encode(_dpapi_encrypt(data)))
            return
        except Exception as e:
            logger.warning(f"DPAPI encrypt failed, trying OS keychain: {e}")
    try:
        if _keyring_available():
            _atomic_write_600(path, tmp, _KEYRING_MARKER + _keyring_encrypt(data))
            return
    except Exception as e:
        logger.warning(f"Keychain encrypt failed, storing plaintext: {e}")
    if not _warned_plaintext:
        logger.error(
            "SECURITY: credentials stored UNENCRYPTED (no DPAPI or OS-keychain "
            "backend on this platform). The file is chmod 600 but NOT encrypted "
            "at rest. Surfaced to the UI as credentials_status().keyStoredUnencrypted."
        )
        _warned_plaintext = True
    # A fresh plaintext write may flip the on-disk state, so re-arm the
    # once-per-process startup ERROR to reflect it on the next status poll.
    _plaintext_error_logged = False
    _atomic_write_600(path, tmp, data)


def _read_secret_bytes(path: Path, upgrade: bool = True) -> bytes:
    """Read a credential file, decrypting DPAPI- or keychain-wrapped content.
    Legacy plaintext is returned as-is and (when a backend is available)
    re-encrypted in place on this read."""
    raw = path.read_bytes()
    if raw.startswith(_DPAPI_MARKER):
        return _dpapi_decrypt(base64.b64decode(raw[len(_DPAPI_MARKER):]))
    if raw.startswith(_KEYRING_MARKER):
        return _keyring_decrypt(raw[len(_KEYRING_MARKER):])
    # Legacy / plaintext: hand back the bytes, and opportunistically upgrade the
    # file to the best at-rest scheme so it isn't left in the clear.
    if upgrade and (_dpapi_available() or _keyring_available()):
        try:
            _write_secret_bytes(path, raw)
        except Exception:
            pass
    return raw


def _is_plaintext_secret_file(path: Path) -> bool:
    """True iff `path` holds a secret written WITHOUT at-rest encryption (neither
    the DPAPI nor the keychain marker prefix). Derives the state from the file
    itself, so it's durable across restarts and can't go stale like a saved flag."""
    try:
        if not path.exists():
            return False
        with open(path, "rb") as fh:
            head = fh.read(max(len(_DPAPI_MARKER), len(_KEYRING_MARKER)))
        return not (head.startswith(_DPAPI_MARKER) or head.startswith(_KEYRING_MARKER))
    except OSError:
        return False


def key_stored_unencrypted(env: Optional[str] = None) -> bool:
    """Durable, queryable flag: is the wallet private key sitting on disk in
    CLEARTEXT (plaintext-0600 fallback, because no DPAPI/keychain backend was
    usable)? Surfaced in `credentials_status()` so the UI can warn the user."""
    try:
        return _is_plaintext_secret_file(_wallet_key_file(env or _current_env))
    except Exception:
        return False


def _log_plaintext_at_rest_once() -> None:
    """Log at ERROR (once per process = once per startup) when the wallet key is
    stored unencrypted. Loud + repeated-every-launch, unlike the buried one-time
    warning at write time that the user never sees."""
    global _plaintext_error_logged
    if _plaintext_error_logged:
        return
    try:
        if key_stored_unencrypted():
            _plaintext_error_logged = True
            logger.error(
                "SECURITY: wallet private key is stored UNENCRYPTED on disk "
                "(no DPAPI or OS-keychain backend available on this platform). "
                "The file is chmod 600 but NOT encrypted at rest. Anyone who can "
                "read this account's files can steal the key. "
                "credentials_status().keyStoredUnencrypted == true."
            )
    except Exception:
        pass


def _credentials_dir() -> Path:
    # Same base dir as the rest of the Scout store (kryptscout.config):
    # KRYPT_SCOUT_USERDATA under Electron, the repo root in dev/tests.
    from kryptscout.config import DATA_DIR

    return DATA_DIR.parent / "credentials"


# Single-network model: 'mainnet' is the ONLY real env. `env` is interpolated
# straight into credential FILE NAMES (wallet.{env}.key, apicreds.{env}.json),
# so an attacker-influenced value (e.g. RPC-supplied) containing path separators
# or '..' could steer key writes/reads outside the credentials dir. Hard-allowlist
# it so that can't happen.
_VALID_ENVS = ("mainnet",)


def _validate_env(env: str) -> str:
    e = env if isinstance(env, str) else ""
    e = e.strip()
    if (e not in _VALID_ENVS) or ("/" in e) or ("\\" in e) or (".." in e) or (os.sep in e):
        raise ValueError(
            f"invalid network env {env!r}: only {_VALID_ENVS} are allowed "
            f"(no path separators or '..')"
        )
    return e


def _wallet_key_file(env: str = NETWORK) -> Path:
    return _credentials_dir() / f"wallet.{_validate_env(env)}.key"


def _api_creds_file(env: str = NETWORK) -> Path:
    return _credentials_dir() / f"apicreds.{_validate_env(env)}.json"


def _wallet_meta_file(env: str = NETWORK) -> Path:
    # Non-secret sidecar: the deposit-wallet (funder) address + signatureType.
    # Plaintext JSON — these are public on-chain facts, not key material.
    return _credentials_dir() / f"wallet.{_validate_env(env)}.meta.json"


# ───────── module state ───────────────────────────────────────────────

_current_env: str = NETWORK
_cached_account = None            # eth_account.LocalAccount
_cached_api_creds: Optional[dict] = None


def set_env(env: str) -> None:
    # Single-network model; accept the value for call-site compatibility.
    global _current_env
    _current_env = NETWORK


def get_env() -> str:
    return _current_env


def reset_credential_cache() -> None:
    """Drop cached wallet + API creds. Call after the user saves new keys."""
    global _cached_account, _cached_api_creds
    _cached_account = None
    _cached_api_creds = None


# ───────── eth_account (lazy import so non-trading paths don't need it) ─


def _eth_account():
    try:
        from eth_account import Account  # type: ignore
        return Account
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "eth-account is required for Polymarket signing. "
            "Install it with `pip install eth-account`."
        ) from e


def _normalize_priv(text: str) -> str:
    pk = (text or "").strip()
    # Tolerate "key=0x…" lines and surrounding quotes.
    for line in pk.splitlines():
        line = line.strip().strip('"').strip("'")
        if not line or line.startswith("#"):
            continue
        if "=" in line and not line.startswith("0x"):
            _, _, line = line.partition("=")
            line = line.strip().strip('"').strip("'")
        pk = line
        break
    if pk and not pk.startswith("0x"):
        pk = "0x" + pk
    return pk


def _load_account():
    global _cached_account
    if _cached_account is not None:
        return _cached_account
    f = _wallet_key_file(_current_env)
    if not f.exists():
        raise FileNotFoundError(f"Polymarket wallet key not configured ({f})")
    _log_plaintext_at_rest_once()  # loud ERROR at startup if the key is cleartext
    pk = _normalize_priv(_read_secret_bytes(f).decode("utf-8", "replace"))
    acct = _eth_account().from_key(pk)
    _cached_account = acct
    return acct


def get_address() -> str:
    """The wallet (EOA) address derived from the saved private key."""
    return _load_account().address


# ───────── API credentials (L2) ───────────────────────────────────────


def _load_api_creds() -> Optional[dict]:
    global _cached_api_creds
    if _cached_api_creds is not None:
        return _cached_api_creds
    f = _api_creds_file(_current_env)
    if not f.exists():
        return None
    try:
        data = json.loads(_read_secret_bytes(f).decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("apiKey"):
            _cached_api_creds = data
            return data
    except Exception:
        return None
    return None


def get_api_creds() -> Optional[dict]:
    return _load_api_creds()


def has_api_creds(env: Optional[str] = None) -> bool:
    return _api_creds_file(env or _current_env).exists()


def set_api_creds(creds: dict, env: Optional[str] = None) -> None:
    """Persist derived L2 API credentials {apiKey, secret, passphrase}."""
    global _cached_api_creds
    e = env or _current_env
    _write_secret_bytes(
        _api_creds_file(e), json.dumps(creds).encode("utf-8")
    )
    if e == _current_env:
        _cached_api_creds = creds


def clear_api_creds(env: Optional[str] = None) -> None:
    """Delete the derived L2 API creds (keep the wallet key). Used to force a
    re-derive when the CLOB rejects the current creds (stale after a key change,
    or otherwise invalid) — so the bot self-heals instead of needing a manual
    Wallet → Test."""
    global _cached_api_creds
    e = env or _current_env
    f = _api_creds_file(e)
    if f.exists():
        try:
            f.unlink()
        except Exception:
            pass
    if e == _current_env:
        _cached_api_creds = None


# ───────── wallet mode (deposit wallet / signatureType) ───────────────
# A Polymarket account can trade as a raw EOA (signatureType 0) or via a
# smart-contract "deposit wallet" (signatureType 3, POLY_1271). In the latter
# case the wallet KEY signs, but the funds + positions live at a separate
# "funder" address (the deposit wallet). We persist {funder, signatureType}
# next to the key so the read + order paths can target the right wallet.

SIGNATURE_TYPE_POLY_PROXY = 1
SIGNATURE_TYPE_POLY_GNOSIS_SAFE = 2
SIGNATURE_TYPE_POLY_1271 = 3


class WalletMetaError(RuntimeError):
    """The wallet meta sidecar exists but could not be read or parsed."""


# Last successfully-read meta, per env. `set_wallet_meta` writes the sidecar with
# tmp+replace while orders are being signed; on Windows a concurrent reader can
# hit a sharing violation on that rename. Serving the cached value there keeps a
# transient read failure from being mistaken for "no deposit wallet".
_wallet_meta_cache: dict[str, dict] = {}


def get_wallet_meta(env: Optional[str] = None) -> dict:
    """The {funder, signatureType} sidecar, or {} for a raw-EOA wallet.

    An ABSENT file means raw EOA. An UNREADABLE one does not: returning {} there
    would silently drop a configured deposit wallet, and every order signed after
    that carries the signer EOA as `maker` — which Polymarket rejects outright
    ("maker address not allowed"). So an unreadable-but-present sidecar falls back
    to the last known-good value, and raises when there isn't one. Fail closed:
    the order path must never guess the maker address."""
    e = env or _current_env
    f = _wallet_meta_file(e)
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:
        cached = _wallet_meta_cache.get(e)
        if cached is not None:
            return dict(cached)
        raise WalletMetaError(f"wallet meta {f.name} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        cached = _wallet_meta_cache.get(e)
        if cached is not None:
            return dict(cached)
        raise WalletMetaError(f"wallet meta {f.name} is not a JSON object")
    _wallet_meta_cache[e] = dict(data)
    return data


def set_wallet_meta(
    funder: str = "", signature_type: int = 0, env: Optional[str] = None
) -> None:
    e = env or _current_env
    f = _wallet_meta_file(e)
    f.parent.mkdir(parents=True, exist_ok=True)
    payload = {"funder": (funder or "").strip(), "signatureType": int(signature_type or 0)}
    tmp = f.with_suffix(f.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(f)
    _wallet_meta_cache[e] = dict(payload)


def get_funder(env: Optional[str] = None) -> str:
    """The deposit-wallet (funder) address, or "" when trading as a raw EOA."""
    return (get_wallet_meta(env).get("funder") or "").strip()


def get_signature_type(env: Optional[str] = None) -> int:
    try:
        return int(get_wallet_meta(env).get("signatureType") or 0)
    except (TypeError, ValueError):
        return 0


def trading_address(env: Optional[str] = None) -> str:
    """The address that actually holds funds + positions: the funder when a
    deposit wallet is configured, otherwise the signer EOA."""
    return get_funder(env) or get_address()


# ───────── credential management (UI surface) ─────────────────────────


def credentials_present(env: Optional[str] = None) -> bool:
    """True once a wallet key is saved. API creds are derived on demand, so
    only the wallet key is required to consider the account 'connected'."""
    return _wallet_key_file(env or _current_env).exists()


def credentials_status(env: Optional[str] = None) -> dict:
    """Diagnostic info for the UI — never returns secret material."""
    e = env or _current_env
    wf = _wallet_key_file(e)
    # A corrupt sidecar must not take down the whole status poll — report it as a
    # distinct wallet mode so the UI can tell "raw EOA" from "we can't tell".
    meta_error = ""
    try:
        funder = get_funder(e)
        sig_type = get_signature_type(e)
    except WalletMetaError as exc:
        funder, sig_type, meta_error = "", 0, str(exc)
    # ERROR-log (once per process) if the key is stored in cleartext — this
    # status is polled at startup, so the warning fires loudly on every launch.
    _log_plaintext_at_rest_once()
    info = {
        "env": e,
        "hasWalletKey": wf.exists(),
        "hasApiCreds": _api_creds_file(e).exists(),
        "address": "",
        "addressPreview": "",
        "funder": funder,
        "signatureType": sig_type,
        "metaError": meta_error,
        "walletMode": "error" if meta_error else ("deposit" if funder else "eoa"),
        # Durable, queryable security flag: the private key is on disk unencrypted
        # (no DPAPI/keychain backend). The UI should surface a persistent warning.
        "keyStoredUnencrypted": _is_plaintext_secret_file(wf),
    }
    if wf.exists():
        try:
            pk = _normalize_priv(_read_secret_bytes(wf).decode("utf-8", "replace"))
            addr = _eth_account().from_key(pk).address
            info["address"] = addr
            info["addressPreview"] = addr[:6] + "…" + addr[-4:]
        except Exception:
            pass
    return info


def credentials_status_all() -> dict:
    """Single-network status (mainnet)."""
    return {
        "current": _current_env,
        "mainnet": credentials_status(NETWORK),
    }


def save_credentials(
    private_key: str, api_creds: Optional[dict] = None, env: Optional[str] = None,
    funder: Optional[str] = None, signature_type: Optional[int] = None,
) -> None:
    """Save a wallet private key (and optionally pre-derived API creds) for
    `env` (always mainnet). Validates the key parses to an address.

    `funder` is the deposit-wallet address for a Polymarket account wallet.
    When supplied we START at signatureType 3 (POLY_1271); the real scheme is
    then auto-detected on-chain (`polymarket_api.detect_wallet_signature_type`,
    run by `check_trading_ready`) and corrected to POLY_PROXY (1, Magic/email) or
    POLY_GNOSIS_SAFE (2, MetaMask) when the deposit wallet is one of those —
    otherwise every order for those accounts is rejected. Without a funder the
    wallet trades as a raw EOA (signatureType 0)."""
    e = env or _current_env
    pk = _normalize_priv(private_key)
    if not pk:
        raise ValueError("private key is empty")
    try:
        _eth_account().from_key(pk)  # validates
    except Exception as ex:
        raise ValueError(f"private key did not parse: {ex}") from ex
    f = (funder or "").strip()
    if f and (not f.startswith("0x") or len(f) != 42):
        raise ValueError("deposit wallet address must be a 0x… 42-char address")
    _write_secret_bytes(_wallet_key_file(e), (pk + "\n").encode("utf-8"))
    if api_creds:
        set_api_creds(api_creds, e)
    else:
        # A new/changed signer key INVALIDATES any API creds derived for the old
        # signer. If we leave the stale apicreds file, ensure_api_creds reuses it
        # and every order is rejected by the CLOB with "the order signer address
        # has to be the address of the api key". Drop the stale creds so they're
        # re-derived for THIS key on the next authed call. (Re-saving the same
        # key just re-derives identical creds — harmless.)
        acf = _api_creds_file(e)
        if acf.exists():
            try:
                acf.unlink()
            except Exception:
                pass
    if signature_type is None:
        signature_type = SIGNATURE_TYPE_POLY_1271 if f else 0
    set_wallet_meta(funder=f, signature_type=int(signature_type), env=e)
    if e == _current_env:
        reset_credential_cache()


def clear_credentials(env: Optional[str] = None) -> None:
    """Delete the wallet key + derived API creds for `env`."""
    e = env or _current_env
    for p in (_wallet_key_file(e), _api_creds_file(e)):
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass
    if e == _current_env:
        reset_credential_cache()


def migrate_legacy_credentials(target_env: str) -> bool:
    """No legacy layout to migrate for PolyBot. Kept for call-site
    compatibility with the service bootstrap."""
    return False


# Local↔server clock offset (seconds), set by polymarket_api._sync_clock_offset()
# after fetching the CLOB /time endpoint. now_ts() adds it so signed L1/L2
# timestamps match what Polymarket's freshness check expects even when the user's
# system clock is wrong — a skewed clock otherwise rejects every signed request as
# stale in an opaque loop the credential re-derive cannot fix.
_time_offset: float = 0.0


def set_time_offset(offset: float) -> None:
    global _time_offset
    _time_offset = float(offset)


def get_time_offset() -> float:
    return _time_offset


def sync_server_time(force: bool = False) -> int:
    # Kept for call-site compatibility. The real sync now lives in
    # polymarket_api._sync_clock_offset() (it owns the HTTP client), which calls
    # set_time_offset(); this just returns the current offset.
    return int(_time_offset)


def now_ts() -> int:
    return int(time.time() + _time_offset)


# ───────── L1 — EIP-712 ClobAuth (derive/create API creds) ────────────

_CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"


def _eip712_sign(typed_data: dict) -> str:
    """Sign an EIP-712 typed-data dict with the wallet key, returning a
    0x-prefixed 65-byte signature."""
    from eth_account.messages import encode_typed_data  # type: ignore
    acct = _load_account()
    signable = encode_typed_data(full_message=typed_data)
    signed = acct.sign_message(signable)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def l1_headers(nonce: int = 0) -> dict:
    """Headers for POST /auth/api-key and GET /auth/derive-api-key."""
    ts = str(now_ts())
    addr = get_address()
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ],
        },
        "primaryType": "ClobAuth",
        "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": CHAIN_ID},
        "message": {
            "address": addr,
            "timestamp": ts,
            "nonce": int(nonce),
            "message": _CLOB_AUTH_MESSAGE,
        },
    }
    return {
        "POLY_ADDRESS": addr,
        "POLY_SIGNATURE": _eip712_sign(typed),
        "POLY_TIMESTAMP": ts,
        "POLY_NONCE": str(int(nonce)),
    }


# ───────── L2 — HMAC request signing ──────────────────────────────────


def _b64url_decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def _b64url_encode(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii").replace("+", "-").replace("/", "_")


def l2_headers(method: str, path: str, body: str = "") -> dict:
    """HMAC-SHA256 auth headers for an authenticated CLOB request.

    The signed payload is `timestamp + METHOD + path + body`. The secret is
    base64url-decoded; the signature is base64url-encoded.
    """
    creds = _load_api_creds()
    if not creds:
        raise RuntimeError("Polymarket API credentials not derived yet")
    ts = str(now_ts())
    msg = f"{ts}{method.upper()}{path}{body or ''}"
    sig = hmac.new(
        _b64url_decode(creds["secret"]), msg.encode("utf-8"), hashlib.sha256
    ).digest()
    return {
        "POLY_ADDRESS": get_address(),
        "POLY_SIGNATURE": _b64url_encode(sig),
        "POLY_TIMESTAMP": ts,
        "POLY_API_KEY": creds["apiKey"],
        "POLY_PASSPHRASE": creds["passphrase"],
    }


# ───────── Order — EIP-712 signing ────────────────────────────────────

# USDC + outcome shares are both denominated with 6 decimals on the CLOB.
_DECIMALS = 1_000_000


def _round_amt(x: float) -> int:
    return int(round(x))


def _load_private_key() -> str:
    """The raw 0x hex private key (for libraries that need the key, not a
    LocalAccount). Same source as `_load_account`."""
    f = _wallet_key_file(_current_env)
    if not f.exists():
        raise FileNotFoundError(f"Polymarket wallet key not configured ({f})")
    return _normalize_priv(_read_secret_bytes(f).decode("utf-8", "replace"))


def verify_signer() -> str:
    """Offline self-test of the deposit-wallet (POLY_1271) order signer. Imports
    the lazy `py_clob_client_v2` chain and SIGNS a throwaway order with a random
    test key — no network, no stored credentials. Proves the signer is bundled +
    functional in THIS build (esp. the frozen PyInstaller backend, where these
    are lazy-imported and trivially missed). Returns the signature hex; raises on
    any failure. NOT a real wallet — the key is fixed garbage."""
    from eth_account import Account
    from py_clob_client_v2.signer import Signer  # type: ignore
    from py_clob_client_v2.order_utils.exchange_order_builder_v2 import ExchangeOrderBuilderV2  # type: ignore
    from py_clob_client_v2.order_utils.model.order_data_v2 import OrderDataV2  # type: ignore
    from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2  # type: ignore
    from py_clob_client_v2.order_utils.model.side import Side  # type: ignore

    test_key = "0x" + "11" * 32  # throwaway, NOT a wallet anyone funds
    addr = Account.from_key(test_key).address
    signer = Signer(test_key, CHAIN_ID)
    builder = ExchangeOrderBuilderV2(EXCHANGE_ADDRESS, CHAIN_ID, signer)
    od = OrderDataV2(
        maker=addr, signer=addr, tokenId="1",
        makerAmount="1000000", takerAmount="2000000",
        side=Side.BUY, signatureType=SignatureTypeV2.POLY_1271, expiration="0",
    )
    s = builder.build_signed_order(od)
    sig = getattr(s, "signature", None)
    if not sig or not str(sig).startswith("0x") or len(str(sig)) < 132:
        raise RuntimeError(f"signer produced no/short signature: {sig!r}")
    return str(sig)


def _create_signed_order_1271(
    *, token_id: str, side: str, price: float, size: float,
    neg_risk: bool, expiration: int,
) -> dict:
    """Build + sign a deposit-wallet (signatureType 3 / POLY_1271) order.

    The order's maker AND signer are the deposit wallet (funder); the wallet
    KEY signs an ERC-7739 "TypedDataSign" wrapper that the deposit wallet's
    ERC-1271 `isValidSignature` validates on-chain. We delegate the (intricate,
    money-critical) signing to Polymarket's official `py_clob_client_v2`
    ExchangeOrderBuilderV2 — verified to recover to a valid signature against
    the live deposit wallet. Returns the `order` dict for the V2 /order body.

    This is THE supported deposit-wallet path: a Polymarket account created via
    Gmail/email signup, with the private key exported FROM Polymarket and the
    Profile/deposit address used as the funder. That pairing is signatureType 3.
    """
    funder = get_funder()
    if not funder:
        raise RuntimeError("POLY_1271 order requested but no funder (deposit wallet) configured")
    from py_clob_client_v2.signer import Signer  # type: ignore
    from py_clob_client_v2.order_utils.exchange_order_builder_v2 import ExchangeOrderBuilderV2  # type: ignore
    from py_clob_client_v2.order_utils.model.order_data_v2 import OrderDataV2  # type: ignore
    from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2  # type: ignore
    from py_clob_client_v2.order_utils.model.side import Side  # type: ignore

    side_u = side.upper()
    if side_u == "BUY":
        maker_amount = _round_amt(price * size * _DECIMALS)   # collateral paid
        taker_amount = _round_amt(size * _DECIMALS)           # shares received
    else:
        maker_amount = _round_amt(size * _DECIMALS)           # shares sold
        taker_amount = _round_amt(price * size * _DECIMALS)   # collateral received

    exchange = NEG_RISK_EXCHANGE_ADDRESS if neg_risk else EXCHANGE_ADDRESS
    signer = Signer(_load_private_key(), CHAIN_ID)
    builder = ExchangeOrderBuilderV2(exchange, CHAIN_ID, signer)
    od = OrderDataV2(
        maker=funder, signer=funder, tokenId=str(int(token_id)),
        makerAmount=str(int(maker_amount)), takerAmount=str(int(taker_amount)),
        side=Side.BUY if side_u == "BUY" else Side.SELL,
        signatureType=SignatureTypeV2.POLY_1271, expiration=str(int(expiration)),
    )
    s = builder.build_signed_order(od)
    return {
        "salt": int(s.salt),
        "maker": s.maker,
        "signer": s.signer,
        "tokenId": str(s.tokenId),
        "makerAmount": str(s.makerAmount),
        "takerAmount": str(s.takerAmount),
        "side": side_u,
        "expiration": str(s.expiration),
        "signatureType": int(s.signatureType),
        "signature": s.signature,
        "timestamp": str(s.timestamp),
        "metadata": s.metadata,
        "builder": s.builder,
    }


def create_signed_order(
    *,
    token_id: str,
    side: str,            # "BUY" | "SELL"
    price: float,         # 0..1 USDC per share
    size: float,          # number of outcome shares
    neg_risk: bool = False,
    expiration: int = 0,  # unix seconds; 0 = good-til-canceled (GTD body only)
    salt: Optional[int] = None,
    ts_ms: Optional[int] = None,
) -> dict:
    """Build + EIP-712-sign a CLOB **V2** limit order. Returns the JSON-ready
    order object (including the signature) for POST /order.

    V2 order struct (11 fields): salt, maker, signer, tokenId, makerAmount,
    takerAmount, side, signatureType, timestamp(ms), metadata, builder.
    `taker`, `nonce`, `feeRateBps` were removed vs V1; fees are protocol-set
    at match time (taker-only) and not user-configurable.

    Signature schemes:
      • POLY_1271 (3): smart-contract wallet — delegates to `_create_signed_order_1271`
        (maker = signer = the deposit wallet; validated on-chain via EIP-1271).
      • EOA (0) / POLY_PROXY (1) / POLY_GNOSIS_SAFE (2): the EOA signs the same
        EIP-712 order inline below — `signer` = the EOA, `maker` = the funder
        (the proxy/safe for 1/2, or the EOA itself for 0), `signatureType` tags
        the scheme so the CLOB authorizes the EOA against the right proxy.
    """
    if get_signature_type() == SIGNATURE_TYPE_POLY_1271:
        return _create_signed_order_1271(
            token_id=token_id, side=side, price=price, size=size,
            neg_risk=neg_risk, expiration=expiration,
        )
    acct = _load_account()
    addr = acct.address
    # Non-1271 deposit-wallet accounts — POLY_PROXY (type 1, Polymarket
    # Magic/email proxy) and POLY_GNOSIS_SAFE (type 2, MetaMask Safe) — sign the
    # EXACT SAME EIP-712 order as a raw EOA: the EOA is the `signer`. They differ
    # only in `maker` (the funds-holding proxy/safe = the funder) and the
    # `signatureType` tag telling the CLOB which proxy scheme to authorize the
    # EOA against. Only POLY_1271 (handled above) puts the wallet itself in the
    # signer slot. This mirrors py_clob_client_v2 OrderBuilder._v2_order_signer()
    # (signer=funder ONLY for POLY_1271, else the EOA) and build_order()
    # (maker=funder always; funder defaults to the EOA for a raw type-0 wallet).
    sig_type = get_signature_type()
    maker = get_funder() or addr
    side_u = side.upper()
    side_int = 0 if side_u == "BUY" else 1

    # maker/taker amounts (integer 1e6 units). Collateral + outcome shares
    # both use 6 decimals on the CLOB.
    if side_u == "BUY":
        maker_amount = _round_amt(price * size * _DECIMALS)   # collateral paid
        taker_amount = _round_amt(size * _DECIMALS)           # shares received
    else:
        maker_amount = _round_amt(size * _DECIMALS)           # shares sold
        taker_amount = _round_amt(price * size * _DECIMALS)   # collateral received

    if salt is None:
        # Deterministic-but-unique salt derived from address+token+time so we
        # never need a PRNG and still avoid collisions.
        salt = int(hashlib.sha256(
            f"{addr}{token_id}{time.time_ns()}".encode()
        ).hexdigest()[:16], 16)
    if ts_ms is None:
        ts_ms = time.time_ns() // 1_000_000

    verifying = NEG_RISK_EXCHANGE_ADDRESS if neg_risk else EXCHANGE_ADDRESS

    order_msg = {
        "salt": int(salt),
        "maker": maker,
        "signer": addr,
        "tokenId": int(token_id),
        "makerAmount": int(maker_amount),
        "takerAmount": int(taker_amount),
        "side": int(side_int),
        "signatureType": int(sig_type),
        "timestamp": int(ts_ms),
        "metadata": ZERO_BYTES32,
        "builder": ZERO_BYTES32,
    }

    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
                {"name": "timestamp", "type": "uint256"},
                {"name": "metadata", "type": "bytes32"},
                {"name": "builder", "type": "bytes32"},
            ],
        },
        "primaryType": "Order",
        "domain": {
            "name": "Polymarket CTF Exchange",
            "version": EXCHANGE_VERSION,
            "chainId": CHAIN_ID,
            "verifyingContract": verifying,
        },
        "message": order_msg,
    }
    signature = _eip712_sign(typed)

    # JSON body: numeric fields are strings except salt + signatureType (int);
    # `side` is the "BUY"/"SELL" string. `expiration` rides along unsigned for
    # GTD handling.
    return {
        "salt": int(salt),
        "maker": maker,
        "signer": addr,
        "taker": ZERO_ADDRESS,
        "tokenId": str(int(token_id)),
        "makerAmount": str(int(maker_amount)),
        "takerAmount": str(int(taker_amount)),
        "expiration": str(int(expiration)),
        "side": side_u,
        "signatureType": int(sig_type),
        "signature": signature,
        "timestamp": str(int(ts_ms)),
        "metadata": ZERO_BYTES32,
        "builder": ZERO_BYTES32,
    }


def prime_credentials(sync_time: bool = True) -> bool:
    """Load the wallet key (and API creds if present). Does not derive — the
    API client derives lazily on first authenticated call."""
    _load_account()
    _load_api_creds()
    return True

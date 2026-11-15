"""MCP server state on disk and in memory: the switch, the token, the bridge.

    <userData>/mcp/settings.json    {"enabled": bool} — off until the user says so
    <userData>/mcp/bridge.json      {"port", "pid", "version", "startedAt"} — how
                                    the stdio relay finds the running backend.
                                    Never holds a secret.
    <userData>/credentials/mcp.token  the bearer token, encrypted at rest the
                                    same way the wallet key is (DPAPI → OS
                                    keychain → 0600 file).

The token is what a client must present; the relay reads it from the store
itself, so a client's config file holds a path and nothing else. Rotating it
cuts off every client that is not launched through the relay.

`kryptscout.config` is imported lazily: the stdio relay sets
KRYPT_SCOUT_USERDATA first, and config reads that at import.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("kryptscout.mcp")

TOKEN_PREFIX = "ks_"
AUDIT_MAX = 200
# A client that has not called in this long is shown as gone.
CLIENT_IDLE_S = 15 * 60


def _base_dir() -> Path:
    from kryptscout import config
    return config.DATA_DIR.parent


def mcp_dir() -> Path:
    return _base_dir() / "mcp"


def settings_path() -> Path:
    return mcp_dir() / "settings.json"


def bridge_path() -> Path:
    return mcp_dir() / "bridge.json"


def token_path() -> Path:
    return _base_dir() / "credentials" / "mcp.token"


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat(
        timespec="seconds"
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- the switch


def is_enabled() -> bool:
    """Whether the user has turned agent access on. Off on any doubt."""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("enabled") is True


def set_enabled(enabled: bool) -> None:
    _write_json(settings_path(), {"enabled": bool(enabled), "changedAt": _iso()})
    audit("app", "settings", "accepted", "agent access turned " + ("on" if enabled else "off"))


# ----------------------------------------------------------------- the token

# Keyed by the token file's path so a different store (a test's temp dir)
# is never answered from another store's cache.
_token_cache: dict[str, Optional[str]] = {}


def load_token(fresh: bool = False) -> Optional[str]:
    """`fresh` skips the cache: the stdio relay is a separate long-lived
    process, and a token rotated by the app after it started has to reach it
    on its next call."""
    path = token_path()
    key = str(path)
    if key in _token_cache and not fresh:
        return _token_cache[key]
    token: Optional[str] = None
    if path.exists():
        from kryptscout.autobet import polymarket_auth as auth
        try:
            token = auth._read_secret_bytes(path).decode("utf-8").strip() or None
        except Exception as exc:  # noqa: BLE001 — a transient read error must not stick
            logger.warning("mcp: could not read the token: %s", exc)
            return None
    _token_cache[key] = token
    return token


def _write_token(token: str) -> None:
    from kryptscout.autobet import polymarket_auth as auth
    path = token_path()
    auth._write_secret_bytes(path, token.encode("utf-8"))
    _token_cache[str(path)] = token


def ensure_token() -> str:
    token = load_token()
    if token:
        return token
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    _write_token(token)
    return token


def rotate_token() -> str:
    """Mint a new token. Any client holding the old one stops working, which
    is the point — it is what you do after a token leaks. Clients launched
    through the relay read the new one on their next call."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    _write_token(token)
    _sessions.clear()
    audit("app", "rotate_token", "accepted", "token rotated; sessions cleared")
    return token


def token_matches(supplied: str) -> bool:
    token = load_token()
    if not token or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))


def masked_token() -> Optional[str]:
    token = load_token()
    return f"{token[:6]}…{token[-4:]}" if token else None


# ---------------------------------------------------------------- the bridge


def app_version() -> str:
    version = os.environ.get("KRYPT_APP_VERSION")
    if version:
        return version
    # The stdio relay is launched by the client, not by Electron, so it has no
    # version in its environment; the running app wrote it to the bridge.
    try:
        return str(json.loads(bridge_path().read_text(encoding="utf-8")).get("version") or "dev")
    except (OSError, ValueError):
        return "dev"


def listening_port() -> Optional[int]:
    """The port this backend serves on. Electron passes it in the environment;
    a hand-run uvicorn has it on the command line."""
    raw = os.environ.get("KRYPT_SCOUT_PORT", "").strip()
    if raw.isdigit():
        return int(raw)
    import sys
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--port" and i + 1 < len(argv) and argv[i + 1].isdigit():
            return int(argv[i + 1])
        if arg.startswith("--port=") and arg[7:].isdigit():
            return int(arg[7:])
    return None


def write_bridge(port: int) -> None:
    _write_json(bridge_path(), {
        "port": int(port),
        "pid": os.getpid(),
        "version": os.environ.get("KRYPT_APP_VERSION") or "dev",
        "startedAt": _iso(),
    })


def clear_bridge() -> None:
    """Remove the bridge — but only OUR bridge. During a restart the new
    backend can be up before the old one finishes shutting down."""
    try:
        path = bridge_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        if int(data.get("pid") or 0) == os.getpid():
            path.unlink()
    except (OSError, ValueError, TypeError):
        pass


def read_bridge() -> Optional[dict]:
    try:
        data = json.loads(bridge_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and int(data.get("port") or 0) > 0 else None
    except (OSError, ValueError, TypeError):
        return None


def sync() -> dict:
    """Make the on-disk state match the switch: a token and a bridge while
    agent access is on, no bridge while it is off. Idempotent; called at
    startup and on every change."""
    enabled = is_enabled()
    port = listening_port()
    if enabled:
        ensure_token()
        if port is not None:
            write_bridge(port)
    else:
        clear_bridge()
        _sessions.clear()
    return {"enabled": enabled, "port": port}


# ------------------------------------------------------- sessions and audit

_sessions: dict[str, dict] = {}
_clients: dict[str, dict] = {}
_audit: deque[dict] = deque(maxlen=AUDIT_MAX)


def safe_label(raw: Any, limit: int = 60) -> str:
    """A client's self-declared name, made fit to show: printable, one line."""
    text = "".join(ch for ch in str(raw or "") if ch.isprintable()).strip()
    return text[:limit] or "unknown client"


def new_session(client_info: Any, protocol: str) -> str:
    info = client_info if isinstance(client_info, dict) else {}
    sid = secrets.token_urlsafe(18)
    _sessions[sid] = {
        "client": safe_label(info.get("title") or info.get("name")),
        "version": safe_label(info.get("version"), 30) if info.get("version") else None,
        "protocol": protocol,
    }
    if len(_sessions) > 64:  # a client that reconnects forever must not grow this
        for stale in list(_sessions)[:-64]:
            _sessions.pop(stale, None)
    return sid


def session_for(sid: Optional[str]) -> dict:
    return _sessions.get(sid or "") or {"client": "unknown client", "version": None, "protocol": None}


def end_session(sid: str) -> None:
    _sessions.pop(sid, None)


def note_call(client: str, tool: str) -> None:
    entry = _clients.setdefault(client, {"client": client, "calls": 0, "firstSeen": _iso()})
    entry["calls"] += 1
    entry["lastTool"] = tool
    entry["lastSeen"] = _iso()
    entry["_ts"] = time.time()


def note_connect(session: dict) -> None:
    client = session.get("client") or "unknown client"
    entry = _clients.setdefault(client, {"client": client, "calls": 0, "firstSeen": _iso()})
    entry["version"] = session.get("version")
    entry["protocol"] = session.get("protocol")
    entry["lastSeen"] = _iso()
    entry["_ts"] = time.time()


def clients() -> list[dict]:
    now = time.time()
    rows = []
    for entry in _clients.values():
        row = {k: v for k, v in entry.items() if not k.startswith("_")}
        row["active"] = now - entry.get("_ts", 0.0) < CLIENT_IDLE_S
        rows.append(row)
    return sorted(rows, key=lambda r: r.get("lastSeen") or "", reverse=True)


def audit(client: str, tool: str, outcome: str, detail: str = "") -> None:
    """Writes and refusals are logged one by one; reads are only counted
    (note_call), or a chatty agent would bury the rows worth reading."""
    _audit.append({
        "at": _iso(),
        "client": safe_label(client),
        "tool": tool,
        "outcome": outcome,
        "detail": str(detail)[:200],
    })


def audit_log(limit: int = 50) -> list[dict]:
    rows = list(_audit)[-max(1, min(limit, AUDIT_MAX)):]
    rows.reverse()
    return rows


def reset_for_tests() -> None:
    _sessions.clear()
    _clients.clear()
    _audit.clear()
    _token_cache.clear()


def status() -> dict:
    from . import catalog
    enabled = is_enabled()
    bridge = read_bridge()
    port = listening_port()
    return {
        "enabled": enabled,
        "port": port,
        "url": f"http://127.0.0.1:{port}/mcp" if port else None,
        "bridge": bool(bridge and bridge.get("pid") == os.getpid()),
        "tokenMasked": masked_token(),
        "serverName": catalog.SERVER_NAME,
        "tools": [
            {
                "name": t["name"],
                "title": t["title"],
                "description": t["description"],
                "writes": t["name"] in catalog.WRITE_TOOLS,
            }
            for t in catalog.TOOLS
        ],
        "clients": clients(),
        "audit": audit_log(),
    }

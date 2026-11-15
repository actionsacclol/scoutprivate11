"""stdio relay for the MCP server — `krypt-scout-backend --mcp`.

Some AI clients (Claude Desktop, most notably) only launch MCP servers as a
child process speaking newline-delimited JSON-RPC on stdin/stdout. The real
server lives inside the RUNNING app (http.py), because that is where the
boards and the ledgers are; this process carries lines between the two.

It finds the app through `<userData>/mcp/bridge.json` (port + pid, no secret)
and reads the bearer token from the encrypted credential store itself, so the
client's config file holds a path and nothing else. The backend's port changes
on every launch; the bridge is how a config written once keeps working.

If the app is closed, the relay still answers `initialize` (or, from a
2026-07-28 client, `server/discover`) and `tools/list` — a client that sees a
dead server at startup usually hides it until restarted — and every tool call
says, in words, that the app needs to be open with agent access on.

stdout carries protocol only. Nothing else may ever be written to it, so this
module imports nothing that logs there, and diagnostics go to stderr.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import catalog, protocol

_out_lock = threading.Lock()
_state_lock = threading.Lock()
_session_id: Optional[str] = None
_init_params: Optional[dict] = None
_need_init = True
_inflight: set = set()

NOT_RUNNING = (
    "Krypt Scout is not reachable. Open the app, go to AI Agents and turn on "
    "'Allow AI agents' — then try again. (The app must stay open while an agent "
    "uses it: the boards and the ledgers live there.)"
)
NO_TOKEN = (
    "Krypt Scout has no access token for agents yet. Open the app, go to AI "
    "Agents and turn 'Allow AI agents' off and on again."
)
UNLICENSED = "Krypt Scout needs an active license before an agent can use it. Activate it in the app."


def default_userdata() -> Path:
    """Where Electron keeps this app's data when nothing says otherwise. Used
    only by a packaged relay started without KRYPT_SCOUT_USERDATA (a config
    written by hand): without it, kryptscout.config would resolve its paths
    inside the read-only frozen bundle."""
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return base / "Krypt Scout"


def prepare_environment(argv: list[str]) -> None:
    """Point the app's paths at userData BEFORE anything imports
    kryptscout.config. `--userdata <path>` wins, then the environment, then
    (packaged builds only) the OS default."""
    chosen = None
    if "--userdata" in argv:
        i = argv.index("--userdata")
        if i + 1 < len(argv):
            chosen = argv[i + 1]
    if chosen:
        os.environ["KRYPT_SCOUT_USERDATA"] = chosen
    elif not os.environ.get("KRYPT_SCOUT_USERDATA") and getattr(sys, "frozen", False):
        os.environ["KRYPT_SCOUT_USERDATA"] = str(default_userdata())


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[{catalog.SERVER_NAME} mcp] {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def _send(obj: Any) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with _out_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


class _Unreachable(Exception):
    pass


def _endpoint() -> tuple[str, str]:
    """(url, token) for the running app, or _Unreachable with the reason."""
    from . import store
    bridge = store.read_bridge()
    if bridge is None:
        raise _Unreachable(NOT_RUNNING)
    token = store.load_token(fresh=True)
    if not token:
        raise _Unreachable(NO_TOKEN)
    return f"http://127.0.0.1:{int(bridge['port'])}/mcp", token


def _post(payload: Any, *, timeout: float = 120.0) -> tuple[Optional[Any], Optional[str]]:
    """POST one message → (parsed JSON body or None for 202, session id)."""
    url, token = _endpoint()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    modern = (
        isinstance(payload, dict)
        and "id" in payload
        and protocol.request_era(payload.get("params"))[0] is not None
    )
    if modern:
        # Over HTTP a modern request repeats its version, method and name in
        # headers; the client spoke stdio and sent none, so we add them.
        headers.update(protocol.modern_headers(payload))
    else:
        with _state_lock:
            if _session_id:
                headers["Mcp-Session-Id"] = _session_id
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            sid = response.headers.get("Mcp-Session-Id")
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            # The modern revision answers some JSON-RPC errors with these
            # statuses. The error in the body is the answer.
            try:
                parsed = json.loads(exc.read() or b"null")
            except (ValueError, OSError):
                parsed = None
            if isinstance(parsed, dict) and "error" in parsed:
                return parsed, None
        if exc.code == 401:
            raise _Unreachable(
                "Krypt Scout refused this client's token. It may have been rotated while "
                "the app was closed; open the AI Agents page and try again."
            )
        if exc.code == 402:
            raise _Unreachable(UNLICENSED)
        if exc.code == 503:
            raise _Unreachable(NOT_RUNNING)
        raise _Unreachable(f"Krypt Scout answered HTTP {exc.code}.")
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        _log(f"bridge unreachable: {exc}")
        raise _Unreachable(NOT_RUNNING)
    return (json.loads(body) if body else None), sid


def _upstream_init() -> None:
    """(Re)introduce this client to the app, so the AI Agents page shows its
    real name. Done lazily: the app may have started after the client did, or
    restarted since."""
    global _session_id, _need_init
    with _state_lock:
        params = _init_params
    if params is None:
        return
    _, sid = _post({"jsonrpc": "2.0", "id": "relay-init", "method": "initialize",
                    "params": params}, timeout=15)
    with _state_lock:
        _session_id = sid
        _need_init = False
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=15)


def _handle(msg: Any) -> None:
    global _init_params, _need_init
    if not isinstance(msg, dict):
        _send(protocol.err(None, -32600, "invalid request"))
        return
    method = msg.get("method")
    rid = msg.get("id")
    is_request = "id" in msg and isinstance(method, str)
    modern, bad = protocol.request_era(msg.get("params"))
    if bad is not None:
        if is_request:
            _send({"jsonrpc": "2.0", "id": rid, "error": bad})
        return

    if method == "server/discover" and modern and is_request:
        # A modern client's opening probe. Answered even with the app closed,
        # or a client that hears nothing falls back to the old handshake.
        try:
            response, _ = _post(msg, timeout=15)
        except _Unreachable:
            response = None
        _send(response if isinstance(response, dict) else protocol.ok(rid, protocol.discover_result()))
        return

    if method == "initialize":
        with _state_lock:
            _init_params = dict(msg.get("params") or {})
            _need_init = True
        try:
            _upstream_init()
        except _Unreachable:
            pass
        # Answered locally either way, so a client started before the app
        # still comes up with every tool listed.
        _send(protocol.ok(rid, protocol.initialize_result(msg.get("params") or {})))
        return

    if method == "notifications/initialized":
        return  # sent upstream by _upstream_init

    if method in ("tools/list", "ping", "prompts/list", "resources/list", "resources/templates/list"):
        # Static: answering these must not depend on the app being open.
        result: dict = {
            "tools/list": {"tools": catalog.list_tools()},
            "ping": {},
            "prompts/list": {"prompts": []},
            "resources/list": {"resources": []},
            "resources/templates/list": {"resourceTemplates": []},
        }[method]
        if is_request:
            _send(protocol.ok(rid, protocol.modern_result(method, result) if modern else result))
        return

    try:
        with _state_lock:
            need = _need_init
        if need:
            _upstream_init()
        response, _ = _post(msg)
        if response is not None and is_request:
            _send(response)
    except _Unreachable as exc:
        with _state_lock:
            _need_init = True
        if not is_request:
            return
        if method == "tools/call":
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            _send(protocol.ok(rid, protocol.modern_result(method, result) if modern else result))
        else:
            _send(protocol.err(rid, -32603, str(exc)))


def _end_session() -> None:
    """Tell the app this client has gone. Best effort: a client closing must
    never wait on the app."""
    with _state_lock:
        sid = _session_id
    if not sid:
        return
    try:
        url, token = _endpoint()
    except _Unreachable:
        return
    request = urllib.request.Request(url, method="DELETE", headers={
        "Authorization": f"Bearer {token}", "Mcp-Session-Id": sid,
    })
    try:
        with urllib.request.urlopen(request, timeout=3.0) as response:
            response.read()
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        _log(f"could not end the session: {exc}")


def _spawn(msg: Any) -> None:
    """Handle one message on its own thread (a slow tool call must not hold
    up a ping), remembered until it finishes so EOF can wait for it."""
    def _run() -> None:
        try:
            _handle(msg)
        except Exception as exc:  # noqa: BLE001 — one bad message must not end the relay
            _log(f"unhandled error: {type(exc).__name__}: {exc}")
            if isinstance(msg, dict) and "id" in msg:
                _send(protocol.err(msg.get("id"), -32603, "internal relay error"))
        finally:
            _inflight.discard(threading.current_thread())
    thread = threading.Thread(target=_run, daemon=True)
    _inflight.add(thread)
    thread.start()


def main(argv: Optional[list[str]] = None) -> int:
    prepare_environment(list(sys.argv if argv is None else argv))
    _log("relay started")
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            # stdin closed. Let calls already in flight answer before exiting.
            deadline = time.monotonic() + 60.0
            for thread in list(_inflight):
                thread.join(max(0.0, deadline - time.monotonic()))
            _end_session()
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _send(protocol.err(None, -32700, "parse error"))
            continue
        if isinstance(msg, list):
            for item in msg:
                _spawn(item)
            continue
        if isinstance(msg, dict) and msg.get("method") == "initialize":
            _handle(msg)  # in order: nothing may overtake initialize
            continue
        _spawn(msg)

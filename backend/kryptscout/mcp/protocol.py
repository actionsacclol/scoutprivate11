"""JSON-RPC for the MCP server — both protocol eras, no transport.

Two ways a client can talk:

  * The handshake era (2025-11-25 and earlier): `initialize` opens a session,
    the server returns an Mcp-Session-Id, later requests carry it.
  * The stateless 2026-07-28 revision: no handshake and no session. A client
    may open with `server/discover`, and every request names its protocol
    version and capabilities in `params._meta`.

Both are spoken because clients in the field are split between them. Nothing
in this module touches the network or the rest of the app except through
tools.call, so the stdio relay can import it to shape its own local answers.
"""
from __future__ import annotations

import base64
from typing import Any, Optional

from . import catalog

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_SERVER = "io.modelcontextprotocol/serverInfo"
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022
# A modern list or discover result may be reused this long, under the same token.
CACHE_HINT = {"ttlMs": 60_000, "cacheScope": "private"}
_CACHEABLE = ("server/discover", "tools/list", "prompts/list", "resources/list",
              "resources/templates/list")
# Methods whose name a modern HTTP request repeats in the Mcp-Name header.
_NAME_FIELD = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}
_B64_HEAD, _B64_TAIL = "=?base64?", "?="


def err(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def ok(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def server_info(version: Optional[str] = None) -> dict:
    if version is None:
        from . import store
        version = store.app_version()
    return {"name": catalog.SERVER_NAME, "title": catalog.SERVER_TITLE, "version": version}


def request_era(params: Any) -> tuple[Optional[str], Optional[dict]]:
    """(modern version, error) for one request's params.

    (None, None) is a handshake-era request: it names no version of its own
    (that lives in the session `initialize` opened), or names an old one. An
    error is the JSON-RPC error object to answer with — an unknown version
    gets the list we speak, so the client can retry with one of them."""
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict) or META_VERSION not in meta:
        return None, None
    version = meta[META_VERSION]
    if version in catalog.PROTOCOL_VERSIONS:
        return None, None
    if version not in catalog.MODERN_VERSIONS:
        return None, {
            "code": UNSUPPORTED_VERSION,
            "message": "Unsupported protocol version",
            "data": {"supported": list(catalog.SUPPORTED_VERSIONS), "requested": str(version)},
        }
    if not isinstance(meta.get(META_CAPABILITIES), dict):
        return None, {
            "code": -32602,
            "message": f"_meta is missing {META_CAPABILITIES} (an object; {{}} declares none)",
        }
    return version, None


def modern_result(method: str, result: dict) -> dict:
    """A result as the modern revision shapes it: resultType on every one, who
    answered in _meta, and a cache hint on the lists."""
    out = {
        **result,
        "resultType": "complete",
        "_meta": {**(result.get("_meta") or {}), META_SERVER: server_info()},
    }
    if method in _CACHEABLE:
        out.update(CACHE_HINT)
    return out


def discover_result(instructions: str = catalog.INSTRUCTIONS) -> dict:
    return modern_result("server/discover", {
        "supportedVersions": list(catalog.SUPPORTED_VERSIONS),
        "capabilities": catalog.CAPABILITIES,
        "instructions": instructions,
    })


def initialize_result(params: dict) -> dict:
    asked = str(params.get("protocolVersion") or "")
    version = asked if asked in catalog.PROTOCOL_VERSIONS else catalog.PROTOCOL_VERSIONS[0]
    return {
        "protocolVersion": version,
        "capabilities": catalog.CAPABILITIES,
        "serverInfo": server_info(),
        "instructions": catalog.INSTRUCTIONS,
    }


# ------------------------------------------------- modern HTTP header mirror


def encode_header_value(value: str) -> str:
    """A value fit for an HTTP header: as is when it is plain visible ASCII,
    else the revision's base64 form."""
    plain = (
        bool(value)
        and value == value.strip()
        and all(ch == "\t" or " " <= ch <= "~" for ch in value)
        and not (value.startswith(_B64_HEAD) and value.endswith(_B64_TAIL))
    )
    if plain:
        return value
    return _B64_HEAD + base64.b64encode(value.encode("utf-8")).decode("ascii") + _B64_TAIL


def decode_header_value(value: str) -> Optional[str]:
    if not (
        value.startswith(_B64_HEAD)
        and value.endswith(_B64_TAIL)
        and len(value) >= len(_B64_HEAD) + len(_B64_TAIL)
    ):
        return value
    try:
        return base64.b64decode(value[len(_B64_HEAD):-len(_B64_TAIL)], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _meta_of(msg: dict) -> tuple[dict, dict]:
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    return params, meta


def modern_headers(msg: dict) -> dict:
    """The headers a modern request repeats from its body over HTTP. The stdio
    relay adds these on the client's behalf."""
    params, meta = _meta_of(msg)
    out = {
        "MCP-Protocol-Version": str(meta.get(META_VERSION) or ""),
        "Mcp-Method": str(msg.get("method") or ""),
    }
    field = _NAME_FIELD.get(str(msg.get("method")))
    if field and isinstance(params.get(field), str):
        out["Mcp-Name"] = encode_header_value(params[field])
    return out


def header_mismatch(headers: dict, msg: dict) -> Optional[str]:
    """Why a modern HTTP request's headers disagree with its body, or None.
    Something in front of us may route on the headers while we act on the
    body, so the revision has the two agree. `headers` keys are lower-case."""
    params, meta = _meta_of(msg)
    want = [("MCP-Protocol-Version", meta.get(META_VERSION)), ("Mcp-Method", msg.get("method"))]
    field = _NAME_FIELD.get(str(msg.get("method")))
    if field:
        want.append(("Mcp-Name", params.get(field)))
    for name, value in want:
        got = headers.get(name.lower())
        if got is None:
            return f"the {name} header is missing"
        if name == "Mcp-Name":
            got = decode_header_value(got)
        if got != value:
            return f"the {name} header does not match the request body"
    return None


# ---------------------------------------------------------------- dispatcher


def _modern_session(params: dict) -> dict:
    """A modern request's session, built from its own _meta and never kept."""
    from . import store
    meta = params.get("_meta") or {}
    info = meta.get(META_CLIENT) if isinstance(meta.get(META_CLIENT), dict) else {}
    return {
        "client": store.safe_label(info.get("title") or info.get("name")),
        "version": store.safe_label(info.get("version"), 30) if info.get("version") else None,
        "protocol": meta.get(META_VERSION),
    }


async def handle_message(msg: Any, session_id: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    """One JSON-RPC message → (response, or None for a notification; the new
    session id when this was an `initialize`)."""
    from . import store, tools

    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return err(None, -32600, "invalid request"), None
    method = msg.get("method")
    rid = msg.get("id")
    is_notification = "id" not in msg
    if not isinstance(method, str):
        return None, None  # a response to something we sent; we send nothing
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return (None if is_notification else err(rid, -32602, "params must be an object")), None
    modern, bad = request_era(params)
    if bad is not None:
        return (None if is_notification else {"jsonrpc": "2.0", "id": rid, "error": bad}), None

    if method == "initialize":
        result = initialize_result(params)
        sid = store.new_session(params.get("clientInfo"), result["protocolVersion"])
        store.note_connect(store.session_for(sid))
        return ok(rid, result), sid

    if is_notification:
        return None, None  # notifications/initialized, cancelled, …

    def done(result: dict) -> tuple[dict, None]:
        return ok(rid, modern_result(method, result) if modern else result), None

    if method == "server/discover":
        if not modern:
            return err(rid, -32602,
                       f"server/discover needs _meta with {META_VERSION} and {META_CAPABILITIES}"), None
        store.note_connect(_modern_session(params))
        return ok(rid, discover_result()), None
    if method == "ping":
        return done({})
    if method == "tools/list":
        return done({"tools": catalog.list_tools()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return err(rid, -32602, "tools/call needs a name and an arguments object"), None
        if name not in catalog.TOOL_NAMES:
            return err(rid, -32602, f"unknown tool: {name}"), None
        session = _modern_session(params) if modern else store.session_for(session_id)
        return done(await tools.call(name, args, session))
    # Clients probe these even when we do not advertise them; an empty list is
    # a better answer than an error some clients surface as "server broken".
    if method == "resources/list":
        return done({"resources": []})
    if method == "resources/templates/list":
        return done({"resourceTemplates": []})
    if method == "prompts/list":
        return done({"prompts": []})
    return err(rid, -32601, f"method not found: {method}"), None


async def handle_body(body: Any, session_id: Optional[str]) -> tuple[Optional[Any], Optional[str]]:
    """A POST body is one message or a batch of them."""
    if isinstance(body, list):
        if not body:
            return err(None, -32600, "empty batch"), None
        out, new_sid = [], None
        for message in body:
            response, sid = await handle_message(message, session_id)
            new_sid = new_sid or sid
            if response is not None:
                out.append(response)
        return (out or None), new_sid
    return await handle_message(body, session_id)

"""The MCP HTTP transport: POST /mcp on the backend's own listener.

Streamable HTTP with JSON responses only — no SSE stream, because nothing
here ever pushes unprompted. api/main.py mounts `handle` and checks the
license before calling it; everything else a request has to pass is here, in
this order:

  1. Host must be a loopback name and there must be no Origin header. That is
     the DNS-rebinding defence: a web page in the user's browser can reach
     127.0.0.1, and a browser always sends Origin on a cross-origin POST.
     Desktop MCP clients and scripts do not.
  2. A bearer token (store.py). The per-launch X-Scout-Token that guards /api
     is the renderer's and is deliberately not accepted here.
  3. Agent access must be switched on.
"""
from __future__ import annotations

import json
from typing import Any

from . import catalog, protocol, store

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
MAX_BODY_BYTES = 1024 * 1024


def host_ok(host: str) -> bool:
    name = host.strip().lower()
    if name.startswith("["):
        name = name[: name.find("]") + 1]
    else:
        name = name.split(":", 1)[0]
    return name in LOOPBACK_HOSTS


def _plain(status: int, text: str, headers: dict | None = None):
    from starlette.responses import PlainTextResponse
    return PlainTextResponse(text, status_code=status, headers=headers)


def _json(status: int, payload: Any, headers: dict | None = None):
    from starlette.responses import Response
    return Response(
        json.dumps(payload, default=str, ensure_ascii=False),
        status_code=status,
        media_type="application/json",
        headers=headers,
    )


def refusal(headers: dict) -> tuple[int, str, dict] | None:
    """(status, message, extra headers) when a request may not go further.
    `headers` keys are lower-case."""
    if not host_ok(headers.get("host", "")):
        return 403, "forbidden host", {}
    if headers.get("origin"):
        return 403, "browser requests are not accepted", {}
    sent = headers.get("authorization", "")
    if not sent.lower().startswith("bearer ") or not store.token_matches(sent[7:].strip()):
        return 401, "unauthorized", {"WWW-Authenticate": f'Bearer realm="{catalog.SERVER_NAME}"'}
    if not store.is_enabled():
        return 503, "AI agent access is turned off in Krypt Scout", {}
    return None


async def handle(request):
    """Serve one request to /mcp. The caller has already checked the license."""
    headers = {k.lower(): v for k, v in request.headers.items()}
    refused = refusal(headers)
    if refused is not None:
        status, message, extra = refused
        return _plain(status, message, extra)

    if request.method == "DELETE":
        store.end_session(headers.get("mcp-session-id", ""))
        return _plain(200, "")
    if request.method != "POST":
        # No server-initiated stream: there is nothing to GET.
        return _plain(405, "method not allowed", {"Allow": "POST, DELETE"})

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _plain(413, "request body too large")
    try:
        body = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json(200, protocol.err(None, -32700, "parse error"))

    # A modern request: its headers must repeat its body (checked before
    # anything runs), it has no session, and the revision fixes the HTTP
    # status of the errors a client tells the eras apart by.
    modern = bad = None
    if isinstance(body, dict) and "id" in body:
        modern, bad = protocol.request_era(body.get("params"))
        if bad is None and (modern or headers.get("mcp-protocol-version") in catalog.MODERN_VERSIONS):
            why = protocol.header_mismatch(headers, body)
            if why:
                return _json(400, protocol.err(body.get("id"), protocol.HEADER_MISMATCH,
                                               f"Header mismatch: {why}"))

    response, new_sid = await protocol.handle_body(
        body, None if modern else headers.get("mcp-session-id")
    )
    extra = {"Mcp-Session-Id": new_sid} if new_sid else {}
    if response is None:
        return _plain(202, "", extra)
    status = 200
    if bad is not None:
        status = 400
    elif modern and isinstance(response, dict) and (response.get("error") or {}).get("code") == -32601:
        status = 404
    return _json(status, response, extra)

"""MCP server — let an AI agent (Claude, Cursor, Codex, …) read Krypt Scout.

The Model Context Protocol is how desktop AI clients call tools. Scout serves
it from INSIDE the running backend, so an agent sees exactly what the app's
own screens see: the same boards, the same sharp fair, the same pick ledger.

    catalog.py    the server's name, instructions and tool definitions. Pure
                  data with no imports from the rest of the app, so the stdio
                  relay can list tools without loading it.
    protocol.py   JSON-RPC: the initialize handshake (2025-11-25 and earlier)
                  and the stateless 2026-07-28 revision.
    tools.py      what each tool does, and the one dispatcher every call
                  goes through (audit, errors, size cap).
    store.py      on/off switch, bearer token, bridge file, audit log.
    http.py       the POST /mcp transport, mounted by api/main.py.
    stdio.py      `krypt-scout-backend --mcp`: a relay for clients that launch
                  a child process instead of speaking HTTP.

Off until the user turns it on. Loopback only, bearer token, and behind the
same license gate as the rest of the API.
"""

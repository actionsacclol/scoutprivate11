"""MCP server tests — the agent's door (/mcp), the renderer's controls
(/api/mcp/*), the tools, and the stdio relay.

No network and no real engines. Everything on disk (settings, bridge, token,
the SQLite store) lives under tmp_path; the token goes through the same
at-rest encryption the wallet key uses.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kryptscout import config
from kryptscout.api import main
from kryptscout.core.schemas import (
    Edge, FairPrices, Forecast, Game, GameCard, MoneylineOdds, Prediction, Sport,
)
from kryptscout.mcp import catalog, protocol, stdio, store, tools
from kryptscout.sports import base as engines
from kryptscout.store import db

NOW = datetime.now(timezone.utc).replace(microsecond=0)
LOOPBACK = {"host": "127.0.0.1:8123"}
MODERN = "2026-07-28"


class FakeEngine:
    def __init__(self, sport: Sport, model_name: str) -> None:
        self.sport = sport
        self.model_name = model_name


@pytest.fixture(autouse=True)
def _scratch_store(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.setenv("KRYPT_SCOUT_PORT", "8123")
    monkeypatch.setattr(
        engines, "_REGISTRY",
        {Sport.NBA: FakeEngine(Sport.NBA, "nba-elo-v1"), Sport.NFL: FakeEngine(Sport.NFL, "nfl-elo-v2")},
    )
    store.reset_for_tests()
    yield
    store.reset_for_tests()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main.app.router, "on_startup", [])
    monkeypatch.setattr(main.app.router, "on_shutdown", [])
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture()
def token() -> str:
    """Agent access on, with a token — the state after the user flips the switch."""
    store.set_enabled(True)
    store.sync()
    return store.load_token()


def _auth(token: str, **extra) -> dict:
    return {**LOOPBACK, "authorization": f"Bearer {token}", **extra}


def _rpc(client, token: str, method: str, params: dict | None = None, rid=1, headers=None):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers={**_auth(token), **(headers or {})})


def _tool_text(response) -> tuple[dict | str, bool]:
    result = response.json()["result"]
    text = result["content"][0]["text"]
    try:
        return json.loads(text), result["isError"]
    except ValueError:
        return text, result["isError"]


def _card(game_id: str = "nba-1", start: datetime | None = None) -> GameCard:
    start = start or NOW + timedelta(hours=5)
    game = Game(id=game_id, sport=Sport.NBA, start_time=start, home="Boston Celtics", away="Miami Heat")
    fetched = NOW - timedelta(minutes=1)
    return GameCard(
        game=game,
        prediction=Prediction(
            game_id=game_id, sport=Sport.NBA, model="nba-elo-v1", generated_at=NOW,
            home_win_prob=0.61234567, away_win_prob=0.38765433, predicted_total=221.4,
        ),
        odds=[
            MoneylineOdds(book="draftkings", fetched_at=fetched, home_ml=-150, away_ml=130),
            MoneylineOdds(book="kalshi", fetched_at=fetched, home_ml=-170, away_ml=165),
        ],
        edges=[Edge(
            game_id=game_id, sport=Sport.NBA, side="home", book="draftkings", price=-150,
            model_prob=0.612, market_prob=0.59, ev_per_dollar=0.031, kelly_fraction=0.04,
            fair_prob=0.6185, ref_book="kalshi", gap_pts=1.85, agree=True,
        )],
        fair=FairPrices(
            game_id=game_id, sport=Sport.NBA, source="kalshi", sharp=True,
            home=0.6185, away=0.3815, fetched_at=fetched, depth_usd=4200.0, spread_pts=1.0,
        ),
        forecast=Forecast(
            game_id=game_id, sport=Sport.NBA, kind="market", source="kalshi", sharp=True,
            home=0.6185, away=0.3815, model_home=0.612, model_away=0.388,
        ),
    )


# ---------------------------------------------------------------- the door


def test_off_by_default_and_refuses_without_a_token(client) -> None:
    assert store.is_enabled() is False
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=LOOPBACK)
    assert response.status_code == 401
    assert "Bearer" in response.headers["www-authenticate"]


def test_a_valid_token_is_not_enough_while_the_switch_is_off(client) -> None:
    token = store.ensure_token()
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=_auth(token))
    assert response.status_code == 503
    assert "turned off" in response.text


def test_the_renderer_token_does_not_open_the_agent_door(client, token, monkeypatch) -> None:
    """X-Scout-Token guards /api for the renderer. It must not double as an
    agent credential: anything that can read the renderer's memory would
    otherwise hold a second key."""
    monkeypatch.setattr(main, "_API_TOKEN", "renderer-secret")
    response = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={**LOOPBACK, "x-scout-token": "renderer-secret"},
    )
    assert response.status_code == 401
    # And the agent's token does not open /api either.
    assert client.get("/api/mcp", headers={"authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example", "192.168.1.5:8123"])
def test_a_non_loopback_host_is_refused(client, token, host) -> None:
    """DNS rebinding: the browser resolves evil.example to 127.0.0.1 and sends
    that name as Host."""
    response = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"host": host, "authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_a_browser_request_is_refused_even_with_the_token(client, token) -> None:
    response = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers=_auth(token, origin="http://localhost:5173"),
    )
    assert response.status_code == 403
    assert "browser" in response.text


def test_unlicensed_is_402_before_anything_else(client, token, monkeypatch) -> None:
    monkeypatch.setattr(main, "_licensed_now", lambda: False)
    response = _rpc(client, token, "tools/list")
    assert response.status_code == 402
    assert response.json()["licenseRequired"] is True


def test_get_is_not_a_stream(client, token) -> None:
    response = client.get("/mcp", headers=_auth(token))
    assert response.status_code == 405
    assert response.headers["allow"] == "POST, DELETE"


def test_garbage_is_a_parse_error_not_a_500(client, token) -> None:
    response = client.post("/mcp", content=b"{not json", headers=_auth(token))
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32700


# ------------------------------------------------------- handshake protocol


def test_initialize_opens_a_session_and_names_the_client(client, token) -> None:
    response = _rpc(client, token, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "claude-code", "title": "Claude Code", "version": "9.9"},
    })
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "krypt-scout"
    assert "PRE-GAME" in result["instructions"]
    sid = response.headers["mcp-session-id"]
    assert sid

    # The notification that follows has no reply body.
    note = client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_auth(token, **{"mcp-session-id": sid}),
    )
    assert note.status_code == 202 and note.text == ""
    assert [c["client"] for c in store.clients()] == ["Claude Code"]


def test_an_unknown_handshake_version_gets_our_newest(client, token) -> None:
    result = _rpc(client, token, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert result["protocolVersion"] == catalog.PROTOCOL_VERSIONS[0]


def test_tools_list_matches_the_handlers(client, token) -> None:
    listed = _rpc(client, token, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in listed} == set(tools.HANDLERS)
    for tool in listed:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["description"]
    writes = {t["name"] for t in listed if not t["annotations"]["readOnlyHint"]}
    assert writes == {"log_bet", "delete_bet", "place_bet"}
    # The one tool that reaches an exchange says so.
    assert {t["name"] for t in listed if t["annotations"]["openWorldHint"]} == {"place_bet"}


def test_probe_methods_answer_empty_rather_than_erroring(client, token) -> None:
    assert _rpc(client, token, "resources/list").json()["result"] == {"resources": []}
    assert _rpc(client, token, "prompts/list").json()["result"] == {"prompts": []}
    assert _rpc(client, token, "ping").json()["result"] == {}
    assert _rpc(client, token, "no/such").json()["error"]["code"] == -32601


def test_a_batch_is_answered_as_a_batch(client, token) -> None:
    response = client.post("/mcp", headers=_auth(token), json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    assert [r["id"] for r in response.json()] == [1, 2]


# --------------------------------------------------------- stateless protocol


def _modern(method: str, params: dict | None = None, rid=1) -> tuple[dict, dict]:
    params = dict(params or {})
    params["_meta"] = {
        protocol.META_VERSION: MODERN,
        protocol.META_CAPABILITIES: {},
        protocol.META_CLIENT: {"name": "codex", "version": "2.0"},
    }
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    return body, protocol.modern_headers(body)


def test_modern_discover_needs_no_handshake(client, token) -> None:
    body, headers = _modern("server/discover")
    response = client.post("/mcp", json=body, headers={**_auth(token), **headers})
    assert response.status_code == 200
    result = response.json()["result"]
    assert MODERN in result["supportedVersions"] and "2025-11-25" in result["supportedVersions"]
    assert result["resultType"] == "complete"
    assert result["_meta"][protocol.META_SERVER]["name"] == "krypt-scout"
    assert "mcp-session-id" not in response.headers


def test_modern_tools_list_carries_a_cache_hint(client, token) -> None:
    body, headers = _modern("tools/list")
    result = client.post("/mcp", json=body, headers={**_auth(token), **headers}).json()["result"]
    assert result["ttlMs"] > 0 and result["cacheScope"] == "private"
    assert len(result["tools"]) == len(catalog.TOOLS)


def test_modern_headers_must_repeat_the_body(client, token) -> None:
    body, headers = _modern("tools/call", {"name": "get_status", "arguments": {}})
    headers["Mcp-Name"] = "get_board"  # something in front routes on this; we act on the body
    response = client.post("/mcp", json=body, headers={**_auth(token), **headers})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == protocol.HEADER_MISMATCH


def test_modern_unknown_version_lists_the_ones_we_speak(client, token) -> None:
    body, headers = _modern("ping")
    body["params"]["_meta"][protocol.META_VERSION] = "2031-01-01"
    response = client.post("/mcp", json=body, headers={**_auth(token), **headers})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == protocol.UNSUPPORTED_VERSION
    assert MODERN in error["data"]["supported"]


def test_modern_unknown_method_is_a_404(client, token) -> None:
    body, headers = _modern("no/such")
    response = client.post("/mcp", json=body, headers={**_auth(token), **headers})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == -32601


def test_header_values_survive_non_ascii() -> None:
    for value in ("get_board", "café", " padded ", "=?base64?x?="):
        assert protocol.decode_header_value(protocol.encode_header_value(value)) == value
    assert protocol.encode_header_value("get_board") == "get_board"


# -------------------------------------------------------------------- tools


def test_get_status_reports_the_engines(client, token) -> None:
    payload, is_error = _tool_text(_rpc(client, token, "tools/call", {"name": "get_status", "arguments": {}}))
    assert not is_error
    assert payload["engines"] == [
        {"sport": "nba", "model": "nba-elo-v1"}, {"sport": "nfl", "model": "nfl-elo-v2"},
    ]
    assert payload["autobet"]["wallet_connected"] is False


def test_a_misspelt_argument_is_refused_not_ignored(client, token) -> None:
    payload, is_error = _tool_text(_rpc(
        client, token, "tools/call", {"name": "get_board", "arguments": {"sports": "nba"}},
    ))
    assert is_error
    assert "unknown argument(s) sports" in payload and "sport" in payload
    refused = [row for row in store.audit_log() if row["outcome"] == "refused"]
    assert refused and refused[0]["tool"] == "get_board"


def test_unknown_tool_and_bad_sport(client, token) -> None:
    response = _rpc(client, token, "tools/call", {"name": "place_order", "arguments": {}})
    assert response.json()["error"]["code"] == -32602
    payload, is_error = _tool_text(_rpc(
        client, token, "tools/call", {"name": "get_board", "arguments": {"sport": "cricket"}},
    ))
    assert is_error and "unknown sport" in payload


def test_trim_card_keeps_the_numbers_and_drops_the_bulk() -> None:
    row = tools.trim_card(_card())
    assert row["game_id"] == "nba-1" and row["started"] is False
    assert row["forecast"] == {"kind": "market", "source": "kalshi", "sharp": True, "home": 0.6185, "away": 0.3815}
    assert row["model"] == {"name": "nba-elo-v1", "home": 0.6123, "away": 0.3877, "total": 221.4}
    assert row["fair"]["depth_usd"] == 4200.0 and "thin_book" not in row["fair"]
    assert row["prices"] == [
        {"book": "draftkings", "home": -150, "away": 130},
        {"book": "kalshi", "exchange": True, "home": -170, "away": 165},
    ]
    assert row["value"] == {
        "tier": "STRONG", "side": "home", "team": "Boston Celtics", "book": "draftkings",
        "price": -150, "fair_prob": 0.6185, "ref_book": "kalshi", "ev_per_dollar": 0.031,
    }
    assert "arb" not in row and "lean" not in row
    assert len(json.dumps(row)) < len(_card().model_dump_json())


def test_trim_card_flags_live_prices_after_kickoff() -> None:
    """Every probability here is pre-game; a price quoted after kickoff is the
    score talking, and the agent has to be able to see that."""
    row = tools.trim_card(_card(start=NOW - timedelta(minutes=30)))
    assert row["started"] is True
    assert all(price.get("live") is True for price in row["prices"])


def test_get_board_filters_and_limits(client, token, monkeypatch) -> None:
    plain = _card("nba-2")
    plain.edges, plain.odds, plain.fair = [], [], None

    async def fake_board(sport, with_odds=True):
        assert sport is Sport.NBA and with_odds is True
        return [_card("nba-1"), plain, _card("nba-3")]

    monkeypatch.setattr(main, "board", fake_board)

    def board(**arguments):
        return _tool_text(_rpc(client, token, "tools/call", {"name": "get_board", "arguments": arguments}))

    payload, is_error = board(sport="nba")
    assert not is_error and payload["games_on_slate"] == 3
    assert [g["game_id"] for g in payload["games"]] == ["nba-1", "nba-2", "nba-3"]
    payload, _ = board(sport="nba", only="value")
    assert [g["game_id"] for g in payload["games"]] == ["nba-1", "nba-3"]
    payload, _ = board(sport="nba", only="value", limit=1)
    assert len(payload["games"]) == 1 and "1 more games match" in payload["more"]
    payload, _ = board(sport="nba", only="arb")
    assert payload["games"] == []


def test_get_board_upcoming_leaves_out_games_under_way(client, token, monkeypatch) -> None:
    async def fake_board(sport, with_odds=True):
        return [_card("nba-live", start=NOW - timedelta(hours=1)), _card("nba-later")]

    monkeypatch.setattr(main, "board", fake_board)
    payload, _ = _tool_text(_rpc(
        client, token, "tools/call", {"name": "get_board", "arguments": {"sport": "nba", "only": "upcoming"}},
    ))
    assert [g["game_id"] for g in payload["games"]] == ["nba-later"]
    assert payload["games_on_slate"] == 2


def test_get_game_says_why_an_id_is_missing(client, token, monkeypatch) -> None:
    async def fake_board(sport, with_odds=True):
        return [_card("nba-1")]

    monkeypatch.setattr(main, "board", fake_board)
    payload, is_error = _tool_text(_rpc(
        client, token, "tools/call", {"name": "get_game", "arguments": {"sport": "nba", "game_id": "nba-404"}},
    ))
    assert is_error and "get_board for current ids" in payload
    payload, is_error = _tool_text(_rpc(
        client, token, "tools/call", {"name": "get_game", "arguments": {"sport": "nba", "game_id": "nba-1"}},
    ))
    assert not is_error and payload["home"] == "Boston Celtics" and "line_movement" in payload


def test_an_endpoint_error_becomes_a_sentence(client, token) -> None:
    """ncaab has no engine in this registry: the API's 404 must reach the
    agent as words, not as a stack trace or a status code."""
    payload, is_error = _tool_text(_rpc(
        client, token, "tools/call", {"name": "get_board", "arguments": {"sport": "ncaab"}},
    ))
    assert is_error and payload == "No engine registered for ncaab"


def test_a_crash_inside_a_tool_is_contained(client, token, monkeypatch) -> None:
    async def boom(sport, with_odds=True):
        raise RuntimeError("espn on fire: secret-detail")

    monkeypatch.setattr(main, "board", boom)
    payload, is_error = _tool_text(_rpc(
        client, token, "tools/call", {"name": "get_board", "arguments": {"sport": "nba"}},
    ))
    assert is_error and "failed inside Krypt Scout (RuntimeError)" in payload
    assert "secret-detail" not in payload
    assert store.audit_log()[0]["outcome"] == "error"


def test_log_bet_then_delete_it(client, token) -> None:
    conn = db.connect()
    try:
        with conn:
            db.upsert_game(conn, "nba-1", "nba", "", NOW + timedelta(hours=3), "Boston Celtics", "Miami Heat")
    finally:
        conn.close()

    def call(name, **arguments):
        return _tool_text(_rpc(client, token, "tools/call", {"name": name, "arguments": arguments}))

    payload, is_error = call("log_bet", game_id="nba-1", side="home", price=-110, book="draftkings", stake_units=2)
    assert not is_error and "Nothing was placed" in payload["note"]
    bet_id = payload["logged"]["id"]

    mine, _ = call("get_my_bets")
    assert [b["id"] for b in mine["bets"]] == [bet_id]

    payload, is_error = call("log_bet", game_id="nba-1", side="home", price=-50)
    assert is_error and "could not be logged" in payload  # not American odds
    payload, is_error = call("log_bet", game_id="nope", side="home", price=-110)
    assert is_error and "Unknown game_id" in payload

    payload, is_error = call("delete_bet", bet_id=bet_id)
    assert not is_error and payload == {"deleted": bet_id}
    accepted = [row["tool"] for row in store.audit_log() if row["outcome"] == "accepted" and row["client"] != "app"]
    assert accepted == ["delete_bet", "log_bet"]  # writes are audited one by one, newest first


def test_reads_are_counted_not_audited(client, token) -> None:
    for _ in range(3):
        _rpc(client, token, "tools/call", {"name": "get_status", "arguments": {}})
    assert [row for row in store.audit_log() if row["tool"] == "get_status"] == []
    assert store.clients()[0]["calls"] == 3 and store.clients()[0]["lastTool"] == "get_status"


def test_a_huge_result_is_cut_with_a_note() -> None:
    text = tools._text({"rows": ["x" * 100] * 2000})
    assert len(text) < tools.MAX_RESULT_CHARS + 200
    assert "ask for less" in text


# ---------------------------------------------------- the renderer's controls


def test_turning_it_on_mints_a_token_and_writes_the_bridge(client) -> None:
    status = client.get("/api/mcp").json()
    assert status["enabled"] is False and status["tokenMasked"] is None and status["bridge"] is False
    assert not store.bridge_path().exists()

    status = client.post("/api/mcp/settings", json={"enabled": True}).json()
    assert status["enabled"] is True and status["bridge"] is True
    assert status["url"] == "http://127.0.0.1:8123/mcp"
    assert status["tokenMasked"].startswith("ks_") and "…" in status["tokenMasked"]
    assert len(status["tools"]) == len(catalog.TOOLS)
    bridge = json.loads(store.bridge_path().read_text(encoding="utf-8"))
    assert bridge["port"] == 8123 and bridge["pid"] == os.getpid()
    assert "token" not in json.dumps(bridge).lower()

    status = client.post("/api/mcp/settings", json={"enabled": False}).json()
    assert status["enabled"] is False and not store.bridge_path().exists()
    # The token survives the switch: a client set up once keeps working when
    # access is turned back on.
    assert status["tokenMasked"] is not None


def test_the_token_is_not_stored_in_the_clear(token) -> None:
    raw = store.token_path().read_bytes()
    if sys.platform == "win32":  # DPAPI is always there; elsewhere it depends on a keychain
        assert token.encode() not in raw
    assert store.load_token(fresh=True) == token


def test_rotating_cuts_off_the_old_token(client, token) -> None:
    assert _rpc(client, token, "ping").status_code == 200
    client.post("/api/mcp/token/rotate")
    fresh = store.load_token()
    assert fresh != token
    assert _rpc(client, token, "ping").status_code == 401
    assert _rpc(client, fresh, "ping").status_code == 200


def test_a_bridge_left_by_another_process_is_not_deleted(token) -> None:
    """During a restart the new backend can be up before the old one finishes
    shutting down; the old one must not delete the new one's bridge."""
    path = store.bridge_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pid"] = os.getpid() + 1
    path.write_text(json.dumps(data), encoding="utf-8")
    store.clear_bridge()
    assert path.exists()


# ------------------------------------------------------------ the stdio relay


@pytest.fixture()
def relay(monkeypatch):
    sent: list = []
    monkeypatch.setattr(stdio, "_send", sent.append)
    monkeypatch.setattr(stdio, "_session_id", None)
    monkeypatch.setattr(stdio, "_init_params", None)
    monkeypatch.setattr(stdio, "_need_init", True)
    return sent


def test_relay_answers_the_handshake_and_lists_tools_with_the_app_closed(relay) -> None:
    stdio._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-desktop"}}})
    stdio._handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    stdio._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [m["id"] for m in relay] == [1, 2]
    assert relay[0]["result"]["serverInfo"]["name"] == "krypt-scout"
    assert len(relay[1]["result"]["tools"]) == len(catalog.TOOLS)


def test_relay_tool_call_with_the_app_closed_says_what_to_do(relay) -> None:
    stdio._handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                   "params": {"name": "get_status", "arguments": {}}})
    result = relay[0]["result"]
    assert result["isError"] is True
    assert "Open the app" in result["content"][0]["text"]


def test_relay_names_a_missing_token_differently_from_a_closed_app(relay, token) -> None:
    store.token_path().unlink()
    store.reset_for_tests()
    stdio._handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                   "params": {"name": "get_status", "arguments": {}}})
    assert "no access token" in relay[0]["result"]["content"][0]["text"]


def test_relay_forwards_to_the_running_app(relay, client, token, monkeypatch) -> None:
    """End to end minus the socket: the relay's POST is served by the app."""
    seen: list[dict] = []

    def fake_post(payload, *, timeout=120.0):
        url, bearer = stdio._endpoint()
        assert url == "http://127.0.0.1:8123/mcp" and bearer == token
        headers = _auth(bearer)
        if stdio._session_id:
            headers["mcp-session-id"] = stdio._session_id
        seen.append(payload)
        response = client.post("/mcp", json=payload, headers=headers)
        assert response.status_code in (200, 202), response.text
        return (response.json() if response.content else None), response.headers.get("mcp-session-id")

    monkeypatch.setattr(stdio, "_post", fake_post)
    stdio._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "Claude Desktop"}}})
    stdio._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "get_status", "arguments": {}}})
    assert [m.get("method") for m in seen] == ["initialize", "notifications/initialized", "tools/call"]
    payload = json.loads(relay[1]["result"]["content"][0]["text"])
    assert payload["app"] == "Krypt Scout"
    assert [c["client"] for c in store.clients()] == ["Claude Desktop"]


def test_default_userdata_is_the_electron_location(monkeypatch) -> None:
    assert stdio.default_userdata().name == "Krypt Scout"


def test_prepare_environment_prefers_the_flag(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KRYPT_SCOUT_USERDATA", "from-env")
    stdio.prepare_environment(["x", "--mcp", "--userdata", str(tmp_path)])
    assert os.environ["KRYPT_SCOUT_USERDATA"] == str(tmp_path)


def test_relay_process_writes_only_protocol_to_stdout(tmp_path) -> None:
    """The real binary path: `run_backend.py --mcp`. stdout is the protocol, so
    one stray print (a library banner, a log line) corrupts the stream for
    every client. Also proves the relay starts without the app running."""
    backend = Path(__file__).resolve().parent.parent
    lines = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "pytest"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_status", "arguments": {}}},
    ]) + "\n"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("KRYPT_SCOUT_USERDATA", None)
    proc = subprocess.run(
        [sys.executable, str(backend / "run_backend.py"), "--mcp", "--userdata", str(tmp_path)],
        input=lines.encode("utf-8"), capture_output=True, timeout=60, cwd=str(backend), env=env,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    replies = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]
    by_id = {r["id"]: r for r in replies}
    assert set(by_id) == {1, 2, 3}
    assert by_id[1]["result"]["serverInfo"]["name"] == "krypt-scout"
    assert len(by_id[2]["result"]["tools"]) == len(catalog.TOOLS)
    assert by_id[3]["result"]["isError"] is True
    # It resolved its paths under --userdata and wrote nothing into the repo.
    assert (tmp_path / "data").is_dir()


def test_tool_handlers_never_run_without_asyncio_surprises() -> None:
    """call() is the one door and must never raise, whatever it is handed."""
    result = asyncio.run(tools.call("get_board", {"sport": 5}, {"client": "t"}))
    assert result["isError"] is True
    result = asyncio.run(tools.call("nope", {}, None))
    assert result["isError"] is True

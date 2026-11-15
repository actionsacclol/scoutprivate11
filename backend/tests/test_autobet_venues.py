"""The Kalshi and Polymarket US trading clients — signing, order bodies, book
reading, collateral moves, game matching, and the account endpoints.

No network and no real key: every request function is replaced, and keys are
generated here. What these pin is the WIRE: the shapes were proven on real
accounts in the sibling bots (Krypt Trader, Krypt PolyBot US), and a silent
drift from them is an order the exchange refuses, or worse, misreads.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from fastapi.testclient import TestClient

from kryptscout.api import main
from kryptscout.autobet import engine
from kryptscout.autobet import kalshi_trade as kalshi
from kryptscout.autobet import pmus_trade as pmus
from kryptscout.autobet import venues as venues_mod
from kryptscout.core.schemas import Game, Sport
from kryptscout.store import db

NOW = datetime.now(timezone.utc)
KEY_ID = "1a2b3c4d-0000-4000-8000-000000000000"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _scratch_keys(tmp_path, monkeypatch):
    """Keys, settings and the database live in a scratch folder; nothing here
    reads or writes the real store."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.setattr(engine, "SETTINGS_FILE", tmp_path / "autobet.json")
    monkeypatch.setattr(engine, "PROFILES_FILE", tmp_path / "autobet_profiles.json")
    monkeypatch.setattr(kalshi, "_key_file", lambda: tmp_path / "kalshi.key")
    monkeypatch.setattr(pmus, "_key_file", lambda: tmp_path / "polymarket_us.key")
    kalshi.reset_cache()
    pmus.reset_cache()
    kalshi._market_cache.clear()
    kalshi._series_cache.clear()
    pmus._search_cache.clear()
    pmus._market_cache.clear()
    yield
    kalshi.reset_cache()
    pmus.reset_cache()


def _ed_pem() -> tuple[ed25519.Ed25519PrivateKey, str]:
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return key, pem


# ------------------------------------------------------------------- kalshi


def test_kalshi_signs_timestamp_method_path_with_ed25519():
    key, pem = _ed_pem()
    kalshi.save_credentials("key-123", pem)
    headers = kalshi.sign_headers("post", "/trade-api/v2/portfolio/events/orders",
                                  now_ms=1700000000123)
    assert headers["KALSHI-ACCESS-KEY"] == "key-123"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000123"
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000123POST/trade-api/v2/portfolio/events/orders",
    )
    assert kalshi.credentials_status() == {
        "connected": True, "keyPreview": "key-123", "keyType": "Ed25519"}
    assert kalshi.verify_signer() == "ed25519 ok"


def test_kalshi_signs_with_rsa_pss_and_survives_a_flattened_paste():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    # A key pasted into a one-line field loses its line breaks.
    kalshi.save_credentials("rsa-key", pem.replace("\n", " "))
    headers = kalshi.sign_headers("GET", "/trade-api/v2/portfolio/balance", now_ms=42)
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"42GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )
    assert kalshi.credentials_status()["keyType"] == "RSA"


def test_kalshi_rejects_what_is_not_a_key():
    with pytest.raises(ValueError):
        kalshi.save_credentials("key", "definitely not a pem")
    with pytest.raises(ValueError):
        kalshi.save_credentials("", _ed_pem()[1])
    assert kalshi.credentials_present() is False
    kalshi.save_credentials("key", _ed_pem()[1])
    kalshi.clear_credentials()
    assert kalshi.credentials_present() is False


def test_kalshi_yes_asks_are_the_mirror_of_no_bids():
    book = {"orderbook_fp": {
        "yes_dollars": [["0.5800", "40.00"]],
        "no_dollars": [["0.3900", "100.00"], ["0.4000", "25.00"], ["0.3500", "500.00"]],
    }}
    # A NO bid at 40c is YES offered at 60c: the best ask comes first.
    assert kalshi.yes_asks(book) == [(0.6, 25.0), (0.61, 100.0), (0.65, 500.0)]
    legacy = {"orderbook": {"yes": [[58, 40]], "no": [[39, 100], [40, 25]]}}
    assert kalshi.yes_asks(legacy) == [(0.6, 25.0), (0.61, 100.0)]
    assert kalshi.yes_asks({}) == [] and kalshi.yes_asks(None) == []


def test_kalshi_ask_depth_counts_only_what_the_cap_can_take(monkeypatch):
    async def book(path, params=None):
        assert path == "/markets/KXNBAGAME-X-BOS/orderbook"
        return {"orderbook_fp": {"no_dollars": [["0.4000", "25.00"], ["0.3900", "100.00"],
                                                ["0.3500", "500.00"]]}}

    monkeypatch.setattr(kalshi, "_public_get", book)
    depth = run(kalshi.ask_depth("KXNBAGAME-X-BOS", 61))
    assert depth == {"best_ask_cents": 60, "depth_shares": 125.0,
                     "levels": [[0.6, 25.0], [0.61, 100.0]]}
    assert run(kalshi.ask_depth("KXNBAGAME-X-BOS", 59))["depth_shares"] == 0

    async def broken(path, params=None):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(kalshi, "_public_get", broken)
    assert run(kalshi.ask_depth("KXNBAGAME-X-BOS", 61)) is None


def test_kalshi_fee_rate_comes_from_the_series_and_rounds_up(monkeypatch):
    async def series(path, params=None):
        assert path == "/series/KXNBAGAME"
        return {"series": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}}

    monkeypatch.setattr(kalshi, "_public_get", series)
    assert run(kalshi.fee_rate("KXNBAGAME-26NOV18PHXSAC-SAC")) == pytest.approx(0.07)
    # 10 contracts at 50c: 10 x 0.07 x 0.25 = 17.5c, charged as 18c.
    assert kalshi.fee_usd(10, 0.5) == pytest.approx(0.18)
    assert kalshi.fee_usd(0, 0.5) == 0.0

    async def odd(path, params=None):
        return {"series": {"fee_type": "flat", "fee_multiplier": 1}}

    kalshi._series_cache.clear()
    monkeypatch.setattr(kalshi, "_public_get", odd)
    # A fee shape this formula does not describe is "unknown", never "free".
    assert run(kalshi.fee_rate("KXODD-1")) is None


def test_kalshi_buy_is_an_immediate_capped_yes_bid(monkeypatch):
    sent = []

    async def signed(method, path, *, json_body=None, params=None, retry=True, timeout=None):
        sent.append((method, path, json_body))
        return {"order": {"order_id": "ord-1", "status": "executed",
                          "fill_count_fp": "30.00", "taker_fill_cost_dollars": "12.6000"}}

    monkeypatch.setattr(kalshi, "_signed", signed)
    result = run(kalshi.buy_yes_ioc("KXNBAGAME-X-BOS", 47, 42))
    assert result["order_id"] == "ord-1" and result["filled"] == 30.0
    assert result["avg_prob"] == pytest.approx(0.42)
    ((method, path, body),) = sent
    assert (method, path) == ("POST", "/portfolio/events/orders")
    assert body["ticker"] == "KXNBAGAME-X-BOS" and body["side"] == "bid"
    assert body["count"] == "47.00" and body["price"] == "0.4200"
    assert body["time_in_force"] == "immediate_or_cancel"  # nothing ever rests
    assert body["exchange_index"] == -1 and len(body["client_order_id"]) == 36
    with pytest.raises(ValueError):
        run(kalshi.buy_yes_ioc("KXNBAGAME-X-BOS", 0, 42))
    with pytest.raises(ValueError):
        run(kalshi.buy_yes_ioc("KXNBAGAME-X-BOS", 5, 100))


def test_kalshi_lost_reply_is_unknown_unless_the_order_is_found(monkeypatch):
    calls = []

    async def signed(method, path, *, json_body=None, params=None, retry=True, timeout=None):
        calls.append((method, path))
        if method == "POST":
            raise httpx.ReadTimeout("no reply")
        return {"orders": []}  # the lookup works and shows no such order...

    monkeypatch.setattr(kalshi, "_signed", signed)
    # ...which still is not proof: the answer is "unknown", never "failed".
    with pytest.raises(kalshi.OrderOutcomeUnknown):
        run(kalshi.buy_yes_ioc("KXNBAGAME-X-BOS", 10, 42))

    async def signed_found(method, path, *, json_body=None, params=None, retry=True, timeout=None):
        if method == "POST":
            signed_found.cid = json_body["client_order_id"]
            raise httpx.ReadTimeout("no reply")
        return {"orders": [{"client_order_id": signed_found.cid, "order_id": "ord-9",
                            "status": "executed", "fill_count_fp": "10.00",
                            "taker_fill_cost_dollars": "4.1000"}]}

    monkeypatch.setattr(kalshi, "_signed", signed_found)
    found = run(kalshi.buy_yes_ioc("KXNBAGAME-X-BOS", 10, 42))
    assert found["order_id"] == "ord-9" and found["filled"] == 10.0
    assert found["avg_prob"] == pytest.approx(0.41)


def test_kalshi_reply_without_fill_fields_is_checked_not_assumed(monkeypatch):
    async def signed(method, path, *, json_body=None, params=None, retry=True, timeout=None):
        if method == "POST":
            return {"order": {"order_id": "ord-2", "status": "executed"}}
        assert path == "/portfolio/fills" and params["order_id"] == "ord-2"
        return {"fills": [{"count_fp": "6.00", "yes_price_dollars": "0.4000"},
                          {"count_fp": "4.00", "yes_price_dollars": "0.4200"}]}

    monkeypatch.setattr(kalshi, "_signed", signed)
    result = run(kalshi.buy_yes_ioc("KXNBAGAME-X-BOS", 10, 42))
    assert result["filled"] == 10.0 and result["avg_prob"] == pytest.approx(0.408)

    async def silent(method, path, *, json_body=None, params=None, retry=True, timeout=None):
        if method == "POST":
            return {"order": {"order_id": "ord-3", "status": "executed"}}
        return {"fills": []}

    monkeypatch.setattr(kalshi, "_signed", silent)
    # Accepted, fill not visible anywhere yet: unknown (None), not zero.
    assert run(kalshi.buy_yes_ioc("KXNBAGAME-X-BOS", 10, 42))["filled"] is None


def test_kalshi_order_fill_tells_ended_unfilled_from_not_yet_known(monkeypatch):
    state = {"status": "canceled"}

    async def signed(method, path, *, json_body=None, params=None, retry=True, timeout=None):
        if path == "/portfolio/fills":
            return {"fills": []}
        return {"order": {"order_id": "ord-4", "status": state["status"], "fill_count_fp": "0.00"}}

    monkeypatch.setattr(kalshi, "_signed", signed)
    assert run(kalshi.order_fill("ord-4")) == (0.0, 0.0)
    state["status"] = "resting"
    assert run(kalshi.order_fill("ord-4")) is None


def _collateral(monkeypatch, shards: dict[int, float], shard: int = 3):
    moves = []

    async def market(ticker):
        return {"ticker": ticker, "exchange_index": shard}

    async def balance():
        return {"usd": round(sum(shards.values()), 2), "shards": dict(shards)}

    async def transfer(amount_usd, source, destination):
        moves.append((amount_usd, source, destination))
        shards[source] = round(shards[source] - amount_usd, 2)
        shards[destination] = round(shards.get(destination, 0.0) + amount_usd, 2)
        return {}

    monkeypatch.setattr(kalshi, "market", market)
    monkeypatch.setattr(kalshi, "get_balance", balance)
    monkeypatch.setattr(kalshi, "transfer_between_shards", transfer)
    monkeypatch.setattr(kalshi, "LAND_POLL_S", 0.0)
    return moves


def test_kalshi_moves_only_the_shortfall_between_the_users_own_shards(monkeypatch):
    # 50 contracts at 42c plus a 2c fee allowance needs $22.00 on shard 3.
    moves = _collateral(monkeypatch, {0: 100.0, 3: 5.0})
    note = run(kalshi.ensure_collateral("KXNBAGAME-X-BOS", 50, 42))
    assert moves == [(17.0, 0, 3)]
    assert note == "moved $17.00 to Kalshi exchange 3 for this order"


def test_kalshi_moves_nothing_when_funded_short_or_switched_off(monkeypatch):
    moves = _collateral(monkeypatch, {0: 100.0, 3: 50.0})
    assert run(kalshi.ensure_collateral("KXNBAGAME-X-BOS", 50, 42)) is None

    moves = _collateral(monkeypatch, {0: 3.0, 3: 5.0})  # the whole account is short
    note = run(kalshi.ensure_collateral("KXNBAGAME-X-BOS", 50, 42))
    assert moves == [] and "needs $22.00" in note and "holds $5.00" in note

    moves = _collateral(monkeypatch, {0: 100.0, 3: 5.0})
    note = run(kalshi.ensure_collateral("KXNBAGAME-X-BOS", 50, 42, allow_move=False))
    assert moves == [] and "is off" in note


def test_kalshi_collateral_check_does_nothing_it_cannot_read(monkeypatch):
    moves = _collateral(monkeypatch, {})  # a key with no per-shard view

    assert run(kalshi.ensure_collateral("KXNBAGAME-X-BOS", 50, 42)) is None

    async def no_market(ticker):
        return None

    monkeypatch.setattr(kalshi, "market", no_market)
    assert run(kalshi.ensure_collateral("KXNBAGAME-X-BOS", 50, 42)) is None
    assert moves == []


def test_kalshi_transfer_is_whole_cents_down_in_centicents_and_never_retried(monkeypatch):
    sent = []

    async def signed(method, path, *, json_body=None, params=None, retry=True, timeout=None):
        sent.append((path, json_body, retry))
        return {}

    monkeypatch.setattr(kalshi, "_signed", signed)
    run(kalshi.transfer_between_shards(2.019, 0, 3))
    ((path, body, retry),) = sent
    assert path == "/portfolio/intra_exchange_instance_transfer" and retry is False
    assert body == {"source": "event_contract", "destination": "event_contract",
                    "source_exchange_shard": 0, "destination_exchange_shard": 3,
                    "amount": 20100}  # $2.01, in hundredths of a cent
    with pytest.raises(ValueError):
        run(kalshi.transfer_between_shards(0.004, 0, 3))
    with pytest.raises(ValueError):
        run(kalshi.transfer_between_shards(5, 3, 3))


def test_kalshi_balance_sums_the_shards(monkeypatch):
    async def signed(method, path, **kwargs):
        return {"balance": 500, "balance_breakdown": [
            {"exchange_index": 0, "balance": "5.0000"},
            {"exchange_index": 3, "balance": "12.5000"}]}

    monkeypatch.setattr(kalshi, "_signed", signed)
    assert run(kalshi.get_balance()) == {"usd": 17.5, "shards": {0: 5.0, 3: 12.5}}


# ------------------------------------------------------------ polymarket us


def _secret() -> tuple[ed25519.Ed25519PrivateKey, str]:
    seed = bytes(range(32))
    return ed25519.Ed25519PrivateKey.from_private_bytes(seed), base64.b64encode(seed).decode()


def test_pmus_signs_timestamp_method_path_without_query():
    key, secret = _secret()
    pmus.save_credentials(KEY_ID, secret)
    headers = pmus.auth_headers("get", "/v1/orders?limit=5", now_ms=1700000000000)
    assert headers["X-PM-Access-Key"] == KEY_ID
    key.public_key().verify(base64.b64decode(headers["X-PM-Signature"]),
                            b"1700000000000GET/v1/orders")
    assert pmus.credentials_status() == {"connected": True, "keyPreview": "1a2b3c4d…0000"}
    assert pmus.verify_signer() == "ed25519 ok"


def test_pmus_accepts_a_64_byte_keypair_and_rejects_junk():
    seed = bytes(range(32))
    pmus.save_credentials(KEY_ID, base64.b64encode(seed + bytes(32)).decode())
    assert pmus.sign_message(base64.b64encode(seed + bytes(32)).decode(), "x") == \
        pmus.sign_message(base64.b64encode(seed).decode(), "x")
    for key_id, secret in (("nope", _secret()[1]), (KEY_ID, "!!!"),
                           (KEY_ID, base64.b64encode(b"short").decode())):
        with pytest.raises(ValueError):
            pmus.save_credentials(key_id, secret)
    pmus.clear_credentials()
    assert pmus.credentials_present() is False


def _game(start: datetime) -> Game:
    return Game(id="nba-401", sport=Sport.NBA, league="", start_time=start,
                home="San Antonio Spurs", away="Los Angeles Lakers")


def _event(start: datetime, *, slug: str = "aec-nba-lal-sa-2026-11-27",
           league: str = "nba", bid: str = "0.4100", ask: str = "0.4300") -> dict:
    stamp = start.isoformat().replace("+00:00", "Z")
    return {
        "slug": slug.replace("aec-", ""), "startTime": stamp,
        "markets": [
            {"slug": slug.replace("aec-", "asc-"), "status": "MARKET_STATUS_OPEN",
             "sportsMarketTypeV2": "SPORTS_MARKET_TYPE_SPREAD", "marketSides": []},
            {"slug": slug, "status": "MARKET_STATUS_OPEN", "gameStartTime": stamp,
             "sportsMarketTypeV2": "SPORTS_MARKET_TYPE_MONEYLINE",
             "feeCoefficient": 0.095, "orderPriceMinTickSize": 0.01, "minimumTradeQty": 0.01,
             "bestBidQuote": {"value": bid, "currency": "USD"},
             "bestAskQuote": {"value": ask, "currency": "USD"},
             "marketSides": [
                 {"long": True, "team": {"name": "Los Angeles Lakers", "league": league}},
                 {"long": False, "team": {"name": "San Antonio Spurs", "league": league}},
             ]},
        ],
    }


def test_pmus_matches_one_game_and_reads_each_sides_ask(monkeypatch):
    start = NOW + timedelta(hours=5)
    game = _game(start)
    matched = pmus.match_market(game, [_event(start)], "nba")
    market, sides = matched
    # The Lakers (away) are the long side: away = YES, home = NO.
    assert sides == {"away": "yes", "home": "no"}
    assert pmus.side_ask(market, "yes") == pytest.approx(0.43)
    assert pmus.side_ask(market, "no") == pytest.approx(0.59)  # 1 − the YES bid

    asked: list[str] = []

    async def search(query):
        asked.append(query)
        return [_event(start)]

    monkeypatch.setattr(pmus, "_search", search)
    found = run(pmus.resolve(Sport.NBA, game, "home"))
    assert asked == ["Spurs Lakers"]  # both nicknames first, and it stops at a match
    assert found == {"slug": "aec-nba-lal-sa-2026-11-27", "clob_side": "no",
                     "prob": pytest.approx(0.59)}
    # The row discovery saw answers the fee question with no second request.
    meta = run(pmus.market_meta("aec-nba-lal-sa-2026-11-27"))
    assert meta["fee_schedule"]["rate"] == pytest.approx(0.095)
    assert meta["tick_size"] == 0.01 and meta["min_size"] == 0.01
    assert run(pmus.resolve(Sport.TENNIS, game, "home")) is None
    assert run(pmus.resolve(Sport.NBA, game, "draw")) is None


def test_pmus_searches_by_nicknames_then_falls_back_to_full_names(monkeypatch):
    """The search titles games "BKN Nets vs. MEM Grizzlies", so a full home
    name ("Memphis Grizzlies") returns Memphis college basketball instead. On
    2026-11-14 it found 0 of 5 NBA and 0 of 10 NHL games; both nicknames found
    all fifteen."""
    start = NOW + timedelta(hours=5)
    game = _game(start)
    assert pmus.search_queries(Sport.NBA, game) == [
        "Spurs Lakers", "San Antonio Spurs", "Los Angeles Lakers"]
    # College games are titled by school: ask for the two schools and the sport.
    college = Game(id="cfb-1", sport=Sport.NCAAF, league="", start_time=start,
                   home="Georgia Bulldogs", away="Missouri Tigers")
    assert pmus.search_queries(Sport.NCAAF, college)[0] == "Missouri Georgia football"
    hoops = Game(id="cbb-1", sport=Sport.NCAAB, league="", start_time=start,
                 home="Gonzaga Bulldogs", away="LSU Tigers")
    assert pmus.search_queries(Sport.NCAAB, hoops) == [
        "LSU Gonzaga basketball", "Gonzaga Bulldogs", "LSU Tigers"]

    # A miss on the first query moves on; when nothing matches, every query
    # was tried once and the answer is None.
    asked: list[str] = []

    async def only_full_home(query):
        asked.append(query)
        return [_event(start)] if query == "San Antonio Spurs" else []

    monkeypatch.setattr(pmus, "_search", only_full_home)
    assert run(pmus.resolve(Sport.NBA, game, "away"))["clob_side"] == "yes"
    assert asked == ["Spurs Lakers", "San Antonio Spurs"]

    asked.clear()

    async def nothing(query):
        asked.append(query)
        return []

    monkeypatch.setattr(pmus, "_search", nothing)
    assert run(pmus.resolve(Sport.NBA, game, "home")) is None
    assert asked == ["Spurs Lakers", "San Antonio Spurs", "Los Angeles Lakers"]


def test_pmus_refuses_an_ambiguous_or_wrong_match():
    start = NOW + timedelta(hours=5)
    game = _game(start)
    # Another league's teams with the same names, tomorrow's rematch, and two
    # candidates at once: each is "not this game".
    assert pmus.match_market(game, [_event(start, league="nbasl")], "nba") is None
    assert pmus.match_market(game, [_event(start + timedelta(days=1))], "nba") is None
    twins = [_event(start), _event(start, slug="aec-nba-lal-sa-2026-11-28")]
    assert pmus.match_market(game, twins, "nba") is None
    closed = _event(start)
    closed["markets"][1]["status"] = "MARKET_STATUS_CLOSED"
    assert pmus.match_market(game, [closed], "nba") is None


def test_pmus_ask_depth_mirrors_the_book_for_the_no_side(monkeypatch):
    async def get(url, params=None, *, timeout=None):
        assert url.endswith("/v1/markets/aec-nba-lal-sa-2026-11-27/book")
        return {"marketData": {
            "bids": [{"px": {"value": "0.4100"}, "qty": "50"}, {"px": {"value": "0.4000"}, "qty": "200"}],
            "offers": [{"px": {"value": "0.4300"}, "qty": "30"}, {"px": {"value": "0.4500"}, "qty": "70"}],
        }}

    monkeypatch.setattr(pmus, "_get", get)
    slug = "aec-nba-lal-sa-2026-11-27"
    assert run(pmus.ask_depth(slug, "yes", 44)) == {
        "best_ask_cents": 43, "depth_shares": 30.0, "levels": [[0.43, 30.0]]}
    # Buying NO lifts YES bids: a 41c bid is NO offered at 59c.
    assert run(pmus.ask_depth(slug, "no", 60)) == {
        "best_ask_cents": 59, "depth_shares": 250.0, "levels": [[0.59, 50.0], [0.6, 200.0]]}

    async def nothing(url, params=None, *, timeout=None):
        return None

    monkeypatch.setattr(pmus, "_get", nothing)
    assert run(pmus.ask_depth(slug, "yes", 44)) is None
    assert run(pmus.ask_depth("not a slug!", "yes", 44)) is None


def _filled(order_id: str, qty: str, avg: str, intent: str, state: str = "ORDER_STATE_FILLED") -> dict:
    return {"id": order_id, "executions": [{"type": "EXECUTION_TYPE_FILL", "order": {
        "id": order_id, "state": state, "intent": intent, "cumQuantity": qty,
        "avgPx": {"value": avg, "currency": "USD"}, "quantity": qty,
        "price": {"value": avg, "currency": "USD"}}}]}


def _prime(start: datetime) -> str:
    slug = "aec-nba-lal-sa-2026-11-27"
    pmus._market_cache[slug] = _event(start)["markets"][1]
    return slug


def test_pmus_buying_the_no_side_sends_a_short_at_the_mirrored_price(monkeypatch):
    slug = _prime(NOW + timedelta(hours=5))
    sent = []

    async def authed(method, path, *, json_body=None, params=None, idempotent=True):
        sent.append((method, path, json_body, idempotent))
        return _filled("ord-1", "20", "0.4100", "ORDER_INTENT_BUY_SHORT")

    monkeypatch.setattr(pmus, "_authed", authed)
    result = run(pmus.buy_ioc(slug, "no", 20, 59))
    ((method, path, body, idempotent),) = sent
    assert (method, path) == ("POST", "/v1/orders")
    assert idempotent is False  # no idempotency key on this exchange: sent once
    assert body["marketSlug"] == slug and body["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert body["price"] == {"value": "0.41", "currency": "USD"} or \
        float(body["price"]["value"]) == pytest.approx(0.41)  # 59c NO = 41c YES
    assert body["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert body["type"] == "ORDER_TYPE_LIMIT" and body["quantity"] == 20
    assert body["manualOrderIndicator"] == "MANUAL_ORDER_INDICATOR_AUTOMATIC"
    # Filled 20 at a YES price of 41c = 59c on OUR side.
    assert result == {"order_id": "ord-1", "filled": 20.0, "avg_prob": pytest.approx(0.59)}


def test_pmus_no_trade_refusal_and_unknown_are_three_different_things(monkeypatch):
    slug = _prime(NOW + timedelta(hours=5))

    async def killed(method, path, **kwargs):
        return {"id": "ord-2", "executions": [{"type": "EXECUTION_TYPE_CANCELED", "order": {
            "id": "ord-2", "state": "ORDER_STATE_CANCELED", "intent": "ORDER_INTENT_BUY_LONG",
            "cumQuantity": "0", "quantity": "20", "price": {"value": "0.43"}}}]}

    monkeypatch.setattr(pmus, "_authed", killed)
    assert run(pmus.buy_ioc(slug, "yes", 20, 43)) == {
        "order_id": "", "filled": 0.0, "avg_prob": None}  # clean no-trade

    async def rejected(method, path, **kwargs):
        return {"id": "ord-3", "executions": [{"type": "EXECUTION_TYPE_REJECTED",
                                                "text": "insufficient buying power", "order": {}}]}

    monkeypatch.setattr(pmus, "_authed", rejected)
    with pytest.raises(pmus.PolymarketUSError) as refusal:
        run(pmus.buy_ioc(slug, "yes", 20, 43))
    assert "insufficient buying power" in pmus.error_text(refusal.value)

    async def dropped(method, path, **kwargs):
        raise httpx.ReadTimeout("no reply")

    monkeypatch.setattr(pmus, "_authed", dropped)
    with pytest.raises(pmus.OrderOutcomeUnknown):
        run(pmus.buy_ioc(slug, "yes", 20, 43))

    async def five_hundred(method, path, **kwargs):
        raise pmus.PolymarketUSError(502, "bad gateway")

    monkeypatch.setattr(pmus, "_authed", five_hundred)
    with pytest.raises(pmus.OrderOutcomeUnknown):
        run(pmus.buy_ioc(slug, "yes", 20, 43))

    with pytest.raises(pmus.PolymarketUSError):
        run(pmus.buy_ioc("not a slug!", "yes", 20, 43))


def test_pmus_accepted_without_an_answer_is_asked_again_not_assumed(monkeypatch):
    slug = _prime(NOW + timedelta(hours=5))
    monkeypatch.setattr(pmus, "FINAL_POLL_S", 0.0)
    order = {"state": "ORDER_STATE_NEW"}

    async def authed(method, path, **kwargs):
        if method == "POST":
            return {"id": "ord-5", "executions": []}  # accepted, engine silent
        assert path == "/v1/order/ord-5"
        return {"order": {"id": "ord-5", "intent": "ORDER_INTENT_BUY_LONG", **order}}

    monkeypatch.setattr(pmus, "_authed", authed)
    # Still working after every poll: placed, fill unknown (None, not zero).
    assert run(pmus.buy_ioc(slug, "yes", 20, 43)) == {
        "order_id": "ord-5", "filled": None, "avg_prob": None}

    order.update(state="ORDER_STATE_CANCELED", cumQuantity="0")
    assert run(pmus.buy_ioc(slug, "yes", 20, 43))["order_id"] == ""  # killed unfilled
    assert run(pmus.fills_for_order("ord-5")) == (0.0, 0.0)

    order.update(state="ORDER_STATE_FILLED", cumQuantity="12", avgPx={"value": "0.4250"})
    assert run(pmus.buy_ioc(slug, "yes", 20, 43)) == {
        "order_id": "ord-5", "filled": 12.0, "avg_prob": pytest.approx(0.425)}
    assert run(pmus.fills_for_order("ord-5")) == (12.0, 0.425)


def test_pmus_price_snaps_down_to_the_tick_and_balance_never_reads_as_zero(monkeypatch):
    assert pmus._snap(0.437, 0.01) == pytest.approx(0.43)
    assert pmus._snap(0.4374, 0.0025) == pytest.approx(0.435)
    assert pmus._snap(0.999, 0.01) == pytest.approx(0.99)

    async def balances(method, path, **kwargs):
        return {"balances": [{"currency": "USD", "buyingPower": "41.25", "currentBalance": "60"}]}

    monkeypatch.setattr(pmus, "_authed", balances)
    assert run(pmus.get_balance_usd()) == 41.25  # what open orders have not reserved

    async def empty(method, path, **kwargs):
        return {}

    monkeypatch.setattr(pmus, "_authed", empty)
    with pytest.raises(RuntimeError):
        run(pmus.get_balance_usd())


# ---------------------------------------------------------------- venue layer


def test_venue_registry_and_status_are_local(monkeypatch):
    assert venues_mod.VENUE_KEYS == ("polymarket", "kalshi", "polymarket_us")
    monkeypatch.setattr(venues_mod.pm_auth, "credentials_status",
                        lambda: {"hasWalletKey": False})
    kalshi.save_credentials("key-123", _ed_pem()[1])
    rows = {row["key"]: row for row in venues_mod.status()}
    assert rows["kalshi"]["connected"] is True and rows["kalshi"]["keyPreview"] == "key-123"
    assert rows["polymarket_us"]["connected"] is False
    assert rows["polymarket"]["accountNoun"] == "wallet"
    assert rows["polymarket_us"]["onBoard"] is False and rows["kalshi"]["onBoard"] is True
    # No secret ever leaves through the status.
    assert "PRIVATE KEY" not in str(rows)


def test_kalshi_venue_funds_then_buys_and_reports_a_clean_no_trade(monkeypatch):
    order = []

    async def collateral(ticker, shares, price_cents, *, allow_move=True):
        order.append(("collateral", allow_move))
        return "moved $5.00 to Kalshi exchange 3 for this order"

    async def buy(ticker, shares, price_cents):
        order.append(("buy", ticker, shares, price_cents))
        return {"order_id": "ord-1", "filled": 0.0, "avg_prob": None,
                "status": "canceled", "fill_known": True}

    monkeypatch.setattr(kalshi, "ensure_collateral", collateral)
    monkeypatch.setattr(kalshi, "buy_yes_ioc", buy)
    venue = venues_mod.VENUES["kalshi"]
    monkeypatch.setattr(venue, "auto_move_funds", False)
    market = venues_mod.VenueMarket("kalshi", "KXNBAGAME-X-BOS", "yes", 0.42)
    result = run(venue.buy(market, 10, 42))
    assert order == [("collateral", False), ("buy", "KXNBAGAME-X-BOS", 10, 42)]
    # Nothing filled: no order id, so the engine files it as unmatched.
    assert result["order_id"] == "" and result["note"].startswith("moved $5.00")

    async def lost(ticker, shares, price_cents):
        raise kalshi.OrderOutcomeUnknown("no answer")

    monkeypatch.setattr(kalshi, "buy_yes_ioc", lost)
    with pytest.raises(venues_mod.OrderOutcomeUnknown):
        run(venue.buy(market, 10, 42))


# ------------------------------------------------------------------ endpoints


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main.app.router, "on_startup", [])
    with TestClient(main.app) as test_client:
        yield test_client


def test_connect_test_and_disconnect_an_exchange_key(client, monkeypatch):
    monkeypatch.setattr(venues_mod.pm_auth, "credentials_status",
                        lambda: {"hasWalletKey": False})
    assert client.post("/api/autobet/venues/kalshi/test").status_code == 404  # no key yet
    bad = client.post("/api/autobet/venues/kalshi/credentials",
                      json={"key_id": "k", "secret": "not a pem"})
    assert bad.status_code == 422 and "private key" in bad.json()["detail"].lower()
    assert client.post("/api/autobet/venues/polymarket/credentials",
                       json={"key_id": "k", "secret": "s"}).status_code == 404  # wallet venue

    async def check(self):
        return {"ok": True, "balance_usd": 17.5, "shards": {"0": 5.0, "3": 12.5}}

    monkeypatch.setattr(venues_mod.KalshiVenue, "check", check)
    pem = _ed_pem()[1]
    saved = client.post("/api/autobet/venues/kalshi/credentials",
                        json={"key_id": "key-123", "secret": pem})
    assert saved.status_code == 200
    body = saved.json()
    assert body["check"] == {"ok": True, "balance_usd": 17.5, "shards": {"0": 5.0, "3": 12.5}}
    row = next(v for v in body["venues"] if v["key"] == "kalshi")
    assert row["connected"] is True and row["keyPreview"] == "key-123"
    assert pem.split("\n")[1] not in saved.text  # the key never comes back
    assert client.post("/api/autobet/venues/kalshi/test").json()["balance_usd"] == 17.5
    status = client.get("/api/autobet").json()
    assert next(v for v in status["venues"] if v["key"] == "kalshi")["connected"] is True

    cleared = client.delete("/api/autobet/venues/kalshi/credentials").json()
    assert cleared["cleared"] is True
    assert next(v for v in cleared["venues"] if v["key"] == "kalshi")["connected"] is False


def test_polymarket_us_key_endpoint_and_a_failed_probe_keeps_the_key(client, monkeypatch):
    monkeypatch.setattr(venues_mod.pm_auth, "credentials_status",
                        lambda: {"hasWalletKey": False})

    async def check(self):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(venues_mod.PolymarketUSVenue, "check", check)
    resp = client.post("/api/autobet/venues/polymarket_us/credentials",
                       json={"key_id": KEY_ID, "secret": _secret()[1]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["check"]["ok"] is False and "ConnectError" in body["check"]["error"]
    assert next(v for v in body["venues"] if v["key"] == "polymarket_us")["connected"] is True
    assert _secret()[1] not in resp.text


def test_settings_endpoint_carries_the_venue_knobs(client):
    saved = client.post("/api/autobet/settings",
                        json={"venues": ["kalshi"], "kalshi_auto_move": False}).json()
    assert saved["venues"] == ["kalshi"] and saved["kalshi_auto_move"] is False

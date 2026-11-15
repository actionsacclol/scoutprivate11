"""odds/compare — the daily card: which picks are sent, and each one's price
and page on Polymarket, Kalshi and Polymarket US.

No network: the board is a seeded card and every exchange lookup is replaced.
What these pin is which price each exchange contributes, that the fee lands in
`cost`, that a venue with nothing to say is left out rather than guessed at,
and that a slate of a hundred favourites becomes a short card.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from kryptscout.api import main
from kryptscout.autobet import venues as venues_mod
from kryptscout.core.schemas import Game, GameCard, MoneylineOdds, Sport
from kryptscout.odds import compare

NOW = datetime.now(timezone.utc)
KICKOFF = NOW + timedelta(hours=5)


def run(coro):
    return asyncio.run(coro)


def _card(odds: list[MoneylineOdds], game_id: str = "nba-1") -> GameCard:
    game = Game(id=game_id, sport=Sport.NBA, league="", start_time=KICKOFF,
                home="Boston Celtics", away="Miami Heat")
    return GameCard(game=game, prediction=None, odds=odds)


def _quote(book: str, home_ml: int, away_ml: int, at: datetime = NOW) -> MoneylineOdds:
    return MoneylineOdds(book=book, fetched_at=at, home_ml=home_ml, away_ml=away_ml)


def _pick(side: str = "home", game_id: str = "nba-1") -> dict:
    return {
        "game_id": game_id, "sport": "nba", "side": side, "league": "",
        "home": "Boston Celtics", "away": "Miami Heat", "kickoff": KICKOFF.isoformat(),
    }


@pytest.fixture()
def board(monkeypatch):
    """A board the test fills, and a Polymarket US that lists nothing unless
    the test says otherwise."""
    cards: list[GameCard] = []

    async def _cards(sports):
        return {c.game.id: c for c in cards if c.game.sport in sports}

    async def _nothing(sport, game, side):
        return None

    monkeypatch.setattr(compare, "_cards", _cards)
    monkeypatch.setattr(venues_mod.pmus_trade, "resolve", _nothing)
    return cards


# ------------------------------------------------------------------- pricing


def test_all_three_exchanges_are_priced_for_the_picked_side(board, monkeypatch):
    board.append(_card([_quote("polymarket", -150, 140), _quote("kalshi", -163, 150)]))

    async def _pmus(sport, game, side):
        return {"slug": "nba-bos-mia", "clob_side": "yes", "prob": 0.58 if side == "home" else 0.43}

    monkeypatch.setattr(venues_mod.pmus_trade, "resolve", _pmus)

    prices = run(compare.exchange_prices([_pick("home")]))
    quotes = prices[compare.key("nba-1", "home")]
    assert set(quotes) == {"polymarket", "kalshi", "polymarket_us"}
    assert quotes["polymarket"]["price"] == pytest.approx(0.6, abs=1e-4)  # -150
    assert quotes["kalshi"]["price"] == pytest.approx(0.6198, abs=1e-4)  # -163
    assert quotes["polymarket_us"]["price"] == pytest.approx(0.58)

    away = run(compare.exchange_prices([_pick("away")]))[compare.key("nba-1", "away")]
    assert away["polymarket"]["price"] == pytest.approx(0.4167, abs=1e-4)  # +140
    assert away["polymarket_us"]["price"] == pytest.approx(0.43)


def test_cost_is_price_plus_that_exchanges_taker_fee(board):
    board.append(_card([_quote("polymarket", -150, 140), _quote("kalshi", -150, 140)]))
    quotes = run(compare.exchange_prices([_pick("home")]))[compare.key("nba-1", "home")]
    for venue_key, quote in quotes.items():
        rate = venues_mod.VENUES[venue_key].fallback_fee_rate
        assert quote["cost"] == pytest.approx(0.6 + rate * 0.6 * 0.4, abs=1e-4)
    # Same screen price, different fee: the all-in cost is what separates them.
    assert quotes["polymarket"]["cost"] < quotes["kalshi"]["cost"]
    assert compare.cheapest(quotes) == "polymarket"


def test_a_venue_with_no_quote_is_left_out(board):
    board.append(_card([_quote("kalshi", -150, 140), _quote("draftkings", -160, 135)]))
    quotes = run(compare.exchange_prices([_pick("home")]))[compare.key("nba-1", "home")]
    assert set(quotes) == {"kalshi"}  # no Polymarket quote, no PM US market, no sportsbook


def test_an_in_play_quote_is_not_a_price(board):
    # Fetched after kickoff: the game is under way and the price is the score.
    late = KICKOFF + timedelta(minutes=30)
    board.append(_card([_quote("polymarket", -900, 600, at=late)]))
    assert run(compare.exchange_prices([_pick("home")])) == {}


def test_a_game_off_the_board_can_still_be_priced_on_polymarket_us(board, monkeypatch):
    seen: list[str] = []

    async def _pmus(sport, game, side):
        seen.append(game.home)
        return {"slug": "nba-bos-mia", "clob_side": "yes", "prob": 0.61}

    monkeypatch.setattr(venues_mod.pmus_trade, "resolve", _pmus)
    quotes = run(compare.exchange_prices([_pick("home")]))[compare.key("nba-1", "home")]
    assert set(quotes) == {"polymarket_us"}
    assert seen == ["Boston Celtics"]  # the Game was built from the pick row


def test_rows_that_cannot_be_priced_are_skipped_not_raised(board):
    board.append(_card([_quote("kalshi", -150, 140)]))
    rows = [
        {**_pick("home"), "side": "draw"},  # three-way side: not compared
        {"game_id": "x", "side": "home"},  # no matchup fields
        {**_pick("home"), "kickoff": "not a date"},
        "garbage",
    ]
    assert run(compare.exchange_prices(rows)) == {}


def test_a_failing_lookup_does_not_lose_the_other_exchanges(board, monkeypatch):
    board.append(_card([_quote("kalshi", -150, 140)]))

    async def _boom(sport, game, side):
        raise RuntimeError("gateway 502")

    monkeypatch.setattr(venues_mod.pmus_trade, "resolve", _boom)
    quotes = run(compare.exchange_prices([_pick("home")]))[compare.key("nba-1", "home")]
    assert set(quotes) == {"kalshi"}


# ------------------------------------------------------------------ choosing


def _fav(game_id: str, sport: str = "nba", fair=0.7, model=0.7, **over) -> dict:
    return {**_pick("home", game_id), "sport": sport, "team": f"Team {game_id}", "tier": "MODEL",
            "fair_prob": fair, "model_prob": model, "model": f"{sport}-elo", **over}


def test_a_slate_of_favourites_becomes_a_short_card():
    sports = ("ncaab", "ncaaf", "nba", "nhl")
    slate = [_fav(f"g{i}", sport=sports[i % 4], fair=0.6 + (i % 20) / 100) for i in range(120)]
    card = compare.select_card(slate)
    assert len(card) == compare.DEFAULT_CARD
    assert len({row["game_id"] for row in card}) == len(card)
    assert all(row["prob"] == row["fair_prob"] for row in card)


def test_favourites_need_an_exchange_fair_in_the_band_and_the_model_onside():
    rows = [
        _fav("underdog", fair=0.45),          # under 50%: not a call
        _fav("lock", fair=0.93),              # -1300: nothing to win
        _fav("just-over", fair=0.86),         # the band stops at 85%
        _fav("unpriced", fair=None),          # no exchange prices it
        _fav("split", fair=0.7, model=0.45),  # the model has the other side
        _fav("draw", side="draw"),
        _fav("good", fair=0.7, model=0.66),
    ]
    assert [row["game_id"] for row in compare.select_card(rows)] == ["good"]


def test_the_band_is_fifty_to_eighty_five_inclusive():
    rows = [_fav("low", fair=0.50, model=0.50), _fav("high", fair=0.85, model=0.85)]
    assert {row["game_id"] for row in compare.select_card(rows)} == {"low", "high"}


def test_value_picks_outside_the_band_are_not_sent():
    rows = [
        _fav("lock", tier="STRONG", ev=0.05, price=-1200, book="fanduel", fair=0.94),
        _fav("longshot", tier="LEAN", ev=0.04, price=320, book="draftkings", fair=0.25),
        _fav("fine", tier="LEAN", ev=0.02, price=-150, book="draftkings", fair=0.62),
        # The same game also has an in-band favourite row: it takes the place
        # of the out-of-band value pick instead of the game vanishing.
        _fav("mixed", tier="STRONG", ev=0.06, price=400, book="fanduel", fair=0.22, side="away"),
        _fav("mixed", fair=0.78, model=0.78),
    ]
    card = compare.select_card(rows)
    assert [(row["game_id"], row["tier"]) for row in card] == [("fine", "LEAN"), ("mixed", "MODEL")]
    assert all(0.50 <= row["prob"] <= 0.85 for row in card)


def test_ranked_by_the_lower_of_exchange_and_model():
    rows = [_fav("market-only", fair=0.79, model=0.52), _fav("both", fair=0.72, model=0.74)]
    assert [row["game_id"] for row in compare.select_card(rows)] == ["both", "market-only"]


def test_games_no_engine_rates_only_fill_leftover_places():
    rows = [
        _fav("unrated", fair=0.78, model=0.78) | {"model": compare.MARKET_MODEL},
        _fav("rated", fair=0.62, model=0.62),
    ]
    assert [row["game_id"] for row in compare.select_card(rows)] == ["rated", "unrated"]


def test_no_sport_fills_the_card():
    rows = [_fav(f"cbb{i}", sport="ncaab", fair=0.79) for i in range(10)]
    rows += [_fav(f"nhl{i}", sport="nhl", fair=0.61) for i in range(2)]
    card = compare.select_card(rows, limit=6)
    assert [row["sport"] for row in card].count("ncaab") == 2
    assert {row["game_id"] for row in card} >= {"nhl0", "nhl1"}


def test_one_row_per_game_and_value_picks_lead():
    rows = [
        _fav("g1", created_at="2026-11-09T00:00:00+00:00", team="old"),
        _fav("g1", created_at="2026-11-14T00:00:00+00:00", team="new"),
        _fav("g2", fair=0.79, model=0.79),
        _fav("g3", tier="LEAN", ev=0.03, price=120, book="draftkings"),
        _fav("g4", tier="STRONG", ev=0.02, price=-110, book="fanduel"),
    ]
    card = compare.select_card(rows)
    assert [row["game_id"] for row in card] == ["g4", "g3", "g2", "g1"]
    assert card[-1]["team"] == "new"


def test_daily_card_prices_only_what_it_keeps(board, monkeypatch):
    board.append(_card([_quote("polymarket", -150, 140), _quote("kalshi", -150, 140)], "g0"))
    looked_up: list[str] = []

    async def _pmus(sport, game, side):
        looked_up.append(game.id)
        return None

    monkeypatch.setattr(venues_mod.pmus_trade, "resolve", _pmus)
    slate = [_fav(f"g{i}", fair=0.79 - i / 1000) for i in range(40)]
    out = run(compare.daily_card(slate, limit=2))
    assert out["candidates"] == 40
    assert [row["game_id"] for row in out["card"]] == ["g0", "g1"]
    assert looked_up == ["g0", "g1"]  # two lookups, not forty
    # Same screen price; Polymarket's fee is the lower, so it is the best.
    assert out["card"][0]["best"] == "polymarket"
    assert out["card"][1]["exchanges"] == {} and out["card"][1]["best"] is None


def test_a_pick_over_the_cap_stays_when_an_exchange_sells_it_at_the_cap(board, monkeypatch):
    # Kalshi has both games at 87c (the stored fair, 0.86, is Kalshi's).
    # Polymarket US sells "cheap" at 85c and "dear" at 87c.
    for gid in ("cheap", "dear"):
        board.append(_card([_quote("kalshi", -669, 560)], gid))

    async def _pmus(sport, game, side):
        return {"slug": game.id, "clob_side": "long", "prob": 0.85 if game.id == "cheap" else 0.87}

    monkeypatch.setattr(venues_mod.pmus_trade, "resolve", _pmus)
    slate = [_fav("cheap", fair=0.86, model=0.8), _fav("dear", fair=0.86, model=0.8),
             _fav("far", fair=0.93, model=0.9), _fav("plain", fair=0.7)]
    out = run(compare.daily_card(slate))
    assert [row["game_id"] for row in out["card"]] == ["cheap", "plain"]
    cheap = out["card"][0]
    assert cheap["best"] == "polymarket_us"
    assert cheap["exchanges"]["polymarket_us"]["price"] == pytest.approx(0.85)
    assert cheap["exchanges"]["kalshi"]["price"] > 0.85


def test_the_endpoint_serves_the_card(board, monkeypatch):
    board.append(_card([_quote("polymarket", -150, 140)], "g0"))
    monkeypatch.setattr(main.app.router, "on_startup", [])
    with TestClient(main.app) as client:
        res = client.post("/api/picks/card", json={"picks": [_fav("g0"), _fav("g1")], "limit": 1})
        assert res.status_code == 200
        body = res.json()
        assert body["candidates"] == 2 and len(body["card"]) == 1
        row = body["card"][0]
        assert row["prob"] == pytest.approx(0.7)
        assert row["exchanges"]["polymarket"]["price"] == pytest.approx(0.6)
        assert row["best"] == "polymarket"
        assert client.post("/api/picks/card", json={"picks": []}).json() == {"card": [], "candidates": 0}
        assert client.post("/api/picks/card", json={"picks": [], "limit": 99}).status_code == 422

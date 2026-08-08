"""No-network unit tests for the ESPN odds provider.

Canned payloads mirror real responses from
sports.core.api.espn.com/v2/sports/{path}/events/{id}/competitions/{id}/odds
fetched 2026-07-10 (MLB: Brewers @ Pirates 401816100; EPL: event 740947).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from kryptscout.core.schemas import Game, MoneylineOdds, Sport
from kryptscout.odds import espn_odds
from kryptscout.odds.espn_odds import (
    EspnOddsProvider,
    book_key,
    event_id_for,
    is_in_play_provider,
    league_path_for,
    parse_odds_items,
)

NOW = datetime.now(timezone.utc)

# Real shape (trimmed): provider + top-level moneyLine ints under
# homeTeamOdds/awayTeamOdds, floats for overUnder/overOdds/underOdds, and
# nested open/current blocks we ignore.
MLB_ITEMS = [
    {
        "provider": {"id": "100", "name": "DraftKings", "priority": 1},
        "details": "PIT -129",
        "overUnder": 8.5,
        "spread": -1.5,
        "overOdds": -104.0,
        "underOdds": -116.0,
        "moneylineWinner": False,
        "spreadWinner": False,
        "homeTeamOdds": {
            "favorite": True,
            "underdog": False,
            "moneyLine": -129,
            "current": {
                "pointSpread": {"alternateDisplayValue": "-1.5"},
                "moneyLine": {"value": 1.77, "alternateDisplayValue": "-129"},
            },
        },
        "awayTeamOdds": {
            "favorite": False,
            "underdog": True,
            "moneyLine": 107,
            "current": {
                "pointSpread": {"alternateDisplayValue": "+1.5"},
                "moneyLine": {"value": 2.07, "alternateDisplayValue": "+107"},
            },
        },
    },
]

# Soccer carries drawOdds.moneyLine; Bet 365 items publish only fractional
# summaries (no American moneyLine / overUnder) and must be skipped.
SOCCER_ITEMS = [
    {
        "provider": {"id": "100", "name": "DraftKings", "priority": 1},
        "overUnder": 2.5,
        "overOdds": -145.0,
        "underOdds": 115.0,
        "drawOdds": {"moneyLine": 350.0},
        "homeTeamOdds": {"favorite": False, "underdog": True, "moneyLine": 550},
        "awayTeamOdds": {"favorite": True, "underdog": False, "moneyLine": -200},
    },
    {
        "provider": {"id": "2000", "name": "Bet 365", "priority": 0},
        "drawOdds": {"summary": "1/18", "value": 1.056, "handicap": 0.0},
        "homeTeamOdds": {"favorite": False, "underdog": True},
        "awayTeamOdds": {"favorite": True, "underdog": False},
    },
]


def _game(game_id: str, sport: Sport, league: str = "") -> Game:
    return Game(
        id=game_id, sport=sport, league=league, start_time=NOW,
        home="Home Team", away="Away Team",
    )


# ------------------------------------------------------------------ parsing


def test_parse_mlb_items():
    [ml] = parse_odds_items(MLB_ITEMS, NOW)
    assert ml.book == "draftkings"
    assert ml.fetched_at == NOW
    assert ml.home_ml == -129
    assert ml.away_ml == 107
    assert ml.draw_ml is None
    assert ml.total_line == 8.5
    assert ml.over_price == -104
    assert ml.under_price == -116


def test_parse_soccer_draw_and_skips_lineless_provider():
    [ml] = parse_odds_items(SOCCER_ITEMS, NOW)  # Bet 365 item dropped
    assert ml.book == "draftkings"
    assert ml.home_ml == 550
    assert ml.away_ml == -200
    assert ml.draw_ml == 350
    assert ml.total_line == 2.5
    assert ml.over_price == -145
    assert ml.under_price == 115


def test_parse_empty_items():
    assert parse_odds_items([], NOW) == []


def test_zero_placeholder_prices_become_none():
    # ESPN publishes 0 moneylines on some items (e.g. games in progress);
    # American odds of 0 are undefined and must not reach the edge math.
    items = [
        {
            "provider": {"id": "100", "name": "DraftKings"},
            "overUnder": 8.5,
            "overOdds": 0,
            "underOdds": -116.0,
            "homeTeamOdds": {"moneyLine": 0},
            "awayTeamOdds": {"moneyLine": 107},
        }
    ]
    [ml] = parse_odds_items(items, NOW)
    assert ml.home_ml is None
    assert ml.away_ml == 107
    assert ml.over_price is None
    assert ml.under_price == -116


def test_book_key_normalization():
    assert book_key("DraftKings") == "draftkings"
    assert book_key("ESPN BET") == "espn_bet"
    assert book_key("Bet 365") == "bet_365"
    assert book_key("William Hill (New Jersey)") == "william_hill_new_jersey"


def test_book_key_collapses_alias_spellings():
    # ESPN sends both spellings across events. Two keys for one book inflate
    # the line shop, which takes the best price ACROSS books.
    assert book_key("Draft Kings") == book_key("DraftKings") == "draftkings"


def test_in_play_providers_detected():
    assert is_in_play_provider("DraftKings Live Odds")
    assert is_in_play_provider("DraftKings Live")
    assert is_in_play_provider("Some Book In Play")
    # Pre-game feeds must not be caught by the token match.
    assert not is_in_play_provider("DraftKings")
    assert not is_in_play_provider("ESPN BET")
    assert not is_in_play_provider("Bet 365")


def test_parse_skips_in_play_feed():
    # A live feed prices the game in progress; compared against a strictly
    # pre-game model probability it manufactures enormous fake edges, so it
    # never enters the odds table. See core.schemas.is_in_play.
    items = [
        MLB_ITEMS[0],
        {
            "provider": {"id": "101", "name": "DraftKings Live Odds"},
            "homeTeamOdds": {"moneyLine": -900},
            "awayTeamOdds": {"moneyLine": 600},
        },
    ]
    parsed = parse_odds_items(items, NOW)
    assert [ml.book for ml in parsed] == ["draftkings"]
    assert parsed[0].home_ml == -129


# ----------------------------------------------------------- id / path maps


def test_event_id_parsing():
    assert event_id_for(_game("mlb-401816100", Sport.MLB)) == "401816100"
    assert event_id_for(_game("wnba-401857054", Sport.WNBA)) == "401857054"
    # Sport prefix must match and the remainder must be an ESPN numeric id.
    assert event_id_for(_game("nba-1001x", Sport.NBA)) is None
    assert event_id_for(_game("sr:match:12345", Sport.TENNIS)) is None


def test_league_paths():
    assert league_path_for(_game("mlb-1", Sport.MLB)) == "baseball/leagues/mlb"
    assert league_path_for(_game("nhl-1", Sport.NHL)) == "hockey/leagues/nhl"
    epl = _game("soccer-740947", Sport.SOCCER, league="EPL")
    assert league_path_for(epl) == "soccer/leagues/eng.1"
    mls = _game("soccer-761659", Sport.SOCCER, league="MLS")
    assert league_path_for(mls) == "soccer/leagues/usa.1"
    unknown = _game("soccer-1", Sport.SOCCER, league="Ruritanian Premier")
    assert league_path_for(unknown) is None
    assert league_path_for(_game("tennis-1", Sport.TENNIS)) is None


def test_soccer_league_map_mirrors_datasource():
    from kryptscout.datasources.espn import LEAGUE_PATHS

    assert len(espn_odds.SOCCER_LEAGUES) == len(LEAGUE_PATHS[Sport.SOCCER])
    assert espn_odds.SOCCER_LEAGUES["Bundesliga"] == "ger.1"


# ------------------------------------------------------------ provider/TTL


ODDS = MoneylineOdds(book="draftkings", fetched_at=NOW, home_ml=-129, away_ml=107)


def _stubbed_provider(results, ttl: float = 600.0):
    """Provider whose per-game fetch pops canned results and counts calls."""
    provider = EspnOddsProvider(ttl=ttl)
    calls = []

    async def fake_fetch(client, sem, game):
        calls.append(game.id)
        return results.pop(0)

    provider._fetch_game = fake_fetch
    return provider, calls


def test_odds_for_and_ttl_cache(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(espn_odds.time, "monotonic", lambda: clock[0])
    provider, calls = _stubbed_provider([[ODDS], [ODDS]], ttl=600.0)
    game = _game("mlb-401816100", Sport.MLB)

    first = asyncio.run(provider.odds_for(Sport.MLB, [game]))
    assert first == {game.id: [ODDS]}
    assert calls == [game.id]

    # Within the TTL the cache answers — no second fetch.
    clock[0] += 599.0
    second = asyncio.run(provider.odds_for(Sport.MLB, [game]))
    assert second == {game.id: [ODDS]}
    assert calls == [game.id]

    # Past the TTL the game is refetched.
    clock[0] += 2.0
    third = asyncio.run(provider.odds_for(Sport.MLB, [game]))
    assert third == {game.id: [ODDS]}
    assert calls == [game.id, game.id]


def test_missing_odds_skipped_and_cached():
    # [] = event exists but no odds published (404/empty items): the game is
    # absent from the result and the miss is cached until the TTL lapses.
    provider, calls = _stubbed_provider([[]])
    game = _game("mlb-401816999", Sport.MLB)

    assert asyncio.run(provider.odds_for(Sport.MLB, [game])) == {}
    assert asyncio.run(provider.odds_for(Sport.MLB, [game])) == {}
    assert calls == [game.id]


def test_transient_failure_not_cached():
    # None = network trouble: nothing cached, the next poll retries.
    provider, calls = _stubbed_provider([None, [ODDS]])
    game = _game("mlb-401816100", Sport.MLB)

    assert asyncio.run(provider.odds_for(Sport.MLB, [game])) == {}
    assert asyncio.run(provider.odds_for(Sport.MLB, [game])) == {game.id: [ODDS]}
    assert calls == [game.id, game.id]


def test_build_provider():
    assert isinstance(espn_odds.build_provider(), EspnOddsProvider)

"""Polymarket + Kalshi prediction-market odds providers — no network.

Canned payloads mirror real API responses captured 2026-07-10/11 from
gamma-api.polymarket.com and api.elections.kalshi.com.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from kryptscout.core.schemas import Game, Sport
from kryptscout.odds import kalshi, polymarket

NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)

# Toronto Blue Jays @ San Diego Padres, Jul 11 2026 9:40 PM ET (01:40Z Jul 12)
TOR_SD = Game(
    id="mlb-401700001",
    sport=Sport.MLB,
    start_time=datetime(2026, 7, 12, 1, 40, tzinfo=timezone.utc),
    home="San Diego Padres",
    away="Toronto Blue Jays",
)

# Subway series: Yankees @ Mets — the ambiguity trap for city-named markets
NYY_NYM = Game(
    id="mlb-401700002",
    sport=Sport.MLB,
    start_time=datetime(2026, 7, 11, 23, 10, tzinfo=timezone.utc),
    home="New York Mets",
    away="New York Yankees",
)

# Athletics @ Chicago White Sox — no-city name + shared "Sox" nickname token
OAK_CWS = Game(
    id="mlb-401700003",
    sport=Sport.MLB,
    start_time=datetime(2026, 7, 11, 23, 40, tzinfo=timezone.utc),
    home="Chicago White Sox",
    away="Athletics",
)


@pytest.fixture(autouse=True)
def _clear_caches():
    polymarket._slate_cache.clear()
    kalshi._series_cache.clear()
    yield
    polymarket._slate_cache.clear()
    kalshi._series_cache.clear()


# ---------------------------------------------------------------------------
# Polymarket
# ---------------------------------------------------------------------------

def _pm_event(slug, outcomes, prices, bid, ask, start, **market_extra):
    """Shape observed on GET /events?tag_slug=mlb&closed=false."""
    market = {
        "question": "canned",
        "slug": slug,
        "sportsMarketType": "moneyline",
        "outcomes": outcomes,  # JSON string, exactly as Gamma serves it
        "outcomePrices": prices,
        "active": True,
        "closed": False,
        "gameStartTime": start,
    }
    if bid is not None:
        market["bestBid"] = bid
    if ask is not None:
        market["bestAsk"] = ask
    market.update(market_extra)
    return {"slug": slug, "title": "canned", "markets": [market]}


PM_TOR_SD = _pm_event(
    "mlb-tor-sd-2026-07-11",
    '["Toronto Blue Jays", "San Diego Padres"]',
    '["0.485", "0.515"]',
    0.48,
    0.49,
    "2026-07-12 01:40:00+00",
)


def test_polymarket_parses_real_shaped_event():
    [cand] = polymarket.parse_events([PM_TOR_SD])
    assert cand.outcomes == ["Toronto Blue Jays", "San Diego Padres"]
    assert cand.prob0 == pytest.approx(0.485)  # mid of 0.48/0.49
    assert cand.start == datetime(2026, 7, 12, 1, 40, tzinfo=timezone.utc)


def test_polymarket_skips_futures_props_and_nonmoneyline():
    futures = {"slug": "mlb-world-series-champion-2026", "markets": []}
    props = {"slug": "mlb-tor-sd-2026-07-11-first-five-winner", "markets": []}
    totals = _pm_event(
        "mlb-kc-bal-2026-07-11", '["Over", "Under"]', '["0.45", "0.55"]',
        0.44, 0.46, "2026-07-11 23:05:00+00", sportsMarketType="totals",
    )
    assert polymarket.parse_events([futures, props, totals]) == []


def test_polymarket_string_prices_and_conversion():
    odds = polymarket.match_game(TOR_SD, polymarket.parse_events([PM_TOR_SD]), NOW)
    assert odds is not None
    assert odds.book == "polymarket"
    # Toronto (outcome 0) is the away team: p=0.485 -> +106; home 0.515 -> -106
    assert odds.away_ml == 106
    assert odds.home_ml == -106


def test_polymarket_outcome_names_beat_order():
    """Sides come from outcome names, not position: swapped order still correct."""
    flipped = _pm_event(
        "mlb-tor-sd-2026-07-11",
        '["San Diego Padres", "Toronto Blue Jays"]',
        '["0.515", "0.485"]',
        0.51,
        0.52,
        "2026-07-12 01:40:00+00",
    )
    odds = polymarket.match_game(TOR_SD, polymarket.parse_events([flipped]), NOW)
    assert odds is not None
    assert odds.home_ml == -106  # 0.515 mid of .51/.52
    assert odds.away_ml == 106


def test_polymarket_illiquid_book_skipped():
    dead = _pm_event(
        "mlb-tor-sd-2026-07-11",
        '["Toronto Blue Jays", "San Diego Padres"]',
        '["0.5", "0.5"]',  # default quote, no real market behind it
        0.0,
        1.0,
        "2026-07-12 01:40:00+00",
    )
    assert polymarket.parse_events([dead]) == []


def test_polymarket_wide_spread_falls_back_to_last_trade():
    wide = _pm_event(
        "mlb-tor-sd-2026-07-11",
        '["Toronto Blue Jays", "San Diego Padres"]',
        '["0.495", "0.505"]',
        0.06,
        0.93,
        "2026-07-12 01:40:00+00",
        lastTradePrice=0.62,
    )
    [cand] = polymarket.parse_events([wide])
    assert cand.prob0 == pytest.approx(0.62)


def test_polymarket_doubleheader_ambiguity_skipped():
    """Two same-team markets inside the start window -> conservative skip."""
    game_two = _pm_event(
        "mlb-tor-sd-2026-07-11-game-2",
        '["Toronto Blue Jays", "San Diego Padres"]',
        '["0.44", "0.56"]',
        0.43,
        0.45,
        "2026-07-12 02:40:00+00",
    )
    game_two["slug"] = "mlb-tor-sd-2026-07-11"  # keep game-slug shape
    game_two["markets"][0]["slug"] = "mlb-tor-sd-2026-07-11"
    cands = polymarket.parse_events([PM_TOR_SD, game_two])
    assert len(cands) == 2
    assert polymarket.match_game(TOR_SD, cands, NOW) is None


def test_polymarket_start_time_gates_matching():
    cands = polymarket.parse_events([PM_TOR_SD])
    tomorrow = TOR_SD.model_copy(
        update={"start_time": datetime(2026, 7, 13, 1, 40, tzinfo=timezone.utc)}
    )
    assert polymarket.match_game(tomorrow, cands, NOW) is None


def test_polymarket_odds_for_end_to_end(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(sport):
        calls["n"] += 1
        return [PM_TOR_SD]

    monkeypatch.setattr(polymarket, "_fetch_raw_events", fake_fetch)
    provider = polymarket.build_provider()
    result = asyncio.run(provider.odds_for(Sport.MLB, [TOR_SD, NYY_NYM]))
    assert set(result) == {TOR_SD.id}  # no market for the Mets game -> absent
    [entry] = result[TOR_SD.id]
    assert (entry.home_ml, entry.away_ml) == (-106, 106)
    # Slate discovery is cached: second call reuses the fetch
    asyncio.run(provider.odds_for(Sport.MLB, [TOR_SD]))
    assert calls["n"] == 1


# --- day-ahead behavior (root cause of "no line tomorrow", fixed 2026-07-11)


def test_polymarket_fetch_pages_newest_first():
    """Gamma /events defaults to oldest-first; with more open events than we
    page through (soccer, NBA/NFL props in season) the newest events — i.e.
    tomorrow's games — fell off the end. The fetch must pin newest-first."""
    calls: list[dict] = []

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    class FakeClient:
        async def get(self, url, params=None):
            calls.append(params)
            return FakeResp()

    asyncio.run(polymarket._fetch_tag_events(FakeClient(), "mlb"))
    assert calls[0]["order"] == "id"
    assert calls[0]["ascending"] == "false"
    assert calls[0]["closed"] == "false"


# Real payload values from mlb-mil-pit-2026-07-12 captured 2026-07-11 (a
# day-ahead event with a seeded book): tight 0.47/0.48 quotes a day out.
PM_MIL_PIT_TOMORROW = _pm_event(
    "mlb-mil-pit-2026-07-12",
    '["Milwaukee Brewers", "Pittsburgh Pirates"]',
    '["0.475", "0.525"]',
    0.47,
    0.48,
    "2026-07-12 16:15:00+00",
    lastTradePrice=0.48,
)


def test_polymarket_day_ahead_game_prices():
    game = Game(
        id="mlb-401700100",
        sport=Sport.MLB,
        start_time=datetime(2026, 7, 12, 16, 15, tzinfo=timezone.utc),
        home="Pittsburgh Pirates",
        away="Milwaukee Brewers",
    )
    odds = polymarket.match_game(
        game, polymarket.parse_events([PM_MIL_PIT_TOMORROW]), NOW
    )
    assert odds is not None
    assert odds.away_ml == 111  # Brewers mid 0.475
    assert odds.home_ml == -111


def test_polymarket_placeholder_book_far_future_skipped():
    """Real shape of mlb-pit-cle-2026-07-17 captured 2026-07-11: bid 0.03,
    ask 0.97, no trades, default 0.5/0.5 quote. Previously the 0.5 quote
    leaked through as a fake +100/+100 line; now it must be skipped."""
    placeholder = _pm_event(
        "mlb-pit-cle-2026-07-17",
        '["Pittsburgh Pirates", "Cleveland Guardians"]',
        '["0.5", "0.5"]',
        0.03,
        0.97,
        "2026-07-17 23:10:00+00",
    )
    assert polymarket.parse_events([placeholder]) == []


# ---------------------------------------------------------------------------
# Polymarket tennis
# ---------------------------------------------------------------------------

# Real event atp-sinner-zverev-2026-07-12 (Wimbledon) captured 2026-07-11:
# moneyline outcomes are FULL player names; sibling markets (set winners,
# totals) carry other sportsMarketTypes and surname-only outcome labels.
def _pm_tennis_event(slug, outcomes, bid, ask, start, **extra):
    ev = _pm_event(slug, outcomes, '["0.8", "0.2"]', bid, ask, start, **extra)
    ev["markets"].append(
        {
            "question": "Set 1 Winner: Sinner vs Zverev",
            "sportsMarketType": "tennis_first_set_winner",
            "outcomes": '["Sinner", "Zverev"]',
            "outcomePrices": '["0.71", "0.29"]',
            "active": True,
            "closed": False,
            "bestBid": 0.71,
            "bestAsk": 0.72,
            "gameStartTime": start,
        }
    )
    return ev


PM_SINNER_ZVEREV = _pm_tennis_event(
    "atp-sinner-zverev-2026-07-12",
    '["Jannik Sinner", "Alexander Zverev"]',
    0.80,
    0.81,
    "2026-07-12 15:00:00+00",
)

SINNER_ZVEREV = Game(
    id="tennis-601",
    sport=Sport.TENNIS,
    league="ATP",  # tennis engine leagues are "ATP"/"WTA"
    start_time=datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc),
    home="Jannik Sinner",  # player1; ESPN athlete displayNames
    away="Alexander Zverev",
)


def test_polymarket_tennis_parses_moneyline_only():
    [cand] = polymarket.parse_events([PM_SINNER_ZVEREV], polymarket._TENNIS_SLUG_RE)
    assert cand.outcomes == ["Jannik Sinner", "Alexander Zverev"]
    assert cand.prob0 == pytest.approx(0.805)  # mid of 0.80/0.81


def test_polymarket_tennis_skips_doubles_itf_and_futures():
    # Real slugs observed on tag_slug=tennis, 2026-07-11
    doubles = dict(PM_SINNER_ZVEREV, slug="atp-doubles-grevhei-sabasab-2026-07-11")
    itf = dict(PM_SINNER_ZVEREV, slug="itf-tikhono-shek-2026-07-12")
    futures = {"slug": "2026-mens-wimbledon-winner", "markets": []}
    assert polymarket.parse_events(
        [doubles, itf, futures], polymarket._TENNIS_SLUG_RE
    ) == []


def test_polymarket_tennis_full_name_match_and_conversion():
    cands = polymarket.parse_events([PM_SINNER_ZVEREV], polymarket._TENNIS_SLUG_RE)
    odds = polymarket.match_tennis(SINNER_ZVEREV, cands, NOW)
    assert odds is not None
    assert odds.book == "polymarket"
    assert odds.home_ml == -413  # Sinner 0.805
    assert odds.away_ml == 413
    assert odds.draw_ml is None


def test_polymarket_tennis_tour_gate():
    """A WTA slate entry must never match an ATP market (and vice versa)."""
    cands = polymarket.parse_events([PM_SINNER_ZVEREV], polymarket._TENNIS_SLUG_RE)
    wta_game = SINNER_ZVEREV.model_copy(update={"league": "WTA"})
    assert polymarket.match_tennis(wta_game, cands, NOW) is None
    unknown = SINNER_ZVEREV.model_copy(update={"league": ""})
    assert polymarket.match_tennis(unknown, cands, NOW) is None


def test_polymarket_tennis_side_surname_fallback_and_ambiguity():
    # Initial mismatch (nickname) and surname-only labels fall back to the
    # surname — but only when the two players' surnames differ.
    assert polymarket.tennis_side("Sasha Zverev", SINNER_ZVEREV) == "away"
    assert polymarket.tennis_side("Zverev", SINNER_ZVEREV) == "away"
    assert polymarket.tennis_side("Carlos Alcaraz", SINNER_ZVEREV) is None
    siblings = SINNER_ZVEREV.model_copy(
        update={"home": "Francisco Cerundolo", "away": "Juan Manuel Cerundolo"}
    )
    assert polymarket.tennis_side("Francisco Cerundolo", siblings) == "home"
    assert polymarket.tennis_side("Cerundolo", siblings) is None  # ambiguous
    same_key = SINNER_ZVEREV.model_copy(
        update={"home": "Jannik Sinner", "away": "Jack Sinner"}
    )
    assert polymarket.tennis_side("Jannik Sinner", same_key) is None


def test_polymarket_tennis_transliterated_umlauts_match():
    """Real atp-moeller-faurel-2026-07-12 outcomes are ["Elmer Moeller",
    "Thomas Faurel"]; ESPN lists the same player as "Elmer Moller"."""
    moeller = _pm_tennis_event(
        "atp-moeller-faurel-2026-07-12",
        '["Elmer Moeller", "Thomas Faurel"]',
        0.65,
        0.66,
        "2026-07-12 11:30:00+00",
    )
    game = SINNER_ZVEREV.model_copy(
        update={
            "home": "Thomas Faurel",
            "away": "Elmer Moller",
            "start_time": datetime(2026, 7, 12, 11, 30, tzinfo=timezone.utc),
        }
    )
    cands = polymarket.parse_events([moeller], polymarket._TENNIS_SLUG_RE)
    odds = polymarket.match_tennis(game, cands, NOW)
    assert odds is not None
    assert odds.away_ml == -190  # Moeller mid 0.655
    assert odds.home_ml == 190


def test_polymarket_tennis_start_window():
    """Estimated start times slide (matches queue on courts): 8h window, but
    a different day's market must not match."""
    cands = polymarket.parse_events([PM_SINNER_ZVEREV], polymarket._TENNIS_SLUG_RE)
    slid = SINNER_ZVEREV.model_copy(
        update={"start_time": datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc)}
    )
    assert polymarket.match_tennis(slid, cands, NOW) is not None
    next_day = SINNER_ZVEREV.model_copy(
        update={"start_time": datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)}
    )
    assert polymarket.match_tennis(next_day, cands, NOW) is None


def test_polymarket_tennis_odds_for_end_to_end(monkeypatch):
    async def fake_fetch(sport):
        return [PM_SINNER_ZVEREV]

    monkeypatch.setattr(polymarket, "_fetch_raw_events", fake_fetch)
    provider = polymarket.build_provider()
    result = asyncio.run(provider.odds_for(Sport.TENNIS, [SINNER_ZVEREV]))
    [entry] = result[SINNER_ZVEREV.id]
    assert (entry.home_ml, entry.away_ml) == (-413, 413)


# ---------------------------------------------------------------------------
# Polymarket soccer (3-way negRisk events)
# ---------------------------------------------------------------------------

def _pm_soccer_event(slug, tag, home, away, probs, start, bidask=0.01):
    """Shape observed live 2026-07-11 (e.g. uel-zil-haj-2026-07-16): one
    negRisk event holding three binary Yes/No moneyline markets, team names
    in groupItemTitle. Prices here mirror that real event's books."""
    (h, d, a) = probs

    def mkt(title, question, p, bid, ask):
        return {
            "question": question,
            "sportsMarketType": "moneyline",
            "groupItemTitle": title,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": f'["{p}", "{1 - p}"]',
            "bestBid": bid,
            "bestAsk": ask,
            "active": True,
            "closed": False,
            "gameStartTime": start,
        }

    return {
        "slug": slug,
        "title": f"{home} vs. {away}",
        "negRisk": True,
        "tags": [{"slug": "sports"}, {"slug": "games"}, {"slug": "soccer"}, {"slug": tag}],
        "markets": [
            mkt(home, f"Will {home} win on 2026-07-16?", h, h - bidask, h + bidask),
            mkt(
                f"Draw ({home} vs. {away})",
                f"Will {home} vs. {away} end in a draw?",
                d,
                d - bidask,
                d + bidask,
            ),
            mkt(away, f"Will {away} win on 2026-07-16?", a, a - bidask, a + bidask),
        ],
    }


# Books from the real uel-zil-haj-2026-07-16 event (mids 0.33/0.285/0.405);
# retagged to a covered league — every covered league's own July books were
# still unseeded placeholders at capture time.
PM_ZIL_HAJ = _pm_soccer_event(
    "mls-zil-haj-2026-07-16",
    "mls",
    "MŠK Žilina",
    "HNK Hajduk Split",
    (0.33, 0.285, 0.405),
    "2026-07-16 18:30:00+00",
)

ZIL_HAJ = Game(
    id="soccer-701",
    sport=Sport.SOCCER,
    league="MLS",
    start_time=datetime(2026, 7, 16, 18, 30, tzinfo=timezone.utc),
    home="MSK Zilina",  # ESPN displayNames are accent-folded
    away="Hajduk Split",
)


def test_polymarket_soccer_parses_three_way():
    [cand] = polymarket.parse_soccer_events([PM_ZIL_HAJ])
    assert cand.league == "MLS"
    assert cand.teams[0][0] == "MŠK Žilina"
    assert cand.teams[0][1] == pytest.approx(0.33)
    assert cand.draw_prob == pytest.approx(0.285)
    assert cand.teams[1][1] == pytest.approx(0.405)


def test_polymarket_soccer_match_maps_home_draw_away():
    odds = polymarket.match_soccer(ZIL_HAJ, polymarket.parse_soccer_events([PM_ZIL_HAJ]), NOW)
    assert odds is not None
    assert odds.home_ml == 203  # 0.33
    assert odds.draw_ml == 251  # 0.285
    assert odds.away_ml == 147  # 0.405


def test_polymarket_soccer_league_hint_gates_matching():
    other_league = ZIL_HAJ.model_copy(update={"league": "EPL"})
    cands = polymarket.parse_soccer_events([PM_ZIL_HAJ])
    assert polymarket.match_soccer(other_league, cands, NOW) is None


def test_polymarket_soccer_uncovered_league_tag_skipped():
    """Real chi-wsz-hen-2026-07-11 was tagged chinese-super-league — not one
    of our ESPN leagues, so it must contribute no candidates."""
    csl = _pm_soccer_event(
        "chi-wsz-hen-2026-07-11",
        "chinese-super-league",
        "Wuhan San Zhen FC",
        "Henan FC",
        (0.46, 0.31, 0.475),
        "2026-07-11 11:00:00+00",
    )
    assert polymarket.parse_soccer_events([csl]) == []


def test_polymarket_soccer_inconsistent_sum_skipped():
    """Real shape of the illiquid chi-wsz-hen books: stale quotes summing to
    1.245 across the three binaries -> conservative skip."""
    stale = _pm_soccer_event(
        "mls-wsz-hen-2026-07-11",
        "mls",
        "Wuhan San Zhen FC",
        "Henan FC",
        (0.46, 0.31, 0.475),  # sums to 1.245
        "2026-07-11 11:00:00+00",
    )
    assert polymarket.parse_soccer_events([stale]) == []


def test_polymarket_soccer_odds_for_end_to_end(monkeypatch):
    async def fake_fetch(sport):
        return [PM_ZIL_HAJ]

    monkeypatch.setattr(polymarket, "_fetch_raw_events", fake_fetch)
    provider = polymarket.build_provider()
    result = asyncio.run(provider.odds_for(Sport.SOCCER, [ZIL_HAJ]))
    [entry] = result[ZIL_HAJ.id]
    assert (entry.home_ml, entry.draw_ml, entry.away_ml) == (203, 251, 147)


def test_polymarket_no_games_and_fetch_failure(monkeypatch):
    provider = polymarket.build_provider()
    assert asyncio.run(provider.odds_for(Sport.TENNIS, [])) == {}

    async def boom(sport):
        raise ValueError("gamma down")

    monkeypatch.setattr(polymarket, "_fetch_raw_events", boom)
    assert asyncio.run(provider.odds_for(Sport.MLB, [TOR_SD])) == {}
    assert asyncio.run(provider.odds_for(Sport.TENNIS, [SINNER_ZVEREV])) == {}
    assert asyncio.run(provider.odds_for(Sport.SOCCER, [ZIL_HAJ])) == {}


# ---------------------------------------------------------------------------
# Kalshi
# ---------------------------------------------------------------------------

def _kalshi_market(event_ticker, team, bid="0.4900", ask="0.5000", **extra):
    """Shape observed on GET /markets?series_ticker=KXMLBGAME&status=open."""
    market = {
        "event_ticker": event_ticker,
        "ticker": f"{event_ticker}-X",
        "market_type": "binary",
        "status": "active",
        "yes_sub_title": team,
        "no_sub_title": "other",
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "last_price_dollars": "0.5000",
        "title": "canned winner?",
    }
    market.update(extra)
    return market


K_TOR_SD = "KXMLBGAME-26JUL112140TORSD"  # Jul 11 2026, 9:40 PM ET


def test_kalshi_ticker_datetime_parsing():
    game_date, start = kalshi.parse_ticker_datetime(K_TOR_SD)
    assert (game_date, start) == (
        datetime(2026, 7, 11).date(),
        datetime(2026, 7, 12, 1, 40, tzinfo=timezone.utc),  # 21:40 EDT
    )
    # WNBA tickers omit the start time
    game_date, start = kalshi.parse_ticker_datetime("KXWNBAGAME-26JUL12INDLV")
    assert (game_date, start) == (datetime(2026, 7, 12).date(), None)


def test_kalshi_dollar_strings_and_cents_both_work():
    dollars = _kalshi_market(K_TOR_SD, "Toronto", bid="0.4900", ask="0.5000")
    assert kalshi.market_prob(dollars) == pytest.approx(0.495)
    cents = {  # legacy integer-cent payloads
        "event_ticker": K_TOR_SD,
        "status": "active",
        "yes_sub_title": "Toronto",
        "yes_bid": 49,
        "yes_ask": 50,
        "last_price": 50,
    }
    assert kalshi.market_prob(cents) == pytest.approx(0.495)


def test_kalshi_city_names_match_espn_display_names():
    events = kalshi.parse_markets(
        [
            _kalshi_market(K_TOR_SD, "Toronto", bid="0.4900", ask="0.5000"),
            _kalshi_market(K_TOR_SD, "San Diego", bid="0.5100", ask="0.5200"),
        ]
    )
    odds = kalshi.match_game(TOR_SD, events, NOW)
    assert odds is not None
    assert odds.book == "kalshi"
    assert odds.away_ml == 102  # Toronto 0.495
    assert odds.home_ml == -106  # San Diego 0.515


def test_kalshi_nickname_disambiguates_shared_tokens():
    ticker = "KXMLBGAME-26JUL111940OAKCWS"
    events = kalshi.parse_markets(
        [
            _kalshi_market(ticker, "Athletics", bid="0.4000", ask="0.4100"),
            _kalshi_market(ticker, "White Sox", bid="0.5900", ask="0.6000"),
        ]
    )
    odds = kalshi.match_game(OAK_CWS, events, NOW)
    assert odds is not None
    assert odds.home_ml == -147  # White Sox (home) 0.595
    assert odds.away_ml == 147  # Athletics (away) 0.405


def test_kalshi_shorthand_aliases():
    """Kalshi's real labels for this game are "A's" and "Chicago WS"."""
    ticker = "KXMLBGAME-26JUL111940ATHCWS"
    events = kalshi.parse_markets(
        [
            _kalshi_market(ticker, "A's", bid="0.4000", ask="0.4100"),
            _kalshi_market(ticker, "Chicago WS", bid="0.5900", ask="0.6000"),
        ]
    )
    odds = kalshi.match_game(OAK_CWS, events, NOW)
    assert odds is not None
    assert odds.home_ml == -147 and odds.away_ml == 147


def test_kalshi_inconsistent_complement_skipped():
    """Seen live: thin books where both teams' mids imply prob sum ~1.24."""
    events = kalshi.parse_markets(
        [
            _kalshi_market(K_TOR_SD, "Toronto", bid="0.5300", ask="0.5500"),
            _kalshi_market(K_TOR_SD, "San Diego", bid="0.6900", ask="0.7100"),
        ]
    )
    assert kalshi.match_game(TOR_SD, events, NOW) is None


def test_kalshi_ambiguous_city_skipped():
    """"New York" matches both subway-series teams -> must skip, not guess."""
    ticker = "KXMLBGAME-26JUL111910NYYNYM"
    events = kalshi.parse_markets(
        [
            _kalshi_market(ticker, "New York", bid="0.5500", ask="0.5600"),
            _kalshi_market(ticker, "New York", bid="0.4400", ask="0.4500"),
        ]
    )
    assert kalshi.match_game(NYY_NYM, events, NOW) is None


def test_kalshi_one_sided_market():
    events = kalshi.parse_markets(
        [_kalshi_market(K_TOR_SD, "Toronto", bid="0.4900", ask="0.5000")]
    )
    odds = kalshi.match_game(TOR_SD, events, NOW)
    assert odds is not None
    assert odds.away_ml == 102
    assert odds.home_ml is None


def test_kalshi_illiquid_book_skipped():
    dead = _kalshi_market(
        K_TOR_SD, "Toronto", bid="0.0000", ask="1.0000",
        last_price_dollars="0.0000",
    )
    assert kalshi.market_prob(dead) is None
    assert kalshi.parse_markets([dead]) == []


def test_kalshi_date_gates_matching():
    sunday = "KXMLBGAME-26JUL121610TORSD"  # same teams, next day
    events = kalshi.parse_markets(
        [
            _kalshi_market(sunday, "Toronto", bid="0.5000", ask="0.5100"),
            _kalshi_market(sunday, "San Diego", bid="0.4900", ask="0.5000"),
        ]
    )
    assert kalshi.match_game(TOR_SD, events, NOW) is None


def test_kalshi_odds_for_end_to_end(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(sport):
        calls["n"] += 1
        return [
            _kalshi_market(K_TOR_SD, "Toronto", bid="0.4900", ask="0.5000"),
            _kalshi_market(K_TOR_SD, "San Diego", bid="0.5100", ask="0.5200"),
        ]

    monkeypatch.setattr(kalshi, "_fetch_raw_markets", fake_fetch)
    provider = kalshi.build_provider()
    result = asyncio.run(provider.odds_for(Sport.MLB, [TOR_SD, OAK_CWS]))
    assert set(result) == {TOR_SD.id}
    [entry] = result[TOR_SD.id]
    assert (entry.home_ml, entry.away_ml) == (-106, 102)
    asyncio.run(provider.odds_for(Sport.MLB, [TOR_SD]))
    assert calls["n"] == 1  # 10-minute slate cache


def test_kalshi_unsupported_sport_and_fetch_failure(monkeypatch):
    provider = kalshi.build_provider()
    assert asyncio.run(provider.odds_for(Sport.SOCCER, [TOR_SD])) == {}

    async def boom(sport):
        raise ValueError("kalshi down")

    monkeypatch.setattr(kalshi, "_fetch_raw_markets", boom)
    assert asyncio.run(provider.odds_for(Sport.MLB, [TOR_SD])) == {}

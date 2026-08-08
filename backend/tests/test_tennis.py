"""No-network tests for the tennis engine: name normalization, blend math,
synthetic Elo replay over canned TML (ATP) and tennis-data (WTA) rows, and
ESPN scoreboard parsing against a canned payload."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from kryptscout.core.schemas import Sport
from kryptscout.sports.tennis import (
    OVERALL_WEIGHT,
    SURFACE_WEIGHT,
    TennisEngine,
    TourBook,
    build_engine,
    build_odds_index,
    devig_decimal,
    initial_key,
    join_market,
    market_benchmark,
    normalize_name,
    sackmann_join_key,
    surface_for_tournament,
    tennis_data_key,
    wta_rating_key,
)


# ------------------------------------------------------------- normalization


def test_normalize_name_accents_case_hyphens():
    assert normalize_name("Félix Auger-Aliassime") == "felix auger aliassime"
    assert normalize_name("  NOVAK   Djokovic ") == "novak djokovic"
    assert normalize_name("N. Djokovic") == "n djokovic"


def test_initial_key():
    assert initial_key("Carlos Alcaraz") == "c alcaraz"
    assert initial_key("Juan Martin del Potro") == "j potro"
    assert initial_key("Nadal") is None


def test_resolve_exact_and_initial_fallback():
    book = TourBook()
    book.index_player("Carlos Alcaraz")
    book.index_player("Novak Djokovic")
    assert book.resolve("carlos ALCARAZ") == "Carlos Alcaraz"
    assert book.resolve("C. Alcaraz") == "Carlos Alcaraz"
    assert book.resolve("Some Unknown") is None


def test_resolve_ambiguous_initial_disabled():
    book = TourBook()
    book.index_player("Andy Murray")
    book.index_player("Adam Murray")
    assert book.resolve("A. Murray") is None  # ambiguous fallback dropped
    assert book.resolve("Andy Murray") == "Andy Murray"


def test_wta_rating_key():
    assert wta_rating_key("Sakkari M.") == "sakkari|m"
    # tennis-data's own multi-letter disambiguation survives in the key
    assert wta_rating_key("Pliskova Ka.") == "pliskova|ka"
    assert wta_rating_key("Wang Xin.") == "wang|xin"
    assert wta_rating_key("Del Potro J.M.") == "potro|jm"
    # Spelling variants of one player merge onto one key
    assert wta_rating_key("Ruse E-G.") == "ruse|eg"
    assert wta_rating_key("Ruse E.G.") == "ruse|eg"
    # Multi-token surname keeps only the last token (matches ESPN full names)
    assert wta_rating_key("Romero Gormaz L.") == "gormaz|l"
    # Not tennis-data format -> no key
    assert wta_rating_key("Maria Sakkari") is None
    assert wta_rating_key("Nadal") is None


def test_wta_abbreviated_resolution():
    book = TourBook(abbreviated_names=True)
    for raw in ("Sakkari M.", "Pliskova Ka.", "Pliskova Kr.",
                "Romero Gormaz L.", "Del Potro J.M."):
        book.index_player(wta_rating_key(raw))
    # ESPN full name, tennis-data name, and raw key all resolve
    assert book.resolve("Maria Sakkari") == "sakkari|m"
    assert book.resolve("Sakkari M.") == "sakkari|m"
    assert book.resolve("sakkari|m") == "sakkari|m"
    # Given-name prefix disambiguates the shared surname+initial
    assert book.resolve("Karolina Pliskova") == "pliskova|ka"
    assert book.resolve("Kristyna Pliskova") == "pliskova|kr"
    # A bare first initial cannot pick between Ka./Kr. -> ambiguous
    assert book.resolve("K. Pliskova") is None
    # Multi-token surnames and multi-initial keys (given-name acronym match)
    assert book.resolve("Leyre Romero Gormaz") == "gormaz|l"
    assert book.resolve("Juan Martin del Potro") == "potro|jm"
    # Unknowns and mononyms fall through to None (0.5 prediction path)
    assert book.resolve("Somebody Else") is None
    assert book.resolve("Mononym") is None


def test_surface_for_tournament():
    assert surface_for_tournament("Wimbledon") == "Grass"
    assert surface_for_tournament("Roland Garros") == "Clay"
    assert surface_for_tournament("Nordea Open") == "Clay"
    assert surface_for_tournament("US Open") == "Hard"


# --------------------------------------------------------------- blend math


def test_match_prob_blend():
    book = TourBook()
    book.overall.ratings = {"A": 1600.0, "B": 1500.0}
    book.surfaces["Grass"].ratings = {"A": 1450.0, "B": 1550.0}
    diff = OVERALL_WEIGHT * 100.0 + SURFACE_WEIGHT * (-100.0)
    expected = 1.0 / (1.0 + 10.0 ** (-diff / 400.0))
    assert book.match_prob("A", "B", "Grass") == pytest.approx(expected)
    # Unknown surface -> overall only
    overall_only = 1.0 / (1.0 + 10.0 ** (-100.0 / 400.0))
    assert book.match_prob("A", "B", None) == pytest.approx(overall_only)
    assert book.match_prob("A", "B", "Carpet") == pytest.approx(overall_only)


def test_match_prob_symmetry():
    book = TourBook()
    book.overall.ratings = {"A": 1580.0, "B": 1470.0}
    book.surfaces["Clay"].ratings = {"A": 1520.0, "B": 1530.0}
    p = book.match_prob("A", "B", "Clay")
    q = book.match_prob("B", "A", "Clay")
    assert p + q == pytest.approx(1.0)


# ---------------------------------------------------------- synthetic replay


def _synthetic_matches() -> pd.DataFrame:
    """Dominator beats everyone; Grasscourt only ever wins on grass."""
    rows = []
    num = 1
    for month in range(1, 10):
        d = 20240100 + month * 100 + 1  # fake but monotonically increasing
        rows.append((d, num, "Hard", "Dominator", "Journeyman", "6-1 6-1")); num += 1
        rows.append((d, num, "Hard", "Dominator", "Grasscourt", "6-2 6-2")); num += 1
        rows.append((d, num, "Grass", "Grasscourt", "Journeyman", "6-0 6-0")); num += 1
    # A walkover that must be ignored
    rows.append((20241001, num, "Hard", "Journeyman", "Dominator", "W/O"))
    return pd.DataFrame(
        rows,
        columns=["tourney_date", "match_num", "surface", "winner_name", "loser_name", "score"],
    )


def test_replay_ratings_and_walkover_filtering():
    engine = build_engine()
    matches = TennisEngine._load_matches(
        [_synthetic_matches().to_csv(index=False).encode()]
    )
    # Walkover row filtered out
    assert len(matches) == 27
    metrics = engine.replay("ATP", matches)
    book = engine.books["ATP"]
    assert book.overall.get("Dominator") > book.overall.get("Journeyman")
    # Grasscourt dominates the grass book despite losing overall to Dominator
    assert book.surfaces["Grass"].get("Grasscourt") > book.surfaces["Grass"].get(
        "Dominator"
    )
    assert metrics["n"] == 0  # all synthetic dates precede the backtest window
    # Surface blend shifts the matchup toward the grass specialist on grass
    p_hard = book.match_prob("Dominator", "Grasscourt", "Hard")
    p_grass = book.match_prob("Dominator", "Grasscourt", "Grass")
    assert p_grass < p_hard
    assert p_hard > 0.5


def test_replay_skips_unparseable_tourney_date_instead_of_raising():
    """One typo'd cell upstream must not sink the whole refresh.

    stats.tennismylife.org shipped tourney_date "202s60807" in its ongoing
    feed, which froze the tennis ratings for days: replay() raised ValueError,
    refresh_data() never reached _save_state().
    """
    engine = build_engine()
    df = _synthetic_matches()
    df["tourney_date"] = df["tourney_date"] + 10000  # shift into 2025
    good = len(df) - 1  # the walkover row is filtered out by _load_matches
    # Mixed cell → the whole column reads back as strings, exactly as the real
    # CSV does once upstream ships one non-numeric date.
    df["tourney_date"] = df["tourney_date"].astype(str)
    df.loc[df.index[0], "tourney_date"] = "202s60807"
    matches = TennisEngine._load_matches([df.to_csv(index=False).encode()])

    metrics = engine.replay("ATP", matches)

    assert metrics["n"] == good - 1  # every row but the corrupt one scored
    assert engine.books["ATP"].overall.get("Dominator") > 0
    # The corrupt row never reaches the walk-forward log either.
    assert all(isinstance(rec["date"], int) for rec in engine.backtest_log["ATP"])


def test_replay_backtest_metrics_window():
    engine = build_engine()
    df = _synthetic_matches()
    df["tourney_date"] = df["tourney_date"] + 10000  # shift into 2025
    matches = TennisEngine._load_matches([df.to_csv(index=False).encode()])
    metrics = engine.replay("ATP", matches)
    assert metrics["n"] == 27
    assert 0.0 <= metrics["accuracy"] <= 1.0
    assert metrics["log_loss"] > 0.0


def _tml_year_csv() -> bytes:
    """Canned TennisMyLife per-year rows (Sackmann-compatible columns)."""
    return (
        "tourney_id,tourney_name,surface,tourney_date,match_num,"
        "winner_name,loser_name,score\n"
        "2026-540,Wimbledon,Grass,20260628,101,Jannik Sinner,Jan-Lennard Struff,6-3 6-4 6-2\n"
        "2026-540,Wimbledon,Grass,20260628,102,Novak Djokovic,Felix Auger-Aliassime,7-5 6-4 6-4\n"
    ).encode()


def _tml_ongoing_csv() -> bytes:
    """Ongoing-tournaments feed: one row overlaps the year file, one is new.
    The feed stamps per-match dates rather than the tournament start."""
    return (
        "tourney_id,tourney_name,surface,tourney_date,match_num,"
        "winner_name,loser_name,score\n"
        "2026-540,Wimbledon,Grass,20260707,102,Novak Djokovic,Felix Auger-Aliassime,7-5 6-4 6-4\n"
        "2026-540,Wimbledon,Grass,20260710,103,Alexander Zverev,Arthur Fery,6-4 6-4 6-4\n"
    ).encode()


def test_load_matches_dedupes_ongoing_overlap():
    matches = TennisEngine._load_matches([_tml_year_csv(), _tml_ongoing_csv()])
    assert len(matches) == 3
    dj = matches[matches.winner_name == "Novak Djokovic"]
    assert len(dj) == 1
    # Year-file row (tournament-start date) wins over the ongoing duplicate
    assert int(dj.tourney_date.iloc[0]) == 20260628
    assert "Alexander Zverev" in set(matches.winner_name)


# --------------------------------------------- WTA results (tennis-data.co.uk)


def _wta_workbook_frame() -> pd.DataFrame:
    nan = float("nan")
    rows = [
        # Pinnacle prices -> devigged market_p
        ("2025-03-12", "Hard", "Sakkari M.", "Swiatek I.", "Completed",
         1.60, 2.40, 1.55, 2.35, 1.57, 2.37),
        # PS missing -> B365 fallback
        ("2025-03-13", "Clay", "Pliskova Ka.", "Gauff C.", "Retired",
         nan, nan, 1.30, 3.50, 1.32, 3.40),
        # No usable prices -> row kept, market_p NaN
        ("2025-03-14", "Grass", "Sakkari M.", "Gauff C.", "Completed",
         nan, nan, nan, nan, nan, nan),
        # Walkover -> dropped entirely
        ("2025-03-15", "Hard", "Swiatek I.", "Pliskova Ka.", "Walkover",
         1.50, 2.60, nan, nan, nan, nan),
        # Unkeyable loser (no dotted initial) -> dropped
        ("2025-03-16", "Hard", "Sakkari M.", "Mononym", "Completed",
         1.50, 2.60, nan, nan, nan, nan),
        # Unknown surface -> surface None, row kept
        ("2025-03-17", "Carpet", "Gauff C.", "Swiatek I.", "Completed",
         2.00, 1.80, nan, nan, nan, nan),
    ]
    df = pd.DataFrame(
        rows,
        columns=["Date", "Surface", "Winner", "Loser", "Comment",
                 "PSW", "PSL", "B365W", "B365L", "AvgW", "AvgL"],
    )
    df["Date"] = pd.to_datetime(df["Date"])
    return df


def test_load_wta_results(tmp_path):
    path = tmp_path / "wta_2025.xlsx"
    _wta_workbook_frame().to_excel(path, index=False)
    frame = TennisEngine._load_wta_results([path])
    assert len(frame) == 4  # walkover + unkeyable rows dropped
    assert list(frame.winner_name) == [
        "sakkari|m", "pliskova|ka", "sakkari|m", "gauff|c"
    ]
    assert list(frame.tourney_date) == [20250312, 20250313, 20250314, 20250317]
    # Devigged winner probability from the best available pair
    assert frame.market_p.iloc[0] == pytest.approx(devig_decimal(1.60, 2.40)[0])
    assert frame.market_p.iloc[1] == pytest.approx(devig_decimal(1.30, 3.50)[0])
    assert pd.isna(frame.market_p.iloc[2])
    # Carpet is not a rated surface
    assert pd.isna(frame.surface.iloc[3]) or frame.surface.iloc[3] is None
    # Missing files / wrong columns yield an empty frame
    empty = TennisEngine._load_wta_results([])
    assert empty.empty


def test_replay_wta_market_passthrough_and_resolution(tmp_path):
    path = tmp_path / "wta_2025.xlsx"
    _wta_workbook_frame().to_excel(path, index=False)
    engine = build_engine()
    frame = TennisEngine._load_wta_results([path])
    metrics = engine.replay("WTA", frame)
    assert metrics["n"] == 4
    log = engine.backtest_log["WTA"]
    with_market = [r for r in log if "market_p" in r]
    assert len(with_market) == 3  # the NaN-priced row carries no market_p
    assert with_market[0]["market_p"] == pytest.approx(
        devig_decimal(1.60, 2.40)[0]
    )
    assert market_benchmark(with_market)["n"] == 3
    # Ratings are keyed by wta_rating_key and resolve from ESPN full names
    book = engine.books["WTA"]
    assert book.abbreviated_names
    assert book.resolve("Maria Sakkari") == "sakkari|m"
    assert book.resolve("Karolina Pliskova") == "pliskova|ka"
    assert book.overall.get("sakkari|m") != 1500.0


# ------------------------------------------------- market benchmark (no net)


def test_devig_decimal():
    # Symmetric viggy pair -> 50/50
    p, q = devig_decimal(1.87, 1.87)
    assert p == pytest.approx(0.5)
    assert q == pytest.approx(0.5)
    # Asymmetric pair: 1/1.5=.6667, 1/2.5=.4 -> normalized 0.625/0.375
    p, q = devig_decimal(1.5, 2.5)
    assert p == pytest.approx(0.625)
    assert q == pytest.approx(0.375)
    assert p + q == pytest.approx(1.0)
    with pytest.raises(ValueError):
        devig_decimal(1.0, 2.0)


def test_tennis_data_key():
    assert tennis_data_key("Alcaraz C.") == "alcaraz|c"
    assert tennis_data_key("De Minaur A.") == "minaur|a"
    assert tennis_data_key("Auger-Aliassime F.") == "aliassime|f"
    assert tennis_data_key("Del Potro J.M.") == "potro|j"
    assert tennis_data_key("Pliskova Ka.") == "pliskova|k"  # 2-letter initial
    assert tennis_data_key("Nadal") is None  # no initial


def test_sackmann_join_key_matches_tennis_data_key():
    pairs = [
        ("Carlos Alcaraz", "Alcaraz C."),
        ("Alex de Minaur", "De Minaur A."),
        ("Felix Auger-Aliassime", "Auger-Aliassime F."),
        ("Juan Martin del Potro", "Del Potro J.M."),
        ("Iga Swiatek", "Swiatek I."),  # accented Świątek normalizes too
    ]
    for sackmann, tennis_data in pairs:
        assert sackmann_join_key(sackmann) == tennis_data_key(tennis_data)
    assert sackmann_join_key("Nadal") is None


def _odds_frame() -> pd.DataFrame:
    nan = float("nan")
    rows = [
        # Joins: 2 days into the tournament, Pinnacle prices present
        ("2025-03-12", "Alcaraz C.", "Djokovic N.", 1.60, 2.40, 1.55, 2.35, 1.57, 2.37),
        # Joins via ±1-day tolerance (day before tourney_date), PS missing
        # -> falls back to B365
        ("2025-03-09", "Sinner J.", "Zverev A.", nan, nan, 1.30, 3.50, 1.32, 3.40),
        # Out of window (weeks later) — must NOT join
        ("2025-04-20", "Alcaraz C.", "Djokovic N.", 1.70, 2.20, nan, nan, 1.68, 2.18),
        # Ambiguous rematch inside the window — both dropped
        ("2025-03-11", "Rune H.", "Fritz T.", 1.90, 1.90, 1.87, 1.87, 1.88, 1.88),
        ("2025-03-14", "Rune H.", "Fritz T.", 2.00, 1.80, 1.95, 1.78, 1.97, 1.79),
        # No usable prices at all — skipped at index build time
        ("2025-03-12", "Ruud C.", "Medvedev D.", nan, nan, nan, nan, nan, nan),
    ]
    df = pd.DataFrame(
        rows,
        columns=["Date", "Winner", "Loser", "PSW", "PSL",
                 "B365W", "B365L", "AvgW", "AvgL"],
    )
    df["Date"] = pd.to_datetime(df["Date"])
    return df


def _backtest_records() -> list[dict]:
    return [
        {"date": 20250310, "winner": "Carlos Alcaraz",
         "loser": "Novak Djokovic", "p": 0.7},
        {"date": 20250310, "winner": "Jannik Sinner",
         "loser": "Alexander Zverev", "p": 0.8},
        {"date": 20250310, "winner": "Holger Rune",
         "loser": "Taylor Fritz", "p": 0.6},  # ambiguous odds -> dropped
        {"date": 20250310, "winner": "Casper Ruud",
         "loser": "Daniil Medvedev", "p": 0.55},  # no priced row -> dropped
        {"date": 20250310, "winner": "Nobody Known",
         "loser": "Also Unknown", "p": 0.5},  # no odds row -> dropped
    ]


def test_build_odds_index_price_preference_and_filtering():
    index = build_odds_index(_odds_frame())
    # Priceless row dropped entirely
    assert ("ruud|c", "medvedev|d") not in index
    # Pinnacle preferred when present
    assert index[("alcaraz|c", "djokovic|n")][0][1:] == (1.60, 2.40)
    # B365 fallback when PS is NaN
    assert index[("sinner|j", "zverev|a")][0][1:] == (1.30, 3.50)
    # Empty / column-less frames yield an empty index
    assert build_odds_index(pd.DataFrame()) == {}


def test_join_market_window_tolerance_and_ambiguity():
    joined = join_market(_backtest_records(), build_odds_index(_odds_frame()))
    by_winner = {r["winner"]: r for r in joined}
    assert set(by_winner) == {"Carlos Alcaraz", "Jannik Sinner"}
    # Devigged Pinnacle prob for Alcaraz: (1/1.6) / (1/1.6 + 1/2.4)
    assert by_winner["Carlos Alcaraz"]["market_p"] == pytest.approx(0.6, abs=1e-9)
    # Model prob carried through untouched
    assert by_winner["Carlos Alcaraz"]["p"] == 0.7
    # Sinner row joined a day *before* tourney_date via the -1 tolerance,
    # devigged from the B365 fallback pair
    expected = (1 / 1.30) / (1 / 1.30 + 1 / 3.50)
    assert by_winner["Jannik Sinner"]["market_p"] == pytest.approx(expected)


def test_market_benchmark_math():
    joined = [
        {"p": 0.7, "market_p": 0.6},   # both correct
        {"p": 0.4, "market_p": 0.55},  # model wrong, market right
    ]
    m = market_benchmark(joined)
    assert m["n"] == 2
    assert m["model_accuracy"] == pytest.approx(0.5)
    assert m["market_accuracy"] == pytest.approx(1.0)
    import math
    assert m["model_log_loss"] == pytest.approx(
        -(math.log(0.7) + math.log(0.4)) / 2
    )
    assert m["market_log_loss"] == pytest.approx(
        -(math.log(0.6) + math.log(0.55)) / 2
    )
    assert market_benchmark([]) == {"n": 0}


def test_replay_populates_backtest_log():
    engine = build_engine()
    df = _synthetic_matches()
    df["tourney_date"] = df["tourney_date"] + 10000  # shift into 2025
    matches = TennisEngine._load_matches([df.to_csv(index=False).encode()])
    engine.replay("ATP", matches)
    log = engine.backtest_log["ATP"]
    assert len(log) == 27  # every scored match logged, walkover excluded
    first = log[0]
    assert set(first) == {"date", "winner", "loser", "p"}
    assert first["date"] >= 20250101
    assert 0.0 < first["p"] < 1.0


# ------------------------------------------------------------ ESPN parsing


def _espn_payload() -> dict:
    return {
        "events": [
            {
                "id": "188-2026",
                "name": "Wimbledon",
                "groupings": [
                    {
                        "grouping": {"slug": "mens-singles"},
                        "competitions": [
                            {
                                "id": "177490",
                                "date": "2026-07-10T14:30Z",
                                "status": {"type": {"state": "pre"}},
                                "competitors": [
                                    {
                                        "homeAway": "home",
                                        "athlete": {"displayName": "Novak Djokovic"},
                                    },
                                    {
                                        "homeAway": "away",
                                        "athlete": {"displayName": "Jannik Sinner"},
                                    },
                                ],
                            },
                            {  # completed match — skipped
                                "id": "177489",
                                "date": "2026-07-10T12:30Z",
                                "status": {"type": {"state": "post"}},
                                "competitors": [],
                            },
                            {  # TBD placeholder — skipped
                                "id": "177491",
                                "date": "2026-07-10T15:00Z",
                                "status": {"type": {"state": "pre"}},
                                "competitors": [
                                    {"homeAway": "home", "athlete": {"displayName": "TBD"}},
                                    {"homeAway": "away", "athlete": {"displayName": "TBD"}},
                                ],
                            },
                        ],
                    },
                    {
                        "grouping": {"slug": "mens-doubles"},
                        "competitions": [
                            {
                                "id": "999",
                                "date": "2026-07-10T11:00Z",
                                "status": {"type": {"state": "pre"}},
                                "competitors": [],
                            }
                        ],
                    },
                    {
                        "grouping": {"slug": "womens-singles"},
                        "competitions": [
                            {
                                "id": "177707",
                                "date": "2026-07-10T13:00Z",
                                "status": {"type": {"state": "pre"}},
                                "competitors": [
                                    {
                                        "homeAway": "home",
                                        "athlete": {"displayName": "Linda Noskova"},
                                    },
                                    {
                                        "homeAway": "away",
                                        "athlete": {"displayName": "Karolina Muchova"},
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }
        ]
    }


def test_parse_scoreboards():
    engine = build_engine()
    payload = _espn_payload()
    # Same payload twice mimics the ATP+WTA endpoints overlapping — dedupe
    games = engine._parse_scoreboards([payload, payload], date(2026, 7, 10))
    assert [g.id for g in games] == ["tennis-177707", "tennis-177490"]
    match = games[1]
    assert match.sport == Sport.TENNIS
    assert match.league == "ATP"
    assert match.home == "Novak Djokovic"
    assert match.away == "Jannik Sinner"
    assert match.start_time == datetime(2026, 7, 10, 14, 30, tzinfo=timezone.utc)
    assert games[0].league == "WTA"
    assert engine._game_surfaces["tennis-177490"] == "Grass"


def test_parse_scoreboards_filters_other_dates():
    engine = build_engine()
    games = engine._parse_scoreboards([_espn_payload()], date(2026, 7, 11))
    assert games == []


# ------------------------------------------------------------- predictions


@pytest.mark.asyncio
async def test_predict_known_and_unknown_players():
    from kryptscout.core.schemas import Game

    engine = build_engine()
    engine._loaded = True  # bypass state loading — no network, no disk
    book = engine.books["ATP"]
    book.overall.ratings = {"Novak Djokovic": 1700.0, "Jannik Sinner": 1600.0}
    book.surfaces["Grass"].ratings = {"Novak Djokovic": 1650.0, "Jannik Sinner": 1550.0}
    for p in book.overall.ratings:
        book.index_player(p)
    known = Game(
        id="tennis-1", sport=Sport.TENNIS, league="ATP",
        start_time=datetime(2026, 7, 10, tzinfo=timezone.utc),
        home="Novak Djokovic", away="Jannik Sinner",
    )
    unknown = Game(
        id="tennis-2", sport=Sport.TENNIS, league="ATP",
        start_time=datetime(2026, 7, 10, tzinfo=timezone.utc),
        home="Nobody Nowhere", away="Jannik Sinner",
    )
    engine._game_surfaces["tennis-1"] = "Grass"
    preds = await engine.predict([known, unknown])
    assert preds[0].home_win_prob > 0.5
    assert preds[0].home_win_prob + preds[0].away_win_prob == pytest.approx(1.0)
    assert preds[0].model == "tennis-elo-v1"
    assert preds[0].predicted_total is None
    assert preds[1].home_win_prob == 0.5
    assert preds[1].confidence == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_predict_wta_key_shape_resolution():
    """WTA ratings key on surname|initials; ESPN full names must resolve."""
    from kryptscout.core.schemas import Game

    engine = build_engine()
    engine._loaded = True
    book = engine.books["WTA"]
    assert book.abbreviated_names
    book.overall.ratings = {"sakkari|m": 1700.0, "swiatek|i": 1500.0}
    for p in book.overall.ratings:
        book.index_player(p)
    game = Game(
        id="tennis-9", sport=Sport.TENNIS, league="WTA",
        start_time=datetime(2026, 7, 10, tzinfo=timezone.utc),
        home="Maria Sakkari", away="Iga Swiatek",
    )
    preds = await engine.predict([game])
    assert preds[0].home_win_prob > 0.5
    assert preds[0].confidence > 0.1

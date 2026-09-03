"""Store tests — record/settle/performance roundtrip, CLV, dedupe, movement,
plus pick recording/grading via record_cards/settle (details in test_picks).

Everything runs against a tmp_path SQLite database (db.DB_PATH is
monkeypatched) and settle()'s results source is injected, so no test touches
the network.
"""
from __future__ import annotations

import asyncio
import math
from datetime import date, datetime, timedelta, timezone

import pytest

from kryptscout.core.oddsmath import remove_vig
from kryptscout.core.schemas import Game, GameCard, MoneylineOdds, Prediction, Sport
from kryptscout.store import db, tracker

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")


def _game(
    game_id: str,
    start: datetime,
    home: str = "Home Team",
    away: str = "Away Team",
    sport: Sport = Sport.NBA,
) -> Game:
    return Game(
        id=game_id, sport=sport, league="", start_time=start, home=home, away=away
    )


def _prediction(
    game: Game,
    home_prob: float,
    generated_at: datetime | None = None,
    model: str = "nba-elo-v1",
) -> Prediction:
    return Prediction(
        game_id=game.id,
        sport=game.sport,
        model=model,
        generated_at=generated_at or NOW,
        home_win_prob=home_prob,
        away_win_prob=1.0 - home_prob,
    )


def _final(game: Game, home_score: int, away_score: int, game_id: str | None = None) -> Game:
    return game.model_copy(
        update={
            "id": game_id or game.id,
            "home_score": home_score,
            "away_score": away_score,
            "completed": True,
        }
    )


def _fetcher(results: list[Game]):
    calls: list[tuple[Sport, date]] = []

    async def fetch(sport: Sport, on: date) -> list[Game]:
        calls.append((sport, on))
        return [r for r in results if r.start_time.date() == on]

    fetch.calls = calls
    return fetch


# ------------------------------------------------------------------ roundtrip


def test_record_settle_performance_roundtrip():
    start = NOW - timedelta(days=1)
    g1 = _game("nba-1001", start, home="Boston Celtics", away="Miami Heat")
    g2 = _game("nba-1002", start, home="Denver Nuggets", away="Utah Jazz")
    before = start - timedelta(hours=1)  # predictions must be pre-game to be stored
    cards = [
        GameCard(game=g1, prediction=_prediction(g1, 0.7, generated_at=before)),
        GameCard(game=g2, prediction=_prediction(g2, 0.4, generated_at=before)),
    ]
    counts = tracker.record_cards(cards)
    assert counts == {"games": 2, "predictions": 2, "snapshots": 0, "picks": 2}

    # g1 settles by id; g2's result carries a different id — team/date fallback.
    fetch = _fetcher(
        [_final(g1, 110, 102), _final(g2, 120, 96, game_id="nba-espn-alt")]
    )
    summary = asyncio.run(tracker.settle(Sport.NBA, fetch=fetch))
    assert summary == {"settled": 2, "abandoned": 0, "pending": 0, "picks_graded": 2, "user_bets_graded": 0, "parlays_graded": 0}

    # No odds → MODEL-tier picks: graded W/L against the final, units stay NULL.
    conn = db.connect()
    pick_rows = db.rows(
        conn, "SELECT game_id, side, tier, result, units FROM picks ORDER BY game_id"
    )
    conn.close()
    assert [(r["side"], r["tier"], r["result"], r["units"]) for r in pick_rows] == [
        ("home", "MODEL", "W", None),  # 0.7 home, home won
        ("away", "MODEL", "L", None),  # 0.6 away, home won
    ]
    # One fetch per distinct date: the games' UTC date plus the previous day
    # (ESPN scoreboards are keyed to US-Eastern dates).
    assert fetch.calls == [
        (Sport.NBA, start.date() - timedelta(days=1)),
        (Sport.NBA, start.date()),
    ]

    perf = tracker.performance(sport=Sport.NBA)
    assert len(perf["overall"]) == 1
    row = perf["overall"][0]
    assert (row["sport"], row["model"], row["n"]) == ("nba", "nba-elo-v1", 2)
    # picked home@0.7 (home won: correct); picked away@0.6 (home won: wrong)
    assert row["accuracy"] == pytest.approx(0.5)
    assert row["log_loss"] == pytest.approx((-math.log(0.7) - math.log(0.4)) / 2)
    assert row["brier"] == pytest.approx((2 * 0.3**2 + 2 * 0.6**2) / 2)
    assert len(perf["daily"]) == 1
    assert perf["daily"][0]["n"] == 2

    # Settled games are excluded from later settle runs.
    again = asyncio.run(tracker.settle(Sport.NBA, fetch=_fetcher([])))
    assert again == {"settled": 0, "abandoned": 0, "pending": 0, "picks_graded": 0, "user_bets_graded": 0, "parlays_graded": 0}


def test_settle_matches_result_from_previous_days_scoreboard():
    # A 00:30 UTC game lives on the previous US-Eastern date's ESPN
    # scoreboard; settle must find it there by id.
    start = (NOW - timedelta(days=1)).replace(hour=0, minute=30)
    g = _game("nba-1101", start)
    tracker.record_cards([GameCard(game=g, prediction=_prediction(g, 0.6))])

    async def eastern_keyed(sport: Sport, on: date) -> list[Game]:
        return [_final(g, 101, 99)] if on == start.date() - timedelta(days=1) else []

    summary = asyncio.run(tracker.settle(Sport.NBA, fetch=eastern_keyed))
    assert summary == {"settled": 1, "abandoned": 0, "pending": 0, "picks_graded": 1, "user_bets_graded": 0, "parlays_graded": 0}


def test_settle_leaves_recent_unmatched_games_pending():
    g = _game("nba-2001", NOW - timedelta(hours=6))
    tracker.record_cards([GameCard(game=g, prediction=_prediction(g, 0.55))])

    summary = asyncio.run(tracker.settle(Sport.NBA, fetch=_fetcher([])))
    assert summary == {"settled": 0, "abandoned": 0, "pending": 1, "picks_graded": 0, "user_bets_graded": 0, "parlays_graded": 0}

    conn = db.connect()
    row = conn.execute("SELECT settled_at, completed FROM games").fetchone()
    conn.close()
    assert row["settled_at"] is None and row["completed"] == 0


def test_settle_abandons_old_unmatched_games():
    old = _game("nba-3001", NOW - timedelta(days=10))
    tracker.record_cards([GameCard(game=old, prediction=_prediction(old, 0.9))])

    summary = asyncio.run(tracker.settle(Sport.NBA, fetch=_fetcher([])))
    assert summary == {"settled": 0, "abandoned": 1, "pending": 0, "picks_graded": 1, "user_bets_graded": 0, "parlays_graded": 0}

    conn = db.connect()
    row = conn.execute("SELECT settled_at, completed FROM games").fetchone()
    pick = conn.execute("SELECT result, units FROM picks").fetchone()
    conn.close()
    assert row["settled_at"] is not None and row["completed"] == 0
    # The abandoned game's pick is pushed (MODEL tier: units stay NULL).
    assert (pick["result"], pick["units"]) == ("P", None)

    # Abandoned games never count toward performance or CLV.
    assert tracker.performance()["overall"] == []
    assert tracker.clv()["games"] == []


def test_settle_fetch_error_keeps_games_pending():
    old = _game("nba-3002", NOW - timedelta(days=10))
    tracker.record_cards([GameCard(game=old)])

    async def broken(sport, on):
        raise RuntimeError("espn down")

    summary = asyncio.run(tracker.settle(Sport.NBA, fetch=broken))
    assert summary == {"settled": 0, "abandoned": 0, "pending": 1, "picks_graded": 0, "user_bets_graded": 0, "parlays_graded": 0}


# ---------------------------------------------------------------- pre-game


def test_post_kickoff_prediction_is_refused():
    """61% of the live predictions table had been written after kickoff
    (2026-08-17) and scoring it reversed the market-vs-model verdict. The
    store now refuses the write instead of stamping a row it can't score."""
    g = _game("nba-4002", NOW - timedelta(minutes=30))
    counts = tracker.record_cards([GameCard(game=g, prediction=_prediction(g, 0.7))])
    assert counts["predictions"] == 0
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0
    conn.close()


def test_performance_scores_only_pre_game_predictions():
    """Legacy post-kickoff rows still exist in old stores; performance() must
    score each game on its last PRE-GAME prediction, never the one that knew
    the score."""
    g = _game("nba-4003", NOW - timedelta(hours=3))
    conn = db.connect()
    with conn:
        db.upsert_game(
            conn, g.id, g.sport.value, g.league, g.start_time, g.home, g.away,
            100, 90, True,
        )
        conn.execute("UPDATE games SET settled_at = ? WHERE game_id = ?", (db.iso(NOW), g.id))
        # A post-kickoff row that "knew" the result, and an honest pre-game one.
        db.upsert_prediction(conn, g.id, "nba", "nba-elo-v1", NOW - timedelta(hours=1), 0.99, 0.01)
        db.upsert_prediction(conn, g.id, "nba", "nba-elo-v1", NOW - timedelta(hours=5), 0.40, 0.60)
    conn.close()
    [row] = tracker.performance()["overall"]
    assert row["n"] == 1
    assert row["accuracy"] == 0.0  # the 0.40 pre-game call lost; 0.99 is ignored


# --------------------------------------------------------------------- dedupe


def test_prediction_dedupe_within_hour():
    g = _game("nba-4001", NOW + timedelta(hours=5))

    def card(minutes: float, prob: float) -> GameCard:
        return GameCard(
            game=g,
            prediction=_prediction(g, prob, generated_at=NOW + timedelta(minutes=minutes)),
        )

    tracker.record_cards([card(0, 0.60)])
    tracker.record_cards([card(10, 0.62)])  # within the hour: update in place
    tracker.record_cards([card(130, 0.64)])  # new hour: new row

    conn = db.connect()
    rows = conn.execute(
        "SELECT home_win_prob FROM predictions ORDER BY generated_at"
    ).fetchall()
    n_games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    conn.close()
    assert n_games == 1
    assert [r["home_win_prob"] for r in rows] == [0.62, 0.64]


# ------------------------------------------------------------------------ CLV


def test_clv_open_vs_pregame_close():
    start = NOW - timedelta(days=1)
    g = _game("nba-5001", start)
    t_open = start - timedelta(hours=30)
    t_close = start - timedelta(hours=1)
    t_live = start + timedelta(hours=1)  # in-play: must be ignored

    def snap(when: datetime, home_ml: int, away_ml: int) -> MoneylineOdds:
        return MoneylineOdds(
            book="pinnacle", fetched_at=when, home_ml=home_ml, away_ml=away_ml
        )

    tracker.record_cards(
        [
            GameCard(
                game=g,
                prediction=_prediction(g, 0.65, generated_at=t_close),  # pick: home
                odds=[snap(t_open, -110, -110), snap(t_close, -150, 130),
                      snap(t_live, -400, 320)],
            )
        ]
    )
    asyncio.run(tracker.settle(Sport.NBA, fetch=_fetcher([_final(g, 100, 90)])))

    report = tracker.clv(sport=Sport.NBA)
    assert report["overall"]["n"] == 1
    p_open = remove_vig(-110, -110)[0]
    p_close = remove_vig(-150, 130)[0]
    expected_pts = (p_close - p_open) * 100.0
    expected_bps = (p_close / p_open - 1.0) * 10_000.0
    assert report["overall"]["avg_clv_prob_pts"] == pytest.approx(expected_pts)
    assert report["overall"]["avg_clv_bps"] == pytest.approx(expected_bps)
    (row,) = report["games"]
    assert row["side"] == "home" and row["books"] == 1


def test_clv_requires_two_pregame_snapshots():
    start = NOW - timedelta(days=1)
    g = _game("nba-5002", start)
    tracker.record_cards(
        [
            GameCard(
                game=g,
                prediction=_prediction(g, 0.6),
                odds=[
                    MoneylineOdds(
                        book="dk",
                        fetched_at=start - timedelta(hours=2),
                        home_ml=-120,
                        away_ml=100,
                    )
                ],
            )
        ]
    )
    asyncio.run(tracker.settle(Sport.NBA, fetch=_fetcher([_final(g, 100, 90)])))
    assert tracker.clv()["games"] == []


# -------------------------------------------------------------- line movement


def test_line_movement_orders_snapshots_per_book():
    g = _game("nba-6001", NOW + timedelta(hours=8))
    times = [NOW - timedelta(hours=h) for h in (1, 5, 3)]  # deliberately unsorted
    odds = [
        MoneylineOdds(book="dk", fetched_at=times[0], home_ml=-130, away_ml=110),
        MoneylineOdds(book="dk", fetched_at=times[1], home_ml=-110, away_ml=-110),
        MoneylineOdds(book="dk", fetched_at=times[2], home_ml=-120, away_ml=100),
        MoneylineOdds(book="fanduel", fetched_at=times[1], home_ml=-115, away_ml=-105),
    ]
    tracker.record_cards([GameCard(game=g, odds=odds)])

    movement = tracker.line_movement(g.id)
    assert movement["game_id"] == g.id
    assert set(movement["books"]) == {"dk", "fanduel"}
    dk = movement["books"]["dk"]
    assert [s["fetched_at"] for s in dk] == sorted(s["fetched_at"] for s in dk)
    assert [s["home_ml"] for s in dk] == [-110, -120, -130]  # oldest to newest
    assert len(movement["books"]["fanduel"]) == 1


def test_snapshot_exact_duplicates_ignored():
    g = _game("nba-6002", NOW + timedelta(hours=8))
    ml = MoneylineOdds(book="dk", fetched_at=NOW, home_ml=-130, away_ml=110)
    tracker.record_cards([GameCard(game=g, odds=[ml])])
    counts = tracker.record_cards([GameCard(game=g, odds=[ml])])
    assert counts["snapshots"] == 0
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
    conn.close()
    assert n == 1


def test_snapshots_dedupe_within_the_time_bucket():
    """The board endpoint records on every read and the renderer polls all nine
    boards every 60s, so an append-per-poll grew 1,300+ rows for a single game.
    Captures inside SNAPSHOT_MIN_GAP_MINUTES refresh the bucket in place; the
    freshest quote wins, and the series keeps its shape."""
    g = _game("nba-6003", NOW + timedelta(hours=8))
    # Anchor to a bucket boundary so the three polls below can't straddle one
    # (buckets are fixed wall-clock windows, so a raw NOW would be flaky).
    gap = db.SNAPSHOT_MIN_GAP_MINUTES * 60.0
    base = datetime.fromtimestamp(
        math.floor(NOW.timestamp() / gap) * gap, tz=timezone.utc
    )
    minute = timedelta(minutes=1)
    for i, price in enumerate((-130, -125, -120)):  # three polls, 1 min apart
        tracker.record_cards([GameCard(game=g, odds=[
            MoneylineOdds(book="dk", fetched_at=base + i * minute,
                          home_ml=price, away_ml=110),
        ])])

    series = tracker.line_movement(g.id)["books"]["dk"]
    assert len(series) == 1  # one bucket, not three rows
    assert series[0]["home_ml"] == -120  # freshest quote in the bucket

    # The next bucket always opens a new row — a relative window would instead
    # keep sliding the one row forward and never accrue a series at all.
    beyond = timedelta(minutes=db.SNAPSHOT_MIN_GAP_MINUTES + 1)
    counts = tracker.record_cards([GameCard(game=g, odds=[
        MoneylineOdds(book="dk", fetched_at=base + beyond,
                      home_ml=-115, away_ml=105),
    ])])
    assert counts["snapshots"] == 1
    assert [s["home_ml"] for s in tracker.line_movement(g.id)["books"]["dk"]] == [
        -120, -115,
    ]


def test_snapshot_buckets_are_per_book():
    """Two books captured in the same instant are independent series."""
    g = _game("nba-6004", NOW + timedelta(hours=8))
    tracker.record_cards([GameCard(game=g, odds=[
        MoneylineOdds(book="dk", fetched_at=NOW, home_ml=-130, away_ml=110),
        MoneylineOdds(book="fanduel", fetched_at=NOW, home_ml=-125, away_ml=105),
    ])])
    books = tracker.line_movement(g.id)["books"]
    assert set(books) == {"dk", "fanduel"}
    assert len(books["dk"]) == 1 and len(books["fanduel"]) == 1


def test_clv_by_tier_scores_the_pick_side():
    """Per-tier CLV scores each pick's OWN side vs the close. A home pick whose
    price shortens (open -130 -> close -170) beat the close -> positive CLV in
    the STRONG bucket."""
    start = NOW - timedelta(hours=5)
    conn = db.connect()
    with conn:
        db.upsert_game(conn, "nba-ct1", "nba", "", start, "Home", "Away", 110, 100, True)
        db.mark_settled(conn, "nba-ct1", NOW, 110, 100, True)
        db.upsert_pick(conn, "nba-ct1", "nba", "m", "home", "Home", "dk",
                       -150, 0.06, 0.05, "STRONG", start, start - timedelta(hours=2))
        db.insert_snapshot(conn, "nba-ct1", "nba", "dk",
                           start - timedelta(hours=3), -130, 110)
        db.insert_snapshot(conn, "nba-ct1", "nba", "dk",
                           start - timedelta(minutes=30), -170, 150)
    conn.close()

    r = tracker.clv_by_tier()
    strong = r["by_tier"]["STRONG"]
    assert strong["n"] == 1
    assert strong["avg_clv_bps"] > 0  # home shortened -> we beat the close
    # A single sample can't be significant.
    assert strong["significant"] is False
    assert r["by_tier"]["LEAN"]["n"] == 0 and r["by_tier"]["MODEL"]["n"] == 0

"""Schedule-shaped trackers: rest days from the store, MLB umpire/lineup
first-seen capture, and the scoring that keeps them honest."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kryptscout.core import context as context_mod
from kryptscout.core.schemas import Game, GameCard, MoneylineOdds, Prediction, Sport
from kryptscout.datasources import mlb_context
from kryptscout.store import db, tracker

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")


def _game(game_id, home, away, start, sport=Sport.SOCCER, completed=False, hs=None, as_=None):
    return Game(id=game_id, sport=sport, league="EPL", start_time=start, home=home, away=away,
                completed=completed, home_score=hs, away_score=as_)


def _card(game, odds=None):
    pred = Prediction(game_id=game.id, sport=game.sport, model="m", generated_at=NOW - timedelta(days=30),
                      home_win_prob=0.4, away_win_prob=0.3, draw_prob=0.3)
    return GameCard(game=game, prediction=pred, odds=odds or [])


# ------------------------------------------------------------------ rest days


def test_rest_days_come_from_the_store_and_unknown_when_no_recent_game():
    conn = db.connect()
    with conn:
        db.upsert_game(conn, "s-0", "soccer", "EPL", NOW - timedelta(days=3), "Arsenal", "Spurs", 1, 0, True)
        db.upsert_game(conn, "s-old", "soccer", "EPL", NOW - timedelta(days=40), "Chelsea", "Villa", 1, 0, True)
    assert context_mod.rest_days(conn, "soccer", "Arsenal", NOW) == pytest.approx(3.0)
    assert context_mod.rest_days(conn, "soccer", "Spurs", NOW) == pytest.approx(3.0)
    assert context_mod.rest_days(conn, "soccer", "Chelsea", NOW) is None  # 40 days: a break, not rest
    conn.close()


def test_record_cards_writes_rest_for_the_slate():
    # Arsenal played 3 days ago, Chelsea 7 days ago; both meet tomorrow.
    conn = db.connect()
    with conn:
        db.upsert_game(conn, "s-1", "soccer", "EPL", NOW - timedelta(days=3), "Arsenal", "Spurs", 1, 0, True)
        db.upsert_game(conn, "s-2", "soccer", "EPL", NOW - timedelta(days=7), "Chelsea", "Villa", 1, 0, True)
    conn.close()
    game = _game("s-3", "Arsenal", "Chelsea", NOW + timedelta(days=1))
    counts = tracker.record_cards([_card(game)])
    assert counts["context"] == 1
    conn = db.connect()
    [row] = db.rows(conn, "SELECT * FROM game_context")
    conn.close()
    assert (row["game_id"], row["rest_home_days"], row["rest_away_days"]) == ("s-3", 4.0, 8.0)
    assert context_mod.short_rest_side(row["rest_home_days"], row["rest_away_days"]) is None
    assert context_mod.short_rest_side(3.0, 7.0) == "home"
    assert context_mod.short_rest_side(6.0, 2.5) == "away"
    assert context_mod.short_rest_side(3.0, 3.0) is None  # both short: no asymmetry


# ------------------------------------------------------------- MLB context


def _schedule_payload(posted=True, ump=True):
    game = {
        "officialDate": "2026-09-02",
        "teams": {"home": {"team": {"name": "Cincinnati Reds"}}, "away": {"team": {"name": "San Diego Padres"}}},
        "officials": [
            {"official": {"id": 665297, "fullName": "Tom Hanahan"}, "officialType": "First Base"},
            {"official": {"id": 658082, "fullName": "Jacob Metz"}, "officialType": "Home Plate"},
        ] if ump else [],
        "lineups": {"homePlayers": [{"id": 1}], "awayPlayers": [{"id": 2}]} if posted else {"homePlayers": []},
    }
    return {"dates": [{"date": "2026-09-02", "games": [game]}]}


def test_parse_schedule_reads_home_plate_umpire_and_lineup_state():
    slate = mlb_context.parse_schedule(_schedule_payload())
    ctx = slate[("2026-09-02", "Cincinnati Reds")]
    assert (ctx.umpire_id, ctx.umpire_name, ctx.lineups_posted) == (658082, "Jacob Metz", True)
    ctx2 = mlb_context.parse_schedule(_schedule_payload(posted=False, ump=False))[("2026-09-02", "Cincinnati Reds")]
    assert (ctx2.umpire_id, ctx2.lineups_posted) == (None, False)


def test_capture_stamps_first_seen_once(monkeypatch):
    payloads = iter([_schedule_payload(posted=False), _schedule_payload(posted=True), _schedule_payload(posted=True)])

    async def fake_fetch(dates):
        return mlb_context.parse_schedule(next(payloads))

    monkeypatch.setattr(mlb_context, "fetch_slate_context", fake_fetch)
    game = _game("mlb-1", "Cincinnati Reds", "San Diego Padres", datetime(2026, 9, 2, 16, 40, tzinfo=timezone.utc), sport=Sport.MLB)
    cards = [GameCard(game=game)]
    assert asyncio.run(mlb_context.capture(cards)) == 1  # umpire known, lineups not yet
    conn = db.connect()
    [row] = db.rows(conn, "SELECT * FROM game_context")
    first_ump = row["umpire_seen_at"]
    assert row["umpire_name"] == "Jacob Metz" and first_ump and row["lineups_seen_at"] is None
    assert asyncio.run(mlb_context.capture(cards)) == 1  # lineups now posted
    [row] = db.rows(conn, "SELECT * FROM game_context")
    first_lineups = row["lineups_seen_at"]
    assert first_lineups is not None and row["umpire_seen_at"] == first_ump
    assert asyncio.run(mlb_context.capture(cards)) == 1  # third pass changes no stamps
    [row] = db.rows(conn, "SELECT * FROM game_context")
    conn.close()
    assert row["lineups_seen_at"] == first_lineups and row["umpire_seen_at"] == first_ump


# ----------------------------------------------------------------- scoring


def _seed_soccer(conn, game_id, rest_home, rest_away, hs, as_, ref=(150, 240, 200)):
    start = NOW - timedelta(days=2)
    db.upsert_game(conn, game_id, "soccer", "EPL", start, "H" + game_id, "A" + game_id, hs, as_, True)
    db.upsert_game_context(conn, game_id, "soccer", NOW - timedelta(days=3), rest_home=rest_home, rest_away=rest_away)
    db.insert_snapshot(conn, game_id, "soccer", "polymarket", start - timedelta(hours=2), ref[0], ref[2], ref[1])


def test_soccer_short_rest_tracker_scores_the_short_rest_side():
    conn = db.connect()
    with conn:
        _seed_soccer(conn, "a", 3.0, 7.0, 2, 0)   # home short rest, home wins at +150
        _seed_soccer(conn, "b", 7.0, 3.0, 0, 1)   # away short rest, away wins at +200
        _seed_soccer(conn, "c", 3.0, 7.0, 0, 0)   # home short rest, draw: bet loses
        _seed_soccer(conn, "d", 5.0, 5.0, 1, 0)   # no asymmetry: excluded
    conn.close()
    out = tracker.context_trackers()["soccer_rest"]
    assert out["n"] == 3
    assert out["win_rate"] == pytest.approx(2 / 3)
    assert out["roi_pct"] == pytest.approx((1.5 + 2.0 - 1.0) / 3 * 100)
    assert out["residual_pts"] is not None and out["significant"] is False


def test_mlb_umpire_tracker_uses_the_last_pregame_total():
    conn = db.connect()
    with conn:
        start = NOW - timedelta(days=1)
        for gid, ump, total, line in (("m1", "Jacob Metz", 9, 8.5), ("m2", "Jacob Metz", 11, 8.5), ("m3", "Other Ump", 5, 8.5)):
            db.upsert_game(conn, gid, "mlb", "", start, "H" + gid, "A" + gid, total - 3, 3, True)
            db.upsert_game_context(conn, gid, "mlb", start - timedelta(hours=5), umpire_id=1 if ump == "Jacob Metz" else 2, umpire_name=ump, lineups_posted=True)
            db.insert_snapshot(conn, gid, "mlb", "draftkings", start - timedelta(hours=1), -110, -110, None, line, -110, -110)
    conn.close()
    out = tracker.context_trackers(min_umpire_n=1)["mlb_umpires"]
    assert out["n"] == 3
    assert tracker.context_trackers()["mlb_umpires"]["by_umpire"] == []  # n < 10 stays unlisted
    metz = next(u for u in out["by_umpire"] if u["umpire"] == "Jacob Metz")
    assert metz["n"] == 2 and metz["residual_runs"] == pytest.approx(1.5)
    assert out["lineups_lead_hours_median"] is not None

"""store/cardrecord — the record of the posted daily card.

Everything runs against a tmp_path SQLite database. What these pin: a card is
graded from final scores, on the side and price that were posted; a game is
recorded once; unfinished games are pending, not losses; and "last" is the
most recent graded card day before today.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from kryptscout.api import main
from kryptscout.store import cardrecord, db

NOV14 = date(2026, 11, 14)
NOV15 = date(2026, 11, 15)
NOV16 = date(2026, 11, 16)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")


def _final(game_id: str, home_score: int | None, away_score: int | None) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO games (game_id, sport, league, start_time, home, away,"
            " home_score, away_score, completed) VALUES (?, 'nba', '', ?, 'Home', 'Away', ?, ?, ?)",
            (game_id, db.iso(datetime(2026, 11, 14, 23, tzinfo=timezone.utc)),
             home_score, away_score, int(home_score is not None)),
        )
        conn.commit()
    finally:
        conn.close()


def _pick(game_id: str, side: str = "home", prob: float = 0.7, cost: float | None = 0.72) -> dict:
    row = {"game_id": game_id, "sport": "nba", "side": side, "team": f"Team {game_id}",
           "tier": "MODEL", "prob": prob, "exchanges": {}, "best": None}
    if cost is not None:
        row["exchanges"] = {"kalshi": {"price": cost - 0.01, "cost": cost}}
        row["best"] = "kalshi"
    return row


def test_an_empty_record_has_nothing_to_say():
    assert cardrecord.summary(NOV15) == {
        "overall": {"wins": 0, "losses": 0, "pushes": 0, "pending": 0, "units": 0.0, "since": None},
        "last": None,
    }


def test_a_card_is_graded_from_the_final_scores():
    cardrecord.save(NOV14, [_pick("w"), _pick("l"), _pick("away-w", side="away"),
                            _pick("tie"), _pick("live")])
    _final("w", 110, 100)
    _final("l", 90, 100)
    _final("away-w", 90, 100)
    _final("tie", 100, 100)
    _final("live", None, None)
    out = cardrecord.summary(NOV15)
    overall = out["overall"]
    assert (overall["wins"], overall["losses"], overall["pushes"], overall["pending"]) == (2, 1, 1, 1)
    # Two wins at a cost of 72c pay 1/0.72 - 1 each; the loss costs the unit.
    assert overall["units"] == pytest.approx(round(2 * (1 / 0.72 - 1) - 1, 2))
    assert overall["since"] == "2026-11-14"
    assert out["last"]["date"] == "2026-11-14" and out["last"]["wins"] == 2


def test_a_soccer_draw_is_a_loss_not_a_push():
    cardrecord.save(NOV14, [dict(_pick("draw"), sport="soccer")])
    _final("draw", 1, 1)
    overall = cardrecord.summary(NOV15)["overall"]
    assert (overall["losses"], overall["pushes"], overall["units"]) == (1, 0, -1.0)


def test_a_pick_no_exchange_listed_is_staked_at_its_fair_price():
    cardrecord.save(NOV14, [_pick("w", prob=0.8, cost=None)])
    _final("w", 110, 100)
    assert cardrecord.summary(NOV15)["overall"]["units"] == pytest.approx(0.25)


def test_a_game_is_recorded_once():
    assert cardrecord.save(NOV14, [_pick("g", side="home", cost=0.72)]) == 1
    # A second send the same day: the new game is added, the old row is kept.
    assert cardrecord.save(NOV14, [_pick("g", side="away", cost=0.30), _pick("h")]) == 1
    _final("g", 110, 100)
    overall = cardrecord.summary(NOV15)["overall"]
    assert (overall["wins"], overall["pending"]) == (1, 1)


def test_rows_that_cannot_be_graded_are_not_recorded():
    saved = cardrecord.save(NOV14, [_pick("draw", side="draw"), {"side": "home"}, "junk", _pick("ok")])
    assert saved == 1


def test_last_is_the_newest_graded_day_before_today():
    cardrecord.save(NOV14, [_pick("a")])
    cardrecord.save(NOV15, [_pick("b")])
    _final("a", 110, 100)
    _final("b", None, None)                       # the 15th's game is not final
    assert cardrecord.summary(NOV16)["last"]["date"] == "2026-11-14"
    _final("b", 90, 100)
    out = cardrecord.summary(NOV16)
    assert out["last"]["date"] == "2026-11-15" and out["last"]["losses"] == 1
    # Today's own card is never "last", even once its games finish.
    assert cardrecord.summary(NOV15)["last"]["date"] == "2026-11-14"


def test_the_endpoints_record_and_report(monkeypatch):
    monkeypatch.setattr(main.app.router, "on_startup", [])
    with TestClient(main.app) as client:
        res = client.post("/api/picks/card/posted",
                          json={"date": "2026-11-14", "picks": [_pick("w"), _pick("l")]})
        assert res.status_code == 200 and res.json() == {"saved": 2}
        _final("w", 110, 100)
        _final("l", 90, 100)
        body = client.get("/api/picks/card/record", params={"today": "2026-11-15"}).json()
        assert (body["overall"]["wins"], body["overall"]["losses"]) == (1, 1)
        assert body["last"]["date"] == "2026-11-14"
        assert client.post("/api/picks/card/posted", json={"picks": []}).status_code == 422

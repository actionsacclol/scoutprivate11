"""User-bet tracking tests — the /api/mybets endpoints + tracker/db behind them.

The user's OWN logged bets, distinct from the automatic model-pick record:
POST validation and dedupe, the ungraded-only DELETE guard, settlement
grading at the bet's own stake (via an injected results fetcher — no
network), and the stake-weighted P&L aggregation. Everything runs against a
tmp_path SQLite database (db.DB_PATH is monkeypatched) with app startup
disabled, matching test_api.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from kryptscout.api import main
from kryptscout.core.schemas import Game, Sport
from kryptscout.store import db, tracker

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")


@pytest.fixture()
def client(monkeypatch):
    # Skip _load_engines (would import every real sport module); the mybets
    # endpoints only touch the store.
    monkeypatch.setattr(main.app.router, "on_startup", [])
    with TestClient(main.app) as test_client:
        yield test_client


def _game(
    game_id: str,
    start: datetime,
    home: str = "Boston Celtics",
    away: str = "Miami Heat",
    sport: Sport = Sport.NBA,
) -> Game:
    return Game(
        id=game_id, sport=sport, league="", start_time=start, home=home, away=away
    )


def _record(game: Game) -> Game:
    conn = db.connect()
    try:
        with conn:
            db.upsert_game(
                conn, game.id, game.sport.value, game.league, game.start_time,
                game.home, game.away,
            )
    finally:
        conn.close()
    return game


def _final(game: Game, home_score: int, away_score: int) -> Game:
    return game.model_copy(
        update={
            "home_score": home_score,
            "away_score": away_score,
            "completed": True,
        }
    )


def _fetcher(results: list[Game]):
    async def fetch(sport: Sport, on: date) -> list[Game]:
        return [r for r in results if r.start_time.date() == on]

    return fetch


def _settle(sport: Sport, results: list[Game]) -> dict:
    return asyncio.run(tracker.settle(sport, fetch=_fetcher(results)))


# ----------------------------------------------------------------------- POST


def test_post_creates_bet_resolving_sport_and_team(client):
    _record(_game("nba-1001", NOW + timedelta(hours=6)))
    resp = client.post(
        "/api/mybets",
        json={"game_id": "nba-1001", "side": "home", "price": -120,
              "book": "dk", "note": "fade the road team"},
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["id"] == 1
    assert (row["sport"], row["side"], row["team"]) == ("nba", "home", "Boston Celtics")
    assert (row["price"], row["book"], row["stake_units"]) == (-120, "dk", 1.0)
    assert row["note"] == "fade the road team"
    assert row["duplicate"] is False
    assert row["result"] is None and row["units"] is None and row["graded_at"] is None
    assert row["created_at"]  # ISO stamp set server-side

    away = client.post(
        "/api/mybets", json={"game_id": "nba-1001", "side": "away", "price": 105}
    ).json()
    assert away["team"] == "Miami Heat" and away["book"] is None


def test_post_draw_side_resolves_team_draw(client):
    _record(
        _game("soccer-2001", NOW + timedelta(hours=6),
              home="Arsenal", away="Chelsea", sport=Sport.SOCCER)
    )
    row = client.post(
        "/api/mybets",
        json={"game_id": "soccer-2001", "side": "draw", "price": 230},
    ).json()
    assert (row["sport"], row["team"]) == ("soccer", "DRAW")


def test_post_clamps_stake_units(client):
    _record(_game("nba-1002", NOW + timedelta(hours=6)))
    low = client.post(
        "/api/mybets",
        json={"game_id": "nba-1002", "side": "home", "price": 110,
              "stake_units": 0.01},
    ).json()
    assert low["stake_units"] == pytest.approx(0.1)
    high = client.post(
        "/api/mybets",
        json={"game_id": "nba-1002", "side": "away", "price": 110,
              "stake_units": 5000},
    ).json()
    assert high["stake_units"] == pytest.approx(100.0)


def test_post_unknown_game_is_404(client):
    resp = client.post(
        "/api/mybets", json={"game_id": "nba-9999", "side": "home", "price": -110}
    )
    assert resp.status_code == 404


def test_post_validation_is_422(client):
    _record(_game("nba-1003", NOW + timedelta(hours=6)))
    bad = [
        {"game_id": "nba-1003", "side": "over", "price": -110},  # bad side
        {"game_id": "nba-1003", "side": "home", "price": 50},  # not American
        {"game_id": "nba-1003", "side": "home", "price": 0},
        {"game_id": "nba-1003", "side": "home"},  # missing price
        {"side": "home", "price": -110},  # missing game_id
    ]
    for body in bad:
        assert client.post("/api/mybets", json=body).status_code == 422


# --------------------------------------------------------------------- dedupe


def test_post_exact_ungraded_duplicate_returns_existing_row(client):
    _record(_game("nba-1101", NOW + timedelta(hours=6)))
    body = {"game_id": "nba-1101", "side": "home", "price": -120, "book": "dk"}
    first = client.post("/api/mybets", json=body).json()
    # Same key even with different stake/note: treated as a double-submit.
    again = client.post(
        "/api/mybets", json={**body, "stake_units": 3, "note": "resend"}
    ).json()
    assert again["duplicate"] is True
    assert again["id"] == first["id"]
    assert again["stake_units"] == pytest.approx(1.0)  # original row untouched

    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) FROM user_bets").fetchone()[0]
    conn.close()
    assert n == 1


def test_post_different_price_book_or_side_inserts_new_row(client):
    _record(_game("nba-1102", NOW + timedelta(hours=6)))
    base = {"game_id": "nba-1102", "side": "home", "price": -120, "book": "dk"}
    ids = {client.post("/api/mybets", json=body).json()["id"] for body in (
        base,
        {**base, "price": -125},  # line moved: genuine second bet
        {**base, "book": "fanduel"},
        {**base, "book": None},  # NULL book is its own dedupe key
        {**base, "side": "away", "price": 105},
    )}
    assert len(ids) == 5


def test_post_after_grading_inserts_fresh_row(client):
    g = _record(_game("nba-1103", NOW - timedelta(days=1)))
    body = {"game_id": "nba-1103", "side": "home", "price": -120, "book": "dk"}
    first = client.post("/api/mybets", json=body).json()
    _settle(Sport.NBA, [_final(g, 110, 102)])
    # The graded row no longer guards: an identical POST is a new bet.
    again = client.post("/api/mybets", json=body).json()
    assert again["duplicate"] is False and again["id"] != first["id"]


# --------------------------------------------------------------------- DELETE


def test_delete_ungraded_bet(client):
    _record(_game("nba-1201", NOW + timedelta(hours=6)))
    bet = client.post(
        "/api/mybets", json={"game_id": "nba-1201", "side": "home", "price": -110}
    ).json()
    resp = client.delete(f"/api/mybets/{bet['id']}")
    assert resp.status_code == 200 and resp.json() == {"deleted": bet["id"]}
    assert client.get("/api/mybets").json()["bets"] == []


def test_delete_graded_bet_is_409(client):
    g = _record(_game("nba-1202", NOW - timedelta(days=1)))
    bet = client.post(
        "/api/mybets", json={"game_id": "nba-1202", "side": "home", "price": -110}
    ).json()
    _settle(Sport.NBA, [_final(g, 110, 102)])
    assert client.delete(f"/api/mybets/{bet['id']}").status_code == 409
    # Graded history stays put.
    assert len(client.get("/api/mybets").json()["bets"]) == 1


def test_delete_unknown_bet_is_404(client):
    assert client.delete("/api/mybets/12345").status_code == 404


# -------------------------------------------------------------------- grading


def test_settle_grades_user_bets_at_their_stake():
    start = NOW - timedelta(days=1)
    g1 = _record(_game("nba-2001", start))
    g2 = _record(_game("nba-2002", start, home="Denver Nuggets", away="Utah Jazz"))
    tracker.log_bet("nba-2001", "home", 150, stake_units=2.0)  # home wins: W
    tracker.log_bet("nba-2001", "away", 130, stake_units=1.0)  # L
    tracker.log_bet("nba-2002", "home", -120, stake_units=2.0)  # home wins: W

    summary = _settle(Sport.NBA, [_final(g1, 110, 102), _final(g2, 99, 90)])
    assert summary["settled"] == 2
    assert summary["user_bets_graded"] == 3
    assert summary["picks_graded"] == 0  # no model picks recorded here

    bets = {(b["game_id"], b["side"]): b for b in tracker.my_bets()["bets"]}
    w_plus = bets[("nba-2001", "home")]
    assert w_plus["result"] == "W"
    assert w_plus["units"] == pytest.approx(2.0 * 150 / 100)  # +3.0
    assert w_plus["graded_at"] is not None
    loss = bets[("nba-2001", "away")]
    assert (loss["result"], loss["units"]) == ("L", pytest.approx(-1.0))
    w_minus = bets[("nba-2002", "home")]
    assert w_minus["result"] == "W"
    assert w_minus["units"] == pytest.approx(2.0 * 100 / 120)

    # Already-graded bets don't regrade on the next settle run.
    again = _settle(Sport.NBA, [])
    assert again["user_bets_graded"] == 0


def test_soccer_draw_grades_draw_bets_win_side_bets_lose():
    g = _record(
        _game("soccer-2101", NOW - timedelta(days=1),
              home="Arsenal", away="Chelsea", sport=Sport.SOCCER)
    )
    tracker.log_bet("soccer-2101", "draw", 230, stake_units=1.0)
    tracker.log_bet("soccer-2101", "home", -110, stake_units=2.0)
    _settle(Sport.SOCCER, [_final(g, 1, 1)])

    bets = {b["side"]: b for b in tracker.my_bets()["bets"]}
    assert (bets["draw"]["result"], bets["draw"]["units"]) == ("W", pytest.approx(2.3))
    # Three-way market: the draw beats a home bet outright — no push.
    assert (bets["home"]["result"], bets["home"]["units"]) == ("L", pytest.approx(-2.0))


def test_non_soccer_tie_pushes():
    g = _record(_game("nfl-2201", NOW - timedelta(days=1), sport=Sport.NFL))
    tracker.log_bet("nfl-2201", "home", -110, stake_units=3.0)
    _settle(Sport.NFL, [_final(g, 20, 20)])
    (bet,) = tracker.my_bets()["bets"]
    assert (bet["result"], bet["units"]) == ("P", 0.0)


def test_abandoned_game_pushes():
    _record(_game("nba-2301", NOW - timedelta(days=10)))  # older than lookback
    tracker.log_bet("nba-2301", "home", 140, stake_units=2.0)
    summary = _settle(Sport.NBA, [])
    assert summary["abandoned"] == 1 and summary["user_bets_graded"] == 1
    (bet,) = tracker.my_bets()["bets"]
    assert (bet["result"], bet["units"]) == ("P", 0.0)


# ------------------------------------------------------------------------ GET


def _seed_pnl_history() -> None:
    """Two days of graded bets + one pending: +3.0u, -1.0u, a push, a pending.

    day1 (2 days ago): home +150 x2u, home wins  -> W +3.0
    day2 (1 day ago):  away -120 x1u, home wins  -> L -1.0
    day2 (1 day ago):  home x1u, tie (NFL)       -> P  0.0
    future:            home -110 x1u              -> ungraded
    """
    day1, day2 = NOW - timedelta(days=2), NOW - timedelta(days=1)
    a = _record(_game("nba-3001", day1))
    b = _record(_game("nba-3002", day2, home="Denver Nuggets", away="Utah Jazz"))
    c = _record(_game("nfl-3003", day2, sport=Sport.NFL))
    _record(_game("nba-3004", NOW + timedelta(days=1)))
    tracker.log_bet("nba-3001", "home", 150, stake_units=2.0)
    tracker.log_bet("nba-3002", "away", -120, stake_units=1.0)
    tracker.log_bet("nfl-3003", "home", -110, stake_units=1.0)
    tracker.log_bet("nba-3004", "home", -110, stake_units=1.0)
    _settle(Sport.NBA, [_final(a, 110, 102), _final(b, 99, 90)])
    _settle(Sport.NFL, [_final(c, 20, 20)])


def test_get_mybets_pnl_aggregation(client):
    _seed_pnl_history()
    body = client.get("/api/mybets").json()

    pnl = body["pnl"]
    assert pnl["n"] == 4 and pnl["graded"] == 3
    assert pnl["units"] == pytest.approx(2.0)  # +3.0 - 1.0 + 0.0
    # Stake-weighted: 2.0 units on 4.0 staked (2 + 1 + 1) = 50%.
    assert pnl["roi_pct"] == pytest.approx(50.0)
    assert pnl["record"] == "1-1-1"

    day1 = (NOW - timedelta(days=2)).date().isoformat()
    day2 = (NOW - timedelta(days=1)).date().isoformat()
    assert pnl["daily"] == [
        {"date": day1, "units": pytest.approx(3.0), "cum_units": pytest.approx(3.0)},
        {"date": day2, "units": pytest.approx(-1.0), "cum_units": pytest.approx(2.0)},
    ]

    # Newest bets first (insertion order reversed here).
    assert [b["id"] for b in body["bets"]] == [4, 3, 2, 1]
    # Rows join the games table: matchup + kickoff + final.
    graded_win = next(b for b in body["bets"] if b["game_id"] == "nba-3001")
    assert (graded_win["home"], graded_win["away"]) == ("Boston Celtics", "Miami Heat")
    assert graded_win["kickoff"].startswith(day1)
    assert graded_win["completed"] == 1
    assert (graded_win["home_score"], graded_win["away_score"]) == (110, 102)
    pending = next(b for b in body["bets"] if b["game_id"] == "nba-3004")
    assert pending["result"] is None and pending["completed"] == 0


def test_get_mybets_since_filters_by_game_start_date(client):
    _seed_pnl_history()
    day2 = (NOW - timedelta(days=1)).date().isoformat()
    pnl = client.get("/api/mybets", params={"since": day2}).json()["pnl"]
    assert pnl["n"] == 3  # day1's winner drops; the pending future bet stays
    assert pnl["graded"] == 2
    assert pnl["units"] == pytest.approx(-1.0)
    assert pnl["roi_pct"] == pytest.approx(-50.0)  # -1.0 on 2.0 staked
    assert pnl["record"] == "0-1-1"
    assert [d["date"] for d in pnl["daily"]] == [day2]


def test_get_mybets_empty_store_is_none_safe(client):
    body = client.get("/api/mybets").json()
    assert body["bets"] == []
    assert body["parlays"] == []
    assert body["pnl"] == {
        "n": 0, "graded": 0, "units": 0.0, "roi_pct": None,
        "record": "0-0-0", "daily": [],
    }


# -------------------------------------------------------------------- parlays


def _post_parlay(client, legs: list[dict], **extra) -> object:
    return client.post("/api/mybets/parlay", json={"legs": legs, **extra})


def test_post_parlay_resolves_legs_and_combined_price(client):
    _record(_game("nba-4001", NOW + timedelta(hours=6)))
    _record(
        _game("nhl-4002", NOW + timedelta(hours=7),
              home="Boston Bruins", away="Toronto Maple Leafs", sport=Sport.NHL)
    )
    resp = _post_parlay(
        client,
        [
            {"game_id": "nba-4001", "side": "home", "price": 100, "book": "dk"},
            {"game_id": "nhl-4002", "side": "away", "price": -110},
        ],
        stake_units=2.0,
        note="cross-sport two-legger",
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["duplicate"] is False
    assert row["stake_units"] == pytest.approx(2.0)
    assert row["note"] == "cross-sport two-legger"
    # 2.0 x 1.9091 = 3.8182 decimal -> +282 American.
    assert row["price"] == 282
    assert row["result"] is None and row["units"] is None
    # Legs resolve team/sport server-side and order by kickoff.
    assert [(leg["game_id"], leg["sport"], leg["team"], leg["book"])
            for leg in row["legs"]] == [
        ("nba-4001", "nba", "Boston Celtics", "dk"),
        ("nhl-4002", "nhl", "Toronto Maple Leafs", None),
    ]
    assert all(leg["result"] is None for leg in row["legs"])


def test_post_parlay_validation(client):
    _record(_game("nba-4010", NOW + timedelta(hours=6)))
    _record(_game("nba-4011", NOW + timedelta(hours=6)))
    good = {"game_id": "nba-4010", "side": "home", "price": -110}
    other = {"game_id": "nba-4011", "side": "home", "price": -110}
    assert _post_parlay(client, [good]).status_code == 422  # needs two legs
    assert _post_parlay(  # same-game legs are correlated
        client, [good, {"game_id": "nba-4010", "side": "away", "price": 105}]
    ).status_code == 422
    assert _post_parlay(  # not an American price
        client, [dict(good, price=50), other]
    ).status_code == 422
    assert _post_parlay(  # unknown game
        client, [good, {"game_id": "nba-9999", "side": "home", "price": -110}]
    ).status_code == 404


def test_post_parlay_duplicate_leg_set_returns_existing(client):
    _record(_game("nba-4020", NOW + timedelta(hours=6)))
    _record(_game("nba-4021", NOW + timedelta(hours=6)))
    legs = [
        {"game_id": "nba-4020", "side": "home", "price": -120, "book": "dk"},
        {"game_id": "nba-4021", "side": "away", "price": 140},
    ]
    first = _post_parlay(client, legs).json()
    # Same leg set in any order (even with a different stake) is a double-submit.
    again = _post_parlay(client, list(reversed(legs)), stake_units=3).json()
    assert again["duplicate"] is True and again["id"] == first["id"]
    assert again["stake_units"] == pytest.approx(1.0)
    # A moved price on any leg is a genuinely new slip.
    moved = [legs[0], dict(legs[1], price=145)]
    assert _post_parlay(client, moved).json()["id"] != first["id"]


def test_delete_parlay(client):
    _record(_game("nba-4030", NOW + timedelta(hours=6)))
    _record(_game("nba-4031", NOW + timedelta(hours=6)))
    row = _post_parlay(
        client,
        [
            {"game_id": "nba-4030", "side": "home", "price": -110},
            {"game_id": "nba-4031", "side": "home", "price": -110},
        ],
    ).json()
    resp = client.delete(f"/api/mybets/parlay/{row['id']}")
    assert resp.status_code == 200 and resp.json() == {"deleted": row["id"]}
    assert client.get("/api/mybets").json()["parlays"] == []
    assert client.delete("/api/mybets/parlay/12345").status_code == 404


def test_delete_graded_parlay_is_409(client):
    g1 = _record(_game("nba-4040", NOW - timedelta(days=1)))
    g2 = _record(
        _game("nba-4041", NOW - timedelta(days=1),
              home="Denver Nuggets", away="Utah Jazz")
    )
    row = _post_parlay(
        client,
        [
            {"game_id": "nba-4040", "side": "home", "price": -110},
            {"game_id": "nba-4041", "side": "home", "price": -110},
        ],
    ).json()
    _settle(Sport.NBA, [_final(g1, 110, 102), _final(g2, 99, 90)])
    assert client.delete(f"/api/mybets/parlay/{row['id']}").status_code == 409
    assert len(client.get("/api/mybets").json()["parlays"]) == 1


def test_settle_grades_parlay_when_all_legs_win():
    start = NOW - timedelta(days=1)
    g1 = _record(_game("nba-4101", start))
    g2 = _record(
        _game("nba-4102", start, home="Denver Nuggets", away="Utah Jazz")
    )
    tracker.log_parlay(
        [
            {"game_id": "nba-4101", "side": "home", "price": 100},
            {"game_id": "nba-4102", "side": "home", "price": -110},
        ],
        stake_units=2.0,
    )
    summary = _settle(Sport.NBA, [_final(g1, 110, 102), _final(g2, 99, 90)])
    assert summary["parlays_graded"] == 1
    (parlay,) = tracker.my_bets()["parlays"]
    assert parlay["result"] == "W"
    # 2u x (2.0 x 1.9091 - 1) = +5.64u
    assert parlay["units"] == pytest.approx(2.0 * (2.0 * (1 + 100 / 110) - 1))
    assert parlay["graded_at"] is not None
    assert [leg["result"] for leg in parlay["legs"]] == ["W", "W"]

    # Already-graded parlays don't regrade on the next settle run.
    assert _settle(Sport.NBA, [])["parlays_graded"] == 0


def test_parlay_waits_for_every_leg_lost_leg_loses_push_leg_drops_out():
    start = NOW - timedelta(days=1)
    g1 = _record(_game("nba-4111", start))
    g2 = _record(_game("nfl-4112", start, sport=Sport.NFL))
    g3 = _record(
        _game("nba-4113", start, home="Denver Nuggets", away="Utah Jazz")
    )
    # Slip 1: NFL tie pushes its leg out — pays the winning leg alone.
    tracker.log_parlay(
        [
            {"game_id": "nfl-4112", "side": "home", "price": -110},
            {"game_id": "nba-4111", "side": "home", "price": 150},
        ]
    )
    # Slip 2: one lost leg loses the whole slip at its stake.
    tracker.log_parlay(
        [
            {"game_id": "nba-4111", "side": "away", "price": 130},
            {"game_id": "nba-4113", "side": "home", "price": -120},
        ],
        stake_units=2.0,
    )
    nba = _settle(Sport.NBA, [_final(g1, 110, 102), _final(g3, 99, 90)])
    assert nba["parlays_graded"] == 1  # slip 1 still waits on its NFL leg
    nfl = _settle(Sport.NFL, [_final(g2, 20, 20)])
    assert nfl["parlays_graded"] == 1

    parlays = {p["id"]: p for p in tracker.my_bets()["parlays"]}
    push_win = parlays[1]
    assert (push_win["result"], push_win["units"]) == ("W", pytest.approx(1.5))
    assert sorted(leg["result"] for leg in push_win["legs"]) == ["P", "W"]
    lost = parlays[2]
    assert (lost["result"], lost["units"]) == ("L", pytest.approx(-2.0))


def test_all_pushed_parlay_pushes():
    g1 = _record(_game("nba-4121", NOW - timedelta(days=10)))
    g2 = _record(
        _game("nba-4122", NOW - timedelta(days=10),
              home="Denver Nuggets", away="Utah Jazz")
    )
    tracker.log_parlay(
        [
            {"game_id": "nba-4121", "side": "home", "price": -110},
            {"game_id": "nba-4122", "side": "home", "price": -110},
        ]
    )
    summary = _settle(Sport.NBA, [])  # both games abandon past the lookback
    assert summary["abandoned"] == 2 and summary["parlays_graded"] == 1
    (parlay,) = tracker.my_bets()["parlays"]
    assert (parlay["result"], parlay["units"]) == ("P", 0.0)


def test_get_mybets_pnl_includes_parlays(client):
    _seed_pnl_history()  # 3 graded straights (+2.0u on 4u staked) + 1 pending
    day2 = (NOW - timedelta(days=1)).date().isoformat()
    # A winning parlay across the two settled winners; grades on the next
    # settle pass. Its units land on day2 — the LAST leg's kickoff date.
    tracker.log_parlay(
        [
            {"game_id": "nba-3001", "side": "home", "price": 150},
            {"game_id": "nba-3002", "side": "home", "price": -120},
        ]
    )
    # A pending parlay (future game) stays ungraded.
    tracker.log_parlay(
        [
            {"game_id": "nba-3004", "side": "home", "price": -110},
            {"game_id": "nba-3001", "side": "home", "price": 140},
        ]
    )
    assert _settle(Sport.NBA, [])["parlays_graded"] == 1

    body = client.get("/api/mybets").json()
    parlay_units = 2.5 * (1 + 100 / 120) - 1  # +3.5833 at 1u
    pnl = body["pnl"]
    assert pnl["n"] == 6 and pnl["graded"] == 4
    assert pnl["units"] == pytest.approx(2.0 + parlay_units)
    assert pnl["roi_pct"] == pytest.approx((2.0 + parlay_units) / 5.0 * 100.0)
    assert pnl["record"] == "2-1-1"
    assert pnl["daily"][-1]["date"] == day2
    assert pnl["daily"][-1]["units"] == pytest.approx(-1.0 + parlay_units)
    assert len(body["parlays"]) == 2

    # `since` past every leg drops the graded parlay but keeps the pending
    # one (its last leg is in the future).
    tomorrow = (NOW + timedelta(days=1)).date().isoformat()
    later = client.get("/api/mybets", params={"since": tomorrow}).json()
    assert len(later["parlays"]) == 1
    assert later["parlays"][0]["result"] is None

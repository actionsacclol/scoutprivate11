"""Prediction tracking: record boards, settle results, score the models.

This is the public face of the store. The capture job feeds GameCards in via
record_cards(); the settle job fills final scores from ESPN via settle();
performance() / clv() / line_movement() read the settled history back out for
the terminal's accuracy and closing-line-value views.

All reads/writes go through short-lived sqlite connections (see store.db), so
these functions are safe to call from jobs and API handlers alike.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import httpx

from kryptscout.core.oddsmath import remove_vig
from kryptscout.core.schemas import Game, GameCard, Sport
from kryptscout.datasources import espn
from kryptscout.store import db, picks as picks_mod

_EPS = 1e-12

# Injectable results source: async (sport, on) -> list[Game] with final scores.
ResultsFetcher = Callable[[Sport, date], Awaitable[list[Game]]]


# ------------------------------------------------------------------ recording


def record_cards(cards: list[GameCard]) -> dict[str, int]:
    """Persist a board: upsert games, predictions, odds snapshots, and picks.

    Predictions dedupe to one row per (game, model) per hour (see
    db.upsert_prediction); odds snapshots append one row per book per capture.
    Each card's recommended bet (store.picks, mirroring the terminal's
    src/picks.ts) is upserted to one row per (game, model): refreshed in place
    before the game's start_time, frozen once it has passed (see
    db.upsert_pick) — frozen picks don't count toward "picks".
    Returns counts: {"games": n, "predictions": n, "snapshots": n, "picks": n}.
    """
    counts = {"games": 0, "predictions": 0, "snapshots": 0, "picks": 0}
    now = datetime.now(timezone.utc)
    conn = db.connect()
    try:
        with conn:
            for card in cards:
                g = card.game
                db.upsert_game(
                    conn, g.id, g.sport.value, g.league, g.start_time,
                    g.home, g.away, g.home_score, g.away_score, g.completed,
                )
                counts["games"] += 1
                if card.prediction is not None:
                    p = card.prediction
                    db.upsert_prediction(
                        conn, p.game_id, p.sport.value, p.model, p.generated_at,
                        p.home_win_prob, p.away_win_prob, p.draw_prob,
                        p.predicted_home_score, p.predicted_away_score,
                        p.predicted_total,
                    )
                    counts["predictions"] += 1
                for ml in card.odds:
                    if db.insert_snapshot(
                        conn, g.id, g.sport.value, ml.book, ml.fetched_at,
                        ml.home_ml, ml.away_ml, ml.draw_ml,
                        ml.total_line, ml.over_price, ml.under_price,
                    ):
                        counts["snapshots"] += 1
                pick = picks_mod.derive_pick(card)
                if pick is not None:
                    written = db.upsert_pick(
                        conn, pick.game_id, pick.sport, pick.model, pick.side,
                        pick.team, pick.book, pick.price, pick.ev, pick.kelly,
                        pick.tier, g.start_time, now, model_prob=pick.model_prob,
                    )
                    if written is not None:  # None = frozen, left untouched
                        counts["picks"] += 1
    finally:
        conn.close()
    return counts


# ----------------------------------------------------------------- settlement


async def settle(
    sport: Sport,
    lookback_days: int = 3,
    fetch: ResultsFetcher | None = None,
) -> dict[str, int]:
    """Fill final scores for this sport's unsettled games.

    For every unsettled game whose start time has passed, the results source
    (ESPN scoreboard by date; injectable via `fetch` for tests) is queried
    once per distinct date — both the game's UTC date and the previous day,
    because ESPN scoreboards are keyed to US-Eastern dates while our start
    times are UTC. Results match by game id first — engine ids for team
    sports and tennis are "{sport}-{espn event id}" — falling back to
    case-insensitive (home, away, UTC start date). A matched final fills
    scores and sets completed=1 + settled_at. A game still unmatched once it
    is older than `lookback_days` is marked abandoned: settled_at set,
    completed=0, so it drops out of the pending set without polluting
    accuracy stats. A game one of whose date fetches failed stays pending —
    it is never abandoned on a network error.

    After settling games, ungraded picks of this sport's settled, frozen
    games are graded (see _grade_picks): result W/L against the final score
    ('P' for abandoned games and non-soccer ties), units at a flat 1u stake
    for value picks, NULL units for MODEL-tier picks. The user's own logged
    bets on those settled games are graded the same way at their own stake
    (see _grade_user_bets), and parlay legs / completed parlays across ALL
    sports are graded last (see _grade_parlays — parlays span sports, so
    every settle pass sweeps them).

    Returns {"settled": n, "abandoned": n, "pending": n, "picks_graded": n,
    "user_bets_graded": n, "parlays_graded": n}.
    """
    fetch = fetch or _default_results_fetcher
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)

    conn = db.connect()
    try:
        pending = db.rows(
            conn,
            """
            SELECT game_id, start_time, home, away FROM games
            WHERE sport = ? AND settled_at IS NULL AND start_time <= ?
            ORDER BY start_time
            """,
            (sport.value, db.iso(now)),
        )
        for row in pending:
            row["_start"] = datetime.fromisoformat(row["start_time"])

        # One fetch per distinct date; UTC date + previous day per game.
        days = sorted(
            {d for row in pending for d in
             (row["_start"].date(), row["_start"].date() - timedelta(days=1))}
        )
        by_id: dict[str, Game] = {}
        by_teams: dict[tuple[str, str, date], Game] = {}
        failed_days: set[date] = set()
        for day in days:
            try:
                results = await fetch(sport, day)
            except Exception:  # noqa: BLE001 — network hiccup: keep pending
                failed_days.add(day)
                continue
            for r in results:
                by_id[r.id] = r
                by_teams[(r.home.lower(), r.away.lower(), r.start_time.date())] = r

        summary = {
            "settled": 0, "abandoned": 0, "pending": 0,
            "picks_graded": 0, "user_bets_graded": 0, "parlays_graded": 0,
        }
        with conn:
            for row in pending:
                day = row["_start"].date()
                result = by_id.get(row["game_id"]) or by_teams.get(
                    (row["home"].lower(), row["away"].lower(), day)
                )
                final = (
                    result is not None
                    and result.completed
                    and result.home_score is not None
                    and result.away_score is not None
                )
                fetch_failed = bool(
                    failed_days & {day, day - timedelta(days=1)}
                )
                if final:
                    db.mark_settled(
                        conn, row["game_id"], now,
                        result.home_score, result.away_score, True,
                    )
                    summary["settled"] += 1
                elif row["_start"] < cutoff and not fetch_failed:
                    db.mark_settled(conn, row["game_id"], now, None, None, False)
                    summary["abandoned"] += 1
                else:
                    summary["pending"] += 1
            summary["picks_graded"] = _grade_picks(conn, sport, now)
            summary["user_bets_graded"] = _grade_user_bets(conn, sport, now)
            summary["parlays_graded"] = _grade_parlays(conn, now)
        return summary
    finally:
        conn.close()


def _grade_picks(conn, sport: Sport, now: datetime) -> int:
    """Grade this sport's ungraded picks on settled games past their start.

    Only frozen picks are graded (start_time <= now — always true here since
    only started games settle). Rules, at a flat 1u stake:
      completed game   result W when the pick's side matches the outcome
                       (home/away by score, draw on a tie), else L — except a
                       non-soccer tie, which is a push ('P', 0u): two-way
                       moneylines refund on a tie, while soccer three-ways
                       lose home/away bets to the draw.
      abandoned game   result 'P', 0 units.
    Units apply to value picks only (tier STRONG/LEAN with a price): win pays
    +price/100 for positive American prices, +100/|price| for negative; loss
    is -1.0. MODEL-tier picks get result but NULL units — no price to grade.
    Returns the number of picks graded.
    """
    recs = db.rows(
        conn,
        """
        SELECT k.id, k.side, k.tier, k.price,
               g.sport, g.completed, g.home_score, g.away_score, g.start_time
        FROM picks k
        JOIN games g ON g.game_id = k.game_id
        WHERE k.sport = ? AND k.result IS NULL
          AND g.settled_at IS NOT NULL AND g.start_time <= ?
        """,
        (sport.value, db.iso(now)),
    )
    for rec in recs:
        result, units = _grade(rec)
        db.grade_pick(
            conn, rec["id"], result, units, now,
            locked_at=datetime.fromisoformat(rec["start_time"]),
        )
    return len(recs)


def _grade(rec: dict) -> tuple[str, Optional[float]]:
    """(result, units) for one settled pick row (see _grade_picks rules)."""
    value = rec["tier"] in ("STRONG", "LEAN") and rec["price"] is not None
    if not rec["completed"]:  # abandoned: push the stake back
        return "P", 0.0 if value else None
    outcome = _outcome(rec["home_score"], rec["away_score"])
    if outcome == "draw" and rec["side"] != "draw" and rec["sport"] != Sport.SOCCER.value:
        return "P", 0.0 if value else None  # two-way moneyline tie: push
    if rec["side"] != outcome:
        return "L", -1.0 if value else None
    if not value:
        return "W", None
    price = rec["price"]
    return "W", (price / 100.0 if price > 0 else 100.0 / abs(price))


def _grade_user_bets(conn, sport: Sport, now: datetime) -> int:
    """Grade this sport's ungraded user bets on settled, started games.

    Same outcome rules as _grade_picks — the winning side is home/away by
    score (draw on a soccer tie; a non-soccer tie pushes home/away bets) —
    but at the bet's own stake_units instead of a flat 1u: a win pays
    stake x price/100 for positive American prices (stake x 100/|price| for
    negative), a loss is -stake, a push/abandoned game is 'P' at 0 units.
    Returns the number of bets graded.
    """
    recs = db.rows(
        conn,
        """
        SELECT b.id, b.side, b.price, b.stake_units, b.line,
               g.sport, g.completed, g.home_score, g.away_score
        FROM user_bets b
        JOIN games g ON g.game_id = b.game_id
        WHERE b.sport = ? AND b.result IS NULL
          AND g.settled_at IS NOT NULL AND g.start_time <= ?
        """,
        (sport.value, db.iso(now)),
    )
    for rec in recs:
        result, units = _grade_bet(rec)
        db.grade_user_bet(conn, rec["id"], result, units, now)
    return len(recs)


def _result_for_side(rec: dict) -> str:
    """W/L/P for one side of a settled game — the shared outcome rule.

    Abandoned games push. A tie is a win for draw bets, a push on two-way
    (non-soccer) markets, and an outright loss for home/away sides on soccer
    three-ways. Otherwise the side either matched the outcome or it didn't.
    Needs side, sport, completed, home_score, away_score on `rec`.
    """
    if not rec["completed"]:  # abandoned: push the stake back
        return "P"
    if rec["side"] in ("over", "under"):
        # Totals bets grade against the stored line; exact hits push.
        line = rec.get("line")
        if line is None:
            return "P"  # legacy row without a line — never guess
        total = int(rec["home_score"]) + int(rec["away_score"])
        if total == line:
            return "P"
        hit_over = total > line
        return "W" if (rec["side"] == "over") == hit_over else "L"
    outcome = _outcome(rec["home_score"], rec["away_score"])
    if outcome == "draw" and rec["side"] != "draw" and rec["sport"] != Sport.SOCCER.value:
        return "P"  # two-way moneyline tie: push
    return "W" if rec["side"] == outcome else "L"


def _grade_bet(rec: dict) -> tuple[str, float]:
    """(result, units) for one settled user-bet row (see _grade_user_bets)."""
    stake = rec["stake_units"]
    result = _result_for_side(rec)
    if result == "P":
        return "P", 0.0
    if result == "L":
        return "L", -stake
    return "W", stake * (_decimal(rec["price"]) - 1.0)


def _grade_parlays(conn, now: datetime) -> int:
    """Grade parlay legs on settled games, then any fully-graded parlays.

    Sweeps ALL sports every settle pass — parlays legitimately span sports,
    and a leg only settles when its own sport's settle() runs, so the sweep
    is idempotent and cheap. Each ungraded leg of an ungraded parlay whose
    game has settled gets the straight-bet outcome (_result_for_side). A
    parlay finalizes once every leg has a result:

      any leg L    the slip loses: -stake
      else         pushed legs drop out (their factor is 1 — the stake
                   rides) and each winning leg multiplies the return by its
                   decimal price: units = stake x (product - 1)
      all legs P   the slip pushes: 0u

    Returns the number of parlays finalized (not legs graded).
    """
    legs = db.rows(
        conn,
        """
        SELECT l.id, l.side, g.sport, g.completed, g.home_score, g.away_score
        FROM user_parlay_legs l
        JOIN games g ON g.game_id = l.game_id
        JOIN user_parlays p ON p.id = l.parlay_id
        WHERE l.result IS NULL AND p.result IS NULL
          AND g.settled_at IS NOT NULL AND g.start_time <= ?
        """,
        (db.iso(now),),
    )
    for leg in legs:
        db.grade_parlay_leg(conn, leg["id"], _result_for_side(leg))

    ready = db.rows(
        conn,
        """
        SELECT p.id, p.stake_units FROM user_parlays p
        WHERE p.result IS NULL AND NOT EXISTS (
            SELECT 1 FROM user_parlay_legs l
            WHERE l.parlay_id = p.id AND l.result IS NULL
        )
        """,
    )
    for parlay in ready:
        leg_rows = db.rows(
            conn,
            "SELECT price, result FROM user_parlay_legs WHERE parlay_id = ?",
            (parlay["id"],),
        )
        stake = parlay["stake_units"]
        if any(r["result"] == "L" for r in leg_rows):
            result, units = "L", -stake
        elif all(r["result"] == "P" for r in leg_rows):
            result, units = "P", 0.0
        else:
            returned = 1.0
            for r in leg_rows:
                if r["result"] == "W":
                    returned *= _decimal(r["price"])
            result, units = "W", stake * (returned - 1.0)
        db.grade_user_parlay(conn, parlay["id"], result, units, now)
    return len(ready)


def _decimal(price: int) -> float:
    """Decimal odds (total return per 1u staked) for an American price."""
    return 1.0 + (price / 100.0 if price > 0 else 100.0 / abs(price))


def _american(decimal_odds: float) -> int:
    """American odds for a decimal price. No clamp — combined parlay prices
    legitimately go far beyond single-bet ranges (mirrors src/parlay.ts)."""
    b = decimal_odds - 1.0
    if b <= 0:
        return -100_000  # degenerate (prob ~1); unreachable via real legs
    return round(b * 100) if b >= 1 else -round(100 / b)


async def _default_results_fetcher(sport: Sport, on: date) -> list[Game]:
    if sport is Sport.TENNIS:
        return await _tennis_results(on)
    return await espn.scoreboard(sport, on)


async def _tennis_results(on: date) -> list[Game]:
    """Completed singles matches for a date from ESPN's tennis scoreboards.

    espn.scoreboard() doesn't cover tennis (different JSON shape), so this
    mirrors the tennis engine's parsing but keeps only finished matches.
    Match "scores" are 1/0 by winner flag — enough for win/loss settlement.
    """
    from kryptscout.sports.tennis import _SINGLES_SLUGS, ESPN_SCOREBOARD_URL

    games: list[Game] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for tour in ("atp", "wta"):
            resp = await client.get(
                ESPN_SCOREBOARD_URL.format(tour=tour),
                params={"dates": on.strftime("%Y%m%d")},
            )
            resp.raise_for_status()
            for event in resp.json().get("events", []):
                for grouping in event.get("groupings", []):
                    slug = grouping.get("grouping", {}).get("slug", "")
                    league = _SINGLES_SLUGS.get(slug)
                    if league is None:
                        continue
                    for comp in grouping.get("competitions", []):
                        game = _parse_tennis_final(comp, league)
                        if game is not None:
                            games.append(game)
    return games


def _parse_tennis_final(comp: dict, league: str) -> Optional[Game]:
    if comp.get("status", {}).get("type", {}).get("state") != "post":
        return None
    try:
        start = datetime.strptime(comp["date"], "%Y-%m-%dT%H:%MZ").replace(
            tzinfo=timezone.utc
        )
    except (KeyError, ValueError):
        return None
    home = away = None
    home_won: Optional[bool] = None
    for competitor in comp.get("competitors", []):
        name = (competitor.get("athlete") or {}).get("displayName")
        if not name:
            return None
        if competitor.get("homeAway") == "home":
            home = name
            home_won = bool(competitor.get("winner"))
        else:
            away = name
    if home is None or away is None or home_won is None:
        return None
    return Game(
        id=f"tennis-{comp['id']}",
        sport=Sport.TENNIS,
        league=league,
        start_time=start,
        home=home,
        away=away,
        home_score=int(home_won),
        away_score=int(not home_won),
        completed=True,
    )


# ---------------------------------------------------------------- performance


def performance(
    sport: Sport | None = None,
    model: str | None = None,
    since: date | None = None,
) -> dict:
    """Model accuracy over settled games, grouped by (sport, model).

    Uses each model's LAST recorded prediction per game. For every settled,
    completed game the actual side is home/away by score (draw on a tie);
    metrics per group are:
      n         settled games scored
      accuracy  fraction where argmax(home, away, draw) matched the outcome
      log_loss  mean -ln(prob assigned to the actual side)
      brier     mean multi-class Brier score, sum_k (p_k - y_k)^2 over the
                sides the model priced (draw included only when predicted)

    Returns {"overall": [group rows...], "daily": [{date, n, accuracy,
    log_loss}...]} — the daily series spans all groups after filtering.
    """
    where, params = _filters(sport=sport, model=model, since=since)
    conn = db.connect()
    try:
        recs = db.rows(
            conn,
            f"""
            SELECT g.game_id, g.sport, g.start_time, g.home_score, g.away_score,
                   p.model, p.home_win_prob, p.away_win_prob, p.draw_prob
            FROM games g
            JOIN predictions p ON p.game_id = g.game_id
            WHERE g.settled_at IS NOT NULL AND g.completed = 1
              AND p.generated_at = (
                    SELECT MAX(p2.generated_at) FROM predictions p2
                    WHERE p2.game_id = p.game_id AND p2.model = p.model
              )
              {where}
            ORDER BY g.start_time
            """,
            params,
        )
    finally:
        conn.close()

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    daily: dict[str, list[dict]] = defaultdict(list)
    for rec in recs:
        scored = _score_prediction(rec)
        groups[(rec["sport"], rec["model"])].append(scored)
        daily[rec["start_time"][:10]].append(scored)

    overall = [
        {
            "sport": sport_key,
            "model": model_key,
            "n": len(items),
            "accuracy": _mean(x["correct"] for x in items),
            "log_loss": _mean(x["log_loss"] for x in items),
            "brier": _mean(x["brier"] for x in items),
        }
        for (sport_key, model_key), items in sorted(groups.items())
    ]
    series = [
        {
            "date": day,
            "n": len(items),
            "accuracy": _mean(x["correct"] for x in items),
            "log_loss": _mean(x["log_loss"] for x in items),
        }
        for day, items in sorted(daily.items())
    ]
    return {"overall": overall, "daily": series}


def _score_prediction(rec: dict) -> dict:
    """Accuracy / log loss / Brier for one (game, prediction) record."""
    actual = _outcome(rec["home_score"], rec["away_score"])
    probs = {"home": rec["home_win_prob"], "away": rec["away_win_prob"]}
    if rec["draw_prob"] is not None:
        probs["draw"] = rec["draw_prob"]
    picked = max(probs, key=probs.get)
    p_actual = probs.get(actual, 0.0)
    brier = sum(
        (p - (1.0 if side == actual else 0.0)) ** 2 for side, p in probs.items()
    )
    if actual not in probs:  # e.g. a tie the model never priced
        brier += 1.0
    return {
        "correct": float(picked == actual),
        "log_loss": -math.log(max(p_actual, _EPS)),
        "brier": brier,
    }


def _outcome(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "home"
    if away_score > home_score:
        return "away"
    return "draw"


# ------------------------------------------------------------------------ CLV


def clv(sport: Sport | None = None, since: date | None = None) -> dict:
    """Closing-line value of the model's picks over settled games.

    Definition: for each settled, completed game with at least two odds
    snapshots, take the model's picked side (argmax of its last prediction,
    draw included when priced). Per book, devig the full moneyline market
    (remove_vig) at the FIRST snapshot and at the LAST snapshot taken at or
    before the game's start time, and read off the picked side's vig-free
    probability at each: p_open and p_close. Then

        CLV (prob pts) = (p_close - p_open) * 100
        CLV (bps)      = (p_close / p_open - 1) * 10_000

    Positive CLV means the market closed shorter (more probable) on our side
    than when we first saw it — i.e. a bet at the opening snapshot beat the
    close, the standard sharpness proxy. A game's CLV averages across its
    books (only books with two distinct usable snapshots count); games with
    multiple models yield one row per model.

    Returns {"overall": {n, avg_clv_prob_pts, avg_clv_bps}, "by_sport": [...],
    "games": [per-game rows]}.
    """
    where, params = _filters(sport=sport, since=since)
    conn = db.connect()
    try:
        recs = db.rows(
            conn,
            f"""
            SELECT g.game_id, g.sport, g.start_time,
                   p.model, p.home_win_prob, p.away_win_prob, p.draw_prob
            FROM games g
            JOIN predictions p ON p.game_id = g.game_id
            WHERE g.settled_at IS NOT NULL AND g.completed = 1
              AND p.generated_at = (
                    SELECT MAX(p2.generated_at) FROM predictions p2
                    WHERE p2.game_id = p.game_id AND p2.model = p.model
              )
              {where}
            ORDER BY g.start_time
            """,
            params,
        )
        game_rows = []
        for rec in recs:
            snaps = db.rows(
                conn,
                """
                SELECT book, fetched_at, home_ml, away_ml, draw_ml
                FROM odds_snapshots WHERE game_id = ? ORDER BY fetched_at
                """,
                (rec["game_id"],),
            )
            if len(snaps) < 2:
                continue
            row = _game_clv(rec, snaps)
            if row is not None:
                game_rows.append(row)
    finally:
        conn.close()

    by_sport: dict[str, list[dict]] = defaultdict(list)
    for row in game_rows:
        by_sport[row["sport"]].append(row)

    def _agg(items: list[dict]) -> dict:
        return {
            "n": len(items),
            "avg_clv_prob_pts": _mean(x["clv_prob_pts"] for x in items),
            "avg_clv_bps": _mean(x["clv_bps"] for x in items),
        }

    return {
        "overall": _agg(game_rows),
        "by_sport": [
            {"sport": key, **_agg(items)} for key, items in sorted(by_sport.items())
        ],
        "games": game_rows,
    }


def _game_clv(rec: dict, snaps: list[dict]) -> Optional[dict]:
    """One game's CLV: mean over books of picked-side p_close - p_open."""
    probs = {"home": rec["home_win_prob"], "away": rec["away_win_prob"]}
    if rec["draw_prob"] is not None:
        probs["draw"] = rec["draw_prob"]
    side = max(probs, key=probs.get)

    start_time = rec["start_time"]
    by_book: dict[str, list[dict]] = defaultdict(list)
    for snap in snaps:
        by_book[snap["book"]].append(snap)

    pts, bps = [], []
    for series in by_book.values():
        pregame = [s for s in series if s["fetched_at"] <= start_time]
        if len(pregame) < 2:
            continue
        p_open = _devig_side(pregame[0], side)
        p_close = _devig_side(pregame[-1], side)
        if p_open is None or p_close is None:
            continue
        pts.append((p_close - p_open) * 100.0)
        bps.append((p_close / p_open - 1.0) * 10_000.0)
    if not pts:
        return None
    return {
        "game_id": rec["game_id"],
        "sport": rec["sport"],
        "model": rec["model"],
        "side": side,
        "books": len(pts),
        "clv_prob_pts": _mean(pts),
        "clv_bps": _mean(bps),
    }


def _devig_side(snap: dict, side: str) -> Optional[float]:
    """Vig-free probability of one side from a snapshot's full market."""
    if snap["home_ml"] is None or snap["away_ml"] is None:
        return None
    prices = [snap["home_ml"], snap["away_ml"]]
    sides = ["home", "away"]
    if snap["draw_ml"] is not None:
        prices.append(snap["draw_ml"])
        sides.append("draw")
    if side not in sides:
        return None
    fair = remove_vig(*prices)
    return fair[sides.index(side)]


def _clv_for_side(snaps: list[dict], side: str, start_time: str) -> Optional[tuple[float, float]]:
    """(CLV prob-pts, CLV bps) for a SPECIFIC side over a game's snapshots —
    the pick-side analogue of _game_clv. Averages across books that carry two
    distinct pregame quotes; None if none do."""
    by_book: dict[str, list[dict]] = defaultdict(list)
    for snap in snaps:
        by_book[snap["book"]].append(snap)
    pts, bps = [], []
    for series in by_book.values():
        pregame = [s for s in series if s["fetched_at"] <= start_time]
        if len(pregame) < 2:
            continue
        p_open = _devig_side(pregame[0], side)
        p_close = _devig_side(pregame[-1], side)
        if p_open is None or p_close is None or p_open <= 0:
            continue
        pts.append((p_close - p_open) * 100.0)
        bps.append((p_close / p_open - 1.0) * 10_000.0)
    if not pts:
        return None
    return _mean(pts), _mean(bps)


def clv_by_tier(sport: Sport | None = None, since: date | None = None) -> dict:
    """CLV of the recorded PICKS, grouped by tier (STRONG/LEAN/MODEL).

    Unlike clv() — which scores the model's argmax side — this scores each
    pick's OWN recommended side (what users actually bet), so it answers "does
    the STRONG tier beat the close?". Only settled, completed games whose pick
    has 2+ pregame snapshots count.

    Per tier: n, avg CLV (prob-pts + bps), se_bps (standard error of the mean —
    the honest error bar), win_rate, and `significant` (|t| >= 2 at n >= 30).
    Sports flagged NO_VALUE (tennis) only ever produce MODEL picks, so their
    picks land in the MODEL bucket.
    """
    clauses, params = [], []
    if sport is not None:
        clauses.append("AND k.sport = ?")
        params.append(sport.value)
    if since is not None:
        clauses.append("AND date(g.start_time) >= ?")
        params.append(since.isoformat())
    conn = db.connect()
    try:
        recs = db.rows(
            conn,
            f"""
            SELECT k.game_id, k.side, k.tier, k.result, g.start_time
            FROM picks k JOIN games g ON g.game_id = k.game_id
            WHERE g.settled_at IS NOT NULL AND g.completed = 1
              AND k.side IN ('home', 'away', 'draw')
              {' '.join(clauses)}
            ORDER BY g.start_time
            """,
            tuple(params),
        )
        buckets: dict[str, list[dict]] = defaultdict(list)
        for rec in recs:
            snaps = db.rows(
                conn,
                """
                SELECT book, fetched_at, home_ml, away_ml, draw_ml
                FROM odds_snapshots WHERE game_id = ? ORDER BY fetched_at
                """,
                (rec["game_id"],),
            )
            if len(snaps) < 2:
                continue
            got = _clv_for_side(snaps, rec["side"], rec["start_time"])
            if got is None:
                continue
            pts, bps = got
            buckets[rec["tier"]].append({"pts": pts, "bps": bps, "result": rec["result"]})
    finally:
        conn.close()

    by_tier: dict[str, dict] = {}
    for tier in ("STRONG", "LEAN", "MODEL"):
        items = buckets.get(tier, [])
        n = len(items)
        if n == 0:
            by_tier[tier] = {
                "n": 0, "avg_clv_prob_pts": None, "avg_clv_bps": None,
                "se_bps": None, "win_rate": None, "significant": False,
            }
            continue
        bps = [x["bps"] for x in items]
        mean_bps = sum(bps) / n
        se = None
        if n > 1:
            var = sum((b - mean_bps) ** 2 for b in bps) / (n - 1)
            se = (var ** 0.5) / (n ** 0.5)
        decided = [x for x in items if x["result"] in ("W", "L")]
        win_rate = (
            sum(1 for x in decided if x["result"] == "W") / len(decided)
            if decided else None
        )
        significant = bool(n >= 30 and se and abs(mean_bps / se) >= 2.0)
        by_tier[tier] = {
            "n": n,
            "avg_clv_prob_pts": _mean(x["pts"] for x in items),
            "avg_clv_bps": mean_bps,
            "se_bps": se,
            "win_rate": win_rate,
            "significant": significant,
        }
    return {
        "by_tier": by_tier,
        "note": (
            "CLV of each tier's picks vs the close (positive = beats the "
            "market's final price). Needs ~30+ graded picks per tier before it "
            "means anything — 'significant' flags |t|>=2 at n>=30. Below that "
            "it's provisional: a hot streak and a real edge look identical."
        ),
    }


_SIDE_COLUMNS = {"home": ("open_home_ml", "close_home_ml"),
                 "away": ("open_away_ml", "close_away_ml"),
                 "draw": ("open_draw_ml", "close_draw_ml")}


def clv_vs_close(sport: Sport | None = None, since: date | None = None) -> dict:
    """Did our picks beat the closing line? Scored off `line_endpoints`.

    This is the measurement that separates an edge from a hot streak. It
    reports three numbers, and the FIRST THING IT DOES is split same-book from
    cross-venue picks, because conflating them produces a spectacular and
    completely fake result.

    Why that split is not optional: this app's value picks are priced at
    Polymarket and Kalshi, while ESPN's close is DraftKings. Exchanges run at
    near-zero vig and a sportsbook holds ~4-5%, so an exchange entry price
    "beats" a DK closing price on essentially every bet ever made, win or
    lose. Measured naively over the first backfill that produced +6.1% CLV at
    a 12-of-12 beat rate, which is not an edge — it is the spread between two
    venues, reported as skill.

    * **price_clv_pct** (same book only) — entry vs that same book's close on
      the same side, `(dec_entry / dec_close - 1) * 100`. Same book, same
      side, so the vig cancels and what is left is pure price improvement.
      This is the industry-standard beat-the-close number.

    * **fair_ev_pct** (cross-venue) — the honest comparison when the entry
      venue and the closing book differ: our entry price's implied
      probability against the DEVIGGED closing probability,
      `(p_close_fair / q_entry - 1) * 100`. Removing the closing book's vig is
      what makes the two comparable. Reads as expected value per unit staked.
      It still assumes the entry venue is genuinely low-vig, which holds for
      Polymarket/Kalshi and would not for another sportsbook.

    * **drift_prob_pts** — the devigged open-to-close move on our side, in
      probability points. Available for every pick including MODEL tier (which
      records no price), and it measures whether the market came toward us at
      all. Weaker evidence: a pick made an hour before kickoff gets credited
      with drift that happened before it existed.

    Where `clv()` and `clv_by_tier()` reconstruct endpoints from whatever
    `odds_snapshots` we happened to capture, this uses the book's own declared
    open and close, which `jobs/lines.py` can backfill over settled history.
    That is the difference between n=11 and a real sample.

    Returns {"by_tier": {...}, "overall": {...}, "note": str}.
    """
    clauses, params = [], []
    if sport is not None:
        clauses.append("AND k.sport = ?")
        params.append(sport.value)
    if since is not None:
        clauses.append("AND date(g.start_time) >= ?")
        params.append(since.isoformat())
    conn = db.connect()
    try:
        recs = db.rows(
            conn,
            f"""
            SELECT k.tier, k.side, k.price, k.result, k.book AS entry_book, e.*
            FROM picks k
            JOIN games g ON g.game_id = k.game_id
            JOIN line_endpoints e ON e.game_id = k.game_id
            WHERE g.completed = 1 AND k.side IN ('home', 'away', 'draw')
              {' '.join(clauses)}
            """,
            tuple(params),
        )
    finally:
        conn.close()

    buckets: dict[str, list[dict]] = defaultdict(list)
    for rec in recs:
        open_col, close_col = _SIDE_COLUMNS[rec["side"]]
        open_ml, close_ml = rec[open_col], rec[close_col]
        if close_ml is None:
            continue
        # Devigged drift needs the whole market at both ends; a two-way book
        # missing one side can still price the pick, so guard per endpoint.
        opens = [p for p in (rec["open_home_ml"], rec["open_away_ml"],
                             rec["open_draw_ml"]) if p is not None]
        closes = [p for p in (rec["close_home_ml"], rec["close_away_ml"],
                              rec["close_draw_ml"]) if p is not None]
        p_close_fair = None
        if len(closes) >= 2:
            try:
                p_close_fair = remove_vig(*closes)[_side_index(rec, close_col)]
            except (ValueError, ZeroDivisionError, IndexError):
                p_close_fair = None
        drift = None
        if open_ml is not None and len(opens) >= 2 and p_close_fair is not None:
            try:
                p_open = remove_vig(*opens)[_side_index(rec, open_col)]
                drift = (p_close_fair - p_open) * 100.0
            except (ValueError, ZeroDivisionError, IndexError):
                drift = None

        price_clv = fair_ev = None
        if rec["price"] is not None:
            if rec["entry_book"] == rec["book"]:
                # Same book both ends: vig cancels, this is real price improvement.
                price_clv = (_decimal(rec["price"]) / _decimal(close_ml) - 1.0) * 100.0
            elif p_close_fair is not None:
                # Different venues: the closing book's vig has to come out or
                # the exchange wins by construction. See the docstring.
                q_entry = 1.0 / _decimal(rec["price"])
                if q_entry > _EPS:
                    fair_ev = (p_close_fair / q_entry - 1.0) * 100.0
        buckets[rec["tier"]].append({
            "price_clv": price_clv, "fair_ev": fair_ev, "drift": drift,
            "result": rec["result"],
            "cross_venue": rec["price"] is not None and rec["entry_book"] != rec["book"],
        })

    def _stats(values: list[float]) -> tuple[Optional[float], Optional[float], bool]:
        """(mean, standard error, significant) — significance needs n >= 30."""
        if not values:
            return None, None, False
        mean = _mean(values)
        if len(values) < 2:
            return mean, None, False
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        se = (var ** 0.5) / (len(values) ** 0.5)
        return mean, se, bool(len(values) >= 30 and se and abs(mean / se) >= 2.0)

    def summarize(items: list[dict]) -> dict:
        priced = [x["price_clv"] for x in items if x["price_clv"] is not None]
        fair = [x["fair_ev"] for x in items if x["fair_ev"] is not None]
        drifts = [x["drift"] for x in items if x["drift"] is not None]
        decided = [x for x in items if x["result"] in ("W", "L")]
        clv_mean, clv_se, clv_sig = _stats(priced)
        fair_mean, fair_se, fair_sig = _stats(fair)
        return {
            "n": len(items),
            "n_same_book": len(priced),
            "n_cross_venue": len(fair),
            "price_clv_pct": clv_mean,
            "price_clv_se": clv_se,
            "beat_close_rate": (
                sum(1 for p in priced if p > 0) / len(priced) if priced else None
            ),
            "fair_ev_pct": fair_mean,
            "fair_ev_se": fair_se,
            "positive_ev_rate": (
                sum(1 for f in fair if f > 0) / len(fair) if fair else None
            ),
            "drift_prob_pts": _mean(drifts) if drifts else None,
            "win_rate": (
                sum(1 for x in decided if x["result"] == "W") / len(decided)
                if decided else None
            ),
            "significant": bool(clv_sig or fair_sig),
        }

    everything = [x for items in buckets.values() for x in items]
    return {
        "by_tier": {t: summarize(buckets.get(t, [])) for t in ("STRONG", "LEAN", "MODEL")},
        "overall": summarize(everything),
        "note": (
            "price_clv_pct compares entry to the SAME book's close (vig "
            "cancels). fair_ev_pct is for picks whose entry venue differs from "
            "the closing book — exchange prices beat a sportsbook's close by "
            "construction, so that column devigs the close first. Never read "
            "the two as one number. Needs ~30+ per tier before 'significant' "
            "can trip; run `python -m kryptscout.jobs.lines` for more games."
        ),
    }


def _side_index(rec: dict, column: str) -> int:
    """Position of the pick's side within the price list handed to remove_vig.

    The list is built home, away, draw with Nones dropped, so the index is the
    count of non-None entries before this side — not a fixed offset.
    """
    order = ["home", "away", "draw"]
    prefix = column.split("_")[0]  # "open" | "close"
    target = rec["side"]
    idx = 0
    for name in order:
        if name == target:
            return idx
        if rec[f"{prefix}_{name}_ml"] is not None:
            idx += 1
    raise IndexError(target)


# ------------------------------------------------------------------------ ROI

_VALUE_TIERS = ("STRONG", "LEAN")


def roi(sport: Sport | None = None, since: date | None = None) -> dict:
    """Pick-level ROI over recorded picks (graded and pending).

    Units and the daily series count VALUE picks only (tier STRONG/LEAN —
    the ones with a price to grade at a flat 1u stake); MODEL-tier picks
    contribute record (win_pct) but never units. roi_pct is units staked vs
    units returned: total units / graded value picks * 100. win_pct is
    W / (W + L) as a 0-1 fraction, pushes excluded. All ratios are None
    (not NaN) when their denominator is empty.

    Returns
      {"overall": {n_picks, n_graded, units, roi_pct,
                   by_tier: {STRONG: {n, units, win_pct}, LEAN: {...},
                             MODEL: {n, win_pct, units: None}}},
       "by_sport": [{sport, n, units, win_pct}...],
       "daily": [{date, units, cum_units}...]}   (date = game start date)
    """
    clauses, params = [], []
    if sport is not None:
        clauses.append("AND k.sport = ?")
        params.append(sport.value)
    if since is not None:
        clauses.append("AND date(g.start_time) >= ?")
        params.append(since.isoformat())
    conn = db.connect()
    try:
        recs = db.rows(
            conn,
            f"""
            SELECT k.sport, k.tier, k.result, k.units, g.start_time
            FROM picks k
            JOIN games g ON g.game_id = k.game_id
            WHERE 1 = 1 {' '.join(clauses)}
            ORDER BY g.start_time
            """,
            tuple(params),
        )
    finally:
        conn.close()

    graded = [r for r in recs if r["result"] is not None]
    value_graded = [r for r in graded if r["tier"] in _VALUE_TIERS]
    units = sum(r["units"] or 0.0 for r in value_graded)

    by_tier = {}
    for tier in ("STRONG", "LEAN", "MODEL"):
        items = [r for r in graded if r["tier"] == tier]
        by_tier[tier] = {
            "n": len(items),
            "units": (
                sum(r["units"] or 0.0 for r in items) if tier in _VALUE_TIERS
                else None
            ),
            "win_pct": _win_pct(items),
        }

    sports_seen: dict[str, list[dict]] = defaultdict(list)
    for r in graded:
        sports_seen[r["sport"]].append(r)
    by_sport = [
        {
            "sport": key,
            "n": len(items),
            "units": sum(
                r["units"] or 0.0 for r in items if r["tier"] in _VALUE_TIERS
            ),
            "win_pct": _win_pct(items),
        }
        for key, items in sorted(sports_seen.items())
    ]

    day_units: dict[str, float] = defaultdict(float)
    for r in value_graded:
        day_units[r["start_time"][:10]] += r["units"] or 0.0
    daily, running = [], 0.0
    for day, day_total in sorted(day_units.items()):
        running += day_total
        daily.append({"date": day, "units": day_total, "cum_units": running})

    return {
        "overall": {
            "n_picks": len(recs),
            "n_graded": len(graded),
            "units": units,
            "roi_pct": (
                units / len(value_graded) * 100.0 if value_graded else None
            ),
            "by_tier": by_tier,
        },
        "by_sport": by_sport,
        "daily": daily,
    }


def _win_pct(items: list[dict]) -> Optional[float]:
    """W / (W + L) as a 0-1 fraction (matching `accuracy` elsewhere);
    pushes excluded; None if empty."""
    decided = [r for r in items if r["result"] in ("W", "L")]
    if not decided:
        return None
    wins = sum(1 for r in decided if r["result"] == "W")
    return wins / len(decided)


# ------------------------------------------------------------------ user bets


def log_bet(
    game_id: str,
    side: str,
    price: int,
    book: Optional[str] = None,
    stake_units: float = 1.0,
    note: Optional[str] = None,
    line: Optional[float] = None,
) -> dict:
    """Log one of the user's OWN bets against a recorded game.

    The game must already exist in the games table (the board records games
    on serve, so anything the user tracked is there) — raises KeyError for
    an unknown game_id. sport and team are resolved server-side from the
    game row and the bet's side ("DRAW" for draw bets); stake_units is
    clamped to 0.1..100.

    Returns the stored row plus "duplicate": an exact ungraded duplicate
    (same game/side/book/price — see db.insert_user_bet) returns the
    existing row with duplicate=True instead of inserting a new one.
    """
    now = datetime.now(timezone.utc)
    stake = min(max(float(stake_units), 0.1), 100.0)
    conn = db.connect()
    try:
        found = db.rows(
            conn,
            "SELECT sport, home, away FROM games WHERE game_id = ?",
            (game_id,),
        )
        if not found:
            raise KeyError(game_id)
        game = found[0]
        if side in ("over", "under"):
            if line is None:
                raise ValueError("totals bets need a line")
            team = f"{side.upper()} {line:g}"
        else:
            team = {"home": game["home"], "away": game["away"]}.get(side, "DRAW")
        with conn:
            bet_id, created = db.insert_user_bet(
                conn, game_id, game["sport"], side, team, price, book,
                stake, note, now, line=line if side in ("over", "under") else None,
            )
        row = db.rows(
            conn, "SELECT * FROM user_bets WHERE id = ?", (bet_id,)
        )[0]
        row["duplicate"] = not created
        return row
    finally:
        conn.close()


def delete_bet(bet_id: int) -> Optional[bool]:
    """Remove an ungraded user bet.

    Returns True when deleted, False when the bet is already graded (kept —
    graded history is immutable), None when no such bet exists.
    """
    conn = db.connect()
    try:
        found = db.rows(
            conn, "SELECT result FROM user_bets WHERE id = ?", (bet_id,)
        )
        if not found:
            return None
        if found[0]["result"] is not None:
            return False
        with conn:
            db.delete_user_bet(conn, bet_id)
        return True
    finally:
        conn.close()


def log_parlay(
    legs: list[dict],
    stake_units: float = 1.0,
    note: Optional[str] = None,
) -> dict:
    """Track a parlay the user built (PARLAY page) against recorded games.

    Each leg dict carries game_id, side ('home'|'away'|'draw'), price
    (American, at track time) and an optional book. Structural rules —
    at least two legs, one leg per game (same-game legs are correlated;
    the builder blocks them and we enforce it here) — raise ValueError;
    an unrecorded game_id raises KeyError. sport and team resolve
    server-side like log_bet; stake_units clamps to 0.1..100. The stored
    combined `price` is the product of the legs' decimal prices in American
    odds (mirrors parlayMath in src/parlay.ts).

    Returns the stored parlay row with its "legs" plus "duplicate": an
    ungraded parlay with the exact same leg set (game/side/price/book) is
    returned with duplicate=True instead of inserting a twin.
    """
    if len(legs) < 2:
        raise ValueError("a parlay needs at least two legs")
    ids = [leg["game_id"] for leg in legs]
    if len(set(ids)) != len(ids):
        raise ValueError("one leg per game — same-game legs are correlated")
    now = datetime.now(timezone.utc)
    stake = min(max(float(stake_units), 0.1), 100.0)
    conn = db.connect()
    try:
        resolved, combined = [], 1.0
        for leg in legs:
            found = db.rows(
                conn,
                "SELECT sport, home, away FROM games WHERE game_id = ?",
                (leg["game_id"],),
            )
            if not found:
                raise KeyError(leg["game_id"])
            game = found[0]
            side = leg["side"]
            resolved.append(
                {
                    "game_id": leg["game_id"],
                    "sport": game["sport"],
                    "side": side,
                    "team": {"home": game["home"], "away": game["away"]}.get(
                        side, "DRAW"
                    ),
                    "price": leg["price"],
                    "book": leg.get("book"),
                }
            )
            combined *= _decimal(leg["price"])

        key = _parlay_key(resolved)
        for row in db.rows(
            conn, "SELECT id FROM user_parlays WHERE result IS NULL"
        ):
            existing = db.rows(
                conn,
                """
                SELECT game_id, side, price, book FROM user_parlay_legs
                WHERE parlay_id = ?
                """,
                (row["id"],),
            )
            if _parlay_key(existing) == key:
                report = _parlay_rows(conn, "WHERE p.id = ?", (row["id"],))[0]
                report["duplicate"] = True
                return report
        with conn:
            parlay_id = db.insert_user_parlay(
                conn, stake, _american(combined), note, now, resolved
            )
        report = _parlay_rows(conn, "WHERE p.id = ?", (parlay_id,))[0]
        report["duplicate"] = False
        return report
    finally:
        conn.close()


def _parlay_key(legs: list[dict]) -> frozenset:
    """Order-independent identity of a leg set, for double-submit detection."""
    return frozenset(
        (leg["game_id"], leg["side"], leg["price"], leg["book"]) for leg in legs
    )


def delete_parlay(parlay_id: int) -> Optional[bool]:
    """Remove an ungraded parlay (and its legs).

    Returns True when deleted, False when the parlay is already graded
    (kept — graded history is immutable), None when no such parlay exists.
    """
    conn = db.connect()
    try:
        found = db.rows(
            conn, "SELECT result FROM user_parlays WHERE id = ?", (parlay_id,)
        )
        if not found:
            return None
        if found[0]["result"] is not None:
            return False
        with conn:
            db.delete_user_parlay(conn, parlay_id)
        return True
    finally:
        conn.close()


def _parlay_rows(conn, where: str = "", params: tuple = ()) -> list[dict]:
    """user_parlays rows (newest first), each with its legs joined to games."""
    parlays = db.rows(
        conn,
        f"""
        SELECT p.id, p.stake_units, p.price, p.note, p.created_at,
               p.result, p.units, p.graded_at
        FROM user_parlays p
        {where}
        ORDER BY p.created_at DESC, p.id DESC
        """,
        params,
    )
    for parlay in parlays:
        parlay["legs"] = db.rows(
            conn,
            """
            SELECT l.game_id, l.sport, l.side, l.team, l.price, l.book,
                   l.result,
                   g.home, g.away, g.league, g.start_time AS kickoff,
                   g.completed, g.home_score, g.away_score
            FROM user_parlay_legs l
            JOIN games g ON g.game_id = l.game_id
            WHERE l.parlay_id = ?
            ORDER BY g.start_time, l.id
            """,
            (parlay["id"],),
        )
    return parlays


def _parlay_date(parlay: dict) -> str:
    """The date a parlay counts on: its LAST leg's UTC kickoff date — the
    slip can't grade before that game, so that's when its units land."""
    return max(leg["kickoff"][:10] for leg in parlay["legs"])


def user_clv(since: date | None = None) -> dict:
    """CLV coaching over the user's OWN logged bets — did they beat the close?

    Each bet is compared to the LAST pregame odds capture for its game,
    preferring the same book (apples-to-apples vig) and falling back to the
    freshest book otherwise. Moneyline/draw bets score CLV as the price
    advantage vs close: (decimal_bet / decimal_close − 1) × 100, positive =
    you got a better price than the market's final word. Totals bets score
    line movement in the bet's favor (points; over + line up = +). Bets on
    games with no pregame snapshot after the bet are skipped, not guessed.

    Sustained positive CLV is the strongest known predictor of long-run
    betting profit — that's the coaching angle. Returns {overall {n_bets,
    n_evaluated, beat_close, avg_clv_pct, avg_line_move_pts}, by_sport,
    recent [last 20 evaluated bets with their numbers], verdict}.
    """
    from kryptscout.core.oddsmath import american_to_decimal

    conn = db.connect()
    try:
        params: tuple = ()
        where = ""
        if since is not None:
            where = "AND b.created_at >= ?"
            params = (since.isoformat(),)
        bets = db.rows(
            conn,
            f"""
            SELECT b.*, g.start_time, g.home, g.away
            FROM user_bets b JOIN games g ON g.game_id = b.game_id
            WHERE g.start_time <= ? {where}
            ORDER BY b.created_at DESC
            """,
            (db.iso(datetime.now(timezone.utc)), *params),
        )
        evaluated: list[dict] = []
        for bet in bets:
            snaps = db.rows(
                conn,
                """
                SELECT * FROM odds_snapshots
                WHERE game_id = ? AND fetched_at <= ? AND fetched_at >= ?
                ORDER BY fetched_at DESC LIMIT 12
                """,
                (bet["game_id"], bet["start_time"], bet["created_at"]),
            )
            if not snaps:
                continue  # nothing captured between bet and kickoff — skip
            side = bet["side"]

            def _close(col: str):
                """Freshest snapshot quoting `col`, same book first."""
                pool = [s for s in snaps if s[col] is not None]
                return next((s for s in pool if s["book"] == bet["book"]),
                            pool[0] if pool else None)

            entry: dict | None = None
            if side in ("home", "away", "draw"):
                col = {"home": "home_ml", "away": "away_ml", "draw": "draw_ml"}[side]
                close = _close(col)
                if close is None:
                    continue
                close_price = close[col]
                clv_pct = (american_to_decimal(int(bet["price"]))
                           / american_to_decimal(int(close_price)) - 1.0) * 100.0
                entry = {"clv_pct": round(clv_pct, 2), "close_price": int(close_price),
                         "line_move_pts": None}
            elif side in ("over", "under") and bet.get("line") is not None:
                close = _close("total_line")
                if close is None:
                    continue
                move = float(close["total_line"]) - float(bet["line"])
                if side == "under":
                    move = -move
                entry = {"clv_pct": None, "close_price": None,
                         "line_move_pts": round(move, 1)}
            if entry is None:
                continue
            entry.update({
                "id": bet["id"], "sport": bet["sport"], "team": bet["team"],
                "side": side, "price": bet["price"], "book": bet["book"],
                "close_book": close["book"], "kickoff": bet["start_time"],
                "result": bet["result"],
            })
            evaluated.append(entry)
    finally:
        conn.close()

    ml = [e for e in evaluated if e["clv_pct"] is not None]
    tot = [e for e in evaluated if e["line_move_pts"] is not None]
    beat = sum(1 for e in ml if e["clv_pct"] > 0) + sum(
        1 for e in tot if e["line_move_pts"] > 0)
    flat = sum(1 for e in ml if e["clv_pct"] == 0) + sum(
        1 for e in tot if e["line_move_pts"] == 0)
    avg_clv = (sum(e["clv_pct"] for e in ml) / len(ml)) if ml else None
    avg_move = (sum(e["line_move_pts"] for e in tot) / len(tot)) if tot else None

    n_eval = len(evaluated)
    decisive = max(n_eval - flat, 1)
    beat_rate = beat / decisive if n_eval else None
    if n_eval < 10:
        verdict = "Too few evaluated bets to coach yet — keep logging."
    elif avg_clv is not None and avg_clv >= 1.0:
        verdict = ("You consistently beat the close — that's the strongest "
                   "predictor of long-run profit. Keep doing what you're doing.")
    elif avg_clv is not None and avg_clv <= -1.0:
        verdict = ("You're consistently betting worse-than-closing prices — "
                   "the market moves against you after you bet. Bet earlier "
                   "lines less, shop prices more, or follow the board's edges.")
    else:
        verdict = ("Your prices roughly match the close — no edge, no leak. "
                   "Positive CLV is what separates winners long-run.")

    by_sport: dict[str, dict] = {}
    for e in evaluated:
        s = by_sport.setdefault(e["sport"], {"sport": e["sport"], "n": 0,
                                             "beat": 0, "clv_sum": 0.0, "clv_n": 0})
        s["n"] += 1
        if (e["clv_pct"] or 0) > 0 or (e["line_move_pts"] or 0) > 0:
            s["beat"] += 1
        if e["clv_pct"] is not None:
            s["clv_sum"] += e["clv_pct"]
            s["clv_n"] += 1
    sport_rows = [
        {"sport": s["sport"], "n": s["n"], "beat_rate": s["beat"] / s["n"],
         "avg_clv_pct": round(s["clv_sum"] / s["clv_n"], 2) if s["clv_n"] else None}
        for s in by_sport.values()
    ]
    sport_rows.sort(key=lambda r: -r["n"])

    return {
        "overall": {
            "n_bets": len(bets), "n_evaluated": n_eval,
            "beat_close": beat, "beat_rate": beat_rate,
            "avg_clv_pct": round(avg_clv, 2) if avg_clv is not None else None,
            "avg_line_move_pts": round(avg_move, 2) if avg_move is not None else None,
        },
        "by_sport": sport_rows,
        "recent": evaluated[:20],
        "verdict": verdict,
    }


def my_bets(since: date | None = None) -> dict:
    """The user's logged bets AND parlays (newest first) with their P&L.

    Each bet row carries every user_bets column plus its game's matchup
    fields: home/away/league, `kickoff` (start_time as ISO-8601 UTC), and
    the game's completed flag and scores. Parlays carry their user_parlays
    columns plus a "legs" list in the same joined shape (see _parlay_rows).
    `since` filters straight bets by the game's UTC start date, matching
    roi(), and parlays by their LAST leg's kickoff date (the date the slip
    can grade).

    The pnl block aggregates the GRADED bets and parlays together — a
    parlay counts as ONE bet at its own stake: units is the signed sum,
    roi_pct is stake-weighted (units / total stake_units graded * 100 —
    pushes stake but returns nothing, mirroring roi()'s stake-vs-return
    definition), record is "W-L-P", and daily is the cumulative series
    keyed by game start date (a parlay lands on its last leg's date).
    Ratios are None (not NaN) when nothing is graded.

    Returns {"bets": [...], "parlays": [...], "pnl": {n, graded, units,
    roi_pct, record, daily: [{date, units, cum_units}...]}}.
    """
    clauses, params = [], []
    if since is not None:
        clauses.append("AND date(g.start_time) >= ?")
        params.append(since.isoformat())
    conn = db.connect()
    try:
        bets = db.rows(
            conn,
            f"""
            SELECT b.id, b.game_id, b.sport, b.side, b.team, b.price, b.book,
                   b.stake_units, b.note, b.created_at, b.result, b.units,
                   b.graded_at,
                   g.home, g.away, g.league, g.start_time AS kickoff,
                   g.completed, g.home_score, g.away_score
            FROM user_bets b
            JOIN games g ON g.game_id = b.game_id
            WHERE 1 = 1 {' '.join(clauses)}
            ORDER BY b.created_at DESC, b.id DESC
            """,
            tuple(params),
        )
        parlays = _parlay_rows(conn)
    finally:
        conn.close()

    if since is not None:
        floor = since.isoformat()
        parlays = [
            p for p in parlays if p["legs"] and _parlay_date(p) >= floor
        ]

    graded = [b for b in bets if b["result"] is not None]
    graded_parlays = [p for p in parlays if p["result"] is not None]
    all_graded = graded + graded_parlays
    units = sum(x["units"] or 0.0 for x in all_graded)
    staked = sum(x["stake_units"] for x in all_graded)
    wins = sum(1 for x in all_graded if x["result"] == "W")
    losses = sum(1 for x in all_graded if x["result"] == "L")
    pushes = sum(1 for x in all_graded if x["result"] == "P")

    day_units: dict[str, float] = defaultdict(float)
    for b in graded:
        day_units[b["kickoff"][:10]] += b["units"] or 0.0
    for p in graded_parlays:
        day_units[_parlay_date(p)] += p["units"] or 0.0
    daily, running = [], 0.0
    for day, day_total in sorted(day_units.items()):
        running += day_total
        daily.append({"date": day, "units": day_total, "cum_units": running})

    return {
        "bets": bets,
        "parlays": parlays,
        "pnl": {
            "n": len(bets) + len(parlays),
            "graded": len(all_graded),
            "units": units,
            "roi_pct": units / staked * 100.0 if staked else None,
            "record": f"{wins}-{losses}-{pushes}",
            "daily": daily,
        },
    }


def pick_log(sport: Sport | None = None, since: date | None = None) -> dict:
    """Every recorded VALUE pick (newest kickoff first) with the bot's P&L.

    The tracker page's BOT PICKS view: my_bets()'s shape over the model's own
    record instead of the user's — every +EV pick the bot has recommended
    (tier STRONG/LEAN with a book price), pending and graded alike. MODEL-tier
    picks are excluded: they carry no price, so they never stake units and
    can't be a P&L. `since` filters by the game's UTC start date, matching
    roi() and my_bets().

    Value picks grade at a flat 1u stake (see _grade_picks), so the pnl block
    mirrors my_bets(): units is the signed sum over graded picks, roi_pct is
    units / graded * 100 (1u staked per graded pick — identical to roi()'s
    overall units over these same rows), record is "W-L-P", daily is the
    cumulative series keyed by game start date. Ratios are None when nothing
    is graded.

    Each row carries the picks columns (minus the surrogate id) plus its
    game's home/away/league, `kickoff` (start_time as ISO-8601 UTC), and the
    game's completed flag and scores.

    Returns {"picks": [...], "pnl": {n, graded, units, roi_pct, record,
    daily: [{date, units, cum_units}...]}}.
    """
    clauses, params = [], []
    if sport is not None:
        clauses.append("AND k.sport = ?")
        params.append(sport.value)
    if since is not None:
        clauses.append("AND date(g.start_time) >= ?")
        params.append(since.isoformat())
    conn = db.connect()
    try:
        picks = db.rows(
            conn,
            f"""
            SELECT k.game_id, k.sport, k.model, k.side, k.team, k.book,
                   k.price, k.ev, k.kelly, k.tier, k.created_at, k.locked_at,
                   k.result, k.units, k.graded_at,
                   g.home, g.away, g.league, g.start_time AS kickoff,
                   g.completed, g.home_score, g.away_score
            FROM picks k
            JOIN games g ON g.game_id = k.game_id
            WHERE k.tier IN ('STRONG', 'LEAN') AND k.price IS NOT NULL
              {' '.join(clauses)}
            ORDER BY g.start_time DESC, k.game_id, k.model
            """,
            tuple(params),
        )
    finally:
        conn.close()

    graded = [p for p in picks if p["result"] is not None]
    units = sum(p["units"] or 0.0 for p in graded)
    wins = sum(1 for p in graded if p["result"] == "W")
    losses = sum(1 for p in graded if p["result"] == "L")
    pushes = sum(1 for p in graded if p["result"] == "P")

    day_units: dict[str, float] = defaultdict(float)
    for p in graded:
        day_units[p["kickoff"][:10]] += p["units"] or 0.0
    daily, running = [], 0.0
    for day, day_total in sorted(day_units.items()):
        running += day_total
        daily.append({"date": day, "units": day_total, "cum_units": running})

    return {
        "picks": picks,
        "pnl": {
            "n": len(picks),
            "graded": len(graded),
            "units": units,
            "roi_pct": units / len(graded) * 100.0 if graded else None,
            "record": f"{wins}-{losses}-{pushes}",
            "daily": daily,
        },
    }


# --------------------------------------------------------------- picks listing


def list_picks(
    on: date | None = None,
    sport: Sport | None = None,
    tiers: Optional[list[str]] = None,
) -> list[dict]:
    """One day's recorded picks joined with their games' matchup fields.

    `on` selects games whose start_time falls on that UTC date (start times
    are stored as ISO-8601 UTC strings, so SQLite's date() yields the UTC
    date). Default: today UTC — but a caller in another timezone (e.g. the
    Electron main process formatting Discord posts) should pass its local
    date explicitly, or the slate rolls over at UTC midnight instead of
    local midnight.

    Optional filters: `sport`, and `tiers` (matched case-insensitively,
    e.g. ["STRONG", "LEAN"]). Ordering: value picks first — STRONG then
    LEAN, each by ev descending — then MODEL picks by start_time ascending
    (the picks table stores no model_prob to rank them by).

    Each row carries every picks column (minus the surrogate id) plus the
    game's home/away/league/start_time and `kickoff`, the start_time as an
    ISO-8601 UTC string.
    """
    if on is None:
        on = datetime.now(timezone.utc).date()
    clauses, params = ["AND date(g.start_time) = ?"], [on.isoformat()]
    if sport is not None:
        clauses.append("AND k.sport = ?")
        params.append(sport.value)
    if tiers:
        wanted = [t.upper() for t in tiers]
        clauses.append(f"AND k.tier IN ({', '.join('?' * len(wanted))})")
        params.extend(wanted)
    conn = db.connect()
    try:
        return db.rows(
            conn,
            f"""
            SELECT k.game_id, k.sport, k.model, k.side, k.team, k.book,
                   k.price, k.ev, k.kelly, k.tier, k.created_at, k.locked_at,
                   k.result, k.units, k.graded_at,
                   g.home, g.away, g.league, g.start_time,
                   g.start_time AS kickoff
            FROM picks k
            JOIN games g ON g.game_id = k.game_id
            WHERE 1 = 1 {' '.join(clauses)}
            ORDER BY
                CASE k.tier WHEN 'STRONG' THEN 0 WHEN 'LEAN' THEN 1 ELSE 2 END,
                k.ev DESC,
                g.start_time
            """,
            tuple(params),
        )
    finally:
        conn.close()


# -------------------------------------------------------------- line movement


def line_movement(game_id: str) -> dict:
    """Full odds-snapshot series for one game, per book, in time order.

    Returns {"game_id": ..., "books": {book: [snapshot dicts, ordered by
    fetched_at]}} — the raw material for a UI sparkline of line movement.
    """
    conn = db.connect()
    try:
        snaps = db.rows(
            conn,
            """
            SELECT book, fetched_at, home_ml, away_ml, draw_ml,
                   total_line, over_price, under_price
            FROM odds_snapshots
            WHERE game_id = ?
            ORDER BY book, fetched_at
            """,
            (game_id,),
        )
    finally:
        conn.close()
    books: dict[str, list[dict]] = defaultdict(list)
    for snap in snaps:
        books[snap.pop("book")].append(snap)
    return {"game_id": game_id, "books": dict(books)}


# -------------------------------------------------------------------- helpers


def _filters(
    sport: Sport | None = None,
    model: str | None = None,
    since: date | None = None,
) -> tuple[str, tuple]:
    """Optional WHERE-clause suffix + params shared by the read queries."""
    clauses, params = [], []
    if sport is not None:
        clauses.append("AND g.sport = ?")
        params.append(sport.value)
    if model is not None:
        clauses.append("AND p.model = ?")
        params.append(model)
    if since is not None:
        clauses.append("AND date(g.start_time) >= ?")
        params.append(since.isoformat())
    return " ".join(clauses), tuple(params)


def _mean(values) -> float:
    items = list(values)
    return sum(items) / len(items) if items else float("nan")

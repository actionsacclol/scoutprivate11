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

from kryptscout.core import context as context_mod
from kryptscout.core.oddsmath import american_to_decimal, remove_vig, remove_vig_power
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
                    written = db.upsert_prediction(
                        conn, p.game_id, p.sport.value, p.model, p.generated_at,
                        p.home_win_prob, p.away_win_prob, p.draw_prob,
                        p.predicted_home_score, p.predicted_away_score,
                        p.predicted_total, starts_at=g.start_time,
                    )
                    if written is not None:  # None = post-kickoff, refused
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
                        fair_prob=pick.fair_prob,
                    )
                    if written is not None:  # None = frozen, left untouched
                        counts["picks"] += 1
                # Model-free signals ride along: the exchange-lead reference
                # (scored by lean_clv) and any true-arb sighting. Keys only
                # appear when something was written.
                if card.lean is not None:
                    if db.upsert_lean(conn, card.lean, g.start_time, now) is not None:
                        counts["leans"] = counts.get("leans", 0) + 1
                if card.arb is not None and db.insert_arb_sighting(conn, card.arb, now):
                    counts["arbs"] = counts.get("arbs", 0) + 1
            # Schedule-shaped context (rest days) for every game just recorded.
            written = context_mod.record_rest(conn, cards, now)
            if written:
                counts["context"] = written
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
              -- Strictly pre-game rows only: legacy rows written after
              -- kickoff (see db.upsert_prediction) would score the model on
              -- games it had partly seen.
              AND julianday(p.generated_at) < julianday(g.start_time)
              AND p.generated_at = (
                    SELECT MAX(p2.generated_at) FROM predictions p2
                    WHERE p2.game_id = p.game_id AND p2.model = p.model
                      AND julianday(p2.generated_at) < julianday(g.start_time)
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
              -- Strictly pre-game rows only: legacy rows written after
              -- kickoff (see db.upsert_prediction) would score the model on
              -- games it had partly seen.
              AND julianday(p.generated_at) < julianday(g.start_time)
              AND p.generated_at = (
                    SELECT MAX(p2.generated_at) FROM predictions p2
                    WHERE p2.game_id = p.game_id AND p2.model = p.model
                      AND julianday(p2.generated_at) < julianday(g.start_time)
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
            SELECT k.tier, k.result, k.sport AS pick_sport,
                   -- Score the FIRST-SIGHT entry when the store kept it
                   -- (db.upsert_pick); legacy rows fall back to the locked
                   -- price, which is the venue's last pre-game quote.
                   COALESCE(k.entry_side, k.side) AS side,
                   COALESCE(k.entry_price, k.price) AS price,
                   COALESCE(k.entry_book, k.book) AS entry_book,
                   (k.entry_price IS NOT NULL) AS has_entry,
                   e.*
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
    unscoreable = 0
    entry_recorded = 0
    for rec in recs:
        entry_recorded += int(bool(rec["has_entry"]))
        open_col, close_col = _SIDE_COLUMNS[rec["side"]]
        open_ml, close_ml = rec[open_col], rec[close_col]
        # The close must carry the SAME market the pick was priced on. A
        # soccer pick is a 3-way price (home/draw/away); ESPN's DraftKings
        # soccer close is 2-way (no draw). Devigging two sides against a
        # price set for three read as +36.7% "EV" at a 99.4% positive rate
        # over 166 picks (2026-09-02) and made the overall figure
        # "significant". Such a row cannot be scored in any column.
        sides_needed = _MARKET_SIDES.get(rec["pick_sport"], 2)
        opens = [p for p in (rec["open_home_ml"], rec["open_away_ml"],
                             rec["open_draw_ml"]) if p is not None]
        closes = [p for p in (rec["close_home_ml"], rec["close_away_ml"],
                              rec["close_draw_ml"]) if p is not None]
        if close_ml is None or len(closes) != sides_needed:
            unscoreable += 1
            continue
        try:
            idx = _side_index(rec, close_col)
            p_close_fair = remove_vig(*closes)[idx]
            p_close_power = remove_vig_power(*closes)[idx]
        except (ValueError, ZeroDivisionError, IndexError):
            unscoreable += 1
            continue
        drift = None
        if open_ml is not None and len(opens) == sides_needed:
            try:
                p_open = remove_vig(*opens)[_side_index(rec, open_col)]
                drift = (p_close_fair - p_open) * 100.0
            except (ValueError, ZeroDivisionError, IndexError):
                drift = None

        price_clv = fair_ev = fair_ev_power = None
        if rec["price"] is not None:
            if rec["entry_book"] == rec["book"]:
                # Same book both ends: vig cancels, this is real price improvement.
                price_clv = (_decimal(rec["price"]) / _decimal(close_ml) - 1.0) * 100.0
            else:
                # Different venues: the closing book's vig has to come out or
                # the exchange wins by construction. See the docstring. Both
                # devig methods are reported — they disagree exactly where it
                # matters (underdogs), and the power figure is the honest one.
                q_entry = 1.0 / _decimal(rec["price"])
                if q_entry > _EPS:
                    fair_ev = (p_close_fair / q_entry - 1.0) * 100.0
                    fair_ev_power = (p_close_power / q_entry - 1.0) * 100.0
        buckets[rec["tier"]].append({
            "price_clv": price_clv, "fair_ev": fair_ev,
            "fair_ev_power": fair_ev_power, "drift": drift,
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
        power = [x["fair_ev_power"] for x in items if x["fair_ev_power"] is not None]
        drifts = [x["drift"] for x in items if x["drift"] is not None]
        decided = [x for x in items if x["result"] in ("W", "L")]
        clv_mean, clv_se, clv_sig = _stats(priced)
        fair_mean, fair_se, _ = _stats(fair)
        power_mean, power_se, power_sig = _stats(power)
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
            # Same picks, power devig. Multiplicative devig over-credits
            # underdogs, so this is the figure to believe when the two differ.
            "fair_ev_power_pct": power_mean,
            "fair_ev_power_se": power_se,
            "positive_ev_power_rate": (
                sum(1 for f in power if f > 0) / len(power) if power else None
            ),
            "drift_prob_pts": _mean(drifts) if drifts else None,
            "win_rate": (
                sum(1 for x in decided if x["result"] == "W") / len(decided)
                if decided else None
            ),
            # Significance is only ever claimed on the conservative devig.
            "significant": bool(clv_sig or power_sig),
        }

    everything = [x for items in buckets.values() for x in items]
    return {
        "by_tier": {t: summarize(buckets.get(t, [])) for t in ("STRONG", "LEAN", "MODEL")},
        "overall": summarize(everything),
        "n_unscoreable": unscoreable,
        # Rows scored at their first-sight entry price rather than the locked
        # (last pre-game) price. Legacy rows are close-vs-close comparisons.
        "n_entry_recorded": entry_recorded,
        "note": (
            "price_clv_pct compares entry to the SAME book's close (vig "
            "cancels). fair_ev_pct is for picks whose entry venue differs from "
            "the closing book — exchange prices beat a sportsbook's close by "
            "construction, so that column devigs the close first. "
            "fair_ev_power_pct is the same comparison under power devig, which "
            "does not over-credit underdogs; trust it over fair_ev_pct when "
            "they disagree. Never read the columns as one number. Picks whose "
            "close does not carry their full market (3-way soccer vs a 2-way "
            "close) are counted in n_unscoreable and excluded everywhere. "
            "Needs ~30+ per tier before 'significant' can trip; run "
            "`python -m kryptscout.jobs.lines` for more games."
        ),
    }


# Sides the pick's own market carries. Anything not listed is a two-way
# moneyline; soccer picks are priced on a 3-way market and need a 3-way close.
_MARKET_SIDES = {"soccer": 3}


def _score_home(p: float, home_won: bool) -> dict:
    q = min(1.0 - 1e-9, max(1e-9, p))
    y = 1.0 if home_won else 0.0
    return {
        "log_loss": -math.log(q if home_won else 1.0 - q),
        "brier": (q - y) ** 2,
        "correct": 1.0 if (q >= 0.5) == home_won else 0.0,
    }


def _summ(items: list[dict]) -> Optional[dict]:
    if not items:
        return None
    return {
        "n": len(items),
        "log_loss": _mean(x["log_loss"] for x in items),
        "brier": _mean(x["brier"] for x in items),
        "accuracy": _mean(x["correct"] for x in items),
    }


def context_trackers(min_umpire_n: int = 10) -> dict:
    """Score the schedule-shaped trackers against the market.

    soccer_rest   completed soccer games where exactly one side was on short
                  rest (core.context.short_rest_side), scored as a bet on
                  that side at the last pre-game 3-way reference (Polymarket
                  first, else any book quoting a draw): win rate, flat ROI,
                  and the residual = outcome − reference probability. The
                  cell that measured +7 to +11 pts vs Pinnacle; it needs
                  n≈300 before it means anything here.
    mlb_umpires   actual total − the last pre-game DraftKings total, by
                  home-plate umpire (n ≥ 10 listed), plus how many hours
                  before first pitch the umpire and the lineups first
                  appeared.
    nba_referees  placeholder until a verified feed exists.
    """
    conn = db.connect()
    try:
        soccer = db.rows(
            conn,
            """
            SELECT g.game_id, g.start_time, g.home_score, g.away_score,
                   c.rest_home_days, c.rest_away_days,
                   (SELECT s.home_ml FROM odds_snapshots s WHERE s.game_id = g.game_id AND s.draw_ml IS NOT NULL
                      AND s.home_ml IS NOT NULL AND s.away_ml IS NOT NULL
                      AND julianday(s.fetched_at) < julianday(g.start_time)
                    ORDER BY (s.book = 'polymarket') DESC, s.fetched_at DESC LIMIT 1) AS ref_home,
                   (SELECT s.away_ml FROM odds_snapshots s WHERE s.game_id = g.game_id AND s.draw_ml IS NOT NULL
                      AND s.home_ml IS NOT NULL AND s.away_ml IS NOT NULL
                      AND julianday(s.fetched_at) < julianday(g.start_time)
                    ORDER BY (s.book = 'polymarket') DESC, s.fetched_at DESC LIMIT 1) AS ref_away,
                   (SELECT s.draw_ml FROM odds_snapshots s WHERE s.game_id = g.game_id AND s.draw_ml IS NOT NULL
                      AND s.home_ml IS NOT NULL AND s.away_ml IS NOT NULL
                      AND julianday(s.fetched_at) < julianday(g.start_time)
                    ORDER BY (s.book = 'polymarket') DESC, s.fetched_at DESC LIMIT 1) AS ref_draw,
                   (SELECT s.book FROM odds_snapshots s WHERE s.game_id = g.game_id AND s.draw_ml IS NOT NULL
                      AND s.home_ml IS NOT NULL AND s.away_ml IS NOT NULL
                      AND julianday(s.fetched_at) < julianday(g.start_time)
                    ORDER BY (s.book = 'polymarket') DESC, s.fetched_at DESC LIMIT 1) AS ref_book
            FROM games g JOIN game_context c ON c.game_id = g.game_id
            WHERE g.sport = 'soccer' AND g.completed = 1
              AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
              AND c.rest_home_days IS NOT NULL AND c.rest_away_days IS NOT NULL
            """,
        )
        mlb = db.rows(
            conn,
            """
            SELECT g.game_id, g.start_time, g.home_score, g.away_score,
                   c.umpire_name, c.umpire_seen_at, c.lineups_seen_at,
                   (SELECT s.total_line FROM odds_snapshots s WHERE s.game_id = g.game_id
                      AND s.book = 'draftkings' AND s.total_line IS NOT NULL
                      AND julianday(s.fetched_at) < julianday(g.start_time)
                    ORDER BY s.fetched_at DESC LIMIT 1) AS close_total
            FROM games g JOIN game_context c ON c.game_id = g.game_id
            WHERE g.sport = 'mlb' AND g.completed = 1
              AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
              AND c.umpire_name IS NOT NULL
            """,
        )
    finally:
        conn.close()

    # ---- soccer short rest
    units: list[float] = []
    residuals: list[float] = []
    wins = 0
    for rec in soccer:
        side = context_mod.short_rest_side(rec["rest_home_days"], rec["rest_away_days"])
        if side is None or rec["ref_home"] is None or rec["ref_away"] is None or rec["ref_draw"] is None:
            continue
        prices = [rec["ref_home"], rec["ref_away"], rec["ref_draw"]]
        try:
            fair = remove_vig(*prices) if rec["ref_book"] in ("polymarket", "kalshi") else remove_vig_power(*prices)
        except (ValueError, ZeroDivisionError):
            continue
        idx = 0 if side == "home" else 1
        won = (rec["home_score"] > rec["away_score"]) if side == "home" else (rec["away_score"] > rec["home_score"])
        wins += int(won)
        units.append((american_to_decimal(prices[idx]) - 1.0) if won else -1.0)
        residuals.append((1.0 if won else 0.0) - fair[idx])
    n = len(units)
    res_mean = _mean(residuals) if residuals else None
    res_se = None
    if n >= 2:
        var = sum((v - res_mean) ** 2 for v in residuals) / (n - 1)
        res_se = (var ** 0.5) / (n ** 0.5)
    soccer_out = {
        "n": n,
        "win_rate": (wins / n) if n else None,
        "roi_pct": (sum(units) / n * 100.0) if n else None,
        "residual_pts": (res_mean * 100.0) if res_mean is not None else None,
        "residual_se": (res_se * 100.0) if res_se is not None else None,
        "significant": bool(n >= 30 and res_se and abs(res_mean / res_se) >= 2.0),
        "target_n": 300,
    }

    # ---- MLB umpires + posting timing
    by_ump: dict[str, list[float]] = defaultdict(list)
    all_res: list[float] = []
    ump_leads: list[float] = []
    lineup_leads: list[float] = []
    for rec in mlb:
        start = datetime.fromisoformat(rec["start_time"])
        for key, sink in (("umpire_seen_at", ump_leads), ("lineups_seen_at", lineup_leads)):
            if rec[key]:
                seen = datetime.fromisoformat(rec[key])
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                sink.append((start - seen).total_seconds() / 3600.0)
        if rec["close_total"] is None:
            continue
        residual = (rec["home_score"] + rec["away_score"]) - float(rec["close_total"])
        by_ump[rec["umpire_name"]].append(residual)
        all_res.append(residual)

    def _median(xs: list[float]) -> Optional[float]:
        if not xs:
            return None
        s = sorted(xs)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0

    umpires = []
    for name, vals in by_ump.items():
        if len(vals) < min_umpire_n:
            continue
        m = _mean(vals)
        se = ((sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 / len(vals) ** 0.5) if len(vals) > 1 else None
        umpires.append({"umpire": name, "n": len(vals), "residual_runs": m, "residual_se": se})
    umpires.sort(key=lambda u: -abs(u["residual_runs"]))
    mlb_out = {
        "n": len(all_res),
        "residual_runs": _mean(all_res) if all_res else None,
        "by_umpire": umpires,
        "umpire_lead_hours_median": _median(ump_leads),
        "lineups_lead_hours_median": _median(lineup_leads),
    }
    return {
        "soccer_rest": soccer_out,
        "mlb_umpires": mlb_out,
        "nba_referees": {"n": 0, "note": "No verified referee-assignment feed yet; the column exists, capture starts with the season."},
        "note": (
            "Schedule-shaped trackers scored against the market, never modelled. "
            "soccer_rest bets the short-rest side at the last pre-game 3-way reference; "
            "mlb_umpires is the actual total minus the last pre-game DraftKings total by "
            "home-plate umpire (n >= 10 listed). Expected forecast gain: zero; the soccer "
            "cell is the one large effect found and needs ~300 games to confirm or retire."
        ),
    }


def espn_benchmark(sport: Sport | None = None, since: date | None = None) -> dict:
    """Scout's pre-game forecast vs ESPN's predictor vs the market, same games.

    Rows: decided completed games with a PRE-GAME ESPN document. Scout's
    number is the last pre-game `predictions` row, falling back to the
    recorded pick's model probability (the picks table was clean when the
    predictions table was not). The exchange is the last pre-game Kalshi or
    Polymarket quote; the close is DraftKings' declared close, power-devigged.
    Each source is scored on the games where it exists, and ESPN vs Scout
    head-to-head only where both do. Measured 2026-09-02: MLB level (Scout
    0.6711 vs ESPN 0.6748), WNBA behind (0.5250 vs 0.4996); nothing beats
    the close.
    """
    clauses, params = [], []
    if sport is not None:
        clauses.append("AND g.sport = ?")
        params.append(sport.value)
    if since is not None:
        clauses.append("AND date(g.start_time) >= ?")
        params.append(since.isoformat())
    conn = db.connect()
    try:
        recs = db.rows(
            conn,
            f"""
            SELECT g.game_id, g.sport, g.home_score, g.away_score, e.home_prob AS espn,
                   (SELECT p.home_win_prob FROM predictions p
                     WHERE p.game_id = g.game_id
                       AND julianday(p.generated_at) < julianday(g.start_time)
                     ORDER BY p.generated_at DESC LIMIT 1) AS scout_pred,
                   (SELECT CASE k.side WHEN 'home' THEN k.model_prob WHEN 'away' THEN 1.0 - k.model_prob END
                      FROM picks k WHERE k.game_id = g.game_id AND k.model != 'market-consensus'
                       AND k.model_prob IS NOT NULL
                       AND julianday(k.created_at) < julianday(g.start_time)
                     ORDER BY k.created_at LIMIT 1) AS scout_pick,
                   le.close_home_ml, le.close_away_ml,
                   (SELECT s.home_ml FROM odds_snapshots s
                     WHERE s.game_id = g.game_id AND s.book IN ('kalshi', 'polymarket')
                       AND s.home_ml IS NOT NULL AND s.away_ml IS NOT NULL
                       AND julianday(s.fetched_at) < julianday(g.start_time)
                     ORDER BY s.fetched_at DESC LIMIT 1) AS ex_home,
                   (SELECT s.away_ml FROM odds_snapshots s
                     WHERE s.game_id = g.game_id AND s.book IN ('kalshi', 'polymarket')
                       AND s.home_ml IS NOT NULL AND s.away_ml IS NOT NULL
                       AND julianday(s.fetched_at) < julianday(g.start_time)
                     ORDER BY s.fetched_at DESC LIMIT 1) AS ex_away
            FROM games g
            JOIN espn_predictions e ON e.game_id = g.game_id AND e.pre_game = 1
            LEFT JOIN line_endpoints le ON le.game_id = g.game_id AND le.book = 'draftkings'
            WHERE g.completed = 1 AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
              AND g.home_score != g.away_score
              {' '.join(clauses)}
            """,
            tuple(params),
        )
    finally:
        conn.close()

    # Every comparison is PAIRED: a comparator is scored only on the games it
    # shares with ESPN, and ESPN is re-scored on exactly those games. Scoring
    # each source on its own subset makes the columns incomparable — the
    # first cut of this table had Scout "beating" the close on 184 easy
    # games while the close was scored on 444.
    by_sport: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for rec in recs:
        home_won = rec["home_score"] > rec["away_score"]
        bucket = by_sport[rec["sport"]]
        espn = _score_home(rec["espn"], home_won)
        bucket["espn"].append(espn)
        scout = rec["scout_pred"] if rec["scout_pred"] is not None else rec["scout_pick"]
        if scout is not None:
            bucket["scout"].append((espn, _score_home(scout, home_won)))
        if rec["close_home_ml"] is not None and rec["close_away_ml"] is not None:
            try:
                p_close = remove_vig_power(rec["close_home_ml"], rec["close_away_ml"])[0]
                bucket["close"].append((espn, _score_home(p_close, home_won)))
            except (ValueError, ZeroDivisionError):
                pass
        if rec["ex_home"] is not None and rec["ex_away"] is not None:
            try:
                p_ex = remove_vig(rec["ex_home"], rec["ex_away"])[0]
                bucket["exchange"].append((espn, _score_home(p_ex, home_won)))
            except (ValueError, ZeroDivisionError):
                pass

    def paired(pairs: list[tuple[dict, dict]]) -> Optional[dict]:
        if not pairs:
            return None
        return {
            "n": len(pairs),
            "log_loss": _mean(x["log_loss"] for _, x in pairs),
            "espn_log_loss": _mean(e["log_loss"] for e, _ in pairs),
            "brier": _mean(x["brier"] for _, x in pairs),
            "accuracy": _mean(x["correct"] for _, x in pairs),
            "espn_accuracy": _mean(e["correct"] for e, _ in pairs),
            # Share of common games where the comparator's log loss beat ESPN's.
            "beats_espn": sum(1 for e, x in pairs if x["log_loss"] < e["log_loss"]) / len(pairs),
        }

    def row(key: str, bucket: dict[str, list]) -> dict:
        return {
            "sport": key,
            "n": len(bucket["espn"]),
            "espn": _summ(bucket["espn"]),
            "scout": paired(bucket["scout"]),
            "exchange": paired(bucket["exchange"]),
            "close": paired(bucket["close"]),
        }

    everything: dict[str, list] = defaultdict(list)
    for bucket in by_sport.values():
        for key, items in bucket.items():
            everything[key].extend(items)
    return {
        "overall": row("all", everything),
        "by_sport": [row(k, v) for k, v in sorted(by_sport.items())],
        "note": (
            "Log loss (lower is better). Each comparator — Scout's pre-game "
            "forecast, the last pre-game exchange quote, DraftKings' devigged "
            "close — is scored on the games it shares with ESPN, and ESPN is "
            "re-scored on exactly those games, so every pair is like for like. "
            "ESPN documents modified after kickoff are excluded. Run "
            "`python -m kryptscout.jobs.predictor` to backfill history."
        ),
    }


def lean_clv(sport: Sport | None = None, since: date | None = None) -> dict:
    """Did the exchange-lead reference beat DraftKings' own close?

    Scores every frozen lean (store.lean_signals) at its FIRST-SIGHT entry
    against the same book's declared close from `line_endpoints` — same
    book both ends, so the vig cancels and neither the venue nor the devig
    confound applies. Three numbers per group:

      clv_pct   (dec_entry / dec_close − 1) × 100 on the lean side. The
                measurement that justified the signal was +1.05% ± 0.30.
      hit_rate  share of lines that moved TOWARD the lean side by the close
                (power-devigged), over lines that moved at all. 64% measured;
                the naive "favourite shortens" rule gets 57%.
      roi_pct   flat 1u at the entry price, for context only — at ~1% CLV
                against a ~2.2% per-side vig it is expected to sit at zero.

    Significance needs n ≥ 30 and |mean/se| ≥ 2 on clv_pct; the bar for
    feeding this into anything that bets is n ≥ 70 AND clv above the vig.
    """
    clauses, params = [], []
    if sport is not None:
        clauses.append("AND l.sport = ?")
        params.append(sport.value)
    if since is not None:
        clauses.append("AND date(g.start_time) >= ?")
        params.append(since.isoformat())
    conn = db.connect()
    try:
        recs = db.rows(
            conn,
            f"""
            SELECT l.game_id, l.sport, l.entry_side AS side, l.entry_home_ml,
                   l.entry_away_ml, l.entry_ref_book AS ref_book, l.entry_gap_pts,
                   l.entry_at, g.start_time, g.home_score, g.away_score,
                   e.close_home_ml, e.close_away_ml
            FROM lean_signals l
            JOIN games g ON g.game_id = l.game_id
            JOIN line_endpoints e ON e.game_id = l.game_id AND e.book = l.book
            WHERE g.completed = 1
              AND e.close_home_ml IS NOT NULL AND e.close_away_ml IS NOT NULL
              AND julianday(l.entry_at) < julianday(g.start_time)
              {' '.join(clauses)}
            ORDER BY g.start_time
            """,
            tuple(params),
        )
    finally:
        conn.close()

    scored: list[dict] = []
    for rec in recs:
        idx = 0 if rec["side"] == "home" else 1
        entry_pair = (rec["entry_home_ml"], rec["entry_away_ml"])
        close_pair = (rec["close_home_ml"], rec["close_away_ml"])
        try:
            p_entry = remove_vig_power(*entry_pair)[idx]
            p_close = remove_vig_power(*close_pair)[idx]
        except (ValueError, ZeroDivisionError):
            continue
        entry_price, close_price = entry_pair[idx], close_pair[idx]
        clv = (_decimal(entry_price) / _decimal(close_price) - 1.0) * 100.0
        moved = abs(p_close - p_entry) > 1e-9
        won = None
        if rec["home_score"] is not None and rec["away_score"] is not None:
            if rec["home_score"] != rec["away_score"]:
                won = (rec["home_score"] > rec["away_score"]) == (rec["side"] == "home")
        units = None
        if won is not None:
            units = (_decimal(entry_price) - 1.0) if won else -1.0
        scored.append({
            "sport": rec["sport"], "ref_book": rec["ref_book"], "clv": clv,
            "moved": moved, "toward": moved and p_close > p_entry,
            "units": units,
        })

    def summarize(items: list[dict]) -> dict:
        clvs = [x["clv"] for x in items]
        moved = [x for x in items if x["moved"]]
        staked = [x["units"] for x in items if x["units"] is not None]
        mean = _mean(clvs) if clvs else None
        se = None
        if len(clvs) >= 2:
            var = sum((v - mean) ** 2 for v in clvs) / (len(clvs) - 1)
            se = (var ** 0.5) / (len(clvs) ** 0.5)
        return {
            "n": len(items),
            "clv_pct": mean,
            "clv_se": se,
            "beat_close_rate": (sum(1 for v in clvs if v > 0) / len(clvs)) if clvs else None,
            "n_moved": len(moved),
            "hit_rate": (sum(1 for x in moved if x["toward"]) / len(moved)) if moved else None,
            "roi_pct": (sum(staked) / len(staked) * 100.0) if staked else None,
            "significant": bool(
                len(clvs) >= 30 and se and abs(mean / se) >= 2.0
            ),
        }

    by_sport: dict[str, list[dict]] = defaultdict(list)
    by_ref: dict[str, list[dict]] = defaultdict(list)
    for x in scored:
        by_sport[x["sport"]].append(x)
        by_ref[x["ref_book"]].append(x)
    return {
        "overall": summarize(scored),
        "by_sport": [{"sport": k, **summarize(v)} for k, v in sorted(by_sport.items())],
        "by_reference": [{"ref_book": k, **summarize(v)} for k, v in sorted(by_ref.items())],
        "note": (
            "Same-book closing-line value of the exchange-lead reference: the "
            "DraftKings price at first sight vs DraftKings' declared close on "
            "the side Kalshi/Polymarket favoured. +1.05% ± 0.30 and a 64% "
            "direction hit rate justified recording it (2026-09-02); that is "
            "under the book's vig, so roi_pct is expected near zero. Not a "
            "pick. Run `python -m kryptscout.jobs.lines` to score more games."
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

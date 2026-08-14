"""SQLite persistence layer for the prediction-history store.

Plain stdlib sqlite3 — no ORM. The database lives at DATA_DIR/kryptscout.sqlite
in WAL mode; the schema is applied idempotently on every connect (CREATE TABLE
IF NOT EXISTS), so there is no separate migration step.

Tables
------
games           one row per scheduled event (game_id is the engine id, e.g.
                "nba-401766123"); scores/completed filled at settlement, and
                settled_at doubles as the "no longer pending" marker — an
                abandoned game has settled_at set but completed=0.
predictions     model outputs, deduped to at most one row per
                (game_id, model) per hour: re-recording within an hour
                updates the existing row in place instead of adding one.
odds_snapshots  the per-(game, book) price series behind line movement and
                CLV, bucketed to at most one row per
                SNAPSHOT_MIN_GAP_MINUTES: a capture inside a bucket refreshes
                that bucket's row in place (freshest quote wins) instead of
                appending. The board endpoint records on every read and the
                renderer polls all nine sports every 60s, so an unbucketed
                append grew >1,300 rows for a single game.
picks           the recommended bet per (game, model) — derived from the same
                cards the terminal renders (store.picks mirrors src/picks.ts).
                Re-recording BEFORE the game's start_time refreshes the row in
                place (created_at preserved); once start_time has passed the
                pick is frozen (locked_at set, no further updates), then
                settlement grades it: result 'W'|'L'|'P' (push/abandoned) and
                units at a flat 1u stake for value picks (NULL for MODEL-tier
                picks, which carry no price).

user_bets        the user's OWN logged bets — distinct from picks, which is
                the automatic model record. Multiple rows per game are fine
                (users genuinely double-bet), but an exact ungraded duplicate
                (same game/side/book/price) is treated as a double-submit and
                returned instead of re-inserted. Settlement grades them like
                picks — result 'W'|'L'|'P' — at the bet's own stake_units
                instead of a flat 1u.

auto_bets       every order attempt the Polymarket autobet engine made (one
                row per attempt: dry / placed / unmatched / error). The
                engine's dedupe + daily caps read this table; a PLACED bet is
                also logged to user_bets (note "AUTO · polymarket") so it
                grades into the tracker like a hand-tracked bet.

user_parlays     the user's OWN tracked parlays (built on the PARLAY page),
user_parlay_legs one leg row per game in the slip. Legs grade individually as
                their games settle (result 'W'|'L'|'P' by the same outcome
                rules as straight bets); once every leg has a result the
                parlay grades: any lost leg loses the slip (-stake), pushed
                legs drop out of the payout (their factor is 1 — the stake
                rides), all-pushed slips push. `price` on the parlay row is
                the combined American price at track time; leg prices are
                what the payout is actually computed from.

All timestamps are stored as ISO-8601 UTC strings ("...+00:00"), which sort
lexicographically in chronological order and are accepted by SQLite's
julianday()/date() functions.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from kryptscout.config import DATA_DIR

# Tests monkeypatch this module attribute to point at a temp database.
DB_PATH: Path = DATA_DIR / "kryptscout.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id     TEXT PRIMARY KEY,
    sport       TEXT NOT NULL,
    league      TEXT NOT NULL DEFAULT '',
    start_time  TEXT NOT NULL,
    home        TEXT NOT NULL,
    away        TEXT NOT NULL,
    home_score  INTEGER,
    away_score  INTEGER,
    completed   INTEGER NOT NULL DEFAULT 0,
    settled_at  TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id              TEXT NOT NULL,
    sport                TEXT NOT NULL,
    model                TEXT NOT NULL,
    generated_at         TEXT NOT NULL,
    home_win_prob        REAL NOT NULL,
    away_win_prob        REAL NOT NULL,
    draw_prob            REAL,
    predicted_home_score REAL,
    predicted_away_score REAL,
    predicted_total      REAL,
    UNIQUE (game_id, model, generated_at)
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     TEXT NOT NULL,
    sport       TEXT NOT NULL,
    book        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    home_ml     INTEGER,
    away_ml     INTEGER,
    draw_ml     INTEGER,
    total_line  REAL,
    over_price  INTEGER,
    under_price INTEGER,
    UNIQUE (game_id, book, fetched_at)
);

-- A book's own declared OPEN and CLOSE for a game — the pair closing-line
-- value is defined against. Not derivable from odds_snapshots: that table only
-- holds prices we happened to be running for, and CLV needs both ends of the
-- series. ESPN serves both long after a game settles, so this table backfills
-- (jobs/lines.py) instead of having to accrue. One row per (game, book).
CREATE TABLE IF NOT EXISTS line_endpoints (
    game_id       TEXT NOT NULL,
    sport         TEXT NOT NULL,
    book          TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    open_home_ml  INTEGER,
    open_away_ml  INTEGER,
    open_draw_ml  INTEGER,
    close_home_ml INTEGER,
    close_away_ml INTEGER,
    close_draw_ml INTEGER,
    PRIMARY KEY (game_id, book)
);

CREATE TABLE IF NOT EXISTS picks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id    TEXT NOT NULL,
    sport      TEXT NOT NULL,
    model      TEXT NOT NULL,
    side       TEXT NOT NULL,
    team       TEXT NOT NULL,
    book       TEXT,
    price      INTEGER,
    ev         REAL,
    kelly      REAL,
    model_prob REAL,
    tier       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    locked_at  TEXT,
    result     TEXT,
    units      REAL,
    graded_at  TEXT,
    UNIQUE (game_id, model)
);

CREATE TABLE IF NOT EXISTS user_bets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     TEXT NOT NULL,
    sport       TEXT NOT NULL,
    side        TEXT NOT NULL,
    team        TEXT NOT NULL,
    price       INTEGER NOT NULL,
    book        TEXT,
    stake_units REAL NOT NULL DEFAULT 1.0,
    note        TEXT,
    created_at  TEXT NOT NULL,
    result      TEXT,
    units       REAL,
    graded_at   TEXT
);

CREATE TABLE IF NOT EXISTS user_parlays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stake_units REAL NOT NULL DEFAULT 1.0,
    price       INTEGER NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL,
    result      TEXT,
    units       REAL,
    graded_at   TEXT
);

CREATE TABLE IF NOT EXISTS user_parlay_legs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parlay_id INTEGER NOT NULL,
    game_id   TEXT NOT NULL,
    sport     TEXT NOT NULL,
    side      TEXT NOT NULL,
    team      TEXT NOT NULL,
    price     INTEGER NOT NULL,
    book      TEXT,
    result    TEXT
);

CREATE TABLE IF NOT EXISTS auto_bets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      TEXT NOT NULL,
    sport        TEXT NOT NULL,
    side         TEXT NOT NULL,
    team         TEXT NOT NULL,
    tier         TEXT NOT NULL,
    ev           REAL,
    model_prob   REAL,
    condition_id TEXT NOT NULL,
    clob_side    TEXT NOT NULL,
    slug         TEXT,
    limit_prob   REAL NOT NULL,
    shares       INTEGER NOT NULL,
    usd          REAL NOT NULL,
    order_id     TEXT,
    status       TEXT NOT NULL,
    error        TEXT,
    created_at   TEXT NOT NULL
);

-- Backfilled Polymarket history (jobs/backfill.py): one row per CLOSED
-- moneyline market pulled from Gamma, plus pre-kickoff price snapshots from
-- CLOB prices-history. Feeds the dollar-ROI strategy backtest.
CREATE TABLE IF NOT EXISTS pm_markets (
    condition_id TEXT PRIMARY KEY,
    sport        TEXT NOT NULL,      -- Scout sport key (mlb, nba, ...)
    slug         TEXT,
    game_start   TEXT,               -- ISO UTC kickoff (gameStartTime)
    outcome_a    TEXT NOT NULL,      -- team display name (outcomes[0])
    outcome_b    TEXT NOT NULL,
    token_a      TEXT NOT NULL,      -- clobTokenIds[0] — the side we price
    token_b      TEXT NOT NULL,
    winner       TEXT,               -- 'a' | 'b' (from outcomePrices), NULL unresolved
    volume       REAL,
    prices_fetched INTEGER NOT NULL DEFAULT 0  -- snapshot pass done
);

-- Price of outcome_a at kickoff-minus-offset, from the last trade at or
-- before that instant. at_ts records the trade's actual time so staleness
-- can be gated at query time.
CREATE TABLE IF NOT EXISTS pm_price_snapshots (
    condition_id   TEXT NOT NULL,
    offset_minutes INTEGER NOT NULL,  -- 15, 60, 360, 1440 before kickoff
    price_a        REAL NOT NULL,
    at_ts          TEXT NOT NULL,
    PRIMARY KEY (condition_id, offset_minutes)
);

-- REAL order book for outcome_a at kickoff-minus-offset, harvested from the
-- pmxt public archive (jobs/bookfill.py). Where a row exists, the backtest
-- can fill at the actual executable ask instead of last-trade + guessed
-- ticks. Side b derives from the complement: ask_b = 1 - best_bid_a.
CREATE TABLE IF NOT EXISTS pm_book_snapshots (
    condition_id   TEXT NOT NULL,
    offset_minutes INTEGER NOT NULL,
    best_bid_a     REAL,
    best_ask_a     REAL,
    bid_depth_a    TEXT,              -- top-3 [price, size] JSON, best first
    ask_depth_a    TEXT,
    at_ts          TEXT NOT NULL,     -- book event's actual time (staleness gate)
    PRIMARY KEY (condition_id, offset_minutes)
);

-- One row per candidate the autobet sweep evaluated far enough to resolve a
-- live market: the pick's stale book price vs the venue's live best ask and
-- crossable depth. This is the ground truth that replaces the backtest's
-- guessed FILL_TICKS padding once enough rows accrue — logged for every
-- candidate (skips and dry runs included), not just placed orders.
CREATE TABLE IF NOT EXISTS book_observations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ts          TEXT NOT NULL,
    sport          TEXT NOT NULL,
    game_id        TEXT NOT NULL,
    condition_id   TEXT NOT NULL,
    clob_side      TEXT NOT NULL,
    pick_prob      REAL,              -- market-implied prob of the pick's book price
    live_prob      REAL,              -- venue mid at observation (MarketRef.prob)
    best_ask_cents INTEGER,
    depth_shares   REAL,              -- ask size at-or-below our limit price
    limit_cents    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pm_markets_sport ON pm_markets (sport, game_start);
CREATE INDEX IF NOT EXISTS idx_pm_markets_unfetched ON pm_markets (prices_fetched);
CREATE INDEX IF NOT EXISTS idx_book_obs_sport ON book_observations (sport, at_ts);

CREATE INDEX IF NOT EXISTS idx_predictions_game ON predictions (game_id, model);
CREATE INDEX IF NOT EXISTS idx_parlay_legs_parlay ON user_parlay_legs (parlay_id);
CREATE INDEX IF NOT EXISTS idx_user_parlays_ungraded ON user_parlays (result);
CREATE INDEX IF NOT EXISTS idx_auto_bets_game ON auto_bets (game_id, side);
CREATE INDEX IF NOT EXISTS idx_snapshots_game ON odds_snapshots (game_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_games_pending ON games (sport, settled_at);
CREATE INDEX IF NOT EXISTS idx_picks_ungraded ON picks (sport, result);
CREATE INDEX IF NOT EXISTS idx_user_bets_ungraded ON user_bets (sport, result);
"""


def iso(dt: datetime) -> str:
    """Normalize a datetime to an ISO-8601 UTC string for storage."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the store, applying schema and WAL mode; caller closes it."""
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for DBs created before a column existed.

    CREATE TABLE IF NOT EXISTS never alters an existing table, so a store
    written by an older build is missing newer columns. Each entry adds the
    column when absent — cheap, idempotent, safe on every connect.
    """
    additions = {
        "picks": [("model_prob", "REAL")],  # recorded so strategy backtests can accrue
        # Gamma question text ("Will Arsenal win ...?") — soccer markets are
        # binary Yes/No with the team only named here and in the slug.
        # books_fetched: pm_book_snapshots pass done (jobs/bookfill.py).
        "pm_markets": [("question", "TEXT"),
                       ("books_fetched", "INTEGER NOT NULL DEFAULT 0")],
        # Totals bets: the O/U line the bet grades against (side over/under).
        "user_bets": [("line", "REAL")],
        # Execution ground truth per order attempt: the live book seen at
        # placement (best_ask_cents/book_depth), the fee rate actually applied
        # to the EV gate, and the RECONCILED real fill (fill_shares/fill_prob
        # from the CLOB trades feed) — FAK partials must not grade as full.
        "auto_bets": [("best_ask_cents", "INTEGER"), ("book_depth", "REAL"),
                      ("fee_rate", "REAL"), ("fill_shares", "INTEGER"),
                      ("fill_prob", "REAL")],
    }
    for table, cols in additions.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


# ------------------------------------------------------------------ writers


def upsert_game(
    conn: sqlite3.Connection,
    game_id: str,
    sport: str,
    league: str,
    start_time: datetime,
    home: str,
    away: str,
    home_score: Optional[int] = None,
    away_score: Optional[int] = None,
    completed: bool = False,
) -> None:
    """Insert or refresh a game row; never clears scores or settled_at."""
    conn.execute(
        """
        INSERT INTO games (game_id, sport, league, start_time, home, away,
                           home_score, away_score, completed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (game_id) DO UPDATE SET
            league     = excluded.league,
            start_time = excluded.start_time,
            home       = excluded.home,
            away       = excluded.away,
            home_score = COALESCE(excluded.home_score, games.home_score),
            away_score = COALESCE(excluded.away_score, games.away_score),
            completed  = MAX(games.completed, excluded.completed)
        """,
        (game_id, sport, league, iso(start_time), home, away,
         home_score, away_score, int(completed)),
    )


def upsert_prediction(
    conn: sqlite3.Connection,
    game_id: str,
    sport: str,
    model: str,
    generated_at: datetime,
    home_win_prob: float,
    away_win_prob: float,
    draw_prob: Optional[float] = None,
    predicted_home_score: Optional[float] = None,
    predicted_away_score: Optional[float] = None,
    predicted_total: Optional[float] = None,
) -> bool:
    """Record a prediction, deduping within the hour.

    If a row for the same (game_id, model) exists with generated_at less than
    one hour away, that row is updated in place (its generated_at moves to the
    new timestamp) rather than a new row being inserted. Returns True when a
    new row was created, False when an existing row was refreshed.
    """
    ts = iso(generated_at)
    existing = conn.execute(
        """
        SELECT id FROM predictions
        WHERE game_id = ? AND model = ?
          AND ABS(julianday(generated_at) - julianday(?)) < 1.0 / 24.0
        ORDER BY generated_at DESC LIMIT 1
        """,
        (game_id, model, ts),
    ).fetchone()
    values = (
        ts, home_win_prob, away_win_prob, draw_prob,
        predicted_home_score, predicted_away_score, predicted_total,
    )
    if existing:
        conn.execute(
            """
            UPDATE predictions SET
                generated_at = ?, home_win_prob = ?, away_win_prob = ?,
                draw_prob = ?, predicted_home_score = ?,
                predicted_away_score = ?, predicted_total = ?
            WHERE id = ?
            """,
            values + (existing["id"],),
        )
        return False
    conn.execute(
        """
        INSERT INTO predictions (game_id, sport, model, generated_at,
                                 home_win_prob, away_win_prob, draw_prob,
                                 predicted_home_score, predicted_away_score,
                                 predicted_total)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (game_id, model, generated_at) DO UPDATE SET
            home_win_prob = excluded.home_win_prob,
            away_win_prob = excluded.away_win_prob,
            draw_prob = excluded.draw_prob,
            predicted_home_score = excluded.predicted_home_score,
            predicted_away_score = excluded.predicted_away_score,
            predicted_total = excluded.predicted_total
        """,
        (game_id, sport, model) + values,
    )
    return True


# Minimum spacing between stored snapshots of the same (game, book). The
# capture job runs every ~4h, but GET /api/board/{sport} ALSO records, and the
# renderer polls all nine boards every 60s — which appended a row per book per
# game per minute (measured: 1,313 rows for one MLB game, 41,943 for 284
# games). Line movement and CLV need a series, not a per-minute tape, so a
# snapshot inside this window refreshes the existing row in place instead of
# appending. Mirrors upsert_prediction's hourly dedupe.
SNAPSHOT_MIN_GAP_MINUTES = 15.0


def upsert_line_endpoints(
    conn: sqlite3.Connection,
    game_id: str,
    sport: str,
    book: str,
    fetched_at: datetime,
    open_home_ml: Optional[int] = None,
    open_away_ml: Optional[int] = None,
    open_draw_ml: Optional[int] = None,
    close_home_ml: Optional[int] = None,
    close_away_ml: Optional[int] = None,
    close_draw_ml: Optional[int] = None,
) -> None:
    """Record a book's open/close pair for a game, replacing any prior row.

    Replace rather than ignore: a game re-fetched while still open carries a
    provisional close that the real one should overwrite. The endpoints are a
    property of the game, not a time series, so there is nothing to preserve.
    """
    conn.execute(
        """
        INSERT INTO line_endpoints
            (game_id, sport, book, fetched_at, open_home_ml, open_away_ml,
             open_draw_ml, close_home_ml, close_away_ml, close_draw_ml)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (game_id, book) DO UPDATE SET
            fetched_at    = excluded.fetched_at,
            open_home_ml  = excluded.open_home_ml,
            open_away_ml  = excluded.open_away_ml,
            open_draw_ml  = excluded.open_draw_ml,
            close_home_ml = excluded.close_home_ml,
            close_away_ml = excluded.close_away_ml,
            close_draw_ml = excluded.close_draw_ml
        """,
        (game_id, sport, book, iso(fetched_at), open_home_ml, open_away_ml,
         open_draw_ml, close_home_ml, close_away_ml, close_draw_ml),
    )


def insert_snapshot(
    conn: sqlite3.Connection,
    game_id: str,
    sport: str,
    book: str,
    fetched_at: datetime,
    home_ml: Optional[int] = None,
    away_ml: Optional[int] = None,
    draw_ml: Optional[int] = None,
    total_line: Optional[float] = None,
    over_price: Optional[int] = None,
    under_price: Optional[int] = None,
) -> bool:
    """Record one book's odds snapshot, deduping within a time bucket.

    If a row for the same (game_id, book) already sits within
    SNAPSHOT_MIN_GAP_MINUTES, that row is refreshed in place (its fetched_at
    moves forward to this capture, so the series always carries the freshest
    quote in each bucket — which is what CLV's "close" wants) rather than a
    new row being appended. Exact duplicates are ignored as before.

    Returns True only when a NEW row was created.
    """
    ts = iso(fetched_at)
    # Buckets are anchored to fixed wall-clock boundaries, NOT to a window
    # relative to the stored row. A relative window ratchets: each update
    # pushes fetched_at forward, so a steady 60s poll stream would keep
    # sliding the SAME row forever and the game would never accrue a second
    # snapshot — collapsing line movement and CLV to a single point. With
    # fixed boundaries every bucket is guaranteed exactly one row.
    aware = fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=timezone.utc)
    gap_seconds = SNAPSHOT_MIN_GAP_MINUTES * 60.0
    bucket_start = datetime.fromtimestamp(
        math.floor(aware.timestamp() / gap_seconds) * gap_seconds, tz=timezone.utc
    )
    bucket_end = bucket_start + timedelta(seconds=gap_seconds)
    # Bounded on BOTH sides: captures can arrive out of order (providers are
    # merged per board, and settle/capture runs interleave), and a half-open
    # ">= start" alone would let an older capture overwrite a NEWER bucket's
    # row instead of opening its own.
    existing = conn.execute(
        """
        SELECT id FROM odds_snapshots
        WHERE game_id = ? AND book = ?
          AND fetched_at >= ? AND fetched_at < ?
        ORDER BY fetched_at DESC LIMIT 1
        """,
        (game_id, book, iso(bucket_start), iso(bucket_end)),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE odds_snapshots SET
                fetched_at = ?, home_ml = ?, away_ml = ?, draw_ml = ?,
                total_line = ?, over_price = ?, under_price = ?
            WHERE id = ?
            """,
            (ts, home_ml, away_ml, draw_ml, total_line, over_price,
             under_price, existing["id"]),
        )
        return False
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO odds_snapshots
            (game_id, sport, book, fetched_at, home_ml, away_ml, draw_ml,
             total_line, over_price, under_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (game_id, sport, book, ts, home_ml, away_ml, draw_ml,
         total_line, over_price, under_price),
    )
    return cur.rowcount > 0


def upsert_pick(
    conn: sqlite3.Connection,
    game_id: str,
    sport: str,
    model: str,
    side: str,
    team: str,
    book: Optional[str],
    price: Optional[int],
    ev: Optional[float],
    kelly: Optional[float],
    tier: str,
    start_time: datetime,
    now: datetime,
    model_prob: Optional[float] = None,
) -> Optional[bool]:
    """Record the recommended bet for (game, model), one row per pair.

    Before the game's start_time the pick tracks the board: re-recording
    overwrites side/team/book/price/ev/kelly/tier in place (line moved →
    pick refreshes) while created_at is preserved. Once start_time has
    passed the pick is frozen — an existing row is never modified (only its
    locked_at is stamped if still missing) — and a first-time pick recorded
    after the start is inserted already locked.

    Returns True when inserted, False when refreshed, None when frozen.
    """
    started = iso(now) >= iso(start_time)
    existing = conn.execute(
        "SELECT id, locked_at FROM picks WHERE game_id = ? AND model = ?",
        (game_id, model),
    ).fetchone()
    if existing:
        if started:
            if existing["locked_at"] is None:
                conn.execute(
                    "UPDATE picks SET locked_at = ? WHERE id = ?",
                    (iso(start_time), existing["id"]),
                )
            return None
        conn.execute(
            """
            UPDATE picks SET side = ?, team = ?, book = ?, price = ?,
                             ev = ?, kelly = ?, model_prob = ?, tier = ?
            WHERE id = ?
            """,
            (side, team, book, price, ev, kelly, model_prob, tier, existing["id"]),
        )
        return False
    conn.execute(
        """
        INSERT INTO picks (game_id, sport, model, side, team, book, price,
                           ev, kelly, model_prob, tier, created_at, locked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (game_id, sport, model, side, team, book, price, ev, kelly, model_prob,
         tier, iso(now), iso(start_time) if started else None),
    )
    return True


def grade_pick(
    conn: sqlite3.Connection,
    pick_id: int,
    result: str,
    units: Optional[float],
    graded_at: datetime,
    locked_at: datetime,
) -> None:
    """Finalize a pick: result 'W'|'L'|'P', units (NULL for MODEL picks)."""
    conn.execute(
        """
        UPDATE picks SET result = ?, units = ?, graded_at = ?,
                         locked_at = COALESCE(locked_at, ?)
        WHERE id = ?
        """,
        (result, units, iso(graded_at), iso(locked_at), pick_id),
    )


def insert_user_bet(
    conn: sqlite3.Connection,
    game_id: str,
    sport: str,
    side: str,
    team: str,
    price: int,
    book: Optional[str],
    stake_units: float,
    note: Optional[str],
    created_at: datetime,
    line: Optional[float] = None,
) -> tuple[int, bool]:
    """Log one of the user's own bets, guarding exact double-submits.

    An ungraded row with the same (game_id, side, book, price) is returned
    instead of a new row being inserted — a genuine double-bet changes
    something (book, price) or comes after the first is graded; identical
    ungraded rows are almost always a double-click. Returns (row id,
    created): created False means the existing duplicate was returned.
    """
    existing = conn.execute(
        """
        SELECT id FROM user_bets
        WHERE game_id = ? AND side = ? AND book IS ? AND price = ?
          AND result IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (game_id, side, book, price),
    ).fetchone()
    if existing:
        return existing["id"], False
    cur = conn.execute(
        """
        INSERT INTO user_bets (game_id, sport, side, team, price, book,
                               stake_units, note, created_at, line)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (game_id, sport, side, team, price, book, stake_units, note,
         iso(created_at), line),
    )
    return cur.lastrowid, True


def delete_user_bet(conn: sqlite3.Connection, bet_id: int) -> bool:
    """Remove a user bet by id; returns True when a row was deleted."""
    cur = conn.execute("DELETE FROM user_bets WHERE id = ?", (bet_id,))
    return cur.rowcount > 0


def grade_user_bet(
    conn: sqlite3.Connection,
    bet_id: int,
    result: str,
    units: float,
    graded_at: datetime,
) -> None:
    """Finalize a user bet: result 'W'|'L'|'P' and signed units at its stake."""
    conn.execute(
        "UPDATE user_bets SET result = ?, units = ?, graded_at = ? WHERE id = ?",
        (result, units, iso(graded_at), bet_id),
    )


def insert_user_parlay(
    conn: sqlite3.Connection,
    stake_units: float,
    price: int,
    note: Optional[str],
    created_at: datetime,
    legs: list[dict],
) -> int:
    """Insert a parlay and its legs; returns the parlay id.

    Each leg dict carries game_id, sport, side, team, price, book. Duplicate
    detection lives in tracker.log_parlay (it needs leg-set comparison, not a
    column key).
    """
    cur = conn.execute(
        """
        INSERT INTO user_parlays (stake_units, price, note, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (stake_units, price, note, iso(created_at)),
    )
    parlay_id = cur.lastrowid
    conn.executemany(
        """
        INSERT INTO user_parlay_legs
            (parlay_id, game_id, sport, side, team, price, book)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (parlay_id, leg["game_id"], leg["sport"], leg["side"],
             leg["team"], leg["price"], leg.get("book"))
            for leg in legs
        ],
    )
    return parlay_id


def delete_user_parlay(conn: sqlite3.Connection, parlay_id: int) -> bool:
    """Remove a parlay and its legs; returns True when a row was deleted."""
    conn.execute("DELETE FROM user_parlay_legs WHERE parlay_id = ?", (parlay_id,))
    cur = conn.execute("DELETE FROM user_parlays WHERE id = ?", (parlay_id,))
    return cur.rowcount > 0


def grade_parlay_leg(conn: sqlite3.Connection, leg_id: int, result: str) -> None:
    """Record one settled leg's outcome: 'W'|'L'|'P'."""
    conn.execute(
        "UPDATE user_parlay_legs SET result = ? WHERE id = ?", (result, leg_id)
    )


def grade_user_parlay(
    conn: sqlite3.Connection,
    parlay_id: int,
    result: str,
    units: float,
    graded_at: datetime,
) -> None:
    """Finalize a parlay: result 'W'|'L'|'P' and signed units at its stake."""
    conn.execute(
        "UPDATE user_parlays SET result = ?, units = ?, graded_at = ? WHERE id = ?",
        (result, units, iso(graded_at), parlay_id),
    )


def insert_auto_bet(
    conn: sqlite3.Connection,
    *,
    game_id: str,
    sport: str,
    side: str,
    team: str,
    tier: str,
    ev: Optional[float],
    model_prob: Optional[float],
    condition_id: str,
    clob_side: str,
    slug: Optional[str],
    limit_prob: float,
    shares: int,
    usd: float,
    order_id: Optional[str],
    status: str,
    error: Optional[str],
    created_at: datetime,
    best_ask_cents: Optional[int] = None,
    book_depth: Optional[float] = None,
    fee_rate: Optional[float] = None,
) -> int:
    """Record one autobet attempt (status dry|placed|unmatched|error)."""
    cur = conn.execute(
        """
        INSERT INTO auto_bets (game_id, sport, side, team, tier, ev, model_prob,
                               condition_id, clob_side, slug, limit_prob, shares,
                               usd, order_id, status, error, created_at,
                               best_ask_cents, book_depth, fee_rate)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (game_id, sport, side, team, tier, ev, model_prob, condition_id,
         clob_side, slug, limit_prob, shares, usd, order_id, status, error,
         iso(created_at), best_ask_cents, book_depth, fee_rate),
    )
    return cur.lastrowid


def set_auto_bet_fill(
    conn: sqlite3.Connection,
    bet_id: int,
    fill_shares: float,
    fill_prob: float,
) -> None:
    """Attach a fill to an auto bet: the RECONCILED real fill (from the CLOB
    trades feed) for placed orders, or the executable ask captured at
    creation for dry rows — grading prefers these over shares/limit.

    `fill_shares` may be FRACTIONAL (Polymarket fills partial shares).
    SQLite's INTEGER affinity only narrows a REAL when the conversion is
    lossless, so a 7.5-share partial round-trips as 7.5."""
    conn.execute(
        "UPDATE auto_bets SET fill_shares = ?, fill_prob = ? WHERE id = ?",
        (fill_shares, fill_prob, bet_id),
    )


def insert_book_observation(
    conn: sqlite3.Connection,
    *,
    sport: str,
    game_id: str,
    condition_id: str,
    clob_side: str,
    pick_prob: Optional[float],
    live_prob: Optional[float],
    best_ask_cents: Optional[int],
    depth_shares: Optional[float],
    limit_cents: Optional[int],
    at: datetime,
) -> None:
    """One live-book observation per evaluated autobet candidate — the
    empirical last-trade-vs-executable-ask record that calibrates the
    backtest's fill assumptions."""
    conn.execute(
        """
        INSERT INTO book_observations (at_ts, sport, game_id, condition_id,
                                       clob_side, pick_prob, live_prob,
                                       best_ask_cents, depth_shares, limit_cents)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (iso(at), sport, game_id, condition_id, clob_side, pick_prob,
         live_prob, best_ask_cents, depth_shares, limit_cents),
    )


def upsert_book_snapshot(
    conn: sqlite3.Connection,
    *,
    condition_id: str,
    offset_minutes: int,
    best_bid_a: Optional[float],
    best_ask_a: Optional[float],
    bid_depth_a: Optional[str],
    ask_depth_a: Optional[str],
    at_ts: str,
) -> None:
    """Store one harvested order-book snapshot (jobs/bookfill.py)."""
    conn.execute(
        """
        INSERT INTO pm_book_snapshots (condition_id, offset_minutes, best_bid_a,
                                       best_ask_a, bid_depth_a, ask_depth_a, at_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(condition_id, offset_minutes) DO UPDATE SET
            best_bid_a = excluded.best_bid_a, best_ask_a = excluded.best_ask_a,
            bid_depth_a = excluded.bid_depth_a, ask_depth_a = excluded.ask_depth_a,
            at_ts = excluded.at_ts
        """,
        (condition_id, offset_minutes, best_bid_a, best_ask_a, bid_depth_a,
         ask_depth_a, at_ts),
    )


def mark_settled(
    conn: sqlite3.Connection,
    game_id: str,
    settled_at: datetime,
    home_score: Optional[int],
    away_score: Optional[int],
    completed: bool,
) -> None:
    """Finalize a game: completed with scores, or abandoned (completed=0)."""
    conn.execute(
        """
        UPDATE games SET home_score = ?, away_score = ?, completed = ?,
                         settled_at = ?
        WHERE game_id = ?
        """,
        (home_score, away_score, int(completed), iso(settled_at), game_id),
    )


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Query helper: list of plain dicts (friendlier than sqlite3.Row)."""
    return [dict(r) for r in conn.execute(sql, params).fetchall()]

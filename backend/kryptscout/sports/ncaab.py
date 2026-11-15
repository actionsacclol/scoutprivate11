"""NCAAB (men's college basketball) engine — Elo tuned for the D1 landscape.

v1 replays every season sportsdataverse's hoopR MBB data repo publishes
(2003 onward — 2002 is a 404) from one parquet per season; hoopR keys
seasons by their ending year, so 2024 == the 2023-24 season:

    https://github.com/sportsdataverse/hoopR-mbb-data
        /raw/main/mbb/schedules/parquet/mbb_schedule_{season}.parquet

Elo settings reflect college hoops: K=30 (30-game seasons need fast
movement), 90 rating points of home advantage (the college home edge is
large), margin-of-victory scaling, and heavy between-season regression
(carry=0.55 — rosters churn hard). Neutral-site games (March Madness,
early-season tournaments) are rated with zero home advantage, both in the
historical replay and at predict time via the ESPN scoreboard's neutral-site
flag on Game.

The ~360-team D1 field is inferred per season from game counts; the many
non-D1 guarantee-game guests are pinned at a flat 1200 and never enter the
book, so they can't poison ratings. Because hoopR is ESPN-sourced, the book
is keyed by ESPN displayName via each team id's most recent name, which
keeps one Elo identity across rebrands (IUPUI -> IU Indianapolis) and lines
up with our ESPN scoreboard slates.

Totals come from rolling last-10 points for/against (league average total
~145). Parquet files cache under RAW_DIR/ncaab/ (only the current season is
re-fetched); replayed state persists to DATA_DIR/ncaab_state.json. Replay
logs pre-update probabilities for every D1-vs-D1 game — the walk-forward
backtest printed by `python -m kryptscout.sports.ncaab`.

v3 (2026-11-14) — three live-only faults the backtest could not see, found
by replaying what the engine SERVED day by day (held out 2024-25 + 2025-26,
12,592 served games: log loss 0.5595 as served -> 0.4893 fixed):

1. D1 membership was "10+ games this season", computed from the games
   played so far. In November nobody has ten, so no game of the new season
   updated a rating until mid-December; on 2026-11-14, 749 completed games
   had moved zero ratings. Membership is now "carries a conference id in
   this season's file" (351/351, 358/360, 365/366 agreement with the count
   rule on finished seasons, and known before the first tip); the count rule
   remains the fallback for frames without the column.
2. The season roll-over (carry, added variance) fired only when the first
   completed game of the new season reached the file, so opening-night
   boards were served from last April's ratings unshrunk. `_replay` now
   rolls forward to `through_season` (the calendar season) on its own.
3. A non-D1 guest was missing from the Kalman book and priced as
   (0, init_var): an average D1 team. D1 hosts won 97% of those games while
   the board said 50% (log loss 0.775). The guest is now one generic prior,
   NON_D1_PRIOR, fitted by moments on 4,060 such games of 2016-24.

Roll-over regresses toward the conference mean (CONF_REGRESS_WEIGHT):
−0.0056 [−0.0082, −0.0027] on held-out November D1-vs-D1 games, −0.0010
over the whole season. Recalibration, a full retune of the constants, a
margin cap, rest days and a wider early-season variance were all tested on
the same split and changed nothing.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

from kryptscout.config import DATA_DIR, RAW_DIR
from kryptscout.core.kalman import KalmanBook, KalmanConfig, conference_targets
from kryptscout.core.elo import EloBook, EloConfig
from kryptscout.core.schemas import Game, Prediction, Sport
from kryptscout.datasources import espn
from kryptscout.sports.base import SportEngine

SCHEDULE_URL = (
    "https://github.com/sportsdataverse/hoopR-mbb-data/raw/main/"
    "mbb/schedules/parquet/mbb_schedule_{season}.parquet"
)
RAW_NCAAB_DIR = RAW_DIR / "ncaab"
STATE_PATH = DATA_DIR / "ncaab_state.json"

FIRST_SEASON = 2003  # earliest hoopR season (ending-year key; 2002 404s)
SEASON_START_MONTH = 8  # Aug 1: seasons flip (games run Nov-Apr)
LATEST_MAX_AGE_SECONDS = 24 * 3600  # only the current season file goes stale

MIN_D1_GAMES = 10  # appearances in a season required to count as D1
NON_D1_RATING = 1200.0  # flat rating for non-D1 guests / unknown teams
ROLLING_WINDOW = 10  # games in the rolling points for/against windows
LEAGUE_AVG_POINTS = 72.5  # per-team fallback (league average total ~145)

ELO_CONFIG = EloConfig(
    k=30.0,
    home_advantage=90.0,
    mov_scaling=True,
    season_carry=0.55,
)

# Rebrands ESPN shipped after the last game in our data — the live scoreboard
# name on the left, the name the book was replayed under on the right. Rows
# become no-ops once a new season's data carries the new name.
PREDICT_ALIASES: dict[str, str] = {
    "Saint Francis Red Wolves": "Saint Francis Red Flash",
}

# Only these survive the parquet read — the files carry 80+ columns.
REQUIRED_COLUMNS = [
    "season",
    "season_type",
    "game_date",
    "home_id",
    "away_id",
    "home_display_name",
    "away_display_name",
    "home_score",
    "away_score",
    "status_type_completed",
    "neutral_site",
    # D1 membership + roll-over targets (v3). Present in every hoopR season
    # file 2003-2027; null for a non-D1 side.
    "home_conference_id",
    "away_conference_id",
]


def _current_season(today: date | None = None) -> int:
    """hoopR season key (ending year) for a calendar date."""
    today = today or datetime.now(timezone.utc).date()
    return today.year + 1 if today.month >= SEASON_START_MONTH else today.year


def _seasons(today: date | None = None) -> list[int]:
    return list(range(FIRST_SEASON, _current_season(today) + 1))


async def _download_schedules(
    seasons: list[int], raw_dir: Path = RAW_NCAAB_DIR
) -> list[Path]:
    """Fetch each season's parquet into RAW_DIR/ncaab, caching aggressively.

    Past seasons are immutable — a cached copy is final. The latest season is
    re-fetched once it's older than 24h; a 404 there (file not published yet
    early in the off-season) is tolerated by skipping the season.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        for season in seasons:
            path = raw_dir / f"mbb_schedule_{season}.parquet"
            is_latest = season == seasons[-1]
            fresh = path.exists() and (
                not is_latest
                or time.time() - path.stat().st_mtime < LATEST_MAX_AGE_SECONDS
            )
            if not fresh:
                try:
                    resp = await client.get(SCHEDULE_URL.format(season=season))
                    if resp.status_code == 404 and is_latest:
                        if not path.exists():
                            continue  # next season not published yet
                    else:
                        resp.raise_for_status()
                        path.write_bytes(resp.content)
                except httpx.HTTPError:
                    # Transient GitHub/network failure: replaying from a
                    # stale cached copy beats failing the whole refresh.
                    # With no cache there is nothing to load — re-raise.
                    if not path.exists():
                        raise
            paths.append(path)
    return paths


def _load_frame(paths: list[Path]) -> pd.DataFrame:
    frames = [pd.read_parquet(path, columns=REQUIRED_COLUMNS) for path in paths]
    return pd.concat(frames, ignore_index=True)


def _canonical_names(df: pd.DataFrame) -> dict[int, str]:
    """ESPN team id -> the displayName from that id's most recent game.

    Ids are stable across rebrands while names drift (IUPUI Jaguars -> IU
    Indianapolis Jaguars), so keying the book by each id's latest name keeps
    one Elo identity per program and matches what the live ESPN scoreboard
    calls the team today.
    """
    sides = []
    for side in ("home", "away"):
        sides.append(
            df[[f"{side}_id", f"{side}_display_name", "game_date"]].rename(
                columns={f"{side}_id": "id", f"{side}_display_name": "name"}
            )
        )
    both = pd.concat(sides, ignore_index=True).dropna().sort_values("game_date")
    return dict(zip(both["id"].astype(int), both["name"]))


# Kalman margin filter (core/kalman.py) — the served win probability since
# v2. Tuned 2026-09-02 on hoopR 2016-26, D1 vs D1 (tune 2024-25, held out
# 2026, n=5,319): log loss 0.5288 vs the Elo config's 0.5513, −0.0225
# [−0.0281, −0.0171], accuracy 72.0%. Non-D1 guests never enter the filter;
# the Elo book still runs for the score nudge and the flat-1200 handling.
#
# v3 (2026-11-14): carry 0.85 -> 0.70 TOGETHER WITH regression toward the
# conference mean (CONF_REGRESS_WEIGHT = 1.0), picked on 2016-24 from a
# 9-point grid. Everything else is unchanged: a coordinate search over
# sigma/q/hfa/init_var/season_var/carry (64 configs) moved held-out log loss
# by −0.0011 [−0.0026, +0.0003].
KALMAN_CONFIG = KalmanConfig(
    sigma=10.0, q=0.3, hfa=3.0, init_var=100.0, season_var=30.0, carry=0.70
)
CONF_REGRESS_WEIGHT = 1.0  # 0.0 = regress to zero (pre-v3; then use carry=0.85)

# Generic non-D1 guest as the Kalman filter sees it: (mean, variance) in
# points relative to the average D1 team, fitted by moments on the 4,060
# D1-vs-non-D1 games of 2016-24. Used to PRICE the game only: as before,
# a guarantee game does not update the D1 side's Kalman rating.
NON_D1_PRIOR: tuple[float, float] = (-35.6, 219.0)


def _d1_by_season(
    df: pd.DataFrame, played: pd.DataFrame, min_d1_games: int
) -> dict[int, set]:
    """season -> ids of that season's D1 teams.

    A D1 team carries a conference id on every row of the season's file,
    scheduled rows included, so membership is known before a game is played.
    The old rule — `min_d1_games` appearances — is the fallback for a season
    (or a synthetic test frame) without conference ids; on its own it left
    the in-progress season with no D1 teams at all until mid-December.
    """
    has_conf = "home_conference_id" in df.columns and "away_conference_id" in df.columns
    out: dict[int, set] = {}
    for season, chunk in played.groupby("season"):
        ids: set = set()
        if has_conf:
            full = df[df["season"] == season]
            ids = set(full.loc[full["home_conference_id"].notna(), "home_id"]) | set(
                full.loc[full["away_conference_id"].notna(), "away_id"]
            )
        if not ids:
            counts = pd.concat([chunk["home_id"], chunk["away_id"]]).value_counts()
            ids = set(counts[counts >= min_d1_games].index)
        out[int(season)] = ids
    return out


def _season_conferences(df: pd.DataFrame, canon: dict[int, str]) -> dict[int, dict[str, float]]:
    """season -> {canonical displayName: conference id} (scheduled rows included)."""
    out: dict[int, dict[str, float]] = {}
    if "home_conference_id" not in df.columns or "away_conference_id" not in df.columns:
        return out
    for side in ("home", "away"):
        rows = df[df[f"{side}_conference_id"].notna() & df[f"{side}_id"].notna()]
        for season, team_id, conf in zip(
            rows["season"], rows[f"{side}_id"], rows[f"{side}_conference_id"]
        ):
            name = canon.get(int(team_id))
            if name is not None:
                out.setdefault(int(season), {})[name] = float(conf)
    return out


def _replay(
    df: pd.DataFrame,
    min_d1_games: int = MIN_D1_GAMES,
    through_season: int | None = None,
    guest_records: list[dict] | None = None,
) -> tuple[EloBook, dict[str, deque], dict, KalmanBook]:
    """Replay completed games chronologically through a fresh EloBook.

    Returns the fitted book (keyed by canonical ESPN displayName), per-team
    rolling (points_for, points_against) windows, and per-season backtest
    metrics over D1-vs-D1 games. Pure function of the frame so tests can
    drive it with synthetic data.

    `through_season`: roll the books forward to this (calendar) season even
    if none of its games is in the frame yet — the live engine passes
    `_current_season()` so opening night is served from rolled-over ratings.
    `guest_records`, when given, collects a backtest record for every
    D1-vs-non-D1 game, priced with NON_D1_PRIOR as the board serves it.
    """
    # Own config copy: neutral-site games temporarily zero home advantage,
    # which must never leak into the shared module-level ELO_CONFIG.
    book = EloBook(config=replace(ELO_CONFIG))
    kalman = KalmanBook(KALMAN_CONFIG)
    rolling: dict[str, deque] = {}
    records: list[dict] = []

    canon = _canonical_names(df)
    played = df[
        (df["status_type_completed"] == True)  # noqa: E712 — pandas mask
        & df["season_type"].isin((2, 3))  # regular season + postseason only
        & df["home_score"].notna()
        & df["away_score"].notna()
    ].sort_values(["season", "game_date"], kind="stable")

    d1_by_season = _d1_by_season(df, played, min_d1_games)
    conferences = _season_conferences(df, canon) if CONF_REGRESS_WEIGHT > 0 else {}

    def roll_into(season: int) -> None:
        # Conference of each team in the season being entered; before that
        # season's file is published, last season's alignment stands in.
        conf_of = conferences.get(season) or conferences.get(season - 1) or {}
        book.new_season()
        kalman.new_season(conference_targets(kalman.means, conf_of, CONF_REGRESS_WEIGHT))

    current_season: int | None = None
    for row in played.itertuples(index=False):
        if current_season is not None and row.season != current_season:
            roll_into(int(row.season))
        current_season = row.season

        home_score, away_score = int(row.home_score), int(row.away_score)
        if home_score == away_score:
            continue  # no ties in college hoops — 0-0 forfeits/placeholders

        d1 = d1_by_season[int(row.season)]
        home_d1, away_d1 = row.home_id in d1, row.away_id in d1
        if not home_d1 and not away_d1:
            continue  # non-D1 exhibition — irrelevant to the book

        home, away = canon[int(row.home_id)], canon[int(row.away_id)]
        home_won = home_score > away_score

        # Non-D1 guests are pinned flat at 1200: they take part in the update
        # (so the D1 side is rated fairly) but never persist in the book.
        pinned = home if not home_d1 else (away if not away_d1 else None)
        prior = book.ratings.get(pinned) if pinned is not None else None
        if pinned is not None:
            book.ratings[pinned] = NON_D1_RATING

        hfa = book.config.home_advantage
        if row.neutral_site:
            book.config.home_advantage = 0.0
        try:
            prob = book.expected(home, away)
            if home_d1 and away_d1:
                p_kal = kalman.update(
                    home, away, home_score, away_score, neutral=bool(row.neutral_site)
                )
                records.append(
                    {"season": int(row.season), "p": p_kal, "p_elo": prob, "home_won": home_won}
                )
            elif guest_records is not None:
                # Priced as the board serves it; no Kalman update (unchanged).
                p_guest = kalman.expected(
                    home, away, neutral=bool(row.neutral_site),
                    home_prior=None if home_d1 else NON_D1_PRIOR,
                    away_prior=None if away_d1 else NON_D1_PRIOR,
                )
                guest_records.append(
                    {"season": int(row.season), "p": p_guest, "p_elo": prob, "home_won": home_won}
                )
            book.update(home, away, home_won, margin=abs(home_score - away_score))
        finally:
            book.config.home_advantage = hfa

        if pinned is not None:
            if prior is None:
                del book.ratings[pinned]
            else:
                book.ratings[pinned] = prior

        for team, is_d1, pf, pa in (
            (home, home_d1, home_score, away_score),
            (away, away_d1, away_score, home_score),
        ):
            if is_d1:
                rolling.setdefault(team, deque(maxlen=ROLLING_WINDOW)).append(
                    (pf, pa)
                )

    # The calendar has moved on but no game of the new season is in the frame
    # yet (opening night, or the off-season): roll forward anyway.
    if current_season is not None and through_season is not None:
        for season in range(int(current_season) + 1, int(through_season) + 1):
            roll_into(season)

    return book, rolling, _compute_metrics(records), kalman


def _log_loss(pairs: list[tuple[float, bool]]) -> float:
    """Mean negative log-likelihood over (prob_of_home, home_won) pairs."""
    eps = 1e-12
    total = 0.0
    for p, won in pairs:
        p = min(max(p, eps), 1.0 - eps)
        total -= math.log(p if won else 1.0 - p)
    return total / len(pairs)


def _compute_metrics(records: list[dict]) -> dict:
    """Accuracy/log loss of pre-update probs, overall and per season."""
    if not records:
        return {}

    def score(rows: list[dict]) -> dict:
        return {
            "games": len(rows),
            "accuracy": sum((r["p"] >= 0.5) == r["home_won"] for r in rows)
            / len(rows),
            "log_loss": _log_loss([(r["p"], r["home_won"]) for r in rows]),
        }

    metrics = {"overall": score(records)}
    for season in sorted({r["season"] for r in records}):
        metrics[str(season)] = score([r for r in records if r["season"] == season])
    return metrics


class NcaabEngine(SportEngine):
    sport = Sport.NCAAB
    model_name = "ncaab-kalman-v3"

    def __init__(self, state_path: Path = STATE_PATH) -> None:
        self.state_path = state_path
        self.book = EloBook(config=replace(ELO_CONFIG))
        self.kalman = KalmanBook(KALMAN_CONFIG)
        # Canonical names of this season's D1 teams (conference id in the
        # schedule file). Empty until the first v3 refresh; from then on any
        # side not in it is priced as the generic non-D1 prior.
        self.d1_teams: set[str] = set()
        self.rolling: dict[str, deque] = {}
        self.metrics: dict = {}
        self._load_state()

    async def upcoming_games(self, on: date | None = None) -> list[Game]:
        return await espn.scoreboard(Sport.NCAAB, on)

    async def predict(self, games: list[Game]) -> list[Prediction]:
        now = datetime.now(timezone.utc)
        predictions: list[Prediction] = []
        for game in games:
            # Neutral sites (March Madness, early-season tournaments) get
            # zero home advantage in both the win prob and the score nudge.
            hfa = 0.0 if game.neutral_site else self.book.config.home_advantage
            home_key, away_key = self._resolve(game.home), self._resolve(game.away)
            p_home = self.kalman.expected(
                home_key, away_key, neutral=game.neutral_site,
                home_prior=self._prior(home_key), away_prior=self._prior(away_key),
            )
            home_score = (
                self._avg(game.home, idx=0) + self._avg(game.away, idx=1)
            ) / 2.0
            away_score = (
                self._avg(game.away, idx=0) + self._avg(game.home, idx=1)
            ) / 2.0
            total = home_score + away_score
            # Nudge toward the Elo favorite; equal-and-opposite keeps the total.
            elo_diff = self._rating(game.home) + hfa - self._rating(game.away)
            nudge = 0.5 * (elo_diff / 100.0)
            # A team with no scoring history would price at the league-average
            # placeholder — a constant, not a forecast. The board printed UNDER
            # edges against exactly that (WNBA, 2026-09-02). Emit no total.
            scored = bool(self.rolling.get(self._resolve(game.home))) and bool(
                self.rolling.get(self._resolve(game.away))
            )
            predictions.append(
                Prediction(
                    game_id=game.id,
                    sport=Sport.NCAAB,
                    model=self.model_name,
                    generated_at=now,
                    home_win_prob=p_home,
                    away_win_prob=1.0 - p_home,
                    predicted_home_score=round(home_score + nudge, 1) if scored else None,
                    predicted_away_score=round(away_score - nudge, 1) if scored else None,
                    predicted_total=round(total, 1) if scored else None,
                    confidence=abs(p_home - 0.5) * 2.0,
                )
            )
        return predictions

    async def refresh_data(self) -> None:
        paths = await _download_schedules(_seasons())
        df = _load_frame(paths)
        guest_records: list[dict] = []
        self.book, self.rolling, self.metrics, self.kalman = _replay(
            df, through_season=_current_season(), guest_records=guest_records
        )
        if guest_records:
            self.metrics["non_d1_guest"] = {
                "games": len(guest_records),
                "accuracy": sum((r["p"] >= 0.5) == r["home_won"] for r in guest_records)
                / len(guest_records),
                "log_loss": _log_loss([(r["p"], r["home_won"]) for r in guest_records]),
            }
        canon = _canonical_names(df)
        latest = df[df["season"] == df["season"].max()]
        ids = set(latest.loc[latest["home_conference_id"].notna(), "home_id"]) | set(
            latest.loc[latest["away_conference_id"].notna(), "away_id"]
        )
        self.d1_teams = {canon[int(i)] for i in ids if int(i) in canon}
        self._save_state()

    # -- rating / rolling lookups (alias- and unknown-team-aware) -----------

    def _resolve(self, team: str) -> str:
        """Book key for a live ESPN name, following post-data rebrands."""
        if team in self.book.ratings or team in self.d1_teams:
            return team
        return PREDICT_ALIASES.get(team, team)

    def _prior(self, key: str) -> tuple[float, float] | None:
        """NON_D1_PRIOR for a side that is not a D1 team this season, else None.

        Without a membership list (a state written before v3) every side is
        looked up in the book as before, so nothing changes until a refresh.
        """
        if not self.d1_teams or key in self.d1_teams:
            return None
        return NON_D1_PRIOR

    def _rating(self, team: str) -> float:
        """Elo rating; unknown teams (non-D1 guests) are flat 1200."""
        return self.book.ratings.get(self._resolve(team), NON_D1_RATING)

    def _expected(self, home: str, away: str, hfa: float | None = None) -> float:
        """P(home wins); `hfa` overrides the config edge (0.0 = neutral site)."""
        if hfa is None:
            hfa = self.book.config.home_advantage
        diff = self._rating(home) + hfa - self._rating(away)
        return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))

    def _avg(self, team: str, idx: int) -> float:
        """Rolling mean of points for (idx=0) / against (idx=1) for a team."""
        window = self.rolling.get(self._resolve(team))
        if not window:
            return LEAGUE_AVG_POINTS
        return sum(g[idx] for g in window) / len(window)

    # -- state persistence ---------------------------------------------------

    def _save_state(self) -> None:
        state = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": self.model_name,
            "ratings": self.book.ratings,
            "kalman": self.kalman.to_state(),
            "d1_teams": sorted(self.d1_teams),
            "rolling": {team: list(w) for team, w in self.rolling.items()},
            "metrics": self.metrics,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self.state_path)

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.book = EloBook(
            config=replace(ELO_CONFIG),
            ratings={k: float(v) for k, v in state.get("ratings", {}).items()},
        )
        self.kalman = KalmanBook.from_state(KALMAN_CONFIG, state.get("kalman"))
        self.d1_teams = (
            set(state.get("d1_teams") or []) if state.get("model") == self.model_name else set()
        )
        self.rolling = {
            team: deque((tuple(g) for g in window), maxlen=ROLLING_WINDOW)
            for team, window in state.get("rolling", {}).items()
        }
        self.metrics = state.get("metrics", {})


def build_engine() -> NcaabEngine:
    return NcaabEngine()


if __name__ == "__main__":

    async def _main() -> None:
        seasons = _seasons()
        t0 = time.perf_counter()
        paths = await _download_schedules(seasons)
        dl_seconds = time.perf_counter() - t0

        engine = build_engine()
        t0 = time.perf_counter()
        await engine.refresh_data()  # downloads are cache hits now
        replay_seconds = time.perf_counter() - t0

        latest = str(max(seasons))
        print(f"{engine.model_name} — seasons {seasons[0]}-{seasons[-1]} "
              f"(hoopR ending-year keys)")
        print(f"  download: {dl_seconds:.1f}s ({len(paths)} files)  "
              f"load+replay: {replay_seconds:.1f}s")
        print(f"  teams rated: {len(engine.book.ratings)}")
        for label in ("overall", latest):
            m = engine.metrics.get(label)
            if not m:
                continue
            name = label if label == "overall" else f"{int(label) - 1}-{label[2:]}"
            print(f"  {name}: {m['games']} D1-vs-D1 games  "
                  f"accuracy={m['accuracy']:.4f}  log_loss={m['log_loss']:.4f}")

        top = sorted(
            engine.book.ratings.items(), key=lambda kv: kv[1], reverse=True
        )[:10]
        print("  current top 10 Elo:")
        for name, rating in top:
            print(f"    {rating:7.1f}  {name}")

    asyncio.run(_main())

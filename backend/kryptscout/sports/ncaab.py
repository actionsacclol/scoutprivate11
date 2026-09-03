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
from kryptscout.core.kalman import KalmanBook, KalmanConfig
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
KALMAN_CONFIG = KalmanConfig(sigma=10.0, q=0.3, hfa=3.0, init_var=100.0, season_var=30.0)


def _replay(
    df: pd.DataFrame, min_d1_games: int = MIN_D1_GAMES
) -> tuple[EloBook, dict[str, deque], dict, KalmanBook]:
    """Replay completed games chronologically through a fresh EloBook.

    Returns the fitted book (keyed by canonical ESPN displayName), per-team
    rolling (points_for, points_against) windows, and per-season backtest
    metrics over D1-vs-D1 games. Pure function of the frame so tests can
    drive it with synthetic data.
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

    # D1 membership is per season: real D1 programs play 28+ games while
    # non-D1 guests show up a handful of times for guarantee games.
    d1_by_season: dict[int, set[int]] = {}
    for season, chunk in played.groupby("season"):
        counts = pd.concat([chunk["home_id"], chunk["away_id"]]).value_counts()
        d1_by_season[int(season)] = set(counts[counts >= min_d1_games].index)

    current_season: int | None = None
    for row in played.itertuples(index=False):
        if current_season is not None and row.season != current_season:
            book.new_season()
            kalman.new_season()
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
    model_name = "ncaab-kalman-v2"

    def __init__(self, state_path: Path = STATE_PATH) -> None:
        self.state_path = state_path
        self.book = EloBook(config=replace(ELO_CONFIG))
        self.kalman = KalmanBook(KALMAN_CONFIG)
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
            p_home = self.kalman.expected(
                self._resolve(game.home), self._resolve(game.away), neutral=game.neutral_site
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
        self.book, self.rolling, self.metrics, self.kalman = _replay(df)
        self._save_state()

    # -- rating / rolling lookups (alias- and unknown-team-aware) -----------

    def _resolve(self, team: str) -> str:
        """Book key for a live ESPN name, following post-data rebrands."""
        if team in self.book.ratings:
            return team
        return PREDICT_ALIASES.get(team, team)

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

"""MLB engine — Elo plus starting pitchers plus rolling runs over MLB Stats API.

v3 is the v2 architecture with jointly retuned parameters. The core is a
low-K team Elo (baseball is the noisiest major sport, so K=2 with a small
24-point home advantage — MLB home teams win only ~53%, margin-of-victory
scaling, 75% carry between seasons) overlaid with a starting-pitcher
adjustment. Each starter carries an EWMA of runs allowed per start (team runs
conceded in his starts, span ~30 starts, initialized at league average so
pitchers with few starts are shrunk toward the mean). The gap to league
average converts to Elo points — adj = clamp(K_p * (league_avg - ewma), ±60) —
and the home-minus-away pitcher difference shifts the pre-game win probability
only; team Elo updates stay pure. Totals come from each team's rolling last-20
runs scored/allowed.

v3 retune (2026-07): a joint Elo × pitcher grid (K 2-4, HFA 24-32, K_p 6-16,
span 15-30) scored on 2025 showed K=2 dominating (slower team drift suits
baseball's noise) and longer pitcher memory (K_p 12, span 30) beating v2's
8/20; HFA was flat so 24 stays on priors. On the untouched 2026 validation:
log loss 0.6901 -> 0.6884, accuracy 0.5506 -> 0.5554. Also tested and
REJECTED: an NBA-style XGBoost overlay (elo_diff + pitcher adjustments +
rolling run splits + rest days + games played, Platt-calibrated, chronological
2023-24 fit / 2025 select) LOST to raw Elo on both 2025 (0.6846 vs 0.6783)
and 2026 (0.6924 vs 0.6901) — from final scores and probable-pitcher IDs
alone, Elo is near the information ceiling. ALSO tested and REJECTED
(2026-07): true per-starter innings/ER from game boxscores — ratio-of-EWMAs
starter ER9, with and without a separate team-keyed bullpen component — lost
the 2024+2025 selection to the team-runs-conceded EWMA and didn't improve the
untouched 2026 either (best tune-selected variant landed exactly on v3's
0.6884). Team runs conceded in a pitcher's starts already IS a starter signal
plus the defense/bullpen context that rides behind him; stripping to
starter-only ER removes predictive context. Next ideas need genuinely new
information (lineups, park factors, pitcher handedness splits), not a purer
cut of the same runs.

Data is the official free MLB Stats API (statsapi.mlb.com): regular season and
postseason (gameType R,F,D,L,W — spring training and the All-Star game never
enter the ratings) for 2022 onward, pulled in month-sized chunks with
hydrate=probablePitcher (populated on historical finals too — coverage is
~99.9% of 2022-2026 games). Each chunk is cached under RAW_DIR/mlb/ as
schedule_v2_YYYY-MM.json — past months forever, the current month refreshed
once it is older than 24 hours — so a refresh only ever re-fetches one small
JSON file. v1 caches (schedule_YYYY-MM.json, no pitcher hydrate) are deleted
on sight so the hydrated payload is re-fetched cleanly. The replay itself
re-runs from 2022 each refresh; at ~2500 games/season that costs milliseconds.
The 2026 season to date is scored walk-forward (pre-update probabilities,
pitcher ratings built only from prior starts) as the validation set; K_p and
the EWMA span were tuned on 2025 only.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import math
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from kryptscout.config import DATA_DIR, RAW_DIR
from kryptscout.core.elo import EloBook, EloConfig
from kryptscout.core.schemas import Game, Prediction, Sport
from kryptscout.datasources import espn
from kryptscout.sports.base import SportEngine

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
RAW_MLB_DIR = RAW_DIR / "mlb"
STATE_PATH = DATA_DIR / "mlb_state.json"
CHUNK_MAX_AGE_SECONDS = 24 * 3600
CACHE_VERSION = 2  # v2 payloads carry hydrate=probablePitcher

FIRST_SEASON = 2022
VALIDATE_FROM_SEASON = 2026
SEASON_MONTHS = range(3, 12)  # Opening Day can land in March; WS ends by November
GAME_TYPES = "R,F,D,L,W"  # regular season, wild card, LDS, LCS, World Series
GAME_TYPE_SET = frozenset(GAME_TYPES.split(","))

ROLLING_WINDOW = 20  # games of runs for/against feeding score predictions
LEAGUE_AVG_RUNS = 4.4  # fallback before a team has rolling history

# Starting-pitcher adjustment. v3 joint retune (see module docstring): the
# 2025 tuning surface is flat (~0.0003 log-loss across the top), so these are
# chosen by the consistent pattern (longer memory, slightly stronger K_p) and
# priors, not a single sharp optimum.
PITCHER_ADJ_ENABLED = True
PITCHER_K = 12.0  # Elo points per run of EWMA quality vs league average
PITCHER_SPAN = 30  # EWMA span in starts (~alpha 0.065)
PITCHER_ADJ_CAP = 60.0  # Elo points, either direction

ELO_CONFIG = EloConfig(k=2.0, home_advantage=24.0, mov_scaling=True, season_carry=0.75)

# statsapi names match ESPN displayNames exactly for the 2026 slate (verified
# live, including the Athletics, who are just "Athletics" in both). Historical
# names map to the franchise's current name so it keeps one Elo identity.
NAME_FIXES: dict[str, str] = {
    "Oakland Athletics": "Athletics",  # statsapi name through the 2024 season
}


def _canon(name: str | None) -> str | None:
    """Franchise's current display name, matching ESPN's."""
    if not name:
        return None
    return NAME_FIXES.get(name, name)


def _season_months(season: int, today: date) -> list[tuple[int, int]]:
    """(season, month) chunks to load — never a month that hasn't started."""
    return [
        (season, month)
        for month in SEASON_MONTHS
        if date(season, month, 1) <= today
    ]


def _chunk_path(season: int, month: int) -> Path:
    return RAW_MLB_DIR / f"schedule_v{CACHE_VERSION}_{season:04d}-{month:02d}.json"


def _legacy_chunk_path(season: int, month: int) -> Path:
    """v1 cache name (no pitcher hydrate) — deleted on upgrade."""
    return RAW_MLB_DIR / f"schedule_{season:04d}-{month:02d}.json"


async def _fetch_chunk(
    client: httpx.AsyncClient, season: int, month: int, today: date
) -> dict:
    """One month of schedule JSON, served from RAW_DIR/mlb when possible.

    Past months are immutable and cached forever; the current (or a future)
    month is re-fetched once its cached copy is older than 24 hours. Payloads
    are hydrated with probable pitchers; stale v1 caches (which lack them)
    are removed so nothing ever reads the unhydrated shape again.
    """
    _legacy_chunk_path(season, month).unlink(missing_ok=True)
    path = _chunk_path(season, month)
    mutable = (season, month) >= (today.year, today.month)
    if path.exists():
        fresh = time.time() - path.stat().st_mtime < CHUNK_MAX_AGE_SECONDS
        if not mutable or fresh:
            return json.loads(path.read_text())
    last_day = calendar.monthrange(season, month)[1]
    resp = await client.get(
        SCHEDULE_URL,
        params={
            "sportId": 1,
            "startDate": f"{season:04d}-{month:02d}-01",
            "endDate": f"{season:04d}-{month:02d}-{last_day:02d}",
            "gameType": GAME_TYPES,
            "hydrate": "probablePitcher",
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return payload


async def _load_completed_games(today: date | None = None) -> list[dict]:
    """All completed games from FIRST_SEASON through today, oldest first."""
    today = today or datetime.now(timezone.utc).date()
    rows: list[dict] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for season in range(FIRST_SEASON, today.year + 1):
            for season_, month in _season_months(season, today):
                payload = await _fetch_chunk(client, season_, month, today)
                rows.extend(_parse_schedule(payload))
    rows.sort(key=lambda r: (r["season"], r["date"], r["game_pk"]))
    return rows


def _parse_schedule(payload: dict) -> list[dict]:
    """Completed-game rows out of one schedule payload.

    Keeps only finals (codedGameState "F") of the expected game types with
    both scores present; team names are canonicalized to ESPN displayNames.
    Probable pitchers (present on historical finals when the payload was
    hydrated) ride along as id/name, None when unknown.
    """
    rows: list[dict] = []
    for day in payload.get("dates", []):
        for game in day.get("games", []):
            if game.get("gameType") not in GAME_TYPE_SET:
                continue
            if game.get("status", {}).get("codedGameState") != "F":
                continue
            teams = game.get("teams", {})
            home = _canon(teams.get("home", {}).get("team", {}).get("name"))
            away = _canon(teams.get("away", {}).get("team", {}).get("name"))
            home_score = teams.get("home", {}).get("score")
            away_score = teams.get("away", {}).get("score")
            when = game.get("officialDate") or day.get("date")
            if None in (home, away, home_score, away_score, when):
                continue
            home_pp = teams.get("home", {}).get("probablePitcher") or {}
            away_pp = teams.get("away", {}).get("probablePitcher") or {}
            rows.append(
                {
                    "game_pk": game.get("gamePk"),
                    "date": when,
                    "season": int(when[:4]),
                    "home": home,
                    "away": away,
                    "home_score": int(home_score),
                    "away_score": int(away_score),
                    "home_pitcher_id": home_pp.get("id"),
                    "home_pitcher_name": home_pp.get("fullName"),
                    "away_pitcher_id": away_pp.get("id"),
                    "away_pitcher_name": away_pp.get("fullName"),
                }
            )
    return rows


# -- starting-pitcher ratings ---------------------------------------------------


def _pitcher_adj(
    pitchers: dict[str, dict], pitcher_id, k_pitcher: float = PITCHER_K
) -> float:
    """Elo-point adjustment for one starter; 0 when unknown or unseen.

    A pitcher's EWMA starts at league average, so a debutant also nets 0 —
    the adjustment only grows as real starts pull the EWMA off the prior.
    """
    if pitcher_id is None:
        return 0.0
    entry = pitchers.get(str(pitcher_id))
    if not entry:
        return 0.0
    adj = k_pitcher * (LEAGUE_AVG_RUNS - entry["ewma"])
    return max(-PITCHER_ADJ_CAP, min(PITCHER_ADJ_CAP, adj))


def _record_start(
    pitchers: dict[str, dict],
    pitcher_id,
    name: str | None,
    runs_allowed: int,
    span: int = PITCHER_SPAN,
) -> None:
    """Fold one start (team runs conceded) into the pitcher's EWMA.

    Initializing at LEAGUE_AVG_RUNS is the shrinkage: with alpha=2/(span+1),
    one great start moves a rookie only ~10% of the way off the prior.
    """
    if pitcher_id is None:
        return
    entry = pitchers.setdefault(
        str(pitcher_id), {"name": name, "ewma": LEAGUE_AVG_RUNS, "starts": 0}
    )
    alpha = 2.0 / (span + 1.0)
    entry["ewma"] += alpha * (runs_allowed - entry["ewma"])
    entry["starts"] += 1
    if name:
        entry["name"] = name


def _expected_home_prob(
    book: EloBook, home: str, away: str, pitcher_diff: float = 0.0
) -> float:
    """P(home wins) with the pitcher Elo-point difference folded in."""
    diff = (
        book.get(home)
        + book.config.home_advantage
        - book.get(away)
        + pitcher_diff
    )
    return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))


def _replay(
    rows: list[dict],
    k_pitcher: float | None = None,
    pitcher_span: int = PITCHER_SPAN,
    validate_from: int = VALIDATE_FROM_SEASON,
    log: list | None = None,
) -> tuple[EloBook, dict[str, deque], dict[str, dict], dict]:
    """Replay completed games chronologically through a fresh EloBook.

    Returns the fitted book, per-team rolling (runs_for, runs_against)
    windows, per-pitcher EWMA ratings, and walk-forward metrics
    (pre-update probabilities, pitcher ratings from prior starts only) over
    `validate_from` onward. Team Elo updates never see the pitcher overlay,
    so team ratings match a pitcher-less replay exactly. Pure function of
    the rows so tests can drive it with synthetic data. Duplicate gamePks
    (resumed/suspended games listed on two dates) count once.

    When `log` is given, each validation game also appends
    [date, prob, outcome, home, away] to it — the per-game walk-forward log
    the state file persists (store/backtest.py renders it, and the dollar-ROI
    backtest joins it against backfilled Polymarket prices by team + date).
    """
    if k_pitcher is None:
        k_pitcher = PITCHER_K if PITCHER_ADJ_ENABLED else 0.0
    book = EloBook(config=ELO_CONFIG)
    rolling: dict[str, deque] = {}
    pitchers: dict[str, dict] = {}
    records: list[dict] = []
    seen: set = set()

    current_season: int | None = None
    for row in rows:
        if row["game_pk"] in seen:
            continue
        seen.add(row["game_pk"])
        if current_season is not None and row["season"] != current_season:
            book.new_season()
        current_season = row["season"]

        home, away = row["home"], row["away"]
        home_score, away_score = row["home_score"], row["away_score"]
        if home_score == away_score:  # no binary outcome — never happens in MLB
            continue
        home_won = home_score > away_score

        home_pid = row.get("home_pitcher_id")
        away_pid = row.get("away_pitcher_id")

        if row["season"] >= validate_from:
            pitcher_diff = _pitcher_adj(pitchers, home_pid, k_pitcher) - _pitcher_adj(
                pitchers, away_pid, k_pitcher
            )
            p = _expected_home_prob(book, home, away, pitcher_diff)
            records.append(
                {
                    "p": p,
                    "home_won": home_won,
                    "both_pitchers": home_pid is not None and away_pid is not None,
                }
            )
            if log is not None:
                log.append([row["date"], round(p, 6), int(home_won), home, away])

        book.update(home, away, home_won, margin=abs(home_score - away_score))
        _record_start(
            pitchers, home_pid, row.get("home_pitcher_name"), away_score, pitcher_span
        )
        _record_start(
            pitchers, away_pid, row.get("away_pitcher_name"), home_score, pitcher_span
        )
        for team, rf, ra in (
            (home, home_score, away_score),
            (away, away_score, home_score),
        ):
            rolling.setdefault(team, deque(maxlen=ROLLING_WINDOW)).append((rf, ra))

    return book, rolling, pitchers, _compute_metrics(records)


def _log_loss(pairs: list[tuple[float, bool]]) -> float:
    """Mean negative log-likelihood over (prob_of_home, home_won) pairs."""
    eps = 1e-12
    total = 0.0
    for p, won in pairs:
        p = min(max(p, eps), 1.0 - eps)
        total -= math.log(p if won else 1.0 - p)
    return total / len(pairs)


def _compute_metrics(records: list[dict]) -> dict:
    """Score walk-forward probabilities on the validation slice."""
    if not records:
        return {}
    return {
        "n_games": len(records),
        "elo_accuracy": sum((r["p"] >= 0.5) == r["home_won"] for r in records)
        / len(records),
        "elo_log_loss": _log_loss([(r["p"], r["home_won"]) for r in records]),
        "home_win_rate": sum(r["home_won"] for r in records) / len(records),
        "pitcher_coverage": sum(r.get("both_pitchers", False) for r in records)
        / len(records),
    }


async def _fetch_probables(dates: list[str]) -> dict[tuple[str, str], int]:
    """(officialDate, canonical team name) -> probable pitcher id for a slate.

    One schedule call spanning the dates, hydrated the same way as the
    replay caches. Any failure degrades to an empty map (zero adjustment).
    """
    if not dates:
        return {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                SCHEDULE_URL,
                params={
                    "sportId": 1,
                    "startDate": min(dates),
                    "endDate": max(dates),
                    "gameType": GAME_TYPES,
                    "hydrate": "probablePitcher",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception:
        return {}
    probables: dict[tuple[str, str], int] = {}
    for day in payload.get("dates", []):
        for game in day.get("games", []):
            when = game.get("officialDate") or day.get("date")
            for side in ("home", "away"):
                entry = game.get("teams", {}).get(side, {})
                team = _canon(entry.get("team", {}).get("name"))
                pid = (entry.get("probablePitcher") or {}).get("id")
                if when and team and pid:
                    probables[(when, team)] = pid
    return probables


class MlbEngine(SportEngine):
    sport = Sport.MLB
    model_name = "mlb-elo-v3"

    def __init__(self, state_path: Path = STATE_PATH) -> None:
        self.state_path = state_path
        self.book = EloBook(config=ELO_CONFIG)
        self.rolling: dict[str, deque] = {}
        self.pitchers: dict[str, dict] = {}
        self.metrics: dict = {}
        # Walk-forward per-game log ([date, prob, outcome, home, away]) —
        # persisted for the backtest panel + the dollar-ROI price join.
        self.backtest_log: list = []
        self._load_state()

    async def upcoming_games(self, on: date | None = None) -> list[Game]:
        return await espn.scoreboard(Sport.MLB, on)

    async def predict(self, games: list[Game]) -> list[Prediction]:
        now = datetime.now(timezone.utc)
        probables = await self._slate_probables(games)
        predictions: list[Prediction] = []
        for game in games:
            pitcher_diff = _pitcher_adj(
                self.pitchers, self._probable_for(probables, game, game.home)
            ) - _pitcher_adj(
                self.pitchers, self._probable_for(probables, game, game.away)
            )
            p_home = _expected_home_prob(self.book, game.home, game.away, pitcher_diff)
            home_runs = (self._avg(game.home, idx=0) + self._avg(game.away, idx=1)) / 2.0
            away_runs = (self._avg(game.away, idx=0) + self._avg(game.home, idx=1)) / 2.0
            # A team with no scoring history would price at the league-average
            # placeholder — a constant, not a forecast. The board printed UNDER
            # edges against exactly that (WNBA, 2026-09-02). Emit no total.
            scored = bool(self.rolling.get(game.home)) and bool(self.rolling.get(game.away))
            predictions.append(
                Prediction(
                    game_id=game.id,
                    sport=Sport.MLB,
                    model=self.model_name,
                    generated_at=now,
                    home_win_prob=p_home,
                    away_win_prob=1.0 - p_home,
                    predicted_home_score=round(home_runs, 1) if scored else None,
                    predicted_away_score=round(away_runs, 1) if scored else None,
                    predicted_total=round(home_runs + away_runs, 1) if scored else None,
                    confidence=abs(p_home - 0.5) * 2.0,
                )
            )
        return predictions

    async def refresh_data(self) -> None:
        rows = await _load_completed_games()
        log: list = []
        self.book, self.rolling, self.pitchers, self.metrics = _replay(rows, log=log)
        self.backtest_log = log
        self._save_state()

    # -- probable pitchers ----------------------------------------------------

    async def _slate_probables(self, games: list[Game]) -> dict[tuple[str, str], int]:
        """Probable-pitcher map for the games' dates; empty when gated off.

        ESPN scoreboards don't carry probables, so this is one statsapi
        schedule call keyed by (officialDate, team name).
        """
        if not PITCHER_ADJ_ENABLED or not games:
            return {}
        dates: set[str] = set()
        for game in games:
            d = game.start_time.date()
            # officialDate is US-local; late UTC times can be the prior US day.
            dates.add(d.isoformat())
            dates.add((d - timedelta(days=1)).isoformat())
        return await _fetch_probables(sorted(dates))

    @staticmethod
    def _probable_for(
        probables: dict[tuple[str, str], int], game: Game, team: str
    ) -> int | None:
        d = game.start_time.date()
        for when in (d.isoformat(), (d - timedelta(days=1)).isoformat()):
            pid = probables.get((when, team))
            if pid is not None:
                return pid
        return None

    # -- rolling runs helpers ------------------------------------------------

    def _avg(self, team: str, idx: int) -> float:
        """Rolling mean of runs for (idx=0) / against (idx=1) for a team."""
        window = self.rolling.get(team)
        if not window:
            return LEAGUE_AVG_RUNS
        return sum(g[idx] for g in window) / len(window)

    # -- state persistence -----------------------------------------------------

    def _save_state(self) -> None:
        state = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": self.model_name,
            "ratings": self.book.ratings,
            "rolling": {team: list(w) for team, w in self.rolling.items()},
            "pitchers": self.pitchers,
            "metrics": self.metrics,
            "backtest": self.backtest_log,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2))

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        state = json.loads(self.state_path.read_text())
        self.book = EloBook(config=ELO_CONFIG, ratings=dict(state.get("ratings", {})))
        self.rolling = {
            team: deque((tuple(g) for g in window), maxlen=ROLLING_WINDOW)
            for team, window in state.get("rolling", {}).items()
        }
        self.pitchers = dict(state.get("pitchers", {}))
        self.metrics = state.get("metrics", {})
        self.backtest_log = list(state.get("backtest") or [])


def build_engine() -> MlbEngine:
    return MlbEngine()


async def _main() -> None:
    engine = build_engine()
    await engine.refresh_data()

    m = engine.metrics
    print(f"\n{engine.model_name} — replay {FIRST_SEASON}+, "
          f"walk-forward validation {VALIDATE_FROM_SEASON} to date")
    print(f"  games scored:     {m['n_games']}")
    print(f"  Elo accuracy:     {m['elo_accuracy']:.4f}")
    print(f"  Elo log loss:     {m['elo_log_loss']:.4f}")
    print(f"  home win rate:    {m['home_win_rate']:.4f}")
    print(f"  pitcher coverage: {m['pitcher_coverage']:.4f}")

    top = sorted(engine.book.ratings.items(), key=lambda kv: kv[1], reverse=True)[:5]
    print("\n  current top 5 Elo:")
    for name, rating in top:
        print(f"    {rating:7.1f}  {name}")

    games = await engine.upcoming_games()
    predictions = await engine.predict(games)
    print(f"\n  today's slate ({len(games)} games):")
    for game, pred in zip(games, predictions):
        print(f"    {game.away} @ {game.home}: "
              f"home {pred.home_win_prob:.3f} / away {pred.away_win_prob:.3f}, "
              f"total {pred.predicted_total}")


if __name__ == "__main__":
    asyncio.run(_main())

"""NCAAF engine — Elo replayed over cfbfastR (sportsdataverse) schedule data.

v1 is an Elo baseline tuned for college football's quirks: K=25 (shorter
seasons and higher variance than the NFL), 65 rating points of home advantage
(zeroed for neutral-site games — bowls, kickoff classics — both in the
historical replay and at predict time via the ESPN scoreboard's neutral-site
flag on Game), margin-of-victory K scaling, and only 60% season-to-season
carry (roster turnover in college is brutal, so ratings regress 40% to the
mean every August).

Data: per-season schedule/result CSVs published keylessly by the
sportsdataverse cfbfastR-data repo (one file per season, sourced from
CollegeFootballData.com). Each row carries season, week, start_date, both
schools' names, division classification (fbs/fcs/...), final points, and a
neutral_site flag. Seasons 2021+ replay chronologically; the most recent
completed season is the walk-forward backtest slice.

FBS-only ratings: games against non-FBS opponents (FCS buy games, etc.) update
the FBS side against a flat 1250 "generic FCS" rating instead of creating and
polluting hundreds of lower-division entries — the FCS team is never stored.
Games with no FBS side are skipped entirely.

Team naming: cfbfastR uses school names ("Georgia"); the rest of the terminal
(ESPN scoreboard, odds matching) uses ESPN displayName ("Georgia Bulldogs").
ESPN's team directory carries `location` == the school name, so a live teams
fetch (cached) yields a school -> displayName map and the Elo book is keyed by
displayName throughout. Coverage is exact for every FBS school 2021-2025; an
accent-folded lookup backstops encoding drift, and an unmatched school falls
back to its own name so it still keeps a coherent rating.

FCS opponents in the Kalman filter (v3, 2026-11-14). The served win
probability comes from the Kalman book, and until v3 that book had no notion
of an FCS side at predict time: the replay stored FCS teams under their raw
school names, the live board asked for them by ESPN displayName, missed, and
priced every FCS visitor as (0, init_var) — an average FBS team. Held out
(2025 + 2026, 249 FBS-vs-FCS games) that cost 0.514 log loss on games the
FBS side wins 95% of the time. v3 prices an FCS side as one generic prior
(FCS_PRIOR, fitted by moments on 2022-24: mean −29.4 points, variance 58),
used in the update and never stored — the Kalman counterpart of FCS_RATING.
Same games: 0.155. All served games 2025-26: 0.5397 -> 0.4834.

Season roll-over regresses toward the conference mean (CONF_REGRESS_WEIGHT)
rather than zero: −0.004 [−0.009, +0.001] on the same held-out games, all
of it in weeks 1-4. Set the weight to 0.0 and KALMAN_CONFIG.carry back to
0.85 to switch it off.

Optional enrichment: when CFBD_API_KEY is set, closing spreads from
CollegeFootballData.com score the market's straight-up favorite on the
backtest slice for comparison. Without the key (or on any API failure) the
comparison is silently skipped — it is never required.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
import unicodedata
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
import pandas as pd

from kryptscout.config import DATA_DIR, RAW_DIR
from kryptscout.core.kalman import KalmanBook, KalmanConfig, conference_targets
from kryptscout.core.elo import EloBook, EloConfig
from kryptscout.core.schemas import Game, Prediction, Sport
from kryptscout.datasources import espn
from kryptscout.sports.base import SportEngine

SCHEDULE_URL = (
    "https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main/"
    "schedules/csv/cfb_schedules_{season}.csv"
)
TEAMS_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams"
)
CFBD_LINES_URL = "https://api.collegefootballdata.com/lines"
CFBD_API_KEY = os.environ.get("CFBD_API_KEY", "")

RAW_NCAAF_DIR = RAW_DIR / "ncaaf"
TEAMS_CACHE_PATH = RAW_NCAAF_DIR / "espn_teams.json"
STATE_PATH = DATA_DIR / "ncaaf_state.json"

FIRST_SEASON = 2021
BACKTEST_SEASON = 2025  # most recent completed season; scored walk-forward
SEASON_BOUNDARY_MONTH = 8  # August: season key = calendar year the season kicks off
CSV_MAX_AGE_SECONDS = 24 * 3600  # re-fetch window for an in-progress season
TEAMS_MAX_AGE_SECONDS = 30 * 24 * 3600  # FBS membership shifts ~yearly

FCS_RATING = 1250.0  # flat rating for any non-FBS opponent (never stored)
ROLLING_WINDOW = 8  # games of points for/against feeding score predictions
HOME_POINTS_EDGE = 1.5  # points shifted home-ward in predicted scores
LEAGUE_AVG_POINTS = 27.5  # per-team fallback (~55-point college totals)

ELO_CONFIG = EloConfig(k=25.0, home_advantage=65.0, mov_scaling=True, season_carry=0.60)


def _current_season(today: date) -> int:
    """Season labelled by kickoff year: the 2025 season runs Aug 2025-Jan 2026."""
    return today.year if today.month >= SEASON_BOUNDARY_MONTH else today.year - 1


def _season_is_final(season: int, today: date) -> bool:
    """A season's file stops changing once its championship window has passed."""
    return today >= date(season + 1, 2, 1)


# -- team-name resolution -----------------------------------------------------


def _fold(name: str) -> str:
    """Accent-insensitive, case-insensitive key ("San José State" == "san jose state")."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower().strip()


def make_resolver(school_to_display: dict[str, str]) -> Callable[[str], str]:
    """school name -> ESPN displayName, tolerant of accent/case drift.

    An unmatched school resolves to itself: its Elo identity stays coherent
    even if it can't be joined to the ESPN board (only affects brand-new or
    renamed programs until the teams cache refreshes).
    """
    folded = {_fold(school): display for school, display in school_to_display.items()}

    def resolve(school: str) -> str:
        return school_to_display.get(school) or folded.get(_fold(school)) or school

    return resolve


def _parse_teams_payload(payload: dict) -> dict[str, str]:
    """ESPN teams directory -> {location ("Georgia"): displayName ("Georgia Bulldogs")}.

    The directory covers every division, and a few locations appear twice:
    "Charlotte" is the FBS 49ers (id 2429) and a small college's Saints (id
    3253); "Troy" is the Trojans (2653) and Hudson Valley's Vikings (3237).
    Last-entry-wins filed both FBS programs under the small school's name, so
    the live board's "Charlotte 49ers" / "Troy Trojans" never matched their
    ratings. The directory carries no division, but ESPN numbered the major
    programs first, so a shared location goes to the lowest team id.
    """
    mapping: dict[str, str] = {}
    owner: dict[str, float] = {}
    for sport in payload.get("sports", []):
        for league in sport.get("leagues", []):
            for entry in league.get("teams", []):
                team = entry.get("team", {})
                location, display = team.get("location"), team.get("displayName")
                if not (location and display):
                    continue
                try:
                    team_id = float(team.get("id"))
                except (TypeError, ValueError):
                    team_id = float("inf")
                if location not in mapping or team_id < owner[location]:
                    mapping[location] = display
                    owner[location] = team_id
    return mapping


async def _load_name_map(client: httpx.AsyncClient) -> dict[str, str]:
    """School->displayName map from ESPN, cached 30 days; stale cache beats no map."""
    cache = TEAMS_CACHE_PATH
    if cache.exists() and time.time() - cache.stat().st_mtime < TEAMS_MAX_AGE_SECONDS:
        return _parse_teams_payload(json.loads(cache.read_text(encoding="utf-8")))
    try:
        resp = await client.get(TEAMS_URL, params={"limit": "1000"})
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError):
        if cache.exists():  # network hiccup — fall back to whatever we have
            return _parse_teams_payload(json.loads(cache.read_text(encoding="utf-8")))
        raise
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(payload), encoding="utf-8")
    return _parse_teams_payload(payload)


# -- data download --------------------------------------------------------------


async def _download_season(
    client: httpx.AsyncClient, season: int, today: date
) -> Path | None:
    """Fetch one season's CSV into RAW_DIR/ncaaf; None when not yet published.

    Finished seasons cache forever; an in-progress season refreshes daily.
    """
    path = RAW_NCAAF_DIR / f"cfb_schedules_{season}.csv"
    if path.exists() and (
        _season_is_final(season, today)
        or time.time() - path.stat().st_mtime < CSV_MAX_AGE_SECONDS
    ):
        return path
    resp = await client.get(SCHEDULE_URL.format(season=season))
    if resp.status_code == 404:  # future season not published yet
        return path if path.exists() else None
    resp.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    return path


# -- Elo replay -------------------------------------------------------------------


# Kalman margin filter (core/kalman.py) — the served win probability since
# v2. Tuned 2026-09-02 on cfbd 2021-26 (tune 2023-24, held out 2025,
# n=3,829): log loss 0.4774 vs the Elo config's 0.5251, −0.0477
# [−0.0556, −0.0389], accuracy 75.9%. Elo's margin-of-victory multiplier
# compresses the blowouts that carry most of college football's information.
# The Elo book still runs: it feeds the score nudge and the FCS handling.
#
# v3 (2026-11-14): carry 0.85 -> 0.55 TOGETHER WITH regression toward the
# conference mean (CONF_REGRESS_WEIGHT = 1.0). The pair was picked on
# 2022-24 from a 9-point grid; the other constants are unchanged (a full
# retune of sigma/q/hfa/variances moved held-out log loss by −0.001
# [−0.004, +0.002] — nothing).
KALMAN_CONFIG = KalmanConfig(
    sigma=15.0, q=3.0, hfa=2.5, init_var=150.0, season_var=30.0, carry=0.55
)
CONF_REGRESS_WEIGHT = 1.0  # 0.0 = regress to zero (pre-v3; then use carry=0.85)

# Generic FCS opponent as the Kalman filter sees it: (mean, variance) in
# points relative to the average FBS team. Fitted by moments on the 359
# FBS-vs-FCS games of 2022-24 (minus the mean innovation; excess innovation
# variance). Used for the game, never written to the book.
FCS_PRIOR: tuple[float, float] = (-29.4, 58.0)


def _expected_prob(home_rating: float, away_rating: float, hfa: float) -> float:
    """P(home wins) given explicit ratings and per-game home advantage."""
    diff = home_rating + hfa - away_rating
    return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))


def _game_update(
    book: EloBook,
    home: str,
    away: str,
    home_won: bool,
    margin: int,
    hfa: float,
    home_rated: bool = True,
    away_rated: bool = True,
) -> float:
    """One game through the book with per-game HFA; returns the pre-game P(home).

    Mirrors EloBook.update (same MOV multiplier) but supports neutral sites
    (hfa=0) and frozen non-FBS sides: an unrated side plays at FCS_RATING and
    is never written back, so buy games move only the FBS team.
    """
    r_home = book.get(home) if home_rated else FCS_RATING
    r_away = book.get(away) if away_rated else FCS_RATING
    p_home = _expected_prob(r_home, r_away, hfa)

    tie = margin == 0
    actual = 0.5 if tie else (1.0 if home_won else 0.0)
    k = book.config.k
    if book.config.mov_scaling and margin > 0:
        elo_diff = r_home - r_away
        if not home_won:
            elo_diff = -elo_diff
        k *= math.log(margin + 1) * (2.2 / (elo_diff * 0.001 + 2.2))
    delta = k * (actual - p_home)
    if home_rated:
        book.ratings[home] = r_home + delta
    if away_rated:
        book.ratings[away] = r_away - delta
    return p_home


def _season_conferences(
    df: pd.DataFrame, resolve: Callable[[str], str]
) -> dict[int, dict[str, str]]:
    """season -> {FBS displayName: conference}, from every row of that season
    (scheduled games included, so the map exists before week 1)."""
    out: dict[int, dict[str, str]] = {}
    if "home_conference" not in df.columns or "away_conference" not in df.columns:
        return out
    for side in ("home", "away"):
        rows = df[df[f"{side}_division"] == "fbs"]
        for season, team, conf in zip(
            rows["season"], rows[f"{side}_team"], rows[f"{side}_conference"]
        ):
            if isinstance(conf, str) and conf:
                out.setdefault(int(season), {})[resolve(team)] = conf
    return out


def _replay(
    df: pd.DataFrame,
    resolve: Callable[[str], str],
    fcs_records: list[dict] | None = None,
) -> tuple[EloBook, dict[str, deque], list[dict], KalmanBook]:
    """Replay completed games chronologically through a fresh EloBook.

    Returns the fitted book (keyed by ESPN displayName), per-team rolling
    (points_for, points_against) windows for FBS teams, and one backtest
    record per decided FBS-vs-FBS game from BACKTEST_SEASON onward, carrying
    the pre-update probability. Pure function of the frame so tests can drive
    it with synthetic data.

    `fcs_records`, when given, collects the same record for every decided
    FBS-vs-FCS game of the backtest seasons: the board serves those games
    too, and until v3 nothing scored them.
    """
    book = EloBook(config=ELO_CONFIG)
    kalman = KalmanBook(KALMAN_CONFIG)
    conferences = _season_conferences(df, resolve) if CONF_REGRESS_WEIGHT > 0 else {}
    rolling: dict[str, deque] = {}
    records: list[dict] = []

    played = df[
        (df["completed"] == True)  # noqa: E712 — pandas elementwise
        & df["home_points"].notna()
        & df["away_points"].notna()
    ].sort_values(["season", "start_date"], kind="stable")

    current_season: int | None = None
    for row in played.itertuples(index=False):
        home_fbs = row.home_division == "fbs"
        away_fbs = row.away_division == "fbs"
        if not home_fbs and not away_fbs:  # lower-division game — irrelevant
            continue

        if current_season is not None and row.season != current_season:
            book.new_season()
            kalman.new_season(
                conference_targets(
                    kalman.means, conferences.get(int(row.season), {}), CONF_REGRESS_WEIGHT
                )
            )
        current_season = row.season

        home = resolve(row.home_team) if home_fbs else row.home_team
        away = resolve(row.away_team) if away_fbs else row.away_team
        home_score, away_score = int(row.home_points), int(row.away_points)
        home_won = home_score > away_score
        margin = abs(home_score - away_score)
        hfa = 0.0 if bool(row.neutral_site) else ELO_CONFIG.home_advantage

        p_elo = _game_update(
            book, home, away, home_won, margin, hfa,
            home_rated=home_fbs, away_rated=away_fbs,
        )
        # An FCS side is the generic prior: the FBS side learns from the
        # game, the FCS team itself never enters the Kalman book.
        p_home = kalman.update(
            home, away, home_score, away_score, neutral=bool(row.neutral_site),
            home_prior=None if home_fbs else FCS_PRIOR,
            away_prior=None if away_fbs else FCS_PRIOR,
        )
        if row.season >= BACKTEST_SEASON and margin > 0:
            record = {
                "game_id": int(row.game_id),
                "season": int(row.season),
                "p": p_home,
                "p_elo": p_elo,
                "home_won": home_won,
            }
            if home_fbs and away_fbs:
                records.append(record)
            elif fcs_records is not None:
                fcs_records.append(record)

        for fbs, team, pf, pa in (
            (home_fbs, home, home_score, away_score),
            (away_fbs, away, away_score, home_score),
        ):
            if fbs:
                rolling.setdefault(team, deque(maxlen=ROLLING_WINDOW)).append((pf, pa))

    return book, rolling, records, kalman


# -- backtest metrics ------------------------------------------------------------


def _log_loss(pairs: list[tuple[float, bool]]) -> float:
    """Mean negative log-likelihood over (prob_of_home, home_won) pairs."""
    eps = 1e-12
    total = 0.0
    for p, won in pairs:
        p = min(max(p, eps), 1.0 - eps)
        total -= math.log(p if won else 1.0 - p)
    return total / len(pairs)


def _compute_metrics(
    records: list[dict], spreads: dict[int, float] | None = None
) -> dict:
    """Score the logged pre-game probabilities; optionally against the market.

    `spreads` maps game_id -> closing home spread (negative = home favored).
    The market comparison is restricted to games with a non-pick'em closing
    spread, and Elo is re-scored on that exact subset so the accuracies are
    directly comparable. A spread implies only a favorite, not a probability,
    so no market log loss is reported.
    """
    if not records:
        return {}
    metrics = {
        "n_games": len(records),
        "elo_accuracy": sum((r["p"] >= 0.5) == r["home_won"] for r in records)
        / len(records),
        "elo_log_loss": _log_loss([(r["p"], r["home_won"]) for r in records]),
    }
    if spreads:
        priced = [
            r
            for r in records
            if r["game_id"] in spreads and spreads[r["game_id"]] != 0.0
        ]
        if priced:
            metrics.update(
                {
                    "n_games_with_spread": len(priced),
                    "elo_accuracy_vs_market": sum(
                        (r["p"] >= 0.5) == r["home_won"] for r in priced
                    )
                    / len(priced),
                    "market_accuracy": sum(
                        (spreads[r["game_id"]] < 0.0) == r["home_won"] for r in priced
                    )
                    / len(priced),
                }
            )
    return metrics


def _closing_spread(lines: list[dict]) -> float | None:
    """Pick one closing home spread from a CFBD lines array (consensus first)."""
    fallback: float | None = None
    for line in lines:
        raw = line.get("spread")
        if raw is None:
            continue
        try:
            spread = float(raw)
        except (TypeError, ValueError):
            continue
        if str(line.get("provider", "")).lower() == "consensus":
            return spread
        if fallback is None:
            fallback = spread
    return fallback


async def _fetch_cfbd_spreads(
    client: httpx.AsyncClient, season: int
) -> dict[int, float] | None:
    """Closing spreads by game_id from CollegeFootballData, or None.

    Purely optional enrichment: returns None (silently) when CFBD_API_KEY is
    unset or the API misbehaves — the backtest simply skips the comparison.
    """
    if not CFBD_API_KEY:
        return None
    spreads: dict[int, float] = {}
    try:
        for season_type in ("regular", "postseason"):
            resp = await client.get(
                CFBD_LINES_URL,
                params={"year": season, "seasonType": season_type},
                headers={"Authorization": f"Bearer {CFBD_API_KEY}"},
            )
            resp.raise_for_status()
            for game in resp.json():
                spread = _closing_spread(game.get("lines", []))
                if spread is not None:
                    spreads[int(game["id"])] = spread
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return spreads or None


# -- engine ------------------------------------------------------------------------


class NcaafEngine(SportEngine):
    sport = Sport.NCAAF
    model_name = "ncaaf-kalman-v3"

    def __init__(self, state_path: Path = STATE_PATH) -> None:
        self.state_path = state_path
        self.book = EloBook(config=ELO_CONFIG)
        self.kalman = KalmanBook(KALMAN_CONFIG)
        # FBS membership (displayNames) of the current season, from the
        # schedule file. Empty until the first v3 refresh; from then on any
        # side not in it is priced as the generic FCS prior.
        self.fbs_teams: set[str] = set()
        self.rolling: dict[str, deque] = {}
        self.metrics: dict = {}
        self._load_state()

    async def upcoming_games(self, on: date | None = None) -> list[Game]:
        return await espn.scoreboard(Sport.NCAAF, on)

    async def predict(self, games: list[Game]) -> list[Prediction]:
        now = datetime.now(timezone.utc)
        predictions: list[Prediction] = []
        for game in games:
            # Neutral-site games (bowls, kickoff classics) carry no home
            # edge: zero Elo HFA and no home-ward points shift.
            points_edge = 0.0 if game.neutral_site else HOME_POINTS_EDGE
            p_home = self.kalman.expected(
                game.home, game.away, neutral=game.neutral_site,
                home_prior=self._prior(game.home), away_prior=self._prior(game.away),
            )
            home_score = (
                self._avg(game.home, idx=0) + self._avg(game.away, idx=1)
            ) / 2.0 + points_edge
            away_score = (
                self._avg(game.away, idx=0) + self._avg(game.home, idx=1)
            ) / 2.0 - points_edge
            # A team with no scoring history would price at the league-average
            # placeholder — a constant, not a forecast. The board printed UNDER
            # edges against exactly that (WNBA, 2026-09-02). Emit no total.
            scored = bool(self.rolling.get(game.home)) and bool(self.rolling.get(game.away))
            predictions.append(
                Prediction(
                    game_id=game.id,
                    sport=Sport.NCAAF,
                    model=self.model_name,
                    generated_at=now,
                    home_win_prob=p_home,
                    away_win_prob=1.0 - p_home,
                    predicted_home_score=round(home_score, 1) if scored else None,
                    predicted_away_score=round(away_score, 1) if scored else None,
                    predicted_total=round(home_score + away_score, 1) if scored else None,
                    confidence=abs(p_home - 0.5) * 2.0,
                )
            )
        return predictions

    async def refresh_data(self) -> None:
        """Sync season CSVs (finished seasons stay cached) and replay into Elo."""
        today = datetime.now(timezone.utc).date()
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resolve = make_resolver(await _load_name_map(client))
            frames = []
            for season in range(FIRST_SEASON, today.year + 1):
                path = await _download_season(client, season, today)
                if path is not None:
                    frames.append(pd.read_csv(path, low_memory=False))
            spreads = await _fetch_cfbd_spreads(client, BACKTEST_SEASON)
        df = pd.concat(frames, ignore_index=True)
        fcs_records: list[dict] = []
        self.book, self.rolling, records, self.kalman = _replay(df, resolve, fcs_records)
        self.metrics = _compute_metrics(records, spreads)
        if fcs_records:
            self.metrics["n_fcs_games"] = len(fcs_records)
            self.metrics["fcs_log_loss"] = _log_loss(
                [(r["p"], r["home_won"]) for r in fcs_records]
            )
        latest = df[df["season"] == df["season"].max()]
        self.fbs_teams = {
            resolve(team)
            for side in ("home", "away")
            for team in latest.loc[latest[f"{side}_division"] == "fbs", f"{side}_team"]
        }
        self._save_state()

    def _prior(self, team: str) -> tuple[float, float] | None:
        """FCS_PRIOR for a side that is not an FBS team this season, else None.

        Without a membership list (a state written before v3) every side is
        looked up in the book as before, so nothing changes until a refresh.
        """
        if not self.fbs_teams or team in self.fbs_teams:
            return None
        return FCS_PRIOR

    # -- rolling points helpers -------------------------------------------

    def _avg(self, team: str, idx: int) -> float:
        """Rolling mean of points for (idx=0) / against (idx=1) for a team."""
        window = self.rolling.get(team)
        if not window:
            return LEAGUE_AVG_POINTS
        return sum(g[idx] for g in window) / len(window)

    # -- state persistence -------------------------------------------------

    def _save_state(self) -> None:
        state = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": self.model_name,
            "ratings": self.book.ratings,
            "kalman": self.kalman.to_state(),
            "fbs_teams": sorted(self.fbs_teams),
            "rolling": {team: list(w) for team, w in self.rolling.items()},
            "metrics": self.metrics,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.book = EloBook(
            config=ELO_CONFIG,
            ratings={k: float(v) for k, v in state.get("ratings", {}).items()},
        )
        # A state written before v2 has no filter: fresh means/variances until
        # the next refresh replays the archive (deterministic, seconds).
        self.kalman = KalmanBook.from_state(KALMAN_CONFIG, state.get("kalman"))
        # A pre-v3 state stored FCS teams in the filter under school names and
        # has no membership list; both are rebuilt by the next refresh.
        self.fbs_teams = (
            set(state.get("fbs_teams") or []) if state.get("model") == self.model_name else set()
        )
        self.rolling = {
            team: deque((tuple(g) for g in window), maxlen=ROLLING_WINDOW)
            for team, window in state.get("rolling", {}).items()
        }
        self.metrics = state.get("metrics", {})


def build_engine() -> NcaafEngine:
    return NcaafEngine()


if __name__ == "__main__":
    engine = build_engine()
    asyncio.run(engine.refresh_data())

    m = engine.metrics
    print(f"\n{engine.model_name} — replay {FIRST_SEASON}+, "
          f"backtest {BACKTEST_SEASON} (FBS vs FBS, pre-update probs)")
    print(f"  teams rated:      {len(engine.book.ratings)}")
    print(f"  games scored:     {m['n_games']}")
    # (the keys keep their historical "elo_" names; the numbers are the served Kalman p)
    print(f"  accuracy:         {m['elo_accuracy']:.4f}")
    print(f"  log loss:         {m['elo_log_loss']:.4f}")
    if "fcs_log_loss" in m:
        print(f"  FBS-vs-FCS games: {m['n_fcs_games']}  log loss {m['fcs_log_loss']:.4f}")
    if "market_accuracy" in m:
        print(f"\n  vs closing spread ({m['n_games_with_spread']} priced games):")
        print(f"    Elo accuracy:    {m['elo_accuracy_vs_market']:.4f}")
        print(f"    market accuracy: {m['market_accuracy']:.4f}")
    elif not CFBD_API_KEY:
        print("  (set CFBD_API_KEY for the closing-spread comparison)")

    top = sorted(engine.book.ratings.items(), key=lambda kv: kv[1], reverse=True)[:10]
    print("\n  current top 10 Elo:")
    for name, rating in top:
        print(f"    {rating:7.1f}  {name}")

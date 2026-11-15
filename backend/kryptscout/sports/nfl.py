"""NFL engine — 538-style Elo with QB/rest/preseason/weather overlays (nfl-elo-v2).

Team Elo (K=20, 48 points of home advantage, margin-of-victory K scaling, 1/3
regression to the mean between seasons — the public fivethirtyeight/
nfl-elo-game recipe) plus pre-game overlays that shift win probability only;
team Elo updates never see them.

  * Starting QB: per-QB EWMA of team points scored in his starts (span ~12,
    initialized at the league average so a debutant is fully shrunk to the
    mean). Game adjustment clamp(K_q * (home_qb_value - away_qb_value), ±90).
    The starter is the one games.csv lists for the upcoming game when it
    lists one, else the team's most recent starter.
  * Rest differential: REST_ELO_PER_DAY * (home_rest - away_rest) Elo points.
  * Neutral sites (Super Bowl, international): the 48-point home-advantage
    term is zeroed — games.csv's `location` column drives it in the replay,
    Game.neutral_site (parsed from ESPN) at predict time.
  * Preseason damping: a July/August ESPN game absent from the games.csv
    schedule is preseason; p' = 0.5 + 0.4*(p-0.5).
  * Weather totals: outdoor games inside Open-Meteo's horizon get a coarse
    totals multiplier (wind/precip, floor 0.873). Win prob never sees it.

One data file drives everything: Lee Sharpe's games.csv from nflverse
(MIT/CC-BY) — final scores from 1999, closing moneylines from 2006, starting
QBs and rest days backfilled to 1999. Replay runs from 2006, the start of
the closing-moneyline era. Overlay knobs were coarse-tuned on 2020-2023
only; 2024 onward is the held-out walk-forward validation set, scored on its
own and head-to-head against the devigged closing line
(`python -m kryptscout.sports.nfl` prints the increment table).

There is no trained model here any more. A v3 layer — an XGBoost classifier
over these same inputs plus schedule/venue features, blended 50/50 with this
Elo and served as `nfl-xgb-v3` — shipped until November 2026 and was
retired. On the 2024-25 held-out seasons (569 priced games) the blend moved
log loss from 0.6226 to 0.6220 and accuracy from 0.6714 to 0.6749; the
booster alone was worse than Elo on log loss (0.6254). That is a difference
this sample cannot distinguish from zero, bought with the xgboost and
scikit-learn runtimes in every installer, a version-stamped pickle under
MODELS_DIR, and the heaviest import of the cold start.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

from kryptscout.config import DATA_DIR, RAW_DIR
from kryptscout.core.elo import EloBook, EloConfig
from kryptscout.core.calibrate import temper
from kryptscout.core.oddsmath import remove_vig
from kryptscout.core.schemas import Game, Prediction, Sport
from kryptscout.datasources import espn, weather
from kryptscout.sports.base import SportEngine

logger = logging.getLogger("kryptscout")

GAMES_CSV_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
GAMES_CSV_PATH = RAW_DIR / "nfl" / "games.csv"
STATE_PATH = DATA_DIR / "nfl_state.json"
CSV_MAX_AGE_SECONDS = 24 * 3600

# Replay starts with the closing-moneyline era; QB/rest columns are covered
# from 1999 in current nflverse data, so every feature exists from day one.
REPLAY_FROM_SEASON = 2006
VALIDATE_FROM_SEASON = 2024  # held out; every knob/tune uses earlier seasons only
ROLLING_WINDOW = 10  # games of points for/against feeding score predictions
HOME_POINTS_EDGE = 1.25  # points shifted home-ward in predicted scores
LEAGUE_AVG_POINTS = 22.0  # fallback before a team has rolling history
CALIBRATION_TEMPERATURE = 0.84  # see predict(); 1.0 disables

# Starting-QB adjustment. Coarse grid (K_q 2-12, span 8-20) tuned on
# 2020-2023: log loss 0.6438 -> 0.6422 (QB only). The tune surface is flat
# (~0.002 range) and its edge (K_q 10-12, span 20, big rest) degraded
# held-out log loss, so the smallest config within noise of the tune optimum
# ships. Held-out 2024-2025, QB only: accuracy 0.6626 -> 0.6714, log loss
# 0.6215 -> 0.6206 (see module __main__ for the full increment table).
QB_ADJ_ENABLED = True
QB_K = 4.0  # Elo points per point of QB EWMA scoring vs league average
QB_SPAN = 12  # EWMA span in starts (alpha ~0.154)
QB_ADJ_CAP = 90.0  # Elo points on the game-level QB difference
QB_PRIOR_POINTS = 22.8  # league mean points per team-game, 2010-2025

# Rest differential, same tune/validate protocol. Tune log loss 0.6422 ->
# 0.6418 on top of QB; held-out it's neutral (accuracy +0.0035, log loss
# +0.0002 on top of QB) — kept for its clear mechanism, at the small
# coefficient. Divisional-game damping was also evaluated: its tune-window
# gain disappeared against a damp-ALL-games control (it was soaking up
# global Elo overconfidence, not divisional signal), so it's gated off.
REST_ADJ_ENABLED = True
REST_ELO_PER_DAY = 1.2  # Elo points per day of rest differential

# Preseason damping: p' = 0.5 + PRESEASON_DAMP * (p - 0.5).
PRESEASON_MONTHS = frozenset({7, 8})
PRESEASON_DAMP = 0.4

# Weather totals multipliers (win probability never sees weather).
WIND_MODERATE_KPH = 24.0
WIND_HEAVY_KPH = 40.0
WIND_MODERATE_MULT = 0.95
WIND_HEAVY_MULT = 0.90
PRECIP_PROB_THRESHOLD = 70.0  # percent
PRECIP_MULT = 0.97

ELO_CONFIG = EloConfig(k=20.0, home_advantage=48.0, mov_scaling=True, season_carry=0.67)

MODEL_NAME = "nfl-elo-v2"

# games.csv keys teams by abbreviation; the Elo book (and ESPN, and the rest of
# the terminal) uses full display names. Historical abbreviations map to the
# franchise's current name so a team keeps one Elo identity across relocations.
ABBREV_TO_NAME: dict[str, str] = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams",
    "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
    # Relocations/renames present in the 1999+ data — same franchise, new city.
    "OAK": "Las Vegas Raiders",
    "SD": "Los Angeles Chargers",
    "STL": "Los Angeles Rams",
    "LAR": "Los Angeles Rams",  # alternate Rams code used by some nflverse tools
}

CURRENT_ABBREVS = frozenset(
    "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LA LAC LV "
    "MIA MIN NE NO NYG NYJ PHI PIT SEA SF TB TEN WAS".split()
)


async def _download_games_csv(force: bool = False) -> Path:
    """Fetch games.csv into RAW_DIR/nfl, reusing a copy younger than 24h."""
    path = GAMES_CSV_PATH
    if (
        not force
        and path.exists()
        and time.time() - path.stat().st_mtime < CSV_MAX_AGE_SECONDS
    ):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(GAMES_CSV_URL)
        resp.raise_for_status()
    path.write_bytes(resp.content)
    return path


def _to_ml(value) -> int | None:
    """Moneyline cell -> American odds int, or None when absent/degenerate."""
    if value is None or pd.isna(value) or int(value) == 0:
        return None
    return int(value)


def _cell(value):
    """CSV cell -> value, with pandas NaN normalized to None."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


# -- starting-QB ratings ------------------------------------------------------


def _qb_value(qbs: dict[str, dict], qb_id) -> float:
    """Points-above-league-average value of one QB; 0 when unknown or unseen.

    A QB's EWMA starts at QB_PRIOR_POINTS, so a debutant also nets 0 — the
    value only grows as real starts pull the EWMA off the prior. That's the
    whole trick: a backup's ~0 against an established starter's earned value
    is what moves the number.
    """
    if qb_id is None:
        return 0.0
    entry = qbs.get(str(qb_id))
    if not entry:
        return 0.0
    return entry["ewma"] - QB_PRIOR_POINTS


def _qb_diff(qbs: dict[str, dict], home_qb, away_qb, k_qb: float = QB_K) -> float:
    """Elo-point pre-game adjustment for the QB matchup, clamped to ±cap."""
    adj = k_qb * (_qb_value(qbs, home_qb) - _qb_value(qbs, away_qb))
    return max(-QB_ADJ_CAP, min(QB_ADJ_CAP, adj))


def _record_qb_start(
    qbs: dict[str, dict],
    qb_id,
    name: str | None,
    points_scored: int,
    span: int = QB_SPAN,
) -> None:
    """Fold one start (team points scored) into the QB's EWMA.

    Initializing at QB_PRIOR_POINTS is the shrinkage: with alpha=2/(span+1),
    one big game moves a rookie only ~15% of the way off the prior.
    """
    if qb_id is None:
        return
    entry = qbs.setdefault(
        str(qb_id), {"name": name, "ewma": QB_PRIOR_POINTS, "starts": 0}
    )
    alpha = 2.0 / (span + 1.0)
    entry["ewma"] += alpha * (points_scored - entry["ewma"])
    entry["starts"] += 1
    if name:
        entry["name"] = name


# -- rest and probability -----------------------------------------------------


def _rest_diff(home_rest, away_rest, per_day: float = REST_ELO_PER_DAY) -> float:
    """Elo-point adjustment for the rest-day differential; 0 when unknown.

    One term covers everything: a bye (rest>=13) against a normal week is a
    +6/+7-day differential, a Thursday short week (rest<=4) a negative one.
    """
    if _cell(home_rest) is None or _cell(away_rest) is None:
        return 0.0
    return per_day * (float(home_rest) - float(away_rest))


def _prob_from_diff(diff: float) -> float:
    """Elo logistic: P(home wins) for a total pre-game rating difference."""
    return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))


def _expected_home_prob(
    book: EloBook, home: str, away: str, adjustment: float = 0.0, neutral: bool = False
) -> float:
    """P(home wins) with the pre-game adjustment folded in; no HFA on neutral."""
    hfa = 0.0 if neutral else book.config.home_advantage
    return _prob_from_diff(book.get(home) + hfa - book.get(away) + adjustment)


def _window_avg(window: deque | None, idx: int) -> float:
    """Rolling mean of points for (idx=0) / against (idx=1); league avg cold."""
    if not window:
        return LEAGUE_AVG_POINTS
    return sum(g[idx] for g in window) / len(window)


def _replay(
    df: pd.DataFrame,
    k_qb: float | None = None,
    qb_span: int = QB_SPAN,
    rest_per_day: float | None = None,
    validate_from: int = VALIDATE_FROM_SEASON,
    validate_to: int | None = None,
    log: list | None = None,
) -> tuple[EloBook, dict[str, deque], dict[str, dict], dict[str, str], list[dict], dict]:
    """Replay completed games chronologically through a fresh EloBook.

    Returns the fitted book, per-team rolling (points_for, points_against)
    windows, per-QB EWMA ratings, each team's most recent starting QB, one
    pre-game record per replayed game (season, the rating difference and its
    QB/rest parts, the probability read from it, the result and the closing
    moneylines — all computed from strictly pre-update state), and
    walk-forward metrics over the validation window. Team Elo updates never
    see the QB/rest overlay, so team ratings match a v1 replay exactly.
    Pure function of the frame so tests can drive it with synthetic data.
    """
    if k_qb is None:
        k_qb = QB_K if QB_ADJ_ENABLED else 0.0
    if rest_per_day is None:
        rest_per_day = REST_ELO_PER_DAY if REST_ADJ_ENABLED else 0.0
    book = EloBook(config=ELO_CONFIG)
    rolling: dict[str, deque] = {}
    qbs: dict[str, dict] = {}
    team_qb: dict[str, str] = {}
    records: list[dict] = []

    played = df[
        (df["season"] >= REPLAY_FROM_SEASON)
        & df["home_score"].notna()
        & df["away_score"].notna()
    ].sort_values(["season", "gameday"], kind="stable")

    current_season: int | None = None
    for row in played.itertuples(index=False):
        if current_season is not None and row.season != current_season:
            book.new_season()
        current_season = row.season

        home = ABBREV_TO_NAME.get(row.home_team)
        away = ABBREV_TO_NAME.get(row.away_team)
        if home is None or away is None:  # unknown abbrev — don't poison ratings
            continue

        home_score, away_score = int(row.home_score), int(row.away_score)
        home_won = home_score > away_score
        tie = home_score == away_score

        home_qb = _cell(getattr(row, "home_qb_id", None))
        away_qb = _cell(getattr(row, "away_qb_id", None))
        neutral = _cell(getattr(row, "location", None)) == "Neutral"
        qb_adj = _qb_diff(qbs, home_qb, away_qb, k_qb)
        rest_adj = _rest_diff(
            getattr(row, "home_rest", None), getattr(row, "away_rest", None), rest_per_day
        )

        hfa = 0.0 if neutral else book.config.home_advantage
        raw_gap = book.get(home) - book.get(away)
        record = {
            "season": int(row.season),
            # The full pre-game difference the probability is read from.
            "elo_diff": book.get(home) + hfa - book.get(away) + qb_adj + rest_adj,
            "elo_diff_raw": raw_gap,  # team ratings alone, no HFA or overlays
            "qb_adj_diff": qb_adj,
            "rest_diff": rest_adj,
            "home_won": home_won,
            "tie": tie,
            "home_ml": _to_ml(getattr(row, "home_moneyline", None)),
            "away_ml": _to_ml(getattr(row, "away_moneyline", None)),
        }
        record["p"] = _prob_from_diff(record["elo_diff"])
        records.append(record)
        if log is not None and not tie:
            # 5-field walk-forward row — pre-update prob + team names for the
            # dollar-ROI price join (store/roibacktest.py). Ties are skipped
            # (binary markets void them).
            when = _cell(getattr(row, "gameday", None))
            if when:
                log.append([str(when), round(record["p"], 6), int(home_won), home, away])

        margin = abs(home_score - away_score)
        book.update(home, away, home_won, margin=margin or None, draw=tie)
        for team, qb_id, qb_name, pf, pa in (
            (home, home_qb, _cell(getattr(row, "home_qb_name", None)), home_score, away_score),
            (away, away_qb, _cell(getattr(row, "away_qb_name", None)), away_score, home_score),
        ):
            _record_qb_start(qbs, qb_id, qb_name, pf, qb_span)
            if qb_id is not None:
                team_qb[team] = str(qb_id)
            rolling.setdefault(team, deque(maxlen=ROLLING_WINDOW)).append((pf, pa))

    validation = [
        r
        for r in records
        if r["season"] >= validate_from
        and (validate_to is None or r["season"] <= validate_to)
    ]
    return book, rolling, qbs, team_qb, records, _compute_metrics(validation)


def _upcoming_schedule(df: pd.DataFrame) -> dict[str, dict]:
    """Context for scheduled (unplayed) games, keyed "gameday|home|away".

    games.csv publishes the full upcoming regular season with rest days,
    neutral sites, and venue detail already populated, so predict() can
    apply the rest overlay and zero home advantage without extra fetches.
    Doubles as the preseason detector: a July/August ESPN game with no key
    here isn't on the league schedule — it's preseason.
    """
    upcoming = df[df["home_score"].isna() | df["away_score"].isna()]
    schedule: dict[str, dict] = {}
    for row in upcoming.itertuples(index=False):
        home = ABBREV_TO_NAME.get(row.home_team)
        away = ABBREV_TO_NAME.get(row.away_team)
        home_rest = _cell(getattr(row, "home_rest", None))
        away_rest = _cell(getattr(row, "away_rest", None))
        if None in (home, away, home_rest, away_rest):
            continue
        temp = _cell(getattr(row, "temp", None))
        wind = _cell(getattr(row, "wind", None))
        schedule[f"{row.gameday}|{home}|{away}"] = {
            "home_rest": int(home_rest),
            "away_rest": int(away_rest),
            "season": int(row.season),
            "week": int(_cell(getattr(row, "week", None)) or 0),
            "game_type": str(_cell(getattr(row, "game_type", None)) or "REG"),
            "div_game": int(_cell(getattr(row, "div_game", None)) or 0),
            "roof": _cell(getattr(row, "roof", None)),
            "surface": _cell(getattr(row, "surface", None)),
            "temp": float(temp) if temp is not None else None,
            "wind": float(wind) if wind is not None else None,
            "neutral": _cell(getattr(row, "location", None)) == "Neutral",
            # nflverse fills the expected starters for the next week or two
            # of games. None further out, and predict() then falls back to
            # the team's most recent starter.
            "home_qb_id": _cell(getattr(row, "home_qb_id", None)),
            "away_qb_id": _cell(getattr(row, "away_qb_id", None)),
        }
    return schedule


def _weather_total_multiplier(wx: weather.GameWeather | None) -> float:
    """Totals multiplier for forecast weather; 1.0 for domes/unknown (None).

    Coarse and capped: heavy wind (>40 kph) x0.90, moderate (>24 kph) x0.95,
    likely precipitation (>70%) a further x0.97 — floor 0.90*0.97 = 0.873.
    """
    if wx is None:
        return 1.0
    mult = 1.0
    if wx.wind_kph > WIND_HEAVY_KPH:
        mult *= WIND_HEAVY_MULT
    elif wx.wind_kph > WIND_MODERATE_KPH:
        mult *= WIND_MODERATE_MULT
    if wx.precip_prob_pct > PRECIP_PROB_THRESHOLD:
        mult *= PRECIP_MULT
    return mult


def _log_loss(pairs: list[tuple[float, bool]]) -> float:
    """Mean negative log-likelihood over (prob_of_home, home_won) pairs."""
    eps = 1e-12
    total = 0.0
    for p, won in pairs:
        p = min(max(p, eps), 1.0 - eps)
        total -= math.log(p if won else 1.0 - p)
    return total / len(pairs)


def _compute_metrics(records: list[dict]) -> dict:
    """Score Elo on the validation slice, then head-to-head vs the closing line.

    Accuracy and log loss exclude ties (no binary outcome to score). The
    market comparison is restricted to games carrying both closing moneylines
    and evaluates Elo on that exact subset so the two are directly comparable.
    """
    decided = [r for r in records if not r["tie"]]
    if not decided:
        return {}

    metrics = {
        "n_games": len(decided),
        "n_ties_excluded": len(records) - len(decided),
        "elo_accuracy": sum((r["p"] >= 0.5) == r["home_won"] for r in decided)
        / len(decided),
        "elo_log_loss": _log_loss([(r["p"], r["home_won"]) for r in decided]),
    }

    priced = [r for r in decided if r["home_ml"] is not None and r["away_ml"] is not None]
    if priced:
        market_pairs = [
            (remove_vig(r["home_ml"], r["away_ml"])[0], r["home_won"]) for r in priced
        ]
        metrics.update(
            {
                "n_games_with_moneyline": len(priced),
                "elo_accuracy_vs_market": sum(
                    (r["p"] >= 0.5) == r["home_won"] for r in priced
                )
                / len(priced),
                "elo_log_loss_vs_market": _log_loss(
                    [(r["p"], r["home_won"]) for r in priced]
                ),
                "market_accuracy": sum((q >= 0.5) == won for q, won in market_pairs)
                / len(market_pairs),
                "market_log_loss": _log_loss(market_pairs),
            }
        )
    return metrics


class NflEngine(SportEngine):
    sport = Sport.NFL
    model_name = MODEL_NAME

    def __init__(self, state_path: Path = STATE_PATH) -> None:
        self.state_path = state_path
        self.book = EloBook(config=ELO_CONFIG)
        self.rolling: dict[str, deque] = {}
        self.qbs: dict[str, dict] = {}
        self.team_qb: dict[str, str] = {}
        self.schedule: dict[str, dict] = {}
        self.metrics: dict = {}
        self._load_state()

    async def upcoming_games(self, on: date | None = None) -> list[Game]:
        return await espn.scoreboard(Sport.NFL, on)

    async def predict(self, games: list[Game]) -> list[Prediction]:
        now = datetime.now(timezone.utc)
        predictions: list[Prediction] = []
        # Every game's kickoff forecast at once. Awaited one per game inside
        # the loop below, a 14-game slate spent ~2.6s here on a fast line —
        # nearly half of what the renderer allows a whole board.
        forecasts = await asyncio.gather(*(self._weather_for(g) for g in games))
        for game, forecast in zip(games, forecasts):
            entry = self._schedule_entry(game)
            neutral = game.neutral_site or bool(entry and entry.get("neutral"))
            qb_adj, rest_adj = self._qb_rest_adjustments(game, entry)
            p_elo = _expected_home_prob(
                self.book, game.home, game.away, qb_adj + rest_adj, neutral=neutral
            )
            if self._is_preseason(game):
                # Ratings say little about a game where starters play a series;
                # shrink 60% toward a coin flip, exactly as v2. Confidence is
                # derived from the damped probability, so it drops equally.
                p_home = 0.5 + PRESEASON_DAMP * (p_elo - 0.5)
            else:
                # Calibration audit 2026-09-02 (2018-26 log, n=2,230): slope
                # 0.86, favourites +2.8 pts; logit scale 0.84 on the served
                # number. Preseason is already damped 60% toward a coin flip.
                p_home = temper(p_elo, CALIBRATION_TEMPERATURE)
            home_score = (
                self._avg(game.home, idx=0) + self._avg(game.away, idx=1)
            ) / 2.0 + HOME_POINTS_EDGE
            away_score = (
                self._avg(game.away, idx=0) + self._avg(game.home, idx=1)
            ) / 2.0 - HOME_POINTS_EDGE
            mult = _weather_total_multiplier(forecast)
            home_score *= mult
            away_score *= mult
            # A team with no scoring history would price at the league-average
            # placeholder — a constant, not a forecast. The board printed UNDER
            # edges against exactly that (WNBA, 2026-09-02). Emit no total.
            scored = bool(self.rolling.get(game.home)) and bool(self.rolling.get(game.away))
            predictions.append(
                Prediction(
                    game_id=game.id,
                    sport=Sport.NFL,
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
        path = await _download_games_csv()
        df = pd.read_csv(path, low_memory=False)
        wf_log: list = []
        self.book, self.rolling, self.qbs, self.team_qb, _, self.metrics = _replay(df, log=wf_log)
        self.backtest_log = wf_log
        self.schedule = _upcoming_schedule(df)
        self._save_state()

    # -- pre-game adjustments ------------------------------------------------

    def _qb_rest_adjustments(self, game: Game, entry: dict | None) -> tuple[float, float]:
        """(QB, rest) Elo-point shifts for one game; each piece degrades to 0.

        The starter is the one games.csv lists for this game when it lists
        one (2026-11-14: 8 of 52 listed team-sides named someone other than
        the team's previous starter, 4 of that week's 11 games), else the
        team's most recent start in the replay.
        """
        qb_adj = 0.0
        rest_adj = 0.0
        if QB_ADJ_ENABLED:
            listed = entry or {}
            qb_adj = _qb_diff(
                self.qbs,
                listed.get("home_qb_id") or self.team_qb.get(game.home),
                listed.get("away_qb_id") or self.team_qb.get(game.away),
            )
        if REST_ADJ_ENABLED and entry:
            rest_adj = _rest_diff(entry["home_rest"], entry["away_rest"])
        return qb_adj, rest_adj

    def _schedule_entry(self, game: Game) -> dict | None:
        """games.csv schedule row for an ESPN game, or None.

        gameday is US-local while ESPN start times are UTC, so a night game's
        UTC date can be one day ahead — try both.
        """
        d = game.start_time.date()
        for when in (d.isoformat(), (d - timedelta(days=1)).isoformat()):
            entry = self.schedule.get(f"{when}|{game.home}|{game.away}")
            if entry:
                return entry
        return None

    def _is_preseason(self, game: Game) -> bool:
        """July/August game absent from the games.csv schedule -> preseason.

        games.csv carries no preseason rows (game_type is only REG/WC/DIV/
        CON/SB) but does list the entire upcoming regular season, so schedule
        absence in those months is decisive. The month check runs on the UTC
        date and the US-local day before it, covering late-August night games
        that spill into September 1 UTC.
        """
        d = game.start_time.date()
        in_window = (
            d.month in PRESEASON_MONTHS
            or (d - timedelta(days=1)).month in PRESEASON_MONTHS
        )
        return in_window and self._schedule_entry(game) is None

    async def _weather_for(self, game: Game) -> weather.GameWeather | None:
        """Kickoff forecast for outdoor venues; None (no-op) on any failure."""
        try:
            return await weather.game_weather(Sport.NFL, game.home, game.start_time)
        except Exception:
            return None

    # -- rolling points helpers -------------------------------------------

    def _avg(self, team: str, idx: int) -> float:
        """Rolling mean of points for (idx=0) / against (idx=1) for a team."""
        return _window_avg(self.rolling.get(team), idx)

    # -- state persistence -------------------------------------------------

    def _save_state(self) -> None:
        state = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": self.model_name,
            "ratings": self.book.ratings,
            "rolling": {team: list(w) for team, w in self.rolling.items()},
            "qbs": self.qbs,
            "team_qb": self.team_qb,
            "schedule": self.schedule,
            "metrics": self.metrics,
            "backtest": getattr(self, "backtest_log", []),
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
        self.qbs = dict(state.get("qbs", {}))
        self.team_qb = dict(state.get("team_qb", {}))
        self.schedule = dict(state.get("schedule", {}))
        self.metrics = state.get("metrics", {})
        self.backtest_log = list(state.get("backtest") or [])


def build_engine() -> NflEngine:
    return NflEngine()


def _print_row(label: str, acc: float, ll: float) -> None:
    print(f"  {label:26s} acc {acc:.4f}   log loss {ll:.4f}")


def _print_metrics(label: str, m: dict) -> None:
    _print_row(label, m["elo_accuracy_vs_market"], m["elo_log_loss_vs_market"])


async def _main() -> None:
    path = await _download_games_csv()
    df = pd.read_csv(path, low_memory=False)

    print(f"\nnfl — replay {REPLAY_FROM_SEASON}+, held-out walk-forward "
          f"validation {VALIDATE_FROM_SEASON}+ (all tuning on earlier seasons)")
    variants = [
        ("v1 baseline (team Elo)", 0.0, 0.0),
        ("+QB only", QB_K, 0.0),
        ("+rest only", 0.0, REST_ELO_PER_DAY),
        ("full v2 (QB + rest)", QB_K, REST_ELO_PER_DAY),
    ]
    last: dict = {}
    for label, k_qb, rest in variants:
        *_, last = _replay(df, k_qb=k_qb, rest_per_day=rest)
        _print_metrics(label, last)

    print(f"  {'closing line (devigged)':26s} acc {last['market_accuracy']:.4f}   "
          f"log loss {last['market_log_loss']:.4f}")
    print(f"  ({last['n_games_with_moneyline']} priced games of {last['n_games']} "
          f"decided, {last['n_ties_excluded']} ties excluded)")

    engine = build_engine()
    await engine.refresh_data()

    top = sorted(engine.book.ratings.items(), key=lambda kv: kv[1], reverse=True)[:5]
    print("\n  current top 5 Elo:")
    for name, rating in top:
        print(f"    {rating:7.1f}  {name}")

    started = [q for q in engine.qbs.values() if q["starts"] >= 8]
    top_qbs = sorted(started, key=lambda q: q["ewma"], reverse=True)[:5]
    print("\n  current top 5 QB EWMA (points/start, 8+ starts):")
    for qb in top_qbs:
        print(f"    {qb['ewma']:5.1f}  {qb['name']} ({qb['starts']} starts)")


if __name__ == "__main__":
    asyncio.run(_main())

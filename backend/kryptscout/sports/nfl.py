"""NFL engine — XGBoost over 538-style Elo, with QB/rest/preseason/weather overlays.

The v2 machinery is unchanged underneath: team Elo (K=20, 48 points of home
advantage, margin-of-victory K scaling, 1/3 regression to the mean between
seasons — the public fivethirtyeight/nfl-elo-game recipe) plus pre-game
overlays that shift win probability only; team Elo updates never see them.

  * Starting QB: per-QB EWMA of team points scored in his starts (span ~12,
    initialized at the league average so a debutant is fully shrunk to the
    mean). Game adjustment clamp(K_q * (home_qb_value - away_qb_value), ±90).
  * Rest differential: REST_ELO_PER_DAY * (home_rest - away_rest) Elo points.
  * Neutral sites (Super Bowl, international): the 48-point home-advantage
    term is zeroed — games.csv's `location` column drives it in the replay,
    Game.neutral_site (parsed from ESPN) at predict time.
  * Preseason damping: a July/August ESPN game absent from the games.csv
    schedule is preseason; p' = 0.5 + 0.4*(p-0.5).
  * Weather totals: outdoor games inside Open-Meteo's horizon get a coarse
    totals multiplier (wind/precip, floor 0.873). Win prob never sees it.

v3 layers a small, regularized XGBoost binary classifier on top. During the
chronological replay every played game emits a leakage-safe feature row
computed from strictly pre-game state (exactly where v2 recorded its
pre-update validation probabilities): the full v2 Elo diff (monotone +1
constraint), the raw Elo gap, the QB and rest adjustments, both teams'
rolling points for/against, week, playoff/divisional flags, roof/surface
dummies, temp/wind (NaN -> sentinel), and season. No market columns — the
model stays market-independent so the closing-line comparison means
something. Trained on 2006-2023 (fit 2006-2021, early stopping + Platt
calibration on the 2022-2023 chronological tail); 2024-2025 is never touched
during fit or tuning. On that held-out window the 50/50 Elo+XGB blend beats
both parents on log loss and ties XGB for the best accuracy, so V3_MODE
ships as "blend" (the module __main__ prints the full table and the tuning
search; the constants by V3_MODE record the numbers). The trained model +
calibrator are pickled to
MODELS_DIR (gitignored; retrained deterministically); refresh_data()
retrains when the pickle is missing or older than 7 days — seconds on ~4.9k
rows. Games without a games.csv schedule row (preseason) fall back to the
damped Elo path exactly as v2 does.

One data file drives everything: Lee Sharpe's games.csv from nflverse
(MIT/CC-BY) — final scores from 1999, closing moneylines from 2006, starting
QBs and rest days backfilled to 1999. Replay runs from 2006, the start of
the closing-moneyline era and full feature coverage. Overlay knobs were
coarse-tuned on 2020-2023 only; 2024-2025 is the held-out walk-forward
validation set, scored on its own and head-to-head against the devigged
closing line (`python -m kryptscout.sports.nfl` prints the increment table).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import pickle
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from xgboost import XGBClassifier

from kryptscout.config import DATA_DIR, MODELS_DIR, RAW_DIR
from kryptscout.core import modelio
from kryptscout.core.elo import EloBook, EloConfig
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

# -- v3 XGBoost layer ---------------------------------------------------------
#
# Decision (2024-25 held-out, 569 priced games): elo-v2 acc 0.6714 / log loss
# 0.6226, xgb alone 0.6749 / 0.6254, 50/50 blend 0.6749 / 0.6220 — the blend
# wins on both metrics, so V3_MODE ships "blend"; xgb alone loses on log loss
# and is gated behind the blend. The margin is modest (the features overlap
# Elo heavily). v2's row moved 0.6208 -> 0.6226 vs the previously published
# table solely because neutral sites now zero home advantage (15 held-out
# neutral games; the replay-start change 2010 -> 2006 alone moves nothing at
# 4 decimals). In a no-neutral-handling ablation the ordering holds: v2
# 0.6208, xgb 0.6271, blend 0.6219 on log loss; blend acc 0.6784 vs 0.6749.
V3_MODE = "blend"  # "off" -> pure elo-v2 | "xgb" -> model alone | "blend" -> 50/50
BLEND_ELO_WEIGHT = 0.5

TRAIN_TO_SEASON = 2023  # feature rows through here may touch the fit
EARLY_STOP_FROM_SEASON = 2022  # chronological TRAIN tail: early stop + calibration
MODEL_PATH = MODELS_DIR / "nfl_xgb_v3.pkl"
MODEL_MAX_AGE_SECONDS = 7 * 24 * 3600

TEMP_SENTINEL = -100.0  # games.csv temp is deg F (observed min -6)
WIND_SENTINEL = -1.0  # mph (observed min 0)
ROOF_TYPES = ("outdoors", "dome", "closed", "open")

FEATURE_COLUMNS = (
    "elo_diff",  # full v2 pre-game diff: Elo + HFA (0 on neutral) + QB + rest
    "elo_diff_raw",  # team Elo gap alone, no HFA/overlays
    "qb_adj_diff",  # Elo points from the QB matchup
    "rest_diff",  # Elo points from the rest differential
    "home_pf", "home_pa", "away_pf", "away_pa",  # rolling 10-game points
    "week", "is_playoff", "div_game",
    "roof_outdoors", "roof_dome", "roof_closed", "roof_open",
    "surface_grass",
    "temp", "wind",
    "season",
)
_MONOTONE = "(" + ",".join("1" if c == "elo_diff" else "0" for c in FEATURE_COLUMNS) + ")"

# Small and regularized; the monotone constraint on elo_diff keeps the tree
# ensemble sane where Elo is the dominant signal.
XGB_FIXED_PARAMS = dict(
    objective="binary:logistic",
    n_estimators=500,
    subsample=0.9,
    colsample_bytree=0.8,
    reg_lambda=2.0,
    tree_method="hist",
    eval_metric="logloss",
    early_stopping_rounds=40,
    monotone_constraints=_MONOTONE,
    random_state=0,
)
# Coarse grid, scored on the chronological train tail (2022-23) only.
XGB_GRID = tuple(
    {"max_depth": depth, "learning_rate": lr, "min_child_weight": mcw}
    for depth in (3, 4)
    for lr in (0.03, 0.07)
    for mcw in (5, 25)
)

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


# -- v3 feature rows ----------------------------------------------------------


def _feature_row(
    *,
    elo_home: float,
    elo_away: float,
    hfa: float,
    qb_adj: float,
    rest_adj: float,
    home_pf: float,
    home_pa: float,
    away_pf: float,
    away_pa: float,
    week,
    is_playoff,
    div_game,
    roof,
    surface,
    temp,
    wind,
    season,
) -> dict:
    """One model-ready feature dict, shared by the replay and predict().

    Missing values degrade the same way in both places: temp/wind to
    sentinels, roof to all-zero dummies, surface to the non-grass dummy.
    """
    roof = str(roof).strip().lower() if _cell(roof) is not None else ""
    surface = str(surface).strip().lower() if _cell(surface) is not None else ""
    temp = _cell(temp)
    wind = _cell(wind)
    row = {
        "elo_diff": elo_home + hfa - elo_away + qb_adj + rest_adj,
        "elo_diff_raw": elo_home - elo_away,
        "qb_adj_diff": qb_adj,
        "rest_diff": rest_adj,
        "home_pf": home_pf,
        "home_pa": home_pa,
        "away_pf": away_pf,
        "away_pa": away_pa,
        "week": float(week or 0),
        "is_playoff": 1.0 if is_playoff else 0.0,
        "div_game": float(div_game or 0),
        "surface_grass": 1.0 if surface.startswith("grass") or surface == "dessograss" else 0.0,
        "temp": float(temp) if temp is not None else TEMP_SENTINEL,
        "wind": float(wind) if wind is not None else WIND_SENTINEL,
        "season": float(season),
    }
    for kind in ROOF_TYPES:
        row[f"roof_{kind}"] = 1.0 if roof == kind else 0.0
    return row


def _matrix(rows: list[dict], columns=FEATURE_COLUMNS) -> pd.DataFrame:
    """Feature dicts -> model matrix (DataFrame keeps feature names for xgb)."""
    return pd.DataFrame(
        [[float(r[c]) for c in columns] for r in rows], columns=list(columns)
    )


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
    leakage-safe feature row per replayed game (everything computed from
    strictly pre-update state — this is the v3 training set), and
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
    feats: list[dict] = []

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

        feat = _feature_row(
            elo_home=book.get(home),
            elo_away=book.get(away),
            hfa=0.0 if neutral else book.config.home_advantage,
            qb_adj=qb_adj,
            rest_adj=rest_adj,
            home_pf=_window_avg(rolling.get(home), 0),
            home_pa=_window_avg(rolling.get(home), 1),
            away_pf=_window_avg(rolling.get(away), 0),
            away_pa=_window_avg(rolling.get(away), 1),
            week=_cell(getattr(row, "week", None)),
            is_playoff=_cell(getattr(row, "game_type", None)) not in (None, "REG"),
            div_game=_cell(getattr(row, "div_game", None)),
            roof=getattr(row, "roof", None),
            surface=getattr(row, "surface", None),
            temp=getattr(row, "temp", None),
            wind=getattr(row, "wind", None),
            season=int(row.season),
        )
        feat.update(
            p=_prob_from_diff(feat["elo_diff"]),  # the v2 pre-game probability
            home_won=home_won,
            tie=tie,
            home_ml=_to_ml(getattr(row, "home_moneyline", None)),
            away_ml=_to_ml(getattr(row, "away_moneyline", None)),
        )
        feats.append(feat)
        if log is not None and not tie:
            # 5-field walk-forward row — pre-update prob + team names for the
            # dollar-ROI price join (store/roibacktest.py). Ties are skipped
            # (binary markets void them).
            when = _cell(getattr(row, "gameday", None))
            if when:
                log.append([str(when), round(feat["p"], 6), int(home_won), home, away])

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

    records = [
        f
        for f in feats
        if f["season"] >= validate_from
        and (validate_to is None or f["season"] <= validate_to)
    ]
    return book, rolling, qbs, team_qb, feats, _compute_metrics(records)


# -- v3 training / calibration / persistence -----------------------------------


def _train_model(
    feats: list[dict],
    train_to: int = TRAIN_TO_SEASON,
    tail_from: int = EARLY_STOP_FROM_SEASON,
    grid: tuple[dict, ...] = XGB_GRID,
) -> dict:
    """Fit the v3 XGBoost + Platt-calibrator bundle, walk-forward honest.

    Trees fit on seasons < tail_from; the chronological tail (tail_from
    through train_to) is used only for early stopping, the coarse
    hyperparameter pick (lowest tail log loss), and calibration. Ties carry
    no binary outcome and are dropped. Nothing at or past VALIDATE_FROM_SEASON
    may reach this function's inputs during evaluation.
    """
    train = [f for f in feats if f["season"] <= train_to and not f["tie"]]
    fit = [f for f in train if f["season"] < tail_from]
    tail = [f for f in train if f["season"] >= tail_from]
    if not fit or not tail:
        raise ValueError("need non-empty fit and tail slices to train nfl-xgb-v3")

    x_fit, y_fit = _matrix(fit), np.array([f["home_won"] for f in fit], dtype=int)
    x_tail, y_tail = _matrix(tail), np.array([f["home_won"] for f in tail], dtype=int)

    search: list[dict] = []
    best_model = None
    best_ll = math.inf
    for params in grid:
        model = XGBClassifier(**XGB_FIXED_PARAMS, **params)
        model.fit(x_fit, y_fit, eval_set=[(x_tail, y_tail)], verbose=False)
        entry = {
            **params,
            "best_iteration": int(model.best_iteration),
            "tail_log_loss": float(model.best_score),
        }
        search.append(entry)
        # lowest tail log loss wins; ties keep the earlier (smaller) config
        if entry["tail_log_loss"] < best_ll:
            best_ll = entry["tail_log_loss"]
            best_model = model
            best = entry

    # Platt calibration on the tail's raw margins: strictly increasing, so it
    # preserves the monotone-in-elo_diff property of the constrained trees.
    # Only the two fitted COEFFICIENTS are persisted, never the estimator
    # object — see _apply_platt (and sports/nba.py, which has always done it
    # this way). A pickled sklearn estimator is only guaranteed to behave
    # correctly under the exact version that wrote it, so shipping one makes
    # the model artifact silently version-fragile; two floats are forever.
    raw = np.clip(best_model.predict_proba(x_tail)[:, 1], 1e-6, 1.0 - 1e-6)
    margins = np.log(raw / (1.0 - raw)).reshape(-1, 1)
    calibrator = LogisticRegression(C=1e6, max_iter=1000).fit(margins, y_tail)

    return {
        "model": best_model,
        "platt": (float(calibrator.coef_[0][0]), float(calibrator.intercept_[0])),
        "feature_columns": list(FEATURE_COLUMNS),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_fit": len(fit),
        "n_tail": len(tail),
        "params": best,
        "search": search,
    }


def _apply_platt(margins: np.ndarray, platt: tuple[float, float]) -> np.ndarray:
    """Platt scaling from stored coefficients — exactly what a fitted
    LogisticRegression's predict_proba computes for a single feature:
    sigmoid(a·margin + b). Mirrors nba._apply_platt.

    float64 is deliberate. The margins arrive as float32 (xgboost's output
    dtype) and sklearn upcast them internally, so accumulating in float32
    here would shift every probability by ~5e-8 against the estimator this
    replaced. Below anything that matters, but free to get exactly right.
    """
    a, b = platt
    z = np.asarray(margins, dtype=np.float64).ravel()
    return 1.0 / (1.0 + np.exp(-(a * z + b)))


def _calibrated_probs(bundle: dict, rows: list[dict]) -> np.ndarray:
    """Calibrated P(home wins) for feature rows through the pickled bundle."""
    x = _matrix(rows, bundle["feature_columns"])
    raw = np.clip(bundle["model"].predict_proba(x)[:, 1], 1e-6, 1.0 - 1e-6)
    margins = np.log(raw / (1.0 - raw))
    return _apply_platt(margins, bundle["platt"])


def _save_model(bundle: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(modelio.stamp(bundle)))


def _load_model(path: Path) -> dict | None:
    """Unpickle the bundle; None on missing/corrupt/foreign files (Elo fallback).

    A pre-`platt` bundle (which pickled the sklearn calibrator object) is
    rejected here, so it is retrained into the coefficient form on the next
    refresh rather than being unpickled across sklearn versions.
    """
    if not path.exists():
        return None
    try:
        bundle = pickle.loads(path.read_bytes())
    except Exception:
        return None
    if not isinstance(bundle, dict) or not {"model", "platt", "feature_columns"} <= set(bundle):
        return None
    if not modelio.matches(bundle):
        # Written by a different sklearn/xgboost minor — its predictions are
        # not guaranteed to reproduce, so fall back to Elo and let
        # _model_stale trigger a retrain (deterministic, ~seconds).
        logger.warning(
            "nfl: model pickle was written by a different library stack "
            "(%s vs %s) — falling back to Elo until it is retrained",
            bundle.get(modelio.STAMP_KEY), modelio.library_versions(),
        )
        return None
    return bundle


def _model_stale(path: Path) -> bool:
    """True when the pickle is missing, too old, OR unloadable.

    The unloadable case matters: a version-mismatched pickle can be seconds
    old, so an mtime-only check would leave the engine on Elo forever without
    ever retraining the artifact that would fix it.
    """
    if not path.exists() or time.time() - path.stat().st_mtime > MODEL_MAX_AGE_SECONDS:
        return True
    return _load_model(path) is None


def _upcoming_schedule(df: pd.DataFrame) -> dict[str, dict]:
    """Feature context for scheduled (unplayed) games, keyed "gameday|home|away".

    games.csv publishes the full upcoming regular season with rest, roof,
    surface, and (closer to kickoff) temp/wind already populated, so
    predict() can build the exact training features without extra fetches.
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
    model_name = "nfl-xgb-v3" if V3_MODE != "off" else "nfl-elo-v2"

    def __init__(self, state_path: Path = STATE_PATH, model_path: Path = MODEL_PATH) -> None:
        self.state_path = state_path
        self.model_path = model_path
        self.book = EloBook(config=ELO_CONFIG)
        self.rolling: dict[str, deque] = {}
        self.qbs: dict[str, dict] = {}
        self.team_qb: dict[str, str] = {}
        self.schedule: dict[str, dict] = {}
        self.metrics: dict = {}
        self.model_bundle: dict | None = (
            _load_model(model_path) if V3_MODE != "off" else None
        )
        self._load_state()

    async def upcoming_games(self, on: date | None = None) -> list[Game]:
        return await espn.scoreboard(Sport.NFL, on)

    async def predict(self, games: list[Game]) -> list[Prediction]:
        now = datetime.now(timezone.utc)
        predictions: list[Prediction] = []
        for game in games:
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
            elif V3_MODE != "off" and self.model_bundle is not None and entry is not None:
                row = self._feature_row_for(game, entry, neutral, qb_adj, rest_adj)
                p_xgb = float(_calibrated_probs(self.model_bundle, [row])[0])
                p_home = (
                    BLEND_ELO_WEIGHT * p_elo + (1.0 - BLEND_ELO_WEIGHT) * p_xgb
                    if V3_MODE == "blend"
                    else p_xgb
                )
            else:
                # No pickle or no schedule row (preseason handled above):
                # degrade to the pure Elo path.
                p_home = p_elo
            home_score = (
                self._avg(game.home, idx=0) + self._avg(game.away, idx=1)
            ) / 2.0 + HOME_POINTS_EDGE
            away_score = (
                self._avg(game.away, idx=0) + self._avg(game.home, idx=1)
            ) / 2.0 - HOME_POINTS_EDGE
            mult = _weather_total_multiplier(await self._weather_for(game))
            home_score *= mult
            away_score *= mult
            predictions.append(
                Prediction(
                    game_id=game.id,
                    sport=Sport.NFL,
                    model=self.model_name,
                    generated_at=now,
                    home_win_prob=p_home,
                    away_win_prob=1.0 - p_home,
                    predicted_home_score=round(home_score, 1),
                    predicted_away_score=round(away_score, 1),
                    predicted_total=round(home_score + away_score, 1),
                    confidence=abs(p_home - 0.5) * 2.0,
                )
            )
        return predictions

    async def refresh_data(self) -> None:
        path = await _download_games_csv()
        df = pd.read_csv(path, low_memory=False)
        wf_log: list = []
        self.book, self.rolling, self.qbs, self.team_qb, feats, self.metrics = _replay(df, log=wf_log)
        self.backtest_log = wf_log
        self.schedule = _upcoming_schedule(df)
        if V3_MODE != "off" and _model_stale(self.model_path):
            # Deterministic and cheap (~seconds on ~4.9k rows); the pickle is
            # only a cache, never a source of truth.
            self.model_bundle = _train_model(feats)
            _save_model(self.model_bundle, self.model_path)
        self._save_state()

    # -- pre-game adjustments ------------------------------------------------

    def _qb_rest_adjustments(self, game: Game, entry: dict | None) -> tuple[float, float]:
        """(QB, rest) Elo-point shifts for one game; each piece degrades to 0.

        QBs come from each team's most recent start in the replay — a freshly
        announced backup won't be priced until he has actually started once.
        """
        qb_adj = 0.0
        rest_adj = 0.0
        if QB_ADJ_ENABLED:
            qb_adj = _qb_diff(
                self.qbs, self.team_qb.get(game.home), self.team_qb.get(game.away)
            )
        if REST_ADJ_ENABLED and entry:
            rest_adj = _rest_diff(entry["home_rest"], entry["away_rest"])
        return qb_adj, rest_adj

    def _feature_row_for(
        self, game: Game, entry: dict, neutral: bool, qb_adj: float, rest_adj: float
    ) -> dict:
        """The same features the replay would emit for this game, pre-game."""
        return _feature_row(
            elo_home=self.book.get(game.home),
            elo_away=self.book.get(game.away),
            hfa=0.0 if neutral else self.book.config.home_advantage,
            qb_adj=qb_adj,
            rest_adj=rest_adj,
            home_pf=self._avg(game.home, 0),
            home_pa=self._avg(game.home, 1),
            away_pf=self._avg(game.away, 0),
            away_pa=self._avg(game.away, 1),
            week=entry.get("week"),
            is_playoff=(entry.get("game_type") or "REG") != "REG",
            div_game=entry.get("div_game"),
            roof=entry.get("roof"),
            surface=entry.get("surface"),
            temp=entry.get("temp"),
            wind=entry.get("wind"),
            season=entry.get("season") or game.start_time.year,
        )

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
    feats: list[dict] = []
    last: dict = {}
    for label, k_qb, rest in variants:
        *_, feats, last = _replay(df, k_qb=k_qb, rest_per_day=rest)
        _print_metrics(label, last)

    # v3: features come from the full-v2 replay above (the last variant).
    bundle = _train_model(feats)
    val = [
        f
        for f in feats
        if f["season"] >= VALIDATE_FROM_SEASON
        and not f["tie"]
        and f["home_ml"] is not None
        and f["away_ml"] is not None
    ]
    won = [f["home_won"] for f in val]
    p_xgb = list(_calibrated_probs(bundle, val))
    p_blend = [
        BLEND_ELO_WEIGHT * f["p"] + (1.0 - BLEND_ELO_WEIGHT) * q
        for f, q in zip(val, p_xgb)
    ]
    for label, probs in (("xgb v3 (calibrated)", p_xgb), ("50/50 blend elo+xgb", p_blend)):
        pairs = list(zip(probs, won))
        _print_row(label, sum((p >= 0.5) == w for p, w in pairs) / len(pairs),
                   _log_loss(pairs))
    print(f"  {'closing line (devigged)':26s} acc {last['market_accuracy']:.4f}   "
          f"log loss {last['market_log_loss']:.4f}")
    print(f"  ({last['n_games_with_moneyline']} priced games of {last['n_games']} "
          f"decided, {last['n_ties_excluded']} ties excluded; V3_MODE={V3_MODE!r})")

    print(f"\n  xgb tune (tail={bundle['n_tail']} rows, seasons "
          f"{EARLY_STOP_FROM_SEASON}-{TRAIN_TO_SEASON}; fit={bundle['n_fit']} rows):")
    for s in sorted(bundle["search"], key=lambda s: s["tail_log_loss"]):
        print(f"    depth {s['max_depth']}  lr {s['learning_rate']:.2f}  "
              f"mcw {s['min_child_weight']:2d}  trees {s['best_iteration'] + 1:3d}  "
              f"tail ll {s['tail_log_loss']:.4f}")

    gains = bundle["model"].get_booster().get_score(importance_type="gain")
    print("\n  top feature importances (gain):")
    for name, gain in sorted(gains.items(), key=lambda kv: kv[1], reverse=True)[:8]:
        print(f"    {gain:8.1f}  {name}")

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

"""NBA engine — Elo core with a gradient-boosted overlay (v2).

The v1 Elo baseline is unchanged: standard Elo (K=20, margin-of-victory
scaling, +100 rating points of home advantage) replayed over every completed
NBA game since October 2024, plus rolling last-15 scoring for totals. Raw
daily scoreboards are cached under RAW_DIR/nba/ and the replayed state
persists to DATA_DIR/nba_state.json so refresh_data() stays incremental.

v2 adds a small walk-forward-honest XGBoost classifier over strictly
pre-game features logged during the replay (all rows home-perspective):
elo_diff, each side's rolling last-15 PF/PA, the rolling net-rating diff,
rest days and back-to-back flags (derived from each team's previous game
date), and games-played-this-season per side (early-season noise indicator).

Training is chronological: fit on 2024-25 plus the first half of 2025-26
(the tail of the fit set is held out for early stopping and Platt
calibration), validate on the untouched second half of 2025-26. Elo, the
calibrated XGB, and a 50-50 blend are compared on validation log loss and
the winner ships; a logistic-regression fallback (elo_diff + rest features)
is reported alongside. The bundle pickles to MODELS_DIR and refresh_data()
retrains it when missing or older than 7 days. When the pickle is absent or
the model gated off (Elo won), predict() uses the pure Elo path and
model_name stays "nba-elo-v1"; otherwise it reports "nba-xgb-v2".
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import pickle
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

import httpx
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from kryptscout.config import DATA_DIR, MODELS_DIR, RAW_DIR
from kryptscout.core import modelio
from kryptscout.core.elo import EloBook, EloConfig
from kryptscout.core.schemas import Game, Prediction, Sport
from kryptscout.datasources import espn

from .base import SportEngine

logger = logging.getLogger("kryptscout")

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
)
HISTORY_START = date(2024, 10, 1)  # 2024-25 season (preseason filtered out)
# No history END: the replay always runs through yesterday, like WNBA. A
# hard cap at the 2025-26 Finals (2026-06-30) froze the engine there — no
# refresh ever rewrote the state, the Engine Room showed it stale all
# summer, and the 2026-27 season would never have been ingested.
SEASON_BOUNDARY_MONTH = 8  # Aug 1 splits NBA seasons (off-season midpoint)
CURRENT_SEASON_START = date(2025, 8, 1)  # 2025-26 portion, for backtest metrics
ROLLING_WINDOW = 15  # games in the rolling points-for/against averages
LEAGUE_AVG_POINTS = 113.0  # fallback before a team has any scoring history
CHECKPOINT_EVERY_DAYS = 30  # persist state periodically during long backfills
# v3 added team names to backtest rows (dollar-ROI join). v4 (2026-09-02):
# the Elo retune (K 20→12, home 100→60, carry 0.75→0.5) changes how every
# rating is accumulated, so a state replayed under the old constants must be
# discarded and rebuilt from the cached scoreboards.
STATE_VERSION = 4

# Retuned 2026-09-02 on the engine's own archive (walk-forward, held out
# from 2026-01): K 20 → 12, home advantage 100 → 60, season carry 0.75 → 0.5
# took log loss from 0.5961 to 0.5824, −0.0137 [−0.0235, −0.0035]. The old
# 100-point home edge and fast K were the source of the calibration audit's
# finding that NBA favourites were overstated by 8 points. Replayed after
# the retune: slope 1.07 over the whole log and 1.17 on 2026-01+ (favourites
# now ~2 pts UNDER-stated), so no logit scale is applied here — the retune
# alone fixed the calibration. Two seasons of data behind it; retune again
# when a third exists.
ELO_CONFIG = EloConfig(
    k=12.0,
    home_advantage=60.0,
    mov_scaling=True,
    season_carry=0.5,
)

MODEL_NAME_V1 = "nba-elo-v1"
MODEL_NAME_V2 = "nba-xgb-v2"

# Pre-game feature columns, home-perspective (home flag implicit).
FEATURE_NAMES = [
    "elo_diff",  # raw rating gap, no HFA — the model learns the home edge
    "home_pf",  # rolling last-15 points for/against, both sides
    "home_pa",
    "away_pf",
    "away_pa",
    "net_rating_diff",  # (home PF-PA) minus (away PF-PA), rolling
    "home_rest",  # days since previous game, capped at REST_CAP
    "away_rest",
    "home_b2b",  # back-to-back flag (rest == 1)
    "away_b2b",
    "home_gp",  # games played this season (early-season noise indicator)
    "away_gp",
]
LOGREG_COLS = [0, 6, 7, 8, 9]  # elo_diff + rest/back-to-back fallback features
REST_CAP = 7  # rest saturates here; also the first-game-of-season sentinel
MODEL_MAX_AGE_DAYS = 7  # refresh_data() retrains a pickle older than this
PROB_FLOOR = 0.02  # calibrated probabilities clipped away from 0/1
TRAIN_TAIL_FRACTION = 0.15  # of fit set: early stopping + Platt calibration
MIN_TRAINING_GAMES = 300  # total replayed games required before training
MIN_CURRENT_GAMES = 100  # current-season games required for a real val split


def _season_key(day: date) -> int:
    """Season identified by its starting year: 2024-25 -> 2024."""
    return day.year if day.month >= SEASON_BOUNDARY_MONTH else day.year - 1


def _clip(p: np.ndarray) -> np.ndarray:
    return np.clip(p, PROB_FLOOR, 1.0 - PROB_FLOOR)


def _logit(p: np.ndarray) -> np.ndarray:
    return np.log(p / (1.0 - p))


def _fit_platt(raw: np.ndarray, y: np.ndarray) -> Optional[tuple[float, float]]:
    """Platt scaling on the model's log-odds; None if the slice is one-class."""
    if len(np.unique(y)) < 2:
        return None
    z = _logit(_clip(raw)).reshape(-1, 1)
    lr = LogisticRegression(C=1e6, max_iter=1000).fit(z, y)
    return float(lr.coef_[0][0]), float(lr.intercept_[0])


def _apply_platt(raw: np.ndarray, platt: Optional[tuple[float, float]]) -> np.ndarray:
    if platt is None:
        return _clip(raw)
    a, b = platt
    return _clip(1.0 / (1.0 + np.exp(-(a * _logit(_clip(raw)) + b))))


def _acc_ll(p: np.ndarray, y: np.ndarray) -> dict[str, float]:
    q = np.clip(p, 1e-12, 1.0 - 1e-12)
    return {
        "games": int(len(y)),
        "accuracy": float(np.mean((q >= 0.5) == (y == 1))),
        "log_loss": float(-np.mean(y * np.log(q) + (1 - y) * np.log(1.0 - q))),
    }


class NbaEngine(SportEngine):
    sport = Sport.NBA

    def __init__(
        self,
        state_path: Path | None = None,
        raw_dir: Path | None = None,
        model_path: Path | None = None,
    ) -> None:
        self.state_path = state_path or DATA_DIR / "nba_state.json"
        self.raw_dir = raw_dir or RAW_DIR / "nba"
        self.model_path = model_path or MODELS_DIR / "nba_xgb_v2.pkl"
        self.elo = EloBook(config=ELO_CONFIG)
        self.points_for: dict[str, deque[float]] = {}
        self.points_against: dict[str, deque[float]] = {}
        self.last_game: dict[str, str] = {}  # team -> iso date of previous game
        self.games_played: dict[str, int] = {}  # team -> games this season
        self.last_processed: Optional[date] = None
        # One row per replayed game: (iso date, pre-game P(home win), home won)
        self.backtest: list[tuple[str, float, int]] = []
        # Parallel to backtest: the pre-game FEATURE_NAMES vector per game.
        self.feature_log: list[list[float]] = []
        self._model: Optional[dict] = None  # trained bundle (see train_model)
        self._load_state()
        self._load_model()

    @property
    def model_name(self) -> str:
        if self._model and self._model.get("mode") in ("xgb", "blend"):
            return MODEL_NAME_V2
        return MODEL_NAME_V1

    # ------------------------------------------------------------- engine API

    async def upcoming_games(self, on: date | None = None) -> list[Game]:
        return await espn.scoreboard(Sport.NBA, on)

    async def predict(self, games: list[Game]) -> list[Prediction]:
        now = datetime.now(timezone.utc)
        mode = self._model.get("mode") if self._model else "elo"
        predictions: list[Prediction] = []
        for game in games:
            elo_prob = self.elo.expected(game.home, game.away)
            home_prob = elo_prob
            if mode in ("xgb", "blend"):
                row = self._feature_row(self._slate_date(game), game.home, game.away)
                ml_prob = float(self._model_prob(np.asarray([row]))[0])
                home_prob = 0.5 * (ml_prob + elo_prob) if mode == "blend" else ml_prob
            home_score = (
                self._avg(self.points_for, game.home)
                + self._avg(self.points_against, game.away)
            ) / 2.0
            away_score = (
                self._avg(self.points_for, game.away)
                + self._avg(self.points_against, game.home)
            ) / 2.0
            total = home_score + away_score
            # Nudge toward the Elo favorite; equal-and-opposite keeps the total.
            elo_diff = (
                self.elo.get(game.home)
                + self.elo.config.home_advantage
                - self.elo.get(game.away)
            )
            nudge = 0.5 * (elo_diff / 100.0)
            # A team with no scoring history would price at the league-average
            # placeholder — a constant, not a forecast. The board printed UNDER
            # edges against exactly that (WNBA, 2026-09-02). Emit no total.
            scored = bool(self.points_for.get(game.home)) and bool(self.points_for.get(game.away))
            predictions.append(
                Prediction(
                    game_id=game.id,
                    sport=Sport.NBA,
                    model=self.model_name,
                    generated_at=now,
                    home_win_prob=home_prob,
                    away_win_prob=1.0 - home_prob,
                    predicted_home_score=round(home_score + nudge, 1) if scored else None,
                    predicted_away_score=round(away_score - nudge, 1) if scored else None,
                    predicted_total=round(total, 1) if scored else None,
                    confidence=abs(home_prob - 0.5) * 2.0,
                )
            )
        return predictions

    async def refresh_data(self) -> None:
        """Backfill/extend the ESPN results cache, replay it, retrain if stale.

        Incremental: resumes the day after `last_processed` and only touches
        days whose games are guaranteed final (strictly before today, UTC).
        """
        start = (
            self.last_processed + timedelta(days=1)
            if self.last_processed
            else HISTORY_START
        )
        end = datetime.now(timezone.utc).date() - timedelta(days=1)
        if start <= end:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            since_checkpoint = 0
            async with httpx.AsyncClient(timeout=30) as client:
                day = start
                while day <= end:
                    payload = await self._day_json(client, day)
                    self._process_day(day, payload)
                    since_checkpoint += 1
                    if since_checkpoint >= CHECKPOINT_EVERY_DAYS:
                        self._save_state()
                        since_checkpoint = 0
                    day += timedelta(days=1)
            self._save_state()
        if self._model_stale():
            self.train_model()

    # ------------------------------------------------------------ replay core

    def _process_day(self, day: date, payload: dict) -> None:
        """Roll the season if needed, then replay the day's completed games."""
        if (
            self.last_processed is not None
            and _season_key(day) != _season_key(self.last_processed)
        ):
            self.elo.new_season()
            self.last_game.clear()  # rest/games-played are per-season signals
            self.games_played.clear()
        for home, away, home_score, away_score in self._completed_games(payload):
            self._replay_game(day, home, away, home_score, away_score)
        self.last_processed = day

    def _replay_game(
        self, day: date, home: str, away: str, home_score: int, away_score: int
    ) -> None:
        """One historical result: log prob + features first, then update state."""
        prob = self.elo.expected(home, away)
        home_won = home_score > away_score
        # 5-field rows: team names let the dollar-ROI backtest join this log
        # against backfilled Polymarket prices (store/roibacktest.py).
        self.backtest.append((day.isoformat(), prob, int(home_won), home, away))
        self.feature_log.append(self._feature_row(day, home, away))
        self.elo.update(home, away, home_won, margin=abs(home_score - away_score))
        for team, scored, allowed in (
            (home, home_score, away_score),
            (away, away_score, home_score),
        ):
            self._rolling(self.points_for, team).append(float(scored))
            self._rolling(self.points_against, team).append(float(allowed))
            self.last_game[team] = day.isoformat()
            self.games_played[team] = self.games_played.get(team, 0) + 1

    @staticmethod
    def _completed_games(payload: dict) -> Iterator[tuple[str, str, int, int]]:
        """(home, away, home_score, away_score) for each final in a scoreboard.

        Preseason (season.type outside {2, 3}) and All-Star exhibitions
        (competition type ALLSTAR — ESPN files those under the regular
        season) are skipped so they never pollute the ratings.
        """
        for event in payload.get("events", []):
            season_type = event.get("season", {}).get("type", 2)
            if season_type not in (2, 3):
                continue
            comp = event.get("competitions", [{}])[0]
            comp_type = comp.get("type", {})
            if comp_type.get("abbreviation") == "ALLSTAR" or comp_type.get("id") == "4":
                continue
            if not comp.get("status", {}).get("type", {}).get("completed"):
                continue
            competitors = comp.get("competitors", [])
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            try:
                yield (
                    home["team"]["displayName"],
                    away["team"]["displayName"],
                    int(home["score"]),
                    int(away["score"]),
                )
            except (KeyError, TypeError, ValueError):
                continue

    async def _day_json(self, client: httpx.AsyncClient, day: date) -> dict:
        """One day's raw scoreboard, from cache or ESPN (cached on fetch)."""
        cache = self.raw_dir / f"{day:%Y%m%d}.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        for attempt in range(5):
            resp = await client.get(SCOREBOARD_URL, params={"dates": f"{day:%Y%m%d}"})
            if resp.status_code == 429 or resp.status_code >= 500:
                await asyncio.sleep(3.0 * (attempt + 1))
                continue
            resp.raise_for_status()
            payload = resp.json()
            cache.write_text(json.dumps(payload), encoding="utf-8")
            return payload
        raise RuntimeError(f"ESPN scoreboard unavailable for {day} after retries")

    # --------------------------------------------------------------- features

    def _feature_row(self, day: date, home: str, away: str) -> list[float]:
        """Strictly pre-game FEATURE_NAMES vector, home-perspective.

        Uses only state accumulated before `day`'s result, so the same
        function serves both the replay log and live predict(). Unknown
        teams get league-average scoring, the REST_CAP rest sentinel, and
        zero games played.
        """
        home_pf = self._avg(self.points_for, home)
        home_pa = self._avg(self.points_against, home)
        away_pf = self._avg(self.points_for, away)
        away_pa = self._avg(self.points_against, away)
        home_rest = self._rest_days(home, day)
        away_rest = self._rest_days(away, day)
        return [
            self.elo.get(home) - self.elo.get(away),
            home_pf,
            home_pa,
            away_pf,
            away_pa,
            (home_pf - home_pa) - (away_pf - away_pa),
            float(home_rest),
            float(away_rest),
            float(home_rest == 1),
            float(away_rest == 1),
            float(self.games_played.get(home, 0)),
            float(self.games_played.get(away, 0)),
        ]

    def _rest_days(self, team: str, day: date) -> int:
        """Days since the team's previous game, capped at REST_CAP.

        A team with no previous game this season gets the cap (fully
        rested), which doubles as the unknown-team sentinel.
        """
        last = self.last_game.get(team)
        if not last:
            return REST_CAP
        return min((day - date.fromisoformat(last)).days, REST_CAP)

    @staticmethod
    def _slate_date(game: Game) -> date:
        """The US slate date of a game, matching the replay's scoreboard days.

        ESPN day files are keyed by the US (Eastern) date; a 7pm ET tip is
        already the next day in UTC, so shift 5 hours before taking .date().
        """
        start = game.start_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return (start - timedelta(hours=5)).date()

    @staticmethod
    def _rolling(table: dict[str, deque[float]], team: str) -> deque[float]:
        if team not in table:
            table[team] = deque(maxlen=ROLLING_WINDOW)
        return table[team]

    @staticmethod
    def _avg(table: dict[str, deque[float]], team: str) -> float:
        values = table.get(team)
        if not values:
            return LEAGUE_AVG_POINTS
        return sum(values) / len(values)

    # ---------------------------------------------------------------- model

    def train_model(self) -> Optional[dict]:
        """Walk-forward fit; ship the validation log-loss winner.

        Chronology: fit on 2024-25 + the first half of 2025-26 (the fit
        set's last TRAIN_TAIL_FRACTION reserved for early stopping and Platt
        calibration), validate on the second half of 2025-26 — never touched
        by any fit. Elo / calibrated-XGB / 50-50 blend compete on validation
        log loss; "elo" winning gates the ML path off. A logistic-regression
        fallback (elo_diff + rest) is evaluated for the report only. The
        bundle (including the gate-off verdict) pickles to model_path.
        Returns None when the replay is too short to split honestly.
        """
        if len(self.feature_log) != len(self.backtest):
            return None  # legacy/partial state — needs a full replay first
        days = [r[0] for r in self.backtest]
        season_start = CURRENT_SEASON_START.isoformat()
        current = [i for i, d in enumerate(days) if d >= season_start]
        if len(days) < MIN_TRAINING_GAMES or len(current) < MIN_CURRENT_GAMES:
            return None
        split = current[len(current) // 2]
        X = np.asarray(self.feature_log, dtype=float)
        y = np.asarray([r[2] for r in self.backtest], dtype=int)
        elo_p = np.asarray([r[1] for r in self.backtest], dtype=float)
        X_train, y_train = X[:split], y[:split]
        X_val, y_val, elo_val = X[split:], y[split:], elo_p[split:]

        # Tiny, heavily regularized booster — ~2 seasons is very little data.
        tail = max(20, int(len(X_train) * TRAIN_TAIL_FRACTION))
        clf = XGBClassifier(
            n_estimators=200,
            max_depth=3,
            learning_rate=0.03,
            min_child_weight=25,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_lambda=5.0,
            reg_alpha=1.0,
            gamma=0.25,
            monotone_constraints="(" + ",".join(
                "1" if name == "elo_diff" else "0" for name in FEATURE_NAMES
            ) + ")",
            objective="binary:logistic",
            eval_metric="logloss",
            early_stopping_rounds=25,
            n_jobs=2,
            random_state=7,
            verbosity=0,
        )
        clf.fit(
            X_train[:-tail],
            y_train[:-tail],
            eval_set=[(X_train[-tail:], y_train[-tail:])],
            verbose=False,
        )
        platt = _fit_platt(clf.predict_proba(X_train[-tail:])[:, 1], y_train[-tail:])
        xgb_val = _apply_platt(clf.predict_proba(X_val)[:, 1], platt)

        logreg = make_pipeline(
            StandardScaler(), LogisticRegression(max_iter=1000)
        ).fit(X_train[:, LOGREG_COLS], y_train)
        logreg_val = _clip(logreg.predict_proba(X_val[:, LOGREG_COLS])[:, 1])

        val = {
            "elo": _acc_ll(elo_val, y_val),
            "xgb": _acc_ll(xgb_val, y_val),
            "blend": _acc_ll(0.5 * (xgb_val + elo_val), y_val),
            "logreg": _acc_ll(logreg_val, y_val),
        }
        mode = min(("elo", "xgb", "blend"), key=lambda m: val[m]["log_loss"])
        bundle = {
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,  # "elo" = gated off, predict() stays on the Elo path
            "booster": clf,
            "platt": platt,
            "feature_names": FEATURE_NAMES,
            "val": val,
            "val_range": (days[split], days[-1]),
        }
        self._save_model(bundle)
        self._model = bundle
        return bundle

    def _model_prob(self, X: np.ndarray) -> np.ndarray:
        """Calibrated, clipped P(home win) from the shipped booster."""
        raw = self._model["booster"].predict_proba(X)[:, 1]
        return _apply_platt(raw, self._model["platt"])

    def _model_stale(self) -> bool:
        """True when the bundle is missing, unloadable, aged out, OR the
        pickle is gone from disk.

        The disk check is what makes RETRAIN work: refresh_sport deletes the
        pickle before calling refresh_data(), but a loaded engine still holds
        the bundle in memory. Consulting only self._model would report "fresh"
        and skip the retrain, leaving the artifact deleted until the next
        backend restart (nfl._model_stale checks the path for the same reason).
        """
        if not self.model_path.exists():
            return True
        if not self._model:
            return True
        try:
            trained = datetime.fromisoformat(self._model["trained_at"])
        except (KeyError, TypeError, ValueError):
            return True
        return datetime.now(timezone.utc) - trained > timedelta(
            days=MODEL_MAX_AGE_DAYS
        )

    def _load_model(self) -> None:
        if not self.model_path.exists():
            return
        try:
            bundle = pickle.loads(self.model_path.read_bytes())
        except Exception:  # corrupt/foreign pickle — fall back to Elo
            return
        if not (
            isinstance(bundle, dict)
            and bundle.get("mode") in ("elo", "xgb", "blend")
            and bundle.get("feature_names") == FEATURE_NAMES
        ):
            return
        if not modelio.matches(bundle):
            # Written by a different sklearn/xgboost minor — its predictions
            # aren't guaranteed to reproduce. Leaving self._model unset falls
            # back to Elo AND makes _model_stale() True, so refresh_data()
            # retrains it. See core/modelio.
            logger.warning(
                "nba: model pickle was written by a different library stack "
                "(%s vs %s) — falling back to Elo until it is retrained",
                bundle.get(modelio.STAMP_KEY), modelio.library_versions(),
            )
            return
        self._model = bundle

    def _save_model(self, bundle: dict) -> None:
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.model_path.with_suffix(".pkl.tmp")
        tmp.write_bytes(pickle.dumps(modelio.stamp(bundle)))
        tmp.replace(self.model_path)

    # -------------------------------------------------------------- validation

    def backtest_metrics(self, since: date | None = None) -> dict[str, float]:
        """Accuracy and log loss of the logged pre-game Elo probabilities."""
        cutoff = since.isoformat() if since else ""
        rows = [(r[1], r[2]) for r in self.backtest if r[0] >= cutoff]
        if not rows:
            return {"games": 0, "accuracy": float("nan"), "log_loss": float("nan")}
        correct = sum(1 for p, won in rows if (p >= 0.5) == bool(won))
        eps = 1e-12
        log_loss = -sum(
            math.log(max(p, eps)) if won else math.log(max(1.0 - p, eps))
            for p, won in rows
        ) / len(rows)
        return {
            "games": len(rows),
            "accuracy": correct / len(rows),
            "log_loss": log_loss,
        }

    # ----------------------------------------------------------- persistence

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if state.get("version") != STATE_VERSION:
            return  # pre-v2 state lacks the feature log — force a full replay
        self.elo.ratings = {k: float(v) for k, v in state.get("ratings", {}).items()}
        for attr, key in (
            (self.points_for, "points_for"),
            (self.points_against, "points_against"),
        ):
            for team, values in state.get(key, {}).items():
                attr[team] = deque(
                    (float(v) for v in values), maxlen=ROLLING_WINDOW
                )
        self.last_game = {t: str(d) for t, d in state.get("last_game", {}).items()}
        self.games_played = {
            t: int(n) for t, n in state.get("games_played", {}).items()
        }
        if state.get("last_processed"):
            self.last_processed = date.fromisoformat(state["last_processed"])
        self.backtest = [
            (str(r[0]), float(r[1]), int(r[2]), str(r[3]), str(r[4]))
            for r in state.get("backtest", [])
        ]
        self.feature_log = [
            [float(v) for v in row] for row in state.get("features", [])
        ]

    def _save_state(self) -> None:
        state = {
            "version": STATE_VERSION,
            "model": self.model_name,
            "last_processed": (
                self.last_processed.isoformat() if self.last_processed else None
            ),
            "ratings": self.elo.ratings,
            "points_for": {t: list(v) for t, v in self.points_for.items()},
            "points_against": {t: list(v) for t, v in self.points_against.items()},
            "last_game": self.last_game,
            "games_played": self.games_played,
            "backtest": self.backtest,
            "features": self.feature_log,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self.state_path)


def build_engine() -> NbaEngine:
    return NbaEngine()


if __name__ == "__main__":

    async def _main() -> None:
        engine = build_engine()
        await engine.refresh_data()
        overall = engine.backtest_metrics()
        current = engine.backtest_metrics(since=CURRENT_SEASON_START)
        print(f"model: {engine.model_name}")
        print(f"history through: {engine.last_processed}")
        print(f"teams rated: {len(engine.elo.ratings)}")
        for label, metrics in (("all (2024-26)", overall), ("2025-26", current)):
            print(
                f"{label} (elo): {metrics['games']} games  "
                f"accuracy={metrics['accuracy']:.4f}  "
                f"log_loss={metrics['log_loss']:.4f}"
            )
        bundle = engine._model
        if bundle is None:
            print("no trained bundle (replay too short) — pure Elo path")
            return
        lo, hi = bundle["val_range"]
        print(f"\nvalidation - second half of 2025-26 ({lo} to {hi}):")
        print(f"{'model':<8}{'games':>7}{'accuracy':>10}{'log_loss':>10}")
        for name in ("elo", "xgb", "blend", "logreg"):
            m = bundle["val"][name]
            print(
                f"{name:<8}{m['games']:>7}"
                f"{m['accuracy']:>10.4f}{m['log_loss']:>10.4f}"
            )
        print(f"shipped: {bundle['mode']}  (model_name={engine.model_name})")
        gains = bundle["booster"].get_booster().get_score(importance_type="gain")
        top = sorted(
            ((FEATURE_NAMES[int(k[1:])], v) for k, v in gains.items()),
            key=lambda kv: -kv[1],
        )[:6]
        print("top features by gain: " + ", ".join(f"{n}={v:.1f}" for n, v in top))

    asyncio.run(_main())

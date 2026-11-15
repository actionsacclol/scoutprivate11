"""NBA engine — Elo with margin-of-victory scaling (nba-elo-v1).

Standard Elo (K=12, margin-of-victory scaling, +40 rating points of home
advantage, half-carry between seasons) replayed over every completed NBA
game since October 2024, plus a back-to-back overlay (a side that played
yesterday gives up B2B_ELO points; ratings never see it) and rolling last-15
scoring for totals. Raw daily
scoreboards are cached under RAW_DIR/nba/ and the replayed state persists to
DATA_DIR/nba_state.json so refresh_data() stays incremental.

There is no trained model here. A v2 overlay (an XGBoost classifier over
Elo, rolling scoring, rest and games-played features — `nba-xgb-v2`) was
built and retired in November 2026. It retrained weekly and was allowed to
serve only when it beat Elo on held-out games, and it never earned that: the
last bundle it wrote (2026-11-05, validated on 719 games from 2026-01-30)
scored log loss 0.608 against Elo's 0.567, the 50/50 blend 0.583, and gated
itself off — as the July bundle had (0.623 vs 0.579, n=658). Every install
was already serving `nba-elo-v1`. What the overlay cost was certain: the
xgboost and scikit-learn runtimes in every installer, a version-stamped
pickle under MODELS_DIR, and the heaviest import of the cold start.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

import httpx

from kryptscout.config import DATA_DIR, RAW_DIR
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
ROLLING_WINDOW = 15  # games in the rolling points-for/against averages
LEAGUE_AVG_POINTS = 113.0  # fallback before a team has any scoring history
CHECKPOINT_EVERY_DAYS = 30  # persist state periodically during long backfills
# v3 added team names to backtest rows (dollar-ROI join). v4 (2026-09-02):
# the Elo retune (K 20→12, home 100→60, carry 0.75→0.5) changes how every
# rating is accumulated, so a state replayed under the old constants must be
# discarded and rebuilt from the cached scoreboards. Dropping the v2 feature
# log (November 2026) did NOT bump this: a v4 file's ratings are exactly what
# this engine computes, and its extra keys are simply ignored on load.
# v5 (2026-11): home advantage 60 -> 40 changes every rating update, and the
# state gains each team's last game day for the back-to-back overlay.
STATE_VERSION = 5

# Retuned 2026-09-02 on the engine's own archive (walk-forward, held out
# from 2026-01): K 20 → 12, home advantage 100 → 60, season carry 0.75 → 0.5
# took log loss from 0.5961 to 0.5824, −0.0137 [−0.0235, −0.0035]. The old
# 100-point home edge and fast K were the source of the calibration audit's
# finding that NBA favourites were overstated by 8 points. Replayed after
# the retune: slope 1.07 over the whole log and 1.17 on 2026-01+ (favourites
# now ~2 pts UNDER-stated), so no logit scale is applied here — the retune
# alone fixed the calibration. Two seasons of data behind it; retune again
# when a third exists.
#
# Home advantage 60 -> 40 (2026-11, three seasons): at 60 the engine stated
# 57.5% home wins against 53.6 / 55.5 / 52.5% played, +3.0 pts [+1.3, +4.7]
# pooled over 2,514 games. Walk-forward the log-loss change is inside the
# noise (2025-26: -0.0007 [-0.0035, +0.0020]; 2026-27 to date: -0.0042
# [-0.0117, +0.0038]); this is a bias fix, not a forecast gain. K and carry
# were re-checked on the same grid and stay.
ELO_CONFIG = EloConfig(
    k=12.0,
    home_advantage=40.0,
    mov_scaling=True,
    season_carry=0.5,
)

# Back-to-back overlay: Elo points taken from a side that played the day
# before. Home sides on a back-to-back (opponent rested) won 9.6 pts
# [4.0, 15.0] less often than Elo said (n=276); the closing line was within
# 2.4 pts on the same games. Worth -0.0003 / -0.0012 log loss on the two
# held-out seasons on top of the home-advantage change — not significant on
# its own, kept small. 0 disables it.
B2B_ELO = 25.0
# US game day from a UTC tip-off: NBA games tip 12:00-22:30 Eastern, which is
# 16:00-03:30 UTC; six hours back lands on the Eastern date in both DST
# regimes without needing a timezone database in the frozen bundle.
GAME_DAY_UTC_OFFSET = timedelta(hours=6)

MODEL_NAME = "nba-elo-v1"


def _season_key(day: date) -> int:
    """Season identified by its starting year: 2024-25 -> 2024."""
    return day.year if day.month >= SEASON_BOUNDARY_MONTH else day.year - 1


def _season_start(day: date) -> date:
    """First day of the season `day` falls in (the Aug 1 boundary)."""
    return date(_season_key(day), SEASON_BOUNDARY_MONTH, 1)


class NbaEngine(SportEngine):
    sport = Sport.NBA
    model_name = MODEL_NAME

    def __init__(
        self,
        state_path: Path | None = None,
        raw_dir: Path | None = None,
    ) -> None:
        self.state_path = state_path or DATA_DIR / "nba_state.json"
        self.raw_dir = raw_dir or RAW_DIR / "nba"
        self.elo = EloBook(config=ELO_CONFIG)
        self.points_for: dict[str, deque[float]] = {}
        self.points_against: dict[str, deque[float]] = {}
        self.last_processed: Optional[date] = None
        # Each team's most recent game day (ISO) — the back-to-back overlay.
        self.last_game: dict[str, str] = {}
        # One row per replayed game: (iso date, pre-game P(home win), home
        # won, home, away) — the walk-forward log the backtest pages read.
        self.backtest: list[tuple[str, float, int, str, str]] = []
        self._load_state()

    # ------------------------------------------------------------- engine API

    async def upcoming_games(self, on: date | None = None) -> list[Game]:
        return await espn.scoreboard(Sport.NBA, on)

    async def predict(self, games: list[Game]) -> list[Prediction]:
        now = datetime.now(timezone.utc)
        predictions: list[Prediction] = []
        for game in games:
            home_prob = self._home_prob(
                game.home, game.away, (game.start_time - GAME_DAY_UTC_OFFSET).date()
            )
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
        """Backfill/extend the ESPN results cache and replay it.

        Incremental: resumes the day after `last_processed` and only touches
        days whose games are guaranteed final (strictly before today, UTC).
        """
        start = (
            self.last_processed + timedelta(days=1)
            if self.last_processed
            else HISTORY_START
        )
        end = datetime.now(timezone.utc).date() - timedelta(days=1)
        if start > end:
            return
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

    # ------------------------------------------------------------ replay core

    def _process_day(self, day: date, payload: dict) -> None:
        """Roll the season if needed, then replay the day's completed games."""
        if (
            self.last_processed is not None
            and _season_key(day) != _season_key(self.last_processed)
        ):
            self.elo.new_season()
        for home, away, home_score, away_score in self._completed_games(payload):
            self._replay_game(day, home, away, home_score, away_score)
        self.last_processed = day

    def _replay_game(
        self, day: date, home: str, away: str, home_score: int, away_score: int
    ) -> None:
        """One historical result: log the pre-game prob first, then update."""
        prob = self._home_prob(home, away, day)
        home_won = home_score > away_score
        # 5-field rows: team names let the dollar-ROI backtest join this log
        # against backfilled Polymarket prices (store/roibacktest.py).
        self.backtest.append((day.isoformat(), prob, int(home_won), home, away))
        self.elo.update(home, away, home_won, margin=abs(home_score - away_score))
        for team, scored, allowed in (
            (home, home_score, away_score),
            (away, away_score, home_score),
        ):
            self._rolling(self.points_for, team).append(float(scored))
            self._rolling(self.points_against, team).append(float(allowed))
            self.last_game[team] = day.isoformat()

    def _played_yesterday(self, team: str, day: date) -> bool:
        last = self.last_game.get(team)
        return bool(last) and (day - date.fromisoformat(last)).days == 1

    def _home_prob(self, home: str, away: str, day: date) -> float:
        """Elo P(home) with the back-to-back overlay; ratings never see it."""
        diff = self.elo.get(home) + self.elo.config.home_advantage - self.elo.get(away)
        if B2B_ELO:
            if self._played_yesterday(home, day):
                diff -= B2B_ELO
            if self._played_yesterday(away, day):
                diff += B2B_ELO
        return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))

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

    # ---------------------------------------------------------------- rolling

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
            return  # replayed under other Elo constants — force a full replay
        self.elo.ratings = {k: float(v) for k, v in state.get("ratings", {}).items()}
        for attr, key in (
            (self.points_for, "points_for"),
            (self.points_against, "points_against"),
        ):
            for team, values in state.get(key, {}).items():
                attr[team] = deque(
                    (float(v) for v in values), maxlen=ROLLING_WINDOW
                )
        if state.get("last_processed"):
            self.last_processed = date.fromisoformat(state["last_processed"])
        self.last_game = {k: str(v) for k, v in state.get("last_game", {}).items()}
        self.backtest = [
            (str(r[0]), float(r[1]), int(r[2]), str(r[3]), str(r[4]))
            for r in state.get("backtest", [])
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
            "backtest": self.backtest,
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
        print(f"model: {engine.model_name}")
        print(f"history through: {engine.last_processed}")
        print(f"teams rated: {len(engine.elo.ratings)}")
        current = _season_start(engine.last_processed or datetime.now(timezone.utc).date())
        for label, since in (("all", None), (f"since {current}", current)):
            metrics = engine.backtest_metrics(since=since)
            print(
                f"{label}: {metrics['games']} games  "
                f"accuracy={metrics['accuracy']:.4f}  "
                f"log_loss={metrics['log_loss']:.4f}"
            )

    asyncio.run(_main())

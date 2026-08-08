"""WNBA engine — Elo baseline (v1).

Model: standard Elo (K=28 — the 44-game WNBA season is far shorter than the
NBA's so ratings must adapt faster — margin-of-victory scaling, +80 rating
points of home advantage) replayed over every completed WNBA game since May
2024, sourced from ESPN's scoreboard-by-date API. The WNBA is a summer
league, so the date loop only touches May–October of each year; between
seasons ratings regress 25% toward the mean (carry=0.75) at the Jan 1
boundary. Expansion teams (Golden State Valkyries 2025, Toronto Tempo and
Portland Fire 2026) simply start at the 1500 initial rating on first sight.

Totals come from per-team rolling last-10-game scoring: a side's predicted
score is the average of its own points-for and the opponent's points-against,
nudged by the Elo gap (favorite up, underdog down, total unchanged).

Exhibitions never touch the ratings: the All-Star game (ESPN files it under
the regular season but marks the competition type ALLSTAR) and the
Commissioner's Cup Championship (a standalone final that does not count in
the standings — the qualifying Cup games are regular-season games and are
kept) are both filtered out of the replay.

Raw daily scoreboards are cached under RAW_DIR/wnba/ so re-runs are free, and
the replayed state (ratings, rolling scoring, backtest log, last processed
date) persists to DATA_DIR/wnba_state.json so refresh_data() is incremental.

v1 is deliberately a baseline: the replay loop records pre-game probabilities
for every historical game (see backtest_metrics), which doubles as the
training/evaluation scaffold for a future ML model (wnba-xgb-v2).
"""
from __future__ import annotations

import asyncio
import json
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

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
)
HISTORY_START = date(2024, 5, 1)  # 2024 season (preseason filtered out)
SEASON_FIRST_MONTH = 5  # WNBA plays May..October; skip the off-season entirely
SEASON_LAST_MONTH = 10
ROLLING_WINDOW = 10  # games in the rolling points-for/against averages
LEAGUE_AVG_POINTS = 82.0  # fallback before a team has any scoring history
CHECKPOINT_EVERY_DAYS = 30  # persist state periodically during long backfills

ELO_CONFIG = EloConfig(
    k=28.0,
    home_advantage=80.0,
    mov_scaling=True,
    season_carry=0.75,
)


def _season_key(day: date) -> int:
    """WNBA seasons live entirely inside one calendar year (May-Oct)."""
    return day.year


def _in_season(day: date) -> bool:
    return SEASON_FIRST_MONTH <= day.month <= SEASON_LAST_MONTH


class WnbaEngine(SportEngine):
    sport = Sport.WNBA
    model_name = "wnba-elo-v1"

    def __init__(
        self,
        state_path: Path | None = None,
        raw_dir: Path | None = None,
    ) -> None:
        self.state_path = state_path or DATA_DIR / "wnba_state.json"
        self.raw_dir = raw_dir or RAW_DIR / "wnba"
        self.elo = EloBook(config=ELO_CONFIG)
        self.points_for: dict[str, deque[float]] = {}
        self.points_against: dict[str, deque[float]] = {}
        self.last_processed: Optional[date] = None
        # One row per replayed game: (iso date, pre-game P(home win), home won)
        self.backtest: list[tuple[str, float, int]] = []
        self._load_state()

    # ------------------------------------------------------------- engine API

    async def upcoming_games(self, on: date | None = None) -> list[Game]:
        return await espn.scoreboard(Sport.WNBA, on)

    async def predict(self, games: list[Game]) -> list[Prediction]:
        now = datetime.now(timezone.utc)
        predictions: list[Prediction] = []
        for game in games:
            home_prob = self.elo.expected(game.home, game.away)
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
            predictions.append(
                Prediction(
                    game_id=game.id,
                    sport=Sport.WNBA,
                    model=self.model_name,
                    generated_at=now,
                    home_win_prob=home_prob,
                    away_win_prob=1.0 - home_prob,
                    predicted_home_score=round(home_score + nudge, 1),
                    predicted_away_score=round(away_score - nudge, 1),
                    predicted_total=round(total, 1),
                    confidence=abs(home_prob - 0.5) * 2.0,
                )
            )
        return predictions

    async def refresh_data(self) -> None:
        """Backfill/extend the ESPN results cache and replay it into Elo.

        Incremental: resumes the day after `last_processed` and only touches
        days whose games are guaranteed final (strictly before today, UTC).
        Off-season days (November-April) are skipped without fetching.
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
                if _in_season(day):
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
        """One historical result: log the pre-game prob, then update state."""
        prob = self.elo.expected(home, away)
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

    @staticmethod
    def _completed_games(payload: dict) -> Iterator[tuple[str, str, int, int]]:
        """(home, away, home_score, away_score) for each final in a scoreboard.

        Skipped so they never pollute the ratings: preseason (season.type
        outside {2, 3}), the All-Star exhibition (competition type ALLSTAR —
        ESPN files it under the regular season), and the Commissioner's Cup
        Championship (a standalone final outside the standings; the Cup
        *qualifying* games are ordinary regular-season games and are kept).
        """
        for event in payload.get("events", []):
            season_type = event.get("season", {}).get("type", 2)
            if season_type not in (2, 3):
                continue
            comp = event.get("competitions", [{}])[0]
            comp_type = comp.get("type", {})
            if comp_type.get("abbreviation") == "ALLSTAR" or comp_type.get("id") == "4":
                continue
            if _is_commissioners_cup_final(event, comp):
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
        """Accuracy and log loss of the logged pre-game probabilities."""
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
        rows = state.get("backtest", [])
        if rows and any(len(r) < 5 for r in rows):
            # Pre-team-names log (3-field rows): drop the whole replayed state
            # so the next refresh rebuilds from the cached raw scoreboards and
            # every row gains home/away for the dollar-ROI price join.
            self.elo.ratings = {}
            self.points_for.clear()
            self.points_against.clear()
            self.last_processed = None
            self.backtest = []
            return
        self.backtest = [
            (str(r[0]), float(r[1]), int(r[2]), str(r[3]), str(r[4]))
            for r in rows
        ]

    def _save_state(self) -> None:
        state = {
            "model": self.model_name,
            "last_processed": (
                self.last_processed.isoformat() if self.last_processed else None
            ),
            "ratings": self.elo.ratings,
            "points_for": {t: list(v) for t, v in self.points_for.items()},
            "points_against": {t: list(v) for t, v in self.points_against.items()},
            "backtest": self.backtest,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self.state_path)


def _is_commissioners_cup_final(event: dict, comp: dict) -> bool:
    """True for the standalone Commissioner's Cup Championship game only.

    ESPN tags it via event/competition notes ("Commissioner's Cup
    Championship"); Cup qualifying games carry no such championship note and
    count in the standings, so they must NOT match.
    """
    notes = list(event.get("notes") or []) + list(comp.get("notes") or [])
    for note in notes:
        headline = str(note.get("headline", "")).lower()
        if "commissioner's cup" in headline and (
            "championship" in headline or "final" in headline
        ):
            return True
    name = str(event.get("name", "")).lower()
    return "commissioner's cup championship" in name


def build_engine() -> WnbaEngine:
    return WnbaEngine()


if __name__ == "__main__":

    async def _main() -> None:
        engine = build_engine()
        await engine.refresh_data()
        print(f"model: {engine.model_name}")
        print(f"history through: {engine.last_processed}")
        print(f"teams rated: {len(engine.elo.ratings)}")
        for label, since in (
            ("all (2024-26)", None),
            ("2025+2026", date(2025, 1, 1)),
            ("2026 only", date(2026, 1, 1)),
        ):
            metrics = engine.backtest_metrics(since=since)
            print(
                f"{label}: {metrics['games']} games  "
                f"accuracy={metrics['accuracy']:.4f}  "
                f"log_loss={metrics['log_loss']:.4f}"
            )
        for team in ("Golden State Valkyries", "Toronto Tempo", "Portland Fire"):
            status = (
                f"{engine.elo.ratings[team]:.1f}"
                if team in engine.elo.ratings
                else "NOT RATED"
            )
            print(f"expansion: {team}: {status}")
        slate = await engine.upcoming_games()
        pending = [g for g in slate if not g.completed]
        print(f"live slate: {len(slate)} games ({len(pending)} not yet final)")
        for game, pred in zip(slate, await engine.predict(slate)):
            print(
                f"  {game.away} @ {game.home}  "
                f"P(home)={pred.home_win_prob:.3f}  "
                f"total={pred.predicted_total}"
            )

    asyncio.run(_main())

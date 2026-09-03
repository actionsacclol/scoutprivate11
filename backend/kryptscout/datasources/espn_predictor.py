"""ESPN's own pre-game win probability — a free third reference.

The same unauthenticated core API that serves the odds document serves a
`predictor` document per event: ESPN's model (FPI/BPI-style, a component
projection for MLB) as a home-win percentage under `homeTeam.statistics`.
No key, no published limit — the same good-citizen rules as jobs/lines.py:
small concurrency, one fetch per game, never on the 60-second board poll.

What it is for (measured 2026-09-02 on 578 completed store games): a
world-class comparator on the PERFORMANCE page. Scout's MLB engine was level
with ESPN (log loss 0.6711 vs 0.6748); its WNBA Elo was clearly behind
(0.5250 vs 0.4996, z +2.3); neither added anything to the DraftKings close.
The document is served for completed events too, frozen before kickoff on
572 of 577 — so history backfills (jobs/predictor.py) instead of accruing,
and a document modified after kickoff is stored with pre_game = 0 and never
scored. NFL and NHL return "not supported"; soccer and tennis are not attempted.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

from kryptscout.core.schemas import Game, Sport
from kryptscout.odds.espn_odds import CORE, TIMEOUT, event_id_for, league_path_for
from kryptscout.store import db

logger = logging.getLogger("kryptscout")

# Sports whose predictor document ESPN serves. NFL and NHL answer HTTP 400
# ("Predictor is not supported"), verified 2026-09-02; soccer/tennis untried.
SUPPORTED = frozenset({Sport.MLB, Sport.WNBA, Sport.NBA, Sport.NCAAF, Sport.NCAAB})
# Statistic names seen carrying the home win % (MLB: gameProjection with
# starter/bullpen/batting/park components; WNBA: teamPredWinpct).
STAT_NAMES = ("gameProjection", "teamPredWinpct", "winProbability")
CONCURRENCY = 4
PAUSE_SECONDS = 0.1


@dataclass(frozen=True)
class EspnPrediction:
    game_id: str
    sport: Sport
    home_prob: float
    last_modified: Optional[datetime]
    fetched_at: datetime
    pre_game: bool


def _stat(doc: dict, side: str) -> Optional[float]:
    for stat in (doc.get(side) or {}).get("statistics") or []:
        if stat.get("name") in STAT_NAMES:
            try:
                value = float(stat.get("value"))
            except (TypeError, ValueError):
                continue
            if 0.0 < value < 100.0:
                return value / 100.0
    return None


def _parse_ts(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_predictor(
    doc: dict, game: Game, fetched_at: datetime
) -> Optional[EspnPrediction]:
    """The document's home win probability, stamped pre-game or not.

    Uses the home side's statistic, falling back to the complement of the
    away side's. `pre_game` is decided by the document's own lastModified
    against kickoff; a document without one is judged by the fetch time.
    """
    home = _stat(doc, "homeTeam")
    if home is None:
        away = _stat(doc, "awayTeam")
        home = 1.0 - away if away is not None else None
    if home is None:
        return None
    modified = _parse_ts(doc.get("lastModified"))
    start = game.start_time if game.start_time.tzinfo else game.start_time.replace(tzinfo=timezone.utc)
    stamp = modified or (fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=timezone.utc))
    return EspnPrediction(
        game_id=game.id,
        sport=game.sport,
        home_prob=home,
        last_modified=modified,
        fetched_at=fetched_at,
        pre_game=stamp < start,
    )


async def fetch_predictors(
    games: list[Game], concurrency: int = CONCURRENCY
) -> dict[str, EspnPrediction]:
    """{game.id: prediction} for every supported game ESPN answers for."""
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, EspnPrediction] = {}
    now = datetime.now(timezone.utc)

    async def one(client: httpx.AsyncClient, game: Game) -> None:
        if game.sport not in SUPPORTED:
            return
        event_id = event_id_for(game)
        path = league_path_for(game)
        if not event_id or not path:
            return
        url = f"{CORE}/{path}/events/{event_id}/competitions/{event_id}/predictor"
        async with sem:
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                logger.debug("espn predictor fetch failed for %s: %s", game.id, exc)
                return
            finally:
                await asyncio.sleep(PAUSE_SECONDS)
        if resp.status_code != 200:
            return  # 404 not published, 400 unsupported league
        try:
            doc = resp.json()
        except ValueError:
            return
        parsed = parse_predictor(doc if isinstance(doc, dict) else {}, game, now)
        if parsed is not None:
            out[game.id] = parsed

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        await asyncio.gather(*(one(client, g) for g in games))
    return out


async def capture(games: list[Game]) -> int:
    """Fetch and store the predictor for games that have none yet; returns rows written.

    Best-effort by design: the capture job calls this after the board is
    recorded, and nothing here may raise into it.
    """
    wanted = [g for g in games if g.sport in SUPPORTED and not g.completed]
    if not wanted:
        return 0
    conn = db.connect()
    try:
        have = db.espn_predictions_present(conn, [g.id for g in wanted])
        todo = [g for g in wanted if g.id not in have]
        if not todo:
            return 0
        found = await fetch_predictors(todo)
        written = 0
        with conn:
            for pred in found.values():
                db.upsert_espn_prediction(conn, pred)
                written += 1
        return written
    except Exception:  # noqa: BLE001 — a reference feed must never break capture
        logger.exception("espn predictor capture failed")
        return 0
    finally:
        conn.close()

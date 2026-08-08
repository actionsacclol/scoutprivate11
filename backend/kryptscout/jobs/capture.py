"""Capture job — record today's boards (games, predictions, odds) to the store.

Usage (from the backend directory):

    python -m kryptscout.jobs.capture [--sports nba,mlb,...] [--no-odds]

Builds every available sport engine via the registry (same imports as
api/main.py), pulls the upcoming slate + predictions, attaches current odds
from every registered provider (ESPN lines and prediction markets need no
key; The Odds API joins when ODDS_API_KEY is set) plus the same +EV edges the
board computes, and hands assembled GameCards to tracker.record_cards()
(which also derives and upserts each game's pick). Missing engines and
failing providers degrade gracefully — predictions are still recorded.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import logging

from kryptscout.core.schemas import GameCard, MoneylineOdds, Sport
from kryptscout.odds import providers as odds_providers
from kryptscout.sports import base as engines
from kryptscout.store import picks as picks_mod
from kryptscout.store import tracker

_SPORT_MODULES = ("nba", "nhl", "nfl", "mlb", "wnba", "soccer", "tennis")


def load_engines() -> None:
    """Import every sport module and register its engine (as api/main.py does).

    A sport whose module fails to import (e.g. not built yet, missing data)
    is skipped so the rest of the capture still runs.
    """
    for name in _SPORT_MODULES:
        try:
            module = importlib.import_module(f"kryptscout.sports.{name}")
            engines.register(module.build_engine())
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("kryptscout").warning(
                "engine %s unavailable: %s", name, exc
            )


async def capture_sport(sport: Sport, with_odds: bool) -> dict[str, int]:
    """Record one sport's board; returns tracker.record_cards() counts."""
    engine = engines.get_engine(sport)
    games = await engine.upcoming_games()
    predictions = {p.game_id: p for p in await engine.predict(games)}

    # All providers' lines snapshot together so line movement / CLV work
    # without any API key; merged_odds isolates individual provider failures.
    odds_by_game: dict[str, list[MoneylineOdds]] = {}
    if with_odds:
        odds_by_game = await odds_providers.merged_odds(sport, games)

    cards: list[GameCard] = []
    for game in games:
        odds = odds_by_game.get(game.id, [])
        # Same fallback + edges the board computes, so recorded picks match
        # the terminal exactly (incl. market-consensus fills).
        pred = picks_mod.apply_market_fallback(predictions.get(game.id), odds)
        edges = (
            picks_mod.compute_edges(game.id, sport, pred, odds, game.start_time)
            if pred
            else []
        )
        cards.append(GameCard(game=game, prediction=pred, odds=odds, edges=edges))
    return tracker.record_cards(cards)


async def capture_all(
    sports: list[Sport] | None = None, with_odds: bool = True
) -> dict[str, dict]:
    """Capture every requested sport (default: all registered engines).

    Shared by the CLI below and POST /api/jobs/capture. Returns per-sport
    counts keyed by sport value: record_cards() counts on success, or
    {"error": message} when the engine is missing or the capture failed —
    one sport must not sink the rest.
    """
    load_engines()
    available = {e.sport for e in engines.all_engines()}
    targets = [s for s in (sports or list(Sport)) if s in available]
    skipped = [s for s in (sports or []) if s not in available]
    results: dict[str, dict] = {}
    for sport in skipped:
        results[sport.value] = {"error": "no engine available, skipped"}
    for sport in targets:
        try:
            results[sport.value] = await capture_sport(sport, with_odds)
        except Exception as exc:  # noqa: BLE001
            results[sport.value] = {"error": f"capture failed ({exc})"}
    return results


async def run(sports: list[Sport] | None, with_odds: bool) -> None:
    results = await capture_all(sports, with_odds)
    for sport, counts in results.items():
        if "error" in counts:
            print(f"{sport}: {counts['error']}")
        else:
            print(
                f"{sport}: {counts['games']} games, "
                f"{counts['predictions']} predictions, "
                f"{counts['snapshots']} odds snapshots, "
                f"{counts['picks']} picks"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--sports",
        help="comma-separated sports (default: every registered engine)",
    )
    parser.add_argument(
        "--no-odds",
        action="store_true",
        help="skip market odds (no provider requests made)",
    )
    args = parser.parse_args()
    sports = (
        [Sport(s.strip().lower()) for s in args.sports.split(",") if s.strip()]
        if args.sports
        else None
    )
    asyncio.run(run(sports, with_odds=not args.no_odds))


if __name__ == "__main__":
    main()

"""Settle job — fill final scores, grade picks, then report performance/ROI.

Usage (from the backend directory):

    python -m kryptscout.jobs.settle

Runs tracker.settle() for every sport (only sports with pending games in the
store do any work; settling also grades that sport's frozen picks), then
prints the performance() summary by (sport, model) and the pick-level roi()
summary.
"""
from __future__ import annotations

import asyncio

from kryptscout.core.schemas import Sport
from kryptscout.store import tracker


async def settle_all() -> dict:
    """Settle every sport, then read back performance and ROI.

    Shared by the CLI below and POST /api/jobs/settle. Returns
    {"results": {sport: settle summary or {"error": message}},
     "performance": tracker.performance(), "roi": tracker.roi()} —
    one sport must not sink the rest.
    """
    results: dict[str, dict] = {}
    for sport in Sport:
        try:
            results[sport.value] = await tracker.settle(sport)
        except Exception as exc:  # noqa: BLE001
            results[sport.value] = {"error": f"settle failed ({exc})"}
    return {
        "results": results,
        "performance": tracker.performance(),
        "roi": tracker.roi(),
    }


async def run() -> None:
    report = await settle_all()
    for sport, summary in report["results"].items():
        if "error" in summary:
            print(f"{sport}: {summary['error']}")
        elif any(summary.values()):
            print(
                f"{sport}: settled={summary['settled']} "
                f"abandoned={summary['abandoned']} pending={summary['pending']} "
                f"picks_graded={summary['picks_graded']}"
            )

    perf = report["performance"]
    if not perf["overall"]:
        print("performance: no settled games with predictions yet")
    else:
        print("performance:")
        for row in perf["overall"]:
            print(
                f"  {row['sport']}/{row['model']}: n={row['n']} "
                f"accuracy={row['accuracy']:.3f} log_loss={row['log_loss']:.4f} "
                f"brier={row['brier']:.4f}"
            )

    overall = report["roi"]["overall"]
    roi_pct = overall["roi_pct"]
    print(
        f"roi: picks={overall['n_picks']} graded={overall['n_graded']} "
        f"units={overall['units']:+.2f}"
        + (f" roi={roi_pct:+.1f}%" if roi_pct is not None else "")
    )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

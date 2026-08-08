"""Model-skill backtest over each engine's persisted walk-forward log.

Every sport engine replays its history when it (re)builds and persists the
result to ``DATA_DIR/{sport}_state.json``. Two grains live there:

* **Per-game log** (nba, wnba) — ``"backtest"`` is a raw list of
  ``[date, prob, outcome]`` rows, where ``prob`` is the model's win
  probability for a fixed referent side and ``outcome`` is 1 when that side
  won. Thousands of rows: enough for a real calibration curve, Brier score
  and a confidence-threshold sweep.
* **Aggregate metrics** (nfl, mlb, ncaaf, ncaab, tennis) — only a summary
  dict was persisted (``"metrics"``, or per-tour ``"backtest"`` for tennis).
  We surface those numbers but can't draw a curve from them.

nhl/soccer persist neither and report ``kind == "none"``.

This is a *skill* backtest (does the model's stated confidence match reality),
NOT a dollar-P&L one — these logs carry no odds/prices. The betting-ROI side
lives in ``tracker.roi()`` / ``/api/roi``, graded off recorded picks.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from kryptscout import config
from kryptscout.core.schemas import Sport

# Confidence thresholds for the sweep table: "only count picks the model is at
# least this sure of." Confidence is max(p, 1-p), so 0.50 is every game.
_SWEEP_THRESHOLDS = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85)
_CAL_BINS = 10
_EPS = 1e-15


def _state_path(sport: Sport) -> Path:
    """Where the engine persists its replayed state (mirrors jobs.refresh)."""
    return config.DATA_DIR / f"{sport.value}_state.json"


def _load_state(sport: Sport) -> dict | None:
    path = _state_path(sport)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _clean_rows(raw: object) -> list[tuple[str, float, int]]:
    """Keep only well-formed [date, prob(0..1), outcome(0/1)] rows."""
    rows: list[tuple[str, float, int]] = []
    if not isinstance(raw, list):
        return rows
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        date, prob, outcome = item[0], item[1], item[2]
        if not isinstance(prob, (int, float)) or not math.isfinite(prob):
            continue
        if not 0.0 <= float(prob) <= 1.0:
            continue
        if outcome not in (0, 1, 0.0, 1.0):
            continue
        rows.append((str(date), float(prob), int(outcome)))
    return rows


def _log_report(rows: list[tuple[str, float, int]]) -> dict:
    """Calibration + Brier + confidence-threshold sweep over per-game rows."""
    n = len(rows)
    correct = sum(1 for _, p, y in rows if (p >= 0.5) == (y == 1))
    brier = sum((p - y) ** 2 for _, p, y in rows) / n
    log_loss = sum(
        -(y * math.log(min(max(p, _EPS), 1 - _EPS))
          + (1 - y) * math.log(1 - min(max(p, _EPS), 1 - _EPS)))
        for _, p, y in rows
    ) / n

    # 10-bin reliability: mean predicted prob vs realized win rate per bin.
    bins: list[dict] = []
    for i in range(_CAL_BINS):
        lo = i / _CAL_BINS
        hi = (i + 1) / _CAL_BINS
        # Last bin is inclusive of 1.0.
        members = [
            (p, y) for _, p, y in rows
            if (lo <= p < hi) or (i == _CAL_BINS - 1 and p == 1.0)
        ]
        k = len(members)
        bins.append({
            "lo": lo,
            "hi": hi,
            "n": k,
            "predicted": (sum(p for p, _ in members) / k) if k else None,
            "actual": (sum(y for _, y in members) / k) if k else None,
        })

    sweep: list[dict] = []
    for t in _SWEEP_THRESHOLDS:
        picked = [(p, y) for _, p, y in rows if max(p, 1 - p) >= t]
        k = len(picked)
        acc = (
            sum(1 for p, y in picked if (p >= 0.5) == (y == 1)) / k
            if k else None
        )
        sweep.append({
            "threshold": t,
            "n": k,
            "accuracy": acc,
            "share": k / n if n else 0.0,
        })

    dates = sorted(d for d, _, _ in rows)
    return {
        "kind": "log",
        "n": n,
        "accuracy": correct / n,
        "log_loss": log_loss,
        "brier": brier,
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
        "calibration": bins,
        "sweep": sweep,
        "summary": None,
        "note": None,
    }


def _pick(d: dict, *keys: str) -> float | None:
    """First finite numeric value among keys, else None."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and math.isfinite(v):
            return float(v)
    return None


def _summary_report(metrics: dict) -> dict:
    """Normalize an aggregate metrics/backtest dict to top-line n/acc/log_loss.

    Handles three persisted shapes: a flat elo summary (nfl/mlb/ncaaf), a
    nested ``{"overall": {...}, "2024": {...}}`` breakdown (ncaab), and a
    per-tour ``{"ATP": {...}, "WTA": {...}}`` map (tennis). The raw dict rides
    along under ``summary`` so the UI can show the breakdown verbatim.
    """
    # Nested "overall" wins as the headline; otherwise treat metrics as flat.
    head = metrics.get("overall") if isinstance(metrics.get("overall"), dict) else metrics
    n = _pick(head, "n", "n_games", "games")
    accuracy = _pick(head, "accuracy", "elo_accuracy")
    log_loss = _pick(head, "log_loss", "elo_log_loss")

    # Per-tour shape (tennis): no flat metrics — aggregate the sub-dicts.
    if accuracy is None and any(isinstance(v, dict) for v in metrics.values()):
        subs = [v for v in metrics.values() if isinstance(v, dict)]
        weighted = [
            (_pick(s, "n", "n_games", "games") or 0.0,
             _pick(s, "accuracy", "elo_accuracy"),
             _pick(s, "log_loss", "elo_log_loss"))
            for s in subs
        ]
        tot = sum(w for w, _, _ in weighted)
        if tot > 0:
            n = tot
            accs = [(w, a) for w, a, _ in weighted if a is not None]
            lls = [(w, ll) for w, _, ll in weighted if ll is not None]
            accuracy = sum(w * a for w, a in accs) / sum(w for w, _ in accs) if accs else None
            log_loss = sum(w * ll for w, ll in lls) / sum(w for w, _ in lls) if lls else None

    return {
        "kind": "summary",
        "n": int(n) if n is not None else None,
        "accuracy": accuracy,
        "log_loss": log_loss,
        "brier": None,
        "start": None,
        "end": None,
        "calibration": None,
        "sweep": None,
        "summary": metrics,
        "note": "Aggregate walk-forward metrics only — no per-game log persisted "
                "for a calibration curve.",
    }


def _empty_report() -> dict:
    return {
        "kind": "none",
        "n": None,
        "accuracy": None,
        "log_loss": None,
        "brier": None,
        "start": None,
        "end": None,
        "calibration": None,
        "sweep": None,
        "summary": None,
        "note": "This model persists no walk-forward backtest. It rebuilds live "
                "and is graded going forward on the Performance page.",
    }


def report(sport: Sport, model: str | None = None) -> dict:
    """The backtest report for one sport — always the same envelope shape.

    ``kind`` is "log" (per-game calibration + sweep), "summary" (aggregate
    metrics only) or "none" (nothing persisted). Every report carries
    ``sport`` and ``model`` so the caller needn't join anything.
    """
    state = _load_state(sport)
    if state is None:
        body = _empty_report()
    else:
        rows = _clean_rows(state.get("backtest"))
        if rows:
            body = _log_report(rows)
        else:
            metrics = next(
                (state[k] for k in ("metrics", "backtest")
                 if isinstance(state.get(k), dict) and state.get(k)),
                None,
            )
            body = _summary_report(metrics) if metrics else _empty_report()
        # Prefer the engine-supplied model label; fall back to the state file's.
        state_model = state.get("model")
        if model is None and isinstance(state_model, str):
            model = state_model
    return {"sport": sport.value, "model": model, **body}

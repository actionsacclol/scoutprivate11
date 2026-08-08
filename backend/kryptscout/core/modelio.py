"""Version stamping for pickled model bundles.

Pickled estimators are only guaranteed to behave correctly under the library
version that wrote them. Loading one under a different version does NOT raise
— scikit-learn emits `InconsistentVersionWarning` ("might lead to breaking
code or invalid results") and carries on, so a version drift silently changes
what the model predicts. On a board that prices real money, a quietly wrong
probability is worse than no model at all.

So every bundle carries the MAJOR.MINOR of the libraries whose objects it
contains, and the loaders reject a bundle that doesn't match the running
stack. A rejected bundle is not an error: both engines fall back to their Elo
baseline (honest and documented) and retrain the pickle on the next refresh —
which is deterministic and takes seconds. The artifact is a cache, never a
source of truth.

MAJOR.MINOR, not the full version: patch releases don't change pickle
semantics, and retraining on every patch bump would be pure churn.

The best fix is to not pickle estimators at all — store fitted coefficients
and apply them directly (see nba/nfl `_apply_platt`, two floats instead of a
LogisticRegression). This module covers what's left: the xgboost boosters,
which have no such reduction.
"""
from __future__ import annotations

from typing import Any

STAMP_KEY = "_lib_versions"

# Libraries whose pickled objects appear in a bundle. Import errors are
# tolerated (a stack without one simply contributes no entry) so this can
# never be the reason a model fails to load.
_TRACKED = ("sklearn", "xgboost", "numpy")


def _major_minor(version: str) -> str:
    parts = str(version).split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else str(version)


def library_versions() -> dict[str, str]:
    """{library: "MAJOR.MINOR"} for the running stack."""
    import importlib

    out: dict[str, str] = {}
    for name in _TRACKED:
        try:
            module = importlib.import_module(name)
        except Exception:  # noqa: BLE001 — absent library, not an error here
            continue
        version = getattr(module, "__version__", None)
        if version:
            out[name] = _major_minor(version)
    return out


def stamp(bundle: dict[str, Any]) -> dict[str, Any]:
    """Record the writing stack's versions on a bundle (mutates + returns it)."""
    bundle[STAMP_KEY] = library_versions()
    return bundle


def matches(bundle: Any) -> bool:
    """True when `bundle` was written by a compatible stack.

    An UNSTAMPED bundle returns False: it predates stamping, so its provenance
    is unknown and the only safe reading is "retrain it". Only libraries
    present in BOTH the stamp and the running stack are compared, so adding a
    tracked library later can't invalidate every existing artifact.
    """
    if not isinstance(bundle, dict):
        return False
    stamped = bundle.get(STAMP_KEY)
    if not isinstance(stamped, dict) or not stamped:
        return False
    running = library_versions()
    shared = set(stamped) & set(running)
    if not shared:
        return False
    return all(stamped[name] == running[name] for name in shared)

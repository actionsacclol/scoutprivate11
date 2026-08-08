"""Model-bundle version stamping (core/modelio) + the NFL Platt reduction.

A pickled estimator only behaves correctly under the library version that
wrote it, and MODELS_DIR pickles live in userData — they outlive an app
update that bumps the bundled sklearn/xgboost. These tests pin the safety
net: a foreign bundle is rejected (Elo fallback) and reported stale so the
next refresh retrains it, rather than silently mispredicting.
"""
from __future__ import annotations

import numpy as np
import pytest

from kryptscout.core import modelio
from kryptscout.sports import nfl


def test_stamp_round_trips_and_matches_the_running_stack():
    bundle = modelio.stamp({"model": object()})
    assert bundle[modelio.STAMP_KEY] == modelio.library_versions()
    assert modelio.matches(bundle)


def test_unstamped_bundle_is_rejected():
    """Provenance unknown — the only safe reading is 'retrain it'."""
    assert not modelio.matches({"model": object()})
    assert not modelio.matches({"model": object(), modelio.STAMP_KEY: {}})
    assert not modelio.matches("not a bundle")


def test_foreign_versions_are_rejected():
    bundle = modelio.stamp({"model": object()})
    bundle[modelio.STAMP_KEY] = {**bundle[modelio.STAMP_KEY], "sklearn": "0.1"}
    assert not modelio.matches(bundle)


def test_only_shared_libraries_are_compared():
    """A stamp naming a library this stack doesn't have must not invalidate
    the bundle — otherwise adding a tracked library later would orphan every
    existing artifact."""
    bundle = modelio.stamp({"model": object()})
    bundle[modelio.STAMP_KEY] = {
        **bundle[modelio.STAMP_KEY], "some-future-lib": "9.9",
    }
    assert modelio.matches(bundle)


def test_versions_are_major_minor_so_patch_bumps_dont_churn():
    for version in modelio.library_versions().values():
        assert version.count(".") <= 1


def test_nfl_platt_matches_a_fitted_logistic_regression():
    """The stored (a, b) must reproduce sklearn's predict_proba exactly — that
    equality is the whole justification for not pickling the estimator."""
    from sklearn.linear_model import LogisticRegression

    rng = np.random.default_rng(7)
    margins = rng.normal(0, 2, size=4000)
    y = (rng.random(4000) < 1.0 / (1.0 + np.exp(-margins))).astype(int)
    lr = LogisticRegression(C=1e6, max_iter=1000).fit(margins.reshape(-1, 1), y)
    platt = (float(lr.coef_[0][0]), float(lr.intercept_[0]))

    expected = lr.predict_proba(margins.reshape(-1, 1))[:, 1]
    assert np.array_equal(nfl._apply_platt(margins, platt), expected)


def test_nfl_platt_upcasts_float32_margins():
    """Margins arrive as xgboost float32; accumulating in float32 would shift
    every probability ~5e-8 against the estimator this replaced."""
    margins = np.linspace(-8, 8, 500, dtype=np.float32)
    out = nfl._apply_platt(margins, (1.0187265544405852, 0.07666672106203597))
    assert out.dtype == np.float64


def test_nfl_rejects_a_bundle_written_by_a_foreign_stack(tmp_path):
    import pickle

    path = tmp_path / "nfl_xgb_v3.pkl"
    bundle = modelio.stamp({
        "model": object(), "platt": (1.0, 0.0), "feature_columns": ["a"],
    })
    bundle[modelio.STAMP_KEY] = {**bundle[modelio.STAMP_KEY], "xgboost": "0.1"}
    path.write_bytes(pickle.dumps(bundle))

    assert nfl._load_model(path) is None
    # ...and reported stale, so refresh_data() retrains it. Without this a
    # version-mismatched pickle stays fresh by mtime and Elo is permanent.
    assert nfl._model_stale(path) is True


def test_nfl_accepts_a_bundle_from_this_stack(tmp_path):
    import pickle

    path = tmp_path / "nfl_xgb_v3.pkl"
    path.write_bytes(pickle.dumps(modelio.stamp({
        "model": object(), "platt": (1.0, 0.0), "feature_columns": ["a"],
    })))
    assert nfl._load_model(path) is not None
    assert nfl._model_stale(path) is False

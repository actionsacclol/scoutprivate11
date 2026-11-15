"""Make the kryptscout package importable when pytest runs from anywhere."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(autouse=True)
def _licensed(monkeypatch):
    """Every test runs as if licensed, so the app-wide license gate (API
    middleware + engine live-bet check) doesn't 402/skip unrelated tests. The
    licensing tests explicitly override this to False to exercise the gate."""
    from kryptscout import license
    monkeypatch.setattr(license, "is_licensed", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def _no_cached_forecasts():
    """datasources.weather caches forecasts per venue and day; a test that
    fakes one must not be answered with the previous test's."""
    from kryptscout.datasources import weather
    weather.clear_cache()
    yield
    weather.clear_cache()


@pytest.fixture(autouse=True)
def _scratch_models_dir(tmp_path, monkeypatch):
    """Point config.MODELS_DIR at a scratch directory.

    api.main deletes the artifacts of retired engines once the registry has
    loaded (jobs.refresh.purge_retired_artifacts). A test that exercises that
    path must not reach the developer's real models/ directory.
    """
    from kryptscout import config
    scratch = tmp_path / "_models"
    scratch.mkdir()
    monkeypatch.setattr(config, "MODELS_DIR", scratch)


@pytest.fixture(autouse=True)
def _engines_ready():
    """Every test runs as if the sport engines finished loading.

    api.main loads them on a worker thread and the engine-backed endpoints
    await that (see main._await_engines). Every API test blanks
    app.router.on_startup so the real sport modules are never imported — which
    also means the readiness Event is never set, and each such endpoint would
    block for ENGINE_WARMUP_TIMEOUT_S instead of answering. Declaring the
    registry ready here keeps that trap out of every fixture; the warm-up tests
    clear it explicitly to exercise the wait.
    """
    from kryptscout.api import main
    main._engines_ready.set()
    yield
    main._engines_ready.clear()

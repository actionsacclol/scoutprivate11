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

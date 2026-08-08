"""Paths and environment configuration.

When the Electron supervisor launches the packaged (PyInstaller) backend it
sets KRYPT_SCOUT_USERDATA to the app's userData directory; data/ and models/
must live there — the frozen bundle's own tree is a read-only (and for
onefile, temporary) unpack dir. Without the env var (dev, tests, CLI jobs)
paths stay exactly as before: <repo>/data and <repo>/models.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent

_USERDATA = os.environ.get("KRYPT_SCOUT_USERDATA", "").strip()
_BASE_DIR = Path(_USERDATA) if _USERDATA else PROJECT_DIR

DATA_DIR = _BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
MODELS_DIR = _BASE_DIR / "models"
# Backend log file lives alongside data/ under the Electron userData dir (the
# supervisor also creates userData/logs before launch); logsetup writes
# backend.log here so a full crash/startup history survives past the Electron
# in-memory ring buffer and can be exported for support.
LOG_DIR = _BASE_DIR / "logs"

for d in (DATA_DIR, RAW_DIR, MODELS_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

load_dotenv(PROJECT_DIR / ".env")

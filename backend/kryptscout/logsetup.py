"""Central logging setup for the backend.

Everything the backend prints already reaches the Electron supervisor over the
child's stdout/stderr pipes (python-backend.ts fans it into an in-memory ring
buffer). That ring is capped and dies with the app, so a "backend unreachable"
report that needs the *startup* history — the part that scrolled off, or the
crash that happened before the window opened — has nowhere to come from.

`setup_logging()` adds a rotating file sink at LOG_DIR/backend.log so the full
history survives on disk and can be exported for support (see the Electron
backend:exportLogs handler). It's called at import of kryptscout.api.main, so
both the dev path (`python -m uvicorn kryptscout.api.main:app`) and the frozen
entry (run_backend.py, which imports the same module) get file logging without
any per-entry-point wiring. Console/stderr output is left untouched — uvicorn
still owns it and the supervisor still captures it.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from kryptscout.config import LOG_DIR

LOG_FILE = LOG_DIR / "backend.log"

# 2 MB × 3 backups ≈ the last ~8 MB of history — plenty to cover a startup
# crash loop or a day of capture sweeps, without unbounded disk growth.
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Sentinel on the root logger so a re-import / uvicorn reload never stacks a
# second file handler (which would double every line).
_MARKER = "_krypt_scout_file_log"

_configured = False


def setup_logging() -> None:
    """Attach a rotating backend.log sink to the root + kryptscout loggers.

    Idempotent: safe to call from multiple import sites and across a uvicorn
    reload. Level comes from KRYPT_SCOUT_LOG_LEVEL (default INFO) so support
    can ask a user to relaunch with KRYPT_SCOUT_LOG_LEVEL=DEBUG for a verbose
    capture without a code change.
    """
    global _configured
    root = logging.getLogger()
    if _configured or getattr(root, _MARKER, False):
        return

    level_name = os.environ.get("KRYPT_SCOUT_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    try:
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,  # don't open the file until the first record
        )
    except OSError:
        # A locked/unwritable logs dir must never stop the backend from
        # starting — the Electron ring buffer still captures stderr.
        return
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.setLevel(level)

    root.addHandler(handler)
    if root.level == logging.WARNING or root.level > level:
        # Default root level is WARNING; lower it so INFO records reach the
        # file handler. Never raise it above what the user asked for.
        root.setLevel(level)

    # Our own logger is the one the app writes to — make sure it emits at the
    # chosen level (it propagates to root, so no separate handler needed).
    logging.getLogger("kryptscout").setLevel(level)

    setattr(root, _MARKER, True)
    _configured = True

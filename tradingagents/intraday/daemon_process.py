"""Spawn and monitor the intraday daemon subprocess."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

from tradingagents.intraday.session import (
    HEARTBEAT_STALE_SECONDS,
    RUN_LOCK_PATH,
    is_daemon_alive,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DAEMON_LOG = RUN_LOCK_PATH.parent / "daemon.log"
_STARTUP_WAIT_SECONDS = 5.0
_POLL_INTERVAL = 0.25


def ensure_daemon_started() -> bool:
    """Start the intraday daemon if it is not already running."""
    if is_daemon_alive():
        return True

    RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, str(_PROJECT_ROOT / "scripts" / "intraday_daemon.py")]
    try:
        log_handle = open(_DAEMON_LOG, "a", encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot open daemon log %s: %s", _DAEMON_LOG, exc)
        return False

    try:
        subprocess.Popen(
            cmd,
            cwd=str(_PROJECT_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        logger.error("Failed to spawn intraday daemon: %s", exc)
        log_handle.close()
        return False

    deadline = time.monotonic() + _STARTUP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if is_daemon_alive():
            return True
        time.sleep(_POLL_INTERVAL)

    logger.warning("Daemon spawned but heartbeat not seen within %.0fs", _STARTUP_WAIT_SECONDS)
    return is_daemon_alive(stale_seconds=HEARTBEAT_STALE_SECONDS * 2)

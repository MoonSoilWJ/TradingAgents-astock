"""Persisted intraday monitoring session (start/stop from Web UI)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

_INTRADAY_HOME = Path(os.path.expanduser("~/.tradingagents/intraday"))
SESSION_PATH = _INTRADAY_HOME / "session.json"
RUN_LOCK_PATH = _INTRADAY_HOME / "run.lock"
HEARTBEAT_STALE_SECONDS = 90


def write_daemon_heartbeat() -> None:
    """Touch run.lock so the Web UI can detect a live daemon."""
    RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUN_LOCK_PATH.write_text(
        datetime.now().isoformat(timespec="seconds"),
        encoding="utf-8",
    )


def daemon_last_seen() -> datetime | None:
    if not RUN_LOCK_PATH.exists():
        return None
    try:
        raw = RUN_LOCK_PATH.read_text(encoding="utf-8").strip()
        return datetime.fromisoformat(raw)
    except (OSError, ValueError):
        return None


def is_daemon_alive(stale_seconds: int = HEARTBEAT_STALE_SECONDS) -> bool:
    seen = daemon_last_seen()
    if seen is None:
        return False
    return (datetime.now() - seen).total_seconds() <= stale_seconds


@dataclass
class IntradaySession:
    active: bool = False
    stop_requested: bool = False
    running: bool = False
    ticker: str = ""
    shares: int = 0
    total_capital: float = 100_000.0
    max_position_pct: float = 30.0
    dingtalk_webhook: str = ""
    full_run_done_date: str = ""
    runs_today: int = 0
    last_run_at: str = ""
    last_slot: str = ""
    last_action: str = ""
    trade_date: str = ""

    def reset_daily_if_needed(self) -> None:
        today = date.today().isoformat()
        if self.trade_date != today:
            self.trade_date = today
            self.runs_today = 0
            self.last_slot = ""
            self.full_run_done_date = ""


def load_session() -> IntradaySession:
    if not SESSION_PATH.exists():
        return IntradaySession()
    try:
        raw = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        return IntradaySession(**raw)
    except (json.JSONDecodeError, TypeError, OSError):
        return IntradaySession()


def save_session(session: IntradaySession) -> None:
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(
        json.dumps(asdict(session), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def request_start(
    *,
    ticker: str,
    shares: int,
    total_capital: float,
    max_position_pct: float,
    dingtalk_webhook: str = "",
) -> IntradaySession:
    session = load_session()
    if session.ticker != ticker:
        session.full_run_done_date = ""
        session.runs_today = 0
        session.last_slot = ""
    session.active = True
    session.stop_requested = False
    session.ticker = ticker
    session.shares = shares
    session.total_capital = total_capital
    session.max_position_pct = max_position_pct
    session.dingtalk_webhook = dingtalk_webhook.strip()
    session.reset_daily_if_needed()
    save_session(session)
    return session


def request_stop(hard: bool = True) -> IntradaySession:
    session = load_session()
    session.stop_requested = True
    session.active = False
    if hard:
        session.running = False
    save_session(session)
    return session


def mark_running(running: bool) -> None:
    session = load_session()
    session.running = running
    save_session(session)


def record_run(
    *,
    slot: str,
    action: str,
    full_run: bool,
) -> None:
    session = load_session()
    session.last_run_at = datetime.now().isoformat(timespec="seconds")
    session.last_slot = slot
    session.last_action = action
    session.runs_today += 1
    session.trade_date = date.today().isoformat()
    if full_run:
        session.full_run_done_date = date.today().isoformat()
    session.running = False
    save_session(session)


def record_skipped_slot(slot: str) -> None:
    """Mark a schedule slot as handled without running analysis."""
    session = load_session()
    session.last_run_at = datetime.now().isoformat(timespec="seconds")
    session.last_slot = slot
    session.last_action = "skipped"
    session.trade_date = date.today().isoformat()
    save_session(session)


def should_stop() -> bool:
    return load_session().stop_requested


def is_active() -> bool:
    s = load_session()
    return s.active and not s.stop_requested

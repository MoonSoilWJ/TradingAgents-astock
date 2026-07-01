"""Manage completed and incomplete analysis history."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from tradingagents.default_config import DEFAULT_CONFIG


_INCOMPLETE_TASKS_LOCK = threading.Lock()


def _tradingagents_home() -> Path:
    """Resolve the TradingAgents home directory.

    Honor TRADINGAGENTS_HOME if set (allows sandboxed runs to redirect
    state into a writable path), otherwise fall back to ~/.tradingagents.
    """
    override = os.getenv("TRADINGAGENTS_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".tradingagents"


def _incomplete_tasks_file() -> Path:
    return _tradingagents_home() / "incomplete_tasks.json"


def _results_dir() -> Path:
    # Prefer the configured results_dir (honors TRADINGAGENTS_RESULTS_DIR),
    # fall back to <home>/logs for default installs.
    configured = os.getenv("TRADINGAGENTS_RESULTS_DIR")
    if configured:
        return Path(configured).expanduser()
    return _tradingagents_home() / "logs"


def get_history() -> list[dict[str, str]]:
    """Scan saved analysis logs and return a sorted list (newest first).

    Each entry: {"ticker": "300750", "date": "2026-05-12", "path": "/abs/path/...json"}
    """
    root = _results_dir()
    if not root.exists():
        return []

    entries: list[dict[str, str]] = []
    for log_file in root.rglob("full_states_log_*.json"):
        match = re.search(r"full_states_log_(\d{4}-\d{2}-\d{2})\.json$", log_file.name)
        if not match:
            continue
        date = match.group(1)
        ticker = log_file.parent.parent.name
        entries.append({"ticker": ticker, "date": date, "path": str(log_file)})

    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries


def _completed_key(ticker: str, trade_date: str) -> tuple[str, str]:
    return ticker.upper(), trade_date


def _completed_keys() -> set[tuple[str, str]]:
    return {
        _completed_key(entry["ticker"], entry["date"])
        for entry in get_history()
    }


def _load_incomplete_index() -> list[dict[str, Any]]:
    target = _incomplete_tasks_file()
    if not target.exists():
        return []

    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(data, list):
        return []

    entries: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker", "")).strip().upper()
        trade_date = str(item.get("trade_date", "")).strip()
        if not ticker or not re.match(r"^\d{4}-\d{2}-\d{2}$", trade_date):
            continue
        item["ticker"] = ticker
        item["trade_date"] = trade_date
        entries.append(item)
    return entries


def _save_incomplete_index(entries: list[dict[str, Any]]) -> None:
    target = _incomplete_tasks_file()
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=parent,
        prefix=f"{target.stem}.",
        suffix=".tmp",
        delete=False,
    ) as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
        tmp = Path(f.name)
    tmp.replace(target)


def _checkpoint_step(ticker: str, trade_date: str) -> int | None:
    try:
        from tradingagents.graph.checkpointer import checkpoint_step

        return checkpoint_step(DEFAULT_CONFIG["data_cache_dir"], ticker, trade_date)
    except Exception:
        return None


def record_incomplete_task(
    ticker: str,
    trade_date: str,
    *,
    status: str,
    error: str | None = None,
    completed_stages: list[str] | None = None,
) -> None:
    """Upsert a resumable task entry."""
    ticker = ticker.strip().upper()
    trade_date = trade_date.strip()
    if not ticker or not trade_date:
        return

    with _INCOMPLETE_TASKS_LOCK:
        entries = [
            entry
            for entry in _load_incomplete_index()
            if _completed_key(entry["ticker"], entry["trade_date"])
            != _completed_key(ticker, trade_date)
        ]
        now = time.time()
        entries.append(
            {
                "ticker": ticker,
                "trade_date": trade_date,
                "status": status,
                "error": error or "",
                "completed_stages": completed_stages or [],
                "updated_at": now,
            }
        )
        entries.sort(key=lambda e: float(e.get("updated_at", 0)), reverse=True)
        _save_incomplete_index(entries)


def clear_incomplete_task(ticker: str, trade_date: str) -> None:
    """Remove an incomplete task once it completes successfully."""
    ticker = ticker.strip().upper()
    trade_date = trade_date.strip()
    with _INCOMPLETE_TASKS_LOCK:
        entries = [
            entry
            for entry in _load_incomplete_index()
            if _completed_key(entry["ticker"], entry["trade_date"])
            != _completed_key(ticker, trade_date)
        ]
        _save_incomplete_index(entries)


def get_incomplete_history() -> list[dict[str, Any]]:
    """Return unfinished tasks that can be resumed from their checkpoint."""
    completed = _completed_keys()
    active_entries: list[dict[str, Any]] = []

    with _INCOMPLETE_TASKS_LOCK:
        entries = _load_incomplete_index()
        for entry in entries:
            key = _completed_key(entry["ticker"], entry["trade_date"])
            if key in completed:
                continue

            step = _checkpoint_step(entry["ticker"], entry["trade_date"])
            entry["checkpoint_step"] = step
            active_entries.append(entry)

        active_entries.sort(key=lambda e: float(e.get("updated_at", 0)), reverse=True)
        if len(active_entries) != len(entries):
            _save_incomplete_index(active_entries)
    return active_entries


def load_analysis(path: str) -> dict[str, Any]:
    """Load a saved analysis JSON file."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def extract_signal(state: dict[str, Any]) -> str:
    """Extract the 5-tier portfolio rating from a final state dict."""
    import re

    from tradingagents.agents.utils.rating import parse_rating

    for field in (
        "final_trade_decision",
        "investment_plan",
        "trader_investment_decision",
    ):
        text = state.get(field, "")
        if not text:
            continue
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        rating = parse_rating(cleaned, default="")
        if rating:
            return rating
    return "N/A"

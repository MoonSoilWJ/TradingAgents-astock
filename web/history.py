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


def _ratings_need_persist(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """True when canonicalization added authoritative rating fields or markers."""
    from tradingagents.agents.utils.rating import normalize_rating_label

    for key in ("research_rating", "portfolio_rating"):
        if normalize_rating_label(after.get(key)) and not normalize_rating_label(before.get(key)):
            return True
    for text_key in ("investment_plan", "final_trade_decision"):
        after_text = str(after.get(text_key) or "")
        before_text = str(before.get(text_key) or "")
        if "<!-- TRADINGAGENTS_RATING:" in after_text and after_text != before_text:
            return True
    return False


def load_analysis(path: str) -> dict[str, Any]:
    """Load a saved analysis JSON file."""
    from tradingagents.agents.utils.rating import canonicalize_decision_ratings, normalize_rating_label

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    state = canonicalize_decision_ratings(dict(raw))
    if _ratings_need_persist(raw, state):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    return state


def _strip_think_tags(text: str) -> str:
    import re

    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)


def _extract_trader_action(state: dict[str, Any], field: str) -> str:
    """Parse the trader's Buy/Hold/Sell action, ignoring cited manager ratings."""
    import re

    from tradingagents.agents.utils.rating import (
        normalize_rating_label,
        parse_rating_from_header,
    )

    text = _strip_think_tags(str(state.get(field, "") or ""))
    if not text:
        return ""

    for pattern in (
        re.compile(r"\*\*Action\*\*\s*[：:\-]\s*\**(\w+)", re.IGNORECASE),
        re.compile(r"FINAL TRANSACTION PROPOSAL:\s*\**(\w+)", re.IGNORECASE),
        re.compile(
            r"\| \*\*操作\*\* \| \*\*(Buy|Hold|Sell)(?:[（(]|[^\w]|$)",
            re.IGNORECASE,
        ),
    ):
        match = pattern.search(text)
        if match:
            label = normalize_rating_label(match.group(1))
            if label:
                return label

    filtered = "\n".join(
        line for line in text.splitlines()
        if not re.search(r"研究经理|Research Manager", line, re.IGNORECASE)
    )
    parsed = parse_rating_from_header(filtered, default="")
    return normalize_rating_label(parsed) or ""


def extract_pm_immediate_action(text: str) -> str:
    """Return Buy/Sell when the PM section instructs an immediate trade."""
    import re

    from tradingagents.agents.utils.rating import normalize_rating_label

    cleaned = _strip_think_tags(str(text or ""))
    if not cleaned:
        return ""

    section = cleaned.split("交易指令", 1)[1] if "交易指令" in cleaned else cleaned
    head = section[:2000]

    if re.search(
        r"Buy[（(]买入|\*\*Buy\*\*|立即(?:执行|买入)|买入(?:指令|操作)",
        head,
        re.IGNORECASE,
    ):
        return "Buy"
    if re.search(
        r"Sell[（(]卖出|\*\*Sell\*\*|立即(?:卖出|清仓)|全部清仓",
        head,
        re.IGNORECASE,
    ):
        return "Sell"
    if re.search(r"持有不变|维持(?:现有|当前)仓位|暂不操作|no action", head, re.IGNORECASE):
        return "Hold"

    from tradingagents.agents.utils.rating import parse_rating_from_header

    parsed = parse_rating_from_header(head, default="")
    return normalize_rating_label(parsed) or ""


def extract_field_rating(state: dict[str, Any], field: str) -> str:
    """Parse a 5-tier rating from a single report field, or return empty string."""
    from tradingagents.agents.utils.rating import (
        extract_rating_marker,
        normalize_rating_label,
        parse_rating_from_header,
    )

    text = state.get(field, "")
    if not text:
        return ""
    cleaned = _strip_think_tags(str(text))
    marker = extract_rating_marker(cleaned)
    if marker:
        return marker
    parsed = parse_rating_from_header(cleaned, default="")
    return normalize_rating_label(parsed) or ""


def extract_stage_ratings(state: dict[str, Any]) -> dict[str, str]:
    """Return parsed ratings from each decision stage when present."""
    from tradingagents.agents.utils.rating import canonicalize_decision_ratings, normalize_rating_label

    canonicalize_decision_ratings(state)
    ratings: dict[str, str] = {}
    research = normalize_rating_label(state.get("research_rating")) or extract_field_rating(
        state, "investment_plan"
    )
    if research:
        ratings["research"] = research
    trader = _extract_trader_action(state, "trader_investment_plan") or _extract_trader_action(
        state, "trader_investment_decision"
    )
    if trader:
        ratings["trader"] = trader
    portfolio = normalize_rating_label(state.get("portfolio_rating")) or extract_field_rating(
        state, "final_trade_decision"
    )
    if portfolio:
        ratings["portfolio"] = portfolio
    return ratings


def extract_signal(state: dict[str, Any]) -> str:
    """Extract the portfolio-manager final rating for the top trading signal."""
    from tradingagents.agents.utils.rating import canonicalize_decision_ratings, normalize_rating_label

    canonicalize_decision_ratings(state)

    portfolio = normalize_rating_label(state.get("portfolio_rating"))
    if portfolio:
        return portfolio

    rating = extract_field_rating(state, "final_trade_decision")
    if rating:
        return rating

    risk = state.get("risk_debate_state")
    if isinstance(risk, dict):
        judge = str(risk.get("judge_decision") or "")
        if judge.strip():
            rating = extract_field_rating({"_judge": judge}, "_judge")
            if rating:
                return rating

    return "N/A"


def resolve_report_signal(state: dict[str, Any], fallback: str = "") -> str:
    """Resolve the signal shown in the report, with a safe fallback."""
    from tradingagents.agents.utils.rating import normalize_rating_label

    resolved = extract_signal(state)
    if not resolved or resolved == "N/A":
        resolved = normalize_rating_label(fallback) or (fallback.strip() if fallback else "")
    return resolved or "N/A"

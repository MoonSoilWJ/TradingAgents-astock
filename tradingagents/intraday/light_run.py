"""Lightweight intraday run using cached full-day analyst reports."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from tradingagents.graph.state_json import persistable_graph_state
from tradingagents.agents.managers.intraday_pm import create_intraday_portfolio_manager
from tradingagents.agents.schemas import IntradayDecision, IntradayAction
from tradingagents.default_config import DEFAULT_CONFIG

logger = logging.getLogger(__name__)


def _log_path(ticker: str, trade_date: str, results_dir: str) -> Path:
    return (
        Path(results_dir)
        / ticker
        / "TradingAgentsStrategy_logs"
        / f"full_states_log_{trade_date}.json"
    )


def save_cached_state(
    ticker: str,
    state: dict[str, Any],
    trade_date: str | None = None,
    results_dir: str | None = None,
) -> Path:
    """Persist merged analyst state after a light intraday PM refresh."""
    trade_date = trade_date or date.today().isoformat()
    results_dir = results_dir or DEFAULT_CONFIG["results_dir"]
    path = _log_path(ticker, trade_date, results_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(persistable_graph_state(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_cached_state(ticker: str, trade_date: str | None = None, results_dir: str | None = None) -> dict[str, Any] | None:
    trade_date = trade_date or date.today().isoformat()
    results_dir = results_dir or DEFAULT_CONFIG["results_dir"]
    path = _log_path(ticker, trade_date, results_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load cached state %s: %s", path, exc)
        return None


def run_light_intraday_pm(
    state: dict[str, Any],
    llm: Any,
) -> dict[str, Any]:
    """Re-run intraday PM only on an existing full analysis state."""
    node = create_intraday_portfolio_manager(llm)
    return node(state)

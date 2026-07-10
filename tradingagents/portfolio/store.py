"""Persistent portfolio state for intraday monitoring."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

_INTRADAY_HOME = Path(os.path.expanduser("~/.tradingagents/intraday"))


@dataclass
class PortfolioState:
    ticker: str = ""
    shares: int = 0
    avg_cost: float = 0.0
    cash: float = 0.0
    total_capital: float = 100_000.0
    max_position_pct: float = 30.0
    bought_today: int = 0
    trade_date: str = ""
    settlement: str = "T1"  # T0 or T1

    def market_value(self, price: float) -> float:
        return self.shares * price

    def total_equity(self, price: float) -> float:
        return self.cash + self.market_value(price)

    def sellable_shares(self) -> int:
        if self.settlement == "T0":
            return self.shares
        return max(0, self.shares - self.bought_today)


class PortfolioStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (_INTRADAY_HOME / "portfolio.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self, ticker: str) -> PortfolioState | None:
        data = self._read()
        raw = data.get(ticker)
        if not raw:
            return None
        return PortfolioState(**raw)

    def save(self, state: PortfolioState) -> None:
        data = self._read()
        data[state.ticker] = asdict(state)
        self._write(data)

    def init(
        self,
        ticker: str,
        *,
        shares: int,
        total_capital: float,
        max_position_pct: float,
        settlement: str,
        avg_cost: float = 0.0,
    ) -> PortfolioState:
        today = date.today().isoformat()
        cost_basis = max(0.0, avg_cost) * shares
        cash = max(0.0, total_capital - cost_basis)
        state = PortfolioState(
            ticker=ticker,
            shares=shares,
            avg_cost=max(0.0, avg_cost) if shares > 0 else 0.0,
            cash=cash if shares > 0 else total_capital,
            total_capital=total_capital,
            max_position_pct=max_position_pct,
            bought_today=0,
            trade_date=today,
            settlement=settlement,
        )
        self.save(state)
        return state

    def rollover_if_new_day(self, state: PortfolioState) -> PortfolioState:
        today = date.today().isoformat()
        if state.trade_date != today:
            state.trade_date = today
            state.bought_today = 0
            self.save(state)
        return state

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

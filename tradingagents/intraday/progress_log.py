"""Log pipeline stage progress and full debate reports for intraday daemon runs."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from web.progress import PIPELINE_STAGES

logger = logging.getLogger("intraday_daemon")

_ANALYST_KEYS = [s["report_key"] for s in PIPELINE_STAGES[:7]]
_STAGE_BY_REPORT = {s["report_key"]: s for s in PIPELINE_STAGES if s.get("report_key")}

_RUNS_DIR = Path(os.path.expanduser("~/.tradingagents/intraday/runs"))


def _strip_think_tags(text: str) -> str:
    return re.sub(
        r"<think>.*?</think>\s*",
        "",
        text,
        flags=re.DOTALL,
    ).strip()


def _preview(text: str, limit: int = 400) -> str:
    clean = _strip_think_tags(text).replace("\n", " ")
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


class IntradayRunLogger:
    """Write stage milestones to daemon.log and full text to a per-run markdown file."""

    def __init__(self, ticker: str, trade_date: str, slot: str, *, mode: str = "full") -> None:
        self.ticker = ticker
        self.trade_date = trade_date
        self.slot = slot
        self.mode = mode
        self._done: set[str] = set()
        _RUNS_DIR.mkdir(parents=True, exist_ok=True)
        safe_slot = slot.replace(":", "")
        self.path = _RUNS_DIR / f"{ticker}_{trade_date}_{safe_slot}.md"
        self._fh = self.path.open("w", encoding="utf-8")
        mode_label = "轻量" if mode == "light" else "完整"
        header = (
            f"# Intraday run {ticker} · {trade_date} · slot {slot} ({mode_label})\n\n"
            f"Started: {datetime.now().isoformat(timespec='seconds')}\n\n"
        )
        self._fh.write(header)
        self._fh.flush()
        logger.info("Run report file: %s", self.path)

    def close(self) -> None:
        if self._fh.closed:
            return
        self._fh.write(f"\n---\nFinished: {datetime.now().isoformat(timespec='seconds')}\n")
        self._fh.flush()
        self._fh.close()
        logger.info("Run report saved: %s (%d stages)", self.path, len(self._done))

    def _mark(self, stage_id: str, title: str, body: str) -> None:
        if stage_id in self._done or not body.strip():
            return
        self._done.add(stage_id)
        preview = _preview(body)
        logger.info("[%s] %s — %s", self.ticker, title, preview)
        if len(_strip_think_tags(body)) > len(preview):
            logger.info("[%s]   (full text in %s)", self.ticker, self.path.name)
        self._fh.write(f"## {title}\n\n{_strip_think_tags(body)}\n\n")
        self._fh.flush()

    def log_state(self, state: dict[str, Any]) -> None:
        """Record all completed stages present in a state snapshot."""
        self.on_chunk(state)

    def on_chunk(self, chunk: dict[str, Any]) -> None:
        """Inspect a streamed graph chunk and log newly completed stages."""
        for key in _ANALYST_KEYS:
            content = chunk.get(key, "")
            if not content:
                continue
            meta = _STAGE_BY_REPORT.get(key)
            if meta:
                self._mark(meta["id"], meta["name"], str(content))

        dqs = chunk.get("data_quality_summary", "")
        if dqs:
            self._mark("quality_gate", "质量门控", str(dqs))

        investment_plan = chunk.get("investment_plan", "")
        debate = chunk.get("investment_debate_state")
        if isinstance(debate, dict):
            bull = str(debate.get("bull_history") or "")
            bear = str(debate.get("bear_history") or "")
            if bull:
                self._mark("debate_bull", "多头研究员", bull)
            if bear:
                self._mark("debate_bear", "空头研究员", bear)
        if investment_plan:
            self._mark("debate", "研究经理投资计划", str(investment_plan))
        elif isinstance(debate, dict) and debate.get("judge_decision"):
            self._mark("debate", "研究经理投资计划", str(debate["judge_decision"]))

        trader_plan = chunk.get("trader_investment_plan", "")
        if trader_plan:
            self._mark("trader", "交易决策", str(trader_plan))

        risk = chunk.get("risk_debate_state")
        if isinstance(risk, dict):
            for key, title in (
                ("aggressive_history", "激进风控"),
                ("conservative_history", "保守风控"),
                ("neutral_history", "中性风控"),
            ):
                part = str(risk.get(key) or "")
                if part:
                    self._mark(f"risk_{key}", title, part)
            risk_text = risk.get("judge_decision") or ""
            if risk_text:
                self._mark("risk", "风控裁决 / 盘中PM输入", str(risk_text))

        final = chunk.get("final_trade_decision", "")
        if final:
            self._mark("pm_decision", "盘中最终决策", str(final))

        action = chunk.get("intraday_action")
        qty = chunk.get("intraday_quantity")
        if action:
            reason = chunk.get("intraday_reason", "")
            self._mark(
                "pm_order",
                "盘中订单",
                f"action={action} quantity={qty}\n{reason}",
            )

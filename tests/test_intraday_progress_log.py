"""Tests for intraday run progress logging."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tradingagents.intraday.progress_log import IntradayRunLogger


class TestIntradayRunLogger(unittest.TestCase):
    def test_logs_analyst_and_debate_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            with patch("tradingagents.intraday.progress_log._RUNS_DIR", runs_dir):
                logger = IntradayRunLogger("512660", "2026-07-10", "14:25")
                try:
                    logger.on_chunk(
                        {
                            "market_report": "均线多头排列",
                            "investment_debate_state": {
                                "bull_history": "看多军工",
                                "bear_history": "估值偏高",
                            },
                            "investment_plan": "偏多，关注量能",
                            "trader_investment_plan": "轻仓试探",
                            "risk_debate_state": {
                                "aggressive_history": "可加仓",
                                "judge_decision": "风险可控",
                            },
                            "final_trade_decision": "持有",
                            "intraday_action": "hold",
                            "intraday_quantity": 0,
                            "intraday_reason": "趋势未破",
                        }
                    )
                finally:
                    logger.close()

                text = logger.path.read_text(encoding="utf-8")
                self.assertIn("技术分析", text)
                self.assertIn("均线多头排列", text)
                self.assertIn("研究经理投资计划", text)
                self.assertIn("风控裁决", text)
                self.assertIn("盘中订单", text)


if __name__ == "__main__":
    unittest.main()

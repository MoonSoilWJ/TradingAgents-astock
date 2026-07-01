"""Tests for A-share stock vs ETF instrument classification."""

import unittest

from tradingagents.dataflows.instrument import (
    ETF_ANALYSTS,
    STOCK_ANALYSTS,
    analysts_for_ticker,
    classify_astock_instrument,
    etf_skip_report,
)
from tradingagents.agents.utils.agent_utils import build_instrument_context
from tradingagents.graph.propagation import Propagator


class InstrumentClassificationTests(unittest.TestCase):
    def test_stock_codes(self):
        for code in ("603936", "688981", "000001", "300750"):
            self.assertEqual(classify_astock_instrument(code), "stock")

    def test_etf_codes(self):
        for code in ("589020", "588000", "510300", "159915"):
            self.assertEqual(classify_astock_instrument(code), "etf")

    def test_analyst_pipeline_differs(self):
        stock = analysts_for_ticker("603936")
        etf = analysts_for_ticker("589020")
        self.assertEqual(stock, list(STOCK_ANALYSTS))
        self.assertEqual(etf, list(ETF_ANALYSTS))
        self.assertNotIn("fundamentals", etf)
        self.assertNotIn("lockup", etf)

    def test_etf_skip_report_is_substantive(self):
        report = etf_skip_report("fundamentals")
        self.assertIn("ETF", report)
        self.assertGreater(len(report), 200)

    def test_build_instrument_context_etf(self):
        ctx = build_instrument_context("589020", "etf")
        self.assertIn("ETF", ctx)
        self.assertIn("589020", ctx)

    def test_initial_state_prefills_etf_skips(self):
        state = Propagator().create_initial_state("589020", "2026-07-01")
        self.assertEqual(state["instrument_type"], "etf")
        self.assertIn("ETF 分析模式", state["fundamentals_report"])
        self.assertIn("ETF 分析模式", state["lockup_report"])


if __name__ == "__main__":
    unittest.main()

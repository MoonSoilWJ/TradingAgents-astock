"""Tests for A-share stock vs ETF instrument classification."""

import unittest

from tradingagents.dataflows.instrument import (
    ETF_ANALYSTS,
    STOCK_ANALYSTS,
    analysts_for_ticker,
    classify_astock_instrument,
    etf_skip_report,
    settlement_rule,
)
from tradingagents.agents.utils.agent_utils import build_instrument_context
from tradingagents.graph.propagation import Propagator


class InstrumentClassificationTests(unittest.TestCase):
    def test_stock_codes(self):
        for code in ("603936", "688981", "000001", "300750"):
            self.assertEqual(classify_astock_instrument(code), "stock")

    def test_etf_codes(self):
        for code in ("589020", "588000", "510300", "159915", "517400", "563000"):
            self.assertEqual(classify_astock_instrument(code), "etf")

    def test_is_on_exchange_etf_code(self):
        from tradingagents.dataflows.instrument import is_on_exchange_etf_code

        self.assertTrue(is_on_exchange_etf_code("517400"))
        self.assertTrue(is_on_exchange_etf_code("159570"))
        self.assertTrue(is_on_exchange_etf_code("563000"))
        self.assertFalse(is_on_exchange_etf_code("603936"))

    def test_is_listed_astock_code(self):
        from tradingagents.dataflows.instrument import is_listed_astock_code

        self.assertTrue(is_listed_astock_code("603936"))
        self.assertTrue(is_listed_astock_code("517400"))
        self.assertTrue(is_listed_astock_code("159915"))
        self.assertFalse(is_listed_astock_code("999999"))

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

    def test_settlement_rule_stock_is_t1(self):
        self.assertEqual(settlement_rule("603936"), "T1")

    def test_settlement_rule_equity_etf_is_t1(self):
        for code in ("517400", "510300", "159915", "588000"):
            self.assertEqual(settlement_rule(code), "T1", code)

    def test_settlement_rule_cross_border_etf_is_t0(self):
        for code in ("513100", "513180", "159920", "513600"):
            self.assertEqual(settlement_rule(code), "T0", code)

    def test_settlement_rule_t0_by_name_keyword(self):
        # 159xxx equity ETFs are T+1 unless the name marks cross-border / HK indices.
        self.assertEqual(settlement_rule("159999", "纳指ETF"), "T0")
        self.assertEqual(settlement_rule("159999", "港股通科技ETF"), "T0")
        self.assertEqual(settlement_rule("159999", "恒生ETF"), "T0")
        self.assertEqual(settlement_rule("159999", "创业板ETF"), "T1")

    def test_commodity_lof_is_t0(self):
        for code, name in (
            ("501018", "南方原油"),
            ("161129", "原油基金"),
            ("162411", "原油基金"),
            ("161125", "标普中国新机会"),
            ("159985", "豆粕ETF"),
        ):
            with self.subTest(code=code):
                self.assertEqual(classify_astock_instrument(code), "etf")
                self.assertEqual(settlement_rule(code, name), "T0")

    def test_gold_stock_etf_stays_t1(self):
        self.assertEqual(settlement_rule("517400", "黄金股票"), "T1")

    def test_build_instrument_context_t0_lof(self):
        ctx = build_instrument_context("161129", "etf")
        self.assertIn("T+0", ctx)
        self.assertIn("161129", ctx)


if __name__ == "__main__":
    unittest.main()

"""Tests for intraday portfolio ledger and order normalization."""

import unittest

from tradingagents.agents.schemas import IntradayAction, IntradayDecision, render_intraday_decision
from tradingagents.portfolio.executor import apply_intraday_order, normalize_order
from tradingagents.portfolio.lot import lot_size_for_code
from tradingagents.portfolio.store import PortfolioState, PortfolioStore
from tradingagents.dataflows.instrument import settlement_rule


class PortfolioExecutorTests(unittest.TestCase):
    def _state(self, **kwargs) -> PortfolioState:
        base = dict(
            ticker="517400",
            shares=0,
            cash=100_000.0,
            total_capital=100_000.0,
            max_position_pct=30.0,
            bought_today=0,
            settlement="T1",
        )
        base.update(kwargs)
        return PortfolioState(**base)

    def test_lot_size_main_board_etf(self):
        self.assertEqual(lot_size_for_code("517400"), 100)

    def test_settlement_gold_etf_t1(self):
        self.assertEqual(settlement_rule("517400", "黄金股票"), "T1")

    def test_settlement_nasdaq_etf_t0(self):
        self.assertEqual(settlement_rule("513100"), "T0")

    def test_normalize_buy_respects_lot(self):
        state = self._state()
        decision = IntradayDecision(
            action=IntradayAction.BUY,
            quantity_shares=150,
            reason="test",
        )
        out = normalize_order(decision, state, price=1.0)
        self.assertEqual(out.quantity_shares, 100)

    def test_t1_blocks_same_day_sell(self):
        state = self._state(shares=200, bought_today=200, cash=50_000)
        decision = IntradayDecision(
            action=IntradayAction.SELL,
            quantity_shares=200,
            reason="test",
        )
        out = normalize_order(decision, state, price=1.0)
        self.assertEqual(out.action, IntradayAction.HOLD)

    def test_apply_buy_books_t1(self):
        state = self._state()
        decision = IntradayDecision(
            action=IntradayAction.BUY,
            quantity_shares=100,
            reason="test",
        )
        new_state, applied = apply_intraday_order(
            decision, state, price=1.0, book_trade=True
        )
        self.assertEqual(applied.action, "buy")
        self.assertEqual(new_state.shares, 100)
        self.assertEqual(new_state.bought_today, 100)
        self.assertEqual(new_state.cash, 99_900.0)

    def test_apply_hold_no_book_outside_session(self):
        state = self._state(shares=100, cash=90_000)
        decision = IntradayDecision(action=IntradayAction.HOLD, quantity_shares=0, reason="wait")
        _, applied = apply_intraday_order(
            decision, state, price=1.0, book_trade=False
        )
        self.assertFalse(applied.booked)
        self.assertEqual(applied.action, "hold")

    def test_render_intraday_decision_hold(self):
        md = render_intraday_decision(
            IntradayDecision(action=IntradayAction.HOLD, quantity_shares=0, reason="观望")
        )
        self.assertIn("不动", md)
        self.assertIn("TRADINGAGENTS_INTRADAY: hold:0", md)

    def test_portfolio_init_with_existing_shares(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = PortfolioStore(path=Path(tmp) / "portfolio.json")
            state = store.init(
                "517400",
                shares=200,
                total_capital=100_000,
                max_position_pct=30,
                settlement="T1",
                avg_cost=1.25,
            )
            self.assertEqual(state.shares, 200)
            self.assertEqual(state.avg_cost, 1.25)
            self.assertEqual(state.cash, 99_750.0)


if __name__ == "__main__":
    unittest.main()

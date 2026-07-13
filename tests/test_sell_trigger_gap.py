"""卖出触发：跳空穿越止损/追踪价时按开盘价成交。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backtest_top1_intraday import _fill_on_touch, check_sell_trigger


def _bar(dt: str, o: float, h: float, l: float, c: float) -> dict:
    return {
        "datetime": dt,
        "day": dt.split(" ")[0],
        "time": dt.split(" ")[1],
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": 1.0,
    }


class TestFillOnTouch(unittest.TestCase):
    def test_gap_down_uses_open(self):
        bar = _bar("2026-07-13 09:35:00", 1.33, 1.37, 1.32, 1.35)
        self.assertEqual(_fill_on_touch(bar, 1.468), 1.33)

    def test_intraday_touch_uses_trigger(self):
        bar = _bar("2026-07-10 09:35:00", 1.48, 1.49, 1.46, 1.47)
        self.assertEqual(_fill_on_touch(bar, 1.468), 1.468)


class TestCheckSellTriggerGap(unittest.TestCase):
    def test_stop_loss_gap_down(self):
        buy = 1.4754
        bars = [_bar("2026-07-13 09:35:00", 1.33, 1.37, 1.32, 1.35)]
        price, reason, _ = check_sell_trigger(bars, buy, 0, -0.5, 3.0, 0.5)
        self.assertEqual(reason, "止损")
        self.assertEqual(price, 1.33)

    def test_stop_loss_no_gap(self):
        buy = 1.0
        stop = 0.995
        bars = [_bar("2026-07-10 09:35:00", 1.00, 1.01, 0.994, 0.996)]
        price, reason, _ = check_sell_trigger(bars, buy, 0, -0.5, 3.0, 0.5)
        self.assertEqual(reason, "止损")
        self.assertEqual(price, stop)


if __name__ == "__main__":
    unittest.main()

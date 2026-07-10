"""Tests for intraday schedule and trading session helpers."""

import unittest
from datetime import datetime

from tradingagents.intraday.calendar import (
    INTRADAY_SLOTS,
    is_bookable_session,
    is_trading_day,
    next_slot_after,
    slot_due,
)


class IntradayCalendarTests(unittest.TestCase):
    def test_slots_exclude_lunch(self):
        self.assertNotIn("12:25", INTRADAY_SLOTS)

    def test_slot_due_fires_on_match(self):
        now = datetime(2026, 7, 10, 10, 25)
        self.assertEqual(slot_due(now, "09:25"), "10:25")

    def test_slot_due_skips_already_ran(self):
        now = datetime(2026, 7, 10, 10, 25)
        self.assertIsNone(slot_due(now, "10:25"))

    def test_slot_due_respects_min_gap(self):
        now = datetime(2026, 7, 10, 11, 25)
        # Last run at 11:10 — only 15 min before 11:25 slot
        self.assertIsNone(slot_due(now, "11:10", min_gap_minutes=20))

    def test_next_slot_after(self):
        now = datetime(2026, 7, 10, 10, 30)
        self.assertEqual(next_slot_after(now), "11:25")

    def test_bookable_only_in_session(self):
        self.assertTrue(is_bookable_session(datetime(2026, 7, 10, 10, 0)))
        self.assertFalse(is_bookable_session(datetime(2026, 7, 10, 12, 0)))
        self.assertFalse(is_bookable_session(datetime(2026, 7, 10, 16, 0)))

    def test_weekend_not_trading_day(self):
        self.assertFalse(is_trading_day(datetime(2026, 7, 11).date()))


if __name__ == "__main__":
    unittest.main()

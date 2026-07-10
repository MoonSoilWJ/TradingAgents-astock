"""Tests for intraday session persistence."""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tradingagents.intraday.session import IntradaySession


class IntradaySessionTests(unittest.TestCase):
    def test_record_run_sets_trade_date(self):
        from tradingagents.intraday import session as sess_mod

        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.json"
            with mock.patch.object(sess_mod, "SESSION_PATH", session_path):
                sess_mod.request_start(
                    ticker="159813",
                    shares=0,
                    total_capital=100_000,
                    max_position_pct=30,
                )
                sess_mod.record_run(slot="11:00", action="hold", full_run=True)

                loaded = sess_mod.load_session()
                self.assertTrue(loaded.trade_date)
                self.assertEqual(loaded.runs_today, 1)
                self.assertEqual(loaded.full_run_done_date, loaded.trade_date)

    def test_reset_daily_clears_full_run(self):
        today = date.today().isoformat()
        if today == "2026-07-09":
            self.skipTest("cannot simulate prior day on 2026-07-09")
        session = IntradaySession(
            trade_date="2026-07-09",
            full_run_done_date="2026-07-09",
            runs_today=3,
        )
        session.reset_daily_if_needed()
        self.assertEqual(session.full_run_done_date, "")
        self.assertEqual(session.runs_today, 0)
        self.assertEqual(session.last_slot, "")

    def test_daemon_heartbeat(self):
        from tradingagents.intraday import session as sess_mod

        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "run.lock"
            with mock.patch.object(sess_mod, "RUN_LOCK_PATH", lock):
                sess_mod.write_daemon_heartbeat()
                self.assertTrue(sess_mod.is_daemon_alive())
                self.assertIsNotNone(sess_mod.daemon_last_seen())


if __name__ == "__main__":
    unittest.main()

"""Tests for intraday light-run state persistence."""

import json
import tempfile
import unittest
from pathlib import Path

from tradingagents.intraday.light_run import load_cached_state, save_cached_state


class IntradayLightRunTests(unittest.TestCase):
    def test_save_strips_langchain_messages(self):
        from langchain_core.messages import HumanMessage

        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "company_of_interest": "512660",
                "messages": [HumanMessage(content="512660")],
                "intraday_action": "hold",
            }
            save_cached_state("512660", payload, "2026-07-10", tmp)
            on_disk = json.loads(
                (
                    Path(tmp)
                    / "512660"
                    / "TradingAgentsStrategy_logs"
                    / "full_states_log_2026-07-10.json"
                ).read_text(encoding="utf-8")
            )
            self.assertNotIn("messages", on_disk)
            self.assertEqual(on_disk["intraday_action"], "hold")

    def test_save_and_load_cached_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "company_of_interest": "159813",
                "intraday_action": "hold",
                "intraday_quantity": 0,
                "intraday_reason": "观望",
            }
            save_cached_state("159813", payload, "2026-07-10", tmp)
            loaded = load_cached_state("159813", "2026-07-10", tmp)
            self.assertEqual(loaded["intraday_action"], "hold")
            path = (
                Path(tmp)
                / "159813"
                / "TradingAgentsStrategy_logs"
                / "full_states_log_2026-07-10.json"
            )
            self.assertTrue(path.exists())
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["intraday_reason"], "观望")


if __name__ == "__main__":
    unittest.main()

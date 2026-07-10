"""Tests for intraday daemon subprocess launcher."""

import unittest
from unittest import mock

from tradingagents.intraday import daemon_process


class DaemonProcessTests(unittest.TestCase):
    def test_ensure_daemon_started_noop_when_alive(self):
        with mock.patch.object(daemon_process, "is_daemon_alive", return_value=True):
            with mock.patch("subprocess.Popen") as popen:
                self.assertTrue(daemon_process.ensure_daemon_started())
                popen.assert_not_called()

    def test_ensure_daemon_started_spawns_when_dead(self):
        alive = {"value": False}

        def _alive(*_args, **_kwargs):
            return alive["value"]

        def _popen(*_args, **_kwargs):
            alive["value"] = True
            return mock.Mock()

        with mock.patch.object(daemon_process, "is_daemon_alive", side_effect=_alive):
            with mock.patch("subprocess.Popen", side_effect=_popen):
                with mock.patch("builtins.open", mock.mock_open()):
                    self.assertTrue(daemon_process.ensure_daemon_started())


if __name__ == "__main__":
    unittest.main()

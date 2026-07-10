"""Tests for DingTalk notification helpers."""

import unittest

from tradingagents.notify.dingtalk import _with_keyword


class DingTalkKeywordTests(unittest.TestCase):
    def test_with_keyword_prepends_when_missing(self):
        title, text = _with_keyword("512660 alert", "买入 100 股", keyword="test")
        self.assertIn("test", title)
        self.assertTrue(text.startswith("test"))

    def test_with_keyword_noop_when_present(self):
        title, text = _with_keyword("test alert", "test\n买入", keyword="test")
        self.assertEqual(title, "test alert")
        self.assertEqual(text, "test\n买入")


if __name__ == "__main__":
    unittest.main()

"""Tests for the shared rating heuristic and the SignalProcessor adapter.

The Portfolio Manager produces a typed PortfolioDecision via structured
output and renders it to markdown that always contains a ``**Rating**: X``
header.  The deterministic heuristic in ``tradingagents.agents.utils.rating``
is therefore sufficient to extract the rating downstream — no second LLM
call is needed — and SignalProcessor is now a thin adapter that delegates
to it.
"""

import pytest

from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_rating
from tradingagents.graph.signal_processing import SignalProcessor


# ---------------------------------------------------------------------------
# Heuristic parser
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseRating:
    def test_explicit_label_buy(self):
        assert parse_rating("Rating: Buy\nReasoning here.") == "Buy"

    def test_explicit_label_overweight(self):
        assert parse_rating("Rating: Overweight\nDetails.") == "Overweight"

    def test_explicit_label_with_markdown_bold_value(self):
        # Regression: Rating: **Sell** — markdown around the value.
        assert parse_rating("Rating: **Sell**\nExit immediately.") == "Sell"

    def test_explicit_label_with_markdown_bold_label(self):
        assert parse_rating("**Rating**: Underweight\nTrim exposure.") == "Underweight"

    def test_rendered_pm_markdown_shape(self):
        # The exact shape produced by render_pm_decision must always parse.
        text = (
            "**Rating**: Buy\n\n"
            "**Executive Summary**: Enter at $189-192, 6% portfolio cap.\n\n"
            "**Investment Thesis**: AI capex cycle intact; institutional flows constructive."
        )
        assert parse_rating(text) == "Buy"

    def test_explicit_label_wins_over_prose_with_markdown(self):
        text = (
            "The buy thesis is weakened by guidance.\n"
            "Rating: **Sell**\n"
            "Exit before earnings."
        )
        assert parse_rating(text) == "Sell"

    def test_no_rating_returns_default(self):
        assert parse_rating("No clear directional signal at this time.") == "Hold"

    def test_no_rating_custom_default(self):
        assert parse_rating("Plain prose.", default="Underweight") == "Underweight"

    def test_all_five_tiers_recognised(self):
        for r in RATINGS_5_TIER:
            assert parse_rating(f"Rating: {r}") == r

    def test_chinese_quoted_rating(self):
        assert parse_rating('采用"卖出"评级，建议清仓。') == "Sell"

    def test_chinese_label_colon(self):
        assert parse_rating("评级：买入\n理由如下。") == "Buy"

    def test_chinese_pm_markdown_shape(self):
        text = '### 基金经理最终裁决：采用"卖出"评级\n| **卖出** | 强证据链 |'
        assert parse_rating(text) == "Sell"

    def test_chinese_hold(self):
        assert parse_rating("综合判断，评级：持有") == "Hold"

    def test_final_underweight_beats_body_sell_mentions(self):
        text = (
            "综合来看，不应在此刻进攻。\n\n"
            "对浮动盈利部分可执行卖出锁定利润。\n\n"
            "最终评级：Underweight（减配）"
        )
        assert parse_rating(text) == "Underweight"

    def test_underweight_label_with_chinese_annotation(self):
        assert parse_rating("最终评级：Underweight（减配）") == "Underweight"

    def test_insider_reduction_in_prose_not_a_rating(self):
        text = "内部人减持计划公布，技术面承压，暂无明确评级。"
        assert parse_rating(text, default="Hold") == "Hold"

    def test_research_rating_line(self):
        assert parse_rating("**研究评级：Underweight (减配)**") == "Underweight"

    def test_chinese_bold_rating_line(self):
        assert parse_rating("**评级：** **Overweight（超配）**") == "Overweight"

    def test_rating_marker_wins_over_stop_loss_prose(self):
        text = (
            "<!-- TRADINGAGENTS_RATING: Overweight -->\n\n"
            "**评级：** **Overweight（超配）**\n"
            "一经触发，**立即全部清仓（Sell All）**，评级下调至\"Underweight\"。"
        )
        assert parse_rating(text) == "Overweight"

    def test_pm_markdown_stop_loss_does_not_override_rating(self):
        from tradingagents.agents.utils.rating import canonicalize_decision_ratings

        text = (
            "**评级：** **Overweight（超配）**\n"
            "一经触发，**立即全部清仓（Sell All）**，评级下调至\"Underweight\"。"
        )
        state = canonicalize_decision_ratings({"final_trade_decision": text})
        assert state["portfolio_rating"] == "Overweight"
        assert "<!-- TRADINGAGENTS_RATING: Overweight -->" in state["final_trade_decision"]

    def test_canonicalize_prefers_portfolio_rating_field(self):
        from tradingagents.agents.utils.rating import canonicalize_decision_ratings

        state = canonicalize_decision_ratings(
            {
                "portfolio_rating": "Overweight",
                "final_trade_decision": "正文含清仓字样但不改权威字段",
            }
        )
        assert state["portfolio_rating"] == "Overweight"

    def test_canonicalize_prefers_marker_over_stale_field(self):
        from tradingagents.agents.utils.rating import canonicalize_decision_ratings

        state = canonicalize_decision_ratings(
            {
                "portfolio_rating": "Hold",
                "final_trade_decision": (
                    "<!-- TRADINGAGENTS_RATING: Overweight -->\n\n"
                    "**Rating**: Overweight"
                ),
            }
        )
        assert state["portfolio_rating"] == "Overweight"


# ---------------------------------------------------------------------------
# SignalProcessor: thin adapter over the heuristic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSignalProcessor:
    def test_returns_rating_from_pm_markdown(self):
        sp = SignalProcessor()
        md = "**Rating**: Overweight\n\n**Executive Summary**: Build gradually."
        assert sp.process_signal(md) == "Overweight"

    def test_makes_no_llm_calls(self):
        """SignalProcessor must not invoke the LLM it was constructed with —
        the rating is parseable from the rendered PM markdown directly."""
        from unittest.mock import MagicMock

        llm = MagicMock()
        sp = SignalProcessor(llm)
        sp.process_signal("Rating: Buy\nDetails.")
        llm.invoke.assert_not_called()
        llm.with_structured_output.assert_not_called()

    def test_default_when_no_rating_present(self):
        sp = SignalProcessor()
        assert sp.process_signal("Plain prose without a recommendation.") == "Hold"

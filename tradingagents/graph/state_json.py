"""JSON-safe projection of LangGraph agent state for disk persistence."""

from __future__ import annotations

from typing import Any


def persistable_graph_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-serializable subset of graph state (drops LangChain messages)."""
    investment_debate = state.get("investment_debate_state") or {}
    risk_debate = state.get("risk_debate_state") or {}
    return {
        "company_of_interest": state.get("company_of_interest", ""),
        "trade_date": state.get("trade_date", ""),
        "instrument_type": state.get("instrument_type", "stock"),
        "market_report": state.get("market_report", ""),
        "sentiment_report": state.get("sentiment_report", ""),
        "news_report": state.get("news_report", ""),
        "fundamentals_report": state.get("fundamentals_report", ""),
        "policy_report": state.get("policy_report", ""),
        "hot_money_report": state.get("hot_money_report", ""),
        "lockup_report": state.get("lockup_report", ""),
        "investment_debate_state": {
            "bull_history": investment_debate.get("bull_history", ""),
            "bear_history": investment_debate.get("bear_history", ""),
            "history": investment_debate.get("history", ""),
            "current_response": investment_debate.get("current_response", ""),
            "judge_decision": investment_debate.get("judge_decision", ""),
        },
        "trader_investment_decision": state.get("trader_investment_plan", ""),
        "risk_debate_state": {
            "aggressive_history": risk_debate.get("aggressive_history", ""),
            "conservative_history": risk_debate.get("conservative_history", ""),
            "neutral_history": risk_debate.get("neutral_history", ""),
            "history": risk_debate.get("history", ""),
            "judge_decision": risk_debate.get(
                "judge_decision",
                state.get("final_trade_decision", ""),
            ),
        },
        "investment_plan": state.get("investment_plan", ""),
        "research_rating": state.get("research_rating", ""),
        "final_trade_decision": state.get("final_trade_decision", ""),
        "portfolio_rating": state.get("portfolio_rating", ""),
        "intraday_action": state.get("intraday_action", ""),
        "intraday_quantity": state.get("intraday_quantity", 0),
        "intraday_reason": state.get("intraday_reason", ""),
    }

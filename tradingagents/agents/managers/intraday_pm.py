"""Intraday Portfolio Manager — outputs buy/sell/hold with share counts."""

from __future__ import annotations

from tradingagents.agents.schemas import IntradayDecision, render_intraday_decision
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_language_instruction,
    instrument_type_from_state,
)
from tradingagents.agents.utils.structured import (
    IntradayOrderResult,
    bind_structured,
    invoke_intraday_structured_or_freetext,
)


def _portfolio_context_block(state: dict) -> str:
    shares = int(state.get("portfolio_shares") or 0)
    cash = float(state.get("portfolio_cash") or 0)
    capital = float(state.get("portfolio_capital") or 0)
    max_pct = float(state.get("portfolio_max_pct") or 30)
    price = float(state.get("portfolio_price") or 0)
    settlement = state.get("portfolio_settlement") or "T1"
    sellable = int(state.get("portfolio_sellable") or shares)
    lot = int(state.get("portfolio_lot") or 100)
    return f"""**Current Portfolio**
- Holdings: {shares} shares
- Sellable now ({settlement}): {sellable} shares
- Cash: {cash:,.0f} CNY
- Total capital: {capital:,.0f} CNY
- Max position: {max_pct:.0f}% of capital
- Latest price: {price:.3f} CNY
- Minimum lot: {lot} shares

Output exactly one actionable instruction for THIS round only:
- action=buy → quantity_shares > 0 (multiple of {lot})
- action=sell → quantity_shares > 0 (≤ sellable, multiple of {lot})
- action=hold → quantity_shares = 0

Do NOT use rating words (Overweight/Hold/减持). Use only buy/sell/hold with share counts."""


def create_intraday_portfolio_manager(llm):
    structured_llm = bind_structured(llm, IntradayDecision, "Intraday PM")

    def intraday_pm_node(state) -> dict:
        instrument_type = instrument_type_from_state(state)
        instrument_context = build_instrument_context(
            state["company_of_interest"], instrument_type
        )
        history = state["risk_debate_state"]["history"]
        research_plan = state.get("investment_plan", "")
        trader_plan = state.get("trader_investment_plan", "")

        prompt = f"""As the Intraday Portfolio Manager, synthesize the debate and output ONE executable order for this hour.

{instrument_context}

{_portfolio_context_block(state)}

**A-Share Constraints**
- Settlement: {state.get("portfolio_settlement", "T1")} (T+1: today's buys cannot be sold today)
- Price limits and lot size must be respected
- If no change is needed, action=hold and quantity_shares=0

**Context**
- Research plan: {research_plan}
- Trader plan: {trader_plan}
- Risk debate: {history}

Be decisive. Ground the share count in evidence.{get_language_instruction()}"""

        result = invoke_intraday_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_intraday_decision,
            "Intraday PM",
        )

        risk_debate_state = state["risk_debate_state"]
        new_risk_debate_state = {
            "judge_decision": result.markdown,
            "history": risk_debate_state.get("history", ""),
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state.get("current_aggressive_response", ""),
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": risk_debate_state.get("current_neutral_response", ""),
            "count": risk_debate_state.get("count", 0),
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": result.markdown,
            "intraday_action": result.action,
            "intraday_quantity": result.quantity_shares,
            "intraday_reason": result.reason,
            "portfolio_rating": "",
        }

    return intraday_pm_node

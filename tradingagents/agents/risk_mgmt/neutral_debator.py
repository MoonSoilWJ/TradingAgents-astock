

def create_neutral_debator(llm):
    from tradingagents.agents.utils.agent_utils import (
        get_balanced_decision_guidance,
        get_debate_notes,
        get_settlement_risk_notes,
        instrument_type_from_state,
    )

    def neutral_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        neutral_history = risk_debate_state.get("neutral_history", "")

        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
        current_conservative_response = risk_debate_state.get("current_conservative_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        policy_report = state.get("policy_report", "")
        hot_money_report = state.get("hot_money_report", "")
        lockup_report = state.get("lockup_report", "")

        trader_decision = state["trader_investment_plan"]
        instrument_type = instrument_type_from_state(state)

        prompt = f"""As the Neutral Risk Analyst evaluating an A-share (China mainland) instrument, your role is to provide a balanced perspective, weighing both the potential benefits and risks. Factor in market structure, broader trends, and diversification strategies.

Note: {get_debate_notes(instrument_type)}

{get_balanced_decision_guidance()}

A-Share Neutral Framework — balancing considerations:
{get_settlement_risk_notes(state["company_of_interest"])}
- Distinguish policy signal quality; use fund flow as confirmation not sole thesis
- For stocks: valuation bands and lockup timing; for ETFs: trend + flows + premium-discount
- Position sizing over extreme directional calls when evidence is mixed

Here is the trader's decision:

{trader_decision}

Challenge both the aggressive and conservative analysts. Point out where each perspective is overly optimistic or overly cautious in the A-share context. Use these data sources:

Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest News Report: {news_report}
Company Fundamentals Report: {fundamentals_report}
Policy Analysis Report: {policy_report}
Hot Money / Capital Flow Report: {hot_money_report}
Lockup Expiry / Insider Reduction Report: {lockup_report}
Conversation history: {history} Last aggressive argument: {current_aggressive_response} Last conservative argument: {current_conservative_response}. If no responses yet, present your own argument.

Advocate for a balanced, position-sized approach that captures A-share upside while respecting the market's structural constraints. Output conversationally without special formatting."""

        response = llm.invoke(prompt)

        argument = f"Neutral Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": neutral_history + "\n" + argument,
            "latest_speaker": "Neutral",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": argument,
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return neutral_node

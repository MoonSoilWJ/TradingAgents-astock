

def create_conservative_debator(llm):
    from tradingagents.agents.utils.agent_utils import (
        get_balanced_decision_guidance,
        get_debate_notes,
        get_settlement_risk_notes,
        instrument_type_from_state,
    )

    def conservative_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        conservative_history = risk_debate_state.get("conservative_history", "")

        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        policy_report = state.get("policy_report", "")
        hot_money_report = state.get("hot_money_report", "")
        lockup_report = state.get("lockup_report", "")

        trader_decision = state["trader_investment_plan"]
        instrument_type = instrument_type_from_state(state)

        prompt = f"""As the Conservative Risk Analyst evaluating an A-share (China mainland) instrument, your primary objective is to protect assets, minimize volatility, and ensure steady, reliable growth. Critically examine high-risk elements in the trader's plan, pointing out where it may expose the firm to undue risk.

Note: {get_debate_notes(instrument_type)}

{get_balanced_decision_guidance()}

A-Share Conservative Framework — emphasize structural downside risks where applicable:
{get_settlement_risk_notes(state["company_of_interest"])}
- For **stocks**: lockup expiry, insider reduction, ST/delisting, PE discipline
- For **ETFs**: discount spiral, net outflows, broken trend — not company lockup/PE

Here is the trader's decision:

{trader_decision}

Counter the aggressive and neutral analysts. Highlight where their optimism overlooks A-share structural risks. Use these data sources:

Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest News Report: {news_report}
Company Fundamentals Report: {fundamentals_report}
Policy Analysis Report: {policy_report}
Hot Money / Capital Flow Report: {hot_money_report}
Lockup Expiry / Insider Reduction Report: {lockup_report}
Conversation history: {history} Last aggressive argument: {current_aggressive_response} Last neutral argument: {current_neutral_response}. If no responses yet, present your own argument.

Demonstrate why a conservative stance is the safest path, especially given A-share market structure where downside protection mechanisms (stop-loss, same-day exit) are severely limited. Output conversationally without special formatting."""

        response = llm.invoke(prompt)

        argument = f"Conservative Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": conservative_history + "\n" + argument,
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Conservative",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": argument,
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return conservative_node

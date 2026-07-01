

def create_aggressive_debator(llm):
    from tradingagents.agents.utils.agent_utils import (
        get_balanced_decision_guidance,
        get_debate_notes,
        instrument_type_from_state,
    )

    def aggressive_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        aggressive_history = risk_debate_state.get("aggressive_history", "")

        current_conservative_response = risk_debate_state.get("current_conservative_response", "")
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

        prompt = f"""As the Aggressive Risk Analyst evaluating an A-share (China mainland) instrument, your role is to champion high-reward opportunities and bold strategies. Focus on the potential upside, growth potential, and momentum—even when these come with elevated risk. Counter the conservative and neutral analysts with data-driven rebuttals.

Note: {get_debate_notes(instrument_type)}

{get_balanced_decision_guidance()}

Here is the trader's decision:

{trader_decision}

Challenge the conservative and neutral stances. Demonstrate why their caution risks missing the opportunity. Use these data sources:

Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest News Report: {news_report}
Company Fundamentals Report: {fundamentals_report}
Policy Analysis Report: {policy_report}
Hot Money / Capital Flow Report: {hot_money_report}
Lockup Expiry / Insider Reduction Report: {lockup_report}
Conversation history: {history} Last conservative argument: {current_conservative_response} Last neutral argument: {current_neutral_response}. If no responses yet, present your own argument.

Engage actively, debate persuasively, and assert why aggressive positioning is optimal for this A-share opportunity. Output conversationally without special formatting."""

        response = llm.invoke(prompt)

        argument = f"Aggressive Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": aggressive_history + "\n" + argument,
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Aggressive",
            "current_aggressive_response": argument,
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return aggressive_node

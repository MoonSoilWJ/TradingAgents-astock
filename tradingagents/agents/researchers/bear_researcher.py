

def create_bear_researcher(llm):
    from tradingagents.agents.utils.agent_utils import get_bear_framework, instrument_type_from_state

    def bear_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bear_history = investment_debate_state.get("bear_history", "")

        current_response = investment_debate_state.get("current_response", "")
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        policy_report = state.get("policy_report", "")
        hot_money_report = state.get("hot_money_report", "")
        lockup_report = state.get("lockup_report", "")
        data_quality_summary = state.get("data_quality_summary", "")
        instrument_type = instrument_type_from_state(state)

        prompt = f"""You are a Bear Analyst making the case against investing in this A-share (China mainland) instrument. Your goal is to present a well-reasoned argument emphasizing risks, challenges, and negative indicators. Leverage the provided research and data to highlight potential downsides and counter bullish arguments effectively.

{get_bear_framework(instrument_type, state["company_of_interest"])}

General bear points:
- Risks and Challenges: Market saturation, financial instability, or macroeconomic threats
- Competitive Weaknesses: Weaker market positioning, declining innovation, or competitor threats
- Negative Indicators: Evidence from financial data, market trends, or adverse news
- Bull Counterpoints: Expose over-optimistic assumptions with specific data
- Engagement: Present your argument conversationally, directly engaging with the bull analyst's points

Resources available:
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest news report: {news_report}
Company fundamentals report: {fundamentals_report}
Policy analysis report: {policy_report}
Hot money / capital flow report: {hot_money_report}
Lockup expiry / insider reduction report: {lockup_report}
Data quality assessment: {data_quality_summary}
Conversation history of the debate: {history}
Last bull argument: {current_response}

⚠️ If the data quality assessment flags any report as low-confidence (grade C/D/F), reduce your reliance on that report and note the data limitation in your argument.

Deliver a compelling bear argument grounded in A-share market realities. Refute the bull's claims and demonstrate the risks of investing in this stock within the Chinese regulatory and market structure.
"""

        response = llm.invoke(prompt)

        argument = f"Bear Analyst: {response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bear_node

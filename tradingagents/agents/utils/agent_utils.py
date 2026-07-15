from langchain_core.messages import HumanMessage, RemoveMessage

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_transactions,
    get_global_news
)
from tradingagents.agents.utils.signal_data_tools import (
    get_profit_forecast,
    get_hot_stocks,
    get_northbound_flow,
    get_concept_blocks,
    get_fund_flow,
    get_dragon_tiger_board,
    get_lockup_expiry,
    get_industry_comparison,
)


from tradingagents.dataflows.instrument import (
    InstrumentType,
    SettlementRule,
    classify_astock_instrument,
    settlement_rule,
)


def get_language_instruction() -> str:
    """Return a prompt instruction for the configured output language.

    Returns empty string when English (default), so no extra tokens are used.
    Only applied to user-facing agents (analysts, portfolio manager).
    Internal debate agents stay in English for reasoning quality.
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    if lang.strip().lower() == "english":
        return ""
    return f" Write your entire response in {lang}."


def _lookup_astock_name(ticker: str) -> str | None:
    try:
        from tradingagents.dataflows.a_stock import lookup_astock_name

        return lookup_astock_name(ticker)
    except Exception:
        return None


def settlement_for_ticker(ticker: str) -> SettlementRule:
    """Resolve T+0/T+1 settlement for an A-share code (uses official name when available)."""
    return settlement_rule(ticker, _lookup_astock_name(ticker))


def get_settlement_risk_notes(ticker: str) -> str:
    """Settlement-aware risk bullets for debate / PM agents."""
    if settlement_for_ticker(ticker) == "T0":
        return (
            "- T+0 settlement: same-day sell allowed (cross-border / gold / commodity / bond funds). "
            "Primary risks: **intraday reversal** after sharp rallies, ATR/volatility expansion, "
            "and daily price limits — **do not** argue T+1 overnight lock-in."
        )
    return (
        "- T+1 settlement lock: shares bought today cannot be sold until the next trading day; "
        "size positions for survivable overnight gaps and daily price limit traps"
    )


def get_settlement_constraint_prompt(ticker: str) -> str:
    """One-line settlement rule for trader / PM / intraday prompts."""
    if settlement_for_ticker(ticker) == "T0":
        return (
            "- T+0 settlement: shares bought today **can be sold the same trading day** "
            "(cross-border / gold / commodity / bond on-exchange funds). "
            "Primary risk is **intraday reversal**, not overnight lock-in."
        )
    return (
        "- T+1 settlement: shares bought today cannot be sold until the next trading day"
    )


def get_astock_market_rules_prompt(ticker: str) -> str:
    """A-share market rules block with settlement rule matched to the instrument."""
    is_t0 = settlement_for_ticker(ticker) == "T0"
    settlement_line = (
        "- **T+0 交易制度**：当日买入当日可卖出（跨境/黄金/商品/债券类场内基金）。"
        " 盘中可止损/止盈；核心风险是**日内反转与追高回撤**，勿套用 T+1 隔夜锁仓逻辑。"
        if is_t0
        else "- **T+1 交易制度**：当日买入次日才能卖出，短线策略的可执行性受限。"
    )
    return (
        "⚠️ A 股市场特殊规则（分析时必须纳入考量）：\n"
        "- **涨跌停制度**：主板 ±10%，科创板/创业板 ±20%，ST 股 ±5%。触及涨跌停后流动性骤降，技术指标可能失真。\n"
        f"{settlement_line}\n"
        "- **北向资金**：外资通过沪深港通的流入流出是重要的市场风向标，大幅流入/流出常领先于趋势转折。\n"
        "- **换手率**：A 股散户占比高，换手率是判断资金活跃度和筹码松动的关键指标。\n"
        "- **量价关系**：A 股「量在价先」规律显著，放量突破和缩量回调是核心交易信号。"
    )


def instrument_type_from_state(state: dict) -> InstrumentType:
    """Read instrument type from graph state, falling back to code classification."""
    explicit = state.get("instrument_type")
    if explicit in ("stock", "etf"):
        return explicit
    return classify_astock_instrument(state["company_of_interest"])


def build_instrument_context(
    ticker: str,
    instrument_type: InstrumentType | None = None,
) -> str:
    """Describe the instrument so agents preserve tickers and use the right framework."""
    if instrument_type is None:
        instrument_type = classify_astock_instrument(ticker)

    try:
        from tradingagents.dataflows.a_stock import lookup_astock_name

        official_name = lookup_astock_name(ticker)
    except Exception:
        official_name = None

    settle = settlement_rule(ticker, official_name)
    settlement_note = (
        "Settlement: **T+0** (same-day sell allowed)."
        if settle == "T0"
        else "Settlement: **T+1** (bought shares cannot be sold until next trading day)."
    )

    label = f"`{ticker}` {official_name}" if official_name else f"`{ticker}`"
    base = (
        f"The instrument to analyze is {label}. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`).\n"
        f"{settlement_note}"
    )
    if instrument_type == "etf":
        return (
            f"{base}\n"
            "Instrument class: **A-share on-exchange ETF/LOF** (not a single listed company). "
            "Analyze tracking index / sector exposure, fund flows, premium-discount, "
            "volume trend, and policy tailwinds — not company PE, earnings, or lockup expiry."
        )
    return f"{base}\nInstrument class: **A-share listed stock** (single company)."


def get_balanced_decision_guidance() -> str:
    """Shared rules so decision agents do not default bearish on strong bull evidence."""
    return """
**Balanced decision rules** (mandatory):
- Weigh bullish AND bearish evidence explicitly; **do not default to Sell or Underweight**.
- Confirmed policy/industry tailwinds plus sustained fund inflows can support **Buy** or **Overweight** even after a sharp rally.
- **Do NOT** recommend Sell based solely on: high RSI, consecutive up days, or uniformly positive news.
- Reserve **Sell** for: broken trend with distribution confirmed, material negative catalyst, or risk-reward clearly unfavorable after weighing both sides.
- When both sides have valid points, prefer **Hold** or **Underweight** over extreme **Sell**.
- Verify northbound / fund-flow claims against the instrument's **actual exchange** (Shanghai ETF → 沪股通, not 深股通 totals).
"""


def get_pm_rating_alignment_guidance() -> str:
    """Rules so the PM's 5-tier rating matches the orders in the same report."""
    return """
**Rating ↔ action alignment** (mandatory — rating and trading instructions must agree):
- **Hold**: existing position unchanged; **no new buy or sell orders today**. Do not write "Buy" or "立即买入" in Hold decisions.
- **Overweight**: constructive / scale-in view — **includes** initiating a small observation lot (e.g. 1–3%), waiting for pullback to add, or "短空长多" with a starter position. Use Overweight when you instruct any new purchase from flat or below-target exposure, even if size is small.
- **Buy**: strong conviction for immediate meaningful entry (typically ≥5% for a new position, or a decisive add when already invested).
- **Underweight / Sell**: trim or exit as defined in the scale above.
If your executive summary or 交易指令 section calls for an immediate purchase, the rating **cannot** be Hold — use **Overweight** (small/conditional entry) or **Buy** (strong entry).
"""


def get_stock_decision_notes() -> str:
    return (
        "This is an A-share **listed stock**. Factor in regulatory policy, capital flow, "
        "and lockup / insider reduction when synthesising the debate."
    )


def get_etf_decision_notes() -> str:
    return (
        "This is an A-share **on-exchange ETF**, not a single company. "
        "**Do not** apply company PE/PB, earnings forecasts, lockup expiry, or insider "
        "reduction frameworks. Instead evaluate: tracking index trend, sector/policy "
        "tailwinds, ETF net creation/redemption and fund flows, premium-discount vs NAV, "
        "and technical trend. Missing company fundamentals is expected, not a bearish signal."
    )


def get_debate_notes(instrument_type: InstrumentType) -> str:
    if instrument_type == "etf":
        return get_etf_decision_notes()
    return get_stock_decision_notes()


def get_etf_hot_money_addon() -> str:
    return (
        "\n\n⚠️ **ETF 资金面框架**（本标的为 ETF）："
        "\n- 重点分析 ETF 份额变动、申购赎回、主力/融资资金流向、折溢价率"
        "\n- 龙虎榜/内部人交易/限售解禁**通常不适用**，标注 N/A 即可"
        "\n- 北向资金须区分：**沪市 ETF/科创板成分看沪股通**，勿用深股通总额代替"
        "\n- 连板/游资逻辑适用于成分股主题，但 ETF 本身以趋势与资金持续流入为主要看多依据"
    )



def get_etf_sentiment_addon() -> str:
    return (
        "\n\n⚠️ **ETF 情绪分析补充**："
        "\n- 若利好来自**行业景气、政策、龙头业绩**等基本面驱动，一致乐观可以是**趋势延续**信号，"
        "不要自动当作反向指标"
        "\n- 仅当舆情呈现**纯概念炒作、无基本面支撑、极端散户追高**时，才提高反转警惕"
        "\n- 区分「基本面驱动的情绪升温」与「投机性一致看多」"
    )


def get_bull_framework(instrument_type: InstrumentType) -> str:
    if instrument_type == "etf":
        return """A-Share ETF Bull Framework — prioritize:
- Tracking index / sector policy tailwinds (e.g. chips, new productive forces)
- Sustained ETF net inflows, fund creation, and main-force inflow on the ETF itself
- Trend continuation: price above key moving averages with healthy (not collapsing) volume
- Industry catalysts from underlying holdings (not single-stock lockup clearance)
- Premium-discount: small premium or fair value vs NAV supports continuation
Verify northbound claims: Shanghai STAR/科创板 ETF → 沪股通, not 深股通 totals."""
    return """A-Share Bull Framework — prioritize:
- Policy tailwinds, Northbound inflow, hot money momentum, forward PE/PEG story
- Lockup expiry cleared / no insider reduction overhang"""


def get_bear_framework(
    instrument_type: InstrumentType,
    ticker: str | None = None,
) -> str:
    if instrument_type == "etf":
        t0_note = ""
        if ticker and settlement_for_ticker(ticker) == "T0":
            t0_note = (
                "\n- For this **T+0** fund: intraday reversal after sharp rallies; ATR expansion; "
                "same-day exit is allowed — do NOT cite T+1 overnight lock-in"
            )
        return f"""A-Share ETF Bear Framework — prioritize:
- Confirmed trend break (close below 10/20 EMA with rising volume on down days)
- ETF net outflows / shrinking fund size / widening discount to NAV
- Sector policy headwind or regulatory cooling on thematic ETFs
- Parabolic move ONLY when paired with distribution signals (heavy outflow, broken support){t0_note}
Do NOT argue Sell from missing company PE/PB or lockup data — those do not apply to ETFs."""
    return """A-Share Bear Framework — prioritize:
- Policy headwinds, lockup/insider selling, hot money withdrawal, valuation bubble, T+1 trap"""


def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]

        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")

        return {"messages": removal_operations + [placeholder]}

    return delete_messages


        

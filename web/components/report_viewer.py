"""Render the completed analysis report with expandable sections and PDF download."""

from __future__ import annotations

import re
from typing import Any

import streamlit as st

from tradingagents.agents.utils.rating import rating_display_label

from web.history import extract_pm_immediate_action, extract_stage_ratings
from web.pdf_export import generate_markdown, generate_pdf
from web.stock_display import normalize_stock_mentions, stock_display_label


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def _signal_style(signal: str) -> tuple[str, str]:
    en, cn = rating_display_label(signal)
    if en in {"BUY", "OVERWEIGHT"}:
        return "#22c55e", cn
    if en in {"SELL", "UNDERWEIGHT"}:
        return "#ef4444", cn
    if en == "HOLD":
        return "#fbbf24", cn
    return "#888888", cn


def _stage_ratings_html(final_state: dict[str, Any], signal: str) -> str:
    """One-line summary when earlier pipeline stages disagree with the final signal."""
    from tradingagents.agents.utils.rating import normalize_rating_label

    stage_labels = {
        "research": "研究经理",
        "trader": "交易员",
        "portfolio": "组合经理",
    }
    ratings = extract_stage_ratings(final_state)
    portfolio = normalize_rating_label(signal)
    if portfolio and portfolio != "N/A":
        ratings["portfolio"] = portfolio
    if len(ratings) < 2:
        return ""

    parts: list[str] = []
    for key in ("research", "trader", "portfolio"):
        rating = ratings.get(key)
        if not rating:
            continue
        en, cn = rating_display_label(rating)
        parts.append(f"{stage_labels[key]} {en}（{cn}）")

    if not parts:
        return ""

    return (
        '<div style="font-size:0.85rem; color:#888; margin-top:0.6rem; line-height:1.5;">'
        f"{' · '.join(parts)}"
        "</div>"
    )


_ANALYST_SECTIONS = [
    ("market_report", "📊 技术分析"),
    ("sentiment_report", "💬 市场情绪"),
    ("news_report", "📰 新闻舆情"),
    ("fundamentals_report", "📋 基本面"),
    ("policy_report", "🏛️ 政策分析"),
    ("hot_money_report", "🔥 游资追踪"),
    ("lockup_report", "🔒 解禁/减持"),
]


def _safe_filename_label(label: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", label).strip("_")
    return cleaned or "report"


def _display_report_text(text: Any, ticker: str, final_state: dict[str, Any]) -> str:
    cleaned = _strip_think(str(text))
    return normalize_stock_mentions(cleaned, ticker, final_state)


def render_report(
    final_state: dict[str, Any],
    ticker: str,
    trade_date: str,
    signal: str,
    elapsed: float | None = None,
) -> None:
    """Render the full analysis report."""
    from web.history import resolve_report_signal

    signal = resolve_report_signal(final_state, signal)

    color, cn_signal = _signal_style(signal)
    en_signal, _ = rating_display_label(signal)
    ticker_label = stock_display_label(ticker, final_state)
    stage_ratings_html = _stage_ratings_html(final_state, signal)

    stats_html = ""
    if elapsed is not None:
        m, s = divmod(int(elapsed), 60)
        stats_html = f'<div style="font-size:0.9rem; color:#888; margin-top:0.3rem;">耗时 {m}:{s:02d}</div>'

    st.markdown(
        f"""
        <div style="
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            border: 1px solid #333;
            border-radius: 16px;
            padding: 2rem;
            text-align: center;
            margin: 1rem 0 2rem;
        ">
            <div style="font-size:0.9rem; color:#888; letter-spacing:2px;">TRADING SIGNAL</div>
            <div style="font-size:0.8rem; color:#666; margin-top:0.2rem;">组合经理最终评级（经风控辩论）</div>
            <div style="font-size:3.5rem; font-weight:900; color:{color}; margin:0.3rem 0;">
                {en_signal}
            </div>
            <div style="font-size:1.1rem; color:{color}; margin-bottom:0.3rem;">
                {cn_signal}
            </div>
            <div style="font-size:1.2rem; color:#f5f1eb;">
                {ticker_label} · {trade_date}
            </div>
            {stage_ratings_html}
            {stats_html}
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption("⚠️ 本报告由 AI 自动生成，仅供学习研究，不构成投资建议。")

    # Markdown export always works (no font dependency); PDF is generated
    # lazily and guarded so a PDF/font failure never crashes the results page.
    col_md, col_pdf, col_spacer = st.columns([1, 1, 2])
    with col_md:
        md_text = generate_markdown(final_state, ticker, trade_date, signal)
        st.download_button(
            "📥 下载 Markdown",
            data=md_text.encode("utf-8"),
            file_name=f"TradingAgents-Astock_{_safe_filename_label(ticker_label)}_{trade_date}.md",
            mime="text/markdown",
            use_container_width=True,
        )
    with col_pdf:
        try:
            pdf_bytes = generate_pdf(final_state, ticker, trade_date, signal)
            st.download_button(
                "📄 下载 PDF",
                data=pdf_bytes,
                file_name=f"TradingAgents-Astock_{_safe_filename_label(ticker_label)}_{trade_date}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as exc:  # noqa: BLE001 — never let PDF crash the page
            st.button(
                "📄 PDF 不可用",
                disabled=True,
                use_container_width=True,
                help=f"PDF 生成失败，请改用 Markdown 导出。原因：{exc}",
            )

    st.markdown("---")

    final_decision = final_state.get("final_trade_decision", "")
    if final_decision:
        st.markdown("### 👔 组合经理最终决策")
        st.caption("顶部 TRADING SIGNAL 取自本节；经三方风控辩论后对研究/交易计划的最终裁决。")
        pm_en, pm_cn = rating_display_label(signal)
        st.markdown(f"**组合经理评级：{pm_en}（{pm_cn}）**")
        pm_action = extract_pm_immediate_action(final_decision)
        if pm_action == "Buy" and signal == "Hold":
            st.info(
                "正文「交易指令」建议 **买入**（如 2% 观察仓、分批建仓），"
                "属于小仓位试探性建仓，按五档标尺更贴近 **Overweight（增持）** 而非 Hold（不动）。"
                "本次报告评级字段与操作指令不一致，请以实际操作意图为准；"
                "后续分析已加强组合经理的评级对齐规则。"
            )
        elif pm_action == "Buy" and signal in {"Overweight", "Buy"}:
            action_cn = {"Buy": "买入", "Overweight": "增持"}.get(signal, signal)
            st.caption(f"实际操作：{pm_action}（与 {action_cn} 评级一致）")
        st.markdown(_display_report_text(final_decision, ticker, final_state))
        st.markdown("---")

    inv_plan = final_state.get("investment_plan", "")
    if inv_plan:
        st.markdown("### 📋 研究经理投资计划")
        st.caption("多空辩论后的中期方向与战术建议；可能与组合经理最终评级不同。")
        st.markdown(_display_report_text(inv_plan, ticker, final_state))
        st.markdown("---")

    st.markdown("### 📊 分析师报告")

    for key, title in _ANALYST_SECTIONS:
        content = final_state.get(key, "")
        with st.expander(title, expanded=False):
            if not content or not str(content).strip():
                st.info("该分析师未生成报告（可能因分析中断、模型超时或数据源暂时不可用）。")
                continue
            st.markdown(_display_report_text(content, ticker, final_state))

    debate = final_state.get("investment_debate_state")
    if debate and isinstance(debate, dict):
        st.markdown("### ⚔️ 多空辩论")
        tab_bull, tab_bear, tab_judge = st.tabs(["多方", "空方", "研究经理"])
        with tab_bull:
            st.markdown(_display_report_text(debate.get("bull_history", "") or "无数据", ticker, final_state))
        with tab_bear:
            st.markdown(_display_report_text(debate.get("bear_history", "") or "无数据", ticker, final_state))
        with tab_judge:
            st.markdown(_display_report_text(debate.get("judge_decision", "") or "无数据", ticker, final_state))

    trader_decision = (
        final_state.get("trader_investment_plan", "")
        or final_state.get("trader_investment_decision", "")
    )
    if trader_decision:
        with st.expander("💹 交易员决策", expanded=False):
            st.markdown(_display_report_text(trader_decision, ticker, final_state))

    risk = final_state.get("risk_debate_state")
    if risk and isinstance(risk, dict):
        st.markdown("### 🛡️ 风控评估")
        tab_agg, tab_con, tab_neu, tab_rj = st.tabs(["激进", "保守", "中性", "风控决策"])
        with tab_agg:
            st.markdown(_display_report_text(risk.get("aggressive_history", "") or "无数据", ticker, final_state))
        with tab_con:
            st.markdown(_display_report_text(risk.get("conservative_history", "") or "无数据", ticker, final_state))
        with tab_neu:
            st.markdown(_display_report_text(risk.get("neutral_history", "") or "无数据", ticker, final_state))
        with tab_rj:
            st.markdown(_display_report_text(risk.get("judge_decision", "") or "无数据", ticker, final_state))

    dqs = final_state.get("data_quality_summary", "")
    if dqs:
        with st.expander("✅ 数据质量", expanded=False):
            st.markdown(_display_report_text(dqs, ticker, final_state))

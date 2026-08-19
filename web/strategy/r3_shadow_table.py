"""R3 (月度轮动) SHADOW 仪表盘渲染 (与实盘并排对比), 对称于 b_idle_shadow_table.py."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import streamlit as st

from web.strategy.r3_shadow import (
    closed_to_table_rows,
    load_r3_data,
    open_position_row,
    r3_meta,
)
from web.strategy.theme import fmt_dt

# R3 验证结论 (聚宽 2014-2026 + 本地 SHADOW 实跑, 2026-08-12 对齐聚宽 A 版终态)
R3_WF_SUMMARY = {
    "★R3 (月度轮动, 本地 SHADOW 实跑 · 聚宽 canonical 数)": "2014-2026 +1861% / MDD-23.5% / 夏普1.01",
    "★R3 选股机制 (绝对主因子)": "月度轮动质量池 = +970pp vs 固定全池剔除主题仅 -61pp",
    "★R3 候选质量 (去主题)": "drop_sector=True 剔除主题/行业ETF, R3 +1861% >> FIXED-162(含主题) +952% / MDD-41.4%",
    "实盘对照 A (本地2022-2026窗口)": "+472.25% / 329笔 / 胜57% / 回撤-28.7% (窗口不同不直接比高低)",
    "SHADOW B 对照 (本地2022-2026窗口)": "+613.46% / 429笔 / 回撤-33.2% (窗口不同不直接比高低)",
    "R3 回撤/夏普 (聚宽)": "MDD-23.5% (三路最低) / 夏普1.01 (三路最高)",
    "本地 SHADOW 与聚宽 A 版对齐 (2026-08-12终态)": "14:40锁领头羊(--pick)+14:45复核(--signal,≥3%); "
        "中性月池缺失回退全并集(R3_UNIVERSE); cron: 40 14 --pick / 45 14 --signal / 40 9 --sell-loop",
}


def _metric_card(label: str, value: str, delta: str | None = None, good: bool = True):
    color = "#1faa59" if good else "#d9483b"
    st.markdown(
        f"""<div style="border:1px solid #2a2a3a;border-radius:10px;padding:10px 14px;background:#171721;">
        <div style="font-size:12px;color:#9aa0b5;">{label}</div>
        <div style="font-size:20px;font-weight:700;color:{color};">{value}</div>
        {f'<div style="font-size:12px;color:#9aa0b5;">{delta}</div>' if delta else ''}
        </div>""",
        unsafe_allow_html=True,
    )


def render_r3_overview(days: int = 60):
    """R3 SHADOW 总览卡片 + 交易明细 (对称于 render_b_idle_overview)."""
    meta = r3_meta()
    data = load_r3_data(days=days)
    stats = data["stats"]
    state = data["state"]

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        _metric_card("R3 SHADOW 交易数", f"{stats['trade_count']}", f"近{days}天")
    with col2:
        val = f"{stats['compound_pct']:+.1f}%" if stats["compound_pct"] is not None else "—"
        _metric_card("R3 SHADOW 累计(影子)", val, "未实盘, 仅记录", good=(stats["compound_pct"] or 0) >= 0)
    with col3:
        val = f"{stats['win_rate']:.0f}%" if stats["win_rate"] is not None else "—"
        _metric_card("胜率", val)
    with col4:
        open_pos = open_position_row(state)
        _metric_card("当前持仓", open_pos["标的"] if open_pos else "空仓",
                     open_pos["腿"] if open_pos else "无")

    sm = fmt_dt(meta.get("state_mtime"))
    st.caption(f"状态: `{meta['state_path']}` · 更新 {sm} · 流水 {meta['line_count']} 条 · "
               "⚠️ 影子策略, 仅模拟记录, 不下单")

    st.markdown("**R3 SHADOW 交易明细**")
    rows = closed_to_table_rows(data["closed"])
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("R3 SHADOW 暂无平仓记录 (刚启动或近%d天无交易)" % days)


def render_r3_conclusion():
    """R3 验证结论 (对称于 render_b_idle_vs_live 的验证结论 expander)."""
    with st.expander("📊 R3 策略验证结论 (聚宽 2014-2026 + 本地 SHADOW)"):
        for k, v in R3_WF_SUMMARY.items():
            st.markdown(f"- **{k}**: `{v}`")
        st.markdown("> **R3 = 月度轮动质量池 (当前生产候选, 第3个在跑策略)**")
        st.markdown("> - 选股: 趋势/震荡→R3宇宙按近30天动量取Top25滚动优质池; 中性→当月月池(缺失回退全并集R3_UNIVERSE)。")
        st.markdown("> - 卖点: 纯 TRIX(5,3)死叉, 窗口 次日 09:40~11:05, 11:05 fallback (与SHADOW B一致)。")
        st.markdown("> - 双时点: 14:40 锁领头羊 + 14:45 复核(要求仍≥3%), 防尾盘脉冲 + 不漏尾盘爆发赢家。")
        st.markdown("> - 聚宽 canonical +1861%/MDD-23.5%/夏普1.01 = 三路在跑策略中回撤最低、夏普最高。")
        st.markdown("> - 本地 SHADOW 实际会选到与聚宽逐字对齐的标的, 可对照逐笔。")

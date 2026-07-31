"""B+idle SHADOW 仪表盘渲染 (与实盘并排对比)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from web.strategy.b_idle_shadow import (
    b_idle_meta,
    closed_to_table_rows,
    load_b_idle_data,
    open_position_row,
)
from web.strategy.theme import fmt_dt

# 验证结论 (来自 backtest_core_pool_wf.json / b_idle_merge.json, 落盘缓存)
WF_SUMMARY = {
    "B+hybrid(in-sample)": "+1335.45% / 430笔 / 胜57% / 回撤-27.3%",
    "B+hybrid(OOS)": "+550.39% / 208笔 (分界2024-12-19, 388天)",
    "B+idle合并(OOS)": "+1193.75% / 255笔 (idle贡献+643%)",
    "idle腿(OOS独立)": "+98.92% / 47笔",
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


def render_b_idle_overview(days: int = 60):
    """B+idle SHADOW 总览卡片 + 与实盘对比入口."""
    meta = b_idle_meta()
    data = load_b_idle_data(days=days)
    stats = data["stats"]
    state = data["state"]

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        _metric_card("SHADOW 交易数", f"{stats['trade_count']}", f"近{days}天")
    with col2:
        val = f"{stats['compound_pct']:+.1f}%" if stats["compound_pct"] is not None else "—"
        _metric_card("SHADOW 累计(影子)", val, "未实盘, 仅记录", good=(stats["compound_pct"] or 0) >= 0)
    with col3:
        val = f"{stats['win_rate']:.0f}%" if stats["win_rate"] is not None else "—"
        _metric_card("胜率", val)
    with col4:
        open_pos = open_position_row(state)
        _metric_card("当前持仓", open_pos["标的"] if open_pos else "空仓",
                     open_pos["腿"] if open_pos else "无")

    sm = datetime.fromtimestamp(meta["state_mtime"]).strftime("%Y-%m-%d %H:%M") if meta.get("state_mtime") else "—"
    st.caption(f"状态文件: `{meta['state_path']}` · 更新 {sm} · 流水 {meta['line_count']} 条 · "
               "⚠️ 影子策略, 仅模拟记录, 不下单")


def render_b_idle_vs_live(live_trades: list[dict[str, Any]], days: int = 60):
    """B+idle SHADOW 与实盘 T0 并排对比."""
    st.subheader("🆚 B+idle SHADOW vs 实盘 T0")
    st.caption("同一时间段, 实盘(hybrid-A选股) vs 影子(B全市场选股+idle腿), 验证新策略是否更优")

    data = load_b_idle_data(days=days)
    s_shadow = data["stats"]
    s_live = _live_stats(live_trades)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**交易数**")
        st.markdown(f"- 实盘: `{s_live['count']}`")
        st.markdown(f"- SHADOW: `{s_shadow['trade_count']}`")
    with c2:
        st.markdown("**累计收益(影子/实盘)**")
        lv = f"{s_live['compound']:+.1f}%" if s_live["compound"] is not None else "—"
        sv = f"{s_shadow['compound_pct']:+.1f}%" if s_shadow["compound_pct"] is not None else "—"
        st.markdown(f"- 实盘: `{lv}`")
        st.markdown(f"- SHADOW: `{sv}`")
    with c3:
        st.markdown("**胜率**")
        lv = f"{s_live['win']:.0f}%" if s_live["win"] is not None else "—"
        sv = f"{s_shadow['win_rate']:.0f}%" if s_shadow["win_rate"] is not None else "—"
        st.markdown(f"- 实盘: `{lv}`")
        st.markdown(f"- SHADOW: `{sv}`")

    st.markdown("**SHADOW 交易明细**")
    rows = closed_to_table_rows(data["closed"])
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("SHADOW 暂无平仓记录 (刚启动或近%d天无交易)" % days)

    # 验证结论
    with st.expander("📊 策略验证结论 (WF 样本外)"):
        for k, v in WF_SUMMARY.items():
            st.markdown(f"- **{k}**: `{v}`")
        st.markdown("> **卖点窗口(很重要, 别混)**:")
        st.markdown("> - **核心腿 B** = hybrid: 次日 5分K `TRIX(5,3)死叉` 或 `peak回落0.5%`, 先发生先卖;"
                    "窗口 **次日 09:40~11:05**, 内均未触发则 11:05 收盘平 (对齐回测 simulate_hybrid_v2 → +550.39%)。")
        st.markdown("> - **idle 腿** = 次日 **14:50 固定平仓** (+98.92% OOS, 吃满隔夜趋势)。**14:50 只属于 idle 腿, 不是核心腿。**")
        st.markdown("> B+idle = 全市场Top1选股(B) 不regime过滤 + 闲置资金隔夜动量腿(idle)。"
                    "OOS 合并 +1194%, idle 腿贡献 +643%, 已验证稳健非过拟合。实盘先以 SHADOW 运行观察。")


def _live_stats(live_trades: list[dict[str, Any]]) -> dict[str, Any]:
    rets = [float(t["预估收益%"]) for t in live_trades if t.get("预估收益%") is not None]
    if not rets:
        return {"count": 0, "compound": None, "win": None}
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {
        "count": len(rets),
        "compound": (eq - 1) * 100,
        "win": sum(1 for r in rets if r > 0) / len(rets) * 100,
    }

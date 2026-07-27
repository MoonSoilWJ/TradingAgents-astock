"""Idle-window shadow card for Streamlit."""

from __future__ import annotations

import streamlit as st

from web.strategy.idle_shadow import (
    closed_to_table_rows,
    events_to_log_rows,
    idle_shadow_meta,
    load_idle_shadow_data,
    open_leg_rows,
)
from web.strategy.theme import fmt_dt

TABLE_COLUMNS = {
    "v6": st.column_config.NumberColumn(format="%.2f"),
    "涨%": st.column_config.NumberColumn(format="%.2f"),
    "买入价": st.column_config.NumberColumn(format="%.4f"),
    "卖出价": st.column_config.NumberColumn(format="%.4f"),
    "收益%": st.column_config.NumberColumn(format="%+.2f"),
}


def render_idle_shadow_card(*, days: int = 30, compact: bool = False) -> None:
    """独立卡片：闲置双段 Shadow（不改 14:50 基线实盘）。"""
    meta = idle_shadow_meta()
    if not compact:
        st.subheader("🌤️ T+0 闲置双段 Shadow")
    st.caption(
        "段1: 11:25→13:05→13:30 · 段2: 11:05→14:05→14:15 TRIX · "
        "仅模拟日志，实盘基线仍由 t0_monitor 14:50 执行"
    )

    if compact:
        show_days = days
    else:
        show_days = st.slider(
            "Shadow 显示最近 N 天",
            7,
            90,
            days,
            step=7,
            key="idle_shadow_days",
        )

    if not compact and st.button("🔄 刷新 Shadow", key="refresh_idle_shadow"):
        st.rerun()

    st.caption(
        f"日志: {meta['path']} · 更新 {fmt_dt(meta['mtime'])} · "
        f"{meta['line_count']} 条"
    )

    data = load_idle_shadow_data(days=show_days)
    stats = data["stats"]
    state = data["state"]

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Shadow 平仓", f"{stats['sell_count']} 笔")
    with m2:
        cp = stats.get("compound_pct")
        st.metric("Shadow 复利", f"{cp:+.2f}%" if cp is not None else "—")
    with m3:
        l1 = stats.get("leg1_pct")
        st.metric("段1 累计", f"{l1:+.2f}%" if l1 is not None else "—")
    with m4:
        l2 = stats.get("leg2_pct")
        st.metric("段2 累计", f"{l2:+.2f}%" if l2 is not None else "—")

    open_rows = open_leg_rows(state)
    closed_rows = closed_to_table_rows(data["closed"])
    display = open_rows + closed_rows

    if compact and len(display) > 6:
        display = display[:6]

    if display:
        st.dataframe(
            display,
            use_container_width=True,
            hide_index=True,
            column_config=TABLE_COLUMNS,
        )
        if stats.get("avg_pct") is not None:
            st.caption(
                f"已平仓 {stats['sell_count']} 笔 · "
                f"段1 {stats.get('leg1_count', 0)} / 段2 {stats.get('leg2_count', 0)} · "
                f"均笔 {stats['avg_pct']:+.2f}%"
            )
    else:
        st.info(
            "暂无 Shadow 记录。交易日 cron 跑完后写入 t0_idle_shadow.jsonl；"
            "也可手动: python3 scripts/t0_idle_shadow.py --tick"
        )

    if not compact:
        with st.expander("当日状态 JSON"):
            if state.get("trade_date"):
                st.json(state)
            else:
                st.info("t0_idle_shadow_state.json 尚无当日数据")

        with st.expander("事件流水（最近 40 条）"):
            log_rows = events_to_log_rows(data["events"], limit=40)
            if log_rows:
                st.dataframe(log_rows, use_container_width=True, hide_index=True)
            else:
                st.info("暂无事件")
    elif display:
        st.caption("完整 Shadow 流水与事件见侧边栏 **实盘监控**")

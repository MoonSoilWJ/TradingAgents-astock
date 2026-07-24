"""Reusable T+0 trade table for Streamlit pages."""

from __future__ import annotations

from typing import Any

import streamlit as st

from web.strategy.t0_journal import (
    backfill_journal_from_shadow,
    journal_meta,
    load_t0_trades,
    open_position_row,
    trades_to_table_rows,
)
from web.strategy.theme import fmt_dt

TABLE_COLUMNS = {
    "信号涨幅%": st.column_config.NumberColumn(format="%.2f"),
    "买入价": st.column_config.NumberColumn(format="%.4f"),
    "卖出价": st.column_config.NumberColumn(format="%.4f"),
    "卖出浮盈%": st.column_config.NumberColumn(format="%+.2f"),
    "预估收益%": st.column_config.NumberColumn(format="%+.2f"),
}


def render_t0_trade_table(
    *,
    state_data: dict[str, Any] | None,
    days: int = 60,
    show_shadow_expander: bool = True,
    compact: bool = False,
) -> None:
    """Render T+0 closed/open trades as a dataframe."""
    meta = journal_meta()
    if not compact:
        trade_days = st.slider(
            "显示最近 N 天",
            7,
            180,
            days,
            step=7,
            key=f"t0_trade_days_{'compact' if compact else 'full'}",
        )
    else:
        trade_days = days

    if not compact:
        btn_cols = st.columns([1, 3])
        with btn_cols[0]:
            if st.button("🔄 刷新交易流水", key="refresh_t0_trades"):
                st.rerun()
        with btn_cols[1]:
            if st.button("📥 从 shadow 补写流水", key="backfill_t0_journal"):
                n = backfill_journal_from_shadow()
                st.success(f"已补写 {n} 条到 t0_trade_journal.jsonl")
                st.rerun()

    hint = ""
    if not meta["line_count"]:
        hint = " · 历史成交会从 t0_trail_shadow.jsonl 补全"
    st.caption(
        f"流水: {meta['path']} · 更新 {fmt_dt(meta['mtime'])} · "
        f"{meta['line_count']} 条{hint}"
    )

    trade_data = load_t0_trades(days=trade_days)
    table_rows = trades_to_table_rows(trade_data["closed"])
    open_row = open_position_row(state_data)
    if open_row:
        table_rows.insert(0, open_row)

    if table_rows:
        st.dataframe(
            table_rows,
            use_container_width=True,
            hide_index=True,
            column_config=TABLE_COLUMNS,
        )
        closed_count = sum(1 for r in table_rows if r.get("状态") == "已平仓")
        if closed_count:
            rets = [r["预估收益%"] for r in table_rows if r.get("预估收益%") is not None]
            if rets:
                st.caption(
                    f"已平仓 {closed_count} 笔 · 累计预估收益 {sum(rets):+.2f}% · "
                    f"均笔 {sum(rets) / len(rets):+.2f}%"
                )
    else:
        st.info("暂无 T+0 成交记录（卖出后会写入 t0_trade_journal.jsonl）")

    if show_shadow_expander and not compact:
        with st.expander("Shadow 检查明细（每 50 秒浮盈快照）"):
            shadow_path = trade_data.get("shadow_path")
            if shadow_path and shadow_path.exists():
                st.caption(f"{shadow_path} · 仅展示最近 30 条检查")
                from web.strategy.t0_journal import _read_jsonl  # noqa: PLC0415

                checks = [
                    r for r in _read_jsonl(shadow_path)
                    if r.get("event") != "live_sell" and r.get("price") is not None
                ][-30:]
                if checks:
                    shadow_rows = [
                        {
                            "时间": (r.get("ts") or "")[5:16].replace("T", " "),
                            "代码": r.get("etf"),
                            "标的": r.get("name"),
                            "买入日": r.get("buy_date"),
                            "现价": r.get("price"),
                            "浮盈%": r.get("float_pct"),
                            "TRIX": "Y" if r.get("trix_would_sell") else "N",
                            "追踪": "Y" if r.get("trail_would_sell") else "N",
                        }
                        for r in reversed(checks)
                    ]
                    st.dataframe(shadow_rows, use_container_width=True, hide_index=True)
                else:
                    st.info("暂无 shadow 检查记录")
            else:
                st.info("t0_trail_shadow.jsonl 不存在")

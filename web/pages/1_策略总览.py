"""策略总览 — 防遗忘首页."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from web.strategy.artifact_scanner import min_cache_stats, scan_artifacts
from web.strategy.paths import STATUS_LABELS, STATUS_ORDER
from web.strategy.registry_loader import count_by_status, get_strategies, load_registry
from web.strategy.state_reader import rotation_state, t0_state, walk_forward_state
from web.strategy.t0_journal import load_t0_trades, trades_to_table_rows
from web.strategy.t0_table import TABLE_COLUMNS
from web.strategy.theme import fmt_dt, inject_css, render_strategy_card

st.set_page_config(page_title="策略总览", page_icon="📋", layout="wide")
inject_css()

st.title("📋 策略总览")
st.caption("实盘 / 候选 / 研究策略一览 — 数据来自 strategies/registry.json + ~/.tradingagents/rotation/")

if st.button("🔄 刷新索引", type="primary"):
    load_registry.cache_clear()
    st.rerun()

counts = count_by_status()
cols = st.columns(len(STATUS_ORDER))
for col, status in zip(cols, STATUS_ORDER):
    with col:
        st.metric(STATUS_LABELS.get(status, status), counts.get(status, 0))

cache = min_cache_stats()
rot = rotation_state()
t0 = t0_state()
wf = walk_forward_state()

k1, k2, k3, k4 = st.columns(4)
with k1:
    st.metric("轮动最后更新", fmt_dt(rot["mtime"]))
with k2:
    st.metric("T+0 最后更新", fmt_dt(t0["mtime"]))
with k3:
    st.metric("WF 结论", wf.get("decision") or "—")
with k4:
    st.metric("min_cache 文件", f"{cache['file_count']:,}")

st.divider()
st.subheader("最近 T+0 成交")
recent_t0 = trades_to_table_rows(load_t0_trades(days=30)["closed"][:5])
if recent_t0:
    st.dataframe(
        recent_t0,
        use_container_width=True,
        hide_index=True,
        column_config=TABLE_COLUMNS,
    )
    st.caption("完整流水与 shadow 明细见侧边栏 **实盘监控**")
else:
    st.info("暂无 T+0 成交记录")

st.divider()

focus_statuses = ("live", "shadow", "candidate")
st.subheader("重点策略")
for status in focus_statuses:
    items = get_strategies(status=status)
    if not items:
        continue
    st.markdown(f"#### {STATUS_LABELS.get(status, status)}")
    for strat in items:
        render_strategy_card(strat, expanded=(status == "live"))

with st.expander("研究与已否决策略"):
    for status in ("research", "rejected", "deprecated"):
        items = get_strategies(status=status)
        if not items:
            continue
        st.markdown(f"**{STATUS_LABELS.get(status, status)}**")
        for strat in items:
            render_strategy_card(strat)

st.divider()
st.subheader("最近回测 / WF 结果")
recent = scan_artifacts(limit=15)
if not recent:
    st.info("暂无索引到的 JSON 结果（~/.tradingagents/rotation/）")
else:
    rows = []
    for art in recent:
        rows.append({
            "时间": fmt_dt(art.mtime),
            "文件": art.name,
            "类型": art.kind,
            "策略": art.strategy_id or "—",
            "摘要": art.summary[:60],
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

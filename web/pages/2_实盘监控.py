"""实盘监控 — cron 任务与当日状态."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from web.strategy.artifact_scanner import log_files, min_cache_stats
from web.strategy.registry_loader import get_strategy, load_cron_manifest
from web.strategy.state_reader import rotation_state, t0_state, walk_forward_state
from web.strategy.t0_table import render_t0_trade_table
from web.strategy.theme import fmt_dt, inject_css

st.set_page_config(page_title="实盘监控", page_icon="📡", layout="wide")
inject_css()

st.title("📡 实盘监控")
st.caption("T+0 交易流水 · 定时任务 · 健康状态")

t0 = t0_state()
st.subheader("T+0 交易流水")
render_t0_trade_table(state_data=t0.get("data"), days=60)

st.divider()

manifest = load_cron_manifest()
jobs = manifest.get("jobs", [])

st.subheader("Cron 任务")
if jobs:
    rows = []
    for job in jobs:
        strat = get_strategy(job.get("strategy_id", "")) or {}
        rows.append({
            "任务": job.get("name"),
            "Cron": job.get("cron"),
            "脚本": job.get("script"),
            "策略": strat.get("name", job.get("strategy_id", "")),
            "日志": job.get("log", "—"),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)
else:
    st.warning("未找到 strategies/cron_manifest.json")

st.divider()

col_r, col_t = st.columns(2)

with col_r:
    st.subheader("板块轮动 v6")
    rot = rotation_state()
    st.caption(f"状态文件: {rot['path']} · 更新 {fmt_dt(rot['mtime'])}")
    data = rot.get("data")
    if data:
        st.write(f"**日期** {data.get('date', '—')}")
        top5 = data.get("top5_scores") or []
        if top5:
            st.markdown("**TOP5**")
            for i, s in enumerate(top5[:5], 1):
                etf = s.get("etf_code") or s.get("code", "")
                st.text(
                    f"{i}. {s.get('name', '')} 得分{s.get('score', 0):.1f} "
                    f"3日{s.get('ret_3d', 0):+.1f}% {etf}"
                )
        else:
            st.json(data)
    else:
        st.info("monitor_state.json 不存在或无法解析")

with col_t:
    st.subheader("T+0 基线 TRIX")
    t0 = t0_state()
    st.caption(f"状态文件: {t0['path']} · 更新 {fmt_dt(t0['mtime'])}")
    data = t0.get("data")
    if data:
        strat = data.get("strategy") or {}
        if strat:
            st.markdown("**当前策略版本**")
            st.json(strat)
        sig = data.get("last_signal")
        if sig:
            st.markdown("**最近信号**")
            st.json(sig)
        pos = data.get("position")
        if pos:
            st.markdown("**持仓**")
            st.json(pos)
        if not (strat or sig or pos):
            st.json(data)
    else:
        st.info("t0_monitor_state.json 不存在或无法解析")

st.divider()

col_wf, col_cache = st.columns(2)

with col_wf:
    st.subheader("Walk-Forward 最新")
    wf = walk_forward_state()
    st.caption(f"更新 {fmt_dt(wf['mtime'])} · 运行 {wf.get('run_at') or '—'}")
    if wf.get("data"):
        rec = wf["data"].get("recommendation", {})
        st.markdown(f"**结论:** {rec.get('label', '—')}")
        st.markdown(rec.get("detail", ""))
        with st.expander("完整 JSON"):
            st.json(wf["data"])
    else:
        st.info("t0_walk_forward_state.json 尚未生成")

with col_cache:
    st.subheader("数据缓存")
    cache = min_cache_stats()
    st.metric("min_cache 文件数", f"{cache['file_count']:,}")
    st.write(f"最近缓存日期: **{cache.get('latest_date') or '—'}**")
    st.write(f"最后写入: {fmt_dt(cache.get('latest_mtime'))}")

st.divider()
st.subheader("日志文件")
logs = log_files()
if logs:
    for lg in logs:
        stale = ""
        st.text(f"{lg['name']}  ·  {fmt_dt(lg['mtime'])}  ·  {lg['size']:,} bytes{stale}")
else:
    st.info("暂无 .log 文件")

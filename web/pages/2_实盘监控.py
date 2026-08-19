"""实盘监控 — cron 任务与当日状态."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from web.strategy.artifact_scanner import log_files, min_cache_stats
from web.strategy.registry_loader import get_strategy, load_cron_manifest, load_registry
from web.strategy.state_reader import rotation_state, t0_state, walk_forward_state, state_file_info
from web.strategy.t0_table import render_t0_trade_table
from web.strategy.b_idle_shadow_table import render_b_idle_overview, render_b_idle_vs_live
from web.strategy.r3_shadow_table import render_r3_overview, render_r3_conclusion
from web.strategy.t0_journal import load_t0_trades, trades_to_table_rows
from web.strategy.theme import fmt_dt, inject_css, status_badge_html


def render_r3_attack_pool():
    """R3 月度轮动 SHADOW — registry 信息 + 当前攻击池 (交易明细/持仓见 render_r3_overview).

    R3 现已转本地 SHADOW 实跑 (scripts/t0_r3_monitor.py, --install-r3): 选股与聚宽
    joinquant_unified_single.py 对齐 (regime 感知) —— 趋势/震荡→动量Top25滚动优质池,
    中性→当月R3月度轮动宽池; 仅写 r3_shadow_state.json / r3_journal.jsonl, 不下单, 不改实盘。
    聚宽仅用于历史验证。
    """
    _by_id = load_registry().get("_by_id", {})
    s = _by_id.get("jq_r3_attack")
    if s:
        st.subheader("R3 (月度轮动 SHADOW) — registry 信息")
        st.markdown(f"状态 `{s.get('status')}` · 脚本 `{s.get('script','')}` · 版本 `{s.get('version','')}`")
        rules = s.get("rules", {})
        st.markdown(f"**选股**: {rules.get('pick','')}")
        st.markdown(f"**买 / 卖**: {rules.get('buy','')} / {rules.get('sell','')}")
        st.markdown(f"**结论**: {s.get('conclusion','')}")
    # 当前攻击池 (取 <= 当前月的最后一个非空月份)
    pool_path = _PROJECT_ROOT / "scripts" / "jq_pools" / "jq_attack_R3.json"
    if pool_path.exists():
        try:
            pool = json.loads(pool_path.read_text(encoding="utf-8"))
            ym_now = datetime.now().strftime("%Y-%m")
            valid = [k for k in pool if k <= ym_now and pool[k]]
            ym = max(valid) if valid else None
            if ym:
                codes = pool[ym]
                with st.expander(f"当前攻击池 ({ym}, {len(codes)} 只)", expanded=False):
                    st.text(", ".join(codes))
            else:
                st.info("暂无有效月度攻击池")
        except Exception as e:  # noqa: BLE001
            st.warning(f"读取 R3 攻击池失败: {e}")
    else:
        st.info("未找到 scripts/jq_pools/jq_attack_R3.json")

st.set_page_config(page_title="实盘监控", page_icon="📡", layout="wide")
inject_css()

st.title("📡 实盘监控")
st.caption("T+0 交易流水 · 定时任务 · 健康状态")

# ── 在跑三策略概览 (紧凑卡片 + 今日持仓快照) ──
st.divider()
st.subheader("🚀 当前在跑三策略")
st.caption("实盘 A (本地真实下单) · SHADOW B (本地影子·不下单) · R3 月度轮动 (本地 SHADOW·不下单)")

_running = [
    ("t0_baseline_trix", "实盘 A · 本地真实下单", "t0_monitor_state.json", "position"),
    ("t0_b_idle_shadow", "SHADOW B · 本地影子·不下单", "b_idle_shadow_state.json", "position"),
    ("jq_r3_attack", "R3 月度轮动 · 本地 SHADOW·不下单", "r3_shadow_state.json", "position"),
]
_by_id = load_registry().get("_by_id", {})
_rcols = st.columns(3)
for (_sid, _role, _state_rel, _pk), _col in zip(_running, _rcols):
    _s = _by_id.get(_sid)
    if not _s:
        continue
    with _col:
        _stt = state_file_info(_state_rel)
        _pos = (_stt.get("data") or {}).get(_pk) if _stt.get("data") else None
        _has_open = bool(_pos) and not _pos.get("sold")
        if _has_open:
            _pos_txt = (f"持仓 {_pos.get('name', '')} ({_pos.get('etf', '')}) "
                        f"@{_pos.get('buy_price', '')} 信号{_pos.get('today_gain', '')}%")
        else:
            _pos_txt = "空仓 / 无信号"
        st.markdown(
            f"""<div class="strategy-card">
            <h4>{_s['name']}</h4>
            <div class="strategy-meta">{_role}</div>
            {status_badge_html(_s.get('status', 'shadow'))}
            <div class="rule-kv">今日: {_pos_txt}</div>
            </div>""",
            unsafe_allow_html=True,
        )

# ── 三策略详情 (Tab 分组, 减少垂直滚动) ──
tab_a, tab_b, tab_r3 = st.tabs(
    ["🟢 实盘 A · 本地真实下单", "🔵 SHADOW B · 影子", "🟣 R3 月度轮动 · 影子"]
)

with tab_a:
    t0 = t0_state()
    st.subheader("T+0 交易流水 (实盘)")
    render_t0_trade_table(state_data=t0.get("data"), days=60)
    st.divider()
    st.subheader("T+0 基线 TRIX — 运行状态")
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

with tab_b:
    render_b_idle_overview(days=60)
    live = load_t0_trades(days=60)["closed"]
    live_rows = trades_to_table_rows(live)
    render_b_idle_vs_live(live_rows, days=60)

with tab_r3:
    render_r3_overview(days=60)
    render_r3_attack_pool()
    render_r3_conclusion()

# ── 运维状态 ──
st.divider()
st.subheader("🛠 运维状态")

manifest = load_cron_manifest()
jobs = manifest.get("jobs", [])

st.markdown("**Cron 任务**")
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

col_r, col_wf = st.columns(2)
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

col_cache, col_log = st.columns(2)
with col_cache:
    st.subheader("数据缓存")
    cache = min_cache_stats()
    st.metric("min_cache 文件数", f"{cache['file_count']:,}")
    st.write(f"最近缓存日期: **{cache.get('latest_date') or '—'}**")
    st.write(f"最后写入: {fmt_dt(cache.get('latest_mtime'))}")

with col_log:
    st.subheader("日志文件")
    logs = log_files()
    if logs:
        with st.expander(f"查看 {len(logs)} 个日志文件", expanded=False):
            for lg in logs:
                st.text(f"{lg['name']}  ·  {fmt_dt(lg['mtime'])}  ·  {lg['size']:,} bytes")
    else:
        st.info("暂无 .log 文件")

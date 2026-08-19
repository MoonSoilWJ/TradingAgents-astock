"""R3 (月度轮动) 策略说明书 + 执行步骤 + 验证结论 (WebUI 渲染).

与 SHADOW B 的差异:
  - 选股: 月度轮动质量池 (R3_POOLS, 严格无未来函数) + 14:40 锁领头羊 + 14:45 复核;
  - 腿: R3_动量精选 / R3_月度轮动 (B 只有 core_B);
  - 数据: r3_shadow_state.json / r3_journal.jsonl.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from web.strategy.r3_shadow import (
    closed_to_table_rows,
    load_r3_data,
    open_position_row,
    r3_meta,
)
from web.strategy.r3_shadow_table import R3_WF_SUMMARY
from web.strategy.theme import fmt_dt

# ── 策略一句话 ──────────────────────────────────────────────────────────────
R3_ONELINE = (
    "R3 = 月度轮动质量池 (ATTACK_POOL_RULE=\"R3\", 严格无未来函数: 当月交易用上月末 pool_as_of, "
    "drop_sector=True 剔除主题/行业ETF)。选股: 趋势/震荡→R3宇宙按近30天动量取Top25滚动优质池; "
    "中性→当月月池(缺失回退全并集)。<b>双时点</b>: 14:40 锁领头羊(--pick) + 14:45 复核(--signal, 仍≥3%)。"
    "<b>卖点</b>: 纯 TRIX(5,3)死叉, 窗口次日 09:40~11:05, 11:05 fallback。"
)

# ── 详细执行步骤 ────────────────────────────────────────────────────────────
R3_STEPS = [
    ("14:40", "锁领头羊 (--pick)",
     "扫 R3 宇宙 (趋势/震荡=近30天动量Top25滚动优质池; 中性=当月月池), 取当日涨幅最高的领头羊, "
     "记候选 (14:40 锁定, 不要求此刻≥3%——允许尾盘爆发的赢家, 避免 14:45 回溯漏选)。"),
    ("14:45", "复核成交 (--signal)",
     "要求领头羊在 14:45 实时涨幅仍 <b>≥ 3%</b> 才成交; 跌破则放弃 (防尾盘脉冲 + 必须尾盘仍强)。"
     "命中 → R3_SHADOW 持仓(动量精选/月度轮动腿); 未命中 → 空仓。"),
    ("次日 09:40~11:05", "卖出 (--sell-loop)",
     "全日监控持仓 5分K: <b>TRIX(5,3) 死叉</b>触发即卖; 内未触发则 11:05 收盘 fallback 平 "
     "(纯 TRIX 卖点, 对齐回测 simulate_exit('trix0940_cut'))。买入当日不卖, 须持到次日。"),
]

R3_PARAMS = [
    ("选股规则", "ATTACK_POOL_RULE=\"R3\" · 月度轮动质量池(免上传JSON, jq_attack_pools.py 内联 R1~R6, 键=使用月/值=上月末 pool_as_of, 严格无未来函数)"),
    ("趋势/震荡", "从 R3 宇宙按近 30 天动量取 Top25 滚动优质池 (drop_sector=True 剔除主题/行业ETF)"),
    ("中性 regime", "当月月池; 缺失则回退全并集 (R3_UNIVERSE, 等价聚宽 ATTACK_UNIVERSE)"),
    ("双时点确认", "14:40 锁领头羊 + 14:45 复核仍≥3% (防尾盘脉冲, 不漏尾盘爆发赢家)"),
    ("卖出", "纯 TRIX(5,3) 死叉 (对齐 B, 弃用 hybrid)"),
    ("卖出窗口", "次日 09:40~11:05 (--sell-loop 每50秒, 11:05收盘fallback)"),
    ("运行模式", "SHADOW: 仅写独立状态/流水, 不下单"),
    ("滑点对照", "每笔记 theory_price(回测口径)/actual_src(1min·live·5min)/slippage_pp"),
]


# ── 渲染: 说明书 + 执行步骤 ──────────────────────────────────────────────────
def render_r3_manual() -> None:
    st.markdown('<div class="section-title">策略说明书</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="banner"><b>R3 (月度轮动) = 月度轮动质量池 + 双时点确认 + 纯TRIX(5,3)卖点</b><br>'
        f'<span style="color:#b9bccb;font-size:0.88rem;">{R3_ONELINE}</span></div>',
        unsafe_allow_html=True,
    )

    st.markdown("**关键参数**")
    pc = st.columns(2)
    for i, (k, v) in enumerate(R3_PARAMS):
        with pc[i % 2]:
            st.markdown(f'<div class="rule-kv"><b>{k}</b>: {v}</div>', unsafe_allow_html=True)

    st.markdown('<div class="section-title">每日执行步骤 (SHADOW · 仅记录不下单)</div>',
                unsafe_allow_html=True)
    for t, title, body in R3_STEPS:
        st.markdown(
            f'<div class="step-card"><span class="step-time">{t}</span>'
            f'<span class="step-title">{title}</span>'
            f'<div class="step-body">{body}</div></div>',
            unsafe_allow_html=True,
        )
    st.caption("cron 由 R3 专用脚本安装 (scripts/install_crontab.sh --install-r3); "
               "状态/流水写入独立文件 (不与实盘共用), 绝不读写实盘 t0_monitor 状态、绝不下单。")


# ── 渲染: 完整买卖规则 (详细版) ─────────────────────────────────────────────
def render_r3_rules() -> None:
    st.markdown('<div class="section-title">SHADOW 完整买卖规则 (详细版 · 与代码一字不差)</div>',
                unsafe_allow_html=True)

    st.markdown("**① 运行模式与隔离**")
    st.markdown(
        "- **SHADOW 影子策略**: 与实盘 `t0_monitor.py` 平行运行, **仅记录, 绝不真下单**, 不读写实盘 state / 流水。\n"
        "- **独立状态文件** (SHADOW 专用, 不与实盘共用): `~/.tradingagents/rotation/r3_shadow_state.json`。\n"
        "- **独立流水** (同上目录 `r3_journal.jsonl`)。\n"
        "- **资金模型**: 单笔资金; 信号未命中日直接空仓。\n"
        "- **每个持仓最多持有 1 天**: 次日 11:05 前平。"
    )

    with st.expander("**② 选股 + 买入 (14:40 `--pick` + 14:45 `--signal`)**", expanded=True):
        st.markdown("**扫描范围 (R3 月度轮动)**")
        st.markdown(
            "- 候选宇宙 = R3 月度轮动质量池 (jq_attack_pools.py 内联, 键=使用月/值=上月末 pool_as_of, 严格无未来函数)。\n"
            "- **趋势/震荡**: 从 R3 宇宙按近 30 天动量取 **Top25 滚动优质池** (drop_sector=True 剔除主题/行业ETF)。\n"
            "- **中性**: 当月月池; 当月池缺失时回退 **全并集 (R3_UNIVERSE, 等价聚宽 ATTACK_UNIVERSE)**。"
        )
        st.markdown("**双时点确认**")
        st.markdown(
            "- **14:40 (`--pick`) 锁领头羊**: 取当日涨幅最高的候选, 记候选 (不要求此刻≥3%, 允许尾盘爆发赢家)。\n"
            "- **14:45 (`--signal`) 复核**: 领头羊在 14:45 实时涨幅仍须 **≥ 3%** 才成交; 跌破则放弃 (防尾盘脉冲 + 必须尾盘仍强)。\n"
            "- 命中 → 写入 `position = {etf, name, type:'R3_动量精选'|'R3_月度轮动', buy_price, buy_date, today_gain, ...}`; 未命中 → 空仓。"
        )

    with st.expander("**③ 卖出 (次日 09:40~11:05 `--sell-loop` 每 50 秒)**", expanded=True):
        st.markdown("**监控窗口**")
        st.markdown(
            "- 窗口 **`09:40 ~ 11:05`**, 仅 `is_trix_sell_window` 内 (且为交易日) 才监控。\n"
            "- 循环: 每 `50` 秒调一次 `run_sell_check`; 平仓后提前退出。\n"
            "- 约束: **买入当日不卖** (`buy_date == today` 跳过, 须持到次日); 窗口外不卖。"
        )
        st.markdown("**纯 TRIX(5,3) 死叉卖点** (对齐 SHADOW B)")
        st.markdown(
            "- 拼接 `买入日昨日 5分K + 今日 5分K (至当前)`, 计算 `TRIX(周期5)` 与其 `signal 线 (周期3)`, 当 TRIX 下穿 signal 且首次出现在 `09:40~11:05` 内 → 触发即卖。\n"
            "- **兜底**: `09:40~11:05` 内未触发死叉 → `11:05` 收盘 **fallback 平仓** (`trix_time_sell_1105`)。"
        )
        st.markdown("**执行价、理论价与滑点对照**")
        st.markdown(
            "- 执行价 = `resolve_exec_prices` 在触发时点自动抓 1分K(优先) / 实时价 / 5分K收盘。\n"
            "- 理论价 `theory_price` = 回测口径成交价 (TRIX 死叉当根 5min 收盘 / 11:05 fallback 最近已完成 5min K 收盘)。\n"
            "- 落盘字段: `theory_price` / `theory_return_pct` / `actual_price_src`(1min·live·5min) / `slippage_pp`(实际-理论)。\n"
            "- 影子收益 `ret_num = (卖价 - 买价) / 买价 × 100 - 0.02%` (含费近似, 万3双边)。\n"
            "- 平仓后 `position` 置 `None`, 写流水 / 推送 (非 dry-run) 并 `sync_to_web()`。"
        )

    st.markdown("**④ 每日时间线 (cron)**")
    st.markdown(
        "| 时间 | 命令 | 动作 |\n"
        "|---|---|---|\n"
        "| 14:40 | `--pick` | 锁领头羊 (趋势/震荡=Top25滚动优质池; 中性=当月月池) |\n"
        "| 14:45 | `--signal` | 复核: 14:45 仍≥3% → R3_SHADOW 持仓; 否则空仓 |\n"
        "| 次日 09:40~11:05 | `--sell-loop` (每50秒) | 监控 纯 TRIX(5,3) 死叉卖点 |"
    )

    st.markdown("**⑤ 与聚宽对齐 (重要, 别混)**")
    st.markdown(
        "- 本地 SHADOW R3 已与聚宽 `joinquant_unified_single.py` 的 A 版 (产出 2014-2026 +1861%/夏普1.01) 逐字对齐: "
        "14:40 锁领头羊 + 14:45 复核 + 中性月池缺失回退全并集。\n"
        "- **聚宽 canonical +1861% / MDD-23.5% / 夏普1.01** = 三路在跑策略中回撤最低、夏普最高;"
        "本地 SHADOW 实际会选到与聚宽逐字对齐的标的, 可对照逐笔。\n"
        "- 日K 经 akshare fund_etf_hist_sina 拉取缓存于 `r3_daily.json` (每天至多一次); 缺失/网络失败降级为中性→全并集。\n"
        "- 聚宽 8 月初震荡静默期 (无≥3% 标的) 的 0 交易是设计内正常表现, 非 bug。"
    )

    st.caption("规则来源: scripts/t0_r3_monitor.py 常量; dry-run 下全部只读 (只打印不落盘/不推送)。")


# ── 渲染: 实时 SHADOW 状态 (嵌入页内) ────────────────────────────────────────
def render_r3_live(days: int = 60) -> None:
    st.markdown('<div class="section-title">实时 SHADOW 状态 (与实盘平行 · 仅记录)</div>',
                unsafe_allow_html=True)
    meta = r3_meta()
    data = load_r3_data(days=days)
    stats = data["stats"]
    state = data["state"]

    kpis = [
        ("R3 SHADOW 交易数", str(stats["trade_count"]), f"近{days}天"),
        ("R3 SHADOW 累计(影子)", f"{stats['compound_pct']:+.1f}%" if stats["compound_pct"] is not None else "—",
         "未实盘, 仅记录"),
        ("胜率", f"{stats['win_rate']:.0f}%" if stats["win_rate"] is not None else "—", ""),
        ("当前持仓", (open_position_row(state) or {}).get("标的", "空仓") or "空仓",
         (open_position_row(state) or {}).get("腿", "无")),
    ]
    grid = "".join(
        f'<div class="kpi"><div class="k">{l}</div><div class="v">{v}</div><div class="d">{d}</div></div>'
        for l, v, d in kpis
    )
    st.markdown(f'<div class="kpi-grid">{grid}</div>', unsafe_allow_html=True)

    sm = fmt_dt(meta.get("state_mtime"))
    st.caption(f"状态: `{meta['state_path']}` · 更新 {sm} · 流水 {meta['line_count']} 条 · "
               "⚠️ 影子策略, 仅模拟记录, 不下单")

    rows = closed_to_table_rows(data["closed"])
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("R3 SHADOW 暂无平仓记录 (刚启动或近%d天无交易)" % days)


# ── 渲染: 验证结论 ───────────────────────────────────────────────────────────
def render_r3_validation() -> None:
    st.markdown('<div class="section-title">验证结论 (聚宽 2014-2026 + 本地 SHADOW)</div>',
                unsafe_allow_html=True)
    with st.expander("📊 R3 策略验证结论 (去偏差 · 2026-08-12 对齐终态)"):
        for k, v in R3_WF_SUMMARY.items():
            st.markdown(f"- **{k}**: `{v}`")
        st.markdown("> **当前 R3 = 月度轮动质量池 (第3个在跑策略, 本地 SHADOW 实跑)**")
        st.markdown("> - **选股**: 趋势/震荡→R3宇宙按近30天动量取Top25滚动优质池; 中性→当月月池(缺失回退全并集R3_UNIVERSE)。")
        st.markdown("> - **卖点**: 纯 TRIX(5,3)死叉, 窗口 次日 09:40~11:05, 11:05 fallback (与SHADOW B一致)。")
        st.markdown("> - **双时点**: 14:40 锁领头羊 + 14:45 复核(要求仍≥3%), 防尾盘脉冲 + 不漏尾盘爆发赢家。")
        st.markdown("> - **聚宽 canonical +1861%/MDD-23.5%/夏普1.01** = 三路在跑策略中回撤最低、夏普最高。")
        st.markdown("> - **本地 SHADOW 实际会选到与聚宽逐字对齐的标的**, 可对照逐笔。")


# ── 整页 (供 3_R3策略.py 调用) ──────────────────────────────────────────
def render_r3_page() -> None:
    st.title("🟣 R3 (月度轮动) 策略")
    st.caption("月度轮动质量池 (R3) + 双时点确认 + 纯TRIX(5,3)卖点。与实盘平行运行, 仅记录不下单。"
               "本页含策略说明书、执行步骤与验证结论。")
    render_r3_manual()
    render_r3_rules()
    render_r3_live(days=60)
    render_r3_validation()

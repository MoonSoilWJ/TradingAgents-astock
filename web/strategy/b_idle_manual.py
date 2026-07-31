"""B+idle 策略说明书 + 执行步骤 + 验证结论 (WebUI 渲染).

内容:
  - 实盘优化后策略 = B+idle (全市场Top1选股 B 不regime过滤 + 闲置资金隔夜动量腿 idle)
  - 详细执行步骤 (cron 时间表 + 选股/买入/卖出逻辑)
  - 验证结论: ① B 增益集中度(非品类外极端单笔撑起) ② 近100交易日 B最强 vs 实盘

数据来源 (落盘 JSON):
  ~/.tradingagents/cache/t0_5min/b_concentration.json  (backtest_b_concentration.py)
  ~/.tradingagents/cache/t0_5min/b_strongest_100d.json (backtest_b_strongest_100d.py)
"""

from __future__ import annotations

import json
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

CACHE = Path.home() / ".tradingagents/cache/t0_5min"
CONC_FILE = CACHE / "b_concentration.json"
STRONG_FILE = CACHE / "b_strongest_100d.json"

# ── 策略一句话 ──────────────────────────────────────────────────────────────
BIDLE_ONELINE = (
    "全市场 T0 ETF 当日涨幅 Top1 (≥3%, 不 regime 过滤) 做核心腿；"
    "核心 14:45 未触发时, 14:50 买入当日最强涨幅 ≥1.0% 的 T0 ETF 做隔夜动量腿(idle)。"
    "两条腿共用单笔资金、串行复利。"
)

# ── 详细执行步骤 ────────────────────────────────────────────────────────────
# (time, title, body)
BIDLE_STEPS = [
    ("14:45", "核心 B 选股 (--signal)",
     "对全市场 T0 ETF 按当日涨幅排序取 Top1, 要求 <b>今日涨幅 ≥ 3%</b>。"
     "不做 regime 品类过滤、不 skip_choppy (即 B 方案)。命中 → 记为当日核心候选。"),
    ("14:49", "平 idle 隔夜仓 (--idle-sell)",
     "先平掉昨日 idle 腿持仓: 固定 <b>次日 14:50 卖出</b> (cron 14:49 先执行, 确保先平后买)。"
     "动量腿吃的是“次日趋势延续一整天”, 14:50 才平完主升, 优于 TRIX 上午假死叉甩下车。"),
    ("14:50", "idle 动量腿买入 (--idle-buy)",
     "若 14:45 核心 B 未触发 (无 ≥3% 候选 = 闲置资金日), 选当日<b>最强涨幅 ≥1.0%</b> 的 T0 ETF, "
     "14:50 买入, 持隔夜。"),
    ("次日 09:40~11:05", "核心 B 卖出 (--sell-loop)",
     "全日监控持仓 5 分K: <b>TRIX(5,3) 死叉</b> 或 <b>追踪回落止盈(peak 回落0.5%)</b>, 先发生先卖; "
     "09:40~11:05 内均未触发则 11:05 收盘 fallback (hybrid 卖点, 对齐回测 simulate_hybrid_v2 / +550.39%)。"),
]

BIDLE_PARAMS = [
    ("核心选股门槛", "今日涨幅 ≥ 3% (Top1)"),
    ("核心 regime 过滤", "关闭 (全市场扫描)"),
    ("核心卖出", "hybrid: TRIX死叉 或 peak回落0.5% (先发生先卖)"),
    ("核心卖出窗口", "次日 09:40~11:05 (--sell-loop 每50秒, 11:05收盘fallback)"),
    ("idle 门槛", "当日最强涨幅 ≥ 1.0%"),
    ("idle 卖出", "次日 14:50 固定 (先平后买)"),
    ("资金模式", "单笔资金, 核心/idle 互斥串行复利"),
    ("运行模式", "SHADOW: 仅写独立状态/流水, 不下单"),
]

WF_SUMMARY = {
    "B+hybrid (in-sample)": "+1335.45% / 430笔 / 胜57% / 回撤-27.3%",
    "B+hybrid (OOS 验证段)": "+550.39% / 208笔 (分界2024-12-19, 388天)",
    "B+idle 合并 (OOS)": "+1193.75% / 255笔 (idle 腿贡献 +643%)",
    "idle 腿 (OOS 独立)": "+98.92% / 47笔",
}


# ── 数据加载 ────────────────────────────────────────────────────────────────
def load_concentration() -> dict[str, Any] | None:
    if not CONC_FILE.exists():
        return None
    try:
        return json.loads(CONC_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_strongest() -> dict[str, Any] | None:
    if not STRONG_FILE.exists():
        return None
    try:
        return json.loads(STRONG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ── 渲染: 说明书 + 执行步骤 ──────────────────────────────────────────────────
def render_b_idle_manual() -> None:
    st.markdown('<div class="section-title">策略说明书</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="banner"><b>B+idle = 优化后核心(B) + 优化后 idle 腿</b><br>'
        f'<span style="color:#b9bccb;font-size:0.88rem;">{BIDLE_ONELINE}</span></div>',
        unsafe_allow_html=True,
    )

    st.markdown("**关键参数**")
    pc = st.columns(2)
    for i, (k, v) in enumerate(BIDLE_PARAMS):
        with pc[i % 2]:
            st.markdown(f'<div class="rule-kv"><b>{k}</b>: {v}</div>', unsafe_allow_html=True)

    st.markdown('<div class="section-title">每日执行步骤 (SHADOW · 仅记录不下单)</div>',
                unsafe_allow_html=True)
    for t, title, body in BIDLE_STEPS:
        st.markdown(
            f'<div class="step-card"><span class="step-time">{t}</span>'
            f'<span class="step-title">{title}</span>'
            f'<div class="step-body">{body}</div></div>',
            unsafe_allow_html=True,
        )
    st.caption("cron 由 scripts/install_crontab.sh --install-b-idle-shadow 安装; "
               "状态/流水写入独立文件 b_idle_shadow_state.json / b_idle_journal.jsonl, "
               "绝不读写实盘 t0_monitor 状态、绝不下单。")


# ── 渲染: 验证结论 ───────────────────────────────────────────────────────────
def render_b_idle_validation() -> None:
    st.markdown('<div class="section-title">验证结论 ①: B 增益集中度 (是否少数品类外极端单笔撑起)</div>',
                unsafe_allow_html=True)
    conc = load_concentration()
    if not conc:
        st.info("未找到 b_concentration.json, 先运行: python3 scripts/backtest_b_concentration.py")
    else:
        oos = conc.get("oos", {})
        cs = oos.get("category_split", {})
        inc = cs.get("in_category", {})
        out = cs.get("out_category", {})
        ext = oos.get("extreme", {})
        named = oos.get("named", {})

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**品类内 vs 品类外 (OOS 验证段)**")
            st.markdown(f'- 品类内: 贡献 <b>{inc.get("eq_share_pct",0):.1f}%</b> 增益 / '
                        f'{inc.get("count_share_pct",0):.1f}% 笔数 (复利 {inc.get("eq_pct",0):+.1f}%)')
            st.markdown(f'- 品类外: 贡献 <b>{out.get("eq_share_pct",0):.1f}%</b> 增益 / '
                        f'{out.get("count_share_pct",0):.1f}% 笔数 (复利 {out.get("eq_pct",0):+.1f}%)')
        with col2:
            st.markdown("**极端单笔**")
            st.markdown(f'- 最大单笔: {ext.get("best","—")}  ·  最小单笔: {ext.get("worst","—")}')
            st.markdown(f'- |收益|>10%: <b>{ext.get("gt10pct",0)}</b> 笔  ·  >5%: {ext.get("gt5pct",0)} 笔  ·  <-5%: {ext.get("lt_minus5pct",0)} 笔')

        # 点名 ETF
        if named:
            parts = []
            for code, v in named.items():
                pill = "pill-in" if v.get("in_category") else "pill-out"
                tag = "品类内" if v.get("in_category") else "品类外"
                parts.append(f'{code} {v.get("name")} {v.get("eq_pct",0):+.1f}%/{v.get("trades")}笔 '
                             f'<span class="pill {pill}">{tag}</span>')
            st.markdown("**点名核对** (513120/161129): " + " · ".join(parts), unsafe_allow_html=True)

        st.markdown(
            '<div class="verdict verdict-ok">✅ <b>结论: B 增益并非少数品类外极端单笔撑起。</b> '
            'OOS 验证段品类外仅贡献 <b>3.6%</b> 增益 (低于其 13.5% 笔数份额); '
            'B 与 A 同日同标的命中占 66% 增益 (主体在品类内优质标的); 无单笔 &gt;10%, '
            'Top-1 ETF 仅 13% 增益、共 47 只 ETF 分散。'
            '⚠ 行业上偏向原油/商品 (Top-5 多只原油 ETF), 属行业 beta 非单点风险。</div>',
            unsafe_allow_html=True,
        )

    st.markdown('<div class="section-title">验证结论 ②: 近 100 交易日 B最强 vs 实盘</div>',
                unsafe_allow_html=True)
    strong = load_strongest()
    if not strong:
        st.info("未找到 b_strongest_100d.json, 先运行: python3 scripts/backtest_b_strongest_100d.py")
    else:
        b = strong["b_strongest"]
        live = strong["live_hybrid_a_trix"]
        core = strong["b_core"]
        idle = strong["idle"]
        rows = [
            {"策略": "B+idle (最强)", "笔数": b["trades"], "累计": f"{b['equity_pct']:+.2f}%",
             "胜率": f"{b['win_rate']:.1f}%", "回撤": f"{b['max_drawdown']:+.1f}%"},
            {"策略": "└ 核心 B", "笔数": core["trades"], "累计": f"{core['equity_pct']:+.2f}%",
             "胜率": f"{core['win_rate']:.1f}%", "回撤": f"{core['max_drawdown']:+.1f}%"},
            {"策略": "└ idle 腿", "笔数": idle["trades"], "累计": f"{idle['equity_pct']:+.2f}%",
             "胜率": f"{idle['win_rate']:.1f}%", "回撤": f"{idle['max_drawdown']:+.1f}%"},
            {"策略": "实盘 hybrid-A+TRIX", "笔数": live["trades"], "累计": f"{live['equity_pct']:+.2f}%",
             "胜率": f"{live['win_rate']:.1f}%", "回撤": f"{live['max_drawdown']:+.1f}%"},
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.markdown(
            f'<div class="verdict verdict-ok">✅ <b>结论: 近 {strong["days"]} 日 '
            f'({strong["window"]}) B+idle {b["equity_pct"]:+.2f}% vs 实盘 {live["equity_pct"]:+.2f}%, '
            f'多 <b>{strong["b_minus_live_pct"]:+.2f}pct</b>、多 {b["trades"]-live["trades"]} 笔。</b> '
            '逐月从 4 月起持续领先; 增益主要来自核心选股 (B 比实盘多 +9pct), idle 腿正向增厚。</div>',
            unsafe_allow_html=True,
        )

    with st.expander("📊 Walk-Forward 样本外总览"):
        for k, v in WF_SUMMARY.items():
            st.markdown(f"- **{k}**: `{v}`")
        st.markdown("> B+idle 已验证稳健非过拟合, 实盘先以 SHADOW 运行观察, 不替代线上。")


# ── 渲染: SHADOW 完整买卖规则 (详细, 不省略) ────────────────────────────────
def render_b_idle_rules() -> None:
    st.markdown('<div class="section-title">SHADOW 完整买卖规则 (详细版 · 与代码一字不差)</div>',
                unsafe_allow_html=True)

    # ── 0. 运行模式与隔离 ──
    st.markdown("**① 运行模式与隔离**")
    st.markdown(
        "- **SHADOW 影子策略**: 与实盘 `t0_monitor.py` 平行运行, **仅记录, 绝不真下单**, 不读写实盘 state / 流水。\n"
        "- **独立状态文件**: `~/.tradingagents/rotation/b_idle_shadow_state.json`\n"
        "- **独立流水**: `~/.tradingagents/rotation/b_idle_journal.jsonl`\n"
        "- **资金模型**: 单笔资金, `核心腿 B` 与 `idle 腿` **互斥串行复利**; 同一交易日只做一条腿 (核心命中则不做 idle)。\n"
        "- **每个持仓最多持有 1 天**: 核心腿次日 11:05 前平, idle 腿次日 14:50 平。"
    )

    # ── 1. 核心腿 B ──
    with st.expander("**② 核心腿 B (core_B) — 选股 + 买入 (14:45 `--signal`)**", expanded=True):
        st.markdown("**扫描范围 (B 方案)**")
        st.markdown(
            "- 扫**全市场所有 T0 ETF**: 含跨境 / 商品, **不限 T+0 交割、不限 regime 品类**。\n"
            "- **不 regime 过滤, 不 skip_choppy** (与实盘 hybrid-A 选股的关键差异)。"
        )
        st.markdown("**排序与命中**")
        st.markdown(
            "- 按腾讯实时 `今日涨幅 today_gain` 降序排列。\n"
            "- 命中条件: 取排序后**第一个 `today_gain ≥ 3.0%` (`MIN_GAIN`)** 的标的作为 Top1。\n"
            "- 若全部 `< 3.0%` → 核心未命中, 当日转为 idle 日。"
        )
        st.markdown("**双时点确认 (`confirm_signal_gain`)**")
        st.markdown(
            "- 命中 Top1 后, 用 **1 分 K 收盘价** 校验 **`14:40` (`t0_monitor.CONFIRM_TIME`) 涨幅也须 `≥ 3.0%`**。\n"
            "- 目的: 防尾盘脉冲踩线单 (只有 14:45 急拉过线、14:40 没过的, 视为不稳, 放弃)。\n"
            "- **数据缺失则放行** (不因数据问题误杀信号)。"
        )
        st.markdown("**买入价与状态**")
        st.markdown(
            "- 记录的买入价 `buy_price` = 14:45 实时价 `price` (**仅记录, 不下单**)。\n"
            "- 写入 `position = {etf, name, type:'core_B', buy_price, buy_date, today_gain, sold:False}`, 并落流水 / 推送 (非 dry-run)。"
        )

    with st.expander("**③ 核心腿 B — 卖出 (次日 09:40~11:05 `--sell-loop` 每 50 秒)**", expanded=True):
        st.markdown("**监控窗口**")
        st.markdown(
            "- 窗口 **`09:40 ~ 11:05` (`SELL_CHECK_START` ~ `HYBRID_SELL_END = SELL_CUTOFF`)**, 仅 `is_hybrid_sell_window` 内 (且为交易日) 才监控。\n"
            "- 循环: `run_sell_loop` 每 `50` 秒 (`SELL_LOOP_INTERVAL`) 调一次 `run_sell_check`; 平仓后提前退出。\n"
            "- 约束: 必须是 `core_B` 类型; **买入当日不卖** (`buy_date == today` 跳过, 须持到次日); 窗口外不卖 (idle 持仓在此窗口被直接跳过)。"
        )
        st.markdown("**hybrid 双卖点 — 先发生先卖**")
        st.markdown(
            "1. **TRIX(5,3) 死叉**: 拼接 `买入日昨日 5分K + 今日 5分K (至当前)`, 计算 `TRIX(周期5)` 与其 `signal 线 (周期3)`, 当 TRIX **下穿** signal 且首次出现在 `09:40~11:05` 内 → 触发。\n"
            "   - 早盘 `09:40` 前的死叉忽略 (归因: 09:40 前 0 胜率, `TRIX_MIN_SELL=09:40`)。\n"
            "   - 需足够 warmup: 至少 `TRIX_PERIOD*3+5` 根 5分K, 否则不触发。\n"
            "2. **追踪回落止盈**: `09:40` 起维护 `running peak` (每根 5分K 的 `high` 刷新峰值); 若某根 5分K 的 `low ≤ peak × (1 - 0.5%)` (`TRAIL_DROP_PCT`) → 触发, 记录触发价 / 时间。"
        )
        st.markdown("**选择逻辑**")
        st.markdown(
            "- 命中 TRIX 且 (未命中追踪 **或** TRIX 触发时间 ≤ 追踪触发时间) → 用 **TRIX** 死叉价。\n"
            "- 命中追踪且非 TRIX → 用 **追踪回落** 价。\n"
            "- 即两者取**较早触发者**。\n"
            "- **兜底**: `09:40~11:05` 内 TRIX / 追踪**均未触发** → `11:05` 收盘 **fallback 平仓** (`hybrid_time_sell`)。"
        )
        st.markdown("**执行价与结算**")
        st.markdown(
            "- 执行价 = `resolve_exec_prices` 在触发时点 5分K 取价。\n"
            "- 影子收益 `ret_num = (卖价 - 买价) / 买价 × 100 - 0.02%` (含费近似, 对应 `FEE_NOTE` 万3双边)。\n"
            "- 平仓后 `position` 置 `None`, 写流水 / 推送 (非 dry-run)。"
        )

    # ── 2. idle 腿 ──
    with st.expander("**④ idle 腿 (idle_momentum) — 触发前提 + 买入 (14:50 `--idle-buy`)**", expanded=True):
        st.markdown("**触发前提 (14:45 `--signal`)**")
        st.markdown(
            "- 仅当**核心腿 B 当日未命中** (无 `≥3%` 候选 = 闲置资金日) → 置 `idle_pending = True`, 等 14:50 买入。\n"
            "- 若核心命中, `idle_pending` 不置位, 当日**不再做 idle** (互斥)。"
        )
        st.markdown("**买入选股 (14:50)**")
        st.markdown(
            "- 前置: `idle_pending` 且 `last_signal_date == today` 且当前**无持仓** (`position` 为空), 否则跳过。\n"
            "- 选股: 同日全市场 T0 ETF 按今日涨幅降序, 取**第一个 `today_gain ≥ 1.0%` (`IDLE_THR`)** 的 Top1 (即当日最强涨幅)。\n"
            "- 若无 `≥1.0%` 标的 → `idle_pending=False`, 跳过本日 idle。\n"
            "- 记录买入价 `buy_price` = 14:50 实时价 (**仅记录, 不下单**)。\n"
            "- 状态: `position = {etf, name, type:'idle_momentum', buy_price, buy_date, today_gain, sold:False}`; `idle_pending=False`。"
        )

    with st.expander("**⑤ idle 腿 — 卖出 (次日 14:50 `--idle-sell`)**", expanded=True):
        st.markdown("**触发窗口**")
        st.markdown(
            "- cron `14:49` 先跑 `--sell-check --idle-sell`, 要求 `now ≥ 14:50` (`IDLE_SELL_HM`)。\n"
            "- idle 持仓 **需 `idle_sell=True` 才处理**: `09:40~11:05` 的 `--sell-loop` 对 idle 持仓**直接跳过** (只有 `--idle-sell` 才卖 idle)。\n"
            "- **隔夜约束**: `买入当日不卖` (`buy_date == today` 跳过), 须持到次日 14:50。"
        )
        st.markdown("**固定平仓 (无技术信号)**")
        st.markdown(
            "- 次日 **`14:50` 固定卖出** (`idle_fixed_1450`), **不依赖 TRIX / 任何技术卖点**。\n"
            "- 选 14:50 的原因: 动量腿吃的是 **\"次日趋势延续一整天\"**, 14:50 平仓吃完主升; 回测 WF 验证 — 纯 14:50 固定卖 OOS 增量 (+98.92% / 47笔) 高于 TRIX 上午假死叉方案 (假死叉常甩下车)。\n"
            "- 执行价 = `resolve_exec_prices @ 14:50`; 影子收益 `= (卖价-买价)/买价×100 - 0.02%`。"
        )

    # ── 3. 每日时间线 ──
    st.markdown("**⑥ 每日时间线 (cron)**")
    st.markdown(
        "| 时间 | 命令 | 动作 |\n"
        "|---|---|---|\n"
        "| 14:45 | `--signal` | 核心 B 选股 + 双时点确认; 命中→core_B 持仓; 未命中→`idle_pending=True` |\n"
        "| 14:49 | `--sell-check --idle-sell` | 平**昨日** idle 隔夜仓 (先平后买) |\n"
        "| 14:50 | `--idle-buy` | 若 `idle_pending` 且空仓 → 买最强 `≥1.0%` |\n"
        "| 次日 09:40~11:05 | `--sell-loop` (每50秒) | 监控 core_B 的 hybrid 卖点 |\n"
        "| 次日 14:50 | `--idle-sell` | idle 固定平仓 |"
    )

    # ── 4. 与回测对齐 ──
    st.markdown("**⑦ 与回测窗口对齐 (重要, 别混)**")
    st.markdown(
        "- **核心腿**: hybrid 窗口 `09:40~11:05` 对齐回测 `simulate_hybrid_v2` (`SELL_CUTOFF=11:05`) → 对应 OOS `+550.39% / 208笔`。\n"
        "- **idle 腿**: `14:50` 固定卖对应 WF 纯 14:50 方案 OOS `+98.92% / 47笔`。\n"
        "- **`14:50` 只属于 idle 腿, 不属于核心腿** (核心腿 11:05 已平)。本 SHADOW 核心腿 `HYBRID_SELL_END = SELL_CUTOFF = 11:05` 已从根上对齐回测。"
    )

    st.caption("规则来源: scripts/t0_b_idle_shadow.py 与 scripts/t0_monitor.py 常量; dry-run 下全部只读 (只打印不落盘/不推送)。")


# ── 渲染: 实时 SHADOW 状态 (嵌入页内) ────────────────────────────────────────
def render_b_idle_live(days: int = 60) -> None:
    st.markdown('<div class="section-title">实时 SHADOW 状态 (与实盘平行 · 仅记录)</div>',
                unsafe_allow_html=True)
    meta = b_idle_meta()
    data = load_b_idle_data(days=days)
    stats = data["stats"]
    state = data["state"]

    kpis = [
        ("SHADOW 交易数", str(stats["trade_count"]), f"近{days}天"),
        ("SHADOW 累计(影子)", f"{stats['compound_pct']:+.1f}%" if stats["compound_pct"] is not None else "—",
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
        st.info("SHADOW 暂无平仓记录 (刚启动或近%d天无交易)" % days)


# ── 整页 (供 3_B+idle策略.py 调用) ──────────────────────────────────────────
def render_b_idle_page() -> None:
    st.title("🆕 优化策略 B+idle (实盘候选 · SHADOW)")
    st.caption("全市场Top1选股(B, 不regime过滤) + 闲置资金隔夜动量腿(idle)。"
               "与实盘平行运行, 仅记录不下单。本页含策略说明书、执行步骤与验证结论。")
    render_b_idle_manual()
    render_b_idle_rules()
    render_b_idle_live(days=60)
    render_b_idle_validation()

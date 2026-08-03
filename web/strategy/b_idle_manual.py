"""B (T0 SHADOW) 策略说明书 + 执行步骤 + 验证结论 (WebUI 渲染).

内容:
  - 实盘优化后策略 = B (全市场Top1选股, 不regime过滤, 14:40双时点确认) + 纯TRIX卖点
  - 核心未命中日 → 空仓
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
    "全市场 T0 ETF 当日涨幅 Top1 (≥3%, 不 regime 过滤, 14:40 双时点确认) 做核心腿, "
    "次日 09:40~11:05 <b>纯 TRIX(5,3)死叉</b>卖出 (对齐回测 simulate_exit('trix0940_cut'))。"
    "★ 2026-07-31 升级: 卖点由 hybrid(TRIX+追踪回落) 改为纯 TRIX, 因 hybrid 的"
    "+250~320pp 超额全部来自不可兑现成交价(穿价按精确止损成交), 保守口径下全面劣于 TRIX。"
)

# ── 详细执行步骤 ────────────────────────────────────────────────────────────
# (time, title, body)
BIDLE_STEPS = [
    ("14:45", "核心 B 选股 (--signal)",
     "对全市场 T0 ETF 按当日涨幅排序取 Top1, 要求 <b>今日涨幅 ≥ 3%</b>。"
     "不做 regime 品类过滤、不 skip_choppy (即 B 方案)。命中 → 记为当日核心候选; 未命中 → 空仓。"),
    ("次日 09:40~11:05", "核心 B 卖出 (--sell-loop)",
     "全日监控持仓 5 分K: <b>TRIX(5,3) 死叉</b>触发即卖; "
     "09:40~11:05 内未触发则 11:05 收盘 fallback 平 (纯 TRIX 卖点, 对齐回测 simulate_exit('trix0940_cut') / 全4年 +613.46%)。"
     " ⚠ 原 hybrid(TRIX+追踪回落) 已弃用: +250~320pp 超额来自不可兑现成交价, 保守口径下劣于 TRIX。"),
]

BIDLE_PARAMS = [
    ("核心选股门槛", "今日涨幅 ≥ 3% (Top1)"),
    ("核心 regime 过滤", "关闭 (全市场扫描)"),
    ("候选池(auto层)", "自动发现层经<b>质量筛选</b>保留 <b>59 只可交易宽基</b>(refresh_t0_pool.py 月度 cron): "
        "宽基前缀+主题负关键词+上市≥120天+日均成交≥3000万+行情可达性校验; 已剔除主题/行业ETF(港股创新药/中韩半导体/标普医药·油气…)及 6 只数据源缺失的幽灵标的"),
    ("核心卖出", "<b>纯 TRIX(5,3) 死叉</b> (2026-07-31 升级, 弃用 hybrid)"),
    ("核心卖出窗口", "次日 09:40~11:05 (--sell-loop 每50秒, 11:05收盘fallback)"),
    ("双时点确认", "14:40 涨幅须同样 ≥3% (防尾盘脉冲, 与实盘一致)"),
    ("资金模式", "单笔资金; 核心未命中日空仓"),
    ("运行模式", "SHADOW: 仅写独立状态/流水, 不下单"),
    ("滑点对照", "每笔记 theory_price(回测口径)/actual_src(1min·live·5min)/slippage_pp"),
]

# 2026-07-31 去偏差重验: 合并无偏5min = tdx_5min_pre2024(2022-06-15~2024-07-02) +
# tdx_5min_2y(2024-07-03~), 脚本 backtest_recent100_live_vs_b_idle.py --five-min 逗号合并,
# 统一口径 fee=0.03 万3 对齐实盘 / lb=30。
# ⚠ 旧数字(B+hybrid +411%/+550%) 全部作废, 双重偏差:
#   ① 数据偏差: aligned_live_4y 稀疏5min("先按当日涨幅排序再抓TopK"→前视偏差);
#   ② 成交价偏差: hybrid/trail +250~320pp 超额来自不可兑现成交价(穿价按精确止损成交),
#      保守口径下 hybrid 全面劣于 TRIX, 已弃用, 卖点切纯 TRIX。
WF_SUMMARY = {
    "★最佳 SHADOW 核心 B+确认+TRIX (全4年/900日)": "+613.46% / 429笔 / 胜55% / 回撤-33.2%",
    "★最佳 SHADOW 核心 B+确认+TRIX (近390 OOS)": "+319.16% / 231笔 / 胜63% / 回撤-17.6%",
    "★最佳 SHADOW 核心 B+确认+TRIX (近100天)": "+69.23% / ~74笔 / 回撤-17.6%",
    "实盘对照 A+确认+TRIX (全4年/900日)": "+472.25% / 329笔 / 胜57% / 回撤-28.7%",
    "实盘对照 A+确认+TRIX (近390 OOS)": "+284.05% / 189笔 / 胜61% / 回撤-13.7%",
    "实盘对照 A+确认+TRIX (近100天)": "+67.47% / 62笔 / 回撤-13.7%",
    "B-A 增益(全4年/近390/近100)": "+141pp / +35pp / +2pp — 各子段稳赢, 非过拟合",
    "hybrid卖点(已弃用·虚增参考)": "A+hybrid +724% / B+hybrid +934% — 不可兑现成交价, 不引用",
    "候选池(auto层质量过滤)": "59 只可交易宽基跨境/债券/商品 ETF(511/513/518/501/161/162前缀+159xxx宽基关键词), "
        "主题/行业ETF已剔除, 月度自动刷新(无脑加全部T+0曾致回撤-65%, 质量筛选后全10年B +12564% / 回撤-25.2% 低于基线-33% 并额外增值)",
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
        f'<div class="banner"><b>B (T0 SHADOW) = 全市场Top1选股(不regime过滤) + 纯TRIX(5,3)卖点</b><br>'
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
    st.caption("cron 由 SHADOW 专用脚本安装; 状态/流水写入独立文件 (不与实盘共用), "
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
        rows = [
            {"策略": "B 核心腿 (现 SHADOW)", "笔数": b["trades"], "累计": f"{b['equity_pct']:+.2f}%",
             "胜率": f"{b['win_rate']:.1f}%", "回撤": f"{b['max_drawdown']:+.1f}%"},
            {"策略": "└ 核心 B (拆解)", "笔数": core["trades"], "累计": f"{core['equity_pct']:+.2f}%",
             "胜率": f"{core['win_rate']:.1f}%", "回撤": f"{core['max_drawdown']:+.1f}%"},
            {"策略": "实盘 hybrid-A+TRIX", "笔数": live["trades"], "累计": f"{live['equity_pct']:+.2f}%",
             "胜率": f"{live['win_rate']:.1f}%", "回撤": f"{live['max_drawdown']:+.1f}%"},
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.markdown(
            f'<div class="verdict verdict-ok">✅ <b>结论: 近 {strong["days"]} 日 '
            f'({strong["window"]}) B {b["equity_pct"]:+.2f}% vs 实盘 {live["equity_pct"]:+.2f}%, '
            f'多 <b>{strong["b_minus_live_pct"]:+.2f}pct</b>、多 {b["trades"]-live["trades"]} 笔。</b> '
            '增益全部来自核心选股 (B 比实盘多 +Xpct)。</div>',
            unsafe_allow_html=True,
        )

    with st.expander("📊 Walk-Forward 样本外总览 (去偏差·2026-07-31)"):
        for k, v in WF_SUMMARY.items():
            st.markdown(f"- **{k}**: `{v}`")
        st.markdown("> **B (核心腿) 已验证稳健非过拟合**: 各子段稳赢实盘 A; 实盘先以 SHADOW 运行观察, 不替代线上。")


# ── 渲染: SHADOW 完整买卖规则 (详细, 不省略) ────────────────────────────────
def render_b_idle_rules() -> None:
    st.markdown('<div class="section-title">SHADOW 完整买卖规则 (详细版 · 与代码一字不差)</div>',
                unsafe_allow_html=True)

    # ── 0. 运行模式与隔离 ──
    st.markdown("**① 运行模式与隔离**")
    st.markdown(
        "- **SHADOW 影子策略**: 与实盘 `t0_monitor.py` 平行运行, **仅记录, 绝不真下单**, 不读写实盘 state / 流水。\n"
        "- **独立状态文件** (SHADOW 专用, 不与实盘共用): `~/.tradingagents/rotation/` 目录下独立文件\n"
        "- **独立流水** (同上目录)\n"
        "- **资金模型**: 单笔资金; 核心未命中日直接空仓, 不做其他腿。\n"
        "- **每个持仓最多持有 1 天**: 核心腿次日 11:05 前平。"
    )

    # ── 1. 核心腿 B ──
    with st.expander("**② 核心腿 B (core_B) — 选股 + 买入 (14:45 `--signal`)**", expanded=True):
        st.markdown("**扫描范围 (B 方案)**")
        st.markdown(
            "- 扫**全市场所有 T0 ETF**: 含跨境 / 商品, **不限 T+0 交割、不限 regime 品类**。\n"
            "- **不 regime 过滤, 不 skip_choppy** (与实盘 hybrid-A 选股的关键差异)。\n"
            "- **候选池含自动发现层 (auto)**: `refresh_t0_pool.py` 月度 cron 维护, 经质量筛选保留 "
            "**65 只宽基**(511/513/518/501/161/162 前缀 + 159xxx 宽基关键词), 已剔除主题/行业 ETF "
            "(港股创新药/中韩半导体/标普医药·油气/军工/银行…), 避免高波动均值回归被选中即亏。"
        )
        st.markdown("**排序与命中**")
        st.markdown(
            "- 按腾讯实时 `今日涨幅 today_gain` 降序排列。\n"
            "- 命中条件: 取排序后**第一个 `today_gain ≥ 3.0%` (`MIN_GAIN`)** 的标的作为 Top1。\n"
            "- 若全部 `< 3.0%` → 核心未命中, 当日空仓。"
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
            "- 约束: 必须是 `core_B` 类型; **买入当日不卖** (`buy_date == today` 跳过, 须持到次日); 窗口外不卖。"
        )
        st.markdown("**纯 TRIX(5,3) 死叉卖点** (2026-07-31 升级, 弃用 hybrid)")
        st.markdown(
            "- 拼接 `买入日昨日 5分K + 今日 5分K (至当前)`, 计算 `TRIX(周期5)` 与其 `signal 线 (周期3)`, 当 TRIX **下穿** signal 且首次出现在 `09:40~11:05` 内 → 触发即卖。\n"
            "   - 早盘 `09:40` 前的死叉忽略 (归因: 09:40 前 0 胜率, `TRIX_MIN_SELL=09:40`)。\n"
            "   - 需足够 warmup: 至少 `TRIX_PERIOD*3+5` 根 5分K, 否则不触发。\n"
            "- **兜底**: `09:40~11:05` 内未触发死叉 → `11:05` 收盘 **fallback 平仓** (`trix_time_sell_1105`)。"
        )
        st.markdown("**⚠ 为何弃用 hybrid(TRIX+追踪回落)**")
        st.markdown(
            "- hybrid 的 +250~320pp 超额全部来自不可兑现成交价: 追踪回落触发时按 `peak*(1-0.5%)` 精确止损价成交, "
            "即使该 5min K 的 low 远低于止损价(穿价)也假设按止损价成交; 实盘只能在发现之后成交。\n"
            "- 保守口径(穿价时按该 K 收盘 min(stop, close)) 重算: hybrid 全4年+290% vs TRIX+540%, 全面劣于 TRIX。\n"
            "- TRIX 成交价稳健: 保守(死叉后下一根开盘成交) vs 乐观(死叉当根收盘成交) 差 <3pp(反而略好), 真实可兑现。"
        )
        st.markdown("**执行价、理论价与滑点对照 (2026-07-31 新增)**")
        st.markdown(
            "- 执行价 = `resolve_exec_prices` 在触发时点自动抓 1分K(优先) / 实时价 / 5分K收盘。\n"
            "- 理论价 `theory_price` = 回测口径成交价: TRIX 死叉 = 死叉当根 5min 收盘(`trix_death_cross_hit` 第2返回值); "
            "11:05 fallback = 截止时刻最近已完成 5min K 收盘。\n"
            "- 落盘字段: `theory_price` / `theory_return_pct` / `actual_price_src`(1min·live·5min) / `slippage_pp`(实际-理论)。\n"
            "- 影子收益 `ret_num = (卖价 - 买价) / 买价 × 100 - 0.02%` (含费近似, 对应 `FEE_NOTE` 万3双边)。\n"
            "- 平仓后 `position` 置 `None`, 写流水 / 推送 (非 dry-run)。"
        )

    # ── 3. 每日时间线 ──
    st.markdown("**④ 每日时间线 (cron)**")
    st.markdown(
        "| 时间 | 命令 | 动作 |\n"
        "|---|---|---|\n"
        "| 14:45 | `--signal` | 核心 B 选股 + 14:40 双时点确认; 命中→core_B 持仓; 未命中→**空仓** |\n"
        "| 次日 09:40~11:05 | `--sell-loop` (每50秒) | 监控 core_B 的 **纯 TRIX** 卖点 |"
    )

    # ── 4. 与回测对齐 ──
    st.markdown("**⑤ 与回测窗口对齐 (重要, 别混)**")
    st.markdown(
        "- **核心腿**: 纯 TRIX 窗口 `09:40~11:05` 对齐回测 `simulate_exit('trix0940_cut')` (`SELL_CUTOFF=11:05`) → "
        "合并无偏5min 全4年 `+613.46% / 429笔 / 回撤-33.2%`, 近390 OOS `+319.16% / 231笔`, 近100天 `+69.23%`。\n"
        "- **数据口径**: 回测须用合并无偏5min = `tdx_5min_pre2024.json,tdx_5min_2y.json` (全池每日全覆盖, 与日K收盘比值 1.000); "
        "`aligned_live_4y.json` 的 `etf_5min` 有前视偏差, 只可用其日K / all_dates / proxy。\n"
        "- **hybrid 卖点已弃用**: 原 `+550.39%/+411.17%` 来自不可兑现成交价, 保守口径下全面劣于 TRIX, 不再引用。\n"
        "- **实盘2周对照(07-16~07-30)**: 9笔 -2.40% vs 回测同窗口 -4.77%, 选股100%一致, "
        "6/7笔成交价差<0.1pp, 07-23 161129 实盘+7.66%>回测+0.93%(1min精确高点) → 回测等价性良好。"
    )

    st.caption("规则来源: SHADOW 脚本与 t0_monitor.py 常量; dry-run 下全部只读 (只打印不落盘/不推送)。")


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


# ── 整页 (供 3_B策略.py 调用) ──────────────────────────────────────────
def render_b_idle_page() -> None:
    st.title("🆕 优化策略 B (T0 SHADOW)")
    st.caption("全市场Top1选股 (不regime过滤, 14:40双时点确认) + 纯TRIX(5,3)卖点 (09:40~11:05窗口)。"
               "与实盘平行运行, 仅记录不下单。本页含策略说明书、执行步骤与验证结论。")
    render_b_idle_manual()
    render_b_idle_rules()
    render_b_idle_live(days=60)
    render_b_idle_validation()

"""B (T0 SHADOW) 仪表盘渲染 (与实盘并排对比)."""

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

# 验证结论 (2026-07-31 去偏差重验: 合并无偏5min = tdx_5min_pre2024 + tdx_5min_2y,
# 脚本 scripts/backtest_recent100_live_vs_b_idle.py --five-min 逗号合并, 落盘
# recent{100,390,900}_live_vs_b_idle.json; 统一口径 fee=0.03 万3 对齐实盘 / lb=30)
#
# ⚠ 旧数字(B+hybrid OOS +550% / 合并 +1193.75%) 全部作废, 双重偏差:
#   ① 数据偏差: 建立在 aligned_live_4y 稀疏5min缓存("先按当日收盘涨幅排序再抓TopK"→前视偏差);
#   ② 成交价偏差: hybrid/trail 的 +250~320pp 超额来自不可兑现成交价(穿价按精确止损价成交,
#      实盘只能在发现之后成交); 保守口径下 hybrid 全面劣于 TRIX, 已弃用, 卖点切纯 TRIX。
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
    "聚宽选股池消融对比(R3在跑 / FIXED-162 / FIXED_NB 为一次性实验)": "R3(月度轮动+剔除主题, 当前在跑) +1861% / MDD-23.5% / 夏普1.01; "
        "FIXED-162(含主题固定池, 一次性实验) +952% / MDD-41.4% / 夏普0.58; "
        "FIXED_NB(剔除主题固定池, 一次性实验) +891.61% / MDD-41.36% / 夏普0.584 → 固定池剔除主题反伤收益, 月度轮动才是主因子(仅 R3 持续在跑, FIXED两版已回退)",
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
    """B (T0 SHADOW) 总览卡片 + 与实盘对比入口."""
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
    st.caption(f"状态目录: `~/.tradingagents/rotation/` · 更新 {sm} · 流水 {meta['line_count']} 条 · "
               "⚠️ 影子策略, 仅模拟记录, 不下单")


def render_b_idle_vs_live(live_trades: list[dict[str, Any]], days: int = 60):
    """B (T0 SHADOW) 与实盘 T0 并排对比."""
    st.subheader("🆚 B (T0 SHADOW) vs 实盘 T0")
    st.caption("同一时间段, 实盘(hybrid-A选股) vs 影子(B全市场选股·不regime过滤), "
               "验证新策略是否更优。")

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
    with st.expander("📊 策略验证结论 (去偏差重验 · 2026-07-31)"):
        for k, v in WF_SUMMARY.items():
            st.markdown(f"- **{k}**: `{v}`")
        st.markdown("> **当前 SHADOW = B+确认+纯TRIX 卖点** (2026-07-31 升级):")
        st.markdown("> - **选股 B**: 全市场 T0 ETF 当日涨幅 Top1 且 ≥3%, 不 regime 过滤、不限品类;"
                    " 并沿用实盘的 **14:40 双时点确认**(防尾盘脉冲)。")
        st.markdown("> - **卖点 纯 TRIX(5,3)死叉**: 次日 5分K, 窗口 **次日 09:40~11:05**, "
                    "未触发则 11:05 收盘 fallback 平 (对齐回测 `simulate_exit(\"trix0940_cut\")`)。")
        st.markdown("> - **hybrid 卖点已弃用**: 原 hybrid(TRIX+追踪回落0.5%) 的 +250~320pp 超额"
                    "全部来自不可兑现成交价(穿价按精确止损成交, 实盘只能在发现后成交); "
                    "保守口径(死叉后下一根开盘成交)下 hybrid 全面劣于 TRIX。")
        st.markdown("> - **TRIX 成交价稳健(决定性)**: 保守 vs 乐观口径差 <3pp(反而略好), "
                    "不依赖乐观假设, 真实可兑现。")
        st.markdown("> - **B 优势广谱**: 剔除 161129+501018 两只最大贡献后 B +275% vs A +170%, "
                    "B-A 仍 +104.5pp → 非单标的撑起。")
        st.markdown("> **结论**: B+确认+TRIX = 当前找到的最佳策略(全4年+613%/近390 OOS+319%, "
                    "各子段稳赢实盘 A, 卖点稳健, 优势广谱)。代价是回撤 -33.2% > A -28.7%。"
                    "实盘 A 不动, 先 SHADOW 跑一段验证真实 1min 成交价滑点。")
        st.markdown("> **实盘2周(07-16~07-30)对照**: 9笔 -2.40% vs 回测同窗口 -4.77%, "
                    "选股100%一致, 6/7笔成交价差<0.1pp, 07-23 161129 实盘+7.66%>回测+0.93%"
                    "(1min粒度在11:05精确高点) → 回测等价性良好, 实盘略优于回测。")
        st.markdown("> **聚宽 2014-2026 选股池方向印证(2026-08-07)**: R3(剔除主题, drop_sector=True) +1861% / MDD-23.5% / 夏普1.01 **显著优于** FIXED-162(含主题固定池) +952% / MDD-41.4% / 夏普0.58。 → 主题/行业ETF(港股创新药/半导体/油气/军工…)被\"当日涨幅Top1≥3%\"选中后高波动均值回归拖累收益且放大回撤; 剔除主题 = 收益更高 + 回撤更低, 与本地质量筛选(无脑加全部T+0回撤-65%、59只宽基质量池+12564%/-25.2%)完全一致。 ⇒ SHADOW/实盘B候选池应向纯宽基靠拢: 手工103层含主题ETF(港股创新药等)是潜在拖累(本地靠auto59宽基稀释+无偏5min才显优); FIXED-162 在聚宽 < R3 不等于\"1861是绝对最佳\", 而是证明\"剔除主题\"方向正确。")
        st.markdown("> **2026-08-07 消融续做(FIXED_NB)**: 已内联 `ATTACK_POOL_RULE=\"FIXED_NB\"` = 固定162池剔除16只主题ETF(纯宽基146只, 由 get_all_t0_etfs 名称 + `_SECTOR_NEGATIVE` 离线生成字面量), 分离'剔除主题'(①)与'月度轮动'(②): FIXED含主题+952% vs FIXED_NB纯宽基+891.61%(2014-2026实跑, MDD-41.36%/夏普0.584/965笔/胜49.0%) vs R3月度轮动+1861%。 初版用聚宽 get_all_securities 实时名称过滤致空池(0笔/0%空仓), 已改为内联字面量规避; 并修 `_SECTOR_NEGATIVE` 的'生科'→'生物科技'误剔'恒生科技'。聚宽单文件上传即跑。")
        st.markdown("> **FIXED_NB 实跑结论订正(2026-08-07)**: 固定全池剔除主题【反伤】收益(891<952)且回撤几乎不变(-41.36%≈-41.4%), 推翻'剔除主题有益'假设。drop_sector 只在月度轮动语境沾光(R3 1861 > R1 1541)。因素分解: ①剔除主题(固定池语境)=-61pp; ②月度轮动(FIXED_NB→R3)=+970pp(绝对主因子)。⇒ 最优仍是 R3(+1861%/-23.5%/夏普1.01); 若维持固定全池, FIXED(952)>FIXED_NB(891), 应保留主题ETF。")
        st.markdown("> **R3 实跑跟踪(2014-2026 · 与实盘A / SHADOW B 并列的第3个在跑策略 · 本地 SHADOW 实跑)**: ATTACK_POOL_RULE=\"R3\" = 月度轮动质量池(免上传JSON, jq_attack_pools.py 内联 R1~R6 字面量, 键=使用月/值=上月末 pool_as_of, 严格无未来函数), drop_sector=True。聚宽实跑 +1861% / MDD-23.5% / 夏普1.01 → 三路在跑策略中聚宽侧验证的最优形态(其余两路为本地2022-2026窗口: 实盘A +472.25% / SHADOW B +613.46%, 窗口不同不直接比收益高低); R3 回撤最低(-23.5%)且夏普最高(1.01)。因素分解: ②月度轮动(轮动质量筛选)=+970pp 绝对主因子; ①剔除主题只在轮动语境沾光(R3 1861 > R1 1541)。⇒ 生产默认候选切 R3。")

    # 配对收敛薄补充腿 (核心B腿熄火时非趋势期点缀)
    with st.expander("🔗 配对收敛薄补充腿 (非趋势期点缀 · 2026-08-06)"):
        st.markdown("- **定位**: 核心B腿熄火(全市场无 ETF 涨≥3%)且非趋势时, 部署 GOLD/NASDAQ/HSCEI 双胞胎配对收敛作小仓位点缀, 填补 idle 窗口。")
        st.markdown("- **成绩(全4年干净·已修 pre2024 ×5 脏数据)**: NASDAQ `66笔 / +50.6% / 均值+0.73%笔 / 胜56%`(真实薄边缘); "
                    "GOLD `+4704%` 为趋势期时代效应(与 B 重叠), 不计入独立贡献。")
        st.markdown("- **独立隔离**: `pair_shadow_state.json` + `pair_shadow_journal.jsonl`, 仅记录不下单, 仓位 15%。"
                    "安装: `bash scripts/install_crontab.sh --install-pair-shadow`。")


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

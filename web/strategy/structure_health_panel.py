"""结构健康度面板 — 两种结构的「失效体温计」。

数据: ~/.tradingagents/rotation/structure_health.json
产出: scripts/structure_health_monitor.py (cron 周更)

两个指标的共同设计原则:
  1. 测【机制】不测【盈亏】——盈亏滞后, 机制领先
  2. 用差值/相关【对冲掉市场方向】——隔离结构本身, 与 beta 正交
  3. 零是天然阈值——正=结构在, 负=结构反
  4. 在【策略之外】可计算——空仓也能更新, 故能提前预警
  5. 看分布和趋势, 不看单点
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from web.strategy.paths import ROTATION_DIR

HEALTH_JSON = ROTATION_DIR / "structure_health.json"

LEVEL_STYLE = {
    "ok": ("verdict-ok", "🟢"),
    "warn": ("verdict-warn", "🟡"),
    "stop": ("verdict-warn", "🔴"),
}


def _load() -> dict | None:
    if not HEALTH_JSON.exists():
        return None
    try:
        return json.loads(HEALTH_JSON.read_text(encoding="utf-8"))
    except Exception:
        return None


def _sparkline(history: list, value_key: str = "value") -> pd.DataFrame | None:
    if not history:
        return None
    df = pd.DataFrame(history, columns=["date", value_key])
    df[value_key] = pd.to_numeric(df[value_key], errors="coerce")
    df = df.dropna()
    if df.empty:
        return None
    return df.set_index("date")


def _render_metric_card(title: str, block: dict, unit: str, fmt: str,
                        extra_rows: list[tuple[str, str]], note: str) -> None:
    level = block.get("level", "warn")
    cls, dot = LEVEL_STYLE.get(level, ("verdict-warn", "⚪"))
    cur = block.get("current")

    st.markdown(f'<div class="section-title">{title}</div>', unsafe_allow_html=True)
    st.caption(block.get("metric", ""))

    c1, c2 = st.columns([1, 2])
    with c1:
        cur_s = format(cur, fmt) if isinstance(cur, (int, float)) else "—"
        st.markdown(
            f'<div class="kpi"><div class="k">当前读数</div>'
            f'<div class="v">{cur_s}<span style="font-size:0.8rem;"> {unit}</span></div>'
            f'<div class="d">窗口数 {block.get("window_count", "—")}</div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(f'<div class="verdict {cls}">{dot} <b>{block.get("verdict", "—")}</b></div>',
                    unsafe_allow_html=True)

    with c2:
        df = _sparkline(block.get("history") or [])
        if df is not None and len(df) > 1:
            st.line_chart(df, height=140, use_container_width=True)
            st.caption("零轴以上 = 结构在；跌破零并连续 → 结构反转")
        else:
            st.info("历史序列不足")

    rows = "".join(
        f'<div class="rule-kv"><b>{k}</b>: {v}</div>' for k, v in extra_rows
    )
    st.markdown(rows, unsafe_allow_html=True)
    if note:
        st.caption(note)


def render_structure_health() -> None:
    """渲染结构健康度面板(供 1_策略总览.py 调用)。"""
    data = _load()
    if not data:
        st.info(
            "暂无结构健康度数据。先跑一次建基准:\n\n"
            "`python3 scripts/structure_health_monitor.py --days 3000`"
        )
        return

    st.markdown('<div class="section-title">🌡️ 结构健康度 · 失效体温计</div>',
                unsafe_allow_html=True)
    st.caption(
        "测【机制】不测【盈亏】· 对冲市场方向 · 零为天然阈值 · 策略外可计算（空仓也更新）"
        f" — 更新于 {data.get('updated_at', '—')}"
    )

    # ── 指标一 ──
    om = data.get("overnight_momentum")
    if om:
        slope = om.get("slope", 0)
        slope_txt = ("无衰减" if slope >= -0.01 else "⚠ 有衰减")
        _render_metric_card(
            "① 隔夜动量结构（A / B / R3）",
            om,
            unit="pp",
            fmt="+.3f",
            extra_rows=[
                ("当前读数", f"{om.get('current'):+.4f} pp"),
                ("滚动恒正率", f"{om.get('positive_rate', 0):.0f}%"),
                ("趋势斜率", f"{slope:+.5f} pp/窗口（{slope_txt}）"),
                ("连续 ≤0 窗口", f"{om.get('neg_streak', 0)} 个"),
                ("健康基准", "实测 +0.53pp，恒正率 99%"),
                ("失效阈值", "连续 3 个窗口 ≤0 → 减半仓；5 个 → 停止"),
                ("失效含义", "**直接亏钱**（每天都交易，无处可躲）→ 必须主动减仓"),
            ],
            note=om.get("note", ""),
        )
        by_year = om.get("by_year") or {}
        if by_year:
            st.markdown("**分年均值（pp）**")
            st.dataframe(
                [{"年份": y, "上午溢价": v} for y, v in by_year.items()],
                use_container_width=True, hide_index=True,
            )

    st.divider()

    # ── 指标二 ──
    tf = data.get("trend_following")
    if tf:
        series = tf.get("series") or {}
        primary = series.get(tf.get("primary_code", "588000")) or (
            list(series.values())[0] if series else None)
        level = tf.get("level", "warn")
        cls, dot = LEVEL_STYLE.get(level, ("verdict-warn", "⚪"))

        st.markdown('<div class="section-title">② 趋势跟随结构（科创50）</div>',
                    unsafe_allow_html=True)
        st.caption(tf.get("metric", ""))

        if primary:
            c1, c2 = st.columns([1, 2])
            with c1:
                st.markdown(
                    f'<div class="kpi"><div class="k">当前读数（{primary.get("name")}）</div>'
                    f'<div class="v">{primary.get("current"):+.3f}</div>'
                    f'<div class="d">历史中位 {primary.get("median"):+.3f}</div></div>',
                    unsafe_allow_html=True,
                )
                st.markdown(f'<div class="verdict {cls}">{dot} <b>{tf.get("verdict", "—")}</b></div>',
                            unsafe_allow_html=True)
            with c2:
                df = _sparkline(primary.get("history") or [])
                if df is not None and len(df) > 1:
                    st.line_chart(df, height=140, use_container_width=True)
                    st.caption("零轴以上 = 趋势延续；转负 = 反转市")

            st.markdown(
                f'<div class="rule-kv"><b>为正占比</b>: {primary.get("positive_rate", 0):.0f}%</div>'
                f'<div class="rule-kv"><b>最近250日均值</b>: {primary.get("recent_250_mean", 0):+.3f}</div>'
                f'<div class="rule-kv"><b>失效含义</b>: <b>收益停滞但不亏</b>'
                f'（N12簇自动翻空保护）→ 什么都别做，别放宽参数凑交易</div>',
                unsafe_allow_html=True,
            )

        # 逐年
        if primary and primary.get("by_year"):
            st.markdown("**分年均值 IC**")
            st.dataframe(
                [{"年份": y, "动量IC": v} for y, v in primary["by_year"].items()],
                use_container_width=True, hide_index=True,
            )

        # 对照标的
        if len(series) > 1:
            st.markdown("**跨品种对照**")
            rows = []
            for code, s in series.items():
                rows.append({
                    "代码": code,
                    "名称": s.get("name", code),
                    "当前IC": s.get("current"),
                    "历史中位": s.get("median"),
                    "为正占比%": s.get("positive_rate"),
                    "最近250日": s.get("recent_250_mean"),
                })
            st.dataframe(rows, use_container_width=True, hide_index=True)

        if tf.get("note"):
            st.caption(tf["note"])

    st.divider()

    # ── 对照表 ──
    st.markdown("### 两个体温计的失效模式对照")
    st.markdown(
        """
| | 隔夜动量（A/B/R3） | 趋势跟随（科创50） |
|---|---|---|
| 体温计 | 滚动 60 笔「上午溢价」 | 滚动 250 日「动量 IC」 |
| 计算 | 11:05 收益 − 14:50 收益 | corr(过去20日, 未来20日) |
| **失效时会怎样** | **直接亏钱** | **收益停滞，但不亏** |
| 原因 | 每天都交易，无处可躲 | N12 簇自动翻空，自带回避 |
| 正确行动 | **主动减仓 / 停** | **什么都别做，忍住别改参数** |
| 更新速度 | 快（60 笔 ≈ 半年） | 慢（250 日 ≈ 1 年） |
| 单点可信度 | SE ≈ σ/√60，够用 | SE ≈ 0.064，**单点不显著，须看分布** |

**⚠️ IC 转负期间最危险的动作**：嫌信号太少就放宽参数让它继续开仓
——那等于在反转市里反复追涨杀跌（跨品种测试里深证100 的 −54.37% / 回撤 −71.59% 就是这种组合）。
"""
    )

    st.caption(
        "数据来源 ~/.tradingagents/rotation/structure_health.json · "
        "更新命令 `python3 scripts/structure_health_monitor.py --days 250` · cron 每周六 10:00"
    )

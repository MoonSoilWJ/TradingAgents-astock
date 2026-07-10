"""Streamlit panel for intraday monitoring (start/stop/config)."""

from __future__ import annotations

import os
from datetime import date, datetime

import streamlit as st

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.dataflows.a_stock import lookup_astock_name, resolve_ticker
from tradingagents.dataflows.instrument import settlement_rule
from tradingagents.graph.checkpointer import clear_checkpoint
from tradingagents.intraday.calendar import INTRADAY_SLOTS, is_trading_day, next_slot_after
from tradingagents.intraday.daemon_process import ensure_daemon_started
from tradingagents.intraday.session import (
    is_daemon_alive,
    load_session,
    request_start,
    request_stop,
)
from tradingagents.portfolio.store import PortfolioStore


def _resolve_ticker(raw: str) -> tuple[str, str | None]:
    try:
        return resolve_ticker(raw.strip()), None
    except ValueError as exc:
        return "", str(exc)


def render_intraday_panel(*, compact: bool = False) -> None:
    if not compact:
        st.markdown("### 📡 盘中监控")
        st.caption("首次全流程，当日后续轻量；钉钉推送买/卖/不动；独立 daemon 后台运行")

    session = load_session()
    store = PortfolioStore()
    portfolio = store.load(session.ticker) if session.ticker else None

    running = session.active and not session.stop_requested
    disabled = running

    col1, col2 = st.columns(2)
    with col1:
        ticker_raw = st.text_input(
            "股票代码",
            value=session.ticker or "",
            key="intraday_ticker",
            disabled=disabled,
            placeholder="例: 159813",
        )
    with col2:
        shares = st.number_input(
            "初始持仓(股)",
            min_value=0,
            step=100,
            value=session.shares if session.shares else 0,
            key="intraday_shares",
            disabled=disabled,
        )

    capital = st.number_input(
        "总资金(元)",
        min_value=1000.0,
        step=1000.0,
        value=float(session.total_capital or 100_000),
        key="intraday_capital",
        disabled=disabled,
    )
    max_pct = st.slider(
        "单票最大仓位 %",
        min_value=5,
        max_value=100,
        value=int(session.max_position_pct or 30),
        key="intraday_max_pct",
        disabled=disabled,
    )
    webhook = st.text_input(
        "钉钉 Webhook",
        value=session.dingtalk_webhook or os.getenv("DINGTALK_WEBHOOK", ""),
        key="intraday_webhook",
        disabled=disabled,
        type="password",
    )

    code, err = _resolve_ticker(ticker_raw) if ticker_raw.strip() else ("", None)
    if code:
        name = lookup_astock_name(code) or ""
        rule = settlement_rule(code, name)
        st.caption(f"标的: {code} {name} | 结算: {rule}")

    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        start_clicked = st.button(
            "▶ 开始监控",
            type="primary",
            use_container_width=True,
            disabled=running or not ticker_raw.strip(),
        )
    with btn_col2:
        stop_clicked = st.button(
            "■ 停止",
            use_container_width=True,
            disabled=not running and not session.running,
        )

    if start_clicked:
        if err:
            st.error(err)
        else:
            request_start(
                ticker=code,
                shares=int(shares),
                total_capital=float(capital),
                max_position_pct=float(max_pct),
                dingtalk_webhook=webhook,
            )
            clear_checkpoint(
                DEFAULT_CONFIG["data_cache_dir"],
                code,
                date.today().isoformat(),
            )
            name = lookup_astock_name(code) or ""
            avg_cost = 0.0
            if int(shares) > 0:
                from tradingagents.intraday.runner import fetch_quote

                avg_cost, _ = fetch_quote(code)
            store.init(
                code,
                shares=int(shares),
                total_capital=float(capital),
                max_position_pct=float(max_pct),
                settlement=settlement_rule(code, name),
                avg_cost=avg_cost,
            )
            if ensure_daemon_started():
                st.success(f"已启动 {code}，后台 daemon 已运行。")
            else:
                st.warning(
                    f"已保存 {code} 监控配置，但 daemon 未响应。"
                    " 请手动运行: `tradingagents-intraday`"
                )
            st.rerun()

    if stop_clicked:
        session_before = load_session()
        request_stop(hard=True)
        if session_before.ticker:
            clear_checkpoint(
                DEFAULT_CONFIG["data_cache_dir"],
                session_before.ticker,
                date.today().isoformat(),
            )
            from tradingagents.notify.dingtalk import format_stop_message, send_markdown

            name = lookup_astock_name(session_before.ticker) or session_before.ticker
            send_markdown(
                f"{session_before.ticker} stopped",
                format_stop_message(
                    ticker=session_before.ticker,
                    name=name,
                    slot=datetime.now().strftime("%H:%M"),
                    reason="Web UI 硬停止：后续定时已取消，进行中的任务将被中断",
                ),
                webhook=session_before.dingtalk_webhook or None,
            )
        st.warning("已硬停止：后续定时取消，进行中的任务将被中断。")
        st.rerun()

    st.markdown("---")
    daemon_ok = is_daemon_alive()
    if daemon_ok:
        st.caption("🟢 Daemon 在线")
    else:
        st.warning("Daemon 未检测到，请先运行: `tradingagents-intraday`")

    if running or session.running:
        st.markdown(f"**状态**: {'🟢 运行中' if running else '🟡 收尾中'}")
    else:
        st.markdown("**状态**: ⚪ 未启动")

    if session.ticker:
        p = portfolio or store.load(session.ticker)
        if p:
            st.markdown(
                f"持仓 **{p.shares}** 股 | 现金 **{p.cash:,.0f}** 元 | "
                f"结算 **{p.settlement}** | 今日买入 **{p.bought_today}** 股"
            )
        st.caption(
            f"今日已跑 {session.runs_today} 次"
            + (f" | 上次 {session.last_slot} → {session.last_action}" if session.last_slot else "")
            + (f" | 下次 {next_slot_after()}" if running else "")
        )
        st.caption(f"时刻表: {', '.join(INTRADAY_SLOTS)}")
        if not is_trading_day():
            st.info("今日非交易日；推送仍会发，但非交易时段不记账。")

    with st.expander("启动 daemon"):
        st.code("python3 scripts/intraday_daemon.py", language="bash")
        st.caption("或: tradingagents-intraday")
        st.caption("独立进程，关闭浏览器后仍继续监控。")

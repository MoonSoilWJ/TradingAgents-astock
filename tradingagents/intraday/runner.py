"""Orchestrate full/light intraday cycles with ledger and DingTalk."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from tradingagents.agents.schemas import IntradayAction, IntradayDecision
from tradingagents.dataflows.a_stock import _tencent_quote, lookup_astock_name
from tradingagents.dataflows.instrument import settlement_rule
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.intraday.calendar import (
    is_bookable_session,
    is_trading_day,
    next_slot_after,
)
from tradingagents.intraday.light_run import load_cached_state, run_light_intraday_pm, save_cached_state
from tradingagents.intraday.session import (
    load_session,
    mark_running,
    record_run,
    should_stop,
)
from tradingagents.notify.dingtalk import format_order_message, send_markdown
from tradingagents.portfolio.executor import apply_intraday_order, normalize_order
from tradingagents.portfolio.lot import lot_size_for_code
from tradingagents.portfolio.store import PortfolioStore, PortfolioState

logger = logging.getLogger(__name__)


class HardStopRequested(RuntimeError):
    """Raised when the user requests a hard stop mid-run."""


def _parse_intraday_action(value: str | None) -> IntradayAction:
    try:
        return IntradayAction(str(value or "hold").lower())
    except ValueError:
        return IntradayAction.HOLD


def fetch_quote(ticker: str) -> tuple[float, str]:
    quotes = _tencent_quote([ticker])
    q = quotes.get(ticker) or {}
    price = float(q.get("price") or 0)
    name = (q.get("name") or "").strip() or (lookup_astock_name(ticker) or ticker)
    return price, name


def inject_portfolio(state: dict[str, Any], portfolio: PortfolioState, price: float) -> dict[str, Any]:
    state = dict(state)
    state["portfolio_shares"] = portfolio.shares
    state["portfolio_cash"] = portfolio.cash
    state["portfolio_capital"] = portfolio.total_capital
    state["portfolio_max_pct"] = portfolio.max_position_pct
    state["portfolio_price"] = price
    state["portfolio_settlement"] = portfolio.settlement
    state["portfolio_sellable"] = portfolio.sellable_shares()
    state["portfolio_lot"] = lot_size_for_code(portfolio.ticker)
    return state


def _build_config(base: dict | None = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if base:
        cfg.update(base)
    cfg["intraday_mode"] = True
    cfg["checkpoint_enabled"] = True
    return cfg


def _clear_run_artifacts(ticker: str, trade_date: str, config: dict | None = None) -> None:
    from tradingagents.graph.checkpointer import clear_checkpoint

    cfg = _build_config(config)
    clear_checkpoint(cfg["data_cache_dir"], ticker, trade_date)


def run_full_pipeline(
    ticker: str,
    trade_date: str,
    portfolio: PortfolioState,
    price: float,
    config: dict | None = None,
    *,
    slot: str = "",
) -> dict[str, Any]:
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.intraday.progress_log import IntradayRunLogger

    cfg = _build_config(config)
    graph = TradingAgentsGraph(debug=True, config=cfg)
    init_state, args, resume_step = graph.prepare_graph_run(ticker, trade_date)
    portfolio_patch = inject_portfolio({}, portfolio, price)
    if init_state:
        init_state = inject_portfolio(init_state, portfolio, price)
    elif resume_step is not None:
        try:
            graph.graph.update_state(args.get("config", {}), portfolio_patch)
        except Exception as exc:
            logger.warning("Could not inject portfolio into resumed checkpoint: %s", exc)
    run_log = IntradayRunLogger(ticker, trade_date, slot or datetime.now().strftime("%H:%M"))
    last: dict[str, Any] = {}
    try:
        stream = graph.graph.stream(init_state, **args)
        for chunk in stream:
            if should_stop():
                graph.close_graph_run()
                _clear_run_artifacts(ticker, trade_date, config)
                raise HardStopRequested("hard stop requested")
            run_log.on_chunk(chunk)
            last = chunk
        if not last:
            raise RuntimeError("intraday full pipeline returned empty state")
        if not last.get("final_trade_decision") or not last.get("intraday_action"):
            graph.close_graph_run()
            raise RuntimeError("intraday full pipeline finished without PM decision")
        graph.finalize_graph_run(ticker, trade_date, last)
        graph.close_graph_run()
        last["_intraday_report_path"] = str(run_log.path)
        return last
    finally:
        run_log.close()


def run_intraday_cycle(
    *,
    slot: str,
    force_full: bool = False,
    config: dict | None = None,
) -> dict[str, Any]:
    """Run one intraday cycle for the active session ticker."""
    session = load_session()
    if not session.ticker:
        raise ValueError("intraday session has no ticker")

    ticker = session.ticker
    trade_date = date.today().isoformat()
    store = PortfolioStore()
    name_lookup = lookup_astock_name(ticker) or ""
    settlement = settlement_rule(ticker, name_lookup)

    portfolio = store.load(ticker)
    if portfolio is None:
        portfolio = store.init(
            ticker,
            shares=session.shares,
            total_capital=session.total_capital,
            max_position_pct=session.max_position_pct,
            settlement=settlement,
        )
    portfolio = store.rollover_if_new_day(portfolio)
    portfolio.settlement = settlement

    price, name = fetch_quote(ticker)
    if price <= 0 and portfolio.avg_cost > 0:
        price = portfolio.avg_cost

    need_full = force_full or session.full_run_done_date != trade_date
    mark_running(True)
    try:
        if need_full:
            logger.info("Intraday full run for %s slot=%s", ticker, slot)
            state = run_full_pipeline(
                ticker, trade_date, portfolio, price, config, slot=slot
            )
        else:
            logger.info("Intraday light run for %s slot=%s", ticker, slot)
            cached = load_cached_state(ticker, trade_date, _build_config(config)["results_dir"])
            if not cached:
                state = run_full_pipeline(
                    ticker, trade_date, portfolio, price, config, slot=slot
                )
            else:
                from tradingagents.graph.trading_graph import TradingAgentsGraph
                from tradingagents.intraday.progress_log import IntradayRunLogger

                cfg = _build_config(config)
                graph = TradingAgentsGraph(debug=True, config=cfg)
                cached = inject_portfolio(cached, portfolio, price)
                run_log = IntradayRunLogger(ticker, trade_date, slot, mode="light")
                try:
                    run_log.log_state(cached)
                    pm_out = run_light_intraday_pm(cached, graph.deep_thinking_llm)
                    state = {**cached, **pm_out}
                    run_log.log_state(state)
                    state["_intraday_report_path"] = str(run_log.path)
                    save_cached_state(ticker, state, trade_date, cfg["results_dir"])
                finally:
                    run_log.close()
                graph.close_graph_run()

        decision = IntradayDecision(
            action=_parse_intraday_action(state.get("intraday_action")),
            quantity_shares=int(state.get("intraday_quantity") or 0),
            limit_price=price,
            reason=str(state.get("intraday_reason") or state.get("final_trade_decision", ""))[:500],
        )
        book = is_bookable_session()
        normalized = normalize_order(decision, portfolio, price)
        portfolio, applied = apply_intraday_order(
            normalized, portfolio, price, book_trade=book
        )
        store.save(portfolio)

        webhook = session.dingtalk_webhook
        msg = format_order_message(
            ticker=ticker,
            name=name,
            slot=slot,
            action=applied.action,
            quantity=applied.quantity_shares,
            price=price,
            reason=applied.reason,
            state=portfolio,
            booked=applied.booked,
            note=applied.note,
            runs_today=session.runs_today + 1,
            next_slot=next_slot_after(),
            report_path=state.get("_intraday_report_path"),
        )
        if state.get("_intraday_report_path"):
            logger.info("Full debate report: %s", state["_intraday_report_path"])
        send_markdown(f"{ticker} {slot}", msg, webhook=webhook or None)

        record_run(slot=slot, action=applied.action, full_run=need_full)
        return {
            "action": applied.action,
            "quantity": applied.quantity_shares,
            "booked": applied.booked,
            "full_run": need_full,
        }
    finally:
        mark_running(False)

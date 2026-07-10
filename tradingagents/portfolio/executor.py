"""Normalize and apply intraday orders against portfolio state."""

from __future__ import annotations

from dataclasses import dataclass

from tradingagents.agents.schemas import IntradayAction, IntradayDecision
from tradingagents.portfolio.lot import lot_size_for_code, round_down_to_lot
from tradingagents.portfolio.store import PortfolioState


@dataclass(frozen=True)
class AppliedOrder:
    action: str
    quantity_shares: int
    price: float
    reason: str
    booked: bool
    note: str = ""


def max_buy_shares(state: PortfolioState, price: float, lot: int) -> int:
    if price <= 0:
        return 0
    cap_value = state.total_capital * (state.max_position_pct / 100.0)
    current_value = state.shares * price
    room_value = max(0.0, cap_value - current_value)
    by_cash = state.cash
    by_cap = room_value
    affordable = int(min(by_cash, by_cap) / price)
    return round_down_to_lot(affordable, lot)


def normalize_order(
    decision: IntradayDecision,
    state: PortfolioState,
    price: float,
) -> IntradayDecision:
    """Clamp model output to lot size, cash, position cap, and T+0/T+1."""
    lot = lot_size_for_code(state.ticker)
    action = decision.action
    qty = decision.quantity_shares

    if action == IntradayAction.HOLD or qty <= 0:
        return IntradayDecision(
            action=IntradayAction.HOLD,
            quantity_shares=0,
            limit_price=decision.limit_price or price,
            reason=decision.reason,
        )

    if action == IntradayAction.BUY:
        qty = round_down_to_lot(qty, lot)
        max_qty = max_buy_shares(state, price, lot)
        qty = min(qty, max_qty)
        if qty <= 0:
            return IntradayDecision(
                action=IntradayAction.HOLD,
                quantity_shares=0,
                limit_price=price,
                reason=decision.reason + "（资金或仓位上限不足，未执行买入）",
            )
        return IntradayDecision(
            action=IntradayAction.BUY,
            quantity_shares=qty,
            limit_price=decision.limit_price or price,
            reason=decision.reason,
        )

    # SELL
    qty = round_down_to_lot(qty, lot)
    sellable = round_down_to_lot(state.sellable_shares(), lot)
    qty = min(qty, sellable)
    if qty <= 0:
        return IntradayDecision(
            action=IntradayAction.HOLD,
            quantity_shares=0,
            limit_price=price,
            reason=decision.reason + "（无可卖持仓或 T+1 限制，未执行卖出）",
        )
    return IntradayDecision(
        action=IntradayAction.SELL,
        quantity_shares=qty,
        limit_price=decision.limit_price or price,
        reason=decision.reason,
    )


def apply_intraday_order(
    decision: IntradayDecision,
    state: PortfolioState,
    price: float,
    *,
    book_trade: bool,
) -> tuple[PortfolioState, AppliedOrder]:
    """Apply a normalized order; optionally update portfolio ledger."""
    normalized = normalize_order(decision, state, price)
    action = normalized.action.value
    qty = normalized.quantity_shares

    if action == "hold" or qty <= 0 or not book_trade:
        return state, AppliedOrder(
            action="hold",
            quantity_shares=0,
            price=price,
            reason=normalized.reason,
            booked=False,
            note="" if book_trade else "非交易时段，未记账",
        )

    if action == "buy":
        cost = qty * price
        if cost > state.cash:
            return state, AppliedOrder(
                action="hold",
                quantity_shares=0,
                price=price,
                reason=normalized.reason,
                booked=False,
                note="现金不足",
            )
        total_cost = state.avg_cost * state.shares + cost
        new_shares = state.shares + qty
        state.avg_cost = total_cost / new_shares if new_shares else 0.0
        state.shares = new_shares
        state.cash -= cost
        state.bought_today += qty
        return state, AppliedOrder(
            action="buy",
            quantity_shares=qty,
            price=price,
            reason=normalized.reason,
            booked=True,
        )

    # sell
    proceeds = qty * price
    state.shares -= qty
    state.cash += proceeds
    if state.shares == 0:
        state.avg_cost = 0.0
    return state, AppliedOrder(
        action="sell",
        quantity_shares=qty,
        price=price,
        reason=normalized.reason,
        booked=True,
    )

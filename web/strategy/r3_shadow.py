"""Load R3 (月度轮动) SHADOW journal/state for dashboard (与实盘平行, 仅记录不下单).

与 B 的差异:
  - R3 把买卖合并在一条 sell 记录 (无独立 buy 事件), 且记录 signal_date/buy_date/today_gain;
  - leg 为 "R3_动量精选" / "R3_月度轮动" (B 只有 "core_B");
  - 数据文件为 r3_shadow_state.json / r3_journal.jsonl (B 为 b_idle_shadow_state.json / b_idle_journal.jsonl)。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from web.strategy.paths import ROTATION_DIR

R3_STATE = ROTATION_DIR / "r3_shadow_state.json"
R3_JOURNAL = ROTATION_DIR / "r3_journal.jsonl"

LEG_LABELS = {
    "R3_动量精选": "R3 动量精选",
    "R3_月度轮动": "R3 月度轮动",
}

SELL_REASON_LABELS = {
    "trix_death_cross": "TRIX死叉",
    "trix_time_sell_1105": "11:05定时",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return rows


def load_r3_state() -> dict[str, Any]:
    if not R3_STATE.exists():
        return {}
    try:
        return json.loads(R3_STATE.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _compound(rets: list[float]) -> float:
    p = 1.0
    for r in rets:
        p *= (1.0 + r / 100.0)
    return (p - 1.0) * 100.0


def load_r3_data(*, days: int = 60) -> dict[str, Any]:
    """读 R3 SHADOW 流水, 返回近 days 天的平仓明细 + 汇总统计 + 当前持仓.

    R3 的 journal sell 记录同时含 buy_price/sell_price (合并), 无独立 buy 事件,
    信号涨幅取自记录的 today_gain 字段。
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    events = [
        r for r in _read_jsonl(R3_JOURNAL)
        if (r.get("signal_date") or r.get("sell_date") or "") >= cutoff
    ]
    closed = []
    for ev in events:
        if "sell_price" in ev:  # 平仓记录
            closed.append({
                "signal_date": ev.get("signal_date") or ev.get("buy_date") or "",
                "sell_date": ev.get("sell_date") or "",
                "leg": ev.get("leg") or "",
                "etf": ev.get("etf") or "",
                "name": ev.get("name") or "",
                "signal_gain_pct": ev.get("today_gain"),
                "signal_time": ev.get("signal_time") or "",
                "buy_time": ev.get("buy_time") or "",
                "sell_time": ev.get("sell_time") or "",
                "buy_price": ev.get("buy_price"),
                "sell_price": ev.get("sell_price"),
                "return_pct": ev.get("return_pct"),
                "sell_reason": ev.get("sell_reason") or "",
                "theory_return_pct": ev.get("theory_return_pct"),
                "slippage_pp": ev.get("slippage_pp"),
                "actual_price_src": ev.get("actual_price_src") or "",
            })
    closed.sort(key=lambda x: x.get("sell_date") or "", reverse=True)

    rets = [float(c["return_pct"]) for c in closed if c.get("return_pct") is not None]
    by_leg: dict[str, list[float]] = {}
    for c in closed:
        rp = c.get("return_pct")
        if rp is not None:
            label = LEG_LABELS.get(c.get("leg") or "", c.get("leg") or "?")
            by_leg.setdefault(label, []).append(float(rp))

    return {
        "events": events,
        "closed": closed,
        "state": load_r3_state(),
        "stats": {
            "trade_count": len(closed),
            "compound_pct": _compound(rets) if rets else None,
            "avg_pct": (sum(rets) / len(rets)) if rets else None,
            "win_rate": (sum(1 for r in rets if r > 0) / len(rets) * 100) if rets else None,
            "by_leg": {k: {"count": len(v), "compound": _compound(v)} for k, v in by_leg.items()},
        },
    }


def closed_to_table_rows(closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for c in closed:
        rp = c.get("return_pct")
        rows.append({
            "信号日": c.get("signal_date") or "—",
            "卖出日": c.get("sell_date") or "—",
            "腿": LEG_LABELS.get(c.get("leg") or "", c.get("leg") or ""),
            "标的": f'{c.get("etf")} {c.get("name")}' if c.get("etf") else "—",
            "信号涨幅%": (f'{c["signal_gain_pct"]:+.2f}' if c.get("signal_gain_pct") not in (None, "") else "—"),
            "买入价": c.get("buy_price"),
            "卖出价": c.get("sell_price"),
            "收益%": (f'{rp:+.2f}' if rp is not None else "—"),
            "理论收益%": (f'{c["theory_return_pct"]:+.2f}' if c.get("theory_return_pct") not in (None, "") else "—"),
            "滑点pp": (f'{c["slippage_pp"]:+.2f}' if c.get("slippage_pp") not in (None, "") else "—"),
            "卖出原因": SELL_REASON_LABELS.get(c.get("sell_reason") or "", c.get("sell_reason") or ""),
        })
    return rows


def open_position_row(state: dict[str, Any]) -> dict[str, Any] | None:
    pos = state.get("position")
    if not pos or pos.get("sold"):
        return None
    return {
        "标的": f'{pos.get("etf")} {pos.get("name")}',
        "腿": LEG_LABELS.get(pos.get("type") or "", pos.get("type") or ""),
        "买入日": pos.get("buy_date") or "—",
        "买入价": pos.get("buy_price"),
        "信号涨幅%": (f'{pos["today_gain"]:+.2f}' if pos.get("today_gain") not in (None, "") else "—"),
    }


def r3_meta() -> dict[str, Any]:
    meta: dict[str, Any] = {
        "state_path": str(R3_STATE),
        "journal_path": str(R3_JOURNAL),
        "state_mtime": 0,
        "journal_mtime": 0,
        "line_count": 0,
    }
    try:
        meta["state_mtime"] = R3_STATE.stat().st_mtime
    except OSError:
        pass
    try:
        with R3_JOURNAL.open("r", encoding="utf-8") as f:
            meta["line_count"] = sum(1 for _ in f)
        meta["journal_mtime"] = R3_JOURNAL.stat().st_mtime
    except OSError:
        pass
    return meta

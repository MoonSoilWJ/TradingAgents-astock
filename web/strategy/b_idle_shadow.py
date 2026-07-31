"""Load B (T0 SHADOW) journal/state for dashboard (与实盘平行, 仅记录不下单)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from web.strategy.paths import ROTATION_DIR

BIDLE_STATE = ROTATION_DIR / "b_idle_shadow_state.json"
BIDLE_JOURNAL = ROTATION_DIR / "b_idle_journal.jsonl"

LEG_LABELS = {"core_B": "核心B"}

SELL_REASON_LABELS = {
    "trix_death_cross": "TRIX死叉",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            # idle 隔夜动量腿已停用, 仪表盘不再展示其历史记录
            if row.get("leg") == "idle_momentum" or row.get("type") == "idle_momentum":
                continue
            rows.append(row)
    return rows


def load_b_idle_state() -> dict[str, Any]:
    if not BIDLE_STATE.exists():
        return {}
    try:
        data = json.loads(BIDLE_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _compound(rets: list[float]) -> float:
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return (eq - 1) * 100


def load_b_idle_data(*, days: int = 60) -> dict[str, Any]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    events = [
        r for r in _read_jsonl(BIDLE_JOURNAL)
        if (r.get("signal_date") or r.get("sell_date") or "") >= cutoff
    ]

    # 合并 buy(signal) + sell 成完整交易
    buys: dict[str, dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []
    for ev in events:
        if "buy_price" in ev and "sell_price" not in ev:
            key = f"{ev.get('signal_date')}:{ev.get('etf')}:{ev.get('leg')}"
            buys[key] = ev
        elif "sell_price" in ev:
            key = f"{ev.get('signal_date')}:{ev.get('etf')}:{ev.get('leg')}"
            buy = buys.get(key, {})
            closed.append({
                "signal_date": ev.get("signal_date") or buy.get("signal_date"),
                "sell_date": ev.get("sell_date"),
                "leg": ev.get("leg") or buy.get("leg"),
                "etf": ev.get("etf") or buy.get("etf"),
                "name": ev.get("name") or buy.get("name"),
                "buy_price": ev.get("buy_price") or buy.get("buy_price"),
                "signal_gain_pct": buy.get("today_gain"),
                "sell_price": ev.get("sell_price"),
                "return_pct": ev.get("return_pct"),
                "sell_reason": ev.get("sell_reason"),
                "note": ev.get("note"),
            })

    closed.sort(key=lambda x: x.get("sell_date") or "", reverse=True)
    rets = [float(c["return_pct"]) for c in closed if c.get("return_pct") is not None]
    by_leg: dict[str, list[float]] = {}
    for c in closed:
        rp = c.get("return_pct")
        if rp is not None:
            by_leg.setdefault(c.get("leg") or "?", []).append(float(rp))

    return {
        "events": events,
        "closed": closed,
        "state": load_b_idle_state(),
        "stats": {
            "trade_count": len(closed),
            "compound_pct": _compound(rets) if rets else None,
            "avg_pct": sum(rets) / len(rets) if rets else None,
            "win_rate": (sum(1 for r in rets if r > 0) / len(rets) * 100) if rets else None,
            "core_pct": _compound(by_leg.get("core_B", [])) if by_leg.get("core_B") else None,
            "core_count": len(by_leg.get("core_B", [])),
        },
    }


def closed_to_table_rows(closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for c in closed:
        reason = SELL_REASON_LABELS.get(c.get("sell_reason", ""), c.get("sell_reason", "—"))
        rp = c.get("return_pct")
        rows.append({
            "腿": LEG_LABELS.get(c.get("leg", ""), c.get("leg", "—")),
            "信号日": c.get("signal_date"),
            "平仓日": c.get("sell_date"),
            "代码": c.get("etf"),
            "标的": c.get("name"),
            "信号涨幅%": c.get("signal_gain_pct"),
            "买入价": c.get("buy_price"),
            "卖出价": c.get("sell_price"),
            "收益%": round(rp, 2) if rp is not None else None,
            "卖出原因": reason,
        })
    return rows


def open_position_row(state: dict[str, Any]) -> dict[str, Any] | None:
    pos = state.get("position")
    if not pos or pos.get("sold"):
        return None
    return {
        "腿": LEG_LABELS.get(pos.get("type", ""), pos.get("type", "—")),
        "代码": pos.get("etf"),
        "标的": pos.get("name"),
        "买入日": pos.get("buy_date"),
        "买入价": pos.get("buy_price"),
        "信号涨幅%": pos.get("today_gain"),
    }


def b_idle_meta() -> dict[str, Any]:
    path = BIDLE_JOURNAL
    mtime = None
    line_count = 0
    if path.exists():
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        line_count = sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())
    return {
        "path": path,
        "state_path": BIDLE_STATE,
        "mtime": mtime,
        "line_count": line_count,
        "exists": path.exists(),
        "state_mtime": (BIDLE_STATE.stat().st_mtime if BIDLE_STATE.exists() else None),
    }

"""Load T+0 idle-window shadow journal for dashboard."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from web.strategy.paths import ROTATION_DIR

IDLE_SHADOW_LOG = ROTATION_DIR / "t0_idle_shadow.jsonl"
IDLE_SHADOW_STATE = ROTATION_DIR / "t0_idle_shadow_state.json"

LEG_LABELS = {"leg1": "段1午间", "leg2": "段2午后"}

SELL_REASON_LABELS = {
    "time_sell": "定时卖",
    "trix_death": "TRIX死叉",
    "quote_fallback": "行情兜底",
    "time_fallback": "定时兜底",
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
            rows.append(row)
    return rows


def idle_shadow_meta() -> dict[str, Any]:
    path = IDLE_SHADOW_LOG
    mtime = None
    line_count = 0
    if path.exists():
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        line_count = sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())
    return {
        "path": path,
        "state_path": IDLE_SHADOW_STATE,
        "mtime": mtime,
        "line_count": line_count,
        "exists": path.exists(),
    }


def load_idle_shadow_state() -> dict[str, Any]:
    if not IDLE_SHADOW_STATE.exists():
        return {"trade_date": "", "leg1": None, "leg2": None}
    try:
        data = json.loads(IDLE_SHADOW_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def load_idle_shadow_events(*, days: int = 30) -> list[dict[str, Any]]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [
        r for r in _read_jsonl(IDLE_SHADOW_LOG)
        if (r.get("ts") or "")[:10] >= cutoff
    ]


def _compound(rets: list[float]) -> float:
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return (eq - 1) * 100


def load_idle_shadow_data(*, days: int = 30) -> dict[str, Any]:
    events = load_idle_shadow_events(days=days)
    closed: list[dict[str, Any]] = []
    picks: dict[str, dict[str, Any]] = {}

    for ev in events:
        if ev.get("event") == "pick":
            key = f"{ev.get('ts', '')[:10]}:{ev.get('leg')}"
            picks[key] = ev
        if ev.get("event") != "sell":
            continue
        day = (ev.get("ts") or "")[:10]
        leg = ev.get("leg", "")
        pick = picks.get(f"{day}:{leg}", {})
        closed.append({
            **ev,
            "trade_date": day,
            "v6_score": pick.get("v6_score"),
            "today_gain": pick.get("today_gain"),
            "signal_time": pick.get("signal_time"),
        })

    closed.sort(key=lambda x: x.get("ts", ""), reverse=True)
    sell_rets = [float(s["return_pct"]) for s in closed if s.get("return_pct") is not None]
    by_leg: dict[str, list[float]] = {}
    for s in closed:
        leg = s.get("leg") or "?"
        rp = s.get("return_pct")
        if rp is not None:
            by_leg.setdefault(leg, []).append(float(rp))

    return {
        "events": events,
        "closed": closed,
        "stats": {
            "sell_count": len(closed),
            "compound_pct": _compound(sell_rets) if sell_rets else None,
            "avg_pct": sum(sell_rets) / len(sell_rets) if sell_rets else None,
            "leg1_pct": _compound(by_leg.get("leg1", [])) if by_leg.get("leg1") else None,
            "leg2_pct": _compound(by_leg.get("leg2", [])) if by_leg.get("leg2") else None,
            "leg1_count": len(by_leg.get("leg1", [])),
            "leg2_count": len(by_leg.get("leg2", [])),
        },
        "state": load_idle_shadow_state(),
    }


def open_leg_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    today = date.today().isoformat()
    if state.get("trade_date") != today:
        return rows
    for leg_id in ("leg1", "leg2"):
        slot = state.get(leg_id)
        if not slot or not slot.get("code"):
            continue
        if slot.get("closed"):
            continue
        status = "待买入" if not slot.get("buy_price") else "持仓中"
        rows.append({
            "状态": status,
            "段": LEG_LABELS.get(leg_id, leg_id),
            "代码": slot.get("code"),
            "标的": slot.get("name"),
            "v6": slot.get("v6_score"),
            "涨%": slot.get("today_gain"),
            "买入价": slot.get("buy_price"),
            "卖出价": None,
            "收益%": None,
            "原因": "—",
            "时间": (slot.get("pick_ts") or "")[5:16].replace("T", " "),
        })
    return rows


def closed_to_table_rows(closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for s in closed:
        reason = SELL_REASON_LABELS.get(s.get("sell_reason", ""), s.get("sell_reason", "—"))
        rows.append({
            "日期": s.get("trade_date") or (s.get("ts") or "")[:10],
            "段": LEG_LABELS.get(s.get("leg", ""), s.get("leg", "—")),
            "代码": s.get("code"),
            "标的": s.get("name"),
            "v6": s.get("v6_score"),
            "涨%": s.get("today_gain"),
            "买入价": s.get("buy_price"),
            "卖出价": s.get("sell_price"),
            "收益%": s.get("return_pct"),
            "原因": reason,
            "时间": (s.get("ts") or "")[5:16].replace("T", " "),
        })
    return rows


def events_to_log_rows(events: list[dict[str, Any]], *, limit: int = 40) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ev in reversed(events[-limit:]):
        event = ev.get("event", "?")
        leg = LEG_LABELS.get(ev.get("leg", ""), ev.get("leg", ""))
        detail = ""
        if event == "pick":
            detail = f"v6={ev.get('v6_score')} 涨{ev.get('today_gain'):+.1f}%"
        elif event == "buy":
            detail = f"买 {ev.get('buy_price')}"
        elif event == "sell":
            detail = f"{ev.get('return_pct'):+.2f}% {ev.get('sell_reason')}"
        elif event == "pick_skip":
            detail = "无满足条件标的"
        rows.append({
            "时间": (ev.get("ts") or "")[5:19].replace("T", " "),
            "段": leg,
            "事件": event,
            "代码": ev.get("code", "—"),
            "标的": ev.get("name", "—"),
            "详情": detail,
        })
    return rows

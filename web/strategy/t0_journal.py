"""Load T+0 live trade journal for dashboard tables."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from web.strategy.paths import ROTATION_DIR

TRADE_JOURNAL = ROTATION_DIR / "t0_trade_journal.jsonl"
SHADOW_LOG = ROTATION_DIR / "t0_trail_shadow.jsonl"

SELL_REASON_LABELS = {
    "time_sell": "11:05定时",
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
            rows.append(row)
    return rows


def _parse_pct(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    text = str(val).strip().replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def _trade_key(entry: dict[str, Any]) -> str:
    sell_day = entry.get("sell_date") or str(entry.get("ts", ""))[:10]
    return f"{entry.get('buy_date')}_{entry.get('etf')}_{sell_day}"


def _shadow_check_lookup() -> dict[str, dict[str, Any]]:
    """Map check ts -> 同秒 shadow 检查快照（实时价/浮盈）。"""
    lookup: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(SHADOW_LOG):
        ts = row.get("ts")
        if not ts or row.get("event") == "live_sell":
            continue
        lookup[str(ts)] = row
    return lookup


def _normalize_trade(raw: dict[str, Any], *, source: str) -> dict[str, Any]:
    buy_price = float(raw.get("buy_price") or 0)
    sell_price = float(raw.get("sell_price") or 0)
    return_pct = _parse_pct(raw.get("return_pct"))
    if return_pct is None and buy_price and sell_price:
        return_pct = (sell_price - buy_price) / buy_price * 100

    float_pct = _parse_pct(raw.get("float_pct"))
    if float_pct is None:
        float_pct = return_pct

    sell_reason = str(raw.get("sell_reason") or "")
    sell_date = raw.get("sell_date") or str(raw.get("ts", ""))[:10]

    return {
        "key": _trade_key({**raw, "sell_date": sell_date}),
        "source": source,
        "buy_date": raw.get("buy_date"),
        "buy_time": raw.get("buy_time") or "14:50",
        "etf": raw.get("etf"),
        "name": raw.get("name") or raw.get("etf"),
        "type": raw.get("type", ""),
        "signal_gain_pct": _parse_pct(raw.get("signal_gain_pct") or raw.get("today_gain")),
        "buy_price": buy_price or None,
        "sell_date": sell_date,
        "sell_time": raw.get("sell_time") or "",
        "sell_reason": sell_reason,
        "sell_reason_label": SELL_REASON_LABELS.get(sell_reason, sell_reason or "—"),
        "sell_price": sell_price or None,
        "float_pct": float_pct,
        "return_pct": return_pct,
        "ts": raw.get("ts"),
        "strategy_version": raw.get("strategy_version"),
    }


def _backfill_from_shadow() -> list[dict[str, Any]]:
    check_by_ts = _shadow_check_lookup()
    trades: list[dict[str, Any]] = []
    for row in _read_jsonl(SHADOW_LOG):
        if row.get("event") != "live_sell":
            continue
        ts = str(row.get("ts", ""))
        enriched = dict(row)
        check = check_by_ts.get(ts) or {}
        buy_price = float(enriched.get("buy_price") or 0)

        if check.get("name"):
            enriched["name"] = check.get("name")
        if check.get("float_pct") is not None:
            enriched["float_pct"] = check.get("float_pct")

        sell_reason = str(enriched.get("sell_reason") or "")
        if sell_reason == "trix_death_cross":
            trix_price = check.get("trix_sell_price") or enriched.get("sell_price")
            if trix_price and buy_price:
                enriched["sell_price"] = trix_price
                enriched["return_pct"] = (float(trix_price) - buy_price) / buy_price * 100
            enriched["sell_time"] = check.get("trix_sell_time") or check.get("check_time") or ""
        else:
            live_price = check.get("price")
            if live_price and buy_price:
                enriched["sell_price"] = live_price
                enriched["return_pct"] = (float(live_price) - buy_price) / buy_price * 100
            enriched["sell_time"] = check.get("check_time") or (ts[11:16] if len(ts) >= 16 else "")

        enriched["sell_date"] = ts[:10]
        trades.append(_normalize_trade(enriched, source="shadow"))
    return trades


def load_t0_trades(*, days: int = 60) -> dict[str, Any]:
    """Return closed trades, optional open position row, and file metadata."""
    cutoff = date.today() - timedelta(days=max(days - 1, 0))

    by_key: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(TRADE_JOURNAL):
        if row.get("event") != "trade_closed":
            continue
        trade = _normalize_trade(row, source="journal")
        by_key[trade["key"]] = trade

    for trade in _backfill_from_shadow():
        by_key.setdefault(trade["key"], trade)

    closed = list(by_key.values())
    closed.sort(key=lambda t: (t.get("sell_date") or "", t.get("ts") or ""), reverse=True)

    if days > 0:
        filtered: list[dict[str, Any]] = []
        for trade in closed:
            sell_day = trade.get("sell_date")
            if not sell_day:
                filtered.append(trade)
                continue
            try:
                if date.fromisoformat(sell_day) >= cutoff:
                    filtered.append(trade)
            except ValueError:
                filtered.append(trade)
        closed = filtered

    return {
        "path": TRADE_JOURNAL,
        "shadow_path": SHADOW_LOG,
        "journal_exists": TRADE_JOURNAL.exists(),
        "shadow_exists": SHADOW_LOG.exists(),
        "closed": closed,
    }


def open_position_row(state_data: dict[str, Any] | None) -> dict[str, Any] | None:
    pos = (state_data or {}).get("position")
    if not pos or pos.get("sold"):
        return None
    buy_price = float(pos.get("buy_price") or 0)
    return {
        "状态": "持仓中",
        "买入日": pos.get("buy_date"),
        "买入时间": "14:50",
        "标的": pos.get("name"),
        "代码": pos.get("etf"),
        "类型": pos.get("type", ""),
        "信号涨幅%": _parse_pct(pos.get("today_gain")),
        "买入价": buy_price or None,
        "卖出日": "—",
        "卖出时间": "—",
        "卖出原因": "—",
        "卖出价": None,
        "卖出浮盈%": None,
        "预估收益%": None,
    }


def trades_to_table_rows(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for t in trades:
        float_pct = t.get("float_pct")
        return_pct = t.get("return_pct")
        rows.append({
            "状态": "已平仓",
            "买入日": t.get("buy_date"),
            "买入时间": t.get("buy_time"),
            "标的": t.get("name"),
            "代码": t.get("etf"),
            "类型": t.get("type"),
            "信号涨幅%": t.get("signal_gain_pct"),
            "买入价": t.get("buy_price"),
            "卖出日": t.get("sell_date"),
            "卖出时间": t.get("sell_time"),
            "卖出原因": t.get("sell_reason_label"),
            "卖出价": t.get("sell_price"),
            "卖出浮盈%": round(float_pct, 2) if float_pct is not None else None,
            "预估收益%": round(return_pct, 2) if return_pct is not None else None,
        })
    return rows


def backfill_journal_from_shadow(*, dry_run: bool = False) -> int:
    """把 shadow 里的 live_sell 写入 t0_trade_journal.jsonl（幂等，按 key 去重）。"""
    existing_keys = {
        _trade_key(row)
        for row in _read_jsonl(TRADE_JOURNAL)
        if row.get("event") == "trade_closed"
    }
    written = 0
    for trade in _backfill_from_shadow():
        if trade["key"] in existing_keys:
            continue
        entry = {
            "event": "trade_closed",
            "ts": trade.get("ts") or f"{trade.get('sell_date')}T{trade.get('sell_time', '00:00')}:00",
            "etf": trade.get("etf"),
            "name": trade.get("name"),
            "type": trade.get("type", ""),
            "buy_date": trade.get("buy_date"),
            "buy_time": trade.get("buy_time"),
            "buy_price": trade.get("buy_price"),
            "signal_gain_pct": trade.get("signal_gain_pct"),
            "sell_date": trade.get("sell_date"),
            "sell_time": trade.get("sell_time"),
            "sell_reason": trade.get("sell_reason"),
            "sell_price": trade.get("sell_price"),
            "float_pct": trade.get("float_pct"),
            "return_pct": trade.get("return_pct"),
            "strategy_version": trade.get("strategy_version"),
            "source": "shadow_backfill",
        }
        if dry_run:
            written += 1
            continue
        TRADE_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with TRADE_JOURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        existing_keys.add(trade["key"])
        written += 1
    return written


def journal_meta() -> dict[str, Any]:
    path = TRADE_JOURNAL
    mtime = None
    line_count = 0
    if path.exists():
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        line_count = sum(1 for _ in path.read_text(encoding="utf-8").splitlines() if _.strip())
    return {
        "path": path,
        "mtime": mtime,
        "line_count": line_count,
    }

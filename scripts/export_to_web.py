#!/usr/bin/env python3
"""导出策略数据到 strategy-web 的 strategies.json.

用法:
    # 默认输出到同目录的 strategy-web/public/strategies.json
    # (会自动从当前 TradingAgents-astock 仓库找 ../strategy-web/public/)
    python3 scripts/export_to_web.py

    # 指定输出路径
    python3 scripts/export_to_web.py --out /path/to/strategy-web/public/strategies.json

    # 同时 scp 到远程 ECS (可选)
    python3 scripts/export_to_web.py --scp user@host:/path/to/web-root/

数据来源 (在跑实盘的机器上):
    ~/.tradingagents/rotation/t0_trade_journal.jsonl   (实盘 t0_baseline_trix)
    ~/.tradingagents/rotation/b_idle_journal.jsonl      (shadow, 过滤 idle_momentum)
    ~/.tradingagents/rotation/t0_monitor_state.json    (实盘状态)
    ~/.tradingagents/rotation/b_idle_shadow_state.json  (shadow 状态)
    ~/.tradingagents/rotation/recent390_live_vs_b_idle.json   (OOS 回测, 缺则搜 cache/t0_5min/)

输出 JSON 结构对齐 strategy-web/src/types/strategy.ts:
    [{ id, name, type, status, description, tags,
       backtest: { annualReturn, maxDrawdown, sharpeRatio, winRate,
                   totalReturn, backtestDays, startDate, endDate },
       live:     { dailyReturn, lastDayReturn, totalReturn,
                   runningDays, startDate },
       navCurve, backtestCurve }]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROTATION_DIR = Path.home() / ".tradingagents" / "rotation"

# ─── OOS 回测数据: recent390_live_vs_b_idle.json (390天, 2024-12-20~2026-07-31) ─
# 顶层:
#   live_hybridA_confirm_trix   实盘汇总 {trades, equity_pct, win_rate, max_drawdown}
#   b_confirm_trix              shadow 汇总 {trades, equity_pct, win_rate, max_drawdown}
#   live_trades                 实盘逐笔 (189 笔)
#   b_confirm_trix_trades      shadow 逐笔 (231 笔)
# OOS 回测文件可能位于多个位置，按优先级依次查找：
#   1. ~/.tradingagents/rotation/recent390_live_vs_b_idle.json
#   2. ~/.tradingagents/cache/t0_5min/recent390_live_vs_b_idle.json
#   3. ~/.tradingagents 下任意子目录递归匹配
_OOS_BACKTEST_CANDIDATES = [
    ROTATION_DIR / "recent390_live_vs_b_idle.json",
    Path.home() / ".tradingagents" / "cache" / "t0_5min" / "recent390_live_vs_b_idle.json",
]


def _find_oos_backtest_file() -> Path | None:
    for cand in _OOS_BACKTEST_CANDIDATES:
        if cand.exists():
            return cand
    # 兜底：递归搜索 ~/.tradingagents 下首个匹配文件
    for p in (Path.home() / ".tradingagents").rglob("recent390*idle*.json"):
        return p
    return None


_OOS_CACHE: dict[str, Any] | None = None
_OOS_CACHE_PATH: Path | None = None


def _load_oos_backtest() -> dict[str, Any] | None:
    global _OOS_CACHE, _OOS_CACHE_PATH
    if _OOS_CACHE is not None:
        return _OOS_CACHE
    found = _find_oos_backtest_file()
    if found is None:
        print(f"  ! 警告: 未找到 OOS 回测文件 recent390_live_vs_b_idle.json，回测数据将为空")
        return None
    _OOS_CACHE_PATH = found
    try:
        _OOS_CACHE = json.loads(found.read_text(encoding="utf-8"))
        return _OOS_CACHE
    except (json.JSONDecodeError, OSError):
        return None


# 策略 ID → OOS 文件里的字段名映射
_STRATEGY_OOS_MAP = {
    "t0_baseline_trix": {
        "summary_key": "live_hybridA_confirm_trix",  # 实盘 A+确认+TRIX
        "trades_key": "live_trades",                  # 实盘逐笔
    },
    "t0_coreB_shadow": {
        "summary_key": "b_confirm_trix",              # shadow B+确认+TRIX
        "trades_key": "b_confirm_trix_trades",         # shadow 逐笔
    },
}


def _backtest_summary(strategy_id: str) -> dict[str, Any]:
    """从 OOS 回测文件读摘要."""
    oos = _load_oos_backtest()
    if not oos:
        # fallback
        return {"totalReturn": 0, "tradeCount": 0, "winRate": 0,
                "maxDrawdown": 0, "sharpe": 0}
    keys = _STRATEGY_OOS_MAP.get(strategy_id)
    if not keys:
        return {"totalReturn": 0, "tradeCount": 0, "winRate": 0,
                "maxDrawdown": 0, "sharpe": 0}
    s = oos.get(keys["summary_key"], {})
    return {
        "totalReturn": round(float(s.get("equity_pct", 0)), 2),
        "tradeCount": int(s.get("trades", 0)),
        "winRate": round(float(s.get("win_rate", 0)), 1),
        "maxDrawdown": round(abs(float(s.get("max_drawdown", 0) or 0)), 2),
        "sharpe": 0.0,  # OOS 文件没有 sharpe, 暂留 0
    }


def _backtest_trades(strategy_id: str) -> list[dict[str, Any]]:
    """从 OOS 回测文件读逐笔交易."""
    oos = _load_oos_backtest()
    if not oos:
        return []
    keys = _STRATEGY_OOS_MAP.get(strategy_id)
    if not keys:
        return []
    return oos.get(keys["trades_key"], [])


def _backtest_start_end_dates() -> tuple[str, str]:
    """从 OOS 文件读 start/end 日期."""
    oos = _load_oos_backtest()
    if not oos:
        return "", ""
    window = oos.get("window", "")  # "2024-12-20~2026-07-31"
    if "~" in window:
        parts = window.split("~")
        return parts[0], parts[1]
    return "", ""


def _backtest_days_count() -> int:
    """回测覆盖天数."""
    oos = _load_oos_backtest()
    if oos:
        return int(oos.get("trading_days", 390))
    return 390


# ─── 文件读取 ────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path, *, skip_idle: bool = False) -> list[dict[str, Any]]:
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
        if not isinstance(row, dict):
            continue
        if skip_idle and (
            row.get("leg") == "idle_momentum"
            or row.get("type") == "idle_momentum"
        ):
            continue
        rows.append(row)
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


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


# ─── 交易流水合并 ────────────────────────────────────────────────────────────

def _merge_trades(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """提取交易明细(已平仓 + 持仓中).

    兼容两种 journal 格式:
      1. 实盘 t0_trade_journal.jsonl: sell 事件本身完整包含 buy_date/buy_price/signal_gain_pct
      2. Shadow b_idle_journal.jsonl: buy 信号事件 + sell 事件分开记录,
         需要用 signal_date + etf + leg 匹配合并

    持仓中(只有 buy 信号, 没有对应 sell 事件)也输出, status=open.

    OOS 补充: 实盘 journal 通常不含 signal_time, 这里从 recent390 的 live_trades
    按 (signal_date, etf, sell_date) 匹配, 用真实的 signal_time 回填.
    """
    # 0. 构建 OOS live_trades 的查找表 (用于补充实盘缺失的 signal_time / buy_time)
    _oos = _load_oos_backtest()
    _oos_signal_map: dict[tuple, str] = {}
    _oos_buy_map: dict[tuple, str] = {}
    if _oos:
        for t in _oos.get("live_trades", []):
            k = (str(t.get("signal_date", "")), str(t.get("etf", "")), str(t.get("sell_date", "")))
            if t.get("signal_time"):
                _oos_signal_map[k] = str(t["signal_time"])
            if t.get("buy_time"):
                _oos_buy_map[k] = str(t["buy_time"])

    # 1. 先收集所有 buy 信号事件 (有 buy_price 但没有 sell_price)
    buys: dict[str, dict[str, Any]] = {}
    buy_events: list[dict[str, Any]] = []  # 保留原始顺序, 用于输出持仓中
    for ev in events:
        if (
            "buy_price" in ev
            and "sell_price" not in ev
            and ("signal_date" in ev or "buy_date" in ev)
        ):
            key = f"{ev.get('signal_date') or ev.get('buy_date')}:{ev.get('etf')}:{ev.get('leg', '')}"
            buys[key] = ev
            buy_events.append(ev)

    # 2. 收集所有 sell 事件, 标记已使用的 buy key
    used_buy_keys: set[str] = set()
    closed: list[dict[str, Any]] = []
    for ev in events:
        # 只处理 sell 事件
        is_sell = (
            ev.get("event") == "trade_closed"
            or "sell_price" in ev
            or "sell_date" in ev
        )
        if not is_sell:
            continue

        # 试图匹配对应的 buy 信号事件 (shadow 格式)
        buy = {}
        matched_key = None
        if "signal_date" not in ev and "buy_date" not in ev:
            # 用 etf + leg + buy_price 反查 buy 事件
            for k, v in buys.items():
                if (
                    k.endswith(f":{ev.get('etf', '')}:{ev.get('leg', '')}")
                    and abs(float(v.get("buy_price", 0) or 0) - float(ev.get("buy_price", 0) or 0)) < 1e-6
                ):
                    buy = v
                    matched_key = k
                    break
        elif ev.get("signal_date") or ev.get("buy_date"):
            key = f"{ev.get('signal_date') or ev.get('buy_date')}:{ev.get('etf')}:{ev.get('leg', '')}"
            if key in buys:
                buy = buys[key]
                matched_key = key

        if matched_key:
            used_buy_keys.add(matched_key)

        buy_price = _parse_pct(ev.get("buy_price")) or _parse_pct(buy.get("buy_price")) or 0
        sell_price = _parse_pct(ev.get("sell_price")) or 0
        ret = _parse_pct(ev.get("return_pct"))
        if ret is None and buy_price and sell_price:
            ret = (sell_price - buy_price) / buy_price * 100

        signal_gain = (
            _parse_pct(ev.get("signal_gain_pct"))
            or _parse_pct(buy.get("today_gain"))
            or _parse_pct(buy.get("signal_gain_pct"))
        )

        signal_date = ev.get("buy_date") or buy.get("signal_date") or ev.get("signal_date")

        closed.append({
            "status": "closed",
            "signalDate": signal_date,
            "buyDate": ev.get("buy_date") or buy.get("signal_date") or buy.get("buy_date"),
            "sellDate": ev.get("sell_date"),
            "buyTime": (
                ev.get("buy_time")
                or buy.get("buy_time")
                or _oos_buy_map.get(
                    (str(signal_date or ""), str(ev.get("etf") or buy.get("etf") or ""), str(ev.get("sell_date") or ""))
                )
            ),
            "sellTime": ev.get("sell_time"),
            "signalTime": (
                buy.get("signal_time")
                or ev.get("signal_time")
                or _oos_signal_map.get(
                    (str(signal_date or ""), str(ev.get("etf") or buy.get("etf") or ""), str(ev.get("sell_date") or ""))
                )
            ),
            "etf": ev.get("etf") or buy.get("etf"),
            "name": ev.get("name") or buy.get("name"),
            "buyPrice": round(buy_price, 4) if buy_price else None,
            "sellPrice": round(sell_price, 4) if sell_price else None,
            "signalGainPct": signal_gain,
            "returnPct": round(float(ret), 2) if ret is not None else None,
            "sellReason": ev.get("sell_reason"),
            "note": ev.get("note"),
        })

    # 3. 把没用到的 buy 信号事件作为"持仓中"输出
    open_trades: list[dict[str, Any]] = []
    for ev in buy_events:
        key = f"{ev.get('signal_date') or ev.get('buy_date')}:{ev.get('etf')}:{ev.get('leg', '')}"
        if key in used_buy_keys:
            continue
        buy_price = _parse_pct(ev.get("buy_price")) or 0
        signal_gain = (
            _parse_pct(ev.get("today_gain"))
            or _parse_pct(ev.get("signal_gain_pct"))
        )
        open_trades.append({
            "status": "open",
            "signalDate": ev.get("signal_date") or ev.get("buy_date"),
            "buyDate": ev.get("signal_date") or ev.get("buy_date"),
            "buyTime": ev.get("buy_time") or ev.get("signal_time"),
            "signalTime": (
                ev.get("signal_time")
                or _oos_signal_map.get(
                    (str(ev.get("signal_date") or ev.get("buy_date") or ""), str(ev.get("etf") or ""), "")
                )
            ),
            "sellDate": None,
            "etf": ev.get("etf"),
            "name": ev.get("name"),
            "buyPrice": round(buy_price, 4) if buy_price else None,
            "sellPrice": None,
            "signalGainPct": signal_gain,
            "returnPct": None,  # 持仓中, 无平仓收益
            "sellReason": None,
            "note": ev.get("note"),
        })

    # 4. 合并: 已平仓按 sellDate 倒序, 持仓中按 buyDate 倒序, 持仓中放最前面
    closed.sort(key=lambda x: x.get("sellDate") or "", reverse=True)
    open_trades.sort(key=lambda x: x.get("buyDate") or "", reverse=True)
    return open_trades + closed


def _trades_for_export(closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对齐 strategy-web Trade 类型, 转为前端可读的卖出原因."""
    REASON_LABELS = {
        "time_sell": "11:05定时",
        "trix_death_cross": "TRIX死叉",
    }
    out = []
    for c in closed:
        out.append({
            "status": c.get("status"),
            "signalDate": c.get("signalDate"),
            "buyDate": c.get("buyDate"),
            "sellDate": c.get("sellDate"),
            "buyTime": c.get("buyTime"),
            "sellTime": c.get("sellTime"),
            "signalTime": c.get("signalTime"),
            "etf": c.get("etf"),
            "name": c.get("name"),
            "buyPrice": c.get("buyPrice"),
            "sellPrice": c.get("sellPrice"),
            "signalGainPct": c.get("signalGainPct"),
            "returnPct": c.get("returnPct"),
            "sellReason": REASON_LABELS.get(c.get("sellReason") or "", c.get("sellReason")),
            "note": c.get("note"),
        })
    return out


def _compound(rets: list[float]) -> float:
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return (eq - 1) * 100


# ─── 净值曲线 ────────────────────────────────────────────────────────────────

def _nav_curve(closed: list[dict[str, Any]]) -> list[list[float]]:
    """构造累计涨幅曲线.

    规则: 以 sellDate 为时间点, 按 returnPct 复利累乘, 输出累计收益率%.
    返回 [[timestamp_ms, return_pct], ...]
    """
    curve: list[list[float]] = []
    nav = 1.0  # 净值(内部用, 用于复利计算)
    sorted_closed = sorted(
        [c for c in closed if c.get("sellDate") and c.get("returnPct") is not None],
        key=lambda x: x["sellDate"],
    )
    for c in sorted_closed:
        ret = float(c["returnPct"])
        nav *= 1 + ret / 100
        # 输出累计收益率% = (nav - 1) * 100
        # 注意: 不在此处 round, 保留高精度, 避免相邻累计值相减时误差被放大
        # (前端 tooltip 的"当日涨幅" = 相邻累计差, 需与明细单笔 returnPct 一致)
        cum_return_pct = (nav - 1) * 100
        try:
            ts = int(datetime.strptime(str(c["sellDate"]), "%Y-%m-%d").timestamp() * 1000)
        except ValueError:
            continue
        curve.append([ts, cum_return_pct])
    return curve


def _backtest_curve(strategy_id: str) -> list[list[float]]:
    """回测累计涨幅曲线: 用 OOS 文件逐笔交易复利累乘生成."""
    trades = _backtest_trades(strategy_id)
    if not trades:
        return []
    curve: list[list[float]] = []
    nav = 1.0
    sorted_trades = sorted(
        [t for t in trades if t.get("sell_date") and t.get("return_pct") is not None],
        key=lambda x: x["sell_date"],
    )
    for t in sorted_trades:
        ret = float(t["return_pct"])
        nav *= 1 + ret / 100
        # 不提前 round, 保留高精度, 与明细单笔 returnPct 对齐
        cum_pct = (nav - 1) * 100
        try:
            ts = int(datetime.strptime(str(t["sell_date"]), "%Y-%m-%d").timestamp() * 1000)
        except ValueError:
            continue
        curve.append([ts, cum_pct])
    return curve


def _backtest_trades_export(strategy_id: str) -> list[dict[str, Any]]:
    """输出回测逐笔交易明细."""
    trades = _backtest_trades(strategy_id)
    REASON_LABELS = {
        "time_sell": "11:05定时",
        "trix_death_cross": "TRIX死叉",
    }
    out = []
    for t in trades:
        out.append({
            "status": "closed",
            "signalDate": t.get("signal_date"),
            "buyDate": t.get("signal_date"),
            "sellDate": t.get("sell_date"),
            "buyTime": t.get("buy_time"),
            "sellTime": t.get("sell_time"),
            "signalTime": t.get("signal_time"),
            "etf": t.get("etf"),
            "name": t.get("name"),
            "buyPrice": round(float(t.get("buy_price") or 0), 4) if t.get("buy_price") else None,
            "sellPrice": round(float(t.get("sell_price") or 0), 4) if t.get("sell_price") else None,
            "signalGainPct": _parse_pct(t.get("today_gain")),
            "returnPct": round(float(t.get("return_pct") or 0), 2),
            "sellReason": REASON_LABELS.get(t.get("sell_reason") or "", t.get("sell_reason")),
            "note": "回测",
        })
    # 按平仓日倒序
    out.sort(key=lambda x: x.get("sellDate") or "", reverse=True)
    return out


# ─── 指标计算 ────────────────────────────────────────────────────────────────

def _annualized(total_return_pct: float, days: int) -> float:
    """累计收益 + 天数 -> 年化收益率."""
    if days <= 0:
        return 0.0
    end = 1 + total_return_pct / 100
    if end <= 0:
        return -100.0
    years = days / 365
    annual = (end ** (1 / years) - 1) * 100
    return round(annual, 2)


def _last_day_return(closed: list[dict[str, Any]]) -> float:
    """最近一个完整交易日的收益%."""
    today = date.today()
    # closed 已是倒序, 找第一笔 sellDate < 今天 的成交
    for c in closed:
        sell_date = c.get("sellDate")
        ret = c.get("returnPct")
        if not sell_date or ret is None:
            continue
        try:
            d = datetime.strptime(str(sell_date), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < today:
            return round(float(ret), 2)
    return 0.0


def _running_days(closed: list[dict[str, Any]]) -> int:
    """运行天数 = 最近一笔 - 第一笔 + 1."""
    if not closed:
        return 0
    dates: list[date] = []
    for c in closed:
        sd = c.get("sellDate") or c.get("buyDate") or c.get("signalDate")
        if not sd:
            continue
        try:
            dates.append(datetime.strptime(str(sd), "%Y-%m-%d").date())
        except ValueError:
            continue
    if not dates:
        return 0
    return (max(dates) - min(dates)).days + 1


def _start_date(closed: list[dict[str, Any]]) -> str:
    """最早的信号日."""
    dates: list[str] = []
    for c in closed:
        sd = c.get("signalDate") or c.get("buyDate") or c.get("sellDate")
        if sd:
            dates.append(str(sd))
    return min(dates) if dates else ""


# ─── 策略构造 ────────────────────────────────────────────────────────────────

def _read_state_position(state_file: Path) -> dict[str, Any] | None:
    """读 state 文件里的 position 字段, 如果 sold=False 表示持仓中."""
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        pos = data.get("position")
        if pos and isinstance(pos, dict) and not pos.get("sold", False):
            return pos
    except (json.JSONDecodeError, OSError):
        return None
    return None


def _build_open_trade_from_state(pos: dict[str, Any]) -> dict[str, Any]:
    """从 state 文件的 position 字段构造"持仓中"交易."""
    buy_price = _parse_pct(pos.get("buy_price")) or 0
    signal_gain = _parse_pct(pos.get("today_gain"))
    # 尝试从 OOS live_trades 按 (signal_date, etf) 补充 signal_time / buy_time
    _oos = _load_oos_backtest()
    _oos_sig = None
    _oos_buy = None
    if _oos:
        for t in _oos.get("live_trades", []):
            if str(t.get("signal_date", "")) == str(pos.get("buy_date", "")) and str(t.get("etf", "")) == str(pos.get("etf", "")):
                _oos_sig = t.get("signal_time")
                _oos_buy = t.get("buy_time")
                break
    return {
        "status": "open",
        "signalDate": pos.get("buy_date"),
        "buyDate": pos.get("buy_date"),
        "buyTime": pos.get("buy_time") or _oos_buy,
        "signalTime": pos.get("signal_time") or _oos_sig,
        "sellDate": None,
        "etf": pos.get("etf"),
        "name": pos.get("name"),
        "buyPrice": round(buy_price, 4) if buy_price else None,
        "sellPrice": None,
        "signalGainPct": signal_gain,
        "returnPct": None,
        "sellReason": None,
        "note": "持仓中(来自 state)",
    }


def build_live_strategy() -> dict[str, Any]:
    journal_path = ROTATION_DIR / "t0_trade_journal.jsonl"
    state_path = ROTATION_DIR / "t0_monitor_state.json"
    events = _read_jsonl(journal_path, skip_idle=False)
    closed = _merge_trades(events)
    rets = [float(c["returnPct"]) for c in closed if c.get("returnPct") is not None]

    # 检查 state 文件是否有持仓中的交易
    pos = _read_state_position(state_path)
    open_trade = _build_open_trade_from_state(pos) if pos else None
    if open_trade:
        # 避免重复: 如果 journal 里已经有同 etf + buy_date 的持仓中, 不再加 state 来源的
        has_open = any(
            t.get("status") == "open"
            and t.get("etf") == open_trade["etf"]
            and t.get("buyDate") == open_trade["buyDate"]
            for t in closed
        )
        if not has_open:
            all_trades = [open_trade] + closed
        else:
            all_trades = closed
    else:
        all_trades = closed

    total_return = round(_compound(rets), 2) if rets else 0.0
    running_days = _running_days(closed)
    daily_return = round(total_return / running_days, 4) if running_days else 0.0
    last_day = _last_day_return(closed)

    # 真实回测数据(从 OOS 文件读)
    wf = _backtest_summary("t0_baseline_trix")
    backtest_days = _backtest_days_count()
    backtest_total = wf["totalReturn"]
    backtest_annual = _annualized(backtest_total, backtest_days)
    bt_start, bt_end = _backtest_start_end_dates()

    start_date = _start_date(closed)

    return {
        "id": "t0_baseline_trix",
        "name": "T+0 涨幅TOP1 + TRIX卖出",
        "type": "动量",
        "status": "running",
        "description": (
            "hybrid-A 选股(优质池/原 T0 池, 按市场 regime 切换) + "
            "次日 5分K TRIX(5,3) 死叉卖出 / 11:05 定时 fallback。"
            "已实盘运行, 数据来自 t0_trade_journal.jsonl。"
        ),
        "tags": ["T+0", "ETF", "TRIX", "实盘"],
        "backtest": {
            "annualReturn": backtest_annual,
            "maxDrawdown": wf["maxDrawdown"],
            "sharpeRatio": wf["sharpe"],
            "winRate": wf["winRate"],
            "totalReturn": backtest_total,
            "backtestDays": backtest_days,
            "startDate": bt_start,
            "endDate": bt_end,
        },
        "live": {
            "dailyReturn": daily_return,
            "lastDayReturn": last_day,
            "totalReturn": total_return,
            "runningDays": running_days,
            "startDate": start_date,
        },
        "navCurve": _nav_curve(closed),
        "backtestCurve": _backtest_curve("t0_baseline_trix"),
        "trades": _trades_for_export(all_trades),
        "backtestTrades": _backtest_trades_export("t0_baseline_trix"),
    }


def build_shadow_strategy() -> dict[str, Any]:
    journal_path = ROTATION_DIR / "b_idle_journal.jsonl"
    state_path = ROTATION_DIR / "b_idle_shadow_state.json"
    # 关键: 跳过 idle_momentum 腿, 只展示 core_B
    events = _read_jsonl(journal_path, skip_idle=True)
    closed = _merge_trades(events)
    rets = [float(c["returnPct"]) for c in closed if c.get("returnPct") is not None]

    # 检查 state 文件是否有持仓中的交易
    pos = _read_state_position(state_path)
    open_trade = _build_open_trade_from_state(pos) if pos else None
    if open_trade:
        # 避免重复: 如果 journal 里已经有同 etf + buy_date 的持仓中, 不再加 state 来源的
        has_open = any(
            t.get("status") == "open"
            and t.get("etf") == open_trade["etf"]
            and t.get("buyDate") == open_trade["buyDate"]
            for t in closed
        )
        if not has_open:
            all_trades = [open_trade] + closed
        else:
            all_trades = closed
    else:
        all_trades = closed

    total_return = round(_compound(rets), 2) if rets else 0.0
    running_days = _running_days(closed)
    daily_return = round(total_return / running_days, 4) if running_days else 0.0
    last_day = _last_day_return(closed)

    # 真实回测数据: 从 OOS 文件读 shadow 的逐笔
    wf = _backtest_summary("t0_coreB_shadow")
    backtest_days = _backtest_days_count()
    backtest_total = wf["totalReturn"]
    backtest_annual = _annualized(backtest_total, backtest_days)
    bt_start, bt_end = _backtest_start_end_dates()

    start_date = _start_date(closed)

    return {
        "id": "t0_coreB_shadow",
        "name": "T+0 核心B 旁路 SHADOW",
        "type": "动量",
        "status": "running",
        "description": (
            "全市场 T0 ETF 当日涨幅 Top1 (≥3%, 不 regime 过滤, 14:40 双时点确认) + "
            "次日 09:40~11:05 纯 TRIX(5,3) 死叉卖出。"
            "与实盘平行运行、仅记录不下单; idle 腿已停用, 仅展示 core_B 部分。"
        ),
        "tags": ["T+0", "ETF", "TRIX", "shadow"],
        "backtest": {
            "annualReturn": backtest_annual,
            "maxDrawdown": wf["maxDrawdown"],
            "sharpeRatio": wf["sharpe"],
            "winRate": wf["winRate"],
            "totalReturn": backtest_total,
            "backtestDays": backtest_days,
            "startDate": bt_start,
            "endDate": bt_end,
        },
        "live": {
            "dailyReturn": daily_return,
            "lastDayReturn": last_day,
            "totalReturn": total_return,
            "runningDays": running_days,
            "startDate": start_date,
        },
        "navCurve": _nav_curve(closed),
        "backtestCurve": _backtest_curve("t0_coreB_shadow"),
        "trades": _trades_for_export(all_trades),
        "backtestTrades": _backtest_trades_export("t0_coreB_shadow"),
    }


# ─── 输出 ────────────────────────────────────────────────────────────────────

def _default_out_path() -> Path:
    """自动找 strategy-web/public/strategies.json.

    从脚本所在仓库的父目录找名为 strategy-web 的兄弟项目.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "strategies" / "registry.json").exists():
            candidate = parent.parent / "strategy-web" / "public" / "strategies.json"
            if candidate.parent.exists():
                return candidate
            break
    return Path("strategies.json")


def _scp_to_remote(local_path: Path, remote_target: str) -> None:
    print(f"→ scp {local_path} → {remote_target}")
    subprocess.run(
        ["scp", str(local_path), remote_target],
        check=True,
    )
    print("✓ 上传完成")


def main() -> int:
    parser = argparse.ArgumentParser(description="导出策略数据到 strategy-web")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="输出 JSON 路径 (默认: ../strategy-web/public/strategies.json)",
    )
    parser.add_argument(
        "--scp",
        metavar="REMOTE",
        default=None,
        help="scp 上传到远程, 如 user@host:/var/www/strategies.json",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只检查数据文件是否存在, 不输出",
    )
    args = parser.parse_args()

    # 数据文件检查
    print(f"数据目录: {ROTATION_DIR}")
    files = [
        ("t0_trade_journal.jsonl", "实盘交易日志"),
        ("b_idle_journal.jsonl", "Shadow 日志"),
        ("t0_monitor_state.json", "实盘状态(可选)"),
        ("b_idle_shadow_state.json", "Shadow 状态(可选)"),
    ]
    for name, label in files:
        p = ROTATION_DIR / name
        status = "✓" if p.exists() else "✗"
        print(f"  {status} {name:30s} — {label}")

    if args.check_only:
        return 0

    print("\n构造策略数据...")
    strategies = [build_live_strategy(), build_shadow_strategy()]

    out_path = args.out or _default_out_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(strategies, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n✓ 已写入: {out_path}")
    print(f"  策略数量: {len(strategies)}")
    for s in strategies:
        print(f"  - {s['id']}: {s['name']} (运行 {s['live']['runningDays']} 天, 累计 {s['live']['totalReturn']}%)")

    if args.scp:
        _scp_to_remote(out_path, args.scp)

    return 0


if __name__ == "__main__":
    sys.exit(main())

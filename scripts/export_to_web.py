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

# ─── WF 回测摘要 (硬编码, 来自 b_idle_shadow_table.py 第 28-37 行) ────────────
# 近390 OOS 一组, 既反映样本外表现又不过分
WF_SUMMARY = {
    "live": {  # 实盘 A+确认+TRIX (近390 OOS)
        "totalReturn": 284.05,
        "tradeCount": 189,
        "winRate": 61,
        "maxDrawdown": 13.7,
        "annualReturn": None,  # 回测年化需要从累计收益和天数推算
    },
    "shadow": {  # SHADOW 核心 B+确认+TRIX (近390 OOS)
        "totalReturn": 319.16,
        "tradeCount": 231,
        "winRate": 63,
        "maxDrawdown": 17.6,
        "annualReturn": None,
    },
    "backtest_days": 390,
    "backtest_start_offset_days": 390,  # OOS 区间长度
}


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
    """把 buy(signal) 和 sell 事件合并为完整交易."""
    buys: dict[str, dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []
    for ev in events:
        if "buy_price" in ev and "sell_price" not in ev:
            key = f"{ev.get('signal_date')}:{ev.get('etf')}:{ev.get('leg')}"
            buys[key] = ev
        elif "sell_price" in ev:
            key = f"{ev.get('signal_date')}:{ev.get('etf')}:{ev.get('leg')}"
            buy = buys.get(key, {})
            buy_price = float(ev.get("buy_price") or buy.get("buy_price") or 0)
            sell_price = float(ev.get("sell_price") or 0)
            ret = _parse_pct(ev.get("return_pct"))
            if ret is None and buy_price and sell_price:
                ret = (sell_price - buy_price) / buy_price * 100
            closed.append({
                "signalDate": ev.get("signal_date") or buy.get("signal_date"),
                "buyDate": buy.get("buy_date") or ev.get("signal_date"),
                "sellDate": ev.get("sell_date"),
                "etf": ev.get("etf") or buy.get("etf"),
                "name": ev.get("name") or buy.get("name"),
                "buyPrice": round(buy_price, 4) if buy_price else None,
                "sellPrice": round(sell_price, 4) if sell_price else None,
                "signalGainPct": _parse_pct(buy.get("today_gain")),
                "returnPct": round(float(ret), 2) if ret is not None else None,
                "sellReason": ev.get("sell_reason"),
                "note": ev.get("note"),
            })
    # 按平仓日倒序(最近交易在上)
    closed.sort(key=lambda x: x.get("sellDate") or "", reverse=True)
    return closed


def _trades_for_export(closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对齐 strategy-web Trade 类型, 转为前端可读的卖出原因."""
    REASON_LABELS = {
        "time_sell": "11:05定时",
        "trix_death_cross": "TRIX死叉",
    }
    out = []
    for c in closed:
        out.append({
            "signalDate": c.get("signalDate"),
            "buyDate": c.get("buyDate"),
            "sellDate": c.get("sellDate"),
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
    """从交易流水构造净值曲线.

    规则: 以 sellDate 为时间点, 按 returnPct 复利累乘得净值.
    返回 [[timestamp_ms, nav], ...]
    """
    curve: list[list[float]] = []
    nav = 1.0
    # 用 sellDate 排序(正序, 旧的在前), 然后倒推净值
    sorted_closed = sorted(
        [c for c in closed if c.get("sellDate") and c.get("returnPct") is not None],
        key=lambda x: x["sellDate"],
    )
    for c in sorted_closed:
        ret = float(c["returnPct"])
        nav *= 1 + ret / 100
        try:
            ts = int(datetime.strptime(str(c["sellDate"]), "%Y-%m-%d").timestamp() * 1000)
        except ValueError:
            continue
        curve.append([ts, round(nav, 4)])
    return curve


def _backtest_curve(total_return_pct: float, days: int) -> list[list[float]]:
    """回测净值曲线: 简单线性插值, 仅用于展示形状."""
    curve: list[list[float]] = []
    if days <= 0:
        return curve
    end = (1 + total_return_pct / 100)
    now_ms = int(datetime.now().timestamp() * 1000)
    for i in range(days + 1):
        if i == 0:
            nav = 1.0
        else:
            nav = 1.0 + (end - 1.0) * (i / days)
        ts = now_ms - (days - i) * 86400000
        curve.append([ts, round(nav, 4)])
    return curve


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

def build_live_strategy() -> dict[str, Any]:
    journal_path = ROTATION_DIR / "t0_trade_journal.jsonl"
    events = _read_jsonl(journal_path, skip_idle=False)
    closed = _merge_trades(events)
    rets = [float(c["returnPct"]) for c in closed if c.get("returnPct") is not None]

    total_return = round(_compound(rets), 2) if rets else 0.0
    running_days = _running_days(closed)
    daily_return = round(total_return / running_days, 4) if running_days else 0.0
    last_day = _last_day_return(closed)

    wf = WF_SUMMARY["live"]
    backtest_days = WF_SUMMARY["backtest_days"]
    backtest_total = wf["totalReturn"]
    backtest_annual = _annualized(backtest_total, backtest_days)

    start_date = _start_date(closed)
    backtest_end_date = (date.today() - timedelta(days=1)).isoformat()
    backtest_start_date = (date.today() - timedelta(days=backtest_days)).isoformat()

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
            "sharpeRatio": 0.0,  # 当前无数据, 后续可补
            "winRate": wf["winRate"],
            "totalReturn": backtest_total,
            "backtestDays": backtest_days,
            "startDate": backtest_start_date,
            "endDate": backtest_end_date,
        },
        "live": {
            "dailyReturn": daily_return,
            "lastDayReturn": last_day,
            "totalReturn": total_return,
            "runningDays": running_days,
            "startDate": start_date,
        },
        "navCurve": _nav_curve(closed),
        "backtestCurve": _backtest_curve(backtest_total, backtest_days),
        "trades": _trades_for_export(closed),
    }


def build_shadow_strategy() -> dict[str, Any]:
    journal_path = ROTATION_DIR / "b_idle_journal.jsonl"
    # 关键: 跳过 idle_momentum 腿, 只展示 core_B
    events = _read_jsonl(journal_path, skip_idle=True)
    closed = _merge_trades(events)
    rets = [float(c["returnPct"]) for c in closed if c.get("returnPct") is not None]

    total_return = round(_compound(rets), 2) if rets else 0.0
    running_days = _running_days(closed)
    daily_return = round(total_return / running_days, 4) if running_days else 0.0
    last_day = _last_day_return(closed)

    wf = WF_SUMMARY["shadow"]
    backtest_days = WF_SUMMARY["backtest_days"]
    backtest_total = wf["totalReturn"]
    backtest_annual = _annualized(backtest_total, backtest_days)

    start_date = _start_date(closed)
    backtest_end_date = (date.today() - timedelta(days=1)).isoformat()
    backtest_start_date = (date.today() - timedelta(days=backtest_days)).isoformat()

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
            "sharpeRatio": 0.0,
            "winRate": wf["winRate"],
            "totalReturn": backtest_total,
            "backtestDays": backtest_days,
            "startDate": backtest_start_date,
            "endDate": backtest_end_date,
        },
        "live": {
            "dailyReturn": daily_return,
            "lastDayReturn": last_day,
            "totalReturn": total_return,
            "runningDays": running_days,
            "startDate": start_date,
        },
        "navCurve": _nav_curve(closed),
        "backtestCurve": _backtest_curve(backtest_total, backtest_days),
        "trades": _trades_for_export(closed),
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

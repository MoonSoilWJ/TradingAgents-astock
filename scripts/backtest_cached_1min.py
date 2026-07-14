#!/usr/bin/env python3
"""用缓存的1分K数据回测 — 7月1日起有1分K数据（9个交易日）。

与5分K回测对比，验证1分K TRIX的精确性。

用法:
    python scripts/backtest_cached_1min.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import _calc_stats, fetch_sina_kline  # noqa: E402
from backtest_top1_minute import calc_trix, calc_trix_signal  # noqa: E402
from backtest_t0_etf import apply_net_return  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    TRIX_PERIOD, time_to_min, bar_clock, next_trading_day, MIN_GAIN,
    rank_by_today_gain, select_etf,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_DIR = Path.home() / ".tradingagents" / "rotation" / "min_cache"
FEE_PCT = 0.03
SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
TRIX_START = "09:40"
TRIX_END = "11:05"


def load_cached_1min(code: str, day: str) -> list[dict]:
    """从缓存加载1分K。"""
    f = CACHE_DIR / f"{code}_1min_{day}.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


def load_cached_5min(code: str, day: str) -> list[dict]:
    f = CACHE_DIR / f"{code}_5min_{day}.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


def price_at_time(bars: list[dict], target: str) -> float | None:
    target_min = time_to_min(target)
    best = None
    best_diff = 9999
    for b in bars:
        t = b.get("day", "").split(" ")[1][:5] if " " in b.get("day", "") else b.get("time", "")[:5]
        bt = time_to_min(t)
        if bt < target_min:
            diff = target_min - bt
            if diff < best_diff:
                best_diff = diff
                best = float(b["close"])
    return best


def simulate_trix_1min(
    buy_cost: float,
    today_bars: list[dict],
    next_bars: list[dict],
    period: int = TRIX_PERIOD,
    trix_start: str = TRIX_START,
    trix_end: str = TRIX_END,
) -> tuple[float, str, dict]:
    """1分K TRIX死叉卖。"""
    all_bars = today_bars + next_bars
    min_warmup = period * 3 + 5
    warmup_len = len(today_bars)

    start_min = time_to_min(trix_start)
    end_min = time_to_min(trix_end)

    if len(all_bars) < min_warmup:
        last = float(next_bars[-1]["close"]) if next_bars else buy_cost
        return (last - buy_cost) / buy_cost * 100, "close", {"sell_price": last}

    closes = [float(b["close"]) for b in all_bars]
    trix = calc_trix(closes, period)
    signal = calc_trix_signal(trix, max(period // 2, 3))
    search_start = max(warmup_len, min_warmup)

    last_idx = None
    for i in range(search_start, len(all_bars)):
        b = all_bars[i]
        t = b.get("day", "").split(" ")[1][:5] if " " in b.get("day", "") else ""
        bt = time_to_min(t)
        if bt < start_min or bt >= end_min:
            continue
        if i > 0 and trix[i-1] >= signal[i-1] and trix[i] < signal[i]:
            return (closes[i] - buy_cost) / buy_cost * 100, "trix_death_cross", {
                "sell_price": closes[i], "bar": b.get("day", ""),
            }
        last_idx = i

    if last_idx is not None:
        return (closes[last_idx] - buy_cost) / buy_cost * 100, "timed_sell_11:05", {
            "sell_price": closes[last_idx],
        }

    last = float(next_bars[-1]["close"]) if next_bars else buy_cost
    return (last - buy_cost) / buy_cost * 100, "close", {"sell_price": last}


def run_1min_backtest(etf_list, all_dates, eval_dates, fee_pct):
    trades = []
    for day in eval_dates:
        # 用5分K选股（1分K可能不够选股的涨幅计算）
        # 实际上选股用日K收盘价算涨幅，这里用5分K缓存
        from backtest_t0_today1 import rank_by_today_gain
        # 临时用5分K缓存构建 etf_5min
        etf_5min_day = {}
        for etf in etf_list:
            code = etf["code"]
            bars = load_cached_5min(code, day)
            if bars:
                etf_5min_day[code] = {day: bars}

        # 需要日K数据算涨幅排名 — 用5分K合成
        etf_daily_temp = {}
        for code, day_bars in etf_5min_day.items():
            if day in day_bars and day_bars[day]:
                prev_close = float(day_bars[day][0]["open"])  # 近似前日收盘=今日开盘
                partial = price_at_time(day_bars[day], SIGNAL_TIME)
                if partial:
                    gain = (partial - prev_close) / prev_close * 100
                    etf_daily_temp[code] = {"returns": [{"date": day, "close": partial, "open": prev_close, "return_pct": gain}]}

        # 简化选股：直接用5分K涨幅排名
        scores = []
        for etf in etf_list:
            code = etf["code"]
            bars = etf_5min_day.get(code, {}).get(day, [])
            if not bars:
                continue
            prev_close = float(bars[0]["open"])
            partial = price_at_time(bars, SIGNAL_TIME)
            if not partial or prev_close <= 0:
                continue
            gain = (partial - prev_close) / prev_close * 100
            scores.append((gain, etf))

        scores.sort(key=lambda x: x[0], reverse=True)
        if len(scores) < 2:
            continue

        # 过滤 gain >= MIN_GAIN
        picked = None
        for gain, etf in scores:
            if gain >= MIN_GAIN:
                picked = (gain, etf)
                break
        if not picked:
            continue

        gain, top1 = picked
        code = top1["code"]
        sell_day = next_trading_day(all_dates, day)
        if not sell_day:
            continue

        # 买入价（1分K）
        day_1min = load_cached_1min(code, day)
        buy_price = price_at_time(day_1min, BUY_TIME) if day_1min else price_at_time(load_cached_5min(code, day), BUY_TIME)
        if not buy_price or buy_price <= 0:
            continue

        # 卖出（1分K TRIX）
        sell_1min = load_cached_1min(code, sell_day)
        if not sell_1min:
            continue

        ret_pct, sell_reason, detail = simulate_trix_1min(
            buy_price, day_1min, sell_1min,
        )
        sell_price = detail.get("sell_price", buy_price)
        ret = apply_net_return(buy_price, sell_price, fee_pct)
        trades.append({
            "signal_date": day, "sell_date": sell_day,
            "sector": top1["name"], "etf": code,
            "today_gain": round(gain, 2),
            "buy_price": round(buy_price, 4),
            "sell_price": round(sell_price, 4),
            "sell_reason": sell_reason,
            "return_pct": ret,
        })

    rets = [t["return_pct"] for t in trades]
    stats = _calc_stats(rets) if rets else {}
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {"trades": trades, "trade_count": len(trades),
            "final_equity_pct": (eq - 1) * 100, "stats": stats}


def main():
    etf_list = get_all_t0_etfs()

    # 找有1分K缓存的日期
    dates_1min = set()
    for f in CACHE_DIR.glob("*_1min_*.json"):
        dates_1min.add(f.stem.split("_")[-1])
    dates_1min = sorted(dates_1min)

    print(f"=== 缓存1分K回测 ===")
    print(f"1分K缓存日期: {dates_1min[0]} ~ {dates_1min[-1]} ({len(dates_1min)}天)")

    # 需要前日warmup，所以从第2天开始
    eval_dates = dates_1min[1:]

    # 用5分K的all_dates
    all_dates = sorted(set(f.stem.split("_")[-1] for f in CACHE_DIR.glob("*_5min_*.json")))

    print(f"回测日期: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}天)\n")

    result = run_1min_backtest(etf_list, all_dates, eval_dates, FEE_PCT)

    st = result["stats"]
    print(f"{'='*60}")
    print(f"  1分K回测: {result['trade_count']}笔 {result['final_equity_pct']:+.2f}%")
    if st:
        print(f"  胜率: {st.get('win_rate',0):.1f}% 均笔: {st.get('avg',0):+.2f}%")

    trades = result["trades"]
    if trades:
        from collections import Counter
        print(f"  卖出原因: {dict(Counter(t['sell_reason'] for t in trades))}")
        print(f"\n  {'日期':>12} {'板块':14s} {'gain':>5s} {'买价':>7s} {'卖价':>7s} {'卖因':>18s} {'收益':>7s}")
        print("  " + "-" * 80)
        for t in trades:
            print(f"  {t['signal_date']:>12} {t['sector']:14s} {t['today_gain']:5.1f}% "
                  f"{t['buy_price']:7.4f} {t['sell_price']:7.4f} {t['sell_reason']:>18s} {t['return_pct']:+7.2f}%")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

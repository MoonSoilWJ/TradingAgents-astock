#!/usr/bin/env python3
"""分析11:05定时卖出的18笔在09:40~11:05期间的TRIX状态。

状态分类：
- always_below: TRIX一直在signal下方（全程死叉状态，没等到金叉后的死叉）
- always_above: TRIX一直在signal上方（全程金叉状态，没死叉）
- golden_only:  只有金叉没有死叉
- other: 其他
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1_minute import calc_trix, calc_trix_signal  # noqa: E402
from backtest_t0_etf import price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    TRIX_PERIOD, bars_for_trix, time_to_min, bar_clock,
    rank_by_today_gain, select_etf, next_trading_day, MIN_GAIN,
)
from backtest_t0_new import SIGNAL_TIME, BUY_TIME, TRIX_START, TRIX_END, simulate_trix_timed  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

FEE_PCT = 0.03


def analyze_trix_state(
    today_bars: list[dict],
    next_bars: list[dict],
    trix_start: str = TRIX_START,
    trix_end: str = TRIX_END,
    trix_period: int = TRIX_PERIOD,
) -> dict:
    """分析 09:40~11:05 期间的 TRIX 状态。"""
    all_bars = today_bars + next_bars
    min_warmup = trix_period * 3 + 5
    warmup_len = len(today_bars)

    if len(all_bars) < min_warmup:
        return {"state": "insufficient_data", "bars_in_window": 0}

    closes = [float(b.get("close", 0)) for b in all_bars]
    trix = calc_trix(closes, trix_period)
    signal = calc_trix_signal(trix, max(trix_period // 2, 3))

    start_min = time_to_min(trix_start)
    end_min = time_to_min(trix_end)

    # 收集窗口内的 TRIX vs signal 状态
    states = []  # (bar_time, trix, signal, above)
    for i in range(max(warmup_len, min_warmup), len(all_bars)):
        b = all_bars[i]
        bt = time_to_min(bar_clock(b))
        if bt < start_min or bt >= end_min:
            continue
        above = trix[i] >= signal[i]
        states.append({
            "time": bar_clock(b),
            "trix": round(trix[i], 4),
            "signal": round(signal[i], 4),
            "above": above,
        })

    if not states:
        return {"state": "no_bars", "bars_in_window": 0}

    above_count = sum(1 for s in states if s["above"])
    below_count = len(states) - above_count

    # 检测金叉和死叉
    golden_crosses = 0
    death_crosses = 0
    for i in range(1, len(states)):
        if not states[i-1]["above"] and states[i]["above"]:
            golden_crosses += 1
        if states[i-1]["above"] and not states[i]["above"]:
            death_crosses += 1

    # 分类
    if above_count == len(states):
        state = "always_above"  # 全程金叉（TRIX一直在signal上方）
    elif below_count == len(states):
        state = "always_below"  # 全程死叉（TRIX一直在signal下方）
    elif golden_crosses > 0 and death_crosses == 0:
        state = "golden_only"   # 只有金叉没死叉
    elif death_crosses > 0 and golden_crosses == 0:
        state = "death_only"    # 只有死叉没金叉（但不是死叉卖出？可能时间不满足）
    else:
        state = "mixed"

    return {
        "state": state,
        "bars_in_window": len(states),
        "above_count": above_count,
        "below_count": below_count,
        "golden_crosses": golden_crosses,
        "death_crosses": death_crosses,
        "first_above": states[0]["above"],
        "last_above": states[-1]["above"],
        "trix_start_val": states[0]["trix"],
        "trix_end_val": states[-1]["trix"],
        "signal_start_val": states[0]["signal"],
        "signal_end_val": states[-1]["signal"],
    }


def main():
    from backtest_t0_today1 import load_market_data
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=100)
    args = parser.parse_args()

    etf_list = get_all_t0_etfs()
    print(f"=== TRIX 状态分析 | {args.days}天 ===\n")

    etf_daily, etf_5min, all_dates, _ = load_market_data(etf_list, args.days)
    eval_dates = all_dates[-args.days:]

    results = []
    for day in eval_dates:
        scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, SIGNAL_TIME)
        if len(scores) < 2:
            continue
        picked = select_etf(scores, use_filter=True)
        if not picked:
            continue
        gain, top1 = picked
        code = top1["code"]
        sell_day = next_trading_day(all_dates, day)
        if not sell_day:
            continue

        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, BUY_TIME)
        if not buy_price or buy_price <= 0:
            buy_price = price_at_time(day_bars, SIGNAL_TIME)
        if not buy_price or buy_price <= 0:
            continue

        sell_bars = etf_5min.get(code, {}).get(sell_day, [])
        if not sell_bars:
            continue

        ret_pct, sell_reason, detail = simulate_trix_timed(
            buy_price, bars_for_trix(day_bars), bars_for_trix(sell_bars),
        )
        sell_price = detail.get("sell_price", buy_price)

        trix_info = analyze_trix_state(bars_for_trix(day_bars), bars_for_trix(sell_bars))

        results.append({
            "signal_date": day,
            "sell_date": sell_day,
            "sector": top1["name"],
            "etf": code,
            "today_gain": round(gain, 2),
            "buy_price": round(buy_price, 4),
            "sell_price": round(sell_price, 4),
            "sell_reason": sell_reason,
            "return_pct": round(ret_pct, 2),
            **trix_info,
        })

    # 分组分析
    from collections import defaultdict
    by_reason = defaultdict(list)
    for r in results:
        by_reason[r["sell_reason"]].append(r)

    print(f"总交易: {len(results)}笔\n")

    for reason in sorted(by_reason):
        items = by_reason[reason]
        rets = [r["return_pct"] for r in items]
        eq = 1.0
        for ret in rets:
            eq *= (1 + ret/100)
        wins = sum(1 for r in rets if r > 0)
        print(f"=== {reason} ({len(items)}笔 累计{(eq-1)*100:+.2f}% 胜率{wins}/{len(items)}) ===")

        if reason == "timed_sell_11:05":
            print(f"\n  {'日期':>12} {'板块':14s} {'gain':>5s} {'收益':>7s} {'状态':>14s} "
                  f"{'上方':>4s} {'下方':>4s} {'金叉':>4s} {'死叉':>4s} {'首态':>4s} {'末态':>4s}")
            print("  " + "-" * 100)
            state_counts = defaultdict(int)
            for r in sorted(items, key=lambda x: x["signal_date"]):
                state = r.get("state", "?")
                state_counts[state] += 1
                print(f"  {r['signal_date']:>12} {r['sector']:14s} {r['today_gain']:5.1f}% {r['return_pct']:+7.2f}% "
                      f"{state:>14s} {r.get('above_count',0):4d} {r.get('below_count',0):4d} "
                      f"{r.get('golden_crosses',0):4d} {r.get('death_crosses',0):4d} "
                      f"{'上' if r.get('first_above') else '下':>4s} {'上' if r.get('last_above') else '下':>4s}")
            print(f"\n  状态分布: {dict(state_counts)}")

        print()

    # walk-forward: 前50天参数验证后50天
    print(f"\n{'='*60}")
    print(f"=== Walk-Forward 验证 ===")
    print(f"{'='*60}")
    sorted_results = sorted(results, key=lambda x: x["signal_date"])
    half = len(sorted_results) // 2
    for label, subset in [("前半段(训练)", sorted_results[:half]), ("后半段(验证)", sorted_results[half:])]:
        eq = 1.0
        wins = 0
        for r in subset:
            eq *= (1 + r["return_pct"]/100)
            if r["return_pct"] > 0: wins += 1
        print(f"  {label}: {len(subset)}笔 {(eq-1)*100:+8.2f}% 胜率{wins}/{len(subset)}={wins/len(subset)*100:.0f}%")

    # 4段 walk-forward
    print(f"\n  4段 walk-forward:")
    seg = len(sorted_results) // 4
    for i in range(4):
        s = sorted_results[i*seg:(i+1)*seg if i<3 else len(sorted_results)]
        eq = 1.0
        wins = 0
        for r in s:
            eq *= (1 + r["return_pct"]/100)
            if r["return_pct"] > 0: wins += 1
        print(f"    第{i+1}段 {s[0]['signal_date']}~{s[-1]['signal_date']} ({len(s)}笔): {(eq-1)*100:+8.2f}% 胜率{wins}/{len(s)}={wins/len(s)*100:.0f}%")


if __name__ == "__main__":
    main()

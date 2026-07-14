#!/usr/bin/env python3
"""对比：09:40死叉直接卖 vs 不卖。

方案A（当前）: 09:40~11:05 找死叉卖出，没死叉则11:05定时卖
方案C（新增）: 09:40 如果已处于死叉状态（TRIX<signal），直接卖；否则同方案A
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import _calc_stats  # noqa: E402
from backtest_top1_minute import calc_trix, calc_trix_signal  # noqa: E402
from backtest_t0_etf import FEE_PCT, apply_net_return, price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    TRIX_PERIOD, bars_for_trix, time_to_min, bar_clock,
    rank_by_today_gain, select_etf, next_trading_day,
)
from backtest_t0_new import SIGNAL_TIME, BUY_TIME, TRIX_START, TRIX_END  # noqa: E402

SELL_ON_DEATH_AT_OPEN = True  # 方案C：09:40死叉状态直接卖


def simulate_trix_timed_v2(
    buy_cost: float,
    today_bars: list[dict],
    next_bars: list[dict],
    sell_on_death_at_open: bool = False,
    trix_period: int = TRIX_PERIOD,
    trix_start: str = TRIX_START,
    trix_end: str = TRIX_END,
) -> tuple[float, str, dict]:
    """次日 TRIX 卖出。sell_on_death_at_open=True 时09:40死叉直接卖。"""
    all_bars = today_bars + next_bars
    min_warmup = trix_period * 3 + 5
    warmup_len = len(today_bars)

    start_min = time_to_min(trix_start)
    end_min = time_to_min(trix_end)

    search_bars = []
    for i, b in enumerate(all_bars):
        if i < warmup_len:
            continue
        bt = time_to_min(bar_clock(b))
        if start_min <= bt < end_min:
            search_bars.append((i, b))

    if len(all_bars) < min_warmup:
        for i, b in search_bars:
            if time_to_min(bar_clock(b)) >= end_min - 5:
                sell_price = float(b.get("close", buy_cost))
                return (sell_price - buy_cost) / buy_cost * 100, "timed_sell", {"sell_price": sell_price}
        last_close = float(next_bars[-1].get("close", buy_cost)) if next_bars else buy_cost
        return (last_close - buy_cost) / buy_cost * 100, "close", {"sell_price": last_close}

    closes = [float(b.get("close", 0)) for b in all_bars]
    trix = calc_trix(closes, trix_period)
    signal = calc_trix_signal(trix, max(trix_period // 2, 3))
    search_start = max(warmup_len, min_warmup)

    # 方案C：09:40 第一根 bar 如果 TRIX < signal（死叉状态），直接卖
    if sell_on_death_at_open and search_bars:
        first_idx, first_bar = search_bars[0]
        if first_idx >= 1 and trix[first_idx] < signal[first_idx]:
            sell_price = closes[first_idx]
            return (sell_price - buy_cost) / buy_cost * 100, "death_at_open", {
                "sell_price": sell_price,
                "bar": first_bar.get("day", ""),
                "trix": trix[first_idx],
                "signal": signal[first_idx],
            }

    # 在 09:40~11:05 找死叉
    last_sell_bar_idx = None
    for i, b in search_bars:
        if i < search_start:
            continue
        if i > 0 and trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
            sell_price = closes[i]
            return (sell_price - buy_cost) / buy_cost * 100, "trix_death_cross", {
                "sell_price": sell_price, "bar": b.get("day", ""),
            }
        last_sell_bar_idx = i

    if last_sell_bar_idx is not None:
        sell_price = closes[last_sell_bar_idx]
        return (sell_price - buy_cost) / buy_cost * 100, "timed_sell_11:05", {
            "sell_price": sell_price,
            "bar": all_bars[last_sell_bar_idx].get("day", ""),
        }

    last_close = float(next_bars[-1].get("close", buy_cost)) if next_bars else buy_cost
    return (last_close - buy_cost) / buy_cost * 100, "close", {"sell_price": last_close}


def run_backtest(
    etf_list, etf_daily, etf_5min, all_dates, eval_dates, fee_pct,
    sell_on_death_at_open: bool,
) -> dict:
    trades = []
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

        ret_pct, sell_reason, detail = simulate_trix_timed_v2(
            buy_price, bars_for_trix(day_bars), bars_for_trix(sell_bars),
            sell_on_death_at_open=sell_on_death_at_open,
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=100)
    args = parser.parse_args()

    from t0_etf_list import get_all_t0_etfs
    from backtest_t0_today1 import load_market_data

    etf_list = get_all_t0_etfs()
    print(f"=== 09:40死叉直接卖 对比 ===\n")

    etf_daily, etf_5min, all_dates, _ = load_market_data(etf_list, args.days)
    eval_dates = all_dates[-args.days:]
    print(f"{eval_dates[0]}~{eval_dates[-1]} ({len(eval_dates)}日)\n")

    print(">>> 方案A（当前：09:40~11:05找死叉，无死叉11:05卖）...")
    result_a = run_backtest(etf_list, etf_daily, etf_5min, all_dates, eval_dates, FEE_PCT, sell_on_death_at_open=False)

    print(">>> 方案C（新增：09:40死叉状态直接卖，否则同A）...")
    result_c = run_backtest(etf_list, etf_daily, etf_5min, all_dates, eval_dates, FEE_PCT, sell_on_death_at_open=True)

    from collections import Counter

    print(f"\n{'='*70}")
    print(f"  方案对比 | {len(eval_dates)}天")
    print(f"{'='*70}")

    for label, r in [("方案A(不提前卖)", result_a), ("方案C(09:40死叉直接卖)", result_c)]:
        st = r["stats"]
        print(f"  {label}: {r['trade_count']}笔 {r['final_equity_pct']:+.2f}% "
              f"胜率{st.get('win_rate',0):.1f}% 均笔{st.get('avg',0):+.2f}% "
              f"回撤{st.get('max_drawdown',0):+.2f}% 夏普{st.get('sharpe',0):.2f}")
        reasons = Counter(t["sell_reason"] for t in r["trades"])
        print(f"    卖出原因: {dict(reasons)}")

    # 分段对比
    print(f"\n  分3段:")
    for label, r in [("A", result_a), ("C", result_c)]:
        trades = sorted(r["trades"], key=lambda t: t["sell_date"])
        total = len(trades)
        seg = total // 3
        parts = []
        for i in range(3):
            s = trades[i*seg:(i+1)*seg if i<2 else total]
            eq = 1.0
            for t in s:
                eq *= (1 + t["return_pct"]/100)
            parts.append(f"{(eq-1)*100:+.1f}%")
        print(f"    方案{label}: {' | '.join(parts)}")

    # 方案C中 death_at_open 的明细
    death_open = [t for t in result_c["trades"] if t["sell_reason"] == "death_at_open"]
    if death_open:
        print(f"\n  09:40死叉直接卖明细 ({len(death_open)}笔):")
        eq_do = 1.0
        wins_do = 0
        for t in sorted(death_open, key=lambda x: x["signal_date"]):
            eq_do *= (1 + t["return_pct"]/100)
            if t["return_pct"] > 0: wins_do += 1
            print(f"    {t['signal_date']} {t['sector']:14s} gain={t['today_gain']:.1f}% "
                  f"买@{t['buy_price']:.4f} 卖@{t['sell_price']:.4f} {t['return_pct']:+7.2f}%")
        print(f"    小计: {(eq_do-1)*100:+.2f}% 胜率{wins_do}/{len(death_open)}={wins_do/len(death_open)*100:.0f}%")

    # 对比同日差异
    a_map = {t["signal_date"]: t for t in result_a["trades"]}
    c_map = {t["signal_date"]: t for t in result_c["trades"]}
    diffs = []
    for day in sorted(a_map):
        if day in c_map:
            ta, tc = a_map[day], c_map[day]
            if ta["sell_reason"] != tc["sell_reason"] or abs(ta["return_pct"] - tc["return_pct"]) > 0.1:
                diffs.append((day, ta, tc))
    if diffs:
        print(f"\n  差异单 ({len(diffs)}笔):")
        print(f"    {'日期':>12} {'板块':14s} {'A卖因':>18s} {'A收益':>7s} {'C卖因':>18s} {'C收益':>7s} {'差值':>6s}")
        for day, ta, tc in diffs:
            print(f"    {day:>12} {ta['sector']:14s} {ta['sell_reason']:>18s} {ta['return_pct']:+7.2f}% "
                  f"{tc['sell_reason']:>18s} {tc['return_pct']:+7.2f}% {tc['return_pct']-ta['return_pct']:+6.2f}%")

    print(f"{'='*70}")

    out = Path.home() / ".tradingagents" / "rotation" / f"backtest_death_open_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"plan_a": result_a, "plan_c": result_c}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

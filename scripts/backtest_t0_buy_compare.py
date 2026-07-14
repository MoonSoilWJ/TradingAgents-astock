#!/usr/bin/env python3
"""T+0 ETF 买入方式对比：14:45直接买 vs 14:40信号后TRIX金叉买。

方案A: 14:45 信号 → 14:50 直接买（当前新策略）
方案B: 14:40 信号 → 14:40~14:55 TRIX金叉买，未触发则14:50兜底买
卖出: 次日 TRIX(5,3) 09:40~11:05 死叉卖 / 11:05定时卖

用法:
    python scripts/backtest_t0_buy_compare.py --days 100
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
from backtest_t0_etf import (  # noqa: E402
    FEE_PCT, apply_net_return, price_at_time,
)
from backtest_t0_today1 import (  # noqa: E402
    TRIX_PERIOD, bars_for_trix, time_to_min, bar_clock,
    rank_by_today_gain, select_etf, next_trading_day,
)
from backtest_t0_new import simulate_trix_timed  # noqa: E402

# 方案A参数
A_SIGNAL = "14:45"
A_BUY = "14:50"

# 方案B参数
B_SIGNAL = "14:40"
B_BUY_START = "14:40"   # TRIX金叉搜索起始
B_BUY_END = "14:55"     # TRIX金叉搜索结束
B_FALLBACK = "14:50"    # 兜底买入时点


def find_trix_golden_cross(
    today_bars: list[dict],
    prev_bars: list[dict],
    search_start: str,
    search_end: str,
    trix_period: int = TRIX_PERIOD,
) -> tuple[float | None, str]:
    """在当日 search_start~search_end 找 TRIX 金叉（TRIX 上穿 signal）。

    用前日 bars 做 warmup。
    返回 (buy_price, bar_time) 或 (None, "")。
    """
    all_bars = prev_bars + today_bars
    min_warmup = trix_period * 3 + 5
    warmup_len = len(prev_bars)

    if len(all_bars) < min_warmup:
        return None, ""

    closes = [float(b.get("close", 0)) for b in all_bars]
    trix = calc_trix(closes, trix_period)
    signal = calc_trix_signal(trix, max(trix_period // 2, 3))

    start_min = time_to_min(search_start)
    end_min = time_to_min(search_end)

    for i in range(max(warmup_len, min_warmup), len(all_bars)):
        b = all_bars[i]
        bt = time_to_min(bar_clock(b))
        if bt < start_min or bt >= end_min:
            continue
        # 金叉：前一根 TRIX < signal，当前根 TRIX >= signal
        if i > 0 and trix[i - 1] < signal[i - 1] and trix[i] >= signal[i]:
            return closes[i], b.get("day", "")
    return None, ""


def run_plan_a(
    etf_list, etf_daily, etf_5min, all_dates, eval_dates, fee_pct,
) -> dict:
    """方案A：14:45信号 → 14:50直接买。"""
    trades = []
    for day in eval_dates:
        scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, A_SIGNAL)
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
        buy_price = price_at_time(day_bars, A_BUY)
        if not buy_price or buy_price <= 0:
            buy_price = price_at_time(day_bars, A_SIGNAL)
        if not buy_price or buy_price <= 0:
            continue

        sell_bars = etf_5min.get(code, {}).get(sell_day, [])
        if not sell_bars:
            continue

        ret_pct, sell_reason, detail = simulate_trix_timed(
            buy_price, bars_for_trix(day_bars), bars_for_trix(sell_bars),
        )
        sell_price = detail.get("sell_price", buy_price)
        ret = apply_net_return(buy_price, sell_price, fee_pct)
        trades.append({
            "signal_date": day, "sell_date": sell_day,
            "sector": top1["name"], "etf": code,
            "today_gain": round(gain, 2),
            "buy_price": round(buy_price, 4),
            "buy_reason": "direct_14:50",
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


def run_plan_b(
    etf_list, etf_daily, etf_5min, all_dates, eval_dates, fee_pct,
) -> dict:
    """方案B：14:40信号 → TRIX金叉买 / 14:50兜底买。"""
    trades = []
    for day in eval_dates:
        scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, B_SIGNAL)
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
        # 前日 bars 用于 TRIX warmup
        day_idx = all_dates.index(day) if day in all_dates else -1
        prev_day = all_dates[day_idx - 1] if day_idx > 0 else None
        prev_bars = etf_5min.get(code, {}).get(prev_day, []) if prev_day else []

        # 找 TRIX 金叉
        cross_price, cross_time = find_trix_golden_cross(
            bars_for_trix(day_bars), bars_for_trix(prev_bars),
            B_BUY_START, B_BUY_END, TRIX_PERIOD,
        )

        if cross_price and cross_price > 0:
            buy_price = cross_price
            buy_reason = "trix_golden_cross"
        else:
            # 兜底 14:50 买
            buy_price = price_at_time(day_bars, B_FALLBACK)
            if not buy_price or buy_price <= 0:
                buy_price = price_at_time(day_bars, B_SIGNAL)
            if not buy_price or buy_price <= 0:
                continue
            buy_reason = "fallback_14:50"

        sell_bars = etf_5min.get(code, {}).get(sell_day, [])
        if not sell_bars:
            continue

        ret_pct, sell_reason, detail = simulate_trix_timed(
            buy_price, bars_for_trix(day_bars), bars_for_trix(sell_bars),
        )
        sell_price = detail.get("sell_price", buy_price)
        ret = apply_net_return(buy_price, sell_price, fee_pct)
        trades.append({
            "signal_date": day, "sell_date": sell_day,
            "sector": top1["name"], "etf": code,
            "today_gain": round(gain, 2),
            "buy_price": round(buy_price, 4),
            "buy_reason": buy_reason,
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


def print_compare(result_a: dict, result_b: dict, eval_days: int):
    from collections import Counter

    print(f"\n{'='*70}")
    print(f"  买入方式对比 | {eval_days}天 | T+0 ETF池 | 卖出: TRIX 09:40~11:05")
    print(f"{'='*70}")
    print(f"  方案A: {A_SIGNAL}信号 → {A_BUY}直接买")
    print(f"  方案B: {B_SIGNAL}信号 → TRIX金叉买 / {B_FALLBACK}兜底买")
    print()

    for label, r in [("方案A", result_a), ("方案B", result_b)]:
        st = r["stats"]
        print(f"  {label}: {r['trade_count']}笔 累计{r['final_equity_pct']:+.2f}% "
              f"胜率{st.get('win_rate',0):.1f}% 均笔{st.get('avg',0):+.2f}% "
              f"回撤{st.get('max_drawdown',0):+.2f}% 夏普{st.get('sharpe',0):.2f}")

    # 方案B买入原因分布
    b_reasons = Counter(t["buy_reason"] for t in result_b["trades"])
    print(f"\n  方案B买入原因: {dict(b_reasons)}")

    # 金叉买入 vs 兜底买入 收益对比
    cross_trades = [t for t in result_b["trades"] if t["buy_reason"] == "trix_golden_cross"]
    fallback_trades = [t for t in result_b["trades"] if t["buy_reason"] == "fallback_14:50"]
    if cross_trades:
        eq_c = 1.0
        wins_c = 0
        for t in cross_trades:
            eq_c *= (1 + t["return_pct"]/100)
            if t["return_pct"] > 0: wins_c += 1
        print(f"  金叉买入: {len(cross_trades)}笔 累计{(eq_c-1)*100:+.2f}% 胜率{wins_c}/{len(cross_trades)}={wins_c/len(cross_trades)*100:.0f}%")
    if fallback_trades:
        eq_f = 1.0
        wins_f = 0
        for t in fallback_trades:
            eq_f *= (1 + t["return_pct"]/100)
            if t["return_pct"] > 0: wins_f += 1
        print(f"  兜底买入: {len(fallback_trades)}笔 累计{(eq_f-1)*100:+.2f}% 胜率{wins_f}/{len(fallback_trades)}={wins_f/len(fallback_trades)*100:.0f}%")

    # 分3段对比
    print(f"\n  分3段对比:")
    for label, r in [("A", result_a), ("B", result_b)]:
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

    # 差异单（同日两方案买入价不同）
    a_map = {t["signal_date"]: t for t in result_a["trades"]}
    b_map = {t["signal_date"]: t for t in result_b["trades"]}
    diffs = []
    for day in a_map:
        if day in b_map:
            ta, tb = a_map[day], b_map[day]
            if abs(ta["buy_price"] - tb["buy_price"]) > 0.001:
                diffs.append((day, ta, tb))
    if diffs:
        print(f"\n  买入价差异单（{len(diffs)}笔）:")
        print(f"    {'日期':>12} {'板块':12s} {'A买价':>7s} {'A收益':>7s} {'B买价':>7s} {'B买因':>18s} {'B收益':>7s}")
        for day, ta, tb in diffs[:15]:
            print(f"    {day:>12} {ta['sector']:12s} {ta['buy_price']:7.4f} {ta['return_pct']:+7.2f}% "
                  f"{tb['buy_price']:7.4f} {tb['buy_reason']:>18s} {tb['return_pct']:+7.2f}%")
    print(f"{'='*70}")


def main():
    parser = argparse.ArgumentParser(description="T+0 买入方式对比")
    parser.add_argument("--days", type=int, default=100)
    args = parser.parse_args()

    from t0_etf_list import get_all_t0_etfs
    from backtest_t0_today1 import load_market_data

    etf_list = get_all_t0_etfs()
    print(f"=== T+0 买入方式对比 ===")
    print(f"ETF池 {len(etf_list)}只 | 回测{args.days}日\n")

    etf_daily, etf_5min, all_dates, _ = load_market_data(etf_list, args.days)
    eval_dates = all_dates[-args.days:]
    print(f"日K {len(etf_daily)} 5分K {len(etf_5min)} | {eval_dates[0]}~{eval_dates[-1]} ({len(eval_dates)}日)\n")

    print(">>> 运行方案A (14:45信号/14:50直接买)...")
    result_a = run_plan_a(etf_list, etf_daily, etf_5min, all_dates, eval_dates, FEE_PCT)

    print(">>> 运行方案B (14:40信号/TRIX金叉买/14:50兜底)...")
    result_b = run_plan_b(etf_list, etf_daily, etf_5min, all_dates, eval_dates, FEE_PCT)

    print_compare(result_a, result_b, len(eval_dates))

    out = Path.home() / ".tradingagents" / "rotation" / f"backtest_buy_compare_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"plan_a": result_a, "plan_b": result_b}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

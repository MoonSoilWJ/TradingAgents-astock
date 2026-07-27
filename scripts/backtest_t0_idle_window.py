#!/usr/bin/env python3
"""T+0 闲置窗口回测 — 11:05 卖完 ~ 14:50 买前，独立 T+0 加一层。

实盘节奏（同一份资金）:
  D-1 14:50 买 → D 09:40~11:05 TRIX/定时卖 → 【11:05~14:50 闲置】→ D 14:50 再买

本脚本只在「前一日有基线持仓、当日 11:05 已平仓」的交易日，测试闲置窗口内
同日 T+0 往返是否正期望；与 14:50 隔夜腿互不重叠。

用法:
    python scripts/backtest_t0_idle_window.py --days 100
    python scripts/backtest_t0_idle_window.py --days 100 --grid
    python scripts/backtest_t0_idle_window.py --days 100 --combo 13:30,13:35,14:45
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from itertools import product
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import _calc_stats  # noqa: E402
from backtest_top1_intraday import check_sell_trigger  # noqa: E402
from backtest_top1_minute import calc_trix, calc_trix_signal  # noqa: E402
from backtest_t0_etf import apply_net_return, bar_time_min, price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    MIN_GAIN,
    TRIX_MIN_SELL,
    TRIX_PERIOD,
    bars_for_trix,
    load_market_data,
    rank_by_today_gain,
    resolve_eval_dates,
    select_etf,
    simulate_trix_cross_after,
    time_to_min,
)
from search_t0_time_combo import bars_until, precompute_picks  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

# 实盘基线时点
LIVE_SIGNAL = "14:45"
LIVE_BUY = "14:50"
LIVE_SELL_CUTOFF = "11:05"

# 闲置窗口边界（卖完才能买；14:50 前必须清掉）
IDLE_START = "11:05"
IDLE_END = "14:45"  # 留 5 分钟缓冲，早于 14:50 实盘买

GRID_SIGNALS = [
    "11:05", "11:15", "11:25",
    "13:00", "13:15", "13:30", "13:45",
    "14:00", "14:15", "14:30",
]
GRID_BUYS = [
    "11:10", "11:20", "11:30",
    "13:05", "13:20", "13:35", "13:50",
    "14:05", "14:20", "14:35",
]
GRID_SELLS = [
    "11:30", "14:00", "14:15", "14:30", "14:40", "14:45",
]
GRID_MIN_GAINS = [0.0, 1.0, 2.0, 3.0]

MIN_TRADES = 8


def prev_trading_day(all_dates: list[str], day: str) -> str | None:
    if day not in all_dates:
        return None
    idx = all_dates.index(day)
    if idx <= 0:
        return None
    return all_dates[idx - 1]


def in_idle_window(t: str) -> bool:
    tm = time_to_min(t)
    return time_to_min(IDLE_START) <= tm <= time_to_min(IDLE_END)


def valid_combo(signal: str, buy: str, sell: str) -> bool:
    if not (in_idle_window(signal) and in_idle_window(buy) and in_idle_window(sell)):
        return False
    if time_to_min(buy) <= time_to_min(signal):
        return False
    if time_to_min(sell) <= time_to_min(buy):
        return False
    return True


def idle_eligible_days(
    eval_dates: list[str],
    all_dates: list[str],
    baseline_picks: dict,
) -> list[str]:
    """仅保留「前一日 14:45 有基线信号 → 当日 11:05 已卖」的日期。"""
    days: list[str] = []
    for day in eval_dates:
        prev = prev_trading_day(all_dates, day)
        if not prev:
            continue
        if baseline_picks.get((LIVE_SIGNAL, prev)):
            days.append(day)
    return days


def pick_at_signal(
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    day: str,
    signal_time: str,
    min_gain: float,
    use_filter: bool,
) -> tuple[str, float, str] | None:
    scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, signal_time)
    if len(scores) < 2:
        return None
    if min_gain > 0:
        for gain, info in scores:
            if gain >= min_gain:
                return info["code"], gain, info["name"]
        return None
    if use_filter:
        picked = select_etf(scores, use_filter=True)
        if not picked:
            return None
        gain, info = picked
        return info["code"], gain, info["name"]
    gain, info = scores[0]
    return info["code"], gain, info["name"]


def sell_time_mode(
    day_bars: list[dict],
    buy_time: str,
    sell_cutoff: str,
    buy_price: float,
    fee_pct: float,
) -> tuple[float, str] | None:
    window = bars_until(day_bars, sell_cutoff)
    if not window:
        return None
    sell_price = float(window[-1]["close"])
    if sell_price <= 0:
        return None
    ret = apply_net_return(buy_price, sell_price, fee_pct)
    return ret, "time_sell"


def sell_trail_mode(
    day_bars: list[dict],
    buy_time: str,
    sell_cutoff: str,
    buy_price: float,
    fee_pct: float,
) -> tuple[float, str] | None:
    window = bars_until(day_bars, sell_cutoff)
    if not window:
        return None
    bm = time_to_min(buy_time)
    idx = 0
    for i, b in enumerate(window):
        if bar_time_min(b) <= bm:
            idx = i
    sp, reason, _ = check_sell_trigger(
        window, buy_price, idx,
        stop_loss_pct=-1.0, trail_trigger_pct=1.5, trail_drop_pct=0.4,
    )
    if sp is None:
        sp = float(window[-1]["close"])
        reason = "time_fallback"
    ret = apply_net_return(buy_price, sp, fee_pct)
    return ret, reason


def sell_trix_mode(
    day_bars: list[dict],
    buy_time: str,
    sell_cutoff: str,
    buy_price: float,
    fee_pct: float,
) -> tuple[float, str] | None:
    """窗口内 5 分 TRIX 死叉卖（暖场用 11:05 前 bars）。"""
    window = bars_until(day_bars, sell_cutoff)
    if not window:
        return None
    bm = time_to_min(buy_time)
    pre = [b for b in day_bars if bar_time_min(b) < bm]
    all_b = pre + window
    min_warmup = TRIX_PERIOD * 3 + 5
    if len(all_b) < min_warmup:
        return sell_time_mode(day_bars, buy_time, sell_cutoff, buy_price, fee_pct)

    closes = [float(b["close"]) for b in all_b]
    trix = calc_trix(closes, TRIX_PERIOD)
    sig = calc_trix_signal(trix, 3)
    warm = len(pre)
    start = max(warm, min_warmup)
    sell_min = time_to_min(TRIX_MIN_SELL)

    for i in range(start, len(all_b)):
        t = all_b[i].get("day", "").split(" ")[1][:5] if " " in all_b[i].get("day", "") else all_b[i].get("time", "")[:5]
        if time_to_min(t) < sell_min:
            continue
        if trix[i - 1] >= sig[i - 1] and trix[i] < sig[i]:
            sp = closes[i]
            ret = apply_net_return(buy_price, sp, fee_pct)
            return ret, "trix_death"

    sp = float(window[-1]["close"])
    ret = apply_net_return(buy_price, sp, fee_pct)
    return ret, "time_fallback"


def run_idle_combo(
    idle_days: list[str],
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    signal: str,
    buy: str,
    sell: str,
    sell_mode: str,
    min_gain: float,
    use_filter: bool,
    fee_pct: float,
) -> dict | None:
    trades: list[dict] = []
    for day in idle_days:
        picked = pick_at_signal(
            etf_list, etf_daily, etf_5min, day, signal, min_gain, use_filter
        )
        if not picked:
            continue
        code, gain, name = picked
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, buy)
        if not buy_price or buy_price <= 0:
            continue

        if sell_mode == "trail":
            out = sell_trail_mode(day_bars, buy, sell, buy_price, fee_pct)
        elif sell_mode == "trix":
            out = sell_trix_mode(day_bars, buy, sell, buy_price, fee_pct)
        else:
            out = sell_time_mode(day_bars, buy, sell, buy_price, fee_pct)
        if not out:
            continue
        ret, reason = out
        trades.append({
            "day": day,
            "etf": code,
            "name": name,
            "signal_time": signal,
            "buy_time": buy,
            "sell_time": sell,
            "today_gain": round(gain, 2),
            "return_pct": round(ret, 4),
            "sell_reason": reason,
        })

    if len(trades) < MIN_TRADES:
        return None

    rets = [t["return_pct"] for t in trades]
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    label = f"{signal}/{buy}→{sell}"
    if min_gain > 0:
        label += f" ≥{min_gain:.0f}%"
    label += f" [{sell_mode}]"
    return {
        "signal": signal,
        "buy": buy,
        "sell": sell,
        "sell_mode": sell_mode,
        "min_gain": min_gain,
        "label": label,
        "eligible_days": len(idle_days),
        "trade_count": len(trades),
        "coverage_pct": len(trades) / len(idle_days) * 100 if idle_days else 0,
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets),
        "trades": trades,
    }


def run_baseline_overnight_legs(
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
) -> dict:
    """实盘隔夜腿：D-1 14:50 买 → D 11:05 前 TRIX 卖。"""
    trades: list[dict] = []
    for day in eval_dates:
        prev = prev_trading_day(all_dates, day)
        if not prev:
            continue
        picked = picks.get((LIVE_SIGNAL, prev))
        if not picked:
            continue
        code, gain, name = picked
        buy_bars = etf_5min.get(code, {}).get(prev, [])
        buy_price = price_at_time(buy_bars, LIVE_BUY)
        if not buy_price or buy_price <= 0:
            continue
        sell_bars = etf_5min.get(code, {}).get(day, [])
        window = bars_until(sell_bars, LIVE_SELL_CUTOFF)
        if not window:
            continue
        _, reason, detail = simulate_trix_cross_after(
            buy_price,
            bars_for_trix(buy_bars),
            bars_for_trix(window),
            trix_period=TRIX_PERIOD,
            min_sell_time=TRIX_MIN_SELL,
        )
        sell_price = detail.get("sell_price") or float(window[-1]["close"])
        ret = apply_net_return(buy_price, float(sell_price), fee_pct)
        trades.append({
            "buy_date": prev,
            "sell_date": day,
            "etf": code,
            "return_pct": round(ret, 4),
            "sell_reason": reason,
        })

    rets = [t["return_pct"] for t in trades]
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {
        "trade_count": len(trades),
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets),
        "trades": trades,
    }


def combine_baseline_plus_idle(
    baseline: dict,
    idle: dict,
    all_dates: list[str],
) -> dict:
    """按日顺序复利：同日先记隔夜卖，再记闲置窗口 T+0。"""
    idle_by_day = {t["day"]: t for t in idle["trades"]}
    eq = 1.0
    legs = 0
    for bt in baseline["trades"]:
        eq *= 1 + bt["return_pct"] / 100
        legs += 1
        idle_t = idle_by_day.get(bt["sell_date"])
        if idle_t:
            eq *= 1 + idle_t["return_pct"] / 100
            legs += 1
    return {
        "legs": legs,
        "final_equity_pct": (eq - 1) * 100,
    }


def print_grid_top(results: list[dict], baseline: dict, idle_days: list[str]) -> None:
    results.sort(key=lambda x: x["final_equity_pct"], reverse=True)
    print()
    print("=" * 100)
    print(f"  闲置窗口 TOP 15（ eligible {len(idle_days)} 日 = 有前日基线仓、当日 11:05 已平 ）")
    print("=" * 100)
    print(f"  {'#':>3} {'组合':<42} {'笔':>4} {'覆盖':>6} {'累计':>9} {'胜率':>7} {'均笔':>7} {'+基线':>9}")
    print("  " + "-" * 96)
    for i, r in enumerate(results[:15], 1):
        st = r["stats"]
        combo = combine_baseline_plus_idle(baseline, r, [])
        print(
            f"  {i:>3} {r['label']:<42} {r['trade_count']:>4} "
            f"{r['coverage_pct']:>5.1f}% {r['final_equity_pct']:+8.2f}% "
            f"{st.get('win_rate', 0):6.1f}% {st.get('avg', 0):+6.2f}% "
            f"{combo['final_equity_pct']:+8.2f}%"
        )
    print("=" * 100)


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 11:05~14:45 闲置窗口回测")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--grid", action="store_true", help="网格搜索买卖时点")
    parser.add_argument("--quick-grid", action="store_true", help="午后窗口 time 模式快速网格")
    parser.add_argument("--combo", type=str, default="", help="signal,buy,sell 如 13:30,13:35,14:45")
    parser.add_argument("--sell-mode", choices=["time", "trail", "trix"], default="time")
    parser.add_argument("--min-gain", type=float, default=0.0)
    args = parser.parse_args()

    print(f"=== T+0 闲置窗口回测 ({args.days} 日) ===")
    print(f"    窗口: {IDLE_START} 卖完 → {IDLE_END} 前清仓（14:50 实盘买不受影响）")

    etf_list = get_all_t0_etfs()
    etf_daily, etf_5min, all_dates, proxy_klines = load_market_data(etf_list, args.days)
    eval_dates = resolve_eval_dates(all_dates, args.days, "", "")
    baseline_picks = precompute_picks(
        etf_list, etf_daily, etf_5min, eval_dates, [LIVE_SIGNAL],
        proxy_klines, use_filter=True, skip_choppy=True,
    )
    idle_days = idle_eligible_days(eval_dates, all_dates, baseline_picks)
    baseline = run_baseline_overnight_legs(eval_dates, all_dates, baseline_picks, etf_5min, args.fee)

    print(f"\n>>> 基线隔夜腿: {baseline['trade_count']} 笔, 累计 {baseline['final_equity_pct']:+.2f}%")
    print(f">>> 可插闲置窗口的交易日: {len(idle_days)} 天")

    results: list[dict] = []

    if args.combo:
        parts = [p.strip() for p in args.combo.split(",")]
        if len(parts) != 3:
            print("ERROR: --combo 格式 signal,buy,sell")
            sys.exit(1)
        signal, buy, sell = parts
        if not valid_combo(signal, buy, sell):
            print(f"ERROR: 组合不在闲置窗口内: {signal}/{buy}/{sell}")
            sys.exit(1)
        r = run_idle_combo(
            idle_days, etf_list, etf_daily, etf_5min,
            signal, buy, sell, args.sell_mode, args.min_gain, True, args.fee,
        )
        if not r:
            print("ERROR: 有效交易不足")
            sys.exit(1)
        results = [r]
    elif args.grid or args.quick_grid:
        if args.quick_grid:
            signals = ["13:00", "13:15", "13:30", "13:45", "14:00", "14:15", "14:30"]
            buys = ["13:05", "13:20", "13:35", "13:50", "14:05", "14:20", "14:35"]
            sells = ["14:30", "14:40", "14:45"]
            sell_modes = ["time"]
            min_gains = [0.0, 1.0, 2.0, 3.0]
        else:
            signals, buys, sells = GRID_SIGNALS, GRID_BUYS, GRID_SELLS
            sell_modes = ["time", "trail", "trix"]
            min_gains = GRID_MIN_GAINS
        for signal, buy, sell, mg, sm in product(
            signals, buys, sells, min_gains, sell_modes
        ):
            if not valid_combo(signal, buy, sell):
                continue
            r = run_idle_combo(
                idle_days, etf_list, etf_daily, etf_5min,
                signal, buy, sell, sm, mg, mg >= MIN_GAIN, args.fee,
            )
            if r:
                results.append(r)
        print_grid_top(results, baseline, idle_days)
        # 正收益筛选
        positive = [r for r in results if r["final_equity_pct"] > 0]
        if positive:
            print(f"\n  正收益组合: {len(positive)} / {len(results)}")
            for r in positive[:5]:
                combo = combine_baseline_plus_idle(baseline, r, all_dates)
                print(
                    f"    {r['label']}: idle {r['final_equity_pct']:+.2f}% | "
                    f"+基线 {combo['final_equity_pct']:+.2f}%"
                )
        else:
            print(f"\n  正收益组合: 0 / {len(results)} — 闲置窗口内未找到稳定正期望")
    else:
        # 默认跑一组常用组合 + 小网格 time 模式
        candidates = [
            ("13:30", "13:35", "14:45", "time", 0.0),
            ("13:30", "13:35", "14:45", "time", 2.0),
            ("13:45", "13:50", "14:45", "time", 0.0),
            ("14:00", "14:05", "14:45", "time", 0.0),
            ("14:15", "14:20", "14:45", "time", 0.0),
            ("11:15", "11:20", "11:30", "time", 2.0),
            ("13:30", "13:35", "14:45", "trail", 0.0),
            ("13:30", "13:35", "14:45", "trix", 0.0),
        ]
        for signal, buy, sell, sm, mg in candidates:
            if not valid_combo(signal, buy, sell):
                continue
            r = run_idle_combo(
                idle_days, etf_list, etf_daily, etf_5min,
                signal, buy, sell, sm, mg, mg >= MIN_GAIN, args.fee,
            )
            if r:
                results.append(r)
        results.sort(key=lambda x: x["final_equity_pct"], reverse=True)

    if not results:
        print("无有效闲置窗口策略")
        sys.exit(1)

    best = max(results, key=lambda x: x["final_equity_pct"])
    combined = combine_baseline_plus_idle(baseline, best, all_dates)

    print()
    print("─" * 80)
    print("  闲置窗口策略明细（最优或指定组合）")
    print("─" * 80)
    st = best["stats"]
    print(f"  组合: {best['label']}")
    print(f"  Eligible 日: {best['eligible_days']} | 成交: {best['trade_count']} ({best['coverage_pct']:.1f}%)")
    print(f"  闲置窗口 alone: {best['final_equity_pct']:+.2f}% | 胜率 {st.get('win_rate', 0):.1f}% | 均笔 {st.get('avg', 0):+.2f}%")
    print(f"  基线 alone:     {baseline['final_equity_pct']:+.2f}%")
    print(f"  基线 + 闲置:    {combined['final_equity_pct']:+.2f}%  (Δ {combined['final_equity_pct'] - baseline['final_equity_pct']:+.2f} pp)")

    if best["final_equity_pct"] <= 0:
        print("\n  ⚠ 闲置窗口 alone 非正收益 — 不建议叠加到实盘")
    else:
        print("\n  ✓ 闲置窗口 alone 正收益 — 可考虑 shadow 验证后叠加")

    if not args.grid:
        print(f"\n  {'日期':>12} {'ETF':>8} {'涨%':>6} {'买':>5} {'卖':>5} {'收益':>8} {'原因'}")
        print("  " + "-" * 70)
        for t in best["trades"][-15:]:
            print(
                f"  {t['day']:>12} {t['etf']:>8} {t['today_gain']:+5.1f}% "
                f"{t['buy_time']:>5} {t['sell_time']:>5} {t['return_pct']:+7.2f}% {t['sell_reason']}"
            )

    out = Path.home() / ".tradingagents" / "rotation" / (
        f"backtest_t0_idle_{datetime.now():%Y%m%d_%H%M}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "config": {
            "days": args.days,
            "idle_window": [IDLE_START, IDLE_END],
            "live_buy": LIVE_BUY,
            "eligible_days": len(idle_days),
        },
        "baseline": {k: v for k, v in baseline.items() if k != "trades"},
        "best_idle": {k: v for k, v in best.items() if k != "trades"},
        "combined": combined,
        "grid_count": len(results),
        "top5": [{k: v for k, v in r.items() if k != "trades"} for r in sorted(results, key=lambda x: -x["final_equity_pct"])[:5]],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

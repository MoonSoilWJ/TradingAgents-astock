#!/usr/bin/env python3
"""T+0 卖点三方案对比：海龟2N / 吊灯止损 / 5分K OBV。

固定买点（与实盘一致）：14:45 信号 / 14:50 买入 / ≥3% / 501018 震荡跳过
仅改卖出时机，对比 4 套方案（含当前实盘 TRIX 基线）：
  1. 海龟 2N：最高价 - 2×ATR(20) 追踪止损
  2. 吊灯止损：近 22 根 5 分 K 最高价 - 3×ATR(20)
  3. 5 分 K OBV 死叉（OBV 下穿 MA5）
  4. 实盘基线：5 分 TRIX(5,3) ≥09:40 ≤11:05

卖出窗口：次日 09:40~11:05，超时 11:05 定时卖。

用法:
    python scripts/backtest_t0_sell_exit_compare.py --days 30
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
from backtest_top1_minute import calc_obv  # noqa: E402
from backtest_t0_etf import bar_time_min, price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    TRIX_PERIOD,
    apply_net_return,
    bar_clock,
    load_market_data,
    time_to_min,
)
from search_t0_time_combo import bars_until, precompute_picks, simulate_exit  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
SELL_START = "09:40"
SELL_CUTOFF = "11:05"
MIN_TRADES = 2

ATR_PERIOD = 20
TURTLE_N = 2
CHANDELIER_N = 3
CHANDELIER_LOOKBACK = 22
OBV_MA_PERIOD = 5

VARIANTS = [
    {"key": "turtle_2n", "label": "海龟2N止损"},
    {"key": "chandelier", "label": "吊灯止损"},
    {"key": "obv_5m", "label": "5分K OBV死叉"},
    {"key": "baseline_trix", "label": "实盘TRIX(5,3)"},
]


def calc_atr_series(bars: list[dict], period: int = ATR_PERIOD) -> list[float | None]:
    """Wilder ATR on OHLC bars."""
    if not bars:
        return []
    trs: list[float] = []
    for i, b in enumerate(bars):
        h, l = float(b.get("high", 0)), float(b.get("low", 0))
        if i == 0:
            trs.append(h - l)
        else:
            pc = float(bars[i - 1].get("close", 0))
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr: list[float | None] = [None] * len(bars)
    if len(trs) < period:
        return atr
    s = sum(trs[:period]) / period
    atr[period - 1] = s
    for i in range(period, len(trs)):
        s = (s * (period - 1) + trs[i]) / period
        atr[i] = s
    return atr


def sell_window_indices(
    all_bars: list[dict],
    warmup_len: int,
    sell_start: str = SELL_START,
    sell_cutoff: str = SELL_CUTOFF,
) -> list[int]:
    start_min = time_to_min(sell_start)
    end_min = time_to_min(sell_cutoff)
    out: list[int] = []
    for i in range(warmup_len, len(all_bars)):
        bt = time_to_min(bar_clock(all_bars[i]))
        if start_min <= bt <= end_min:
            out.append(i)
    return out


def timed_sell_price(all_bars: list[dict], indices: list[int]) -> tuple[float, str]:
    if not indices:
        return 0.0, "no_data"
    last_i = indices[-1]
    return float(all_bars[last_i].get("close", 0)), "timed_sell_11:05"


def simulate_turtle_2n(
    buy_price: float,
    today_bars: list[dict],
    next_bars: list[dict],
    atr_period: int = ATR_PERIOD,
    n_mult: float = TURTLE_N,
) -> tuple[float, str]:
    """海龟 2N 追踪止损：stop = max(入场-2N, 持仓最高-2N)。"""
    window = bars_until(next_bars, SELL_CUTOFF)
    if not window:
        return 0.0, "no_data"
    all_bars = today_bars + window
    warmup_len = len(today_bars)
    atr = calc_atr_series(all_bars, atr_period)
    indices = sell_window_indices(all_bars, warmup_len)

    buy_idx = max(0, warmup_len - 1)
    entry_atr = atr[buy_idx] if buy_idx < len(atr) else None
    initial_stop = buy_price - n_mult * entry_atr if entry_atr else buy_price * 0.98

    highest = buy_price
    for i in indices:
        h = float(all_bars[i].get("high", 0))
        l = float(all_bars[i].get("low", 0))
        highest = max(highest, h)
        cur_atr = atr[i]
        if not cur_atr:
            continue
        trail_stop = highest - n_mult * cur_atr
        stop = max(initial_stop, trail_stop)
        if l <= stop:
            sell_price = min(stop, float(all_bars[i].get("open", stop)))
            return sell_price, "turtle_2n_stop"

    sp, reason = timed_sell_price(all_bars, indices)
    return sp, reason


def simulate_chandelier(
    buy_price: float,
    today_bars: list[dict],
    next_bars: list[dict],
    atr_period: int = ATR_PERIOD,
    n_mult: float = CHANDELIER_N,
    lookback: int = CHANDELIER_LOOKBACK,
) -> tuple[float, str]:
    """吊灯止损：近 lookback 根最高价 - N×ATR。"""
    window = bars_until(next_bars, SELL_CUTOFF)
    if not window:
        return 0.0, "no_data"
    all_bars = today_bars + window
    warmup_len = len(today_bars)
    atr = calc_atr_series(all_bars, atr_period)
    indices = sell_window_indices(all_bars, warmup_len)

    for i in indices:
        lb_start = max(0, i - lookback + 1)
        hh = max(float(all_bars[j].get("high", 0)) for j in range(lb_start, i + 1))
        cur_atr = atr[i]
        if not cur_atr:
            continue
        stop = hh - n_mult * cur_atr
        l = float(all_bars[i].get("low", 0))
        if l <= stop:
            sell_price = min(stop, float(all_bars[i].get("open", stop)))
            return sell_price, "chandelier_stop"

    sp, reason = timed_sell_price(all_bars, indices)
    return sp, reason


def calc_obv_ma(obv: list[float], ma_period: int) -> list[float]:
    ma: list[float] = []
    for i in range(len(obv)):
        if i < ma_period - 1:
            ma.append(obv[i])
        else:
            window = obv[i - ma_period + 1:i + 1]
            ma.append(sum(window) / len(window))
    return ma


def simulate_obv_5m(
    buy_price: float,
    today_bars: list[dict],
    next_bars: list[dict],
    ma_period: int = OBV_MA_PERIOD,
) -> tuple[float, str]:
    """5 分 K OBV 死叉，仅在 09:40~11:05 生效。"""
    window = bars_until(next_bars, SELL_CUTOFF)
    if not window:
        return 0.0, "no_data"
    all_bars = today_bars + window
    warmup_len = len(today_bars)
    if len(all_bars) < ma_period + 2:
        sp, reason = timed_sell_price(all_bars, sell_window_indices(all_bars, warmup_len))
        return sp or buy_price, reason

    obv = calc_obv(all_bars)
    obv_ma = calc_obv_ma(obv, ma_period)
    indices = sell_window_indices(all_bars, warmup_len)
    search_start = max(warmup_len, ma_period + 1)

    for i in indices:
        if i < search_start:
            continue
        if obv[i - 1] >= obv_ma[i - 1] and obv[i] < obv_ma[i]:
            return float(all_bars[i].get("close", buy_price)), "obv_death_cross"

    sp, reason = timed_sell_price(all_bars, indices)
    return sp, reason


def simulate_baseline_trix(
    buy_price: float,
    today_bars: list[dict],
    next_bars: list[dict],
) -> tuple[float, str]:
    return simulate_exit(
        "trix0940_cut", buy_price, today_bars, BUY_TIME, next_bars, SELL_CUTOFF,
        trix_period=TRIX_PERIOD, trix_signal_period=3,
    )


SELL_SIMULATORS = {
    "turtle_2n": simulate_turtle_2n,
    "chandelier": simulate_chandelier,
    "obv_5m": simulate_obv_5m,
    "baseline_trix": simulate_baseline_trix,
}


def run_variant(
    key: str,
    label: str,
    etf_5min: dict,
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    fee_pct: float,
) -> dict | None:
    sim_fn = SELL_SIMULATORS[key]
    rets: list[float] = []
    trades: list[dict] = []

    for day in eval_dates:
        picked = picks.get((SIGNAL_TIME, day))
        if not picked:
            continue
        code, gain, name = picked

        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, BUY_TIME)
        if not buy_price or buy_price <= 0:
            continue

        if day not in all_dates:
            continue
        idx = all_dates.index(day)
        if idx + 1 >= len(all_dates):
            continue
        next_day = all_dates[idx + 1]
        next_bars = etf_5min.get(code, {}).get(next_day, [])
        if not next_bars:
            continue

        sell_price, sell_reason = sim_fn(buy_price, day_bars, next_bars)
        if sell_price is None or sell_price <= 0:
            continue

        ret = apply_net_return(buy_price, sell_price, fee_pct)
        rets.append(ret)
        trades.append({
            "signal_date": day,
            "sell_date": next_day,
            "etf": code,
            "name": name,
            "today_gain": round(gain, 2),
            "buy_price": round(buy_price, 4),
            "sell_price": round(sell_price, 4),
            "sell_reason": sell_reason,
            "return_pct": round(ret, 2),
        })

    if len(rets) < MIN_TRADES:
        return None

    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {
        "key": key,
        "label": label,
        "trade_count": len(rets),
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets),
        "trades": trades,
    }


def print_compare(results: list[dict], eval_dates: list[str]):
    print()
    print("=" * 100)
    print("  T+0 卖点对比（固定 14:45/14:50 买，仅改卖出时机）")
    print("=" * 100)
    print(f"  区间: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 信号日)")
    print(f"  买点: {SIGNAL_TIME}/{BUY_TIME} | 卖点窗口: {SELL_START}~{SELL_CUTOFF} | 超时定时卖")
    print(f"  海龟: 最高-{TURTLE_N}×ATR({ATR_PERIOD}) | 吊灯: {CHANDELIER_LOOKBACK}根高-{CHANDELIER_N}×ATR | OBV: MA{OBV_MA_PERIOD}")
    print()
    print(f"  {'卖点方案':<16} {'笔数':>4} {'累计':>10} {'胜率':>8} {'均笔':>8} {'回撤':>8} {'夏普':>8}")
    print("  " + "-" * 72)
    best = max(results, key=lambda r: r["final_equity_pct"])
    for r in results:
        st = r["stats"]
        mark = " ◀" if r is best else ""
        print(
            f"  {r['label']:<16} {r['trade_count']:>4} {r['final_equity_pct']:+9.2f}% "
            f"{st.get('win_rate', 0):7.1f}% {st.get('avg', 0):+7.2f}% "
            f"{st.get('max_drawdown', 0):+7.2f}% {st.get('sharpe', 0):7.2f}{mark}"
        )

    baseline = next((r for r in results if r["key"] == "baseline_trix"), results[0])
    print(f"\n  最优: {best['label']} ({best['final_equity_pct']:+.2f}%)")
    for r in results:
        if r["key"] != baseline["key"]:
            print(f"  {r['label']} vs {baseline['label']}: {r['final_equity_pct'] - baseline['final_equity_pct']:+.2f} pp")

    from collections import Counter
    print("\n  卖出原因分布:")
    for r in results:
        reasons = Counter(t["sell_reason"] for t in r["trades"])
        print(f"    {r['label']}: {dict(reasons)}")

    print(f"\n  {'信号日':>12} ", end="")
    for r in results:
        print(f"{r['label'][:14]:>16}", end="")
    print()
    print("  " + "-" * (12 + 16 * len(results)))
    by_day = {r["label"]: {t["signal_date"]: t for t in r["trades"]} for r in results}
    all_days = sorted({t["signal_date"] for r in results for t in r["trades"]})
    eqs = {r["label"]: 1.0 for r in results}
    for day in all_days:
        print(f"  {day:>12} ", end="")
        for r in results:
            t = by_day[r["label"]].get(day)
            if t:
                eqs[r["label"]] *= 1 + t["return_pct"] / 100
                print(f"{t['return_pct']:+15.2f}%", end="")
            else:
                print(f"{'—':>16}", end="")
        print()
    print("  " + "-" * (12 + 16 * len(results)))
    print(f"  {'累计':>12} ", end="")
    for r in results:
        print(f"{(eqs[r['label']]-1)*100:+15.2f}%", end="")
    print()
    print("=" * 100)


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 卖点三方案对比回测")
    parser.add_argument("--days", type=int, default=30, help="回测交易日数（默认30）")
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    args = parser.parse_args()

    print(f"=== T+0 卖点对比 | 最近 {args.days} 天 ===")
    print(f"买点: {SIGNAL_TIME}/{BUY_TIME}（与实盘一致）")
    print(f"对比: 海龟2N / 吊灯 / OBV / 实盘TRIX\n")

    etf_list = get_all_t0_etfs()
    etf_daily, etf_5min, all_dates, proxy_klines = load_market_data(
        etf_list, args.days + 5,
    )
    if len(etf_5min) < 5:
        print("ERROR: 5分K 数据不足")
        sys.exit(1)

    eval_dates = all_dates[-(args.days + 1):-1]
    if len(eval_dates) < MIN_TRADES:
        print("ERROR: 有效信号日不足")
        sys.exit(1)
    print(f"信号日: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日)\n")

    picks = precompute_picks(
        etf_list, etf_daily, etf_5min, eval_dates, [SIGNAL_TIME],
        proxy_klines, use_filter=True, skip_choppy=True,
    )

    results: list[dict] = []
    for v in VARIANTS:
        print(f">>> {v['label']}...")
        r = run_variant(
            v["key"], v["label"], etf_5min, eval_dates, all_dates, picks, args.fee,
        )
        if r:
            results.append(r)
        else:
            print(f"    WARNING: {v['label']} 有效交易不足")

    if len(results) < 2:
        print("ERROR: 对比方案不足")
        sys.exit(1)

    print_compare(results, eval_dates)

    out = Path.home() / ".tradingagents" / "rotation" / (
        f"backtest_t0_sell_exit_cmp_{datetime.now():%Y%m%d_%H%M}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "config": {
            "days": args.days,
            "signal": SIGNAL_TIME,
            "buy": BUY_TIME,
            "sell_window": f"{SELL_START}~{SELL_CUTOFF}",
            "atr_period": ATR_PERIOD,
            "turtle_n": TURTLE_N,
            "chandelier_n": CHANDELIER_N,
            "chandelier_lookback": CHANDELIER_LOOKBACK,
            "obv_ma": OBV_MA_PERIOD,
            "eval_dates": eval_dates,
            "strategy_version": "t0_1445_1450_5m_trix53_20260715",
        },
        "results": [{k: v for k, v in r.items() if k != "trades"} for r in results],
        "trades": {r["label"]: r["trades"] for r in results},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

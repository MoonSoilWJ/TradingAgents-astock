#!/usr/bin/env python3
"""T+0 卖点对比：1分 TRIX(5,3) vs 1分 TRIX(12,9) vs 5分 TRIX(5,3)。

固定买点：14:45 涨幅TOP1 / 14:50 买入（与实盘候选一致）
卖点：次日 TRIX 死叉 ≥09:40，截止 11:05

未来函数审计（2026-07-15）：
- 选股/买入：price_at_time 仅用 bar 开始时间 < 目标时刻的已完成 K 线
- 卖出：simulate_trix_cross_after 逐 bar 推进，死叉在 bar i 以 close[i] 成交
- 震荡过滤：regime_on_date 仅用 signal_date 及之前日 K（回测侧日 K 为收盘值，实盘 14:45 用实时价，略偏保守）
- 实盘对应：scripts/t0_monitor.py STRATEGY_VERSION t0_1445_1450_5m_trix53_20260715

用法:
    python scripts/backtest_t0_sell_trix_compare.py --ndays 9
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_1min import load_1min_data, print_trade_detail  # noqa: E402
from backtest_t0_today1 import FEE_PCT, load_market_data  # noqa: E402
from backtest_top1 import _calc_stats  # noqa: E402
from backtest_t0_etf import price_at_time  # noqa: E402
from search_t0_time_combo import precompute_picks, simulate_exit  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
SELL_MODE = "trix0940_cut"
SELL_CUTOFF = "11:05"
MIN_TRADES = 2

VARIANTS = [
    {"label": "1分 TRIX(5,3)", "bar": "1min", "trix_period": 5, "trix_signal": 3},
    {"label": "1分 TRIX(12,9)", "bar": "1min", "trix_period": 12, "trix_signal": 9},
    {"label": "5分 TRIX(5,3)", "bar": "5min", "trix_period": 5, "trix_signal": 3},
]


def run_variant(
    label: str,
    bar_key: str,
    etf_bars: dict,
    etf_bars_5m: dict,
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    fee_pct: float,
    trix_period: int,
    trix_signal: int,
) -> dict | None:
    rets: list[float] = []
    trades: list[dict] = []

    for day in eval_dates:
        picked = picks.get((SIGNAL_TIME, day))
        if not picked:
            continue
        code, gain, name = picked

        day_bars_1m = etf_bars.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars_1m, BUY_TIME)
        if not buy_price or buy_price <= 0:
            continue

        bars_src = etf_bars if bar_key == "1min" else etf_bars_5m
        day_bars = bars_src.get(code, {}).get(day, [])

        if day not in all_dates:
            continue
        idx = all_dates.index(day)
        if idx + 1 >= len(all_dates):
            continue
        next_day = all_dates[idx + 1]
        next_bars = bars_src.get(code, {}).get(next_day, [])
        if not next_bars:
            continue

        sell_price, sell_reason = simulate_exit(
            SELL_MODE, buy_price, day_bars, BUY_TIME, next_bars, SELL_CUTOFF,
            trix_period=trix_period, trix_signal_period=trix_signal,
        )
        if sell_price is None or sell_price <= 0:
            continue

        from backtest_t0_today1 import apply_net_return
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
        "label": label,
        "bar": bar_key,
        "trix_period": trix_period,
        "trix_signal": trix_signal,
        "signal": SIGNAL_TIME,
        "buy": BUY_TIME,
        "sell_cutoff": SELL_CUTOFF,
        "trade_count": len(rets),
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets),
        "trades": trades,
    }


def print_compare(results: list[dict], eval_dates: list[str], data_source: str):
    print()
    print("=" * 96)
    print("  T+0 卖点对比（固定 14:45/14:50 买，次日 TRIX≥09:40≤11:05 卖）")
    print("=" * 96)
    print(f"  区间: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 信号日) | 1分K源: {data_source}")
    print(f"  买点统一用 1分K @ {BUY_TIME}（5分卖点方案亦同）")
    print()
    print(f"  {'卖点方案':<18} {'笔数':>4} {'累计':>10} {'胜率':>8} {'均笔':>8} {'回撤':>8} {'夏普':>8}")
    print("  " + "-" * 68)
    best = max(results, key=lambda r: r["final_equity_pct"])
    for r in results:
        st = r["stats"]
        mark = " ◀" if r is best else ""
        print(
            f"  {r['label']:<18} {r['trade_count']:>4} {r['final_equity_pct']:+9.2f}% "
            f"{st.get('win_rate', 0):7.1f}% {st.get('avg', 0):+7.2f}% "
            f"{st.get('max_drawdown', 0):+7.2f}% {st.get('sharpe', 0):7.2f}{mark}"
        )

    print(f"\n  最优: {best['label']} ({best['final_equity_pct']:+.2f}%)")
    if len(results) >= 2:
        base = results[0]["final_equity_pct"]
        for r in results[1:]:
            print(f"  {r['label']} vs {results[0]['label']}: {r['final_equity_pct'] - base:+.2f} pp")

    print(f"\n  {'信号日':>12} ", end="")
    for r in results:
        print(f"{r['label'][:14]:>16}", end="")
    print()
    print("  " + "-" * (12 + 16 * len(results)))
    by_day = {r["label"]: {t["signal_date"]: t for t in r["trades"]} for r in results}
    all_days = sorted({d for r in results for t in r["trades"] for d in [t["signal_date"]]})
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
    print("=" * 96)


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 TRIX 卖点三方案对比")
    parser.add_argument("--ndays", type=int, default=9, help="1分K天数（东财 ndays）")
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--source", choices=["auto", "em", "sina"], default="auto")
    args = parser.parse_args()

    print(f"=== T+0 TRIX 卖点对比 | 最近 {args.ndays} 天 1分K ===")
    print(f"买点: {SIGNAL_TIME}/{BUY_TIME} | 卖点: TRIX≥09:40≤{SELL_CUTOFF}\n")

    etf_list = get_all_t0_etfs()
    etf_daily, etf_1min, all_dates, proxy_klines, data_source = load_1min_data(
        etf_list, args.ndays, source=args.source,
    )
    if len(etf_1min) < 5:
        print("ERROR: 1分K 数据不足")
        sys.exit(1)

    print("\n>>> 拉取 5 分 K（卖点方案3）...")
    etf_daily_5m, etf_5min, all_dates_5m, _ = load_market_data(etf_list, args.ndays + 5)
    all_dates = sorted(set(all_dates) | set(all_dates_5m))

    m1_dates = sorted({d for bars in etf_1min.values() for d in bars})
    eval_dates = m1_dates[:-1] if len(m1_dates) > 1 else m1_dates
    if len(eval_dates) < MIN_TRADES:
        print("ERROR: 有效信号日不足")
        sys.exit(1)
    print(f"\n信号日: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日)\n")

    picks = precompute_picks(
        etf_list, etf_daily, etf_1min, eval_dates, [SIGNAL_TIME],
        proxy_klines, use_filter=True, skip_choppy=True,
    )

    results: list[dict] = []
    for v in VARIANTS:
        print(f">>> {v['label']}...")
        r = run_variant(
            v["label"], v["bar"], etf_1min, etf_5min, eval_dates, all_dates,
            picks, args.fee, v["trix_period"], v["trix_signal"],
        )
        if r:
            results.append(r)
        else:
            print(f"    WARNING: {v['label']} 有效交易不足")

    if len(results) < 2:
        print("ERROR: 对比方案不足")
        sys.exit(1)

    print_compare(results, eval_dates, data_source)

    for r in results:
        print_trade_detail({**r, "sell_mode": SELL_MODE}, r["label"])

    out = Path.home() / ".tradingagents" / "rotation" / f"backtest_t0_sell_trix_cmp_{datetime.now():%Y%m%d_%H%M}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "config": {
            "ndays": args.ndays, "signal": SIGNAL_TIME, "buy": BUY_TIME,
            "sell": f"TRIX≥09:40≤{SELL_CUTOFF}", "eval_dates": eval_dates,
            "data_source": data_source,
        },
        "results": [{k: v for k, v in r.items() if k != "trades"} for r in results],
        "trades": {r["label"]: r["trades"] for r in results},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

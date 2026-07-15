#!/usr/bin/env python3
"""T+0 候选策略 + 次日 5 分 TRIX(5,3) 日内连续交易回测。

选股/买点（不变）:
- 14:45 信号选当日涨幅≥3% TOP1，501018 震荡期跳过
- 14:50 买入

次日卖出（本脚本）:
- 5 分钟 K TRIX(5,3) 金叉买、死叉卖，允许日内连续 T+0 往返
- 14:40 仍持仓则兜底卖出

用法:
    python scripts/backtest_t0_day2_trix.py --days 30
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
from backtest_t0_etf import apply_net_return, bar_time_min, price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    bar_clock,
    load_market_data,
    resolve_eval_dates,
)
from search_t0_time_combo import precompute_picks  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
FALLBACK_SELL = "14:40"
TRIX_PERIOD = 5
TRIX_SIGNAL = 3
MIN_TRADES = 3


def bars_until(bars: list[dict], cutoff: str) -> list[dict]:
    cm = bar_time_min({"time": cutoff})
    return [b for b in bars if bar_time_min(b) <= cm]


def simulate_day2_trix_t0(
    day1_buy: float,
    warmup_bars: list[dict],
    day2_bars: list[dict],
    fee_pct: float = FEE_PCT,
) -> tuple[float, list[dict], str]:
    """次日 5 分 TRIX 连续交易。返回 (周期收益率%, 动作列表, 最终原因)。"""
    window = bars_until(day2_bars, FALLBACK_SELL)
    if not window:
        return 0.0, [], "no_day2_bars"

    all_bars = list(warmup_bars) + list(window)
    min_warmup = TRIX_PERIOD * 3 + 5
    if len(all_bars) < min_warmup:
        last = float(window[-1]["close"])
        ret = apply_net_return(day1_buy, last, fee_pct)
        return ret, [{"action": "fallback", "time": window[-1].get("time", ""), "price": last}], "insufficient_data"

    closes = [float(b["close"]) for b in all_bars]
    trix = calc_trix(closes, TRIX_PERIOD)
    sig = calc_trix_signal(trix, TRIX_SIGNAL)
    warm = len(warmup_bars)
    start = max(warm, min_warmup)

    holding = True
    entry = day1_buy
    equity = 1.0
    actions: list[dict] = []
    final_reason = "fallback_1440"

    for i in range(start, len(all_bars)):
        t = bar_clock(all_bars[i])[:5]
        price = closes[i]
        death = trix[i - 1] >= sig[i - 1] and trix[i] < sig[i]
        golden = trix[i - 1] <= sig[i - 1] and trix[i] > sig[i]

        if death and holding:
            r = apply_net_return(entry, price, fee_pct)
            equity *= 1 + r / 100
            actions.append({"action": "sell", "reason": "trix_death", "time": t, "price": price, "ret": r})
            holding = False
            final_reason = "trix_death"
            continue

        if golden and not holding:
            entry = price
            holding = True
            actions.append({"action": "buy", "reason": "trix_golden", "time": t, "price": price})
            continue

    if holding:
        fb_bar = window[-1]
        fb_price = float(fb_bar["close"])
        fb_time = bar_clock(fb_bar)[:5]
        r = apply_net_return(entry, fb_price, fee_pct)
        equity *= 1 + r / 100
        actions.append({"action": "sell", "reason": "fallback_1440", "time": fb_time, "price": fb_price, "ret": r})
        final_reason = "fallback_1440"

    return (equity - 1) * 100, actions, final_reason


def run_backtest(
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
) -> dict | None:
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

        ret, actions, final_reason = simulate_day2_trix_t0(buy_price, day_bars, next_bars, fee_pct)
        rets.append(ret)
        trades.append({
            "signal_date": day,
            "sell_date": next_day,
            "etf": code,
            "name": name,
            "today_gain": round(gain, 2),
            "buy_price": round(buy_price, 4),
            "return_pct": round(ret, 2),
            "sell_reason": final_reason,
            "day2_actions": len(actions),
            "actions": actions,
        })

    if len(rets) < MIN_TRADES:
        return None

    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {
        "trade_count": len(rets),
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets),
        "trades": trades,
    }


def print_report(result: dict, eval_dates: list[str]):
    print()
    print("=" * 90)
    print("  T+0 候选买点 + 次日 5分 TRIX(5,3) 日内连续交易")
    print("=" * 90)
    print(f"  选股: {SIGNAL_TIME} 涨幅≥3% TOP1 | 买入: {BUY_TIME} | 震荡跳过: 是")
    print(f"  次日: 5分K TRIX({TRIX_PERIOD},{TRIX_SIGNAL}) 金叉买/死叉卖 | {FALLBACK_SELL} 兜底卖")
    print(f"  区间: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 信号日) | 手续费万3双边")
    print()
    st = result["stats"]
    print(f"  笔数: {result['trade_count']} | 累计: {result['final_equity_pct']:+.2f}%")
    print(f"  胜率: {st.get('win_rate', 0):.1f}% | 均笔: {st.get('avg', 0):+.2f}% | "
          f"回撤: {st.get('max_drawdown', 0):+.2f}% | 夏普: {st.get('sharpe', 0):.2f}")

    from collections import Counter
    reasons = Counter(t["sell_reason"] for t in result["trades"])
    print(f"  最终卖出: {dict(reasons)}")

    print(f"\n  {'信号日':>12} {'次日':>12} {'ETF':>8} {'涨%':>6} {'买价':>8} {'收益':>8} {'动作':>4} {'原因':>14}")
    print("  " + "-" * 82)
    eq = 1.0
    for t in result["trades"]:
        eq *= 1 + t["return_pct"] / 100
        print(
            f"  {t['signal_date']:>12} {t['sell_date']:>12} {t['etf']:>8} {t['today_gain']:+5.1f}% "
            f"{t['buy_price']:8.4f} {t['return_pct']:+7.2f}% {t['day2_actions']:>4} {t['sell_reason']:>14} | "
            f"累计 {(eq-1)*100:+7.2f}%"
        )
    print("=" * 90)


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 次日 5分 TRIX 日内连续交易回测")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--no-skip-choppy", dest="skip_choppy", action="store_false", default=True)
    args = parser.parse_args()

    print("=== T+0 次日 5分 TRIX 连续交易回测 ===")
    etf_list = get_all_t0_etfs()
    etf_daily, etf_5min, all_dates, proxy_klines = load_market_data(etf_list, args.days)
    eval_dates = resolve_eval_dates(all_dates, args.days)
    if len(eval_dates) < 3:
        print("ERROR: 有效交易日不足")
        sys.exit(1)

    picks = precompute_picks(
        etf_list, etf_daily, etf_5min, eval_dates, [SIGNAL_TIME],
        proxy_klines, use_filter=True, skip_choppy=args.skip_choppy,
    )

    result = run_backtest(eval_dates, all_dates, picks, etf_5min, args.fee)
    if not result:
        print("ERROR: 有效交易不足")
        sys.exit(1)

    print_report(result, eval_dates)

    out = Path.home() / ".tradingagents" / "rotation" / f"backtest_t0_day2_trix_{datetime.now():%Y%m%d_%H%M}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "config": {
            "days": args.days,
            "signal": SIGNAL_TIME,
            "buy": BUY_TIME,
            "trix": [TRIX_PERIOD, TRIX_SIGNAL],
            "fallback": FALLBACK_SELL,
            "eval_dates": eval_dates,
        },
        "result": {k: v for k, v in result.items() if k != "trades"},
        "trades": result["trades"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

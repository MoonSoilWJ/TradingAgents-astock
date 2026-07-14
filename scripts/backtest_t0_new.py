#!/usr/bin/env python3
"""T+0 ETF 新策略回测 — 14:45 信号 / 14:50 买入 / 次日 5分K TRIX 09:40~11:05 / 11:05 定时卖。

与旧策略(14:50/5分K全天)对比：
- 信号提前5分钟(14:45)
- 买入提前5分钟(14:50)
- TRIX 仅在 09:40~11:05 生效（不到全天）
- 11:05 前无死叉则定时卖出（不留过夜）

注：新策略原设计用1分K，但1分K历史数据源不可用，此处用5分K近似。
TRIX(5,3) 在5分K上比1分K粗略，但趋势方向一致。

用法:
    python scripts/backtest_t0_new.py --days 100
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import _calc_stats, fetch_sina_kline  # noqa: E402
from backtest_top1_minute import calc_trix, calc_trix_signal  # noqa: E402
from backtest_t0_etf import (  # noqa: E402
    FEE_PCT, apply_net_return, fetch_5min_kline, normalize_5min_bars, price_at_time,
)
from backtest_t0_today1 import (  # noqa: E402
    MIN_GAIN, TRIX_PERIOD, bars_for_trix, time_to_min, bar_clock,
    rank_by_today_gain, select_etf,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

SINA_INTERVAL = 0.25
SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
TRIX_START = "09:40"   # TRIX 死叉生效起始
TRIX_END = "11:05"     # TRIX 死叉生效结束 / 定时卖出


def next_trading_day(all_dates: list[str], day: str) -> str | None:
    if day not in all_dates:
        return None
    i = all_dates.index(day)
    return all_dates[i + 1] if i + 1 < len(all_dates) else None


def simulate_trix_timed(
    buy_cost: float,
    today_bars: list[dict],
    next_bars: list[dict],
    trix_period: int = TRIX_PERIOD,
    trix_start: str = TRIX_START,
    trix_end: str = TRIX_END,
) -> tuple[float, str, dict]:
    """次日 TRIX 死叉在 [trix_start, trix_end) 内卖，超时定时卖。"""
    all_bars = today_bars + next_bars
    min_warmup = trix_period * 3 + 5
    warmup_len = len(today_bars)

    start_min = time_to_min(trix_start)
    end_min = time_to_min(trix_end)

    # 找 09:40~11:05 范围内的 next_day bars
    search_bars = []
    for i, b in enumerate(all_bars):
        if i < warmup_len:
            continue
        bt = time_to_min(bar_clock(b))
        if start_min <= bt < end_min:
            search_bars.append((i, b))

    if len(all_bars) < min_warmup:
        # 数据不足，用11:05前最后一根bar卖
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

    # 无死叉 → 11:05 定时卖（用接近11:05的最后一根bar）
    if last_sell_bar_idx is not None:
        sell_price = closes[last_sell_bar_idx]
        return (sell_price - buy_cost) / buy_cost * 100, "timed_sell_11:05", {
            "sell_price": sell_price,
            "bar": all_bars[last_sell_bar_idx].get("day", ""),
        }

    # fallback
    last_close = float(next_bars[-1].get("close", buy_cost)) if next_bars else buy_cost
    return (last_close - buy_cost) / buy_cost * 100, "close", {"sell_price": last_close}


def run_backtest(
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    eval_dates: list[str],
    fee_pct: float,
) -> dict:
    trades: list[dict] = []

    for day in eval_dates:
        scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, SIGNAL_TIME)
        if len(scores) < 2:
            continue

        picked = select_etf(scores, use_filter=True)
        if picked is None:
            continue

        gain, top1 = picked
        code = top1["code"]
        sell_day = next_trading_day(all_dates, day)
        if not sell_day:
            continue

        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, BUY_TIME)
        if buy_price is None or buy_price <= 0:
            buy_price = price_at_time(day_bars, SIGNAL_TIME)
        if buy_price is None or buy_price <= 0:
            continue

        sell_bars = etf_5min.get(code, {}).get(sell_day, [])
        if not sell_bars:
            continue

        ret_pct, sell_reason, detail = simulate_trix_timed(
            buy_price,
            bars_for_trix(day_bars),
            bars_for_trix(sell_bars),
            trix_period=TRIX_PERIOD,
            trix_start=TRIX_START,
            trix_end=TRIX_END,
        )
        sell_price = detail.get("sell_price", buy_price)

        ret = apply_net_return(buy_price, sell_price, fee_pct)
        trades.append({
            "signal_date": day,
            "sell_date": sell_day,
            "sector": top1["name"],
            "etf": code,
            "today_gain": round(gain, 2),
            "buy_price": round(buy_price, 4),
            "sell_price": round(sell_price, 4),
            "sell_reason": sell_reason,
            "return_pct": ret,
        })

    rets = [t["return_pct"] for t in trades]
    stats = _calc_stats(rets) if rets else {}
    equity = 1.0
    for r in rets:
        equity *= 1 + r / 100
    return {
        "trades": trades,
        "trade_count": len(trades),
        "final_equity_pct": (equity - 1) * 100,
        "stats": stats,
    }


def load_market_data(etf_list: list[dict], lookback: int):
    from backtest_t0_today1 import load_market_data as _load
    return _load(etf_list, lookback, daily_only=False)


def main():
    parser = argparse.ArgumentParser(description="T+0 新策略回测(14:45/11:05定时卖)")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    args = parser.parse_args()

    etf_list = get_all_t0_etfs()
    print(f"=== T+0 新策略回测 ===")
    print(f"信号{SIGNAL_TIME} 买入{BUY_TIME} | TRIX({TRIX_PERIOD}) {TRIX_START}~{TRIX_END} 定时卖")
    print(f"ETF池 {len(etf_list)}只 | 回测{args.days}日\n")

    etf_daily, etf_5min, all_dates, _ = load_market_data(etf_list, args.days)
    eval_dates = all_dates[-args.days:]
    print(f"日K {len(etf_daily)} 5分K {len(etf_5min)} | {eval_dates[0]}~{eval_dates[-1]} ({len(eval_dates)}日)\n")

    result = run_backtest(etf_list, etf_daily, etf_5min, all_dates, eval_dates, args.fee)

    st = result["stats"]
    print(f"{'='*60}")
    print(f"  交易笔数: {result['trade_count']}")
    print(f"  累计收益: {result['final_equity_pct']:+.2f}%")
    if st:
        print(f"  胜率: {st.get('win_rate',0):.1f}%")
        print(f"  均笔: {st.get('avg',0):+.2f}%")
        print(f"  最大回撤: {st.get('max_drawdown',0):+.2f}%")
        print(f"  夏普: {st.get('sharpe',0):.2f}")

    trades = sorted(result["trades"], key=lambda t: t["sell_date"])
    if trades:
        from collections import Counter
        reasons = Counter(t["sell_reason"] for t in trades)
        print(f"  卖出原因: {dict(reasons)}")

        total = len(trades)
        seg = total // 3
        print(f"\n  分3段:")
        for i in range(3):
            s = trades[i*seg:(i+1)*seg if i<2 else total]
            eq = 1.0
            wins = 0
            for t in s:
                eq *= (1 + t["return_pct"]/100)
                if t["return_pct"] > 0: wins += 1
            print(f"    第{i+1}段 {s[0]['signal_date']}~{s[-1]['sell_date']} ({len(s)}笔): {(eq-1)*100:+7.2f}% 胜率{wins}/{len(s)}={wins/len(s)*100:.0f}%")

    print(f"{'='*60}")

    out = Path.home() / ".tradingagents" / "rotation" / f"backtest_t0_new_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

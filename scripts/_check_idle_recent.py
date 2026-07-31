#!/usr/bin/env python3
"""核对 idle 动量腿是否含未来函数: 打印最近 N 笔 idle 交易完整明细。

重点核对:
  1) 买入日 14:45 核心信号(SIGNAL_TIME)确实未触发 -> 才是合法 idle 日
  2) 选股用的涨幅 = 买入日 14:50 的价 vs 昨收(只用买入日当天及之前数据)
  3) 买入价 = 买入日 14:50 那根 5min bar 的 close
  4) 卖出价 = 次日 14:50 那根 5min bar 的 close
"""
from __future__ import annotations
import json
from pathlib import Path

from backtest_t0_etf import price_at_time  # noqa: E402
from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa: E402
from backtest_t0_today1 import FEE_PCT, next_trading_day  # noqa: E402
from quality_pool import build_picks_hybrid  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

from backtest_idle_momentum_merge import (  # noqa: E402
    MOM_BUY, LB, build_prev_close, pick_momentum,
)

CACHE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
N = 3  # 最近几笔


def bar_at(bars, t):
    return price_at_time(bars, t)


def main():
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    proxy = cache.get("proxy_klines", [])
    codes5 = set(etf_5min.keys())
    orig_pool = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    etf_list = get_all_t0_etfs()
    prev_close = build_prev_close(etf_list, etf_daily)

    START = "2022-06-15"
    eval_dates = [d for d in all_dates if d >= START]

    picks_a = build_picks_hybrid(
        eval_dates, orig_pool, etf_daily, etf_5min, all_dates, proxy,
        lookback=LB, warmup=LB,
    )
    idle_days = [d for d in eval_dates[LB:] if not picks_a.get((SIGNAL_TIME, d))]

    print(f"SIGNAL_TIME(核心判定时点) = {SIGNAL_TIME}")
    print(f"MOM_BUY(idle买入时点) = {MOM_BUY}")
    print(f"idle 日总数 = {len(idle_days)}\n")

    # 收集最近 N 笔实际成交的 idle 交易
    trades = []
    for day in idle_days:
        pk = pick_momentum(etf_list, etf_5min, prev_close, day, 1.0)
        if not pk:
            continue
        code, gain = pk
        day_bars = etf_5min.get(code, {}).get(day, [])
        bp = price_at_time(day_bars, MOM_BUY)
        if not bp or bp <= 0:
            continue
        nd = next_trading_day(all_dates, day)
        if not nd:
            continue
        next_bars = etf_5min.get(code, {}).get(nd, [])
        sp = price_at_time(next_bars, MOM_BUY)
        if not sp or sp <= 0:
            continue
        ret = (sp / bp - 1) * 100 - 2 * FEE_PCT
        # 核对: 买入日 14:45 核心信号是否真的没触发
        core_pick = picks_a.get((SIGNAL_TIME, day))
        trades.append({
            "buy_day": day, "etf": code, "gain": gain, "buy_px": bp,
            "sell_day": nd, "sell_px": sp, "ret": ret,
            "core_triggered": bool(core_pick),
        })

    print(f"实际成交 idle 交易(近{N}笔, thr=1.0):\n")
    for t in trades[-N:]:
        print(f"  买入日 {t['buy_day']}  标的 {t['etf']}")
        print(f"    ① 14:45核心信号是否触发 = {t['core_triggered']}  "
              f"-> {'⚠触发了却当idle买(未来函数!)' if t['core_triggered'] else '✓确为idle日(合法)'}")
        print(f"    ② 选股涨幅(买入日14:50价 vs 昨收) = {t['gain']:+.2f}%  (只用买入日当天数据)")
        print(f"    ③ 买入价(买入日{MOM_BUY} close) = {t['buy_px']:.4f}")
        print(f"    ④ 卖出日 {t['sell_day']} 卖出价(次日{MOM_BUY} close) = {t['sell_px']:.4f}")
        print(f"    ⑤ 收益率(扣双边费万3) = {t['ret']:+.2f}%\n")


if __name__ == "__main__":
    main()

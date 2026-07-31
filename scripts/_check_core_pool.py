#!/usr/bin/env python3
"""核心日策略上升空间量化:
   方案A(现状): build_picks_hybrid scheme-A (趋势日用优质池, 中性用原池)
   方案B(放宽池): 所有核心日扫全市场T0 ETF Top1(≥3%, 14:45)
   卖点另测: trix(纯TRIX) vs hybrid(TRIX+追踪回落)
"""
from __future__ import annotations
import json
from pathlib import Path

from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME, TRIX_PERIOD  # noqa: E402
from quality_pool import build_picks_hybrid, load_quality_pool  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT, MIN_GAIN, rank_by_today_gain,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402
from backtest_idle_momentum_merge import build_prev_close  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
START = "2022-06-15"
LB = 30


def main():
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    proxy = cache.get("proxy_klines", [])
    codes5 = set(etf_5min.keys())
    orig_pool = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    etf_list = get_all_t0_etfs()
    eval_dates = [d for d in all_dates if d >= START]

    # 方案A: 现状 hybrid-A
    picks_a = build_picks_hybrid(
        eval_dates, orig_pool, etf_daily, etf_5min, all_dates, proxy,
        lookback=LB, warmup=LB,
    )

    # 方案B: 全市场 Top1 (所有日子用 rank_by_today_gain 全 etf_list, 14:45, ≥3%)
    picks_b = {}
    for day in eval_dates:
        tg = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, SIGNAL_TIME)
        for gain, etf in tg:
            if gain >= MIN_GAIN:
                picks_b[(SIGNAL_TIME, day)] = (
                    etf["code"], gain, etf.get("name", etf["code"]))
                break

    n_a = sum(1 for d in eval_dates if picks_a.get((SIGNAL_TIME, d)))
    n_b = sum(1 for d in eval_dates if picks_b.get((SIGNAL_TIME, d)))
    print(f"核心日候选数: 现状A(优质池)={n_a}天 | 全市场B={n_b}天 | "
          f"(全市场多出 {n_b-n_a} 天)\n")

    print(f"{'方案':<22}{'卖点':<10}{'笔数':>5}{'累计':>11}{'胜率':>7}{'回撤':>9}")
    print("-" * 54)
    for label, picks in (("现状A(趋势用优质池)", picks_a),
                         ("B(全市场Top1)", picks_b)):
        for sell_mode in ("trix", "hybrid"):
            r = run_strategy(sell_mode, eval_dates, all_dates, picks,
                             etf_5min, FEE_PCT)
            if not r:
                continue
            trs = r["trades"]
            eq = 1.0
            peak = 1.0
            mdd = 0.0
            for t in trs:
                eq *= 1 + t["return_pct"] / 100
                peak = max(peak, eq)
                mdd = min(mdd, (eq - peak) / peak * 100)
            win = sum(1 for t in trs if t["return_pct"] > 0) / len(trs) * 100 if trs else 0
            print(f"{label:<20}{sell_mode:<10}{len(trs):>5}"
                  f"{r['final_equity_pct']:>+10.2f}%{win:>6.1f}%{mdd:>+8.1f}%")
        print()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""实盘等价窗口(2022-06-15~2026-07-28)回测报告 —— 与 t0_monitor.py 逻辑完全一致。

选股: build_picks_hybrid (hybrid-A: regime→优质/原池; 14:45信号)
卖点: run_strategy("trix") (14:50买, 次日09:40~11:05 TRIX(5,3)死叉)
按年拆分累计/胜率/笔数。结果落盘 live_validated_2022_2026.json。

注: 此窗口 5分钟+日K+501018 proxy 数据完整, 是与实盘逐日等价的窗口。
2015-2021 因老ETF未上市+proxy缺失, 仅作"数据受限旧池近似", 不计入本结论。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa: E402
from backtest_t0_today1 import FEE_PCT, resolve_eval_dates  # noqa: E402
from quality_pool import (  # noqa: E402
    HYBRID_SCHEME_B,
    build_picks_hybrid,
    build_pool_from_train,
    pick_orig_top1,
    pick_top1_from_pool,
    load_quality_pool,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
OUT = Path.home() / ".tradingagents/cache/t0_5min/live_validated_2022_2026.json"
START = "2022-06-15"


def per_year(r):
    if not r:
        return {}
    by = defaultdict(list)
    for t in r["trades"]:
        by[t["signal_date"][:4]].append(t["return_pct"])
    out = {}
    eq = 1.0
    for y in sorted(by):
        rs = by[y]
        yr_eq = 1.0
        for x in rs:
            yr_eq *= 1 + x / 100
        out[y] = {
            "trades": len(rs),
            "year_return_pct": round((yr_eq - 1) * 100, 2),
            "win_rate": round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1),
        }
    return out


def main():
    cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    proxy = cache["proxy_klines"]
    codes5 = set(etf_5min.keys())
    orig_pool = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    static_quality = load_quality_pool()

    eval_dates = [d for d in all_dates if d >= START]
    idx_map = {d: i for i, d in enumerate(eval_dates)}
    lb = 30
    print(f"=== 实盘等价窗口 {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}天) ===")
    print(f"原T0池 {len(orig_pool)}只 | 数据: 5min+日K+proxy 完整\n")

    def rolling_quality(day):
        i = idx_map[day]
        if i < lb:
            return static_quality
        return build_pool_from_train(eval_dates[i - lb:i], etf_daily, etf_5min, all_dates, proxy)

    results = {}

    # hybrid-A (实盘同款)
    picks_a = build_picks_hybrid(eval_dates, orig_pool, etf_daily, etf_5min, all_dates, proxy, lookback=lb, warmup=lb)
    ra = run_strategy("trix", eval_dates, all_dates, picks_a, etf_5min, FEE_PCT)
    results["hybrid_A"] = ra
    print(f"★ hybrid-A (实盘同款): {ra['final_equity_pct']:+.2f}%  {ra['trade_count']}笔 胜{ra['stats']['win_rate']:.0f}% 回撤{ra['stats']['max_drawdown']:+.1f}%")
    for y, s in per_year(ra).items():
        print(f"    {y}: {s['year_return_pct']:+.2f}%  {s['trades']}笔 胜{s['win_rate']:.0f}%")

    # hybrid-B
    picks_b = build_picks_hybrid(eval_dates, orig_pool, etf_daily, etf_5min, all_dates, proxy, lookback=lb, warmup=lb, scheme=HYBRID_SCHEME_B)
    rb = run_strategy("trix", eval_dates, all_dates, picks_b, etf_5min, FEE_PCT)
    results["hybrid_B"] = rb
    print(f"  hybrid-B (变体):     {rb['final_equity_pct']:+.2f}%  {rb['trade_count']}笔 胜{rb['stats']['win_rate']:.0f}% 回撤{rb['stats']['max_drawdown']:+.1f}%")

    # 原T0池
    picks_o = {(SIGNAL_TIME, d): pick_orig_top1(orig_pool, d, etf_daily, etf_5min, proxy) for d in eval_dates}
    ro = run_strategy("trix", eval_dates, all_dates, picks_o, etf_5min, FEE_PCT)
    results["orig_pool"] = ro
    print(f"  原T0池基准:          {ro['final_equity_pct']:+.2f}%  {ro['trade_count']}笔 胜{ro['stats']['win_rate']:.0f}% 回撤{ro['stats']['max_drawdown']:+.1f}%")

    # 优质滚动
    picks_q = {}
    for d in eval_dates:
        qp = rolling_quality(d)
        picks_q[(SIGNAL_TIME, d)] = pick_top1_from_pool(qp, d, etf_daily, etf_5min, proxy, use_regime_filter=True)
    rq = run_strategy("trix", eval_dates, all_dates, picks_q, etf_5min, FEE_PCT)
    results["quality_rolling"] = rq
    print(f"  优质滚动(lb=30):      {rq['final_equity_pct']:+.2f}%  {rq['trade_count']}笔 胜{rq['stats']['win_rate']:.0f}% 回撤{rq['stats']['max_drawdown']:+.1f}%")

    out = {
        "window": f"{eval_dates[0]}~{eval_dates[-1]}",
        "n_days": len(eval_dates),
        "note": "与 t0_monitor.py 逻辑一致(选股build_picks_hybrid+卖点run_strategy trix); 此窗口5min/日K/proxy数据完整, 为实盘等价窗口",
        "results": {k: {
            "final_equity_pct": v["final_equity_pct"],
            "trade_count": v["trade_count"],
            "win_rate": v["stats"]["win_rate"],
            "max_drawdown": v["stats"]["max_drawdown"],
            "per_year": per_year(v),
        } for k, v in results.items()},
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结论已落盘: {OUT}")


if __name__ == "__main__":
    main()

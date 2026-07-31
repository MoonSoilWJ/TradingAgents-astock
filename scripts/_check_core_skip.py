#!/usr/bin/env python3
"""用与合并回测完全相同的 build_picks_hybrid 调用, 查 513120/2026-06-29 为何未触发核心。"""
from __future__ import annotations
import json
from pathlib import Path

from quality_pool import (  # noqa: E402
    build_picks_hybrid, regime_uses_quality_pool,
    DEFAULT_HYBRID_SCHEME, _quality_pool_for_day, load_quality_pool,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402
from backtest_t0_hybrid_sell import SIGNAL_TIME  # noqa: E402
from backtest_t0_today1 import regime_on_date  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
DAY = "2026-06-29"
CODE = "513120"
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
    eval_dates = [d for d in all_dates if d >= START]

    picks_a = build_picks_hybrid(
        eval_dates, orig_pool, etf_daily, etf_5min, all_dates, proxy,
        lookback=LB, warmup=LB,
    )
    val = picks_a.get((SIGNAL_TIME, DAY))
    print(f"① build_picks_hybrid 在 {DAY} 的 (14:45) 判定 = {val}")
    print(f"   -> {'⚠竟选中了票(那idle不该买它)' if val else '✓为None/idle合法'}")

    reg = regime_on_date(proxy, DAY)
    print(f"\n② 当日 regime mode = {reg.get('mode')}")
    print(f"   regime_uses_quality_pool(reg, scheme={DEFAULT_HYBRID_SCHEME!r}) "
          f"= {regime_uses_quality_pool(reg, scheme=DEFAULT_HYBRID_SCHEME)}")
    print(f"   -> 核心在该日走 {'优质池' if regime_uses_quality_pool(reg) else '原T0池'} 分支")

    if regime_uses_quality_pool(reg):
        pool = _quality_pool_for_day(
            DAY, eval_dates, etf_daily, etf_5min, all_dates, proxy,
            lookback=LB, orig_pool=orig_pool, static_quality=load_quality_pool(),
        )
        codes = {e["code"] for e in pool}
        print(f"\n③ 该日滚动优质池含 {len(pool)} 只, 是否含 513120? -> {CODE in codes}")
        if CODE not in codes:
            print("   -> 513120 不在优质池, 故核心(优质池分支)选不到它, 只能 idle 买到")


if __name__ == "__main__":
    main()

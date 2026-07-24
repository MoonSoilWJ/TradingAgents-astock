#!/usr/bin/env python3
"""优质池 + 原 T+0 池 — 混合/并行策略回测。

★ 主推 hybrid: 趋势/震荡→滚动优质池(震荡仍交易)；中性→原T0池

其它对照:
  max_gain      — 两池各出 Top1，取当日涨幅更高者
  quality_first — 优质池优先，无信号则回退原 T+0 池
  orig_first    — 原 T+0 池优先，无信号则回退优质池
  union         — 两池并集按涨幅 Top1（优质池 regime 过滤）

用法:
    python scripts/backtest_parallel_pool.py
    python scripts/backtest_parallel_pool.py --mode hybrid
    python scripts/backtest_parallel_pool.py --recent 30 --lookback 30
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa: E402
from backtest_t0_today1 import FEE_PCT, resolve_eval_dates  # noqa: E402
from quality_pool import (  # noqa: E402
    HYBRID_SCHEME_B,
    build_picks_hybrid,
    build_pool_from_train,
    compound_returns,
    pick_orig_top1,
    pick_top1_from_pool,
    load_quality_pool,
)
from search_t0_time_combo import segment_stats  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402
from tradingagents.dataflows.instrument import settlement_rule  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/pool_20260721_days100_allmarket.json"


def merge_pool(a: list[dict], b: list[dict]) -> list[dict]:
    m: dict[str, dict] = {}
    for e in a + b:
        m[e["code"]] = e
    return list(m.values())


def build_parallel_picks(
    mode: str,
    eval_dates: list[str],
    orig_pool: list[dict],
    quality_pool_fn,
    etf_daily: dict,
    etf_5min: dict,
    proxy: list[dict],
) -> dict:
    picks: dict = {}
    for day in eval_dates:
        qpool = quality_pool_fn(day)
        qp = pick_top1_from_pool(
            qpool, day, etf_daily, etf_5min, proxy,
            use_regime_filter=True,
        ) if qpool else None
        op = pick_orig_top1(orig_pool, day, etf_daily, etf_5min, proxy)

        if mode == "max_gain":
            if qp and op:
                picks[(SIGNAL_TIME, day)] = qp if qp[1] >= op[1] else op
            else:
                picks[(SIGNAL_TIME, day)] = qp or op
        elif mode == "quality_first":
            picks[(SIGNAL_TIME, day)] = qp or op
        elif mode == "orig_first":
            picks[(SIGNAL_TIME, day)] = op or qp
        elif mode == "union":
            uni = merge_pool(qpool or [], orig_pool)
            picks[(SIGNAL_TIME, day)] = pick_top1_from_pool(
                uni, day, etf_daily, etf_5min, proxy, use_regime_filter=True,
            )
        else:
            raise ValueError(f"unknown mode: {mode}")
    return picks


def manual_returns(dates: list[str], picks: dict, all_dates: list[str], etf_5min: dict) -> list[float]:
    from backtest_t0_etf import price_at_time
    from backtest_t0_today1 import apply_net_return
    from search_t0_time_combo import simulate_exit

    rets: list[float] = []
    for day in dates:
        p = picks.get((SIGNAL_TIME, day))
        if not p:
            continue
        code = p[0]
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, "14:50")
        if not buy_price or day not in all_dates:
            continue
        idx = all_dates.index(day)
        if idx + 1 >= len(all_dates):
            continue
        nd = all_dates[idx + 1]
        nb = etf_5min.get(code, {}).get(nd, [])
        if not nb:
            continue
        sp, _, _ = simulate_exit("trix", buy_price, day_bars, "14:50", nb, "11:05")
        if sp:
            rets.append(apply_net_return(buy_price, sp, FEE_PCT))
    return rets


def report(label: str, picks: dict, dates: list[str], all_dates: list[str], etf_5min: dict):
    r = run_strategy("trix", dates, all_dates, picks, etf_5min, FEE_PCT)
    t1 = sum(
        1 for d in dates
        if picks.get((SIGNAL_TIME, d))
        and settlement_rule(picks[(SIGNAL_TIME, d)][0], picks[(SIGNAL_TIME, d)][2]) != "T0"
    )
    if not r:
        sig = sum(1 for d in dates if picks.get((SIGNAL_TIME, d)))
        extra = ""
        if sig:
            rets = manual_returns(dates, picks, all_dates, etf_5min)
            if rets:
                extra = f" 手工{compound_returns(rets):+.2f}%({len(rets)}笔)"
        print(f"  {label:32s} 成交不足 信号{sig}天{extra} T1={t1}")
        return None
    n = max(len(dates) // 3, 1) if len(dates) >= 9 else len(dates)
    segs = [dates[:n], dates[n: 2 * n], dates[2 * n:]] if len(dates) >= 9 else [dates]
    ss = [segment_stats(r["trades"], s)["total"] for s in segs if s]
    seg_str = " ".join(f"{x:+.1f}%" for x in ss[:3])
    print(
        f"  {label:32s} {r['final_equity_pct']:+7.2f}%  "
        f"{r['trade_count']}笔 胜{r['stats']['win_rate']:.0f}% "
        f"回撤{r['stats']['max_drawdown']:+.1f}% T1={t1}  [{seg_str}]"
    )
    return r


def main() -> None:
    parser = argparse.ArgumentParser(description="优质池+原T0池混合/并行回测")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--lookback", type=int, default=30, help="优质池滚动训练窗")
    parser.add_argument("--recent", type=int, default=0, help="仅测最近 N 信号日")
    parser.add_argument(
        "--mode", nargs="+",
        default=["hybrid", "max_gain", "orig_first"],
        help="hybrid | max_gain | quality_first | orig_first | union",
    )
    parser.add_argument("--cache", type=str, default=str(CACHE_FILE))
    args = parser.parse_args()

    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    eval_dates = resolve_eval_dates(cache["all_dates"], args.days, "", "")
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    all_dates = cache["all_dates"]
    proxy = cache["proxy_klines"]
    codes5 = set(etf_5min.keys())
    orig_pool = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    static_quality = load_quality_pool()

    test_dates = eval_dates[-args.recent:] if args.recent else eval_dates
    idx_map = {d: i for i, d in enumerate(eval_dates)}
    lb = args.lookback

    def rolling_quality(day: str) -> list[dict]:
        i = idx_map[day]
        if i < lb:
            return static_quality
        train = eval_dates[i - lb:i]
        return build_pool_from_train(train, etf_daily, etf_5min, all_dates, proxy)

    window = f"最近{args.recent}天" if args.recent else f"全{len(eval_dates)}天"
    print(f"=== 优质池 + 原T0池 混合回测 ({window}) ===")
    print(f"区间: {test_dates[0]} ~ {test_dates[-1]}")
    print(f"★ hybrid: 趋势/震荡→优质(lb={lb})；中性→原T0池 | 原池 {len(orig_pool)}只\n")

    warmup = max(0, len(eval_dates) - len(test_dates) - lb) if args.recent else lb
    picks_hybrid_full = build_picks_hybrid(
        eval_dates, orig_pool, etf_daily, etf_5min, all_dates, proxy,
        lookback=lb, warmup=warmup,
    )
    picks_hybrid = {k: v for k, v in picks_hybrid_full.items() if k[1] in test_dates}

    picks_orig = {
        (SIGNAL_TIME, d): pick_orig_top1(orig_pool, d, etf_daily, etf_5min, proxy)
        for d in test_dates
    }
    picks_q_only: dict = {}
    for d in test_dates:
        qpool = rolling_quality(d)
        picks_q_only[(SIGNAL_TIME, d)] = pick_top1_from_pool(
            qpool, d, etf_daily, etf_5min, proxy, use_regime_filter=True,
        )

    if "hybrid" in args.mode:
        ra = report("★ hybrid-A·趋势震荡→优质/中性→原池", picks_hybrid, test_dates, all_dates, etf_5min)
        picks_hybrid_b = build_picks_hybrid(
            eval_dates, orig_pool, etf_daily, etf_5min, all_dates, proxy,
            lookback=lb, warmup=warmup, scheme=HYBRID_SCHEME_B,
        )
        picks_hybrid_b = {k: v for k, v in picks_hybrid_b.items() if k[1] in test_dates}
        rb = report("  hybrid-B·趋势中性→原池/震荡→优质", picks_hybrid_b, test_dates, all_dates, etf_5min)
        if ra is not None and rb is not None:
            winner = "A" if ra["final_equity_pct"] >= rb["final_equity_pct"] else "B"
            print(
                f"  → 混合方案胜者: {winner} "
                f"({ra['final_equity_pct']:+.2f}% vs {rb['final_equity_pct']:+.2f}%)"
            )

    report("基准·原T0池", picks_orig, test_dates, all_dates, etf_5min)
    report(f"基准·优质滚动(lb={lb})", picks_q_only, test_dates, all_dates, etf_5min)

    parallel_modes = [
        ("max_gain", "并行·取涨幅更高"),
        ("quality_first", "并行·优质优先→原池"),
        ("orig_first", "并行·原池优先→优质"),
        ("union", "并行·并集+regime过滤"),
    ]
    for mode, label in parallel_modes:
        if mode not in args.mode:
            continue
        picks = build_parallel_picks(
            mode, test_dates, orig_pool, rolling_quality,
            etf_daily, etf_5min, proxy,
        )
        report(label, picks, test_dates, all_dates, etf_5min)


if __name__ == "__main__":
    main()

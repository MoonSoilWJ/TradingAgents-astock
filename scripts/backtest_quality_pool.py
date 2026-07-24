#!/usr/bin/env python3
"""优质池 walk-forward 回测 — 固定池 vs 滚动池 vs ★ hybrid 混合 vs 基准。

用法:
    python scripts/backtest_quality_pool.py
    python scripts/backtest_quality_pool.py --lookback 30 60
    python scripts/backtest_quality_pool.py --recent 30 --lookback 30
    python scripts/backtest_quality_pool.py --hybrid --lookback 30
    python scripts/backtest_quality_pool.py --emit
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa: E402
from backtest_t0_today1 import FEE_PCT, resolve_eval_dates, regime_on_date, select_etf, rank_by_today_gain  # noqa: E402
from quality_pool import (  # noqa: E402
    DEFAULT_POOL_PATH,
    build_picks_fixed,
    build_picks_hybrid,
    build_picks_rolling,
    build_picks_rules_on_universe,
    build_pool_from_train,
    compound_returns,
    get_scan_universe,
    pick_orig_top1,
    pick_top1_from_pool,
    refresh_and_save,
    load_quality_pool,
)
from search_t0_time_combo import segment_stats  # noqa: E402
from t0_etf_list import get_all_t0_etfs, get_all_market_etf_lof  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/pool_20260721_days100_allmarket.json"


def _baseline_picks(pool, eval_dates, etf_daily, etf_5min, proxy, oos_only_from: int):
    picks = {}
    for i, day in enumerate(eval_dates):
        if i < oos_only_from:
            picks[(SIGNAL_TIME, day)] = None
            continue
        reg = regime_on_date(proxy, day)
        if reg and reg.get("skip_choppy"):
            picks[(SIGNAL_TIME, day)] = None
            continue
        scores = rank_by_today_gain(pool, etf_daily, etf_5min, day, SIGNAL_TIME)
        if len(scores) < 2:
            picks[(SIGNAL_TIME, day)] = None
            continue
        sel = select_etf(scores, True)
        if not sel:
            picks[(SIGNAL_TIME, day)] = None
            continue
        g, e = sel
        picks[(SIGNAL_TIME, day)] = (e["code"], g, e["name"])
    return picks


def _report(
    label: str,
    picks: dict,
    oos_dates: list[str],
    all_dates: list[str],
    etf_5min: dict,
    *,
    min_trades_note: bool = True,
):
    r = run_strategy("trix", oos_dates, all_dates, picks, etf_5min, FEE_PCT)
    if not r:
        oos_picks = sum(1 for d in oos_dates if picks.get((SIGNAL_TIME, d)))
        extra = ""
        if min_trades_note and oos_picks:
            rets = _manual_returns(oos_dates, picks, all_dates, etf_5min)
            if rets:
                extra = f" → 手工复利{compound_returns(rets):+.2f}% ({len(rets)}笔)"
        print(f"  {label}: 有效交易不足 (OOS信号{oos_picks}天){extra}")
        return None
    n = len(oos_dates) // 3 if len(oos_dates) >= 9 else 0
    segs = [oos_dates[:n], oos_dates[n: 2 * n], oos_dates[2 * n:]] if n > 0 else [oos_dates]
    ss = [segment_stats(r["trades"], s)["total"] for s in segs]
    seg_str = " ".join(f"{x:+.1f}%" for x in ss[:3])
    print(
        f"  {label:28s} {r['final_equity_pct']:+7.2f}%  "
        f"({r['trade_count']}笔 胜{r['stats']['win_rate']:.0f}% 回撤{r['stats']['max_drawdown']:+.1f}%)  "
        f"分段[{seg_str}]"
    )
    return r


def _manual_returns(oos_dates, picks, all_dates, etf_5min):
    from backtest_t0_etf import price_at_time
    from backtest_t0_today1 import apply_net_return
    from search_t0_time_combo import simulate_exit

    rets = []
    for day in oos_dates:
        p = picks.get((SIGNAL_TIME, day))
        if not p:
            continue
        code = p[0]
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, "14:50")
        if not buy_price:
            continue
        if day not in all_dates:
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


def _rolling_picks_window(
    eval_dates: list[str],
    oos_dates: list[str],
    lookback: int,
    etf_daily,
    etf_5min,
    all_dates,
    proxy,
) -> tuple[dict, list[list[dict]]]:
    idx_map = {d: i for i, d in enumerate(eval_dates)}
    picks: dict = {}
    pool_history: list[list[dict]] = []
    for day in oos_dates:
        i = idx_map[day]
        if i < lookback:
            picks[(SIGNAL_TIME, day)] = None
            continue
        train = eval_dates[i - lookback:i]
        pool = build_pool_from_train(train, etf_daily, etf_5min, all_dates, proxy)
        pool_history.append(pool)
        picks[(SIGNAL_TIME, day)] = pick_top1_from_pool(
            pool, day, etf_daily, etf_5min, proxy,
        )
    return picks, pool_history


def run_recent_compare(
    eval_dates: list[str],
    recent: int,
    lookback: int,
    etf_daily,
    etf_5min,
    all_dates,
    proxy,
    codes5: set[str],
) -> None:
    if len(eval_dates) < recent + lookback:
        print(f"ERROR: 信号日不足 (需要>={recent + lookback})")
        return
    oos = eval_dates[-recent:]
    print(f"\n=== 最近 {recent} 天 × lookback={lookback} ===")
    print(f"OOS: {oos[0]} ~ {oos[-1]}\n")

    pool_orig = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    pool_am = [e for e in get_all_market_etf_lof() if e["code"] in codes5]

    scan_uni = get_scan_universe()
    oos_from = len(eval_dates) - recent
    p_orig = _baseline_picks(pool_orig, eval_dates, etf_daily, etf_5min, proxy, oos_from)
    p_am = _baseline_picks(pool_am, eval_dates, etf_daily, etf_5min, proxy, oos_from)
    p_rules = build_picks_rules_on_universe(
        scan_uni, eval_dates, etf_daily, etf_5min, proxy, oos_from=oos_from,
    )
    p_roll, hist = _rolling_picks_window(
        eval_dates, oos, lookback, etf_daily, etf_5min, all_dates, proxy,
    )
    warmup = len(eval_dates) - recent - lookback
    picks_hybrid = build_picks_hybrid(
        eval_dates, pool_orig, etf_daily, etf_5min, all_dates, proxy,
        lookback=lookback, warmup=max(0, warmup),
    )

    _report("★ hybrid", picks_hybrid, oos, all_dates, etf_5min)
    _report("原T0池", p_orig, oos, all_dates, etf_5min)
    _report(f"原T0+v2规则({len(scan_uni)}只)", p_rules, oos, all_dates, etf_5min)
    _report("全市场Top1", p_am, oos, all_dates, etf_5min)
    _report(f"优质滚动(lb={lookback})", p_roll, oos, all_dates, etf_5min)
    if hist:
        last = hist[-1]
        codes = ", ".join(e["code"] for e in last[:10])
        changes = sum(
            1 for i in range(1, len(hist))
            if {e["code"] for e in hist[i]} != {e["code"] for e in hist[i - 1]}
        )
        print(f"    末窗池 {len(last)} 只: {codes}")
        print(f"    {recent} 天内池变更 {changes}/{max(len(hist) - 1, 0)} 次")


def main() -> None:
    parser = argparse.ArgumentParser(description="优质池 walk-forward 对比")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--lookback", type=int, nargs="+", default=[10, 30, 60])
    parser.add_argument("--recent", type=int, default=0, help="仅对比最近 N 信号日 (配合 --lookback)")
    parser.add_argument("--hybrid", action="store_true", help="输出 ★ hybrid 混合策略对比")
    parser.add_argument("--emit", action="store_true", help="写入 strategies/data/quality_pool.json")
    parser.add_argument("--cache", type=str, default=str(CACHE_FILE))
    args = parser.parse_args()

    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    all_dates = cache["all_dates"]
    proxy = cache["proxy_klines"]
    eval_dates = resolve_eval_dates(all_dates, args.days, "", "")
    codes5 = set(etf_5min.keys())

    print("=== 优质池 Walk-Forward 对比 ===")
    print(f"信号日: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}日)")
    print(f"规则: 复盘品类/黑名单/500万流动性 + regime选股 | T+0/T+1 均含")

    pool_orig = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    pool_am = [e for e in get_all_market_etf_lof() if e["code"] in codes5]

    if args.recent:
        for lb in args.lookback:
            run_recent_compare(
                eval_dates, args.recent, lb,
                etf_daily, etf_5min, all_dates, proxy, codes5,
            )
        if args.emit and args.lookback:
            path = refresh_and_save(
                etf_daily, etf_5min, all_dates, proxy, eval_dates,
                lookback=args.lookback[0],
            )
            print(f"\n已写入: {path} ({len(load_quality_pool(path))} 只)")
        return

    print()

    for lb in args.lookback:
        if len(eval_dates) <= lb:
            print(f"lookback={lb} 跳过（信号日不足）")
            continue
        print(f"--- lookback={lb}  OOS={eval_dates[lb]}~{eval_dates[-1]} ({len(eval_dates)-lb}日) ---")

        pool_orig = [e for e in get_all_t0_etfs() if e["code"] in codes5]
        pool_am = [e for e in get_all_market_etf_lof() if e["code"] in codes5]

        oos = eval_dates[lb:]
        scan_uni = get_scan_universe()
        _report("原T0池", _baseline_picks(pool_orig, eval_dates, etf_daily, etf_5min, proxy, lb),
                oos, all_dates, etf_5min)
        _report(
            f"原T0+v2规则({len(scan_uni)}只)",
            build_picks_rules_on_universe(
                scan_uni, eval_dates, etf_daily, etf_5min, proxy, oos_from=lb,
            ),
            oos, all_dates, etf_5min,
        )
        _report("全市场Top1", _baseline_picks(pool_am, eval_dates, etf_daily, etf_5min, proxy, lb),
                oos, all_dates, etf_5min)

        if args.hybrid:
            picks_h = build_picks_hybrid(
                eval_dates, pool_orig, etf_daily, etf_5min, all_dates, proxy,
                lookback=lb, warmup=lb,
            )
            _report(f"★ hybrid lb={lb}", picks_h, oos, all_dates, etf_5min)

        picks_fixed, pool_fixed = build_picks_fixed(
            eval_dates, lb, etf_daily, etf_5min, all_dates, proxy,
        )
        _report(f"固定池(train 1~{lb})", picks_fixed, oos, all_dates, etf_5min)
        print(f"    固定池 {len(pool_fixed)} 只: {', '.join(e['code'] for e in pool_fixed[:8])}...")

        picks_roll, pool_hist = build_picks_rolling(
            eval_dates, lb, etf_daily, etf_5min, all_dates, proxy,
        )
        _report(f"滚动池(每日重算{lb}日)", picks_roll, oos, all_dates, etf_5min)
        if pool_hist:
            last = pool_hist[-1]
            changes = sum(
                1 for i in range(1, len(pool_hist))
                if {e["code"] for e in pool_hist[i]} != {e["code"] for e in pool_hist[i - 1]}
            )
            print(f"    末日池 {len(last)} 只: {', '.join(e['code'] for e in last[:8])}...")
            print(f"    池子变更 {changes}/{len(pool_hist)-1} 天 (OOS内)")
        print()

    if args.emit:
        lb = args.lookback[0]
        path = refresh_and_save(
            etf_daily, etf_5min, all_dates, proxy, eval_dates, lookback=lb,
        )
        print(f"已写入: {path} ({len(load_quality_pool(path))} 只)")


if __name__ == "__main__":
    main()

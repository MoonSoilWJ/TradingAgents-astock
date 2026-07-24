#!/usr/bin/env python3
"""每日滚动寻优买卖时间 vs 固定实盘基线 — 100日5分K walk-forward。

模拟：每个信号日前，用最近 lookback 天在窄网格上搜最优时间组合，
当日仅按该组合执行 1 笔；与固定 14:45/14:50/TRIX/11:05 对比。

用法:
    python scripts/backtest_t0_dynamic_time.py --days 100
    python scripts/backtest_t0_dynamic_time.py --lookbacks 7,14,30
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_today1 import FEE_PCT, resolve_eval_dates  # noqa: E402
from search_t0_time_combo import (  # noqa: E402
    BASELINE,
    precompute_picks,
    run_combo,
    segment_stats,
)
from t0_walk_forward import (  # noqa: E402
    NARROW_BUY,
    NARROW_CUTOFFS,
    NARROW_MODES,
    NARROW_SIGNALS,
    combo_key,
    iter_narrow_combos,
    is_baseline,
    search_on_window,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402
from quality_pool import build_picks_hybrid  # noqa: E402

DEFAULT_CACHE = Path.home() / ".tradingagents/cache/t0_5min/pool_20260721_days100_allmarket.json"


def build_picks(
    pool_mode: str,
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    eval_dates: list[str],
    all_dates: list[str],
    proxy_klines: list[dict],
    signal_times: list[str],
    hybrid_lb: int,
) -> dict:
    if pool_mode == "hybrid":
        warmup = hybrid_lb
        return build_picks_hybrid(
            eval_dates, etf_list, etf_daily, etf_5min, all_dates, proxy_klines,
            lookback=hybrid_lb, warmup=warmup, signal_times=signal_times,
        )
    return precompute_picks(
        etf_list, etf_daily, etf_5min, eval_dates,
        signal_times, proxy_klines, True, True,
    )


def min_trades_for(lookback: int) -> int:
    return max(3, min(8, lookback // 4))


def run_single_day(
    spec: dict,
    day: str,
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
) -> dict | None:
    import search_t0_time_combo as stc

    old = stc.MIN_TRADES
    stc.MIN_TRADES = 1
    try:
        return run_combo(
            spec["signal"], spec["buy"], spec["sell_mode"], spec.get("sell_cutoff"),
            [day], all_dates, picks, etf_5min, fee_pct,
        )
    finally:
        stc.MIN_TRADES = old


def spec_from_result(r: dict) -> dict:
    return {
        "signal": r["signal"],
        "buy": r["buy"],
        "sell_mode": r["sell_mode"],
        "sell_cutoff": r.get("sell_cutoff"),
        "label": r.get("label", ""),
    }


def compound_trades(trades: list[dict]) -> float:
    eq = 1.0
    for t in trades:
        eq *= 1 + t["return_pct"] / 100
    return (eq - 1) * 100


def baseline_train_result(
    train: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
    min_trades: int,
) -> dict | None:
    import search_t0_time_combo as stc

    old = stc.MIN_TRADES
    stc.MIN_TRADES = min_trades
    try:
        return run_combo(
            BASELINE["signal"], BASELINE["buy"], BASELINE["sell_mode"], BASELINE["sell_cutoff"],
            train, all_dates, picks, etf_5min, fee_pct,
        )
    finally:
        stc.MIN_TRADES = old


def run_daily_dynamic(
    lookback: int,
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
    *,
    require_stable: bool = False,
    min_edge_pp: float = 0.0,
) -> dict:
    combos = iter_narrow_combos()
    min_tr = min_trades_for(lookback)
    oos_dates = eval_dates[lookback:]
    trades: list[dict] = []
    daily_log: list[dict] = []
    prev_key = ""
    changes = 0
    baseline_picks = 0
    edge_fallbacks = 0

    for i in range(lookback, len(eval_dates)):
        day = eval_dates[i]
        train = eval_dates[i - lookback:i]
        ranked = search_on_window(
            combos, train, all_dates, picks, etf_5min, fee_pct,
            min_trades=min_tr,
            require_stable=require_stable,
            min_positive_segments=2 if require_stable else 0,
        )
        bl_train = baseline_train_result(train, all_dates, picks, etf_5min, fee_pct, min_tr)
        bl_train_pct = bl_train["final_equity_pct"] if bl_train else 0.0

        use_baseline = False
        if not ranked or is_baseline(ranked[0]):
            use_baseline = True
        elif min_edge_pp > 0 and ranked[0]["final_equity_pct"] - bl_train_pct < min_edge_pp:
            use_baseline = True
            edge_fallbacks += 1

        if use_baseline:
            spec = dict(BASELINE)
            spec["label"] = BASELINE["label"]
            best = ranked[0] if ranked else None
            baseline_picks += 1
        else:
            best = ranked[0]
            spec = spec_from_result(best)

        key = combo_key(spec)
        if prev_key and key != prev_key:
            changes += 1
        prev_key = key

        day_r = run_single_day(spec, day, all_dates, picks, etf_5min, fee_pct)
        if day_r and day_r.get("trades"):
            t = day_r["trades"][0]
            trades.append(t)
            daily_log.append({
                "day": day,
                "combo": key,
                "train_best_pct": ranked[0]["final_equity_pct"] if ranked else None,
                "train_baseline_pct": bl_train_pct,
                "ret": t["return_pct"],
            })
        else:
            daily_log.append({
                "day": day, "combo": key,
                "train_best_pct": ranked[0]["final_equity_pct"] if ranked else None,
                "train_baseline_pct": bl_train_pct,
                "ret": None,
            })

    total = compound_trades(trades)
    fixed_oos = run_combo(
        BASELINE["signal"], BASELINE["buy"], BASELINE["sell_mode"], BASELINE["sell_cutoff"],
        oos_dates, all_dates, picks, etf_5min, fee_pct,
    )
    fixed_oos_pct = fixed_oos["final_equity_pct"] if fixed_oos else 0.0
    fixed_oos_n = fixed_oos["trade_count"] if fixed_oos else 0

    return {
        "lookback": lookback,
        "oos_days": len(oos_dates),
        "trades": len(trades),
        "total_pct": total,
        "fixed_oos_pct": fixed_oos_pct,
        "fixed_oos_trades": fixed_oos_n,
        "edge_vs_fixed_oos": total - fixed_oos_pct,
        "combo_changes": changes,
        "baseline_combo_days": baseline_picks,
        "edge_fallbacks": edge_fallbacks,
        "daily_log": daily_log,
        "trade_list": trades,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="动态买卖时间 walk-forward vs 固定基线")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--lookbacks", type=str, default="7,14,30,60")
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--cache", type=str, default=str(DEFAULT_CACHE))
    parser.add_argument("--stable", action="store_true", help="训练窗要求3段中2段为正")
    parser.add_argument("--pool", choices=("orig", "hybrid"), default="orig", help="选股池")
    parser.add_argument("--hybrid-lb", type=int, default=30, help="hybrid 滚动优质池 lookback")
    parser.add_argument("--min-edge", type=float, default=0.0, help="训练窗领先基线不足则回退基线(pp)")
    args = parser.parse_args()

    cache_path = Path(args.cache)
    if not cache_path.exists():
        print(f"ERROR: 缓存不存在 {cache_path}")
        sys.exit(1)

    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    eval_dates = resolve_eval_dates(cache["all_dates"], args.days, "", "")
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in cache["etf_5min"]]
    picks = build_picks(
        args.pool, etf_list, cache["etf_daily"], cache["etf_5min"],
        eval_dates, cache["all_dates"], cache["proxy_klines"],
        NARROW_SIGNALS, args.hybrid_lb,
    )

    fixed_full = run_combo(
        BASELINE["signal"], BASELINE["buy"], BASELINE["sell_mode"], BASELINE["sell_cutoff"],
        eval_dates, cache["all_dates"], picks, cache["etf_5min"], args.fee,
    )

    pool_label = "★ hybrid-A" if args.pool == "hybrid" else "原T0池"
    edge_note = f" | min-edge={args.min_edge}pp" if args.min_edge > 0 else ""
    print("=== 动态买卖时间 walk-forward | 100日5分K ===")
    print(f"区间: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 信号日)")
    print(f"窄网格: {len(iter_narrow_combos())} 组 | {pool_label} | 震荡跳过+涨幅≥3%{edge_note}")
    print(f"固定基线(全样本): {BASELINE['label']}")
    if fixed_full:
        print(f"  → {fixed_full['final_equity_pct']:+.2f}% ({fixed_full['trade_count']}笔) "
              f"回撤{fixed_full['stats'].get('max_drawdown', 0):+.2f}%\n")
    else:
        print("  → (无效)\n")

    lookbacks = [int(x) for x in args.lookbacks.split(",") if x.strip()]
    results = []
    for lb in lookbacks:
        if lb >= len(eval_dates) - 5:
            continue
        print(f">>> lookback={lb} 天 (min_trades={min_trades_for(lb)})...")
        r = run_daily_dynamic(
            lb, eval_dates, cache["all_dates"], picks, cache["etf_5min"], args.fee,
            require_stable=args.stable,
            min_edge_pp=args.min_edge,
        )
        results.append(r)

    print("\n" + "=" * 105)
    hdr = f"{'方案':<28} {'OOS复利':>10} {'固定基线OOS':>12} {'超额':>8} {'笔数':>5} {'换参':>5}"
    if args.min_edge > 0:
        hdr += f" {'回退':>5}"
    print(hdr)
    print("=" * 105)
    for r in results:
        label = f"动态 lb={r['lookback']}"
        line = (
            f"{label:<28} {r['total_pct']:+9.2f}% {r['fixed_oos_pct']:+11.2f}% "
            f"{r['edge_vs_fixed_oos']:+7.2f}pp {r['trades']:>5} {r['combo_changes']:>5}"
        )
        if args.min_edge > 0:
            line += f" {r.get('edge_fallbacks', 0):>5}"
        print(line)
    if fixed_full:
        line = (
            f"{'固定基线(全100日)':<28} {fixed_full['final_equity_pct']:+9.2f}% {'—':>12} {'—':>8} "
            f"{fixed_full['trade_count']:>5} {'0':>5}"
        )
        if args.min_edge > 0:
            line += f" {'—':>5}"
        print(line)
    print("=" * 105)

    best = max(results, key=lambda x: x["total_pct"]) if results else None
    if best and len(eval_dates) >= 27:
        seg = len(best["trade_list"])
        if seg >= 9:
            oos_start = eval_dates[best["lookback"]]
            oos_dates = [d for d in eval_dates if d >= oos_start]
            seg_size = len(oos_dates) // 3
            segs = [
                oos_dates[:seg_size],
                oos_dates[seg_size: 2 * seg_size],
                oos_dates[2 * seg_size:],
            ]
            print(f"\n最优动态 lb={best['lookback']} 分3段 vs 固定基线:")
            bl_trades = fixed_full["trades"] if fixed_full else []
            for name, ds in [("前期", segs[0]), ("中期", segs[1]), ("后期", segs[2])]:
                d_pct = segment_stats(best["trade_list"], ds)["total"]
                f_pct = segment_stats(bl_trades, ds)["total"]
                print(f"  {name}: 动态 {d_pct:+.2f}% | 固定 {f_pct:+.2f}% | 差 {d_pct - f_pct:+.2f}pp")

    tag = f"{args.pool}_edge{int(args.min_edge)}" if args.min_edge > 0 else args.pool
    out = Path.home() / ".tradingagents/rotation" / f"backtest_t0_dynamic_time_{tag}_{eval_dates[-1]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "eval": [eval_dates[0], eval_dates[-1]],
        "pool": args.pool,
        "min_edge": args.min_edge,
        "hybrid_lb": args.hybrid_lb if args.pool == "hybrid" else None,
        "baseline_full": fixed_full["final_equity_pct"] if fixed_full else None,
        "baseline_trades": fixed_full["trade_count"] if fixed_full else None,
        "results": [{k: v for k, v in r.items() if k not in ("daily_log", "trade_list")} for r in results],
        "best_lookback": best["lookback"] if best else None,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存: {out}")


if __name__ == "__main__":
    main()

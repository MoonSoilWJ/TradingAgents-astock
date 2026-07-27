#!/usr/bin/env python3
"""闲置窗口 11:05~14:45 — 不同池子 × 选股逻辑 × 买卖组合搜索。

同一本金链不变:
  D-1 14:50 基线买 → D 11:05 前卖 → 【闲置 T+0】→ D 14:50 基线再买

用法:
    python scripts/backtest_t0_idle_pool_search.py --days 100
    python scripts/backtest_t0_idle_pool_search.py --days 100 --use-cache
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from itertools import product
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import _calc_stats  # noqa: E402
from backtest_t0_idle_window import (  # noqa: E402
    IDLE_END,
    IDLE_START,
    LIVE_BUY,
    LIVE_SIGNAL,
    combine_baseline_plus_idle,
    idle_eligible_days,
    run_baseline_overnight_legs,
    run_idle_combo,
    sell_time_mode,
    sell_trail_mode,
    sell_trix_mode,
    valid_combo,
)
from backtest_t0_pool_100d import load_or_fetch  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    MIN_GAIN,
    load_market_data,
    rank_by_today_gain,
    resolve_eval_dates,
    select_etf,
)
from backtest_t0_etf import price_at_time  # noqa: E402
from quality_pool import (  # noqa: E402
    build_picks_hybrid,
    load_quality_pool,
    pick_top1_from_pool,
)
from rotation_v6 import partial_score_at  # noqa: E402
from search_t0_time_combo import precompute_picks  # noqa: E402
from t0_etf_list import (  # noqa: E402
    COMMODITY_ETFS,
    GOLD_ETFS,
    filter_t0_settlement,
    get_all_market_etf_lof,
    get_all_t0_etfs,
    get_quality_etfs,
    get_t0_only_etfs,
)
from tradingagents.dataflows.instrument import settlement_rule  # noqa: E402

CACHE_DIR = Path.home() / ".tradingagents" / "cache" / "t0_5min"

# 从前轮网格筛出的代表性买卖组合
IDLE_COMBOS = [
    ("11:05", "14:05", "14:15", "trix", 3.0),
    ("11:05", "14:05", "14:30", "trix", 0.0),
    ("13:00", "14:35", "14:40", "time", 0.0),
    ("13:30", "13:35", "14:45", "time", 2.0),
    ("13:30", "13:35", "14:45", "trail", 0.0),
    ("14:15", "14:20", "14:45", "time", 3.0),
]

PICK_MODES = ("gain3", "gain0", "gain2", "low", "v6", "quality", "hybrid", "continue", "commodity")
POOL_MODES = ("t0", "t0_only", "quality", "all_t0", "commodity_gold")

MIN_TRADES = 8


def _pool_list(pool_key: str, etf_5min: dict) -> tuple[list[dict], str]:
    codes = set(etf_5min.keys())
    if pool_key == "t0":
        lst = [e for e in get_all_t0_etfs() if e["code"] in codes]
        return lst, f"原T+0池({len(lst)})"
    if pool_key == "t0_only":
        lst = [e for e in get_t0_only_etfs() if e["code"] in codes]
        return lst, f"T+0交割池({len(lst)})"
    if pool_key == "quality":
        lst = [e for e in get_quality_etfs() if e["code"] in codes]
        return lst, f"优质扫描池({len(lst)})"
    if pool_key == "commodity_gold":
        seen: set[str] = set()
        lst = []
        for pool in (COMMODITY_ETFS, GOLD_ETFS):
            for code, name, sina in pool:
                if code in seen or code not in codes:
                    continue
                seen.add(code)
                lst.append({
                    "code": code, "name": name, "sina_symbol": sina,
                    "etf_name": name, "type_name": "商品黄金",
                })
        return lst, f"商品+黄金({len(lst)})"
    # all_t0: 全市场但仅 T+0 交割
    raw = [e for e in get_all_market_etf_lof() if e["code"] in codes]
    lst = filter_t0_settlement(raw)
    return lst, f"全市场T+0({len(lst)})"


def _bar_vol(bars: list[dict], signal_time: str) -> float:
    tm = signal_time
    for b in reversed(bars):
        clock = b.get("day", "").split(" ")[1][:5] if " " in b.get("day", "") else b.get("time", "")[:5]
        if clock <= tm:
            try:
                return float(b.get("volume") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def rank_by_v6(
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    day: str,
    signal_time: str,
) -> list[tuple[float, dict]]:
    scores: list[tuple[float, dict]] = []
    for etf in etf_list:
        code = etf["code"]
        info = etf_daily.get(code)
        if not info:
            continue
        returns = info["returns"]
        idx_map = {r["date"]: i for i, r in enumerate(returns)}
        if day not in idx_map or idx_map[day] < 3:
            continue
        idx = idx_map[day]
        bars = etf_5min.get(code, {}).get(day, [])
        partial = price_at_time(bars, signal_time)
        if partial is None or partial <= 0:
            partial = returns[idx]["close"]
        pvol = _bar_vol(bars, signal_time) or returns[idx].get("volume", 0)
        score = partial_score_at(returns, idx, float(partial), float(pvol))
        if score > 0:
            scores.append((score, etf))
    scores.sort(key=lambda x: x[0], reverse=True)
    return scores


def build_idle_picks(
    pick_mode: str,
    pool: list[dict],
    idle_days: list[str],
    signal_times: list[str],
    etf_daily: dict,
    etf_5min: dict,
    proxy_klines: list[dict],
    eval_dates: list[str],
    all_dates: list[str],
    baseline_picks: dict,
) -> dict[tuple[str, str], tuple[str, float, str] | None]:
    picks: dict[tuple[str, str], tuple[str, float, str] | None] = {}
    static_q = load_quality_pool()
    orig = get_all_t0_etfs()

    if pick_mode == "hybrid":
        return build_picks_hybrid(
            idle_days, orig, etf_daily, etf_5min, all_dates, proxy_klines,
            signal_times=signal_times,
        )

    if pick_mode == "quality":
        for sig in signal_times:
            for day in idle_days:
                picks[(sig, day)] = pick_top1_from_pool(
                    pool, day, etf_daily, etf_5min, proxy_klines,
                    skip_choppy=True, use_regime_filter=True, t0_only=False,
                    signal_time=sig,
                )
        return picks

    for sig in signal_times:
        for day in idle_days:
            if pick_mode == "continue":
                prev_idx = all_dates.index(day) - 1 if day in all_dates else -1
                if prev_idx < 0:
                    picks[(sig, day)] = None
                    continue
                prev = all_dates[prev_idx]
                bp = baseline_picks.get((LIVE_SIGNAL, prev))
                if not bp:
                    picks[(sig, day)] = None
                    continue
                code, _, name = bp
                if code not in {e["code"] for e in pool}:
                    picks[(sig, day)] = None
                    continue
                scores = rank_by_today_gain(
                    [e for e in pool if e["code"] == code],
                    etf_daily, etf_5min, day, sig,
                )
                if not scores:
                    picks[(sig, day)] = None
                    continue
                gain, etf = scores[0]
                if gain < 1.0:
                    picks[(sig, day)] = None
                    continue
                picks[(sig, day)] = (code, gain, name)
                continue

            if pick_mode == "v6":
                scores = rank_by_v6(pool, etf_daily, etf_5min, day, sig)
            elif pick_mode == "low":
                scores = rank_by_today_gain(pool, etf_daily, etf_5min, day, sig)
                scores = list(reversed(scores))
            else:
                scores = rank_by_today_gain(pool, etf_daily, etf_5min, day, sig)

            if len(scores) < 2:
                picks[(sig, day)] = None
                continue

            min_g = {
                "gain3": MIN_GAIN,
                "gain2": 2.0,
                "gain0": 0.0,
                "commodity": 2.0,
                "v6": 0.0,
                "low": 0.0,
            }.get(pick_mode, MIN_GAIN)

            if pick_mode == "v6":
                for sc, etf in scores:
                    if sc <= 0:
                        continue
                    g_scores = rank_by_today_gain([etf], etf_daily, etf_5min, day, sig)
                    gain = g_scores[0][0] if g_scores else 0
                    if gain >= 2.0:
                        picks[(sig, day)] = (etf["code"], gain, etf.get("name", etf["code"]))
                        break
                else:
                    picks[(sig, day)] = None
                continue

            if min_g <= 0:
                gain, etf = scores[0]
                picks[(sig, day)] = (etf["code"], gain, etf.get("name", etf["code"]))
            else:
                picked = select_etf(scores, use_filter=True)
                picks[(sig, day)] = (
                    (picked[1]["code"], picked[0], picked[1].get("name", picked[1]["code"]))
                    if picked else None
                )
    return picks


def run_idle_with_picks(
    idle_days: list[str],
    picks: dict,
    etf_5min: dict,
    signal: str,
    buy: str,
    sell: str,
    sell_mode: str,
    fee_pct: float,
) -> dict | None:
    trades: list[dict] = []
    for day in idle_days:
        picked = picks.get((signal, day))
        if not picked:
            continue
        code, gain, name = picked
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, buy)
        if not buy_price or buy_price <= 0:
            continue
        if sell_mode == "trail":
            out = sell_trail_mode(day_bars, buy, sell, buy_price, fee_pct)
        elif sell_mode == "trix":
            out = sell_trix_mode(day_bars, buy, sell, buy_price, fee_pct)
        else:
            out = sell_time_mode(day_bars, buy, sell, buy_price, fee_pct)
        if not out:
            continue
        ret, reason = out
        trades.append({"day": day, "etf": code, "return_pct": ret, "sell_reason": reason})

    if len(trades) < MIN_TRADES:
        return None
    rets = [t["return_pct"] for t in trades]
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {
        "trade_count": len(trades),
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets),
        "trades": trades,
    }


def segment_idle(result: dict, idle_days: list[str]) -> tuple[float, float]:
    if not result or not idle_days:
        return 0.0, 0.0
    mid = len(idle_days) // 2
    halves = (set(idle_days[:mid]), set(idle_days[mid:]))
    out = []
    for half in halves:
        rets = [t["return_pct"] for t in result["trades"] if t["day"] in half]
        eq = 1.0
        for r in rets:
            eq *= 1 + r / 100
        out.append((eq - 1) * 100)
    return out[0], out[1]


def load_data(days: int, use_cache: bool) -> tuple[dict, dict, list[str], list[dict], str]:
    if use_cache:
        for f in sorted(CACHE_DIR.glob("pool_*_days*_allmarket.json"), reverse=True):
            if f.exists():
                cached = json.loads(f.read_text(encoding="utf-8"))
                n = len(cached.get("etf_5min", {}))
                if n >= 100:
                    print(f">>> 使用缓存: {f.name} ({n} 只)")
                    return (
                        cached["etf_daily"],
                        cached["etf_5min"],
                        cached["all_dates"],
                        cached.get("proxy_klines", []),
                        f"cache({n})",
                    )
    etf_list = get_all_market_etf_lof()
    return (*load_or_fetch(etf_list, days, use_cache=False, write_cache=False, fetch_limit=None)[:4], "live")


def main() -> None:
    parser = argparse.ArgumentParser(description="闲置窗口 池子×选股 搜索")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    print(f"=== 闲置窗口 池×选股 搜索 ({args.days} 日) ===")
    etf_daily, etf_5min, all_dates, proxy_klines, src = load_data(args.days, args.use_cache)
    eval_dates = resolve_eval_dates(all_dates, args.days, "", "")
    baseline_picks = precompute_picks(
        get_all_t0_etfs(), etf_daily, etf_5min, eval_dates, [LIVE_SIGNAL],
        proxy_klines, use_filter=True, skip_choppy=True,
    )
    idle_days = idle_eligible_days(eval_dates, all_dates, baseline_picks)
    baseline = run_baseline_overnight_legs(
        eval_dates, all_dates, baseline_picks, etf_5min, args.fee,
    )
    print(f">>> 数据源: {src} | 基线 {baseline['trade_count']} 笔 {baseline['final_equity_pct']:+.2f}%")
    print(f">>> 闲置 eligible: {len(idle_days)} 日")

    results: list[dict] = []
    signal_times = sorted({c[0] for c in IDLE_COMBOS})

    for pool_key in POOL_MODES:
        pool, pool_label = _pool_list(pool_key, etf_5min)
        if len(pool) < 5:
            continue
        for pick_mode in PICK_MODES:
            if pick_mode == "commodity" and pool_key != "commodity_gold":
                pool_use, _ = _pool_list("commodity_gold", etf_5min)
            else:
                pool_use = pool
            picks = build_idle_picks(
                pick_mode, pool_use, idle_days, signal_times,
                etf_daily, etf_5min, proxy_klines, eval_dates, all_dates, baseline_picks,
            )
            for signal, buy, sell, sell_mode, min_gain in IDLE_COMBOS:
                if not valid_combo(signal, buy, sell):
                    continue
                # min_gain in combo only applies to gain filter modes; skip mismatch for v6/quality
                r = run_idle_with_picks(
                    idle_days, picks, etf_5min, signal, buy, sell, sell_mode, args.fee,
                )
                if not r:
                    continue
                seg1, seg2 = segment_idle(r, idle_days)
                combined = combine_baseline_plus_idle(baseline, r, all_dates)
                st = r["stats"]
                results.append({
                    "pool": pool_key,
                    "pool_label": pool_label,
                    "pick": pick_mode,
                    "combo": f"{signal}/{buy}→{sell} [{sell_mode}]",
                    "idle_pct": r["final_equity_pct"],
                    "idle_count": r["trade_count"],
                    "idle_win": st.get("win_rate", 0),
                    "idle_avg": st.get("avg", 0),
                    "combined_pct": combined["final_equity_pct"],
                    "delta_pp": combined["final_equity_pct"] - baseline["final_equity_pct"],
                    "seg_front": seg1,
                    "seg_back": seg2,
                })

    results.sort(key=lambda x: (x["idle_pct"], x["delta_pp"]), reverse=True)
    positive = [r for r in results if r["idle_pct"] > 0 and r["seg_back"] > 0]
    stable = [r for r in results if r["idle_pct"] > 0 and r["seg_front"] > 0 and r["seg_back"] > 0]

    print()
    print("=" * 110)
    print(f"  TOP {args.top}（按闲置窗口 alone 累计排序）| 共 {len(results)} 有效组合")
    print(f"  闲置 alone>0: {sum(1 for r in results if r['idle_pct']>0)} | "
          f"前后段均>0: {len(stable)}")
    print("=" * 110)
    print(f"  {'#':>3} {'池':<12} {'选股':<10} {'买卖组合':<28} {'笔':>4} "
          f"{'闲置':>9} {'+基线':>9} {'前段':>8} {'后段':>8} {'均笔':>7}")
    print("  " + "-" * 106)
    for i, r in enumerate(results[: args.top], 1):
        print(
            f"  {i:>3} {r['pool']:<12} {r['pick']:<10} {r['combo']:<28} {r['idle_count']:>4} "
            f"{r['idle_pct']:+8.2f}% {r['combined_pct']:+8.2f}% "
            f"{r['seg_front']:+7.2f}% {r['seg_back']:+7.2f}% {r['idle_avg']:+6.2f}%"
        )

    print()
    if stable:
        print(f"  ★ 前后段均正收益（更稳）: {len(stable)} 个")
        b = stable[0]
        print(f"    最优: {b['pool_label']} + {b['pick']} + {b['combo']}")
        print(f"    闲置 {b['idle_pct']:+.2f}% | 叠加 {b['combined_pct']:+.2f}% | "
              f"前{b['seg_front']:+.2f}% 后{b['seg_back']:+.2f}%")
    elif positive:
        print(f"  ⚠ 有 {len(positive)} 个全段正收益，但样本外后段未必稳（见后段列）")
        b = positive[0]
        print(f"    最高: {b['pool_label']} + {b['pick']} + {b['combo']} → idle {b['idle_pct']:+.2f}%")
    else:
        print("  ✗ 未找到闲置窗口 alone 正收益组合")

    out = Path.home() / ".tradingagents" / "rotation" / (
        f"backtest_t0_idle_pool_{datetime.now():%Y%m%d_%H%M}.json"
    )
    out.write_text(json.dumps({
        "config": {"days": args.days, "data_source": src, "eligible": len(idle_days)},
        "baseline": {k: v for k, v in baseline.items() if k != "trades"},
        "top20": results[:20],
        "stable_positive": stable[:10],
        "counts": {
            "total": len(results),
            "idle_positive": sum(1 for r in results if r["idle_pct"] > 0),
            "both_segments_positive": len(stable),
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

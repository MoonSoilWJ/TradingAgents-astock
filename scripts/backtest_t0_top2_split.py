#!/usr/bin/env python3
"""T+0 TOP1 / TOP2 / TOP1+TOP2半仓 对比。

规则与实盘一致：14:45 信号 / 14:50 买入 / 次日 TRIX≥09:40≤11:05
- TOP1满仓：100% 涨幅≥3% 排名第1
- TOP2满仓：100% 涨幅≥3% 排名第2（需 TOP2 也过过滤，否则跳过）
- 各半仓：50% TOP1 + 50% TOP2

用法:
    python scripts/backtest_t0_top2_split.py --days 100
    python scripts/backtest_t0_top2_split.py --days 100 --segments
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
from backtest_t0_etf import price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    apply_net_return,
    load_market_data,
    passes_gain_filter,
    rank_by_today_gain,
    regime_on_date,
    resolve_eval_dates,
)
from search_t0_time_combo import (  # noqa: E402
    precompute_picks,
    run_combo,
    segment_stats,
    simulate_exit,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
SELL_MODE = "trix0940_cut"
SELL_CUTOFF = "11:05"
WEIGHT_EACH = 0.5


def select_top_n(
    scores: list[tuple[float, dict]],
    use_filter: bool,
    n: int = 2,
) -> list[tuple[float, dict]]:
    picks: list[tuple[float, dict]] = []
    for gain, etf in scores:
        if use_filter and not passes_gain_filter(gain):
            continue
        picks.append((gain, etf))
        if len(picks) >= n:
            break
    return picks


def precompute_rank_picks(
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    eval_dates: list[str],
    signal_time: str,
    proxy_klines: list[dict],
    use_filter: bool,
    skip_choppy: bool,
    rank: int,
) -> dict[tuple[str, str], tuple[str, float, str] | None]:
    """rank=1 → TOP1，rank=2 → TOP2（均需过涨幅过滤）。"""
    picks: dict[tuple[str, str], tuple[str, float, str] | None] = {}
    for day in eval_dates:
        key = (signal_time, day)
        if skip_choppy:
            regime = regime_on_date(proxy_klines, day)
            if regime and regime.get("skip_choppy"):
                picks[key] = None
                continue
        scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, signal_time)
        if len(scores) < rank:
            picks[key] = None
            continue
        selected = select_top_n(scores, use_filter, n=rank)
        if len(selected) < rank:
            picks[key] = None
            continue
        gain, info = selected[rank - 1]
        picks[key] = (info["code"], gain, info["name"])
    return picks


def precompute_top2_picks(
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    eval_dates: list[str],
    signal_time: str,
    proxy_klines: list[dict],
    use_filter: bool,
    skip_choppy: bool,
) -> dict[tuple[str, str], list[tuple[str, float, str]] | None]:
    picks: dict[tuple[str, str], list[tuple[str, float, str]] | None] = {}
    for day in eval_dates:
        key = (signal_time, day)
        if skip_choppy:
            regime = regime_on_date(proxy_klines, day)
            if regime and regime.get("skip_choppy"):
                picks[key] = None
                continue
        scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, signal_time)
        if len(scores) < 2:
            picks[key] = None
            continue
        selected = select_top_n(scores, use_filter, n=2)
        if not selected:
            picks[key] = None
            continue
        picks[key] = [(info["code"], gain, info["name"]) for gain, info in selected]
    return picks


def _leg_return(
    code: str,
    day: str,
    buy_time: str,
    all_dates: list[str],
    etf_5min: dict,
    fee_pct: float,
) -> tuple[float, float, float, str] | None:
    day_bars = etf_5min.get(code, {}).get(day, [])
    buy_price = price_at_time(day_bars, buy_time)
    if not buy_price or buy_price <= 0:
        return None
    if day not in all_dates:
        return None
    idx = all_dates.index(day)
    if idx + 1 >= len(all_dates):
        return None
    next_day = all_dates[idx + 1]
    next_bars = etf_5min.get(code, {}).get(next_day, [])
    if not next_bars:
        return None
    sell_price, sell_reason = simulate_exit(
        SELL_MODE, buy_price, day_bars, buy_time, next_bars, SELL_CUTOFF,
    )
    if sell_price is None or sell_price <= 0:
        return None
    ret = apply_net_return(buy_price, sell_price, fee_pct)
    return buy_price, sell_price, ret, sell_reason


def run_top2_split(
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
) -> dict:
    rets: list[float] = []
    trades: list[dict] = []

    for day in eval_dates:
        picked = picks.get((SIGNAL_TIME, day))
        if not picked:
            continue

        legs: list[dict] = []
        for rank, (code, gain, name) in enumerate(picked, 1):
            leg = _leg_return(code, day, BUY_TIME, all_dates, etf_5min, fee_pct)
            if leg is None:
                continue
            buy_p, sell_p, ret, reason = leg
            legs.append({
                "rank": rank,
                "etf": code,
                "name": name,
                "today_gain": round(gain, 2),
                "buy_price": round(buy_p, 4),
                "sell_price": round(sell_p, 4),
                "sell_reason": reason,
                "return_pct": round(ret, 2),
            })

        if not legs:
            continue

        # 固定两档各 50%：仅 TOP1 有效时另一半现金
        w = WEIGHT_EACH
        day_ret = sum(w * lg["return_pct"] for lg in legs)
        if len(legs) == 1:
            day_ret = w * legs[0]["return_pct"]

        rets.append(day_ret)
        trades.append({
            "signal_date": day,
            "return_pct": round(day_ret, 2),
            "legs": legs,
            "leg_count": len(legs),
        })

    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {
        "label": "TOP1+TOP2各50%",
        "trade_count": len(rets),
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets),
        "trades": trades,
    }


def print_compare(results: list[dict], eval_dates: list[str], split: dict | None = None):
    print()
    print("=" * 88)
    print("  T+0 TOP1 / TOP2 / 半仓对比")
    print("=" * 88)
    print(f"  区间: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 信号日)")
    print(f"  买点: {SIGNAL_TIME}/{BUY_TIME} | 卖点: TRIX≥09:40≤{SELL_CUTOFF}")
    print()
    print(f"  {'方案':<18} {'回合':>4} {'累计':>10} {'胜率':>8} {'均回合':>8} {'回撤':>8}")
    print("  " + "-" * 62)
    for r in results:
        st = r["stats"]
        print(
            f"  {r['label']:<18} {r['trade_count']:>4} {r['final_equity_pct']:+9.2f}% "
            f"{st.get('win_rate', 0):7.1f}% {st.get('avg', 0):+7.2f}% "
            f"{st.get('max_drawdown', 0):+7.2f}%"
        )
    if len(results) >= 2:
        base = results[0]["final_equity_pct"]
        for r in results[1:]:
            print(f"  {r['label']} vs {results[0]['label']}: {r['final_equity_pct'] - base:+.2f} pp")

    if split:
        top2_rets: list[float] = []
        for t in split["trades"]:
            for lg in t["legs"]:
                if lg["rank"] == 2:
                    top2_rets.append(lg["return_pct"])
        if top2_rets:
            st2 = _calc_stats(top2_rets)
            print(
                f"\n  半仓中 TOP2 腿({len(top2_rets)}笔): 均笔 {st2.get('avg', 0):+.2f}% | "
                f"胜率 {st2.get('win_rate', 0):.1f}%"
            )
        dual = sum(1 for t in split["trades"] if t["leg_count"] == 2)
        print(f"  双仓有效日: {dual}/{split['trade_count']}")
    print("=" * 88)


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 TOP1 vs TOP1+TOP2半仓")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--segments", action="store_true")
    args = parser.parse_args()

    print(f"=== T+0 TOP2 半仓对比 | {args.days} 日 ===")
    etf_list = get_all_t0_etfs()
    etf_daily, etf_5min, all_dates, proxy_klines = load_market_data(
        etf_list, max(args.days + 15, 115),
    )
    eval_dates = resolve_eval_dates(all_dates, args.days, "", "")
    if len(eval_dates) < 5:
        print("ERROR: 有效交易日不足")
        sys.exit(1)
    print(f"回测 {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日)\n")

    picks_top1 = precompute_picks(
        etf_list, etf_daily, etf_5min, eval_dates, [SIGNAL_TIME],
        proxy_klines, use_filter=True, skip_choppy=True,
    )
    picks_top2_only = precompute_rank_picks(
        etf_list, etf_daily, etf_5min, eval_dates, SIGNAL_TIME,
        proxy_klines, use_filter=True, skip_choppy=True, rank=2,
    )
    picks_top2 = precompute_top2_picks(
        etf_list, etf_daily, etf_5min, eval_dates, SIGNAL_TIME,
        proxy_klines, use_filter=True, skip_choppy=True,
    )

    top1 = run_combo(
        SIGNAL_TIME, BUY_TIME, SELL_MODE, SELL_CUTOFF,
        eval_dates, all_dates, picks_top1, etf_5min, args.fee,
    )
    top2 = run_combo(
        SIGNAL_TIME, BUY_TIME, SELL_MODE, SELL_CUTOFF,
        eval_dates, all_dates, picks_top2_only, etf_5min, args.fee,
    )
    if top1:
        top1["label"] = "TOP1满仓"
    if top2:
        top2["label"] = "TOP2满仓"
    split = run_top2_split(eval_dates, all_dates, picks_top2, etf_5min, args.fee)

    if not top1 or not top2:
        print("ERROR: 样本不足")
        sys.exit(1)

    results = [top1, top2, split]
    print_compare(results, eval_dates, split)

    if args.segments and len(eval_dates) >= 9:
        seg_size = len(eval_dates) // 3
        segs = [
            ("前期", eval_dates[:seg_size]),
            ("中期", eval_dates[seg_size: 2 * seg_size]),
            ("后期", eval_dates[2 * seg_size:]),
        ]
        print("\n  分 3 段累计（独立起算）:")
        print(f"  {'阶段':<6} {'TOP1':>10} {'TOP2':>10} {'半仓':>10}")
        for name, ds in segs:
            s1 = segment_stats(top1["trades"], ds)
            s2 = segment_stats(top2["trades"], ds)
            s3 = segment_stats(split["trades"], ds)
            print(f"  {name:<6} {s1['total']:+9.2f}% {s2['total']:+9.2f}% {s3['total']:+9.2f}%")

    out = Path.home() / ".tradingagents" / "rotation" / f"backtest_t0_top2_{datetime.now():%Y%m%d_%H%M}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "config": {
            "days": args.days,
            "signal": SIGNAL_TIME,
            "buy": BUY_TIME,
            "sell": f"TRIX≥09:40≤{SELL_CUTOFF}",
            "eval_dates": eval_dates,
        },
        "top1": {k: v for k, v in top1.items() if k != "trades"},
        "top2": {k: v for k, v in top2.items() if k != "trades"},
        "split": {k: v for k, v in split.items() if k != "trades"},
        "trades_top1": top1["trades"],
        "trades_top2": top2["trades"],
        "trades_split": split["trades"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

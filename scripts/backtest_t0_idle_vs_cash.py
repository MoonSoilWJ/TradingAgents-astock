#!/usr/bin/env python3
"""11:05 卖完后：段2 T+0 vs 持币 — 固定参数，不做网格。

核心问题
  前日 14:50 基线仓在 D 日 11:05 已平，11:05~14:50 这段资金：
    A) 持币（0%）等到 14:50 再买基线
    B) 做一笔段2 T+0（11:05 选 → 14:05 买 → 14:15 TRIX 卖）

  段2 参数固定为 Shadow 定版，不搜参、不对比「基线+段2 vs 仅基线」。

用法:
    python scripts/backtest_t0_idle_vs_cash.py --days 100 --use-cache
    python scripts/backtest_t0_idle_vs_cash.py --days 100 --use-cache --detail
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
from backtest_t0_idle_dual import run_baseline_sell_on_day, run_leg  # noqa: E402
from backtest_t0_idle_grid import MIN_GAIN_V6, build_v6_picks, load_data  # noqa: E402
from backtest_t0_idle_pool_search import _pool_list  # noqa: E402
from backtest_t0_idle_window import (  # noqa: E402
    LIVE_SIGNAL,
    idle_eligible_days,
    run_baseline_overnight_legs,
)
from backtest_t0_today1 import FEE_PCT, resolve_eval_dates  # noqa: E402
from search_t0_time_combo import precompute_picks  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

# Shadow 定版段2 — 固定，不搜
LEG2 = {
    "signal": "11:05",
    "buy": "14:05",
    "sell": "14:15",
    "buy_mode": "fixed",
    "sell_mode": "trix",
    "id": "leg2",
}


def compound(rets: list[float]) -> float:
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return (eq - 1) * 100


def half_split(idle_days: list[str]) -> tuple[list[str], list[str]]:
    mid = len(idle_days) // 2
    return idle_days[:mid], idle_days[mid:]


def eval_slot(
    idle_days: list[str],
    v6_picks: dict,
    etf_bars: dict,
    fee: float,
) -> dict:
    """闲置时段：段2 vs 持币(0%)。"""
    rows: list[dict] = []
    for day in idle_days:
        leg = run_leg(day, v6_picks, etf_bars, LEG2, fee)
        if leg:
            rows.append({
                "day": day,
                "mode": "leg2",
                "etf": leg["etf"],
                "name": leg.get("name", ""),
                "return_pct": leg["return_pct"],
                "cash_pct": 0.0,
                "delta_pp": leg["return_pct"],
            })
        else:
            rows.append({
                "day": day,
                "mode": "cash",
                "etf": "",
                "name": "",
                "return_pct": 0.0,
                "cash_pct": 0.0,
                "delta_pp": 0.0,
            })
    leg2_rets = [r["return_pct"] for r in rows if r["mode"] == "leg2"]
    traded = [r for r in rows if r["mode"] == "leg2"]
    deltas = [r["delta_pp"] for r in rows]
    return {
        "rows": rows,
        "eligible": len(idle_days),
        "leg2_trades": len(traded),
        "leg2_compound_pct": compound(leg2_rets) if leg2_rets else 0.0,
        "cash_compound_pct": 0.0,
        "delta_compound_pct": compound(deltas),
        "stats_traded_only": _calc_stats(leg2_rets) if leg2_rets else {},
        "stats_all_days": _calc_stats(deltas),
        "win_rate_traded": (
            sum(1 for r in leg2_rets if r > 0) / len(leg2_rets) * 100 if leg2_rets else 0.0
        ),
    }


def eval_full_chain(
    idle_days: list[str],
    all_dates: list[str],
    baseline_picks: dict,
    v6_picks: dict,
    etf_bars: dict,
    fee: float,
) -> dict:
    """整日链：基线卖 + (段2|持币) — 看段2 对全天复利的影响。"""
    cash_day_rets: list[float] = []
    leg2_day_rets: list[float] = []

    for day in idle_days:
        bt = run_baseline_sell_on_day(day, all_dates, baseline_picks, etf_bars, fee)
        if not bt:
            continue
        leg = run_leg(day, v6_picks, etf_bars, LEG2, fee)
        cash_day_rets.append(bt["return_pct"])
        day_rets = [bt["return_pct"]]
        if leg:
            day_rets.append(leg["return_pct"])
        leg2_day_rets.append(compound(day_rets))

    return {
        "days": len(cash_day_rets),
        "cash_chain_pct": compound(cash_day_rets),
        "leg2_chain_pct": compound(leg2_day_rets),
        "edge_pp": compound(leg2_day_rets) - compound(cash_day_rets),
    }


def verdict(slot: dict, h1: dict, h2: dict) -> tuple[str, str]:
    """基于闲置时段本身给结论，不看基线隔夜。"""
    total = slot["delta_compound_pct"]
    t1, t2 = h1["delta_compound_pct"], h2["delta_compound_pct"]
    traded = slot["leg2_trades"]
    wr = slot["win_rate_traded"]
    avg = (slot["stats_traded_only"] or {}).get("avg", 0.0)

    if traded < 8:
        return "样本不足", f"段2 仅成交 {traded} 笔，无法判断"

    if total <= 0:
        return "持币更好", f"段2 累计 {total:+.2f}% ≤ 0，不如持币"

    if t1 <= 0 and t2 <= 0:
        return "持币更好", f"前后半段2增量均为负 ({t1:+.2f}% / {t2:+.2f}%)"

    if t1 <= 0 or t2 <= 0:
        return "不确定", f"段2 全段 {total:+.2f}%，但前后半不一致 ({t1:+.2f}% / {t2:+.2f}%)"

    if wr < 50 or avg <= 0:
        return "不确定", f"累计 {total:+.2f}% 但胜率 {wr:.0f}% 或均笔 {avg:+.2f}% 偏弱"

    return "段2 优于持币", f"段2 累计 {total:+.2f}%，前后半 {t1:+.2f}% / {t2:+.2f}%，胜率 {wr:.0f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description="11:05 后 段2 T+0 vs 持币")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--detail", action="store_true")
    args = parser.parse_args()

    etf_daily, etf_bars, all_dates, proxy_klines, src = load_data(args.days, args.use_cache)
    eval_dates = resolve_eval_dates(all_dates, args.days, "", "")
    baseline_picks = precompute_picks(
        get_all_t0_etfs(), etf_daily, etf_bars, eval_dates, [LIVE_SIGNAL],
        proxy_klines, use_filter=True, skip_choppy=True,
    )
    idle_days = idle_eligible_days(eval_dates, all_dates, baseline_picks)
    pool, pool_label = _pool_list("all_t0", etf_bars)
    v6_picks = build_v6_picks(pool, idle_days, [LEG2["signal"]], etf_daily, etf_bars)

    slot = eval_slot(idle_days, v6_picks, etf_bars, args.fee)
    h1_days, h2_days = half_split(idle_days)
    h1 = eval_slot(h1_days, v6_picks, etf_bars, args.fee)
    h2 = eval_slot(h2_days, v6_picks, etf_bars, args.fee)
    chain = eval_full_chain(idle_days, all_dates, baseline_picks, v6_picks, etf_bars, args.fee)
    label, reason = verdict(slot, h1, h2)

    st = slot["stats_traded_only"] or {}
    print()
    print("=" * 88)
    print("  11:05 卖完后：段2 T+0 vs 持币（固定参数，不搜网格）")
    print("=" * 88)
    print(f"  数据: {src} | 池: {pool_label} | v6≥{MIN_GAIN_V6}%")
    print(f"  段2: {LEG2['signal']} 选 → {LEG2['buy']} 买 → {LEG2['sell']} TRIX 卖")
    print(f"  eligible 日: {len(idle_days)} ({idle_days[0]} ~ {idle_days[-1]})")
    print()
    print("  【闲置时段本身 — 这是本题答案】")
    print(f"  {'':28} {'笔数':>6} {'累计':>10} {'均笔':>8} {'胜率':>8}")
    print("  " + "-" * 68)
    print(f"  {'持币 (0%)':<28} {slot['eligible']:>6} {0.0:+9.2f}% {0.0:+7.2f}% {0.0:>7.1f}%")
    print(
        f"  {'段2 T+0 (有成交日)':<28} {slot['leg2_trades']:>6} "
        f"{slot['leg2_compound_pct']:+9.2f}% {st.get('avg', 0):+7.2f}% {slot['win_rate_traded']:>7.1f}%"
    )
    print(
        f"  {'段2 vs 持币 (eligible 全日)':<28} {slot['eligible']:>6} "
        f"{slot['delta_compound_pct']:+9.2f}% "
        f"{(slot['stats_all_days'] or {}).get('avg', 0):+7.2f}%"
    )
    print()
    print("  【前后半 — 粗样本外】")
    print(f"  前半 ({len(h1_days)} 日): 段2 {h1['leg2_compound_pct']:+.2f}% vs 持币 0%  → 增量 {h1['delta_compound_pct']:+.2f}%")
    print(f"  后半 ({len(h2_days)} 日): 段2 {h2['leg2_compound_pct']:+.2f}% vs 持币 0%  → 增量 {h2['delta_compound_pct']:+.2f}%")
    print()
    print("  【整日链参考 — 基线卖 + 闲置选择】")
    print(f"  基线卖 + 持币: {chain['cash_chain_pct']:+.2f}% ({chain['days']} 日)")
    print(f"  基线卖 + 段2:  {chain['leg2_chain_pct']:+.2f}%")
    print(f"  整日链差:      {chain['edge_pp']:+.2f} pp")
    print()
    icon = "✅" if label == "段2 优于持币" else ("⛔" if "持币" in label else "⚠️")
    print(f"  {icon} 结论: {label}")
    print(f"     {reason}")
    print("=" * 88)

    if args.detail:
        print()
        print(f"  {'日期':<12} {'模式':<6} {'代码':<8} {'段2收益%':>10} {'vs持币':>10}")
        print("  " + "-" * 52)
        for r in slot["rows"]:
            print(
                f"  {r['day']:<12} {r['mode']:<6} {r['etf']:<8} "
                f"{r['return_pct']:+9.2f}% {r['delta_pp']:+9.2f}%"
            )

    out = Path.home() / ".tradingagents" / "rotation" / (
        f"backtest_t0_idle_vs_cash_{datetime.now():%Y%m%d_%H%M}.json"
    )
    payload = {
        "question": "11:05卖完后 段2 T+0 vs 持币",
        "leg2_fixed": LEG2,
        "eligible": len(idle_days),
        "slot": {k: v for k, v in slot.items() if k != "rows"},
        "half1_delta_pct": h1["delta_compound_pct"],
        "half2_delta_pct": h2["delta_compound_pct"],
        "full_chain_edge_pp": chain["edge_pp"],
        "verdict": {"label": label, "reason": reason},
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果: {out}")


if __name__ == "__main__":
    main()

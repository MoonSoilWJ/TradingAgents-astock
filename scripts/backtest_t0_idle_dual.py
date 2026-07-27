#!/usr/bin/env python3
"""闲置双段 + 基线 — 同日三笔 T+0/隔夜（时间不重叠，同一本金串行复用）。

D 日时间线（eligible 日）:
  09:40~11:05  卖前日 14:50 基线仓（隔夜腿平仓）
  13:05~13:30  段1 午间 T+0（11:25 信号 / 13:05 买 / 13:30 卖）
  14:05~14:15  段2 午后 T+0（11:05 信号 / 14:05 买 / TRIX 卖）
  14:50        段3 基线买 → D+1 11:05 前卖

固定: 全市场 T+0 池 + v6 选股(涨幅≥2%)
段2 TRIX 卖出走 idle_window.sell_trix_mode（与 pool_search +30% 一致）

用法:
    python scripts/backtest_t0_idle_dual.py --days 100 --use-cache
    python scripts/backtest_t0_idle_dual.py --days 10 --bar 1min --detail --source sina
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
from backtest_t0_idle_grid import (  # noqa: E402
    MIN_GAIN_V6,
    build_v6_picks,
    load_data,
    resolve_buy,
    resolve_sell,
)
from backtest_t0_idle_pool_search import _pool_list, segment_idle  # noqa: E402
from backtest_t0_idle_window import (  # noqa: E402
    LIVE_BUY,
    LIVE_SIGNAL,
    LIVE_SELL_CUTOFF,
    idle_eligible_days,
    prev_trading_day,
    run_baseline_overnight_legs,
    sell_time_mode,
    sell_trix_mode,
)
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    TRIX_MIN_SELL,
    resolve_eval_dates,
)
from backtest_t0_etf import apply_net_return, price_at_time  # noqa: E402
from search_t0_time_combo import precompute_picks  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

# 段1: 网格最优
LEG1 = {"signal": "11:25", "buy": "13:05", "sell": "13:30", "buy_mode": "fixed", "sell_mode": "time"}
# 段2: 池搜索 TRIX 最优
LEG2 = {"signal": "11:05", "buy": "14:05", "sell": "14:15", "buy_mode": "fixed", "sell_mode": "trix"}
TRIX_P, TRIX_S, OBV_MA = 5, 3, 5

SEG_NAMES = {
    "baseline_sell": "段3卖(基线隔夜)",
    "leg1": "段1午间",
    "leg2": "段2午后",
}


def _sell_price_from_net(buy_price: float, net_pct: float, fee: float) -> float:
    f = fee / 100
    return buy_price * (1 + f) * (1 + net_pct / 100) / (1 - f)


def _day_bars(etf_bars: dict, code: str, day: str) -> list[dict]:
    return etf_bars.get(code, {}).get(day, [])


def _trix_bars(etf_bars: dict, code: str, day: str, bar_mode: str) -> list[dict]:
    raw = _day_bars(etf_bars, code, day)
    if bar_mode != "1min" or not raw:
        return raw
    from backtest_t0_hybrid_1min import resample_1min_to_5min  # noqa: PLC0415

    return resample_1min_to_5min(raw)


def run_baseline_sell_on_day(
    day: str,
    all_dates: list[str],
    picks: dict,
    etf_bars: dict,
    fee: float,
    *,
    bar_mode: str = "5min",
) -> dict | None:
    """D 日早盘：卖前一日 14:50 基线仓。"""
    prev = prev_trading_day(all_dates, day)
    if not prev:
        return None
    picked = picks.get((LIVE_SIGNAL, prev))
    if not picked:
        return None
    code, gain, name = picked
    buy_bars = _day_bars(etf_bars, code, prev)
    sell_bars = _day_bars(etf_bars, code, day)
    buy_price = price_at_time(buy_bars, LIVE_BUY)
    if not buy_price or buy_price <= 0:
        return None

    if bar_mode == "1min":
        from backtest_t0_hybrid_1min import simulate_trix_5m_signal_1m_exec  # noqa: PLC0415

        sell_price, reason, sell_exec, _ = simulate_trix_5m_signal_1m_exec(
            buy_price, buy_bars, sell_bars, TRIX_MIN_SELL, LIVE_SELL_CUTOFF,
        )
        if not sell_price or sell_price <= 0:
            return None
        ret = apply_net_return(buy_price, float(sell_price), fee)
        sell_time = sell_exec or LIVE_SELL_CUTOFF
    else:
        from backtest_t0_today1 import bars_for_trix, simulate_trix_cross_after  # noqa: PLC0415
        from search_t0_time_combo import bars_until  # noqa: PLC0415

        window = bars_until(sell_bars, LIVE_SELL_CUTOFF)
        if not window:
            return None
        _, reason, detail = simulate_trix_cross_after(
            buy_price,
            bars_for_trix(buy_bars),
            bars_for_trix(window),
            trix_period=TRIX_P,
            min_sell_time=TRIX_MIN_SELL,
        )
        sell_price = detail.get("sell_price") or float(window[-1]["close"])
        sell_time = detail.get("bar", LIVE_SELL_CUTOFF)
        ret = apply_net_return(buy_price, float(sell_price), fee)

    return {
        "day": day,
        "segment": "baseline_sell",
        "buy_date": prev,
        "etf": code,
        "name": name,
        "gain": gain,
        "buy_time": LIVE_BUY,
        "sell_time": str(sell_time),
        "buy_price": round(buy_price, 4),
        "sell_price": round(float(sell_price), 4),
        "return_pct": round(ret, 4),
        "sell_reason": reason,
        "leg": f"{LIVE_SIGNAL}/{LIVE_BUY}→{LIVE_SELL_CUTOFF}",
    }


def run_leg(
    day: str,
    picks: dict,
    etf_bars: dict,
    leg: dict,
    fee: float,
    *,
    bar_mode: str = "5min",
    segment: str = "",
) -> dict | None:
    sig, buy, sell = leg["signal"], leg["buy"], leg["sell"]
    picked = picks.get((sig, day))
    if not picked:
        return None
    code, gain, name = picked
    bars = _day_bars(etf_bars, code, day)
    if not bars:
        return None
    buy_price, buy_time, buy_reason = resolve_buy(
        bars, sig, buy, leg["buy_mode"], TRIX_P, TRIX_S, OBV_MA,
    )
    if not buy_price or buy_price <= 0:
        return None
    trix_bars = _trix_bars(etf_bars, code, day, bar_mode)
    if leg["sell_mode"] == "trix":
        out = sell_trix_mode(trix_bars, buy_time, sell, buy_price, fee)
    elif leg["sell_mode"] == "time":
        out = sell_time_mode(bars, buy_time, sell, buy_price, fee)
    else:
        out = resolve_sell(
            trix_bars, buy_time, sell, buy_price, leg["sell_mode"],
            TRIX_P, TRIX_S, OBV_MA, fee,
        )
    if not out:
        return None
    ret, sell_reason = out
    return {
        "day": day,
        "segment": segment or leg.get("id", ""),
        "etf": code,
        "name": name,
        "gain": gain,
        "buy_time": buy_time,
        "sell_time": sell,
        "buy_price": round(buy_price, 4),
        "sell_price": round(_sell_price_from_net(buy_price, ret, fee), 4),
        "return_pct": ret,
        "buy_reason": buy_reason,
        "sell_reason": sell_reason,
        "leg": f"{sig}/{buy}→{sell}",
    }


def compound(rets: list[float]) -> float:
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return (eq - 1) * 100


def print_daily_triple_detail(
    days: list[str],
    all_dates: list[str],
    baseline_picks: dict,
    v6_picks: dict,
    etf_bars: dict,
    leg2: dict,
    fee: float,
    *,
    bar_mode: str,
) -> None:
    """逐日列出三笔：基线卖 + 段1 + 段2。"""
    print()
    print("=" * 110)
    print(f"  最近 {len(days)} 个交易日 — 每日三笔明细（1分K精确价）" if bar_mode == "1min"
          else f"  最近 {len(days)} 个交易日 — 每日三笔明细")
    print("=" * 110)
    print(
        f"  {'日期':<12} {'段':<14} {'代码':<8} {'标的':<10} {'买@':>6} {'卖@':>6} "
        f"{'买价':>8} {'卖价':>8} {'收益%':>8} {'卖因':<14} {'v6涨%':>6}"
    )
    print("  " + "-" * 106)

    day_chain: list[float] = []
    for day in days:
        rows: list[dict] = []
        bt = run_baseline_sell_on_day(
            day, all_dates, baseline_picks, etf_bars, fee, bar_mode=bar_mode,
        )
        if bt:
            rows.append(bt)
        t1 = run_leg(
            day, v6_picks, etf_bars, {**LEG1, "id": "leg1"}, fee,
            bar_mode=bar_mode, segment="leg1",
        )
        if t1:
            rows.append(t1)
        t2 = run_leg(
            day, v6_picks, etf_bars, {**leg2, "id": "leg2"}, fee,
            bar_mode=bar_mode, segment="leg2",
        )
        if t2:
            rows.append(t2)

        if not rows:
            print(f"  {day:<12} {'— 无成交 —'}")
            continue

        day_rets = [r["return_pct"] for r in rows]
        day_chain.append(compound(day_rets))

        for i, r in enumerate(rows):
            seg = SEG_NAMES.get(r.get("segment", ""), r.get("segment", ""))
            buy_at = r.get("buy_time") or LIVE_BUY
            if r.get("segment") == "baseline_sell":
                buy_at = f"{r.get('buy_date','')[-5:]} {buy_at}"
            name = (r.get("name") or "")[:8]
            print(
                f"  {day if i == 0 else '':<12} {seg:<14} {r['etf']:<8} {name:<10} "
                f"{str(buy_at)[-5:]:>6} {str(r.get('sell_time', ''))[-5:]:>6} "
                f"{r.get('buy_price', 0):8.4f} {r.get('sell_price', 0):8.4f} "
                f"{r['return_pct']:+7.2f}% {str(r.get('sell_reason', ''))[:14]:<14} "
                f"{r.get('gain', 0):+5.1f}%"
            )
        print(
            f"  {'':12} {'当日复利':<14} {'':8} {'':10} {'':>6} {'':>6} "
            f"{'':>8} {'':>8} {compound(day_rets):+7.2f}%"
        )
        print("  " + "-" * 106)

    if day_chain:
        print(f"  {'区间':<12} {'10日链复利':<14} {'':8} {'':10} {'':>6} {'':>6} "
              f"{'':>8} {'':>8} {compound(day_chain):+7.2f}%")
    print("=" * 110)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--bar", choices=("5min", "1min"), default="5min")
    parser.add_argument("--source", default="sina", choices=("auto", "em", "sina"))
    parser.add_argument("--detail", action="store_true", help="输出最近 N 日每日三笔明细")
    parser.add_argument("--leg2-signal", default="11:05", help="段2选股时点(或 13:30 再选)")
    args = parser.parse_args()

    leg2 = {**LEG2, "signal": args.leg2_signal, "id": "leg2"}

    if args.bar == "1min":
        from backtest_t0_1min import load_1min_data  # noqa: PLC0415
        from t0_etf_list import get_all_market_etf_lof  # noqa: PLC0415

        fetch_ndays = max(args.days + 8, 15)
        etf_list = get_all_market_etf_lof()
        etf_daily, etf_bars, all_dates, proxy_klines, src = load_1min_data(
            etf_list,
            ndays=fetch_ndays,
            source=args.source,
            use_cache=args.use_cache,
            cache_suffix="_allmarket",
            min_write_count=50,
        )
        bar_tag = f"1min({args.source})"
    else:
        etf_daily, etf_bars, all_dates, proxy_klines, src = load_data(args.days, args.use_cache)
        bar_tag = src

    eval_dates = resolve_eval_dates(all_dates, args.days, "", "")
    pool, pool_label = _pool_list("all_t0", etf_bars)

    baseline_picks = precompute_picks(
        get_all_t0_etfs(), etf_daily, etf_bars, eval_dates, [LIVE_SIGNAL],
        proxy_klines, True, True,
    )
    idle_days = idle_eligible_days(eval_dates, all_dates, baseline_picks)
    baseline = run_baseline_overnight_legs(
        eval_dates, all_dates, baseline_picks, etf_bars, FEE_PCT,
    )

    signals = sorted(set([LEG1["signal"], leg2["signal"]]))
    v6_picks = build_v6_picks(pool, idle_days, signals, etf_daily, etf_bars)

    leg1_trades, leg2_trades, dual_trades = [], [], []
    chain_rets: list[float] = []

    for day in idle_days:
        t1 = run_leg(
            day, v6_picks, etf_bars, {**LEG1, "id": "leg1"}, FEE_PCT, bar_mode=args.bar,
        )
        t2 = run_leg(day, v6_picks, etf_bars, leg2, FEE_PCT, bar_mode=args.bar)
        if t1:
            leg1_trades.append(t1)
        if t2:
            leg2_trades.append(t2)
        day_rets = []
        if t1:
            day_rets.append(t1["return_pct"])
        if t2:
            day_rets.append(t2["return_pct"])
        if day_rets:
            dual_trades.append({"day": day, "legs": [t1, t2], "day_ret": compound(day_rets)})
            chain_rets.extend(day_rets)

    # 全链: 隔夜卖日上的段1+段2 + 所有基线隔夜腿
    full_eq = 1.0
    overnight_by_sell = {t["sell_date"]: t for t in baseline["trades"]}
    leg12_by_day: dict[str, list[float]] = {}
    for day in idle_days:
        rets = []
        t1 = run_leg(
            day, v6_picks, etf_bars, {**LEG1, "id": "leg1"}, FEE_PCT, bar_mode=args.bar,
        )
        t2 = run_leg(day, v6_picks, etf_bars, leg2, FEE_PCT, bar_mode=args.bar)
        if t1:
            rets.append(t1["return_pct"])
        if t2:
            rets.append(t2["return_pct"])
        if rets:
            leg12_by_day[day] = rets

    for bt in baseline["trades"]:
        full_eq *= 1 + bt["return_pct"] / 100
        for r in leg12_by_day.get(bt["sell_date"], []):
            full_eq *= 1 + r / 100

    leg1_pct = compound([t["return_pct"] for t in leg1_trades])
    leg2_pct = compound([t["return_pct"] for t in leg2_trades])
    dual_pct = compound(chain_rets)
    base_pct = baseline["final_equity_pct"]
    full_pct = (full_eq - 1) * 100

    # 分段稳健性
    mid = len(idle_days) // 2
    halves = (set(idle_days[:mid]), set(idle_days[mid:]))

    def seg_dual(days_set: set[str]) -> float:
        rs = []
        for day in idle_days:
            if day not in days_set:
                continue
            for leg in ({**LEG1, "id": "leg1"}, leg2):
                t = run_leg(day, v6_picks, etf_bars, leg, FEE_PCT, bar_mode=args.bar)
                if t:
                    rs.append(t["return_pct"])
        return compound(rs)

    print()
    print("=" * 88)
    print("  同日三笔 + 基线（11:05卖 → 13:05~13:30 → 14:05~14:15 → 14:50买）")
    print("=" * 88)
    print(f"  K线: {bar_tag} | 池: {pool_label} | v6≥{MIN_GAIN_V6}%")
    print(f"  段1: {LEG1['signal']} 选 → {LEG1['buy']} 买 → {LEG1['sell']} 定时卖")
    print(f"  段2: {leg2['signal']} 选 → {leg2['buy']} 买 → {leg2['sell']} TRIX卖")
    print(f"  段3: {LIVE_SIGNAL}/{LIVE_BUY} 基线隔夜")
    print(f"  eligible 日: {len(idle_days)}")
    print()
    print(f"  {'方案':<28} {'笔数':>6} {'累计':>10} {'胜率':>8} {'均笔':>8}")
    print("  " + "-" * 68)

    def row(label: str, trades: list[dict]) -> None:
        rets = [t["return_pct"] for t in trades]
        st = _calc_stats(rets) if rets else {}
        print(
            f"  {label:<28} {len(trades):>6} {compound(rets):+9.2f}% "
            f"{st.get('win_rate', 0):7.1f}% {st.get('avg', 0):+7.2f}%"
        )

    row("段1 alone", leg1_trades)
    row("段2 alone", leg2_trades)
    row("段1+段2（同日双段）", [{"return_pct": r} for r in chain_rets])
    print(f"  {'基线 overnight alone':<28} {baseline['trade_count']:>6} {base_pct:+9.2f}%")
    triple_days = sum(
        1 for d in idle_days
        if run_leg(day, v6_picks, etf_bars, {**LEG1, "id": "leg1"}, FEE_PCT, bar_mode=args.bar)
        and run_leg(day, v6_picks, etf_bars, leg2, FEE_PCT, bar_mode=args.bar)
    )
    print(f"  {'★ 全链(段1+段2+基线)':<28} {baseline['trade_count']+len(chain_rets):>6} {full_pct:+9.2f}%")
    print(f"  {'  日均成交(段1+段2+基线买)':<28} {triple_days}/{len(idle_days)} 天三笔全成")
    print(f"  {'  vs 基线 alone':<28} {'':>6} {full_pct - base_pct:+9.2f} pp")

    both_days = sum(
        1 for d in idle_days
        if run_leg(day, v6_picks, etf_bars, {**LEG1, "id": "leg1"}, FEE_PCT, bar_mode=args.bar)
        and run_leg(day, v6_picks, etf_bars, leg2, FEE_PCT, bar_mode=args.bar)
    )
    print()
    print(f"  同日两段均成交: {both_days}/{len(idle_days)} 天")
    print(f"  段1+段2 前段/后段: {seg_dual(halves[0]):+.2f}% / {seg_dual(halves[1]):+.2f}%")
    print(f"  简单加总(段1+段2): {leg1_pct:+.2f}% + {leg2_pct:+.2f}% ≈ {leg1_pct + leg2_pct:.2f}%*")
    print("  * 复利非加算；上表「段1+段2」为顺序复利结果")
    print("=" * 88)

    if args.detail and idle_days:
        detail_days = idle_days[-args.days:] if args.days < len(idle_days) else idle_days
        print_daily_triple_detail(
            detail_days,
            all_dates,
            baseline_picks,
            v6_picks,
            etf_bars,
            leg2,
            FEE_PCT,
            bar_mode=args.bar,
        )

    out = Path.home() / ".tradingagents" / "rotation" / (
        f"backtest_t0_idle_dual_{datetime.now():%Y%m%d_%H%M}.json"
    )
    out.write_text(json.dumps({
        "leg1_pct": leg1_pct,
        "leg2_pct": leg2_pct,
        "dual_pct": dual_pct,
        "baseline_pct": base_pct,
        "full_chain_pct": full_pct,
        "both_legs_days": both_days,
        "eligible": len(idle_days),
        "leg2_signal": args.leg2_signal,
    }, indent=2), encoding="utf-8")
    print(f"\n结果: {out}")


if __name__ == "__main__":
    main()

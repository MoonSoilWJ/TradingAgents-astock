#!/usr/bin/env python3
"""闲置窗口网格搜索 — 固定池(全市场T+0) + 固定选股(v6)，搜索 TRIX/OBV 买卖策略。

实盘节奏（不变）:
  D-1 14:50 基线买 → D 11:05 前卖 → 【11:05~14:45 闲置 T+0】→ D 14:50 基线再买

固定:
  池: 全市场 T+0 交割池
  选股: v6 得分 TOP1，且信号时刻当日涨幅 ≥2%

搜索:
  信号时点 × 买入方式(fixed/TRIX金叉/OBV金叉) × 卖出方式(time/TRIX死叉/OBV死叉/先触发)

用法:
    python scripts/backtest_t0_idle_grid.py --days 100 --use-cache
    python scripts/backtest_t0_idle_grid.py --days 100 --use-cache --top 25
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
from backtest_top1_minute import calc_obv, calc_trix, calc_trix_signal  # noqa: E402
from backtest_t0_etf import apply_net_return, bar_time_min, price_at_time  # noqa: E402
from backtest_t0_idle_pool_search import (  # noqa: E402
    CACHE_DIR,
    _bar_vol,
    _pool_list,
    load_data,
    rank_by_v6,
    segment_idle,
)
from backtest_t0_idle_window import (  # noqa: E402
    LIVE_SIGNAL,
    combine_baseline_plus_idle,
    idle_eligible_days,
    run_baseline_overnight_legs,
    valid_combo,
)
from backtest_t0_today1 import FEE_PCT, resolve_eval_dates, time_to_min  # noqa: E402
from rotation_v6 import partial_score_at  # noqa: E402
from search_t0_time_combo import bars_until, precompute_picks  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

IDLE_START = "11:05"
IDLE_END = "14:45"
MIN_GAIN_V6 = 2.0
MIN_TRADES = 8

GRID_SIGNALS = [
    "11:05", "11:15", "11:25",
    "13:00", "13:15", "13:30", "13:45",
    "14:00", "14:15", "14:30",
]
GRID_BUYS = [
    "11:10", "11:20", "11:30",
    "13:05", "13:20", "13:35", "13:50",
    "14:05", "14:20", "14:35",
]
GRID_SELLS = [
    "11:30", "13:30", "14:00", "14:15", "14:30", "14:40", "14:45",
]

TRIX_PARAMS = [(5, 3), (8, 5), (12, 9)]
OBV_MA_PERIODS = (5, 8, 10)

BUY_MODES = ("fixed", "trix_g", "obv_g")
SELL_MODES = ("time", "trix", "obv", "trix_obv", "obv_trix")


def _bar_clock(bar: dict) -> str:
    day = bar.get("day", "")
    if " " in day:
        return day.split(" ")[1][:5]
    return str(bar.get("time", "00:00:00"))[:5]


def _obv_ma(obv: list[float], ma_period: int) -> list[float]:
    out: list[float] = []
    for i in range(len(obv)):
        if i < ma_period - 1:
            out.append(obv[i])
        else:
            w = obv[i - ma_period + 1 : i + 1]
            out.append(sum(w) / len(w))
    return out


def build_v6_picks(
    pool: list[dict],
    idle_days: list[str],
    signal_times: list[str],
    etf_daily: dict,
    etf_5min: dict,
) -> dict[tuple[str, str], tuple[str, float, str] | None]:
    picks: dict[tuple[str, str], tuple[str, float, str] | None] = {}
    from backtest_t0_today1 import rank_by_today_gain  # noqa: PLC0415

    for sig in signal_times:
        for day in idle_days:
            scores = rank_by_v6(pool, etf_daily, etf_5min, day, sig)
            picked = None
            for _sc, etf in scores:
                g_rows = rank_by_today_gain([etf], etf_daily, etf_5min, day, sig)
                if not g_rows:
                    continue
                gain = g_rows[0][0]
                if gain >= MIN_GAIN_V6:
                    picked = (etf["code"], gain, etf.get("name", etf["code"]))
                    break
            picks[(sig, day)] = picked
    return picks


def _slice_bars(day_bars: list[dict], start: str, end: str) -> list[dict]:
    sm, em = time_to_min(start), time_to_min(end)
    return [b for b in day_bars if sm <= bar_time_min(b) <= em]


def resolve_buy(
    day_bars: list[dict],
    signal: str,
    buy_deadline: str,
    buy_mode: str,
    trix_p: int,
    trix_s: int,
    obv_ma: int,
) -> tuple[float | None, str, str]:
    """返回 (buy_price, buy_time, buy_reason)。"""
    if buy_mode == "fixed":
        px = price_at_time(day_bars, buy_deadline)
        if px and px > 0:
            return px, buy_deadline, "fixed"
        return None, "", "no_price"

    seg = _slice_bars(day_bars, signal, buy_deadline)
    if len(seg) < max(trix_p * 3, obv_ma + 2):
        px = price_at_time(day_bars, buy_deadline)
        if px and px > 0:
            return px, buy_deadline, "fallback_fixed"
        return None, "", "warmup_short"

    pre = [b for b in day_bars if bar_time_min(b) < time_to_min(signal)]
    all_b = pre + seg
    warm = len(pre)

    if buy_mode == "trix_g":
        closes = [float(b["close"]) for b in all_b]
        trix = calc_trix(closes, trix_p)
        sig = calc_trix_signal(trix, trix_s)
        start = max(warm, trix_p * 3)
        for i in range(start, len(all_b)):
            if bar_time_min(all_b[i]) > time_to_min(buy_deadline):
                break
            if trix[i - 1] <= sig[i - 1] and trix[i] > sig[i]:
                t = _bar_clock(all_b[i])
                return closes[i], t, "trix_golden"
    elif buy_mode == "obv_g":
        obv = calc_obv(all_b)
        ma = _obv_ma(obv, obv_ma)
        start = max(warm, obv_ma)
        for i in range(start, len(all_b)):
            if bar_time_min(all_b[i]) > time_to_min(buy_deadline):
                break
            if obv[i - 1] <= ma[i - 1] and obv[i] > ma[i]:
                return float(all_b[i]["close"]), _bar_clock(all_b[i]), "obv_golden"

    px = price_at_time(day_bars, buy_deadline)
    if px and px > 0:
        return px, buy_deadline, "fallback_fixed"
    return None, "", "no_trigger"


def resolve_sell(
    day_bars: list[dict],
    buy_time: str,
    sell_cutoff: str,
    buy_price: float,
    sell_mode: str,
    trix_p: int,
    trix_s: int,
    obv_ma: int,
    fee_pct: float,
) -> tuple[float, str] | None:
    seg = _slice_bars(day_bars, buy_time, sell_cutoff)
    if not seg:
        return None

    if sell_mode == "time":
        sp = float(seg[-1]["close"])
        return apply_net_return(buy_price, sp, fee_pct), "time_sell"

    pre = [b for b in day_bars if bar_time_min(b) < time_to_min(buy_time)]
    all_b = pre + seg
    warm = len(pre)
    closes = [float(b["close"]) for b in all_b]

    trix_events: list[tuple[int, float, str]] = []
    obv_events: list[tuple[int, float, str]] = []

    if sell_mode in ("trix", "trix_obv", "obv_trix"):
        if len(all_b) >= trix_p * 3 + 2:
            trix = calc_trix(closes, trix_p)
            sig = calc_trix_signal(trix, trix_s)
            start = max(warm, trix_p * 3)
            for i in range(start, len(all_b)):
                if bar_time_min(all_b[i]) > time_to_min(sell_cutoff):
                    break
                if trix[i - 1] >= sig[i - 1] and trix[i] < sig[i]:
                    trix_events.append((i, closes[i], "trix_death"))

    if sell_mode in ("obv", "trix_obv", "obv_trix"):
        if len(all_b) >= obv_ma + 2:
            obv = calc_obv(all_b)
            ma = _obv_ma(obv, obv_ma)
            start = max(warm, obv_ma)
            for i in range(start, len(all_b)):
                if bar_time_min(all_b[i]) > time_to_min(sell_cutoff):
                    break
                if obv[i - 1] >= ma[i - 1] and obv[i] < ma[i]:
                    obv_events.append((i, float(all_b[i]["close"]), "obv_death"))

    if sell_mode == "trix" and trix_events:
        _, sp, reason = trix_events[0]
        return apply_net_return(buy_price, sp, fee_pct), reason
    if sell_mode == "obv" and obv_events:
        _, sp, reason = obv_events[0]
        return apply_net_return(buy_price, sp, fee_pct), reason

    if sell_mode in ("trix_obv", "obv_trix") and (trix_events or obv_events):
        merged = trix_events + obv_events
        if sell_mode == "trix_obv":
            merged.sort(key=lambda x: (x[0], 0 if x[2] == "trix_death" else 1))
        else:
            merged.sort(key=lambda x: (x[0], 0 if x[2] == "obv_death" else 1))
        _, sp, reason = merged[0]
        return apply_net_return(buy_price, sp, fee_pct), reason

    sp = float(seg[-1]["close"])
    return apply_net_return(buy_price, sp, fee_pct), "time_fallback"


def run_combo(
    idle_days: list[str],
    picks: dict,
    etf_5min: dict,
    signal: str,
    buy: str,
    sell: str,
    buy_mode: str,
    sell_mode: str,
    trix_p: int,
    trix_s: int,
    obv_ma: int,
    fee_pct: float,
) -> dict | None:
    trades: list[dict] = []
    for day in idle_days:
        picked = picks.get((signal, day))
        if not picked:
            continue
        code, gain, name = picked
        day_bars = etf_5min.get(code, {}).get(day, [])
        if not day_bars:
            continue
        buy_price, buy_time, buy_reason = resolve_buy(
            day_bars, signal, buy, buy_mode, trix_p, trix_s, obv_ma,
        )
        if not buy_price or buy_price <= 0:
            continue
        if time_to_min(buy_time) >= time_to_min(sell):
            continue
        out = resolve_sell(
            day_bars, buy_time, sell, buy_price, sell_mode,
            trix_p, trix_s, obv_ma, fee_pct,
        )
        if not out:
            continue
        ret, sell_reason = out
        trades.append({
            "day": day,
            "etf": code,
            "return_pct": ret,
            "buy_reason": buy_reason,
            "sell_reason": sell_reason,
            "buy_time": buy_time,
        })

    if len(trades) < MIN_TRADES:
        return None
    rets = [t["return_pct"] for t in trades]
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    label = (
        f"{signal}/{buy_mode}→{sell_mode}≤{sell} "
        f"TRIX({trix_p},{trix_s}) OBVma{obv_ma}"
    )
    return {
        "signal": signal,
        "buy": buy,
        "sell": sell,
        "buy_mode": buy_mode,
        "sell_mode": sell_mode,
        "trix_p": trix_p,
        "trix_s": trix_s,
        "obv_ma": obv_ma,
        "label": label,
        "trade_count": len(trades),
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets),
        "trades": trades,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="闲置窗口 TRIX/OBV 网格（固定池+v6）")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    print("=== 闲置窗口 TRIX/OBV 网格 ===")
    print(f"    固定: 全市场T+0池 + v6选股(涨幅≥{MIN_GAIN_V6}%)")
    print(f"    窗口: {IDLE_START} ~ {IDLE_END}")

    etf_daily, etf_5min, all_dates, proxy_klines, src = load_data(args.days, args.use_cache)
    eval_dates = resolve_eval_dates(all_dates, args.days, "", "")
    pool, pool_label = _pool_list("all_t0", etf_5min)
    print(f">>> {pool_label} | 数据源: {src}")

    baseline_picks = precompute_picks(
        get_all_t0_etfs(), etf_daily, etf_5min, eval_dates, [LIVE_SIGNAL],
        proxy_klines, True, True,
    )
    idle_days = idle_eligible_days(eval_dates, all_dates, baseline_picks)
    baseline = run_baseline_overnight_legs(
        eval_dates, all_dates, baseline_picks, etf_5min, args.fee,
    )
    print(f">>> 基线隔夜: {baseline['trade_count']} 笔 {baseline['final_equity_pct']:+.2f}%")
    print(f">>> 闲置 eligible: {len(idle_days)} 日")

    v6_picks = build_v6_picks(pool, idle_days, GRID_SIGNALS, etf_daily, etf_5min)

    results: list[dict] = []
    grid = list(product(
        GRID_SIGNALS, GRID_BUYS, GRID_SELLS,
        BUY_MODES, SELL_MODES, TRIX_PARAMS, OBV_MA_PERIODS,
    ))
    print(f">>> 网格 {len(grid)} 组...")

    for i, (sig, buy, sell, bm, sm, (tp, ts), obv_ma) in enumerate(grid):
        if not valid_combo(sig, buy, sell):
            continue
        r = run_combo(
            idle_days, v6_picks, etf_5min, sig, buy, sell, bm, sm, tp, ts, obv_ma, args.fee,
        )
        if not r:
            continue
        seg_f, seg_b = segment_idle(r, idle_days)
        comb = combine_baseline_plus_idle(baseline, r, all_dates)
        st = r["stats"]
        results.append({
            **{k: v for k, v in r.items() if k != "trades"},
            "combined_pct": comb["final_equity_pct"],
            "delta_pp": comb["final_equity_pct"] - baseline["final_equity_pct"],
            "seg_front": seg_f,
            "seg_back": seg_b,
        })
        if (i + 1) % 2000 == 0:
            print(f"    进度 {i+1}/{len(grid)} 有效 {len(results)}")

    results.sort(key=lambda x: (x["final_equity_pct"], x["seg_back"]), reverse=True)
    stable = [r for r in results if r["final_equity_pct"] > 0 and r["seg_front"] > 0 and r["seg_back"] > 0]

    print()
    print("=" * 118)
    print(f"  TOP {args.top} | 有效 {len(results)} | 闲置>0且前后段均>0: {len(stable)}")
    print("=" * 118)
    print(
        f"  {'#':>3} {'信号':>6} {'买截止':>6} {'买法':>8} {'卖法':>8} {'卖≤':>6} "
        f"{'TRIX':>7} {'OBV':>4} {'笔':>4} {'闲置':>9} {'+基线':>9} {'前段':>7} {'后段':>7} {'均笔':>7}"
    )
    print("  " + "-" * 114)
    for i, r in enumerate(results[: args.top], 1):
        st = r["stats"]
        print(
            f"  {i:>3} {r['signal']:>6} {r['buy']:>6} {r['buy_mode']:>8} {r['sell_mode']:>8} "
            f"{r['sell']:>6} {r['trix_p']:>2},{r['trix_s']:<3} {r['obv_ma']:>4} {r['trade_count']:>4} "
            f"{r['final_equity_pct']:+8.2f}% {r['combined_pct']:+8.2f}% "
            f"{r['seg_front']:+6.2f}% {r['seg_back']:+6.2f}% {st.get('avg', 0):+6.2f}%"
        )

    if stable:
        b = stable[0]
        print()
        print(f"  ★ 前后段均正（更稳）: {b['label']}")
        print(
            f"    闲置 {b['final_equity_pct']:+.2f}% | +基线 {b['combined_pct']:+.2f}% | "
            f"前{b['seg_front']:+.2f}% 后{b['seg_back']:+.2f}%"
        )

    # 按卖法汇总最优
    print()
    print("  各卖法最优:")
    for sm in SELL_MODES:
        sub = [r for r in results if r["sell_mode"] == sm]
        if sub:
            b = sub[0]
            print(
                f"    {sm:10s} {b['final_equity_pct']:+7.2f}% | "
                f"{b['signal']}/{b['buy_mode']}/{b['buy']}→{b['sell']} "
                f"T{b['trix_p']},{b['trix_s']} O{b['obv_ma']}"
            )

    out = Path.home() / ".tradingagents" / "rotation" / (
        f"backtest_t0_idle_grid_{datetime.now():%Y%m%d_%H%M}.json"
    )
    out.write_text(json.dumps({
        "config": {
            "pool": "all_t0",
            "pick": "v6_gain2",
            "days": args.days,
            "eligible": len(idle_days),
            "grid_size": len(grid),
        },
        "baseline": {k: v for k, v in baseline.items() if k != "trades"},
        "top30": [{k: v for k, v in r.items()} for r in results[:30]],
        "stable_top10": [{k: v for k, v in r.items()} for r in stable[:10]],
        "counts": {"valid": len(results), "stable": len(stable)},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

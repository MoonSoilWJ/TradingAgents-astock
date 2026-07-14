#!/usr/bin/env python3
"""T+0 ETF 买卖时间窗口网格搜索 — 当日涨幅 TOP1 + 多种卖出策略。

用法:
    python scripts/search_t0_time.py --days 100
    python scripts/search_t0_time.py --days 100 --top 30
    python scripts/search_t0_time.py --days 100 --no-skip-choppy  # 不过滤震荡

搜索维度:
    信号时间 × 买入时间 × 卖出策略（定时/TRIX/追踪/当日T+0）
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
from backtest_top1_intraday import check_sell_trigger  # noqa: E402
from backtest_t0_etf import price_at_time, bar_time_min  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    TRIX_MIN_SELL,
    TRIX_PERIOD,
    apply_net_return,
    bars_for_trix,
    load_market_data,
    rank_by_today_gain,
    regime_on_date,
    select_etf,
    simulate_trix_cross_after,
    time_to_min,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

SIGNALS = [
    "09:40", "09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15",
    "13:00", "13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45", "14:50",
]
BUYS = [
    "09:45", "09:50", "10:05", "10:20", "10:35", "10:50", "11:05", "11:20",
    "13:05", "13:20", "13:35", "13:50", "14:05", "14:20", "14:35", "14:50", "14:55",
]
SELL_CUTS = [
    "09:35", "09:50", "10:05", "10:20", "10:35", "10:50", "11:05", "11:20",
    "13:00", "13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45",
]


def same_session(sig: str, buy: str) -> bool:
    sig_am = time_to_min(sig) < time_to_min("11:30")
    buy_am = time_to_min(buy) < time_to_min("11:30")
    return sig_am == buy_am


def bars_until(bars: list[dict], cutoff: str) -> list[dict]:
    cm = time_to_min(cutoff)
    return [b for b in bars if bar_time_min(b) <= cm]


def buy_idx(bars: list[dict], buy_time: str) -> int:
    bm = time_to_min(buy_time)
    idx = 0
    for i, b in enumerate(bars):
        if bar_time_min(b) <= bm:
            idx = i
    return idx


def simulate_exit(
    mode: str,
    buy: float,
    day_bars: list[dict],
    buy_time: str,
    next_bars: list[dict],
    sell_cut: str | None = None,
) -> float | None:
    if mode == "same_close":
        return float(day_bars[-1]["close"]) if day_bars else None
    if mode == "same_trail":
        idx = buy_idx(day_bars, buy_time)
        sp, _, _ = check_sell_trigger(day_bars, buy, idx, -1.5, 2.0, 0.5)
        return sp
    if mode == "time":
        bs = bars_until(next_bars, sell_cut or "15:00")
        return float(bs[-1]["close"]) if bs else None
    if mode in ("trix_full", "trix_0940"):
        min_sell = "09:30" if mode == "trix_full" else TRIX_MIN_SELL
        _, _, d = simulate_trix_cross_after(
            buy, bars_for_trix(day_bars), bars_for_trix(next_bars),
            trix_period=TRIX_PERIOD, min_sell_time=min_sell,
        )
        return d.get("sell_price")
    if mode in ("trix_cut", "trix0940_cut"):
        bs = bars_until(next_bars, sell_cut or "15:00")
        if not bs:
            return None
        min_sell = "09:30" if mode == "trix_cut" else TRIX_MIN_SELL
        _, _, d = simulate_trix_cross_after(
            buy, bars_for_trix(day_bars), bars_for_trix(bs),
            trix_period=TRIX_PERIOD, min_sell_time=min_sell,
        )
        return d.get("sell_price")
    if mode == "trail":
        bs = bars_until(next_bars, sell_cut) if sell_cut else next_bars
        if not bs:
            return None
        sp, _, _ = check_sell_trigger(bs, buy, 0, -1.5, 2.0, 0.5)
        return sp
    if mode == "fixed":
        bs = bars_until(next_bars, sell_cut) if sell_cut else next_bars
        if not bs:
            return None
        tp, sl = buy * 1.03, buy * 0.985
        for b in bs:
            if float(b["low"]) <= sl:
                return sl
            if float(b["high"]) >= tp:
                return tp
        return float(bs[-1]["close"])
    return None


def precompute_picks(
    etf_list, etf_daily, etf_5min, eval_dates, proxy_klines, skip_choppy: bool,
) -> dict[tuple[str, str], tuple | None]:
    picks: dict[tuple[str, str], tuple | None] = {}
    for sig in SIGNALS:
        for day in eval_dates:
            if skip_choppy:
                regime = regime_on_date(proxy_klines, day)
                if regime and regime.get("skip_choppy"):
                    picks[(sig, day)] = None
                    continue
            scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, sig)
            if len(scores) < 2:
                picks[(sig, day)] = None
                continue
            picked = select_etf(scores, True)
            picks[(sig, day)] = (
                (picked[1]["code"], picked[0], picked[1]["name"]) if picked else None
            )
    return picks


def run_combo(
    sig: str,
    buy: str,
    mode: str,
    sell_cut: str | None,
    label: str,
    picks: dict,
    etf_5min: dict,
    all_dates: list[str],
    eval_dates: list[str],
    fee: float,
) -> dict | None:
    rets: list[float] = []
    for day in eval_dates:
        p = picks.get((sig, day))
        if not p:
            continue
        code, _, _ = p
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_p = price_at_time(day_bars, buy)
        if not buy_p:
            continue
        if mode in ("same_close", "same_trail"):
            sp = simulate_exit(mode, buy_p, day_bars, buy, [], None)
        else:
            if day not in all_dates:
                continue
            idx = all_dates.index(day)
            if idx + 1 >= len(all_dates):
                continue
            nb = etf_5min.get(code, {}).get(all_dates[idx + 1], [])
            if not nb:
                continue
            sp = simulate_exit(mode, buy_p, day_bars, buy, nb, sell_cut)
        if not sp:
            continue
        rets.append(apply_net_return(buy_p, sp, fee))
    if len(rets) < 10:
        return None
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {
        "label": label,
        "signal": sig,
        "buy": buy,
        "sell": sell_cut or mode,
        "count": len(rets),
        "total": (eq - 1) * 100,
        "stats": _calc_stats(rets),
    }


def search(
    days: int,
    skip_choppy: bool,
    fee: float,
) -> tuple[list[str], list[dict]]:
    etf_list = get_all_t0_etfs()
    etf_daily, etf_5min, all_dates, proxy_klines = load_market_data(
        etf_list, days, daily_only=False,
    )
    eval_dates = all_dates[-days:]
    picks = precompute_picks(
        etf_list, etf_daily, etf_5min, eval_dates, proxy_klines, skip_choppy,
    )
    results: list[dict] = []
    for sig in SIGNALS:
        for buy in BUYS:
            if time_to_min(buy) <= time_to_min(sig):
                continue
            if not same_session(sig, buy):
                continue
            if time_to_min(buy) > time_to_min("14:55"):
                continue
            for sell in SELL_CUTS:
                if time_to_min(sell) >= time_to_min(buy):
                    continue
                for mode, lbl_tpl in [
                    ("time", "定时卖@{}"),
                    ("trix_cut", "TRIX≤{}"),
                    ("trix0940_cut", "TRIX≥09:40≤{}"),
                    ("trail", "追踪≤{}"),
                ]:
                    r = run_combo(
                        sig, buy, mode, sell, lbl_tpl.format(sell),
                        picks, etf_5min, all_dates, eval_dates, fee,
                    )
                    if r:
                        results.append(r)
            for mode, lbl in [
                ("trix_full", "TRIX死叉(次日全天)"),
                ("trix_0940", "TRIX≥09:40(次日全天)"),
                ("trail", "追踪(次日全天)"),
                ("fixed", "固定+3%/-1.5%"),
            ]:
                r = run_combo(
                    sig, buy, mode, None, lbl,
                    picks, etf_5min, all_dates, eval_dates, fee,
                )
                if r:
                    results.append(r)
            for mode, lbl in [("same_close", "当日收盘卖"), ("same_trail", "当日追踪卖")]:
                r = run_combo(
                    sig, buy, mode, None, lbl,
                    picks, etf_5min, all_dates, eval_dates, fee,
                )
                if r:
                    results.append(r)
    results.sort(key=lambda x: x["total"], reverse=True)
    return eval_dates, results


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 ETF 买卖时间窗口搜索")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--no-skip-choppy", action="store_true")
    args = parser.parse_args()

    skip = not args.no_skip_choppy
    print(f"=== T+0 时间窗口搜索 ({args.days}日) ===")
    print(f"过滤: 涨幅≥3% | 震荡跳过={'是' if skip else '否'} | 手续费万{args.fee*100:.0f}\n")

    eval_dates, results = search(args.days, skip, args.fee)
    print(f"区间: {eval_dates[0]} ~ {eval_dates[-1]}")
    print(f"有效组合: {len(results)}\n")
    print(f"{'#':>3} {'信号':>6} {'买入':>6} {'策略':<28} {'笔':>4} {'累计':>9} {'胜率':>6} {'回撤':>8}")
    print("-" * 90)
    for i, r in enumerate(results[: args.top], 1):
        st = r["stats"]
        print(
            f"{i:>3} {r['signal']:>6} {r['buy']:>6} {r['label']:<28} {r['count']:>4} "
            f"{r['total']:+8.2f}% {st.get('win_rate', 0):>5.1f}% {st.get('max_drawdown', 0):>+7.2f}%"
        )
    if results:
        best = results[0]
        print(f"\n★ 最优: 信号{best['signal']} 买{best['buy']} | {best['label']} → {best['total']:+.2f}%")

    out = Path.home() / ".tradingagents" / "rotation" / f"search_t0_time_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"eval_dates": [eval_dates[0], eval_dates[-1]], "results": results[:100]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n已保存: {out}")


if __name__ == "__main__":
    main()

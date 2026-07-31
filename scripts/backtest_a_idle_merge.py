#!/usr/bin/env python3
"""实盘核心(hybrid-A+TRIX) + idle(隔夜动量腿) 合并回测 —— 回答「实盘+idle 会不会大于 B+idle 的 124.5%」。

核心腿 = 实盘等价: build_picks_hybrid(regime过滤+滚动优质池) + run_strategy("trix")(TRIX死叉卖)。
idle 腿: 核心 14:45 未触发时, 14:50 买当日最强涨幅≥1.0% T0 ETF, 次日 14:50 固定卖。
            ⚠ 关键: idle 只在「实盘核心未触发」的日子买 —— 与 B+idle 不同(B核心触发更多→idle日更少)。

用法:
    python scripts/backtest_a_idle_merge.py            # 近100日(对齐 B+idle 的 +124.5% 窗口)
    python scripts/backtest_a_idle_merge.py --days 100
    python scripts/backtest_a_idle_merge.py --start 2022-06-15  # 全周期对照
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa: E402
from backtest_t0_etf import price_at_time  # noqa: E402
from backtest_t0_idle_window import sell_time_mode  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT, next_trading_day,
)
from backtest_b_idle_merge import (  # noqa: E402
    LB, MOM_BUY, MOM_SELL, MOM_THR,
    build_prev_close, merge_equity, pick_momentum, stats_of,
)
from quality_pool import build_picks_hybrid  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
OUT = Path.home() / ".tradingagents/cache/t0_5min/a_idle_merge.json"


def run_live_plus_idle(cache, eval_dates, all_dates, lb_start):
    etf_daily = cache["etf_daily"]
    etf_5min = __import__("copy").deepcopy(cache["etf_5min"])
    proxy = cache["proxy_klines"]
    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    prev_close = build_prev_close(etf_list, etf_daily)

    # ① 实盘核心腿 (hybrid-A + TRIX)
    picks = build_picks_hybrid(
        eval_dates, etf_list, etf_daily, etf_5min, all_dates, proxy,
        lookback=LB, warmup=LB,
    )
    res = run_strategy("trix", eval_dates, all_dates, picks, etf_5min, FEE_PCT)
    core_trades = res["trades"] if res else []

    # ② idle 腿: 仅在「实盘核心未触发」的日子买
    idle_days = [d for d in eval_dates[LB:] if not picks.get((SIGNAL_TIME, d))]
    mom = []
    skipped = 0
    for day in idle_days:
        pk = pick_momentum(etf_list, etf_5min, prev_close, day, MOM_THR)
        if not pk:
            skipped += 1
            continue
        code, gain = pk
        bars = etf_5min.get(code, {}).get(day, [])
        bp = price_at_time(bars, MOM_BUY)
        if not bp or bp <= 0:
            continue
        nday = next_trading_day(all_dates, day)
        if not nday:
            continue
        nb = etf_5min.get(code, {}).get(nday, [])
        out = sell_time_mode(nb, MOM_BUY, MOM_SELL, bp, FEE_PCT)
        if not out:
            continue
        ret, reason = out
        mom.append({
            "signal_date": day, "sell_date": nday, "etf": code,
            "today_gain": round(gain, 2), "return_pct": round(ret, 4),
            "sell_reason": "momentum_" + reason,
        })
    merged = core_trades + mom
    return core_trades, mom, merged, skipped, len(idle_days)


def per_year(trades):
    by = defaultdict(list)
    for t in trades:
        by[t["signal_date"][:4]].append(t["return_pct"])
    out = {}
    for y in sorted(by):
        rs = by[y]
        eq = 1.0
        for x in rs:
            eq *= 1 + x / 100
        out[y] = {
            "trades": len(rs),
            "year_return_pct": round((eq - 1) * 100, 2),
            "win_rate": round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1),
        }
    return out


def fmt(pct):
    return f"{pct:+.2f}%"


def main():
    ap = argparse.ArgumentParser(description="实盘核心(hybrid-A+TRIX)+idle 合并回测")
    ap.add_argument("--cache", type=str, default=str(CACHE_FILE))
    ap.add_argument("--days", type=int, default=100,
                    help="近 N 交易日窗口(对齐 B+idle 的 +124.5% 口径)")
    ap.add_argument("--start", type=str, default=None,
                    help="或直接指定起点日期(全周期对照)")
    args = ap.parse_args()

    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]

    if args.start:
        win_start = args.start
        s = max(0, all_dates.index(win_start) - LB) if win_start in all_dates else 0
        eval_dates = all_dates[s:]
    else:
        N = args.days
        win_start = all_dates[-N]
        s = max(0, len(all_dates) - N - LB)
        eval_dates = all_dates[s:]

    print(f"=== 实盘核心(hybrid-A+TRIX) + idle(隔夜动量) 合并回测 ===")
    print(f"    窗口 {win_start} ~ {all_dates[-1]} (eval含warmup共 {len(eval_dates)}天)\n")

    core_tr, mom_tr, merged_tr, skipped, n_idle = run_live_plus_idle(
        cache, eval_dates, all_dates, s)
    # 仅取窗口内
    core_tr = [t for t in core_tr if t["signal_date"] >= win_start]
    mom_tr = [t for t in mom_tr if t["signal_date"] >= win_start]
    merged_tr = [t for t in merged_tr if t["signal_date"] >= win_start]

    sc = stats_of(core_tr)
    sm = stats_of(mom_tr)
    sall = stats_of(merged_tr)

    print(f"★ 实盘核心(hybrid-A+TRIX): {fmt(sc['equity_pct'])}  {sc['trades']}笔  "
          f"胜{sc['win_rate']:.0f}%  回撤{sc['max_drawdown']:+.1f}%")
    print(f"★ idle 腿(实盘未触发日):    {fmt(sm['equity_pct'])}  {sm['trades']}笔  "
          f"胜{sm['win_rate']:.0f}%  回撤{sm['max_drawdown']:+.1f}%  "
          f"(idle日 {n_idle}天, 其中 {skipped} 天无≥{MOM_THR:.1f}%标的)")
    print(f"\n>>> 合并(实盘核心+idle):    {fmt(sall['equity_pct'])}  {sall['trades']}笔  "
          f"胜{sall['win_rate']:.0f}%  回撤{sall['max_drawdown']:+.1f}%")
    print(f"    = 实盘核心 {fmt(sc['equity_pct'])}  +  idle增量 {fmt(sall['equity_pct']-sc['equity_pct'])}")

    # 对比 B+idle 的 +124.5%
    print(f"\n>>> 对比 B+idle(B核心+idle, 同口径窗口): +124.54%")
    print(f"    {'结论':<6}{'实盘+idle':>12}{'B+idle':>12}")
    print("    " + "-" * 30)
    verdict = "实盘+idle 更大" if sall["equity_pct"] > 124.54 else "B+idle 更大"
    print(f"    {verdict:<6}{fmt(sall['equity_pct']):>12}{'+124.54%':>12}")
    print(f"    (B+idle 多出的 {(124.54 - sall['equity_pct']):+.2f}pct 来自「B核心升级 vs 实盘A核心」, 非idle腿)")

    if args.days == 100 and not args.start:
        print(f"\n  逐年(实盘核心 / idle / 合并):")
        pc, pm, pa = per_year(core_tr), per_year(mom_tr), per_year(merged_tr)
        for y in sorted(pa):
            c = pc.get(y, {"year_return_pct": 0})
            m = pm.get(y, {"year_return_pct": 0})
            a = pa[y]
            print(f"    {y}: 核心{fmt(c['year_return_pct'])}  idle{fmt(m['year_return_pct'])}  "
                  f"合并{fmt(a['year_return_pct'])}")

    result = {
        "window": f"{win_start}~{all_dates[-1]}",
        "live_core": sc,
        "idle": sm,
        "merged": sall,
        "idle_days": n_idle,
        "idle_skipped": skipped,
        "vs_b_idle_pct": 124.54,
        "verdict": verdict,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已落盘: {OUT}")


if __name__ == "__main__":
    main()

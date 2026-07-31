#!/usr/bin/env python3
"""近 100 个交易日: B最强策略(B核心 + idle动量腿) 回测 vs 实盘策略(hybrid-A + TRIX) 回测, 同窗对比。

B最强 = 优化后核心(全市场Top1 ≥3%, 不regime过滤, hybrid=TRIX+追踪回落卖点)
      + 优化后idle(核心14:45未触发则14:50买当日最强涨幅≥1.0% T0 ETF, 次日14:50固定卖)
实盘   = 当前线上逻辑(hybrid-A选股 + 次日5分K TRIX(5,3)死叉卖), 即 build_picks_hybrid + run_strategy("trix")。

另叠加真实实盘成交日志(t0_trade_journal.jsonl)在窗口内的已实现收益做现实校验。

用法:
    python3 scripts/backtest_b_strongest_100d.py
    python3 scripts/backtest_b_strongest_100d.py --days 100
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
from backtest_t0_today1 import FEE_PCT, next_trading_day  # noqa: E402
from backtest_t0_etf import price_at_time  # noqa: E402
from backtest_t0_idle_window import sell_time_mode  # noqa: E402
from backtest_b_idle_merge import (  # noqa: E402
    LB, MOM_BUY, MOM_SELL, MOM_THR,
    build_picks_B, build_prev_close, merge_equity, pick_momentum, stats_of,
)
from quality_pool import build_picks_hybrid  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
LIVE_JOURNAL = Path.home() / ".tradingagents/rotation/t0_trade_journal.jsonl"
OUT = Path.home() / ".tradingagents/cache/t0_5min/b_strongest_100d.json"


def monthly_equity(trades):
    ts = sorted(trades, key=lambda t: t["signal_date"])
    by = defaultdict(list)
    for t in ts:
        by[t["signal_date"][:7]].append(t["return_pct"])
    eq = 1.0
    out = []
    for m in sorted(by):
        for r in by[m]:
            eq *= 1 + r / 100
        out.append((m, round((eq - 1) * 100, 2)))
    return out


def run_b_strongest(cache, eval_dates, all_dates, lb_start):
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    prev_close = build_prev_close(etf_list, etf_daily)

    picks_b = build_picks_B(eval_dates, etf_list, etf_daily, etf_5min, LB)
    core = run_strategy("hybrid", eval_dates, all_dates, picks_b, etf_5min, FEE_PCT)
    core_trades = core["trades"] if core else []

    idle_days = [d for d in eval_dates[LB:] if not picks_b.get((SIGNAL_TIME, d))]
    mom = []
    for day in idle_days:
        pk = pick_momentum(etf_list, etf_5min, prev_close, day, MOM_THR)
        if not pk:
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
    return core_trades, mom, merged


def run_live_strategy(cache, eval_dates, all_dates):
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    proxy = cache["proxy_klines"]
    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    picks = build_picks_hybrid(
        eval_dates, etf_list, etf_daily, etf_5min, all_dates, proxy,
        lookback=LB, warmup=LB,
    )
    res = run_strategy("trix", eval_dates, all_dates, picks, etf_5min, FEE_PCT)
    return res["trades"] if res else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(CACHE_FILE))
    ap.add_argument("--days", type=int, default=100)
    args = ap.parse_args()

    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    N = args.days
    win_start = all_dates[-N]
    s = max(0, len(all_dates) - N - LB)
    eval_dates = all_dates[s:]

    print(f"=== 近 {N} 交易日: B最强(B核心+idle) vs 实盘(hybrid-A+TRIX) ===")
    print(f"    窗口 {win_start} ~ {all_dates[-1]}  (eval含warmup共 {len(eval_dates)}天)\n")

    core_tr, mom_tr, merged_tr = run_b_strongest(cache, eval_dates, all_dates, s)
    core_tr = [t for t in core_tr if t["signal_date"] >= win_start]
    mom_tr = [t for t in mom_tr if t["signal_date"] >= win_start]
    merged_tr = [t for t in merged_tr if t["signal_date"] >= win_start]

    live_tr = run_live_strategy(cache, eval_dates, all_dates)
    live_tr = [t for t in live_tr if t["signal_date"] >= win_start]

    def show(name, trades):
        sm = stats_of(trades)
        print(f"  {name:<22} 笔数 {sm['trades']:>3}  累计 {sm['equity_pct']:>+9.2f}%  "
              f"胜率 {sm['win_rate']:>5.1f}%  回撤 {sm['max_drawdown']:>+7.1f}%")
        return sm

    print(f"  {'策略':<22}{'笔数':>5}{'累计':>11}{'胜率':>7}{'回撤':>9}")
    print("  " + "-" * 54)
    b_sm = show("B+idle(最强,合并)", merged_tr)
    show("  B核心(hybrid卖)", core_tr)
    show("  idle动量腿", mom_tr)
    l_sm = show("实盘 hybrid-A+TRIX", live_tr)
    print()
    print(f"  → 近{N}日 B+idle 累计 {b_sm['equity_pct']:+.2f}%  vs 实盘 {l_sm['equity_pct']:+.2f}%  "
          f"差值 {b_sm['equity_pct']-l_sm['equity_pct']:+.2f}pct  "
          f"(B多 {b_sm['trades']-l_sm['trades']} 笔)")

    print("\n  ── 逐月累计(窗口内) ──")
    bm = monthly_equity(merged_tr)
    lm = monthly_equity(live_tr)
    mset = sorted(set(m for m, _ in bm) | set(m for m, _ in lm))
    print(f"  {'月':<9}{'B+idle':>10}{'实盘':>10}")
    for m in mset:
        bv = next((v for x, v in bm if x == m), None)
        lv = next((v for x, v in lm if x == m), None)
        print(f"  {m:<9}{('' if bv is None else f'{bv:+.1f}%'):>10}{('' if lv is None else f'{lv:+.1f}%'):>10}")

    # 真实实盘日志叠加
    if LIVE_JOURNAL.exists():
        rows = []
        for line in LIVE_JOURNAL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("event") == "trade_closed" and o.get("buy_date", "") >= win_start:
                rows.append(o)
        if rows:
            real_eq = 1.0
            for o in rows:
                real_eq *= 1 + float(o.get("return_pct", 0)) / 100
            src = rows[0].get("source", "")
            print(f"\n  ── 成交日志(窗口内 {len(rows)} 笔, source={src}) ──")
            print(f"  已实现累计: {(real_eq-1)*100:+.2f}%  "
                  f"(同窗 B+idle 回测 {b_sm['equity_pct']:+.2f}%, 实盘策略回测 {l_sm['equity_pct']:+.2f}%)")
            if src == "shadow_backfill":
                print(f"  注意: 该日志 source=shadow_backfill, 实为 B+idle SHADOW 自身回放, "
                      f"并非独立实盘成交; 独立实盘对照以『实盘 hybrid-A+TRIX』回测为准(上表)。")

    result = {
        "window": f"{win_start}~{all_dates[-1]}",
        "days": N,
        "b_strongest": b_sm,
        "b_core": stats_of(core_tr),
        "idle": stats_of(mom_tr),
        "live_hybrid_a_trix": l_sm,
        "b_minus_live_pct": round(b_sm["equity_pct"] - l_sm["equity_pct"], 2),
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已落盘: {OUT}")


if __name__ == "__main__":
    main()

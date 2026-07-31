#!/usr/bin/env python3
"""B(全市场Top1选股) + idle(闲置资金隔夜动量腿) 合并回测 + Walk-Forward 验证。

核心腿 B: 全市场 T0 ETF 扫 ≥3% Top1, 不 regime 过滤, 不 skip, hybrid(TRIX+追踪回落)卖点。
idle 腿: 核心 14:45 未触发时, 14:50 买当日最强涨幅≥1.0% T0 ETF, 次日 14:50 固定卖。
两腿都在 14:50 决策、按 idle/signal 路由, 资金每天最多一笔, 串行复利等价单资金。

对照:
  - 纯 B 核心腿(无 idle) in-sample / OOS
  - B+idle 合并 in-sample / OOS
  - B+idle 的 idle 腿独立贡献

WF 60/40 分界 2024-12-19 (与 backtest_core_pool_wf 一致), 验证段 388 天。

用法:
    python scripts/backtest_b_idle_merge.py
    python scripts/backtest_b_idle_merge.py --mode wf
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
    FEE_PCT, rank_by_today_gain, passes_gain_filter,
    next_trading_day,
)
from quality_pool import regime_on_date  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
OUT = Path.home() / ".tradingagents/cache/t0_5min/b_idle_merge.json"
START = "2022-06-15"
LB = 30

MOM_BUY = "14:50"
MOM_THR = 1.0          # idle 动量选股阈值(记忆 id 48239141: 1.0% OOS 最优)
MOM_SELL = "14:50"     # 次日固定 14:50 卖(已验证 OOS 增量最高)


# ── B 选股核心腿: 全市场 T0 ETF, ≥3%, 不 regime 过滤, 不 skip ──
def build_picks_B(eval_dates, etf_list, etf_daily, etf_5min, warmup):
    picks = {}
    for i, day in enumerate(eval_dates):
        if i < warmup:
            picks[(SIGNAL_TIME, day)] = None
            continue
        scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, SIGNAL_TIME)
        cands = [(g, e) for g, e in scores if passes_gain_filter(g)]
        if cands:
            g, e = cands[0]
            picks[(SIGNAL_TIME, day)] = (
                e["code"], g, e.get("name") or e.get("etf_name") or e["code"])
        else:
            picks[(SIGNAL_TIME, day)] = None
    return picks


def build_prev_close(etf_list, etf_daily) -> dict:
    pc = {}
    for etf in etf_list:
        code = etf["code"]
        info = etf_daily.get(code)
        if not info:
            continue
        d = {}
        returns = info.get("returns", [])
        for i in range(1, len(returns)):
            p = returns[i - 1].get("close")
            if p and p > 0:
                d[returns[i]["date"]] = float(p)
        pc[code] = d
    return pc


def pick_momentum(etf_list, etf_5min, prev_close, day, threshold):
    """idle 日 14:50 取当日涨幅最高的 T0 ETF；要求涨幅 ≥ threshold。"""
    cands = []
    for etf in etf_list:
        code = etf["code"]
        prev = prev_close.get(code, {}).get(day)
        if not prev or prev <= 0:
            continue
        bars = etf_5min.get(code, {}).get(day, [])
        px = price_at_time(bars, MOM_BUY)
        if px is None or px <= 0:
            continue
        gain = (px - prev) / prev * 100
        cands.append((gain, code))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0], reverse=True)
    best_gain, best_code = cands[0]
    if best_gain < threshold:
        return None
    return best_code, best_gain


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


def merge_equity(trades):
    ts = sorted(trades, key=lambda t: t["signal_date"])
    eq = 1.0
    for t in ts:
        eq *= 1 + t["return_pct"] / 100
    return (eq - 1) * 100


def stats_of(trades):
    if not trades:
        return {"trades": 0, "equity_pct": 0.0, "win_rate": 0.0, "max_drawdown": 0.0}
    eq = cur = 1.0
    peak = 1.0
    mdd = 0.0
    for t in sorted(trades, key=lambda x: x["signal_date"]):
        r = t["return_pct"]
        eq *= 1 + r / 100
        cur *= 1 + r / 100
        peak = max(peak, cur)
        mdd = min(mdd, (cur - peak) / peak * 100)
    win = sum(1 for t in trades if t["return_pct"] > 0) / len(trades) * 100
    return {"trades": len(trades), "equity_pct": round((eq - 1) * 100, 2),
            "win_rate": round(win, 1), "max_drawdown": round(mdd, 1)}


def main():
    ap = argparse.ArgumentParser(description="B+idle 合并回测 + WF")
    ap.add_argument("--cache", type=str, default=str(CACHE_FILE))
    ap.add_argument("--start", type=str, default=START)
    ap.add_argument("--mode", type=str, default="both",
                    choices=["full", "wf", "both"])
    ap.add_argument("--fee", type=float, default=FEE_PCT)
    args = ap.parse_args()

    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    proxy = cache.get("proxy_klines", [])
    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    prev_close = build_prev_close(etf_list, etf_daily)
    eval_dates = [d for d in all_dates if d >= args.start]

    print(f"=== B(全市场Top1)+idle(隔夜动量) 合并回测 ===")
    print(f"    区间 {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}天)")
    print(f"    核心腿: hybrid(TRIX+追踪回落)卖 | idle腿: {MOM_BUY}买≥{MOM_THR:.1f}%→次日{MOM_SELL}卖\n")

    # ① 核心腿 B (hybrid 卖点)
    picks_b = build_picks_B(eval_dates, etf_list, etf_daily, etf_5min, LB)
    core = run_strategy("hybrid", eval_dates, all_dates, picks_b, etf_5min, FEE_PCT)
    core_trades = core["trades"] if core else []
    print(f"★ 纯 B 核心(hybrid): {core['final_equity_pct']:+.2f}%  {len(core_trades)}笔  "
          f"胜{core['stats']['win_rate']:.0f}%  回撤{core['stats']['max_drawdown']:+.1f}%")
    for y, s in per_year(core_trades).items():
        print(f"    {y}: {s['year_return_pct']:+.2f}%  {s['trades']}笔 胜{s['win_rate']:.0f}%")

    # ② idle 动量腿
    idle_days = [d for d in eval_dates[LB:] if not picks_b.get((SIGNAL_TIME, d))]
    print(f"\n>>> idle(隔夜闲置)日: {len(idle_days)} 天（排除核心 warmup 前 {LB} 天）")
    momentum_trades = []
    skipped = 0
    for day in idle_days:
        pk = pick_momentum(etf_list, etf_5min, prev_close, day, MOM_THR)
        if not pk:
            skipped += 1
            continue
        code, gain = pk
        day_bars = etf_5min.get(code, {}).get(day, [])
        bp = price_at_time(day_bars, MOM_BUY)
        if not bp or bp <= 0:
            continue
        nday = next_trading_day(all_dates, day)
        if not nday:
            continue
        next_bars = etf_5min.get(code, {}).get(nday, [])
        out = sell_time_mode(next_bars, MOM_BUY, MOM_SELL, bp, args.fee)
        if not out:
            continue
        ret, reason = out
        momentum_trades.append({
            "signal_date": day, "sell_date": nday, "etf": code,
            "today_gain": round(gain, 2), "return_pct": round(ret, 4),
            "sell_reason": "momentum_" + reason,
        })
    mom_eq = merge_equity(momentum_trades)
    print(f"    动量腿触发: {len(momentum_trades)} 笔（idle日中 {skipped} 天无≥{MOM_THR:.1f}%标的）")
    for y, s in per_year(momentum_trades).items():
        print(f"    {y}: {s['year_return_pct']:+.2f}%  {s['trades']}笔 胜{s['win_rate']:.0f}%")

    # ③ 合并
    merged = core_trades + momentum_trades
    merged_eq = merge_equity(merged)
    sm = stats_of(merged)
    print(f"\n>>> 合并 (B核心+idle动量): {merged_eq:+.2f}%  {sm['trades']}笔  "
          f"胜{sm['win_rate']:.0f}%  回撤{sm['max_drawdown']:+.1f}%  [IN-SAMPLE上界, 过拟合风险]")
    print(f"    = 纯B核心 {core['final_equity_pct']:+.2f}%  +  idle增量 {merged_eq - core['final_equity_pct']:+.2f}%")
    print(f"    ⚠ 真实预期看下方 OOS: 合并 {oos_merged['equity_pct']:+.2f}% (idle贡献 +{oos_merged['equity_pct']-oos_core['equity_pct']:+.2f}%)")
    py_c = per_year(core_trades)
    py_m = per_year(momentum_trades)
    py_all = per_year(merged)
    print(f"\n  {'年':<6}{'B核心':>10}{'idle腿':>10}{'合并':>10}")
    print("  " + "-" * 36)
    for y in sorted(py_all):
        c = py_c.get(y, {"year_return_pct": 0})
        m = py_m.get(y, {"year_return_pct": 0})
        a = py_all[y]
        print(f"  {y:<6}{c['year_return_pct']:>+9.1f}%{m['year_return_pct']:>+9.1f}%{a['year_return_pct']:>+9.1f}%")

    # ④ Walk-Forward 60/40
    core_eval = eval_dates[LB:]
    split = int(len(core_eval) * 0.6)
    split_date = core_eval[split]
    print(f"\n>>> Walk-Forward 60/40 分界 {split_date} (OOS {len(core_eval)-split}天)")

    def on(tr, lo=None):
        return [t for t in tr if lo is None or t["signal_date"] >= lo]

    oos_core = stats_of(on(core_trades, lo=split_date))
    oos_mom = stats_of(on(momentum_trades, lo=split_date))
    oos_merged = stats_of(on(merged, lo=split_date))
    print(f"    OOS B核心:    {oos_core['equity_pct']:+.2f}%  {oos_core['trades']}笔")
    print(f"    OOS idle腿:   {oos_mom['equity_pct']:+.2f}%  {oos_mom['trades']}笔")
    print(f"    OOS 合并:     {oos_merged['equity_pct']:+.2f}%  {oos_merged['trades']}笔  "
          f"(idle增量 {oos_merged['equity_pct']-oos_core['equity_pct']:+.2f}%)")
    # OOS 逐年
    oos_core_y = per_year(on(core_trades, lo=split_date))
    oos_mom_y = per_year(on(momentum_trades, lo=split_date))
    oos_all_y = per_year(on(merged, lo=split_date))
    print(f"\n  OOS逐年:")
    print(f"  {'年':<6}{'B核心':>10}{'idle腿':>10}{'合并':>10}")
    print("  " + "-" * 36)
    for y in sorted(oos_all_y):
        c = oos_core_y.get(y, {"year_return_pct": 0})
        m = oos_mom_y.get(y, {"year_return_pct": 0})
        a = oos_all_y[y]
        print(f"  {y:<6}{c['year_return_pct']:>+9.1f}%{m['year_return_pct']:>+9.1f}%{a['year_return_pct']:>+9.1f}%")

    result = {
        "window": f"{eval_dates[0]}~{eval_dates[-1]}",
        "split_date": split_date,
        "in_sample": {
            "b_core": stats_of(core_trades),
            "idle": stats_of(momentum_trades),
            "merged": sm,
        },
        "oos": {
            "b_core": oos_core,
            "idle": oos_mom,
            "merged": oos_merged,
            "idle_increment_pct": round(oos_merged["equity_pct"] - oos_core["equity_pct"], 2),
        },
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已落盘: {OUT}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""核心日选股消融 2x2: 池(候选集) × skip(震荡/中性是否跳过)
   A = build_picks_hybrid        (A池: 优质/原池路由  + skip: 按regime)
   P = 全市场池 + 与A相同skip行为 (+ 品类过滤)      → 隔离"放宽候选集"效应
   C = A池 + 永不skip(所有日子交易)                 → 隔离"不skip_choppy"效应
   B = 全市场池 + 不skip          (现状B, = P + Δ(skip) = C + Δ(池))
   卖点固定 trix (隔离选股维度)。WF 60/40 看各增量在OOS是否稳健。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa: E402
from quality_pool import (  # noqa: E402
    build_picks_hybrid, regime_uses_quality_pool, _quality_pool_for_day,
    load_quality_pool, pick_top1_from_pool, regime_on_date,
)
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT, MIN_GAIN, rank_by_today_gain, passes_gain_filter,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
START = "2022-06-15"
LB = 30


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


def per_year(trades):
    by = defaultdict(list)
    for t in trades:
        by[t["signal_date"][:4]].append(t["return_pct"])
    out = {}
    for y in sorted(by):
        eq = 1.0
        for x in by[y]:
            eq *= 1 + x / 100
        out[y] = {"trades": len(by[y]), "year_return_pct": round((eq - 1) * 100, 2)}
    return out


def build_picks_P(eval_dates, etf_list, etf_daily, etf_5min, proxy, warmup):
    picks = {}
    for i, day in enumerate(eval_dates):
        if i < warmup:
            picks[(SIGNAL_TIME, day)] = None
            continue
        reg = regime_on_date(proxy, day)
        skip = (reg or {}).get("mode") != "震荡"   # 与 A 同 skip 行为
        picks[(SIGNAL_TIME, day)] = pick_top1_from_pool(
            etf_list, day, etf_daily, etf_5min, proxy,
            skip_choppy=skip, use_regime_filter=True)
    return picks


def build_picks_C(eval_dates, orig_pool, etf_daily, etf_5min, all_dates, proxy, lookback, warmup):
    static_quality = load_quality_pool()
    picks = {}
    for i, day in enumerate(eval_dates):
        if i < warmup:
            picks[(SIGNAL_TIME, day)] = None
            continue
        reg = regime_on_date(proxy, day)
        if regime_uses_quality_pool(reg):   # 趋势/震荡 → 优质池, 不skip
            pool = _quality_pool_for_day(
                day, eval_dates, etf_daily, etf_5min, all_dates, proxy,
                lookback=lookback, static_quality=static_quality, orig_pool=orig_pool)
            picks[(SIGNAL_TIME, day)] = pick_top1_from_pool(
                pool, day, etf_daily, etf_5min, proxy,
                skip_choppy=False, use_regime_filter=True)
        else:                                # 中性 → 原T0池, 不skip
            scores = rank_by_today_gain(orig_pool, etf_daily, etf_5min, day, SIGNAL_TIME)
            cands = [(g, e) for g, e in scores if passes_gain_filter(g)]
            if cands:
                g, e = cands[0]
                picks[(SIGNAL_TIME, day)] = (e["code"], g, e.get("name") or e.get("etf_name") or e["code"])
            else:
                picks[(SIGNAL_TIME, day)] = None
    return picks


def build_picks_B(eval_dates, etf_list, etf_daily, etf_5min, warmup):
    picks = {}
    for i, day in enumerate(eval_dates):
        if i < warmup:
            picks[(SIGNAL_TIME, day)] = None
            continue
        tg = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, SIGNAL_TIME)
        for gain, etf in tg:
            if gain >= MIN_GAIN:
                picks[(SIGNAL_TIME, day)] = (etf["code"], gain, etf.get("name", etf["code"]))
                break
    return picks


def main():
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    proxy = cache.get("proxy_klines", [])
    codes5 = set(etf_5min.keys())
    orig_pool = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    etf_list = get_all_t0_etfs()
    eval_dates = [d for d in all_dates if d >= START]

    # 构造 4 方案 picks
    print("构造 4 个消融方案 picks ...", flush=True)
    pA = build_picks_hybrid(eval_dates, orig_pool, etf_daily, etf_5min,
                            all_dates, proxy, lookback=LB, warmup=LB)
    pP = build_picks_P(eval_dates, etf_list, etf_daily, etf_5min, proxy, LB)
    pC = build_picks_C(eval_dates, orig_pool, etf_daily, etf_5min, all_dates, proxy, LB, LB)
    pB = build_picks_B(eval_dates, etf_list, etf_daily, etf_5min, LB)

    runs = {"A": pA, "P": pP, "C": pC, "B": pB}
    trades = {}
    for k, picks in runs.items():
        r = run_strategy("trix", eval_dates, all_dates, picks, etf_5min, FEE_PCT)
        trades[k] = r["trades"] if r else []
        print(f"  {k} 完成: {len(trades[k])}笔", flush=True)

    sA, sP, sC, sB = (stats_of(trades[k]) for k in ("A", "P", "C", "B"))
    print(f"\n=== 核心日选股消融 2x2 (池 × skip) · trix卖点 · WF 60/40 ===")
    print(f"窗口 {eval_dates[0]}~{eval_dates[-1]} ({len(eval_dates)}天, 有效{LB} warmup)\n")

    print("[In-sample 全周期]")
    print(f"  {'方案':<22}{'笔数':>5}{'累计':>11}{'胜率':>7}{'回撤':>9}")
    print("  " + "-" * 54)
    for k, lab in (("A", "A 现状(A池+skip)"), ("P", "P 全市场+skip"),
                   ("C", "C A池+不skip"), ("B", "B 全市场+不skip")):
        s = trades and stats_of(trades[k])
        print(f"  {lab:<20}{s['trades']:>5}{s['equity_pct']:>+10.2f}%{s['win_rate']:>6.1f}%{s['max_drawdown']:>+8.1f}%")
    print()

    # WF 切分
    core_eval = eval_dates[LB:]
    split = int(len(core_eval) * 0.6)
    split_date = core_eval[split]

    def on(trades, lo=None, hi=None):
        return [t for t in trades
                if (lo is None or t["signal_date"] >= lo)
                and (hi is None or t["signal_date"] < hi)]

    def stat_on(k, lo=None, hi=None):
        return stats_of(on(trades[k], lo, hi))

    oos = {k: stat_on(k, lo=split_date) for k in runs}
    tr = {k: stat_on(k, hi=split_date) for k in runs}

    print(f"[Walk-Forward 60/40]  分界 {split_date}  "
          f"(训练{len(core_eval[:split])}天 / OOS{len(core_eval[split:])}天)\n")
    print(f"  训练段:", end="")
    for k in ("A", "P", "C", "B"):
        print(f"  {k}={tr[k]['equity_pct']:>+7.2f}%(n{tr[k]['trades']})", end="")
    print(f"\n  OOS段:  ", end="")
    for k in ("A", "P", "C", "B"):
        print(f"  {k}={oos[k]['equity_pct']:>+7.2f}%(n{oos[k]['trades']})", end="")
    print("\n")

    # 消融增量 (OOS)
    d_pool = round(oos["P"]["equity_pct"] - oos["A"]["equity_pct"], 2)   # 放宽候选集
    d_skip = round(oos["C"]["equity_pct"] - oos["A"]["equity_pct"], 2)   # 不skip
    total = round(oos["B"]["equity_pct"] - oos["A"]["equity_pct"], 2)
    d_pool_n = oos["P"]["trades"] - oos["A"]["trades"]
    d_skip_n = oos["C"]["trades"] - oos["A"]["trades"]
    print("[消融增量 (OOS)]")
    print(f"  Δ(池)  = P - A = {d_pool:>+8.2f}%  (+{d_pool_n}笔)   ← 放宽候选集(全市场 vs 优质/原池) 贡献")
    print(f"  Δ(skip)= C - A = {d_skip:>+8.2f}%  (+{d_skip_n}笔)   ← 不skip_choppy(震荡/中性也交易) 贡献")
    print(f"  总增益 = B - A = {total:>+8.2f}%")
    print(f"  加性近似: Δ(池)+Δ(skip) = {d_pool + d_skip:>+8.2f}%  (与总增益差 {d_pool + d_skip - total:>+7.2f}%, "
          f"差异来自两来源重叠/替代, 非零属正常)")
    if d_pool != 0 and d_skip != 0:
        print(f"  占比: 放宽池 {d_pool/(d_pool+d_skip)*100:>5.1f}%  |  不skip {d_skip/(d_pool+d_skip)*100:>5.1f}%")
    print()

    # OOS 逐年对照
    print("[OOS 验证段逐年  Δ(池) / Δ(skip) / 总增益]")
    pya, pyp, pyc, pyb = (per_year(on(trades[k], lo=split_date)) for k in ("A", "P", "C", "B"))
    print(f"    {'年':<6}{'Δ(池)':>10}{'Δ(skip)':>10}{'B-A':>10}")
    print("    " + "-" * 38)
    for y in sorted(pya):
        dp = pyp.get(y, {"year_return_pct": 0})["year_return_pct"] - pya[y]["year_return_pct"]
        ds = pyc.get(y, {"year_return_pct": 0})["year_return_pct"] - pya[y]["year_return_pct"]
        tb = pyb.get(y, {"year_return_pct": 0})["year_return_pct"] - pya[y]["year_return_pct"]
        print(f"    {y:<6}{dp:>+9.1f}%{ds:>+9.1f}%{tb:>+9.1f}%")

    result = {
        "window": f"{eval_dates[0]}~{eval_dates[-1]}",
        "split_date": split_date,
        "in_sample": {"A": sA, "P": sP, "C": sC, "B": sB},
        "walk_forward": {"train": tr, "oos": oos},
        "ablation_oos": {
            "d_pool": d_pool, "d_pool_n": d_pool_n,
            "d_skip": d_skip, "d_skip_n": d_skip_n,
            "total": total,
            "pool_share_pct": round(d_pool / (d_pool + d_skip) * 100, 1) if (d_pool + d_skip) else None,
            "skip_share_pct": round(d_skip / (d_pool + d_skip) * 100, 1) if (d_pool + d_skip) else None,
        },
        "oos_per_year": {y: {
            "d_pool": round(pyp.get(y, {"year_return_pct": 0})["year_return_pct"] - pya[y]["year_return_pct"], 2),
            "d_skip": round(pyc.get(y, {"year_return_pct": 0})["year_return_pct"] - pya[y]["year_return_pct"], 2),
            "total": round(pyb.get(y, {"year_return_pct": 0})["year_return_pct"] - pya[y]["year_return_pct"], 2),
        } for y in pya},
    }
    OUT = Path.home() / ".tradingagents/cache/t0_5min/core_pool_ablation.json"
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结论已落盘: {OUT}")


if __name__ == "__main__":
    main()

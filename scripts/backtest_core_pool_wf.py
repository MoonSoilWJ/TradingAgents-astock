#!/usr/bin/env python3
"""核心日选股方案 × 卖点 Walk-Forward 验证:
   选股: A = build_picks_hybrid (现状 hybrid-A)
         B = 所有核心日扫全市场 T0 ETF Top1 (>=3%, 14:45), 不 regime 过滤, 不 skip_choppy
   卖点: trix   = TRIX(5,3)死叉卖 (09:40起)
         hybrid = TRIX死叉 + 追踪回落止盈
   验证 B 相对 A 的增益在样本外(OOS)是否稳健, 以及 hybrid 卖点是否进一步放大增益。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa: E402
from quality_pool import build_picks_hybrid  # noqa: E402
from backtest_t0_today1 import FEE_PCT, MIN_GAIN, rank_by_today_gain  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
START = "2022-06-15"
LB = 30
SELL_MODES = ["trix", "hybrid"]
SELL_LABEL = {"trix": "TRIX死叉", "hybrid": "TRIX+追踪回落"}


def stats_of(trades):
    if not trades:
        return {"trades": 0, "equity_pct": 0.0, "win_rate": 0.0, "max_drawdown": 0.0}
    eq = 1.0
    peak = cur = 1.0
    mdd = 0.0
    for t in sorted(trades, key=lambda x: x["signal_date"]):
        r = t["return_pct"]
        eq *= 1 + r / 100
        cur *= 1 + r / 100
        peak = max(peak, cur)
        mdd = min(mdd, (cur - peak) / peak * 100)
    win = sum(1 for t in trades if t["return_pct"] > 0) / len(trades) * 100
    return {
        "trades": len(trades),
        "equity_pct": round((eq - 1) * 100, 2),
        "win_rate": round(win, 1),
        "max_drawdown": round(mdd, 1),
    }


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


def build_picks(choice, eval_dates, orig_pool, etf_list, etf_daily, etf_5min, all_dates, proxy):
    if choice == "A":
        return build_picks_hybrid(
            eval_dates, orig_pool, etf_daily, etf_5min,
            all_dates, proxy, lookback=LB, warmup=LB,
        )
    # B: 全市场 Top1 (所有有效日, >=MIN_GAIN, 不 regime 过滤)
    picks = {}
    for i, day in enumerate(eval_dates):
        if i < LB:
            picks[(SIGNAL_TIME, day)] = None
            continue
        tg = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, SIGNAL_TIME)
        for gain, etf in tg:
            if gain >= MIN_GAIN:
                picks[(SIGNAL_TIME, day)] = (
                    etf["code"], gain, etf.get("name", etf["code"]))
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

    # 预跑所有 (选股, 卖点) 组合
    print("=== 核心日选股 × 卖点 Walk-Forward 验证 ===")
    print(f"窗口 {eval_dates[0]}~{eval_dates[-1]} ({len(eval_dates)}天, 有效{LB} warmup)\n")

    results = {}
    for choice in ("A", "B"):
        picks = build_picks(choice, eval_dates, orig_pool, etf_list,
                            etf_daily, etf_5min, all_dates, proxy)
        for mode in SELL_MODES:
            r = run_strategy(mode, eval_dates, all_dates, picks, etf_5min, FEE_PCT)
            results[(choice, mode)] = r["trades"] if r else []

    # In-sample 全周期汇总
    print("[In-sample 全周期]")
    print(f"  {'方案':<14}{'卖点':<12}{'笔数':>5}{'累计':>11}{'胜率':>7}{'回撤':>9}")
    print("  " + "-" * 56)
    for choice, clabel in (("A", "A(现状)"), ("B", "B(全市场)")):
        for mode in SELL_MODES:
            s = stats_of(results[(choice, mode)])
            print(f"  {clabel:<12}{SELL_LABEL[mode]:<10}{s['trades']:>5}"
                  f"{s['equity_pct']:>+10.2f}%{s['win_rate']:>6.1f}%{s['max_drawdown']:>+8.1f}%")
    print()

    # WF 切分
    core_eval = eval_dates[LB:]
    split = int(len(core_eval) * 0.6)
    split_date = core_eval[split]

    def on(trades, lo=None, hi=None):
        return [t for t in trades
                if (lo is None or t["signal_date"] >= lo)
                and (hi is None or t["signal_date"] < hi)]

    # 重点: B vs A 在两种卖点下的 OOS 增益
    print(f"[Walk-Forward 60/40]  分界 {split_date}  "
          f"(训练{len(core_eval[:split])}天 / OOS{len(core_eval[split:])}天)\n")

    wf_out = {}
    for mode in SELL_MODES:
        ta = results[("A", mode)]
        tb = results[("B", mode)]
        train_a, test_a = on(ta, hi=split_date), on(ta, lo=split_date)
        train_b, test_b = on(tb, hi=split_date), on(tb, lo=split_date)
        sta, stb = stats_of(train_a), stats_of(train_b)
        sea, seb = stats_of(test_a), stats_of(test_b)
        selected = "B" if stb["equity_pct"] > sta["equity_pct"] else "A"

        print(f"▶ 卖点 = {SELL_LABEL[mode]}:")
        print(f"    训练段: A={sta['equity_pct']:>+8.2f}%(n{sta['trades']})  "
              f"B={stb['equity_pct']:>+8.2f}%(n{stb['trades']})  "
              f"训练增益={stb['equity_pct']-sta['equity_pct']:>+7.2f}%")
        print(f"    OOS段:  A={sea['equity_pct']:>+8.2f}%(n{sea['trades']})  "
              f"B={seb['equity_pct']:>+8.2f}%(n{seb['trades']})  "
              f"★ B OOS增益={seb['equity_pct']-sea['equity_pct']:>+7.2f}%(n{seb['trades']-sea['trades']})  "
              f"→ 训练选{selected}")
        pya, pyb = per_year(test_a), per_year(test_b)
        print(f"    OOS逐年 (B增益):", end="")
        for y in sorted(pya):
            b = pyb.get(y, {"year_return_pct": 0.0})
            print(f" {y}:{b['year_return_pct']-pya[y]['year_return_pct']:>+5.1f}%", end="")
        print("\n")
        wf_out[mode] = {
            "train": {"A": sta, "B": stb,
                      "gain": round(stb["equity_pct"] - sta["equity_pct"], 2)},
            "test_oos": {"A": sea, "B": seb,
                         "gain": round(seb["equity_pct"] - sea["equity_pct"], 2)},
            "selected": selected,
        }

    # hybrid 相对 trix 的卖点增益 (B方案)
    print("[卖点增益对照, B方案]")
    for mode in SELL_MODES:
        s = stats_of(results[("B", mode)])
        print(f"  B + {SELL_LABEL[mode]}: {s['equity_pct']:>+9.2f}%  笔{s['trades']}  胜{s['win_rate']:.1f}%  MDD{s['max_drawdown']:+.1f}%")
    hybrid_vs_trix = (stats_of(results[("B", "hybrid")])["equity_pct"]
                      - stats_of(results[("B", "trix")])["equity_pct"])
    print(f"  → B 上 hybrid 卖点相对 trix 卖点 in-sample 增益 = {hybrid_vs_trix:>+.2f}%")

    result = {
        "window": f"{eval_dates[0]}~{eval_dates[-1]}",
        "split_date": split_date,
        "in_sample": {
            f"{c}_{m}": stats_of(results[(c, m)])
            for c in ("A", "B") for m in SELL_MODES
        },
        "walk_forward": wf_out,
    }
    OUT = Path.home() / ".tradingagents/cache/t0_5min/core_pool_wf.json"
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结论已落盘: {OUT}")


if __name__ == "__main__":
    main()

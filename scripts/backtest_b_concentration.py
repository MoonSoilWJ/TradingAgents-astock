#!/usr/bin/env python3
"""B(全市场Top1选股) 增益集中度检查 — 确认 B 的 OOS/全周期增益不是由少数品类外 ETF 或少数极端单笔撑起。

方法:
  - 用 build_picks_B 选股 + run_strategy("hybrid") 跑核心腿, 得到逐笔 trades(含 etf/name/return_pct/signal_date)。
  - 对每笔按其 signal_date 的 regime 判定: 该 ETF 的 trade_category 是否落在 A 策略的 ALLOWED_CATEGORIES 内。
    * 品类内  = A 的 regime 品类白名单允许 (A 本也可能选)
    * 品类外  = B 破了 A 的 regime 白名单选到的标的
  - 按 ETF 汇总累计(复利)收益、笔数、胜率; 计算 top-N ETF 对总增益的贡献份额(增益份额/笔数份额)衡量集中度。
  - 单独列出极端单笔(>10%/>5%/<-5%)与用户点名的 513120/161129。

用法:
    python scripts/backtest_b_concentration.py
    python scripts/backtest_b_concentration.py --window oos      # 仅 OOS(2024-12-19~)
    python scripts/backtest_b_concentration.py --window full     # 全周期(2022-06-15~)
    python scripts/backtest_b_concentration.py --window both     # 两者(默认)
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
from backtest_t0_today1 import FEE_PCT  # noqa: E402
from backtest_b_idle_merge import build_picks_B  # noqa: E402
from quality_pool import (  # noqa: E402
    ALLOWED_CATEGORIES,
    build_picks_hybrid,
    regime_on_date,
    trade_category,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
OUT = Path.home() / ".tradingagents/cache/t0_5min/b_concentration.json"
LB = 30
OOS_START = "2024-12-19"
FULL_START = "2022-06-15"


def standalone_eq(trades):
    eq = 1.0
    for t in trades:
        eq *= 1 + t["return_pct"] / 100
    return (eq - 1) * 100


def sum_ret(trades):
    return sum(t["return_pct"] for t in trades)


def attr_by_etf(trades):
    d = defaultdict(list)
    for t in trades:
        d[t["etf"]].append(t)
    out = []
    for code, ts in d.items():
        out.append({
            "code": code,
            "name": ts[0].get("name") or code,
            "trades": len(ts),
            "win": sum(1 for x in ts if x["return_pct"] > 0),
            "sum_r": round(sum(x["return_pct"] for x in ts), 2),
            "eq_pct": round(standalone_eq(ts), 2),
        })
    out.sort(key=lambda x: x["eq_pct"], reverse=True)
    return out


def classify(proxy, trade):
    day = trade["signal_date"]
    reg = regime_on_date(proxy, day) or {}
    mode = reg.get("mode", "中性")
    allowed = ALLOWED_CATEGORIES.get(mode, ALLOWED_CATEGORIES["中性"])
    cat = trade_category(trade.get("name"), trade["etf"])
    return mode, cat, (cat in allowed)


def window_trades(cache, start):
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    proxy = cache["proxy_klines"]
    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]

    # 选股需要 LB 预热, 从 start 前 LB 天开始 eval, 再过滤到 start 之后
    idx = all_dates.index(start) if start in all_dates else 0
    s = max(0, idx - LB)
    eval_dates = all_dates[s:]
    picks = build_picks_B(eval_dates, etf_list, etf_daily, etf_5min, LB)
    res = run_strategy("hybrid", eval_dates, all_dates, picks, etf_5min, FEE_PCT)
    trades = [t for t in (res["trades"] if res else []) if t["signal_date"] >= start]

    # A 策略(build_picks_hybrid)同窗选股, 用于逐日对照: B 选的 ETF 当天 A 是否也会选同一只
    a_picks = build_picks_hybrid(
        eval_dates, etf_list, etf_daily, etf_5min, all_dates, proxy,
        lookback=LB, warmup=LB,
    )
    a_flags = []
    for t in trades:
        ap = a_picks.get((SIGNAL_TIME, t["signal_date"]))
        a_flags.append(bool(ap and ap[0] == t["etf"]))
    return trades, proxy, a_flags


def concentration_report(trades, proxy, label, a_flags=None):
    if not trades:
        print(f"\n[{label}] 无成交")
        return {}

    total_eq = standalone_eq(trades)
    total_sum = sum_ret(trades)
    total_cnt = len(trades)
    etf_attr = attr_by_etf(trades)

    # A 对照: B 选的 ETF 当天 A 是否也会选同一只; 拆 B 增益为 同A / 异A
    same_g, diff_g = [], []
    if a_flags:
        for t, f in zip(trades, a_flags):
            (same_g if f else diff_g).append(t)
    def grp2(g):
        if not g:
            return {"trades": 0, "eq_pct": 0.0, "sum_r": 0.0}
        return {"trades": len(g), "eq_pct": round(standalone_eq(g), 2),
                "sum_r": round(sum_ret(g), 2)}
    sg, dg = grp2(same_g), grp2(diff_g)
    a_same_cnt = sg["trades"]
    a_diff_cnt = dg["trades"]

    # 品类内/外拆分
    in_cat, out_cat = [], []
    for t in trades:
        mode, cat, inc = classify(proxy, t)
        (in_cat if inc else out_cat).append(t)

    def grp(g):
        if not g:
            return {"trades": 0, "eq_pct": 0.0, "sum_r": 0.0, "win": 0.0}
        win = sum(1 for x in g if x["return_pct"] > 0) / len(g) * 100
        return {"trades": len(g), "eq_pct": round(standalone_eq(g), 2),
                "sum_r": round(sum_ret(g), 2), "win": round(win, 1)}

    in_g, out_g = grp(in_cat), grp(out_cat)
    in_share_eq = in_g["eq_pct"] / total_eq * 100 if total_eq else 0
    out_share_eq = out_g["eq_pct"] / total_eq * 100 if total_eq else 0
    in_share_cnt = in_g["trades"] / total_cnt * 100
    out_share_cnt = out_g["trades"] / total_cnt * 100

    # 集中度: top-N 对总增益(可加和口径)的份额 vs 笔数份额
    def topn_share(n):
        top = etf_attr[:n]
        codes = {x["code"] for x in top}
        tg = [t for t in trades if t["etf"] in codes]
        gshare = sum_ret(tg) / total_sum * 100 if total_sum else 0
        cshare = len(tg) / total_cnt * 100
        return {
            "n": n,
            "codes": [f"{x['code']}({x['name']})" for x in top],
            "top_eq_pct": round(standalone_eq(tg), 2),
            "top_sum_r": round(sum_ret(tg), 2),
            "top_trades": len(tg),
            "gain_share_pct": round(gshare, 1),
            "count_share_pct": round(cshare, 1),
            "ratio_gain_to_count": round(gshare / cshare, 2) if cshare else 0,
        }

    # 极端单笔
    rs = [t["return_pct"] for t in trades]
    big_up = [t for t in trades if t["return_pct"] > 10]
    mid_up = [t for t in trades if t["return_pct"] > 5]
    big_dn = [t for t in trades if t["return_pct"] < -5]
    worst = min(trades, key=lambda t: t["return_pct"])
    best = max(trades, key=lambda t: t["return_pct"])

    # 点名 ETF
    named = {}
    for code in ("513120", "161129"):
        ts = [t for t in trades if t["etf"] == code]
        if ts:
            mode, cat, inc = classify(proxy, ts[0])
            named[code] = {
                "name": ts[0].get("name"), "trades": len(ts),
                "category": cat, "in_category": inc,
                "eq_pct": round(standalone_eq(ts), 2),
            }

    print("=" * 100)
    print(f"  [{label}]  B 核心腿增益集中度")
    print("=" * 100)
    print(f"  总成交: {total_cnt} 笔  不同 ETF: {len(etf_attr)} 只  "
          f"累计(复利): {total_eq:+.2f}%  可加和增益: {total_sum:+.2f}%")
    win = sum(1 for x in trades if x["return_pct"] > 0) / total_cnt * 100
    print(f"  整体胜率: {win:.1f}%  单笔均值: {total_sum/total_cnt:+.3f}%")

    if a_flags:
        print(f"\n  ── B 选股 vs A(hybrid-A) 逐日对照 ──")
        print(f"  B 成交中 A 同日也会选同一只: {a_same_cnt} 笔 (复利 {sg['eq_pct']:+.1f}%, 增益份额 {sg['eq_pct']/total_eq*100:.1f}%)")
        print(f"  B 选了 A 不会选的标的(=B新增增益来源): {a_diff_cnt} 笔 (复利 {dg['eq_pct']:+.1f}%, 增益份额 {dg['eq_pct']/total_eq*100:.1f}%)")
        # 异A 部分的集中度
        if diff_g:
            d_attr = attr_by_etf(diff_g)
            d_top3 = d_attr[:3]
            d_codes = {x["code"] for x in d_top3}
            d_top = [t for t in diff_g if t["etf"] in d_codes]
            d_top_eq = standalone_eq(d_top)
            print(f"  → 异A 增益中 Top-3 ETF({', '.join(c['code'] for c in d_top3)}) 占 {d_top_eq/total_eq*100:.1f}% of 总增益, "
                  f"其余 {len(d_attr)-3} 只分散承接")

    print(f"\n  ── 品类内 vs 品类外 (A regime 白名单) ──")
    print(f"  {'分组':<10}{'笔数':>6}{'占比':>8}{'胜率':>7}{'复利':>10}{'增益份额':>10}")
    print(f"  {'品类内':<10}{in_g['trades']:>6}{in_share_cnt:>7.1f}%{in_g['win']:>6.1f}%{in_g['eq_pct']:>+9.1f}%{in_share_eq:>9.1f}%")
    print(f"  {'品类外':<10}{out_g['trades']:>6}{out_share_cnt:>7.1f}%{out_g['win']:>6.1f}%{out_g['eq_pct']:>+9.1f}%{out_share_eq:>9.1f}%")
    print(f"  → 品类外增益份额 {out_share_eq:.1f}% vs 笔数份额 {out_share_cnt:.1f}%  "
          f"(比值 {out_share_eq/out_share_cnt:.2f}, 越接近1越均匀)")

    print(f"\n  ── 集中度 (top-N ETF 对总增益的份额) ──")
    for n in (1, 3, 5, 10):
        s = topn_share(n)
        print(f"  Top-{n:<2} 增益份额 {s['gain_share_pct']:>6.1f}%  笔数份额 {s['count_share_pct']:>6.1f}%  "
              f"增益/笔数比 {s['ratio_gain_to_count']:>5.2f}  | 自身复利 {s['top_eq_pct']:+.1f}%")
        print(f"         {', '.join(s['codes'])}")

    print(f"\n  ── 极端单笔 ──")
    print(f"  最大单笔: {best['etf']} {best.get('name')} {best['return_pct']:+.2f}% ({best['signal_date']})")
    print(f"  最小单笔: {worst['etf']} {worst.get('name')} {worst['return_pct']:+.2f}% ({worst['signal_date']})")
    print(f"  |收益|>10%: {len(big_up)} 笔   >5%: {len(mid_up)} 笔   <-5%: {len(big_dn)} 笔")
    if big_up:
        print(f"   >10%标的: " + ", ".join(f"{t['etf']}{t['return_pct']:+.1f}%" for t in big_up[:12]))

    print(f"\n  ── 点名 ETF ──")
    if named:
        for code, v in named.items():
            print(f"  {code} {v['name']}: {v['trades']}笔  品类{'内' if v['in_category'] else '外'}({v['category']})  "
                  f"贡献复利 {v['eq_pct']:+.2f}%")
    else:
        print("  513120/161129 在本窗口未被 B 选中")

    print(f"\n  ── Top-15 ETF (按复利贡献) ──")
    print(f"  {'代码':<8}{'名称':<16}{'笔':>4}{'胜率':>7}{'可加和':>9}{'复利':>10}{'品类':>6}")
    for x in etf_attr[:15]:
        inc = ""
        ts = [t for t in trades if t["etf"] == x["code"]]
        mode, cat, inc_b = classify(proxy, ts[0])
        winx = x["win"] / x["trades"] * 100
        print(f"  {x['code']:<8}{x['name']:<16}{x['trades']:>4}{winx:>6.0f}%{x['sum_r']:>+8.1f}%{x['eq_pct']:>+9.1f}%"
              f"{'内' if inc_b else '外':>6}")

    return {
        "label": label,
        "trades": total_cnt,
        "distinct_etfs": len(etf_attr),
        "equity_pct": round(total_eq, 2),
        "sum_r_pct": round(total_sum, 2),
        "win_rate": round(win, 1),
        "category_split": {
            "in_category": {**in_g, "eq_share_pct": round(in_share_eq, 1), "count_share_pct": round(in_share_cnt, 1)},
            "out_category": {**out_g, "eq_share_pct": round(out_share_eq, 1), "count_share_pct": round(out_share_cnt, 1)},
        },
        "concentration": {n: topn_share(n) for n in (1, 3, 5, 10)},
        "extreme": {
            "best": f"{best['etf']} {best['return_pct']:+.2f}%",
            "worst": f"{worst['etf']} {worst['return_pct']:+.2f}%",
            "gt10pct": len(big_up), "gt5pct": len(mid_up), "lt_minus5pct": len(big_dn),
        },
        "named": named,
        "top15": etf_attr[:15],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(CACHE_FILE))
    ap.add_argument("--window", default="both", choices=["oos", "full", "both"])
    args = ap.parse_args()

    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    out = {}
    if args.window in ("oos", "both"):
        tr, proxy, flags = window_trades(cache, OOS_START)
        out["oos"] = concentration_report(tr, proxy, f"OOS {OOS_START}~{cache['all_dates'][-1]} (验证段)", flags)
    if args.window in ("full", "both"):
        tr, proxy, flags = window_trades(cache, FULL_START)
        out["full"] = concentration_report(tr, proxy, f"全周期 {FULL_START}~{cache['all_dates'][-1]}", flags)

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已落盘: {OUT}")


if __name__ == "__main__":
    main()

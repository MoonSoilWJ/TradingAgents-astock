#!/usr/bin/env python3
"""大一统策略 2022-2026 回测 + 自动优化 + 权益曲线图。

进攻腿 = A选股(hybrid-A, regime+滚动优质池) + 14:40确认 + TRIX卖  ← 与聚宽对齐
防守腿 = 黄金/国债等权月度再平衡(Overlay剩余资金模式)
优化目标: 弱市(2022/2023)也能盈利, 强市(2024/2025/2026)更强。

窗口: 2022-06-15 ~ 2026-07-31 (A选股无偏数据可达起点; 2022上半年无 unbiased 5min 故不含)
防守资产本地: 518880/511090/511260 (510880红利本地缺, 聚宽可补)

用法: python3 scripts/backtest_unified_2022_2026.py
"""
from __future__ import annotations
import argparse
import json
import statistics as st
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from backtest_t0_hybrid_sell import run_strategy  # noqa: E402
from backtest_recent100_live_vs_b_idle import apply_confirm  # noqa: E402
from quality_pool import build_picks_hybrid, regime_on_date  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min"
ALIGNED = CACHE / "aligned_live_4y.json"
DENSE = [CACHE / "tdx_5min_pre2024.json",
         CACHE / "tdx_5min_2y.json",
         CACHE / "tdx_5min_auto.json"]
FULL_DAILY = CACHE / "full_daily_2015_2026.json"
DENSE_START = "2022-06-15"
START = "2022-06-15"
END = "2026-07-31"
FEE = 0.0003
DEFENSE = ["518880", "511090", "511260"]   # 本地可用防守资产(红利510880缺)

WEAK = ["2022", "2023"]
STRONG = ["2024", "2025", "2026"]


# --------------------------------------------------------------------------
def load():
    cache = json.loads(ALIGNED.read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    proxy = cache["proxy_klines"]
    etf_daily = cache["etf_daily"]
    etf_5min: dict = {}
    for p in DENSE:
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))["etf_5min"]
        for c, days in d.items():
            etf_5min.setdefault(c, {}).update(days)
    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    return all_dates, proxy, etf_daily, etf_5min, etf_list


def build_attack_A(all_dates, proxy, etf_daily, etf_5min, etf_list, rank_time="14:45"):
    post = [d for d in all_dates if d >= DENSE_START]
    lb = 30
    warmup = lb
    picks = build_picks_hybrid(
        post, etf_list, etf_daily, etf_5min, all_dates, proxy,
        lookback=lb, warmup=warmup, signal_times=[rank_time],
    )
    picks, rej = apply_confirm(picks, etf_daily, etf_5min, "14:40")
    res = run_strategy("trix", post, all_dates, picks, etf_5min, FEE,
                       signal_time=rank_time)
    trades = res["trades"] if res else []
    print(f"[进攻] A选股 + {rank_time}排名 + 14:40确认 + TRIX | 成交 {len(trades)} 笔, "
          f"确认否决 {rej} 天", flush=True)
    return trades


def run_defense(full_daily, codes, all_dates, start, end):
    d = json.loads(full_daily.read_text(encoding="utf-8"))
    closes = {}
    for c in codes:
        recs = d[c]["returns"]
        closes[c] = {r["date"]: r["close"] for r in recs}
    n = len(codes)
    w = {c: 1.0 / n for c in codes}
    idx_map = {dd: i for i, dd in enumerate(all_dates)}
    daily = {}
    last_month = None
    for day in all_dates:
        if day < start or day > end:
            continue
        md = day[:7]
        r = {}
        ok = True
        for c in codes:
            s = closes[c]
            if day not in s:
                ok = False
                break
            i = idx_map[day] - 1
            while i >= 0:
                if all_dates[i] in s:
                    r[c] = s[day] / s[all_dates[i]] - 1
                    break
                i -= 1
            else:
                ok = False
                break
        if not ok:
            daily[day] = 0.0
            continue
        if last_month is None or md != last_month:
            w = {c: 1.0 / n for c in codes}
            last_month = md
        dr = sum(w[c] * r[c] for c in codes)
        if abs(dr) > 0.10:
            dr = 0.0
        daily[day] = dr
        for c in codes:
            w[c] = w[c] * (1 + r[c])
        tot = sum(w.values())
        w = {c: w[c] / tot for c in codes}
    return daily


def simulate(trades, def_daily, all_dates, start, end, attack_split,
             mode="fixed", dd_thr=None, regime_on=None):
    by_sell, by_sig = {}, {}
    for t in trades:
        by_sell.setdefault(t["sell_date"], []).append(t)
        by_sig.setdefault(t["signal_date"], []).append(t)
    open_pos = []
    eq = 1.0
    peak = 1.0
    curve = []
    for day in all_dates:
        if day < start or day > end:
            continue
        dr = def_daily.get(day, 0.0)
        # 平仓(实现收益)
        for t in by_sell.get(day, []):
            if t["etf"] in open_pos:
                k = len(open_pos)
                open_pos.remove(t["etf"])
                ret = t["return_pct"] / 100.0
                eq *= (1 + (attack_split / k) * ret)
        k = len(open_pos)
        # 有效攻击仓位(dd模式: 深回撤时暂停开新仓)
        dd = (eq / peak - 1) * 100
        if mode == "fixed":
            eff = attack_split
            can_open = True
        elif mode == "dd":
            eff = attack_split
            can_open = (dd > dd_thr)   # 深回撤(dd很负)时不新开 → 留防守
        elif mode == "regime":
            reg = regime_on(day) if regime_on else "中性"
            eff = attack_split if reg == "趋势" else min(attack_split, 0.4)
            can_open = True
        else:
            eff = attack_split
            can_open = True
        if k == 0:
            eq *= (1 + dr)
        else:
            eq = eq * (1 - eff) * (1 + dr) + eq * eff
        peak = max(peak, eq)
        # 新开仓
        for t in by_sig.get(day, []):
            if t["etf"] not in open_pos and can_open:
                open_pos.append(t["etf"])
        curve.append((day, eq))
    return curve


def metrics(curve):
    if not curve:
        return 0, {}, 0, 0
    total = (curve[-1][1] / curve[0][1] - 1) * 100
    eqmap = {d: e for d, e in curve}
    years = sorted({d[:4] for d, _ in curve})
    yr = {}
    for y in years:
        days = [d for d, _ in curve if d[:4] == y]
        if days:
            yr[y] = (eqmap[days[-1]] / eqmap[days[0]] - 1) * 100
    peak = curve[0][1]
    mdd = 0.0
    for _, e in curve:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    mdd *= 100
    rets = [curve[i + 1][1] / curve[i][1] - 1 for i in range(len(curve) - 1)]
    mu, sd = (st.mean(rets), st.pstdev(rets)) if len(rets) > 1 else (0, 0)
    sharpe = (mu / sd * 244 ** 0.5) if sd > 0 else 0.0
    return total, yr, mdd, sharpe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plot", action="store_true", help="生成权益曲线 PNG")
    ap.add_argument("--rank-time", default="14:45",
                    help="进攻排名时点(对齐聚宽用 14:40; 默认14:45)")
    args = ap.parse_args()

    all_dates, proxy, etf_daily, etf_5min, etf_list = load()
    trades = build_attack_A(all_dates, proxy, etf_daily, etf_5min, etf_list,
                            rank_time=args.rank_time)
    def_daily = run_defense(FULL_DAILY, DEFENSE, all_dates, START, END)
    print(f"[防守] 等权 {DEFENSE} 月度再平衡\n", flush=True)

    # ---- 网格搜索 ----
    configs = []
    for sp in [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
        configs.append(("fixed", sp, None))
    for thr in [-8, -12, -15]:
        configs.append(("dd", 1.0, thr))
    configs.append(("regime", 1.0, None))

    results = []
    for mode, sp, thr in configs:
        reg_fn = (lambda day: regime_on_date(proxy, day)) if mode == "regime" else None
        curve = simulate(trades, def_daily, all_dates, START, END, sp,
                         mode=mode, dd_thr=thr, regime_on=reg_fn)
        total, yr, mdd, sharpe = metrics(curve)
        weak_min = min(yr.get(w, 0.0) for w in WEAK)
        strong_mean = sum(yr.get(s, 0.0) for s in STRONG) / len(STRONG)
        # 优化目标: 抬升弱市(权重高) + 适度奖励强市 - 回撤惩罚
        score = weak_min + 0.25 * strong_mean - max(0, -mdd) * 0.02
        results.append({
            "mode": mode, "sp": sp, "thr": thr, "total": total, "yr": yr,
            "mdd": mdd, "sharpe": sharpe, "weak_min": weak_min,
            "strong_mean": strong_mean, "score": score, "curve": curve,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    print("配置网格 (按优化得分排序):")
    print(f"{'模式':<8}{'split':>6}{'thr':>6}{'总%':>10}{'弱市min':>9}"
          f"{'强市均':>9}{'MDD%':>8}{'夏普':>7}")
    print("-" * 64)
    for r in results:
        print(f"{r['mode']:<8}{r['sp']:>6.2f}{str(r['thr']):>6}{r['total']:>10.1f}"
              f"{r['weak_min']:>9.1f}{r['strong_mean']:>9.1f}"
              f"{r['mdd']:>8.1f}{r['sharpe']:>7.2f}")
    print()
    best = results[0]
    print(f"★ 最优配置: mode={best['mode']} split={best['sp']} "
          f"thr={best['thr']}")
    print(f"  总收益 {best['total']:.1f}% | 弱市min {best['weak_min']:.1f}% "
          f"| 强市均 {best['strong_mean']:.1f}% | MDD {best['mdd']:.1f}% "
          f"| 夏普 {best['sharpe']:.2f}")
    print("  逐年:", "  ".join(f"{y}:{best['yr'].get(y,0):+.1f}%" for y in
          sorted(best['yr'])))

    # 基准对照: 纯进攻 / 纯防守
    atk_only = simulate(trades, def_daily, all_dates, START, END, 1.0, "fixed")
    def_only = simulate(trades, def_daily, all_dates, START, END, 0.0, "fixed")
    ta, _, ma, sa = metrics(atk_only)
    td, _, md, sd = metrics(def_only)
    print(f"\n[对照] 纯进攻(split=1): 总 {ta:.1f}% MDD {ma:.1f}% 夏普 {sa:.2f}")
    print(f"[对照] 纯防守(split=0): 总 {td:.1f}% MDD {md:.1f}% 夏普 {sd:.2f}")

    if args.plot:
        out = HERE.parent / "unified_2022_2026_equity.png"
        plot(all_dates, best, atk_only, def_only, out)


def plot(all_dates, best, atk_only, def_only, out):
    def norm(curve):
        return [(d, e / curve[0][1]) for d, e in curve]
    b = norm(best["curve"])
    a = norm(atk_only)
    d = norm(def_only)
    fig, ax = plt.subplots(2, 1, figsize=(13, 9),
                           gridspec_kw={"height_ratios": [3, 1]})
    ax[0].plot([x for x, _ in b], [y for _, y in b], label="Unified (best)", lw=2, color="black")
    ax[0].plot([x for x, _ in a], [y for _, y in a], label="Pure Attack (A)", lw=1.2, color="red", alpha=0.7)
    ax[0].plot([x for x, _ in d], [y for _, y in d], label="Pure Defense", lw=1.2, color="green", alpha=0.7)
    ax[0].set_title(f"Unified Strategy Equity Curve 2022-06~2026-07  "
                    f"(best: {best['mode']} split={best['sp']} | "
                    f"tot {best['total']:.0f}% MDD {best['mdd']:.0f}% Sharpe {best['sharpe']:.2f})")
    ax[0].set_ylabel("Net value (start=1)")
    ax[0].legend(loc="upper left")
    ax[0].grid(alpha=0.3)

    yrs = sorted(best["yr"].keys())
    vals = [best["yr"][y] for y in yrs]
    colors = ["#2ca02c" if v >= 0 else "#d62728" for v in vals]
    ax[1].bar(yrs, vals, color=colors)
    ax[1].axhline(0, color="black", lw=0.8)
    ax[1].set_title("Yearly Return % (green=profit red=loss)")
    ax[1].set_ylabel("Year Return %")
    ax[1].grid(alpha=0.3, axis="y")
    for i, v in enumerate(vals):
        ax[1].text(i, v + (3 if v >= 0 else -6), f"{v:+.0f}", ha="center", fontsize=8)

    fig.tight_layout()
    fig.savefig(out, dpi=130)
    print(f"\n曲线图已保存: {out}")


if __name__ == "__main__":
    main()

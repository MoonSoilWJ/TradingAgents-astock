#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证「盘中实时量比」能否替代前视量比 —— 决定半路板是真 edge 还是纯泄漏。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
问题
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  严格版回测中, 半路板全部利润来自 量比 = 当日【全天】成交额 / 昨日额。
  这个量在盘中(买入那一刻)不可知 → 泄漏。去掉它最好组合 -83.7%(样本外)。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
本脚本的替代方案(无前视)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  在时点 t(如 10:30, 已交易 60 分钟):
      盘中量比 = (截至 t 的累计成交额 / 已交易分钟 × 240) / 昨日全天成交额
  即"按当前节奏外推的全天成交额 ÷ 昨天全天" —— 这个数在 t 时刻真实可知。

  触发: 当前涨幅 ∈ [thr, 9.8%)  且  盘中量比 ≥ vr_min
  买入: 该 30 分钟 K 的收盘价
  卖出: 次日收盘

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
对照三线
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  A 盘中量比(本方案, 无前视)  ← 能否为正?
  B 全天量比(前视, 上限参考)  ← 之前 +14659% 的来源
  C 不限量比                  ← 之前 -83.7% 的基线

用法: python3 scripts/backtest_banlu_intraday.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

MIN30 = Path.home() / ".tradingagents" / "youzi" / "min30.pkl"
COST = 0.002
IDX_LABEL = {1: "10:30", 2: "11:00", 3: "11:30",
             4: "13:30", 5: "14:00", 6: "14:30"}


def perf(port: pd.Series) -> tuple[float, float, float]:
    if len(port) < 20:
        return (np.nan,) * 3
    eq = (1 + port).cumprod()
    tot = (eq.iloc[-1] - 1) * 100
    yrs = max((port.index[-1] - port.index[0]).days / 365.25, 0.1)
    ann = ((1 + tot / 100) ** (1 / yrs) - 1) * 100
    return tot, ann, port.mean() * 100


def main() -> int:
    df = pd.read_pickle(MIN30)
    df["date"] = df["datetime"].dt.date
    df = df.sort_values(["code", "datetime"])
    df["idx"] = df.groupby(["code", "date"]).cumcount()
    df["cum_amt"] = df.groupby(["code", "date"])["amount"].cumsum()

    daily = (df.groupby(["code", "date"])
             .agg(close=("close", "last"), amt=("amount", "sum"))
             .reset_index())
    daily["prev_close"] = daily.groupby("code")["close"].shift(1)
    daily["prev_amt"] = daily.groupby("code")["amt"].shift(1)
    daily["next_close"] = daily.groupby("code")["close"].shift(-1)

    d = df.merge(daily[["code", "date", "prev_close", "prev_amt", "next_close", "amt"]],
                 on=["code", "date"], how="left")
    d = d.dropna(subset=["prev_close", "prev_amt", "next_close"])
    n = len(d)
    print("=" * 92)
    print("  盘中实时量比验证 (无前视) — 决定半路板是真 edge 还是纯泄漏")
    print(f"  {d['date'].min()} ~ {d['date'].max()} | {d['code'].nunique()} 只 | "
          f"{n:,} 根 | 成本 {COST*100:.1f}%")
    print("=" * 92)

    # 全天量比(前视) & 盘中量比(无前视)
    d["vr_day"] = d["amt"] / d["prev_amt"]                       # B: 前视
    d["vr_now"] = (d["cum_amt"] / ((d["idx"] + 1) * 30) * 240) / d["prev_amt"]
    d["pct"] = d["close"] / d["prev_close"] - 1
    d["ret"] = d["next_close"] / d["close"] - 1                  # 次日收盘卖

    uniq = d.drop_duplicates(["code", "date", "idx"])

    def run(vr_col, vr_min, thr=0.04, idx=None):
        s = uniq
        if idx is not None:
            s = s[s["idx"] == idx]
        sig = (s["pct"] >= thr) & (s["pct"] < 0.098) & (s[vr_col] >= vr_min)
        g = s[sig].groupby("date")["ret"].mean() - COST
        return g.dropna()

    print(f"\n  【A】盘中量比(无前视, 可在该时点真实算出) — 阈值 4%")
    print(f"  {'时点':<8}" + "".join(f"{f'量比≥{v}':>13}" for v in (0, 1.5, 2.0, 3.0)))
    for idx, lab in IDX_LABEL.items():
        cells = []
        for v in (0, 1.5, 2.0, 3.0):
            p = run("vr_now", v, idx=idx)
            t, a, dr = perf(p)
            cells.append(f"{a:>+11.1f}%" if not np.isnan(a) else f"{'—':>12}")
        print(f"  {lab:<8}" + "".join(f"{c:>13}" for c in cells))
    print("  (表内为年化%, 已扣成本)")

    print(f"\n  【B】全天量比(⚠前视, 盘中不可知) — 作为上限对照, 阈值 4%")
    for idx, lab in IDX_LABEL.items():
        p = run("vr_day", 2.0, idx=idx)
        t, a, dr = perf(p)
        print(f"  {lab:<8}量比≥2 → 年化 {a:>+12.1f}%  累计 {t:>+12.1f}%  "
              f"日均 {dr:>+6.3f}%")

    print(f"\n  【C】不限量比(基线) — 阈值 4%")
    for idx, lab in IDX_LABEL.items():
        p = run("vr_now", 0, idx=idx)
        t, a, dr = perf(p)
        print(f"  {lab:<8}→ 年化 {a:>+12.1f}%  累计 {t:>+12.1f}%  日均 {dr:>+6.3f}%")

    print(f"\n  【D】触发阈值 × 盘中量比(10:30 时点) 年化%")
    print(f"  {'阈值':<8}" + "".join(f"{f'量比≥{v}':>13}" for v in (0, 1.5, 2.0, 3.0)))
    for thr in (0.03, 0.04, 0.05, 0.06, 0.08):
        cells = []
        for v in (0, 1.5, 2.0, 3.0):
            p = run("vr_now", v, thr=thr, idx=1)
            t, a, dr = perf(p)
            cells.append(f"{a:>+11.1f}%" if not np.isnan(a) else f"{'—':>12}")
        print(f"  {thr*100:>3.0f}%{'':<4}" + "".join(f"{c:>13}" for c in cells))

    # 【E】8% 阈值的稳健性: 是真 edge 还是极端值假象?
    print(f"\n  【E】高阈值(8%)稳健性诊断 — 10:30 时点")
    dc = df.groupby(["code", "date"]).agg(dclose=("close", "last")).reset_index()
    u = uniq.merge(dc, on=["code", "date"], how="left")
    u["day_close_pct"] = u["dclose"] / u["prev_close"] - 1
    for thr in (0.07, 0.08, 0.09):
        for v in (0.0, 2.0):
            s = u[(u["idx"] == 1) & (u["pct"] >= thr) & (u["pct"] < 0.098) &
                  (u["vr_now"] >= v)]
            if len(s) < 50:
                continue
            g = s.groupby("date")["ret"].mean() - COST
            t, a, dr = perf(g)
            seal = (s["day_close_pct"] >= 0.098).mean() * 100
            # 去掉最好的 5 天后
            g2 = g.sort_values().iloc[:-5] if len(g) > 5 else g
            t2, a2, _ = perf(g2.sort_index())
            print(f"  阈值{thr*100:.0f}% 量比≥{v}: 笔 {len(s):>6,} | 日均 {len(s)/max(g.size,1):>5.1f}只 "
                  f"| 年化 {a:>+8.1f}% | 去掉最好5天 {a2:>+8.1f}% "
                  f"| 当日最终封板率 {seal:>5.1f}% | 单笔中位 {s['ret'].median()*100:>+6.2f}% "
                  f"| 胜率 {(g>COST*-1).mean()*100:.1f}%")
    # 【F】分年 + 样本内外: 确认不是某段行情的运气
    print(f"\n  【F】分年 & 样本内外 (10:30, 量比不限)")
    SPLIT = pd.Timestamp("2025-07-01")
    for thr in (0.08, 0.09):
        s = u[(u["idx"] == 1) & (u["pct"] >= thr) & (u["pct"] < 0.098)]
        g = (s.groupby("date")["ret"].mean() - COST).dropna()
        gi = pd.to_datetime(pd.Index(g.index))
        line = []
        for y in sorted(set(gi.year)):
            gy = g[gi.year == y]
            if len(gy) < 10:
                continue
            t, a, dr = perf(gy)
            line.append(f"{y}:{t:+.0f}%")
        isp = perf(g[gi < SPLIT])
        osp = perf(g[gi >= SPLIT])
        print(f"  阈值{thr*100:.0f}%  分年[{' '.join(line)}]")
        print(f"          样本内(24.07~25.06) 年化 {isp[1]:>+8.1f}% | "
              f"样本外(25.07~26.09) 年化 {osp[1]:>+8.1f}% | "
              f"样本外累计 {osp[0]:>+8.1f}%")
    # 【G】基准对照: 排除"只是放大了市场 beta"
    print(f"\n  【G】同期基准对照 — 策略收益是否只是市场上涨?")
    d2 = daily.sort_values(["code", "date"]).copy()
    d2["r"] = d2.groupby("code")["close"].pct_change()
    base = d2.groupby("date")["r"].mean().dropna()
    gi_base = pd.to_datetime(pd.Index(base.index))
    bt, ba, _ = perf(base)
    print(f"  基准(等权持有池内 {daily['code'].nunique()} 只, 全期): "
          f"累计 {bt:+.1f}%  年化 {ba:+.1f}%")
    for thr in (0.08, 0.09):
        s = u[(u["idx"] == 1) & (u["pct"] >= thr) & (u["pct"] < 0.098)]
        g = (s.groupby("date")["ret"].mean() - COST).dropna()
        gt, ga, _ = perf(g)
        # 只在"有信号日"比较, 剔除空仓日差异
        bd = pd.to_datetime(pd.Index(g.index))
        bb = base[gi_base.isin(bd)]
        bbt = ((1 + bb).prod() - 1) * 100
        # 等权组合: 有信号日持策略, 无信号日空仓
        full = g.reindex(base.index).fillna(0.0)
        ft, fa, _ = perf(full)
        print(f"  阈值{thr*100:.0f}%: 策略(仅信号日) 累计 {gt:+.1f}% | "
              f"同样这些天基准 {bbt:+.1f}% | 超额 {gt-bbt:+.1f}pp")
        print(f"          含空仓日的完整账户: 累计 {ft:+.1f}% 年化 {fa:+.1f}% "
              f"(覆盖 {len(g)}/{len(base)} 天 = {len(g)/len(base)*100:.0f}%)")
    # 【H】滑点敏感度: 9% 位置追涨, 实际成交价可能更差
    print(f"\n  【H】滑点敏感度 (10:30, 量比不限) — 越不依赖低滑点越可信")
    print(f"  {'单边成本':<10}" + "".join(f"{f'{t*100:.0f}%档':>14}" for t in (0.07, 0.08, 0.09)))
    for c in (0.001, 0.002, 0.005, 0.010):
        cells = []
        for thr in (0.07, 0.08, 0.09):
            s = u[(u["idx"] == 1) & (u["pct"] >= thr) & (u["pct"] < 0.098)]
            g = (s.groupby("date")["ret"].mean() - c).dropna()
            t, a, dr = perf(g)
            cells.append(f"{a:>+12.1f}%" if not np.isnan(a) else f"{'—':>13}")
        print(f"  {c*100:>6.1f}%{'':<3}" + "".join(f"{x:>14}" for x in cells))
    print("  (表内年化%; 8~9% 买入正在冲板的票, 实际滑点可能远大于 0.1%)")

    print(f"\n  判定: 若【A】最好年化仍为负 → 前视量比即全部利润(低阈值档已证伪)")
    print(f"        若【F】分年均为正 +【G】超额为正 → 8~9% 档是真 edge, 值得工程化")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

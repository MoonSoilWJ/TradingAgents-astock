#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""半路板「优化搜索 + 样本外验证」— 回答"参数没调好 vs 根本没 edge"。

方法(唯一能区分"真优化"和"过拟合"的做法):
  · 样本内 2022-04~2024-12: 网格搜索 480 种参数组合, 挑最优
  · 样本外 2025-01~2026-09: 用样本内选出的参数检验
  若样本外的排名与样本内无关(甚至为负) → 说明样本内的"好"是噪音拟合, 不是 edge

搜索维度(对应"选股/评分/时机"三类质疑):
  选股: 触发阈值 / 流通市值区间 / 连板数上限 / 距60日高点上限
  时机: 量比下限(⚠前视) / 离场口径(次日收盘 vs 次日开盘)

另含【评分分档】: 按实盘评分体系的分数分档统计, 检验"评分体系"是否有区分度。

用法: python3 scripts/search_banlu_opt.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

MB_DAILY = Path.home() / ".tradingagents" / "youzi" / "mb_daily.pkl"
MB_FLOAT = Path.home() / ".tradingagents" / "youzi" / "mb_float.json"
POOL_N = 600
IS_END = pd.Timestamp("2024-12-31")     # 样本内截止
COST = 0.002


def load():
    df = pd.read_pickle(MB_DAILY)
    piv = lambda v: df.pivot(index="date", columns="code", values=v).sort_index()
    d = {"close": piv("close"), "high": piv("high"),
         "open": piv("open"), "amount": piv("amount")}
    with open(MB_FLOAT) as f:
        fs = pd.Series({k: float(v) for k, v in json.load(f).items()})
    return d, fs.reindex(d["close"].columns)


def streak_matrix(lim: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(0.0, index=lim.index, columns=lim.columns)
    for c in lim.columns:
        s = lim[c].fillna(False).astype(bool)
        out[c] = s.groupby((~s).cumsum()).cumsum()
    return out


def perf(port: pd.Series) -> tuple[float, float, float, float]:
    """→ (累计%, 年化%, 日均%, 胜率%)"""
    if len(port) < 20:
        return (np.nan,) * 4
    eq = (1 + port).cumprod()
    tot = (eq.iloc[-1] - 1) * 100
    yrs = max((port.index[-1] - port.index[0]).days / 365.25, 0.1)
    ann = ((1 + tot / 100) ** (1 / yrs) - 1) * 100
    return tot, ann, port.mean() * 100, (port > 0).mean() * 100


def main() -> int:
    d, fs = load()
    close, high, openp, amount = d["close"], d["high"], d["open"], d["amount"]
    sl = slice(pd.Timestamp("2022-04-01"), None)
    close, high, openp, amount = (close.loc[sl], high.loc[sl],
                                  openp.loc[sl], amount.loc[sl])
    prev = close.shift(1)

    avg_amt = amount.rolling(20).mean().shift(1)
    in_pool = avg_amt.rank(axis=1, ascending=False) <= POOL_N
    lim = (close / prev - 1) >= 0.098
    st = streak_matrix(lim).shift(1)
    h60 = high.rolling(60).max().shift(1)
    vr = amount / amount.shift(1)                 # ⚠前视
    nxt_c, nxt_o = close.shift(-1), openp.shift(-1)

    is_mask = pd.Series(close.index <= IS_END, index=close.index)
    os_mask = pd.Series(close.index > IS_END, index=close.index)

    THRS = [0.04, 0.05, 0.06, 0.07, 0.08]
    MVS = [("全部", 0, 1e9), ("<50亿", 0, 50), ("50~200亿", 50, 200),
           ("200~500亿", 200, 500)]
    STS = [("全部", 99), ("≤首板", 0), ("≤2板", 1), ("≤3板", 2), ("≤4板", 3)]
    DDS = [("全部", 200), ("≤10%", 10), ("≤20%", 20)]
    VRS = [("不限", 0.0), ("≥1.5⚠", 1.5), ("≥2⚠", 2.0)]
    EXITS = [("次日收盘", nxt_c), ("次日开盘", nxt_o)]

    print("=" * 100)
    print("  半路板 · 网格搜索(样本内 2022-04~2024-12) + 样本外验证(2025-01~2026-09)")
    print("=" * 100)
    print(f"  组合数 {len(THRS)*len(MVS)*len(STS)*len(DDS)*len(VRS)*len(EXITS)} | "
          f"成本 {COST*100:.1f}%/天")

    recs = []
    for thr in THRS:
        buy = np.maximum(openp, prev * (1 + thr))
        touch = (high / prev - 1) >= thr
        mv = fs * buy / 1e8
        dd = (h60 - buy) / h60 * 100
        for mvn, lo, hi in MVS:
            mv_ok = (mv >= lo) & (mv <= hi)
            for stn, smax in STS:
                st_ok = st <= smax
                for ddn, dmax in DDS:
                    dd_ok = dd <= dmax
                    for vrn, vmin in VRS:
                        vr_ok = (vr >= vmin) if vmin > 0 else pd.DataFrame(
                            True, index=close.index, columns=close.columns)
                        sig = touch & in_pool & mv_ok & st_ok & dd_ok & vr_ok
                        for exn, nx in EXITS:
                            r = (nx / buy - 1).where(sig).mean(axis=1) - COST
                            r = r.dropna()
                            if len(r) < 60:
                                continue
                            ti, ai, di, wi = perf(r[is_mask.reindex(r.index)])
                            to, ao, do_, wo = perf(r[os_mask.reindex(r.index)])
                            recs.append({
                                "thr": thr, "mv": mvn, "streak": stn, "dd": ddn,
                                "vr": vrn, "exit": exn,
                                "is_tot": ti, "is_ann": ai, "is_wr": wi,
                                "os_tot": to, "os_ann": ao, "os_wr": wo,
                                "npd": sig.sum(axis=1).mean(),
                            })
        print(f"  阈值 {thr*100:.0f}% 完成, 累计 {len(recs)} 组合", flush=True)

    R = pd.DataFrame(recs)
    R.to_csv("/tmp/banlu_search.csv", index=False)
    print(f"\n  有效组合 {len(R)}")

    # 样本内 top10 → 看样本外
    print(f"\n【一】样本内(2022-2024)最优 10 组合 → 它们在样本外(2025-2026)的表现")
    print(f"  {'阈值':>5}{'市值':>9}{'连板':>7}{'距高':>7}{'量比':>7}{'离场':>9}"
          f"{'样本内累计':>12}{'样本外累计':>12}{'样本外年化':>11}{'样本外胜率':>10}")
    top = R.sort_values("is_tot", ascending=False).head(10)
    for _, r in top.iterrows():
        print(f"  {r['thr']*100:>4.0f}%{r['mv']:>9}{r['streak']:>7}{r['dd']:>7}"
              f"{r['vr']:>7}{r['exit']:>9}{r['is_tot']:>+11.1f}%{r['os_tot']:>+11.1f}%"
              f"{r['os_ann']:>+10.1f}%{r['os_wr']:>9.1f}%")

    # 相关性: 样本内好 → 样本外也好吗?
    sub = R.dropna(subset=["is_tot", "os_tot"])
    corr = sub["is_tot"].corr(sub["os_tot"], method="spearman")
    print(f"\n【二】样本内 vs 样本外 排名相关性 (Spearman): **{corr:+.3f}**")
    print("      (接近 0 或为负 = 样本内的最优无法预测样本外 = 过拟合)")

    # 样本外最好的组合(对照: 如果它样本内很差, 也是运气)
    print(f"\n【三】样本外(2025-2026)最优 5 组合 → 回看它们样本内的表现")
    top_o = R.sort_values("os_tot", ascending=False).head(5)
    for _, r in top_o.iterrows():
        print(f"  {r['thr']*100:>4.0f}%{r['mv']:>9}{r['streak']:>7}{r['dd']:>7}"
              f"{r['vr']:>7}{r['exit']:>9}  样本外{r['os_tot']:>+8.1f}%  "
              f"样本内{r['is_tot']:>+8.1f}%")

    # 全样本最优(如果有人这么做 = 偷看未来)
    print("\n【四】全样本(2022-2026)最优 5 组合 —— 事后诸葛亮, 实盘不可实现")
    R["all_tot"] = (1 + R["is_tot"] / 100) * (1 + R["os_tot"] / 100) * 100 - 100
    for _, r in R.sort_values("all_tot", ascending=False).head(5).iterrows():
        print(f"  {r['thr']*100:>4.0f}%{r['mv']:>9}{r['streak']:>7}{r['dd']:>7}"
              f"{r['vr']:>7}{r['exit']:>9}  全样本{r['all_tot']:>+10.1f}%  "
              f"样本外{r['os_tot']:>+8.1f}%")

    # 评分分档: 实盘评分体系有区分度吗?
    print(f"\n【五】评分分档检验 (阈值6%, 次日收盘卖; 评分=实盘五维体系)")
    buy = np.maximum(openp, prev * 1.06)
    ret = (nxt_c / buy - 1)
    touch = (high / prev - 1) >= 0.06
    sel = touch & in_pool
    mv = fs * buy / 1e8
    score = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    score += st.apply(lambda s: s.map({0: 30, 1: 32, 2: 24, 3: 8}).fillna(0))
    score += np.where((mv >= 30) & (mv <= 200), 25,
                      np.where((mv > 200) & (mv <= 500), 15, 0))
    dd = (h60 - buy) / h60 * 100
    score += np.where(dd <= 5, 15, np.where(dd <= 15, 10, np.where(dd <= 25, 4, 0)))
    score += np.where(vr >= 5, 10, np.where(vr >= 3, 8, 5))
    tb = pd.DataFrame({"sc": score.where(sel).stack(),
                       "ret": ret.where(sel).stack()}).dropna()
    tb["band"] = pd.cut(tb["sc"], bins=[-1, 40, 55, 70, 85, 200],
                        labels=["C <40", "B 40-55", "A 55-70", "S 70-85", "S+ >85"])
    print(f"  {'评分档':<10}{'占比':>8}{'毛收益':>10}{'胜率':>8}{'笔数':>10}")
    for b in ["C <40", "B 40-55", "A 55-70", "S 70-85", "S+ >85"]:
        s = tb[tb["band"] == b]
        if len(s) == 0:
            continue
        print(f"  {b:<10}{len(s)/len(tb)*100:>7.1f}%{s['ret'].mean()*100:>+9.3f}%"
              f"{(s['ret'] > 0).mean()*100:>7.1f}%{len(s):>10,}")
    print(f"  (毛收益需 > {COST*100:.1f}% 才覆盖成本)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

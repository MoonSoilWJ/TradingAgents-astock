#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最终配置逐日盯市回测: 双时点(10:30+11:00 首触) + 封板持有·断板卖。

口径:
  候选   首次触及 9%(未封板) 的 30分钟K ∈ {10:30, 11:00}, 买入价 = 该K收盘
  卖出   断板日(收盘涨幅<9.8%)的【次日】收盘卖(A股T+1, 买入日断板→次日卖),
         最长持有 5 个交易日
  组合   每日收益 = 当日全部活跃持仓的当日涨幅等权均值, 逐日复利(空仓日 0)

对照: 10:30 单时点 + 固定次日卖 (此前口径, 年化 +225.5%)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

MIN30 = Path.home() / ".tradingagents" / "youzi" / "min30.pkl"
COST = 0.002          # 单边
MAX_HOLD = 5
SEALED = 0.098


def main() -> int:
    df = pd.read_pickle(MIN30)
    df["date"] = df["datetime"].dt.date
    df = df.sort_values(["code", "datetime"])
    df["idx"] = df.groupby(["code", "date"]).cumcount()

    daily = (df.groupby(["code", "date"])
             .agg(dclose=("close", "last")).reset_index())
    daily["prev"] = daily.groupby("code")["dclose"].shift(1)
    daily["dret"] = daily["dclose"] / daily["prev"] - 1
    daily = daily.dropna(subset=["prev"])

    # ── 候选: 首次触及 9% 未封板, 时点 ∈ {10:30, 11:00} ──
    b = df[["code", "date", "idx", "close"]].copy()
    b["prev_c"] = b.groupby("code")["close"].shift(1)   # 不精确, 仅初筛占位
    d = b.merge(daily[["code", "date", "prev"]], on=["code", "date"])
    d = d.dropna(subset=["prev"])
    d["pct"] = d["close"] / d["prev"] - 1
    hit = d[(d["pct"] >= SEALED - 0.008) & (d["pct"] < SEALED)]
    first = hit.loc[hit.groupby(["code", "date"])["idx"].idxmin()]
    cand = first[first["idx"].isin([1, 2])].copy()
    cand = cand.rename(columns={"close": "buy"})

    # 动态池(无前视): 截至 T-1 的 20日均额前 600 — 用全市场数据每日重算,
    # 消除"用今天的活跃名单回测历史"的前视偏差
    damt = df.groupby(["code", "date"])["amount"].sum().reset_index()
    dpiv = damt.pivot(index="date", columns="code", values="amount").sort_index()
    in_pool = (dpiv.rolling(20).mean().shift(1)
               .rank(axis=1, ascending=False) <= 600)
    mask = pd.Series([bool(in_pool.at[dt, c])
                      if (dt in in_pool.index and c in in_pool.columns) else False
                      for dt, c in zip(cand["date"], cand["code"])],
                     index=cand.index)
    n0 = len(cand)
    cand = cand[mask]
    print(f"动态池过滤: {n0} → {len(cand)} 笔 (截至T-1成交额前600)")

    # ── 每笔的持有区间与各日收益 ──
    seqs = {c: g.sort_values("date").reset_index(drop=True)
            for c, g in daily.groupby("code")}
    day_rows = []            # (date, per-trade return)
    trade_rows = []          # (code, buy_date, hold_days, total_ret)
    for _, r in cand.iterrows():
        s = seqs.get(r["code"])
        if s is None:
            continue
        idxs = s.index[s["date"] == r["date"]]
        if len(idxs) == 0:
            continue
        i0 = idxs[0]
        # 断板日: 买入日起第一个 dret < 9.8% (含买入日当天)
        i_break = None
        for k in range(0, MAX_HOLD + 1):
            j = i0 + k
            if j >= len(s):
                break
            if s["dret"].iloc[j] < SEALED:
                i_break = j
                break
        # 卖出规则(T+1 约束):
        #   买入日当天就断板 → 只能次日卖 (i0+1)
        #   已持有≥1天后断板 → 断板日当天尾盘卖 (i_break)
        #   从未断板         → 买入日+MAX_HOLD 强制清仓
        if i_break is None:
            i_sell = i0 + MAX_HOLD
        elif i_break == i0:
            i_sell = i0 + 1
        else:
            i_sell = i_break
        i_sell = min(i_sell, i0 + MAX_HOLD, len(s) - 1)
        i_sell = max(i_sell, i0 + 1)                 # 至少持到次日
        # 各日收益
        for j in range(i0, i_sell + 1):
            if j >= len(s):
                break
            base = r["buy"] if j == i0 else s["dclose"].iloc[j - 1]
            day_rows.append((s["date"].iloc[j],
                             s["dclose"].iloc[j] / base - 1))
        j_sell = min(i_sell, len(s) - 1)
        trade_rows.append((r["code"], r["date"], j_sell - i0,
                           s["dclose"].iloc[j_sell] / r["buy"] - 1 - COST))

    T = pd.DataFrame(day_rows, columns=["date", "r"])
    port = T.groupby("date")["r"].mean() - COST      # 日组合收益(含成本)
    port = port.sort_index()
    eq = (1 + port).cumprod()
    tot = (eq.iloc[-1] - 1) * 100
    yrs = (pd.Timestamp(port.index[-1]) - pd.Timestamp(port.index[0])).days / 365.25
    ann = ((1 + tot / 100) ** (1 / yrs) - 1) * 100
    mdd = ((eq - eq.cummax()) / eq.cummax()).min() * 100

    TR = pd.DataFrame(trade_rows, columns=["code", "buy_date", "hold", "ret"])
    gi = pd.to_datetime(pd.Index(port.index))
    print("\n" + "=" * 88)
    print("  最终配置逐日盯市: 双时点(10:30+11:00 首触) + 封板持有·断板卖")
    print(f"  {port.index[0]} ~ {port.index[-1]} | {len(cand)} 笔 | "
          f"覆盖 {len(port)}/{gi.nunique()} 天")
    print("=" * 88)
    print(f"  笔均(净) {TR['ret'].mean()*100:+.2f}% | 胜率 {(TR['ret']>0).mean()*100:.1f}%"
          f" | 平均持有 {TR['hold'].mean():.2f} 天")
    print(f"  组合: 累计 {tot:+.0f}% | 年化 {ann:+.1f}% | 回撤 {mdd:.1f}%")
    print(f"  对照(10:30单时点+次日卖): 年化 +225.5% 累计 +985% 回撤 -36.9%(近似口径)")
    print("\n  分年:")
    buy_years = pd.to_datetime(pd.Index(TR["buy_date"])).year
    for y in sorted(set(gi.year)):
        py = port[gi.year == y]
        e = (1 + py).cumprod()
        ty = TR[buy_years == y]["ret"]
        print(f"    {y}: 累计 {(e.iloc[-1]-1)*100:+.0f}% | 笔均 {ty.mean()*100:+.2f}%"
              f" | 胜率 {(ty>0).mean()*100:.1f}%")
    hold_d = TR["hold"].value_counts().sort_index().to_dict()
    print(f"\n  持有天数分布: {hold_d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

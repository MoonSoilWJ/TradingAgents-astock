#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""龙头接力回测 — 只接龙头, 不接杂毛。区分"接所有涨停"与"接最高板"。

口径(全部无前视, 动态池前600, A股T+1):
  T 日收盘后识别龙头(仅用当日可知信息: 连板高度/成交额人气)
  T+1 竞价买入(排除一字板 — 买不到)
  卖出: T+1 收盘 或 T+2 收盘
龙头分档:
  对照  所有涨停股(无差别接力, 此前已测 -0.19%/天)
  2板+/3板+/4板+  连板数过滤
  空间板  当日涨停股中连板数最高的
  人气    连板≥3 且 成交额当日排名前50
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

MB = Path.home() / ".tradingagents" / "youzi" / "mb_daily.pkl"
COST = 0.002
IS_END = pd.Timestamp("2024-12-31")


def streak_matrix(lim):
    out = pd.DataFrame(0.0, index=lim.index, columns=lim.columns)
    for c in lim.columns:
        s = lim[c].fillna(False).astype(bool)
        out[c] = s.groupby((~s).cumsum()).cumsum()
    return out


def perf(port):
    if len(port) < 20:
        return (np.nan,) * 4
    eq = (1 + port).cumprod()
    tot = (eq.iloc[-1] - 1) * 100
    yrs = max((port.index[-1] - port.index[0]).days / 365.25, 0.1)
    ann = ((1 + tot / 100) ** (1 / yrs) - 1) * 100
    mdd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    return tot, ann, port.mean() * 100, mdd


def main() -> int:
    df = pd.read_pickle(MB)
    piv = lambda v: df.pivot(index="date", columns="code", values=v).sort_index()
    close, openp, amount = piv("close"), piv("open"), piv("amount")
    close = close[close.index >= pd.Timestamp("2022-04-01")]
    openp = openp.reindex(close.index)
    amount = amount.reindex(close.index)
    prev = close.shift(1)
    dret = close / prev - 1
    lim = dret >= 0.098
    st = streak_matrix(lim)
    nx_open, nx_close, nx2 = openp.shift(-1), close.shift(-1), close.shift(-2)
    yizi = nx_open / close - 1 >= 0.098
    in_pool = (amount.rolling(20).mean().shift(1)
               .rank(axis=1, ascending=False) <= 600)
    amt_rank = amount.rank(axis=1, ascending=False)

    max_st = st.where(lim).max(axis=1)
    is_space = st.eq(max_st, axis=0) & lim & (st >= 3)   # 按行广播(每日空间板)

    tiers = [
        ("对照:所有涨停", lim & in_pool),
        ("2板+", (st >= 2) & lim & in_pool),
        ("3板+", (st >= 3) & lim & in_pool),
        ("4板+", (st >= 4) & lim & in_pool),
        ("空间板(最高连板)", is_space & in_pool),
        ("3板+人气前50", (st >= 3) & lim & (amt_rank <= 50) & in_pool),
    ]

    print("=" * 96)
    print("  龙头接力回测 — T日涨停→T+1竞价买(排一字)→卖出  无前视/动态池/A股T+1")
    print(f"  {close.index[0].date()} ~ {close.index[-1].date()} | 成本 {COST*100:.1f}%")
    print("=" * 96)
    print(f"  {'档位':<18}{'卖出':<10}{'笔数':>7}{'笔均':>9}{'累计':>12}{'年化':>9}"
          f"{'回撤':>8}{'胜率':>7}{'竞价溢价':>10}")

    best = None
    for label, sig in tiers:
        buyable = sig & ~yizi
        for sell_lab, r in (("T+1收盘", nx_close / nx_open - 1),
                            ("T+2收盘", nx2 / nx_open - 1)):
            masked = pd.DataFrame(np.where(buyable.values, r.values, np.nan),
                                  index=r.index, columns=r.columns)
            port = masked.mean(axis=1) - COST
            port = port.dropna()
            t, a, dr, m = perf(port)
            vals = r.values[buyable.values]
            vals = vals[np.isfinite(vals)]
            prem = (nx_open / close - 1).values[buyable.values]
            prem = prem[np.isfinite(prem)]
            print(f"  {label:<18}{sell_lab:<10}{len(vals):>7}{vals.mean()*100:>+8.2f}%"
                  f"{t:>+11.0f}%{a:>+8.1f}%{m:>7.1f}%{(vals>0).mean()*100:>6.1f}%"
                  f"{prem.mean()*100:>+9.2f}%")
            if sell_lab == "T+1收盘" and np.isfinite(a) and (best is None or a > best[1]):
                best = (label, a, port)
        print()

    if best and np.isfinite(best[1]):
        label, a, port = best
        gi = pd.to_datetime(pd.Index(port.index))
        print(f"  【最优档: {label} (T+1收盘卖) 分年】")
        for y in sorted(set(gi.year)):
            py = port[gi.year == y]
            if len(py) < 20:
                continue
            t, aa, dr, m = perf(py)
            print(f"    {y}: 累计 {t:+.0f}%  日均 {dr:+.3f}%  胜率 {(py>0).mean()*100:.1f}%")
        isp = perf(port[gi <= IS_END])
        osp = perf(port[gi > IS_END])
        print(f"    样本内(~2024末) 年化 {isp[1]:+.1f}% | 样本外(2025+) 年化 {osp[1]:+.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

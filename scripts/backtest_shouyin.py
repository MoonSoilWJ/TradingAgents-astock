#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首阴低吸回测 — 连板龙头第一次收阴时买入, 赌资金回流/反包。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么选这个策略
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  半路板失败的根因: 要猜"这只会不会封板" — 猜别人的意图, 天然被动。
  首阴低吸不同: 涨停已经发生(既成事实), 只在龙头断板日买入 —
  用的是"资金曾经高度认可过这只票"这个已确认的信息, 而不是预测。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
口径
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  前置: 截至 T-1 连续涨停 N 天 (N=1/2/3)
  首阴: T 日收盘未涨停 且 收阴(close < open)
  买入: A 首阴日收盘  |  B 首阴次日开盘(低吸)
  卖出: A 次日收盘    |  B 买入当日收盘 或 再持一日
  成本: 0.2%(双边) ; 数据: 全市场主板日K, 无前视

用法: python3 scripts/backtest_shouyin.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

MB_DAILY = Path.home() / ".tradingagents" / "youzi" / "mb_daily.pkl"
MB_FLOAT = Path.home() / ".tradingagents" / "youzi" / "mb_float.json"
COST = 0.002
IS_END = pd.Timestamp("2024-12-31")


def load():
    df = pd.read_pickle(MB_DAILY)
    piv = lambda v: df.pivot(index="date", columns="code", values=v).sort_index()
    d = {"close": piv("close"), "open": piv("open"),
         "high": piv("high"), "amount": piv("amount")}
    d["close"] = d["close"][d["close"].index >= pd.Timestamp("2022-04-01")]
    d["open"] = d["open"].reindex(d["close"].index)
    d["high"] = d["high"].reindex(d["close"].index)
    d["amount"] = d["amount"].reindex(d["close"].index)
    with open(MB_FLOAT) as f:
        fs = pd.Series({k: float(v) for k, v in json.load(f).items()})
    return d, fs.reindex(d["close"].columns)


def streak_matrix(lim: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(0.0, index=lim.index, columns=lim.columns)
    for c in lim.columns:
        s = lim[c].fillna(False).astype(bool)
        out[c] = s.groupby((~s).cumsum()).cumsum()
    return out


def mask_apply(r: pd.DataFrame, sig: pd.DataFrame) -> pd.DataFrame:
    """用 numpy 掩码置 NaN (避免 DataFrame.where 的对齐歧义)。"""
    return pd.DataFrame(np.where(sig.values, r.values, np.nan),
                        index=r.index, columns=r.columns)


def perf(port: pd.Series) -> tuple[float, float, float, float]:
    if len(port) < 20:
        return (np.nan,) * 4
    eq = (1 + port).cumprod()
    tot = (eq.iloc[-1] - 1) * 100
    yrs = max((port.index[-1] - port.index[0]).days / 365.25, 0.1)
    ann = ((1 + tot / 100) ** (1 / yrs) - 1) * 100
    mdd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    return tot, ann, port.mean() * 100, mdd


def main() -> int:
    d, fs = load()
    close, openp, high, amount = d["close"], d["open"], d["high"], d["amount"]
    prev = close.shift(1)
    dret = close / prev - 1
    lim = dret >= 0.098
    st = streak_matrix(lim)                       # 截至当日的连续涨停天数
    yin = close < openp                          # 收阴
    yang_next = (close.shift(-1) / close - 1)    # 次日涨幅(反包判定)

    # 动态池: 截至 T-1 的 20 日均额前 600 (与半路板口径一致, 防活跃池前视)
    in_pool = (amount.rolling(20).mean().shift(1)
               .rank(axis=1, ascending=False) <= 600)

    base = (dret.where(in_pool)).mean(axis=1).dropna()
    bt, ba, bd, bm = perf(base)

    print("=" * 92)
    print("  首阴低吸回测 (全市场主板日K, 无前视, 动态池前600)")
    print(f"  {close.index[0].date()} ~ {close.index[-1].date()} | "
          f"{len(close.columns)} 只 | 成本 {COST*100:.1f}%")
    print("=" * 92)
    print(f"  对照 等权持有池内: 年化 {ba:+.1f}% 累计 {bt:+.0f}% 回撤 {bm:.1f}%\n")

    print(f"  [诊断] 涨停 {int(lim.sum().sum())} | 连板≥1 {int((st>=1).sum().sum())} | "
          f"收阴 {int(yin.sum().sum())} | 池内 {int(in_pool.sum().sum())}")
    _t = (st.shift(1) >= 1) & (~lim) & yin & in_pool
    print(f"  [诊断] 首阴信号(N=1) 命中 {int(_t.sum().sum())} 笔")

    print("\n  【一】A 模式: 首阴日收盘买入 → 次日收盘卖")
    print(f"  {'前置连板':<10}{'笔数':>7}{'日均':>9}{'累计':>11}{'年化':>9}"
          f"{'回撤':>8}{'胜率':>7}{'次日反包率':>10}")
    for n in (1, 2, 3, 4):
        sig = (st.shift(1) >= n) & (~lim) & yin & in_pool
        r = (close.shift(-1) / close - 1)
        port = mask_apply(r, sig).mean(axis=1) - COST
        port = port.dropna()
        t, a, dr, m = perf(port)
        allr = pd.Series(r.values[sig.values])
        allr = allr[np.isfinite(allr)]
        reb = pd.Series(yang_next.values[sig.values])
        reb = reb[np.isfinite(reb)]
        print(f"  ≥{n}板{'':<7}{len(allr):>7}{dr:>+8.3f}%{t:>+10.0f}%"
              f"{a:>+8.1f}%{m:>7.1f}%{(allr>0).mean()*100:>6.1f}%"
              f"{reb.mean()*100:>9.1f}%")

    print("\n  【二】B 模式: 首阴次日开盘买入(低吸) → 当日收盘 / 次日收盘卖")
    print(f"  {'前置连板':<10}{'卖出':<12}{'笔数':>7}{'日均':>9}{'累计':>11}"
          f"{'年化':>9}{'回撤':>8}{'胜率':>7}")
    for n in (1, 2, 3):
        for lab, r in (("当日收盘", close.shift(-1) / openp.shift(-1) - 1),
                       ("次日收盘", close.shift(-2) / openp.shift(-1) - 1)):
            # 注意: ~lim 先取反再 shift, 否则 shift 引入 NaN 会把 bool 变 float
            notlim_prev = (~lim).shift(1).fillna(False).astype(bool)
            yin_prev = yin.shift(1).fillna(False).astype(bool)
            sig = (st.shift(2) >= n) & notlim_prev & yin_prev & \
                in_pool.shift(1).fillna(False).astype(bool)
            port = mask_apply(r, sig).mean(axis=1) - COST
            port = port.dropna()
            t, a, dr, m = perf(port)
            allr = pd.Series(r.values[sig.values])
            allr = allr[np.isfinite(allr)]
            print(f"  ≥{n}板{'':<7}{lab:<12}{len(allr):>7}{dr:>+8.3f}%"
                  f"{t:>+10.0f}%{a:>+8.1f}%{m:>7.1f}%{(allr>0).mean()*100:>6.1f}%")

    # 最优档(N=2, A模式)细看
    print("\n  【三】细看 N=2 首阴 (A模式) — 分年 + 样本内外")
    sig = (st.shift(1) >= 2) & (~lim) & yin & in_pool
    r = (close.shift(-1) / close - 1)
    port = (r.where(sig)).mean(axis=1) - COST
    port = port.dropna()
    gi = pd.to_datetime(pd.Index(port.index))
    for y in sorted(set(gi.year)):
        py = port[gi.year == y]
        if len(py) < 20:
            continue
        t, a, dr, m = perf(py)
        print(f"    {y}: 累计 {t:+.0f}%  年化 {a:+.1f}%  日均 {dr:+.3f}%"
              f"  回撤 {m:.1f}%  胜率 {(py>0).mean()*100:.1f}%")
    isp = perf(port[gi <= IS_END])
    osp = perf(port[gi > IS_END])
    print(f"    样本内(~2024末): 年化 {isp[1]:+.1f}% | "
          f"样本外(2025+): 年化 {osp[1]:+.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

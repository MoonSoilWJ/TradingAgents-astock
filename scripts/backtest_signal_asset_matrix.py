#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 【N12 信号源 × 资产 矩阵回测】回答两个问题:
#   1) 哪个宽基的 N12 开关最好? (科创50 / 沪深300 / 中证500)
#   2) 同一个开关该配什么资产? (全市场个股等权 / 对应ETF / 其他ETF)
#
# 规则: 信号资产 N12 六簇 frac>0.5 → 持有目标资产; 否则空仓货基(1.8%)
#      当日收盘信号当日收盘成交, 滑点万10
# 窗口: 2022-01-04 ~ 2026-11-11 (个股数据起点)
#
# 用法: python3 scripts/backtest_signal_asset_matrix.py
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_n12_stock import vote_target

CACHE = Path("/Users/licheng/.tradingagents/cache")
MB = Path("/Users/licheng/.tradingagents/youzi/mb_daily.pkl")
SLIP = 0.001
MMF = (1 + 0.018) ** (1 / 252) - 1
START = "2022-01-04"

SIGNALS = [("科创50", "idx_sh588000.json"), ("沪深300", "idx_sh510300.json"),
           ("中证500", "idx_sh510500.json")]
ASSETS = [("科创50ETF", "idx_sh588000.json"), ("沪深300ETF", "idx_sh510300.json"),
          ("中证500ETF", "idx_sh510500.json"), ("个股等权", None)]


def load_etf(fname):
    e = pd.read_json(CACHE / fname)
    e["date"] = pd.to_datetime(e["date"])
    return e[["date", "close"]].sort_values("date").reset_index(drop=True)


def load_stock_equal_weight():
    df = pd.read_pickle(MB)
    df["date"] = pd.to_datetime(df["date"])
    close = df.pivot(index="date", columns="code", values="close")
    pct = close.pct_change(fill_method=None)
    ret = pct.mean(axis=1).fillna(0.0)
    eq = (1 + ret).cumprod()
    return pd.DataFrame({"date": eq.index, "close": eq.to_numpy()})


def sim(tgt, close):
    """tgt[i]=i日收盘后目标仓位; i日收益用tgt[i-1]."""
    n = len(close)
    eq = np.empty(n)
    units, cash, pos = 0.0, 1.0, 0
    for i in range(n):
        if i > 0 and pos == 1:
            cash = units * close[i]
        t = int(tgt[i])
        if t != pos:
            if t == 1:
                units = cash * (1 - SLIP) / close[i]
                cash = 0.0
            else:
                cash = units * close[i] * (1 - SLIP)
                units = 0.0
            pos = t
        if pos == 0:
            cash *= (1 + MMF)
        eq[i] = cash if pos == 0 else units * close[i]
    return eq


def main():
    sig_data = {}
    for name, f in SIGNALS:
        e = load_etf(f)
        tgt, _ = vote_target(e["close"].to_numpy(float))
        sig_data[name] = pd.DataFrame({"date": e["date"], "tgt": tgt})

    asset_data = {}
    for name, f in ASSETS:
        asset_data[name] = load_stock_equal_weight() if f is None else load_etf(f)
        asset_data[name] = asset_data[name][asset_data[name]["date"] >= START].reset_index(drop=True)

    print("=" * 100)
    print("N12 开关 × 资产 矩阵   %s ~ 2026-11-11   (年化 / MDD)" % START)
    print("=" * 100)
    hdr = "%-12s" % "资产＼信号"
    for sname, _ in SIGNALS:
        hdr += " %-22s" % sname
    hdr += " %-20s" % "买入持有"
    print(hdr)
    print("-" * 100)

    out = {}
    for aname, adf in asset_data.items():
        row = "%-12s" % aname
        for sname, _ in SIGNALS:
            m = adf.merge(sig_data[sname], on="date", how="left")
            m["tgt"] = m["tgt"].ffill().fillna(0).astype(int)
            close = m["close"].to_numpy(float)
            if len(close) < 300:
                row += " %-22s" % "-"
                continue
            eq = sim(m["tgt"].to_numpy(), close)
            yrs = (m["date"].iloc[-1] - m["date"].iloc[0]).days / 365.25
            total = eq[-1] / eq[0] - 1
            ann = (1 + total) ** (1 / yrs) - 1
            mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
            row += " %+7.1f%% / %-6.1f%%   " % (ann * 100, mdd * 100)
            out[(aname, sname)] = (ann, mdd)
        # 买入持有
        clean = adf.dropna(subset=["close"])
        close = clean["close"].to_numpy(float)
        yrs = (clean["date"].iloc[-1] - clean["date"].iloc[0]).days / 365.25
        tot = close[-1] / close[0] - 1
        ann = (1 + tot) ** (1 / yrs) - 1
        mdd = ((close - np.maximum.accumulate(close)) / np.maximum.accumulate(close)).min()
        row += " %+7.1f%% / %-6.1f%%" % (ann * 100, mdd * 100)
        print(row)

    print("-" * 100)
    print("判读: 看每个资产行里, 哪个信号列的年化最高且MDD最低 → 最优开关;")
    print("      看同一信号列里, 哪个资产行表现最好 → 该开关应配什么资产。")


if __name__ == "__main__":
    main()

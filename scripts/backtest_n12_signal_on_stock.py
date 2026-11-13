#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 【用科创50(588000)的 N12 买卖点 交易个股】——择时信号跨资产可用性测试
#   信号: 588000 六簇 frac > 0.5 → 持有目标个股, 否则空仓(货基1.8%)
#   执行: 当日收盘信号当日收盘成交(14:55), 滑点万10
#   对照: 标的买入持有 / 588000 N12 本身
#
# 用法: python3 scripts/backtest_n12_signal_on_stock.py 600183
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_n12_stock import vote_target

MB = Path("/Users/licheng/.tradingagents/youzi/mb_daily.pkl")
ETF = Path("/Users/licheng/.tradingagents/cache/idx_sh588000.json")
SLIP = 0.001
MMF_APY = 0.018


def load_stock(code):
    df = pd.read_pickle(MB)
    s = df[df["code"] == code].sort_values("date").reset_index(drop=True)
    s["date"] = pd.to_datetime(s["date"])
    return s[["date", "open", "close"]]


def load_etf():
    e = pd.read_json(ETF)
    e["date"] = pd.to_datetime(e["date"])
    return e[["date", "close"]].sort_values("date").reset_index(drop=True)


def sim(tgt, close, mmf_daily):
    """tgt[i]=i日收盘后的目标仓位; i日收益用tgt[i-1]. 返回eq/交易/持仓占比."""
    n = len(close)
    eq = np.empty(n)
    units, cash, pos = 0.0, 1.0, 0
    trades = []
    entry_px, entry_i = 0.0, 0
    for i in range(n):
        r = close[i] / close[i - 1] - 1 if (i > 0 and pos == 1) else 0.0
        cost = 0.0
        t = int(tgt[i])
        if t != pos:
            cost = SLIP
            if t == 1:
                units = cash * (1 - SLIP) / close[i]
                cash = 0.0
                entry_px, entry_i = close[i] * (1 + SLIP), i
            else:
                spx = close[i] * (1 - SLIP)
                cash = units * spx
                units = 0.0
                trades.append(dict(ret=spx / entry_px - 1, hold=i - entry_i))
            pos = t
        if pos == 1:
            cash = 0.0
            eq[i] = units * close[i]
        else:
            cash *= (1 + mmf_daily)
            eq[i] = cash
            units = 0.0
    if pos == 1:
        trades.append(dict(ret=close[-1] / entry_px - 1, hold=n - 1 - entry_i))
    return eq, trades


def stats(tag, eq, trades, dates):
    yrs = (dates[-1] - dates[0]) / np.timedelta64(1, "D") / 365.25
    total = eq[-1] / eq[0] - 1
    ann = (1 + total) ** (1 / yrs) - 1
    mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    vol = np.std(np.diff(eq) / eq[:-1]) * np.sqrt(252)
    rs = np.array([t["ret"] for t in trades])
    print("  %-24s 累计 %+9.1f%%  年化 %+7.2f%%  MDD %7.2f%%  波动 %5.1f%%  笔数 %3d  胜率 %3.0f%%  均笔 %+.2f%%"
          % (tag, total * 100, ann * 100, mdd * 100, vol * 100, len(rs),
             (rs > 0).mean() * 100 if len(rs) else 0, rs.mean() * 100 if len(rs) else 0))
    return dict(total=float(total), ann=float(ann), mdd=float(mdd), n=int(len(rs)),
                win=float((rs > 0).mean() if len(rs) else 0),
                mean=float(rs.mean() if len(rs) else 0))


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else "600183"
    st = load_stock(code)
    etf = load_etf()

    # 588000 N12 信号
    e_close = etf["close"].to_numpy().astype(float)
    e_tgt, e_frac = vote_target(e_close)
    sig = pd.DataFrame({"date": etf["date"], "tgt": e_tgt, "frac": e_frac})

    # 对齐到个股交易日(信号取当日值; 个股停牌时沿用最近信号)
    df = st.merge(sig, on="date", how="left")
    df["tgt"] = df["tgt"].ffill().fillna(0).astype(int)
    dates = df["date"].to_numpy()
    close = df["close"].to_numpy().astype(float)
    tgt = df["tgt"].to_numpy()
    mmf = (1 + MMF_APY) ** (1 / 252) - 1

    print("=" * 108)
    print("用 588000 的 N12 买卖点 交易 %s   %s ~ %s  (%.2f 年)"
          % (code, pd.Timestamp(dates[0]).date(), pd.Timestamp(dates[-1]).date(),
             (dates[-1] - dates[0]) / np.timedelta64(1, "D") / 365.25))
    print("信号: 588000 六簇 frac>0.5 持仓 / 否则空仓货基; 同日收盘成交, 滑点万10")
    print("N12 持仓占比 %.0f%% (信号来自 588000)" % (tgt.mean() * 100))
    print("=" * 108)

    eq, tr = sim(tgt, close, mmf)
    stats("① 个股 + N12择时", eq, tr, dates)
    eqh = np.cumprod(np.append([1.0], close[1:] / close[:-1]))
    stats("② 个股 买入持有", eqh, [], dates)

    # 588000 自身 N12
    e_dates = etf["date"].to_numpy()
    e_eq, e_tr = sim(e_tgt, e_close, mmf)
    m = (e_dates >= dates[0]) & (e_dates <= dates[-1])
    stats("③ 588000 + N12择时(同期)", e_eq[m] / e_eq[m][0], e_tr, e_dates[m])

    print("-" * 108)
    print("判读: 若①显著优于② → 宽基择时信号能改善个股持有体验;")
    print("      若①显著劣于② → 个股自身趋势与科创50节奏不同步, 信号跨资产失效。")


if __name__ == "__main__":
    main()

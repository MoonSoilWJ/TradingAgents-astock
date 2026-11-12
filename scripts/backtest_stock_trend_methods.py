#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 【趋势个股三种标准抓法对比回测】
#   数据: ~/.tradingagents/youzi/mb_daily.pkl (主板全市场, 2022-2026, OHLC+amount)
#   口径: 信号日收盘确认 → 次日开盘买入(无前视); 卖出当日收盘(14:55可执行);
#         滑点万10双边; 最多持5只等权; 闲置现金不计息
#
#   V1 突破: 收盘创60日新高 + 成交额>20日均额x1.5 + MA20>MA60 + 排除涨停/低价/次新
#   V2 回调: MA20>MA60 + 回踩MA20±3% + 20日涨幅>10% + 排除涨停
#   V3 相对强度: 20日涨幅全市场前3% + 收盘>MA20 + 排除涨停
#   卖出: 跌破均线(V1/V3=MA20, V2=MA60) 或 峰值回撤(10%/15%/12%) 或 -8%止损
#
# 用法: python3 scripts/backtest_stock_trend_methods.py
import sys
from pathlib import Path

import numpy as np
import pandas as pd

MB_PKL = Path("/Users/licheng/.tradingagents/youzi/mb_daily.pkl")
SLIP = 0.001
MAX_HOLD = 5
STOP = -0.08
MIN_PRICE = 3.0
MIN_AMT = 5e7
MIN_LISTED = 120


def load_data():
    df = pd.read_pickle(MB_PKL)
    df["date"] = pd.to_datetime(df["date"])
    dates = np.sort(df["date"].unique())
    close = df.pivot(index="date", columns="code", values="close")
    open_ = df.pivot(index="date", columns="code", values="open")
    amt = df.pivot(index="date", columns="code", values="amount")
    return dates, close, open_, amt


def build_signals(close, amt):
    pct = close.pct_change(fill_method=None)
    ret20 = close.pct_change(20, fill_method=None)
    ma20 = close.rolling(20, min_periods=20).mean()
    ma60 = close.rolling(60, min_periods=60).mean()
    amt20 = amt.rolling(20, min_periods=20).mean()
    listed = close.notna().cumsum()
    hi60 = close.rolling(60, min_periods=60).max()

    common = ((pct < 0.095) & (close > MIN_PRICE) & (amt >= MIN_AMT)
              & (listed >= MIN_LISTED))
    ma_bull = ma20 > ma60

    v1 = (common & (close >= hi60) & (amt > amt20 * 1.5) & ma_bull).to_numpy()
    v2 = (common & ma_bull & ((close / ma20 - 1).abs() < 0.03)
          & (ret20 > 0.10)).to_numpy()
    # V3: 20日涨幅截面排名前3%
    r20 = ret20.to_numpy()
    valid = np.isfinite(r20) & common.to_numpy()
    thresh = np.full(len(r20), np.inf)
    for i in range(len(r20)):
        row = r20[i][valid[i]]
        if len(row) >= 50:
            thresh[i] = np.quantile(row, 0.97)
    v3 = (valid & (r20 >= thresh[:, None]) & (close > ma20).to_numpy())

    return {"V1 突破": v1, "V2 回调": v2, "V3 相对强度": v3}, ma20.to_numpy(), ma60.to_numpy(), ret20


def sim(sig, ma_slow, close, open_, trail, dates, rank_key):
    """信号日T收盘确认 → T+1开盘买; 跌破ma_slow/trail/stop 当日收盘卖."""
    C = close.to_numpy()
    O = open_.to_numpy()
    C_ff = pd.DataFrame(C).ffill().to_numpy()  # 估值用填充价(停牌沿用前收盘)
    n_days, n_codes = C.shape
    codes = np.array(close.columns)
    pos_cols = {c: j for j, c in enumerate(codes)}

    cash = 1.0
    units, entry_px, peak, entry_day = {}, {}, {}, {}
    pending = []          # 昨日信号: [(col, rank_val)]
    eq = np.empty(n_days)
    trades = []

    for i in range(n_days):
        # 1) 执行昨日 pending 买入(今日开盘)
        if pending and i > 0:
            slots = MAX_HOLD - len(units)
            if slots > 0:
                order = sorted(pending, key=lambda x: -x[1])[:slots]
                equity = cash + sum(units[c] * C_ff[i - 1, pos_cols[c]] for c in units)
                per = min(cash / len(order), equity / MAX_HOLD)
                if not np.isfinite(per) or per <= 0:
                    per = 0.0
                for j, _ in order:
                    op = O[i, j]
                    if not np.isfinite(op) or op <= 0 or np.isnan(C[i - 1, j]):
                        continue
                    # 次日开盘涨停跳空(>9.5%)则放弃, 买不进
                    if op / C[i - 1, j] - 1 > 0.095:
                        continue
                    px = op * (1 + SLIP)
                    if per <= 0:
                        continue
                    c = codes[j]
                    units[c] = per / px
                    entry_px[c] = px
                    peak[c] = px
                    entry_day[c] = i
                    cash -= per
        pending = []

        # 2) 卖出检查(今日收盘)
        for c in list(units):
            j = pos_cols[c]
            px_c = C[i, j]
            if not np.isfinite(px_c):
                continue
            peak[c] = max(peak[c], px_c)
            ret = px_c / entry_px[c] - 1
            dd = 1 - px_c / peak[c]
            sell = False
            reason = ""
            if ret <= STOP:
                sell, reason = True, "stop"
            elif np.isfinite(ma_slow[i, j]) and px_c < ma_slow[i, j]:
                sell, reason = True, "ma_break"
            elif dd >= trail:
                sell, reason = True, "trail"
            if sell:
                spx = px_c * (1 - SLIP)
                cash += units[c] * spx
                trades.append(dict(code=c, bpx=entry_px[c], spx=spx,
                                   ret=spx / entry_px[c] - 1, reason=reason,
                                   hold=i - entry_day[c]))
                del units[c], entry_px[c], peak[c], entry_day[c]
        # 3) 生成今日信号(明日开盘执行)
        if i < n_days - 1:
            cols = np.where(sig[i])[0]
            vals = rank_key[i]
            pending = [(j, vals[j] if np.isfinite(vals[j]) else 0.0)
                       for j in cols if codes[j] not in units]
        # 4) 权益(填充价估值, 停牌不丢市值)
        mv = sum(units[c] * C_ff[i, pos_cols[c]] for c in units)
        eq[i] = cash + mv

    return eq, trades


def main():
    dates, close, open_, amt = load_data()
    print("数据: %d 天 x %d 只  %s ~ %s" %
          (close.shape[0], close.shape[1], pd.Timestamp(dates[0]).date(),
           pd.Timestamp(dates[-1]).date()))
    sigs, ma20, ma60, ret20 = build_signals(close, amt)

    print("=" * 100)
    print("%-14s %9s %9s %8s %6s %6s %8s" %
          ("策略", "累计", "年化", "MDD", "笔数", "胜率", "均笔"))
    print("=" * 100)
    yrs = (dates[-1] - dates[0]) / np.timedelta64(1, "D") / 365.25
    for name, sig in sigs.items():
        ma_slow = ma60 if name == "V2 回调" else ma20
        trail = {"V1 突破": 0.10, "V2 回调": 0.15, "V3 相对强度": 0.12}[name]
        try:
            eq, trades = sim(sig, ma_slow, close, open_, trail, dates, ret20.to_numpy())
        except Exception as e:
            print("%-14s FAIL %s" % (name, repr(e)[:80]))
            continue
        total = eq[-1] / eq[0] - 1
        ann = (1 + total) ** (1 / yrs) - 1
        mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
        rs = np.array([t["ret"] for t in trades])
        print("%-14s %+8.1f%% %+8.2f%% %7.1f%% %6d %5.0f%% %+7.3f%%" %
              (name, total * 100, ann * 100, mdd * 100, len(rs),
               (rs > 0).mean() * 100 if len(rs) else 0,
               rs.mean() * 100 if len(rs) else 0))
        if len(rs):
            print("   持有中位 %d 日 | 卖出原因: %s" %
                  (np.median([t["hold"] for t in trades]),
                   dict(pd.Series([t["reason"] for t in trades]).value_counts())))
    print("=" * 100)
    print("判读: 三种抓法只要有一种 年化>0 且笔数>100 → 该抓法在A股主板有正期望;")
    print("      全负 → 个股趋势抓法同样被证伪, 与ETF动量(R3)区别在于个股无隔夜连续性溢价。")


if __name__ == "__main__":
    main()

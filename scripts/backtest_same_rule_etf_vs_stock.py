#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 【R3 规则原样搬运: ETF池 vs 个股池 同规则对比】
#
# 规则(日线近似, 两边完全一致):
#   T日收盘: 池内选当日涨幅 >= 3% 的最强 1 只, 收盘价买入(滑点万10)
#   T+1日收盘: 卖出(滑点万10)
#   无候选日空仓; 不设止损/门禁/池子轮动
#
# 池子三种:
#   ETF  : 106 只 T0 ETF (跨境/商品/黄金/债券, 即 R3 的候选域)
#   个股A: 主板全市场 3051 只
#   个股B: 主板近20日涨幅前25%的强势池(近似 R3 的动量优质池筛选)
#
# 注意: 日线口径无法复现 R3 的 14:40选/14:45复核/次日09:40-11:05 TRIX卖
#       → 三者都在"次日收盘卖"口径下对比, 差异即资产类别+池子筛选的净效应
#
# 用法: python3 scripts/backtest_same_rule_etf_vs_stock.py
import json
from pathlib import Path

import numpy as np
import pandas as pd

MB_PKL = Path("/Users/licheng/.tradingagents/youzi/mb_daily.pkl")
ETF_DAILY = Path("/Users/licheng/.tradingagents/cache/t0_5min/backfill_daily_1000.json")
SLIP = 0.001
MIN_GAIN = 0.03
LIMIT = 0.095


def load_etf():
    d = json.load(open(ETF_DAILY))["etf_daily"]
    frames, opens = {}, {}
    for code, v in d.items():
        rows = v["returns"]
        if len(rows) < 60:
            continue
        df = pd.DataFrame(rows)[["date", "open", "close"]]
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        frames[code] = df["close"]
        opens[code] = df["open"]
    close = pd.DataFrame(frames).sort_index()
    op = pd.DataFrame(opens).sort_index()
    return close, op


def load_stock():
    df = pd.read_pickle(MB_PKL)
    df["date"] = pd.to_datetime(df["date"])
    close = df.pivot(index="date", columns="code", values="close")
    op = df.pivot(index="date", columns="code", values="open")
    amt = df.pivot(index="date", columns="code", values="amount")
    return close, op, amt


def sim(close_mat, open_mat, pct_mat, eligible_mat, dates, sell_at_open=False):
    """T收盘选最强买入, T+1 开盘或收盘卖出. 返回逐日权益与交易台账."""
    C = close_mat.to_numpy()
    O = open_mat.to_numpy()
    C_ff = pd.DataFrame(C).ffill().to_numpy()  # 估值用填充价(停牌/退市不丢市值)
    P = pct_mat
    E = eligible_mat
    n_days = len(dates)
    pos_j, units, buy_px = -1, 0.0, 0.0
    cash = 1.0
    eq = np.empty(n_days)
    trades = []
    for i in range(n_days):
        # 卖出(昨日买入)
        if pos_j >= 0:
            px = O[i, pos_j] if sell_at_open else C[i, pos_j]
            if not np.isfinite(px):
                px = C_ff[i, pos_j]  # 停牌/退市: 按最后有效价平仓, 防资金卡死
            spx = px * (1 - SLIP)
            if np.isfinite(spx):
                cash = units * spx
                trades.append(dict(ret=spx / buy_px - 1, hold=1))
                pos_j = -1
        # 买入(今日信号)
        if pos_j < 0 and i < n_days - 1:
            row_p, row_e = P[i], E[i]
            mask = row_e & np.isfinite(row_p) & (row_p >= MIN_GAIN)
            if mask.any():
                cand = np.where(mask)[0]
                j = cand[np.argmax(row_p[cand])]
                bpx = C[i, j] * (1 + SLIP)
                if np.isfinite(bpx) and bpx > 0:
                    units = cash / bpx
                    buy_px = bpx
                    pos_j = j
                    cash = 0.0
        # 权益(填充价估值)
        eq[i] = cash + (units * C_ff[i, pos_j] if pos_j >= 0 else 0.0)
    return eq, trades


def stats(tag, eq, trades, dates):
    yrs = (dates[-1] - dates[0]) / np.timedelta64(1, "D") / 365.25
    total = eq[-1] / eq[0] - 1
    ann = (1 + total) ** (1 / yrs) - 1 if total > -1 else -1.0
    mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    rs = np.array([t["ret"] for t in trades])
    empty = np.mean(np.abs(np.diff(eq)) < 1e-12) * 100
    print("%-10s %+9.1f%% %+8.2f%% %7.1f%% %5d %5.0f%% %+7.3f%% %6.0f%%"
          % (tag, total * 100, ann * 100, mdd * 100, len(rs),
             (rs > 0).mean() * 100 if len(rs) else 0,
             rs.mean() * 100 if len(rs) else 0, empty))
    return dict(total=float(total), ann=float(ann), mdd=float(mdd),
                n=int(len(rs)), win=float((rs > 0).mean() if len(rs) else 0),
                mean=float(rs.mean() if len(rs) else 0))


def monthly_pool_mask(close_mat, top_n=25, lookback=60):
    """月度轮动池: 每月最后一个交易日按过去 lookback 日涨幅取 TopN, 下月生效(无前视)."""
    C = close_mat.to_numpy()
    n_days, n_codes = C.shape
    ret = np.full_like(C, np.nan)
    ret[lookback:] = C[lookback:] / C[:-lookback] - 1
    idx = pd.DatetimeIndex(close_mat.index)
    months = idx.to_period("M")
    month_end = np.zeros(n_days, bool)
    for i in range(n_days):
        if i == n_days - 1 or months[i] != months[i + 1]:
            month_end[i] = True
    mask = np.zeros((n_days, n_codes), bool)
    cur = np.zeros(n_codes, bool)
    for i in range(n_days):
        mask[i] = cur
        if month_end[i]:
            r = ret[i]
            ok = np.isfinite(r)
            if ok.sum() >= top_n:
                top = np.argsort(np.where(ok, r, -np.inf))[-top_n:]
                cur = np.zeros(n_codes, bool)
                cur[top] = True
    return mask


def main():
    START = "2022-06-15"   # ETF 日线覆盖起点, 与个股数据共同窗口
    etf_close, etf_open = load_etf()
    st_close, st_open, st_amt = load_stock()

    # 共同日期轴: ETF 交易日(限 START 之后)
    dates = np.array(etf_close.index[etf_close.index >= START])

    # ETF 池
    etf_c = etf_close.reindex(pd.DatetimeIndex(dates))
    etf_o = etf_open.reindex(pd.DatetimeIndex(dates))
    etf_pct = etf_c.pct_change(fill_method=None)
    etf_elig = pd.DataFrame(np.isfinite(etf_pct.to_numpy()) & (etf_c > 0), index=etf_c.index,
                            columns=etf_c.columns)

    # 个股池(对齐到同一日期轴)
    di = pd.DatetimeIndex(dates)
    st_c = st_close.reindex(di).ffill()
    st_o = st_open.reindex(di).ffill()
    st_pct = st_close.pct_change(fill_method=None).reindex(di)
    st_listed = st_close.notna().cumsum().reindex(di).fillna(0)
    st_elig_base = (np.isfinite(st_pct) & (st_c > 3.0)
                    & (st_pct < LIMIT) & (st_listed >= 120))
    st20 = st_close.pct_change(20, fill_method=None).reindex(di)
    r20 = st20.to_numpy()
    thr = np.full(len(r20), np.inf)
    for i in range(len(r20)):
        row = r20[i][np.isfinite(r20[i])]
        if len(row) >= 50:
            thr[i] = np.quantile(row, 0.75)
    strong = np.isfinite(r20) & (r20 >= thr[:, None])
    st_strong = (st_elig_base & pd.DataFrame(strong, index=st_c.index,
                                             columns=st_c.columns)).to_numpy()

    # 月度轮动池 Top25 (与 R3 的月度池同构)
    etf_pool = monthly_pool_mask(etf_c, top_n=25, lookback=60)
    st_pool = monthly_pool_mask(st_c, top_n=25, lookback=60)

    for sell_open, tag in ((True, "次日开盘卖(隔夜动量口径)"), (False, "次日收盘卖")):
        print("\n" + "=" * 104)
        print("R3 规则原样搬运: 当日涨幅>=3%% 最强 Top1, T收盘买 → %s" % tag)
        print("窗口 %s ~ %s (%d 交易日)" % (pd.Timestamp(dates[0]).date(),
                                          pd.Timestamp(dates[-1]).date(), len(dates)))
        print("=" * 104)
        print("%-14s %10s %9s %8s %6s %6s %9s %7s" %
              ("池子", "累计", "年化", "MDD", "笔数", "胜率", "均笔", "空仓%"))
        print("-" * 104)

        eq, tr = sim(etf_c, etf_o, etf_pct.to_numpy(), etf_elig.to_numpy(), dates, sell_open)
        stats("ETF全池", eq, tr, dates)
        eq, tr = sim(etf_c, etf_o, etf_pct.to_numpy(),
                     (etf_elig.to_numpy() & etf_pool), dates, sell_open)
        stats("ETF月度Top25", eq, tr, dates)
        eq, tr = sim(st_c, st_o, st_pct.to_numpy(), st_elig_base.to_numpy(), dates, sell_open)
        stats("个股全市场", eq, tr, dates)
        eq, tr = sim(st_c, st_o, st_pct.to_numpy(),
                     (st_elig_base.to_numpy() & st_pool), dates, sell_open)
        stats("个股月度Top25", eq, tr, dates)
        eq, tr = sim(st_c, st_o, st_pct.to_numpy(), st_strong, dates, sell_open)
        stats("个股日度Top25%", eq, tr, dates)

    print("\n" + "-" * 104)
    print("判读: ETF月度Top25 vs 个股月度Top25 是同构同规则对比(月度轮动池+当日涨>=3%最强).")
    print("      若个股仍显著为负 → 差异不在池子机制, 而在'涨3%'的生成机制(趋势 vs 操纵).")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 【科创50风向标 + skill选股 + 隔夜短线】
#
# 入场三条件:
#   1) 风向标: 588000 的 N12 frac > 0.5 (科创50金叉期, 死叉时清仓不做)
#   2) 个股金叉: 昨日 frac<=0.5 且 今日 frac>0.5
#   3) skill 偏好: 年化波动>60% / frac∈(0.60,0.90](4-5票, 排满票) / 今日涨幅<9.5%(排涨停)
# 交易: T日收盘(14:55近似)买入Top5等权, T+1 卖出(开盘/收盘/最高 三口径对照)
#
# 用法: python3 scripts/backtest_kc50_gate_n12_stock.py
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n12_cluster_now import COMB_N12  # noqa: E402

MB = Path("/Users/licheng/.tradingagents/youzi/mb_daily.pkl")
ETF = Path("/Users/licheng/.tradingagents/cache/idx_sh588000.json")
SLIP = 0.001
TOP_N = 5
MIN_VOL = 0.60
FRAC_LO, FRAC_HI = 0.60, 0.90
LIMIT = 0.095


def frac_matrix(close_mat):
    """N12 六簇看多占比矩阵(与 backtest_n12_stock.vote_target 同口径)."""
    tot = np.zeros(close_mat.shape)
    valid = np.zeros(close_mat.shape, bool)
    for n, m in COMB_N12:
        e = close_mat.ewm(span=n, adjust=False).mean()
        e = e.ewm(span=n, adjust=False).mean()
        e = e.ewm(span=n, adjust=False).mean()
        tr = e.pct_change(fill_method=None) * 100
        sig = tr.rolling(m, min_periods=m).mean()
        ok = pd.notna(tr.to_numpy()) & pd.notna(sig.to_numpy())
        v = (tr > sig).to_numpy().astype(float)
        v[~ok] = 0.0
        tot += v
        valid |= ok
    return np.where(valid, tot / len(COMB_N12), np.nan)


def main():
    from backtest_n12_stock import vote_target

    e = pd.read_json(ETF)
    e["date"] = pd.to_datetime(e["date"])
    e_frac_tgt, e_frac = vote_target(e["close"].to_numpy(float))
    gate = pd.DataFrame({"date": e["date"], "gate": (e_frac > 0.5).astype(int)})

    df = pd.read_pickle(MB)
    df["date"] = pd.to_datetime(df["date"])
    close = df.pivot(index="date", columns="code", values="close")
    open_ = df.pivot(index="date", columns="code", values="open")
    high = df.pivot(index="date", columns="code", values="high")
    amt = df.pivot(index="date", columns="code", values="amount")
    close = close.loc[:, close.notna().sum() >= 900]
    amt = amt.loc[:, close.columns]
    open_ = open_.loc[:, close.columns]
    high = high.loc[:, close.columns]

    frac = frac_matrix(close)
    frac_df = pd.DataFrame(frac, index=close.index, columns=close.columns)
    pct = close.pct_change(fill_method=None)
    vol = pct.rolling(120, min_periods=120).std() * np.sqrt(252)
    prev_frac = frac_df.shift(1)

    # 金叉 + skill 过滤
    cross = ((prev_frac <= 0.5) & (frac_df > 0.5)
             & (frac_df > FRAC_LO) & (frac_df <= FRAC_HI)
             & (vol > MIN_VOL) & (pct < LIMIT) & (amt >= 5e7))
    # 风向标
    g = pd.DataFrame({"date": close.index}).merge(gate, on="date", how="left")["gate"].ffill().fillna(0)
    cross = cross.mul(g.to_numpy()[:, None]).astype(bool)

    C = close.to_numpy()
    O = open_.to_numpy()
    H = high.to_numpy()
    C_ff = pd.DataFrame(C).ffill().to_numpy()
    n_days = len(close.index)
    dates = close.index

    cand_count = cross.sum(axis=1)
    print("=" * 96)
    print("科创50风向标 + skill选股 + 隔夜短线   %s ~ %s"
          % (dates[0].date(), dates[-1].date()))
    print("风向标开销: 科创50 frac>0.5 的天数 %d/%d (%.0f%%)"
          % (int(g.sum()), len(g), g.mean() * 100))
    print("金叉候选: 共 %d 个信号日, 日均 %.1f 只, 最多 %d 只"
          % (int((cand_count > 0).sum()), cand_count[cand_count > 0].mean() if (cand_count > 0).any() else 0,
             int(cand_count.max())))
    print("=" * 96)

    for tag, sell_arr in (("次日开盘卖", O), ("次日收盘卖", C), ("次日最高卖(乐观上界)", H)):
        capital = 1.0
        trades = []
        eq_curve = []
        for i in range(n_days - 1):
            cols = np.where(cross.to_numpy()[i])[0]
            if len(cols) == 0:
                eq_curve.append(capital)
                continue
            cols = cols[np.argsort(-frac[i, cols])][:TOP_N]
            per = capital / TOP_N
            day_pnl = 0.0
            for j in cols:
                bpx = C[i, j]
                spx = sell_arr[i + 1, j]
                if not np.isfinite(bpx) or not np.isfinite(spx) or bpx <= 0:
                    continue
                r = spx / bpx - 1 - 2 * SLIP
                day_pnl += per * r
                trades.append(r)
            capital += day_pnl
            eq_curve.append(capital)
        eq = np.array(eq_curve)
        rs = np.array(trades)
        yrs = (dates[-1] - dates[0]).days / 365.25
        total = eq[-1] - 1
        ann = (1 + total) ** (1 / yrs) - 1
        mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
        from scipy import stats as st
        t = float(st.ttest_1samp(rs, 0)[0]) if len(rs) >= 10 else 0.0
        print("\n[%s]" % tag)
        print("  交易 %d 笔  胜率 %.0f%%  均笔 %+.3f%%  中位 %+.3f%%  t=%.2f"
              % (len(rs), (rs > 0).mean() * 100 if len(rs) else 0, rs.mean() * 100 if len(rs) else 0,
                 np.median(rs) * 100 if len(rs) else 0, t if len(rs) >= 10 else 0))
        print("  累计 %+.1f%%  年化 %+.2f%%  MDD %.1f%%" % (total * 100, ann * 100, mdd * 100))

    print("\n" + "-" * 96)
    print("对照: 不加风向标(全时段)、不加skill过滤、随机5只 — 见前序实验。")
    print("判读: 若'开盘卖'口径为正 → 隔夜动量可兑现; 若仅'最高卖'为正 → 收益不可实现。")


if __name__ == "__main__":
    main()

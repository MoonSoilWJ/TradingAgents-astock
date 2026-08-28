import os
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ["NO_PROXY"] = "*"
import akshare as ak
import pandas as pd
import numpy as np

idx = ak.stock_zh_index_daily(symbol="sh000688")[["date", "close"]].copy()
idx["date"] = pd.to_datetime(idx["date"])
fund = ak.fund_open_fund_info_em(symbol="010623", indicator="单位净值走势")
fcols = list(fund.columns)
fund = fund[[fcols[0], fcols[1]]].copy()
fund.columns = ["date", "nav"]
fund["date"] = pd.to_datetime(fund["date"])
fund["nav"] = pd.to_numeric(fund["nav"], errors="coerce")
fund = fund.dropna()
df = pd.merge(idx, fund, on="date", how="inner").sort_values("date").reset_index(drop=True)
c = df["close"].values
nav = df["nav"].values


def trial(N, M):
    s = pd.Series(c)
    e1 = s.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr = e3.pct_change() * 100
    sig = tr.rolling(M).mean()
    gold = (tr > sig) & (tr.shift(1) <= sig.shift(1))
    dead = (tr < sig) & (tr.shift(1) >= sig.shift(1))
    cash = 1.0
    units = 0.0
    entry = None
    eq = []
    for i in range(len(c)):
        nd = nav[i]
        if i > 0 and gold.iloc[i] and units == 0:
            units = cash / nd
            cash = 0.0
            entry = i
        elif i > 0 and dead.iloc[i] and units > 0:
            amt = units * nd * (1 - (0.015 if (i - entry) < 7 else 0.0))
            cash = amt
            units = 0.0
            entry = None
        eq.append(cash + units * nd)
    eq = np.array(eq)
    ret = eq[-1] / eq[0] - 1
    yrs = len(c) / 252.0
    ann = (eq[-1] / eq[0]) ** (1 / max(yrs, 1e-9)) - 1
    peak = np.maximum.accumulate(eq)
    mdd = ((eq - peak) / peak).min()
    n = int(gold.sum())
    return ret, ann, mdd, n


print("N/M    累计     年化     最大回撤   金叉次数")
for N in [9, 12, 15, 20]:
    for M in [5, 9, 12, 20]:
        r, a, m, n = trial(N, M)
        tag = "   <== 当前默认" if (N, M) == (12, 9) else ""
        print(f"{N:>2}/{M:<2}  {r*100:>8.1f}%  {a*100:>7.1f}%  {m*100:>8.1f}%   {n:>3}{tag}")

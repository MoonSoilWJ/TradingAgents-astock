"""科创50日线TRIX -> 恒越成长精选C(010623) 定时调仓回测。"""
import os
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ["NO_PROXY"] = "*"
import akshare as ak
import pandas as pd
import numpy as np

FUND = "010623"
INDEX = "sh000688"
START = "20200101"
END = "20260827"
TRIX_N = 12
TRIX_M = 9
STEP = 0.2          # 每次买卖 = 总股本的 1/5
PENALTY_7D = 0.015  # 持有<7天赎回费
INVERT = True       # True: 金叉卖/死叉买 (高卖低买); False: 金叉买/死叉卖


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def trix_signals(close):
    e1 = ema(close, TRIX_N)
    e2 = ema(e1, TRIX_N)
    e3 = ema(e2, TRIX_N)
    tr = e3.pct_change() * 100.0
    sig = tr.rolling(TRIX_M).mean()
    golden = (tr > sig) & (tr.shift(1) <= sig.shift(1))
    death = (tr < sig) & (tr.shift(1) >= sig.shift(1))
    if INVERT:
        sell_sig = golden
        buy_sig = death
    else:
        sell_sig = death
        buy_sig = golden
    return buy_sig, sell_sig


def load_index():
    df = ak.stock_zh_index_daily(symbol=INDEX)
    df = df[["date", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df[(df["date"] >= pd.Timestamp(START)) & (df["date"] <= pd.Timestamp(END))]
    df = df.sort_values("date").reset_index(drop=True)
    return df


def load_fund():
    df = ak.fund_open_fund_info_em(symbol=FUND, indicator="单位净值走势")
    cols = list(df.columns)
    date_col = "净值日期" if "净值日期" in cols else cols[0]
    nav_col = "单位净值" if "单位净值" in cols else cols[1]
    df = df[[date_col, nav_col]].copy()
    df.columns = ["date", "nav"]
    df["date"] = pd.to_datetime(df["date"])
    df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
    df = df.dropna()
    df = df[(df["date"] >= pd.Timestamp(START)) & (df["date"] <= pd.Timestamp(END))]
    df = df.sort_values("date").reset_index(drop=True)
    return df


def simulate(idx_close, nav_series, dates):
    buy, sell = trix_signals(idx_close)
    cash = 1.0
    units = 0.0
    lots = []
    equity = []
    fee_paid = 0.0
    n_trades = 0
    n_penalty = 0
    penalty_amt = 0.0

    for i, d in enumerate(dates):
        nav = float(nav_series.loc[d])
        total = cash + units * nav

        if i > 0 and bool(buy.loc[d]):
            target_fund = total * STEP
            if target_fund > units * nav:
                add = min(target_fund - units * nav, cash)
                if add > 1e-9:
                    u = add / nav
                    units += u
                    cash -= add
                    lots.append([d, u, nav])
                    n_trades += 1
        elif i > 0 and bool(sell.loc[d]):
            target_cash = total * STEP
            if units * nav > target_cash and units > 1e-12:
                rm_value = min(target_cash, units * nav)
                rm_units = rm_value / nav
                rm_units = min(rm_units, units)
                remaining = rm_units
                proceeds_total = 0.0
                while remaining > 1e-12 and lots:
                    lot = lots[0]
                    ld, lu, ln = lot
                    take = min(lu, remaining)
                    age = (d - ld).days
                    pf = PENALTY_7D if age < 7 else 0.0
                    proceeds_total += take * nav * (1 - pf)
                    if pf > 0:
                        n_penalty += 1
                        penalty_amt += take * nav * pf
                    lu -= take
                    remaining -= take
                    if lu <= 1e-12:
                        lots.pop(0)
                    else:
                        lots[0][1] = lu
                cash += proceeds_total
                units -= rm_units
                fee_paid += rm_units * nav - proceeds_total
                n_trades += 1

        equity.append(cash + units * nav)

    eq = pd.Series(equity, index=dates)
    return eq, dict(n_trades=n_trades, n_penalty=n_penalty, penalty_amt=penalty_amt, fee_paid=fee_paid)


def metrics(eq):
    eq = eq.dropna()
    ret = eq.iloc[-1] / eq.iloc[0] - 1
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    ann = (eq.iloc[-1] / eq.iloc[0]) ** (1 / max(years, 1e-9)) - 1
    peak = eq.cummax()
    dd = (eq - peak) / peak
    mdd = dd.min()
    return ret, ann, mdd


def main():
    print("加载科创50日线 ...")
    idx = load_index()
    print("  指数交易日数:", len(idx), idx["date"].min().date(), "~", idx["date"].max().date())
    print("加载基金净值 ...")
    fund = load_fund()
    print("  基金净值点数:", len(fund), fund["date"].min().date(), "~", fund["date"].max().date())

    idx = idx.set_index("date")["close"]
    fund = fund.set_index("date")["nav"]
    common = idx.index.intersection(fund.index)
    idx = idx.loc[common]
    fund = fund.loc[common]
    print("  共同交易日:", len(common))

    eq, stat = simulate(idx, fund, list(common))
    ret, ann, mdd = metrics(eq)

    bench = fund / fund.iloc[0]
    bench_ret = bench.iloc[-1] - 1

    print("\n=============== 回测结果 (方向: 金叉卖/死叉买, 高卖低买) ===============")
    print("区间:", common.min().date(), "~", common.max().date())
    print(f"策略累计收益 : {ret*100:.2f}%")
    print(f"策略年化     : {ann*100:.2f}%")
    print(f"策略最大回撤 : {mdd*100:.2f}%")
    print(f"基准(满仓持有基金)累计收益: {bench_ret*100:.2f}%")
    print(f"总交易次数   : {stat['n_trades']}")
    print(f"触发<7天赎回费的笔数: {stat['n_penalty']}")
    print(f"<7天赎回费总额(占期初): {stat['penalty_amt']*100:.4f}%")
    print(f"总赎回费(含惩罚): {stat['fee_paid']*100:.4f}%")
    print("============================================================")

    global INVERT
    INVERT = False
    eq2, stat2 = simulate(idx, fund, list(common))
    r2, a2, m2 = metrics(eq2)
    print("\n[对照] 标准方向(金叉买/死叉卖) 累计:", f"{r2*100:.2f}%  年化:", f"{a2*100:.2f}%  回撤:", f"{m2*100:.2f}%")


if __name__ == "__main__":
    main()

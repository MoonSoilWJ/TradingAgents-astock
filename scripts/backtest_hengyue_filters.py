"""科创50 TRIX/趋势过滤 -> 恒越成长精选C(010623) 多方案回测对比。"""
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
STEP = 0.2
PENALTY_7D = 0.015


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def trix(close):
    e1 = ema(close, TRIX_N)
    e2 = ema(e1, TRIX_N)
    e3 = ema(e2, TRIX_N)
    return e3.pct_change() * 100.0


def load_index():
    df = ak.stock_zh_index_daily(symbol=INDEX)
    df = df[["date", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df[(df["date"] >= pd.Timestamp(START)) & (df["date"] <= pd.Timestamp(END))]
    return df.sort_values("date").reset_index(drop=True)


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
    return df.sort_values("date").reset_index(drop=True)


def sim_event(dates, nav, buy_event, sell_event):
    """买卖事件: 触发当日 1/5 步进 (买加 20% 总仓 / 卖减 20% 总仓), FIFO, <7天罚。"""
    cash = 1.0
    units = 0.0
    lots = []
    eq = []
    pen = 0
    for i, d in enumerate(dates):
        nav_d = float(nav.loc[d])
        total = cash + units * nav_d
        if i > 0 and bool(buy_event.loc[d]):
            target = total * STEP
            if target > units * nav_d:
                add = min(target - units * nav_d, cash)
                if add > 1e-9:
                    units += add / nav_d
                    cash -= add
                    lots.append([d, add / nav_d, nav_d])
        elif i > 0 and bool(sell_event.loc[d]):
            target_cash = total * STEP
            if units * nav_d > target_cash and units > 1e-12:
                rm_value = min(target_cash, units * nav_d)
                rm_units = min(rm_value / nav_d, units)
                remaining = rm_units
                proceeds = 0.0
                while remaining > 1e-12 and lots:
                    ld, lu, ln = lots[0]
                    take = min(lu, remaining)
                    pf = PENALTY_7D if (d - ld).days < 7 else 0.0
                    proceeds += take * nav_d * (1 - pf)
                    if pf > 0:
                        pen += 1
                    lu -= take
                    remaining -= take
                    if lu <= 1e-12:
                        lots.pop(0)
                    else:
                        lots[0][1] = lu
                cash += proceeds
                units -= rm_units
        eq.append(cash + units * nav_d)
    return pd.Series(eq, index=dates), pen


def sim_target(dates, nav, target):
    """目标仓位 0/1: 每日再平衡到目标暴露 (满仓/空仓型)。"""
    cash = 1.0
    units = 0.0
    lots = []
    eq = []
    pen = 0
    for i, d in enumerate(dates):
        nav_d = float(nav.loc[d])
        frac = max(0.0, min(1.0, float(target.loc[d])))
        want = frac * (cash + units * nav_d)
        if want > units * nav_d:
            add = min(want - units * nav_d, cash)
            if add > 1e-9:
                units += add / nav_d
                cash -= add
                lots.append([d, add / nav_d, nav_d])
        elif units * nav_d > want + 1e-9:
            rm_value = units * nav_d - want
            rm_units = min(rm_value / nav_d, units)
            remaining = rm_units
            proceeds = 0.0
            while remaining > 1e-12 and lots:
                ld, lu, ln = lots[0]
                take = min(lu, remaining)
                pf = PENALTY_7D if (d - ld).days < 7 else 0.0
                proceeds += take * nav_d * (1 - pf)
                if pf > 0:
                    pen += 1
                lu -= take
                remaining -= take
                if lu <= 1e-12:
                    lots.pop(0)
                else:
                    lots[0][1] = lu
            cash += proceeds
            units -= rm_units
        eq.append(cash + units * nav_d)
    return pd.Series(eq, index=dates), pen


def metrics(eq):
    eq = eq.dropna()
    ret = eq.iloc[-1] / eq.iloc[0] - 1
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    ann = (eq.iloc[-1] / eq.iloc[0]) ** (1 / max(years, 1e-9)) - 1
    mdd = ((eq - eq.cummax()) / eq.cummax()).min()
    return ret, ann, mdd


def run():
    idx = load_index().set_index("date")["close"]
    fund = load_fund().set_index("date")["nav"]
    common = idx.index.intersection(fund.index)
    idx = idx.loc[common]
    fund = fund.loc[common]
    close = idx
    dates = list(common)
    print("共同交易日:", len(dates), common.min().date(), "~", common.max().date())

    tr = trix(close)
    sig = tr.rolling(TRIX_M).mean()
    golden = (tr > sig) & (tr.shift(1) <= sig.shift(1))
    death = (tr < sig) & (tr.shift(1) >= sig.shift(1))
    ma120 = close.rolling(120).mean()
    ma60 = close.rolling(60).mean()
    up120 = close > ma120
    up60 = close > ma60
    cross_up120 = up120 & (~up120.shift(1).fillna(False))
    cross_dn120 = (~up120) & (up120.shift(1).fillna(False))
    cross_up60 = up60 & (~up60.shift(1).fillna(False))
    cross_dn60 = (~up60) & (up60.shift(1).fillna(False))

    bench = fund / fund.iloc[0]
    bret, bann, bmdd = metrics(bench)

    print(f"\n{'策略':<38}{'累计%':>10}{'年化%':>10}{'最大回撤%':>12}{'<7天罚笔':>10}")
    print("-" * 82)

    def show_event(name, b, s):
        eq, pen = sim_event(dates, fund, b, s)
        r, a, m = metrics(eq)
        print(f"{name:<38}{r*100:>10.2f}{a*100:>10.2f}{m*100:>12.2f}{pen:>10}")

    def show_target(name, target):
        eq, pen = sim_target(dates, fund, target.astype(float))
        r, a, m = metrics(eq)
        print(f"{name:<38}{r*100:>10.2f}{a*100:>10.2f}{m*100:>12.2f}{pen:>10}")

    print(f"{'持有(满仓不动)':<38}{bret*100:>10.2f}{bann*100:>10.2f}{bmdd*100:>12.2f}{0:>10}")
    show_event("TRIX标准(金叉买/死叉卖)", golden, death)
    show_event("TRIX反向(金叉卖/死叉买)", death, golden)
    show_event("TRIX+MA120过滤买入(买须>MA120)", golden & up120, death)
    show_event("TRIX+MA120门槛(买须多头/破位即卖)", golden & up120, death | (~up120))
    show_event("MA120交叉(买上穿/卖下穿)", cross_up120, cross_dn120)
    show_event("MA60交叉(买上穿/卖下穿)", cross_up60, cross_dn60)
    show_target("MA120状态(满仓/空仓)", up120)
    show_target("MA60状态(满仓/空仓)", up60)

    # A: TRIX 金叉满仓 / 死叉空仓 (按交叉切换, 非1/5步进)
    target_a = pd.Series(0.0, index=dates)
    last = 0.0
    for i, d in enumerate(dates):
        if bool(golden.loc[d]):
            last = 1.0
        elif bool(death.loc[d]):
            last = 0.0
        target_a.loc[d] = last
    show_target("A:TRIX交叉满仓/空仓(金叉满/死叉空)", target_a)

    # B: TRIX 状态法 (柱>0 满仓, <=0 空仓)
    target_b = (tr > 0).astype(float)
    show_target("B:TRIX状态(柱>0满仓/<=0空仓)", target_b)

    # 补充: 双过滤状态 (TRIX>0 且 科创50>MA120 才满仓)
    target_c = ((tr > 0) & up120).astype(float)
    show_target("C:TRIX>0且>MA120 双过滤状态", target_c)


if __name__ == "__main__":
    run()

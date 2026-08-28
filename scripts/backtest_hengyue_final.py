"""科创50 TRIX 金叉满仓/死叉空仓 -> 恒越成长精选C(010623) 现实版回测。
执行模型: 14:40 依据当日可得信息判信号, 15:00 按净值成交(拿得到T+1收益)。
含 FIFO 份额跟踪、<7天赎回费、可配置滑点、逐笔交易明细、收益曲线。
"""
import os
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ["NO_PROXY"] = "*"
import akshare as ak
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

FUND = "010623"
INDEX = "sh000688"
START = "20200101"
END = "20260827"
TRIX_N = 12
TRIX_M = 9
PENALTY_7D = 0.015
SLIP = 0.0005  # 执行滑点(保守): 14:40快照与15:00净值的偏差 + 其它摩擦, 可调0


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


def simulate(dates, nav, golden, death):
    """金叉满仓 / 死叉空仓。返回 (权益序列, 交易明细列表)。"""
    cash = 1.0
    units = 0.0
    lots = []          # [买入日, 份额, 净值]
    eq = []
    trades = []        # 逐笔记录
    pos = 0            # 当前是否满仓(状态)
    entry_date = None
    entry_nav = None
    for i, d in enumerate(dates):
        nav_d = float(nav.loc[d])
        # 状态机: 金叉置满仓, 死叉置空仓 (只在交叉日切换)
        if i > 0 and bool(golden.loc[d]):
            if pos == 0:
                # 买入 -> 满仓
                add = cash
                if add > 1e-9:
                    u = add / nav_d
                    units += u
                    cash -= add
                    cash -= add * SLIP          # 滑点摩擦
                    lots.append([d, u, nav_d])
                    entry_date = d
                    entry_nav = nav_d
                    pos = 1
                    trades.append({"日期": d, "动作": "买入(金叉)", "净值": nav_d,
                                   "本次金额": add, "买入份额": u,
                                   "现金": cash, "总权益": cash + units * nav_d})
        elif i > 0 and bool(death.loc[d]):
            if pos == 1:
                # 卖出 -> 空仓
                rm_units = units
                remaining = rm_units
                proceeds = 0.0
                pen_cnt = 0
                pen_amt = 0.0
                while remaining > 1e-12 and lots:
                    ld, lu, ln = lots[0]
                    take = min(lu, remaining)
                    pf = PENALTY_7D if (d - ld).days < 7 else 0.0
                    p = take * nav_d * (1 - pf)
                    proceeds += p
                    if pf > 0:
                        pen_cnt += 1
                        pen_amt += take * nav_d * pf
                    lu -= take
                    remaining -= take
                    if lu <= 1e-12:
                        lots.pop(0)
                    else:
                        lots[0][1] = lu
                cash += proceeds
                cash -= proceeds * SLIP        # 滑点摩擦
                units -= rm_units
                hold_days = (d - entry_date).days
                pnl = rm_units * nav_d - (rm_units * entry_nav)
                trades.append({"日期": d, "动作": "卖出(死叉)", "净值": nav_d,
                               "本次金额": proceeds, "赎回份额": rm_units,
                               "现金": cash, "总权益": cash + units * nav_d,
                               "持仓天数": hold_days, "笔笔盈亏": pnl})
                pos = 0
                entry_date = None
                entry_nav = None
        eq.append(cash + units * nav_d)
    return pd.Series(eq, index=dates), trades


def metrics(eq):
    eq = eq.dropna()
    ret = eq.iloc[-1] / eq.iloc[0] - 1
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    ann = (eq.iloc[-1] / eq.iloc[0]) ** (1 / max(years, 1e-9)) - 1
    mdd = ((eq - eq.cummax()) / eq.cummax()).min()
    return ret, ann, mdd


def main():
    idx = load_index().set_index("date")["close"]
    fund = load_fund().set_index("date")["nav"]
    common = idx.index.intersection(fund.index)
    idx = idx.loc[common]
    fund = fund.loc[common]
    dates = list(common)
    print("共同交易日:", len(dates), common.min().date(), "~", common.max().date(),
          f"  滑点假设 SLIP={SLIP}")

    tr = trix(idx)
    sig = tr.rolling(TRIX_M).mean()
    golden = (tr > sig) & (tr.shift(1) <= sig.shift(1))
    death = (tr < sig) & (tr.shift(1) >= sig.shift(1))

    bench = fund / fund.iloc[0]
    bret, bann, bmdd = metrics(bench)

    eq, trades = simulate(dates, fund, golden, death)
    sret, sann, smdd = metrics(eq)

    print("\n====== 绩效对比 ======")
    print(f"{'指标':<14}{'策略(TRIX满仓/空仓)':>20}{'基准(持有)':>14}")
    print(f"{'累计收益':<14}{sret*100:>18.2f}%{bret*100:>13.2f}%")
    print(f"{'年化收益':<14}{sann*100:>18.2f}%{bann*100:>13.2f}%")
    print(f"{'最大回撤':<14}{smdd*100:>18.2f}%{bmdd*100:>13.2f}%")
    print(f"{'期末权益(期初=1)':<14}{eq.iloc[-1]:>18.3f}{bench.iloc[-1]:>13.3f}")

    print(f"\n====== 逐笔交易明细 (共 {len(trades)} 笔) ======")
    print(f"{'日期':<12}{'动作':<14}{'净值':>9}{'金额':>12}{'份额':>12}{'现金':>12}{'总权益':>12}{'持仓天数':>10}")
    for t in trades:
        hd = t.get("持仓天数", "")
        print(f"{t['日期'].date()!s:<12}{t['动作']:<14}{t['净值']:>9.4f}"
              f"{t['本次金额']:>12.4f}{(t.get('买入份额') or t.get('赎回份额') or 0):>12.4f}"
              f"{t['现金']:>12.4f}{t['总权益']:>12.4f}{str(hd):>10}")

    # 收益曲线
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
    os.makedirs(out_dir, exist_ok=True)
    out_png = os.path.join(out_dir, "equity_curve.png")
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(eq.index, eq.values, label=f"TRIX In/Out (full) {sret*100:.1f}%", lw=1.6, color="tab:blue")
    ax.plot(bench.index, bench.values, label=f"Buy&Hold {bret*100:.1f}%", lw=1.2, color="gray", alpha=0.7)
    ax.set_title("Hengyue Growth C (010623) — STAR50 TRIX Timing")
    ax.set_ylabel("Net Value (start=1)")
    ax.legend()
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    print(f"\n收益曲线已保存: {os.path.abspath(out_png)}")

    # 交易明细落盘 CSV
    out_csv = os.path.join(out_dir, "trades_detail.csv")
    pd.DataFrame(trades).to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"逐笔交易明细 CSV: {os.path.abspath(out_csv)}")


if __name__ == "__main__":
    main()

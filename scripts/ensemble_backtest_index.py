"""投票版, 但交易标的=科创50指数本身(用指数收盘买卖, 无基金7天赎回费)。
同一时间段, 与基金版对照。
"""
import os
os.environ.pop("http_proxy", None); os.environ.pop("https_proxy", None); os.environ["NO_PROXY"]="*"
import akshare as ak
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

INDEX = "sh000688"
START = "20210201"  # 与恒越基金(010623)上市时间对齐
PENALTY = 0.0      # 直接交易指数, 无7天赎回费
SLIP = 0.0005
COMBOS = [(9, 9), (9, 12), (12, 9), (12, 12), (15, 9), (15, 12), (20, 9)]


def load():
    idx = ak.stock_zh_index_daily(symbol=INDEX)[["date", "close"]].copy()
    idx["date"] = pd.to_datetime(idx["date"])
    idx = idx[(idx["date"] >= pd.Timestamp(START))].reset_index(drop=True)
    return idx


def trix_pos(close, N, M):
    s = pd.Series(close)
    e1 = s.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr = e3.pct_change() * 100
    sig = tr.rolling(M).mean()
    return (tr > sig).astype(int).values


def simulate(target, price):
    cash = 1.0; units = 0.0; entry = None; eq = []
    pos = 0
    for i in range(len(price)):
        nd = price[i]
        tgt = int(target[i])
        if i > 0 and tgt == 1 and pos == 0:
            fee = cash * SLIP
            units = (cash - fee) / nd
            cash = 0.0; entry = i; pos = 1
        elif i > 0 and tgt == 0 and pos == 1:
            amt = units * nd * (1 - PENALTY)
            fee = amt * SLIP
            cash = amt - fee; units = 0.0; entry = None; pos = 0
        eq.append(cash + units * nd)
    return np.array(eq)


def metrics(eq):
    eq = np.array(eq)
    ret = eq[-1] / eq[0] - 1
    yrs = len(eq) / 252.0
    ann = (eq[-1] / eq[0]) ** (1 / max(yrs, 1e-9)) - 1
    peak = np.maximum.accumulate(eq)
    mdd = ((eq - peak) / peak).min()
    return ret, ann, mdd


def main():
    df = load()
    close = df["close"].values
    dates = df["date"].values

    states = np.column_stack([trix_pos(close, n, m) for (n, m) in COMBOS])
    vote = states.mean(axis=1) > 0.5
    target = vote.astype(int)

    bench = close / close[0]          # 持有指数
    single = trix_pos(close, 12, 9)

    eq_ens = simulate(target, close)
    eq_sin = simulate(single, close)

    re, ae, me = metrics(eq_ens)
    rs, as_, ms = metrics(eq_sin)
    rb, ab, mb = metrics(bench)

    print(f"交易标的=科创50指数本身  组合{COMBOS}  滑点={SLIP} 无7天费\n")
    print(f"{'指标':<10}{'多参数投票':>14}{'单组12/9':>14}{'持有指数':>12}")
    print(f"{'累计':<10}{re*100:>12.1f}%{rs*100:>13.1f}%{rb*100:>11.1f}%")
    print(f"{'年化':<10}{ae*100:>12.1f}%{as_*100:>13.1f}%{ab*100:>11.1f}%")
    print(f"{'最大回撤':<10}{me*100:>12.1f}%{ms*100:>13.1f}%{mb*100:>11.1f}%")
    print(f"{'期末权益':<10}{eq_ens[-1]:>14.3f}{eq_sin[-1]:>14.3f}{bench[-1]:>12.3f}")

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
    os.makedirs(out_dir, exist_ok=True)
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(dates, eq_ens, label=f"Ensemble Vote {re*100:.1f}%", lw=1.6, color="tab:blue")
    ax.plot(dates, eq_sin, label=f"Single 12/9 {rs*100:.1f}%", lw=1.2, color="tab:green", alpha=0.8)
    ax.plot(dates, bench, label=f"Hold Index {rb*100:.1f}%", lw=1.2, color="gray", alpha=0.7)
    ax.set_title("STAR50 Index itself — TRIX Ensemble Voting")
    ax.set_ylabel("Net Value (start=1)")
    ax.legend(); ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate(); fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "index_ensemble_equity.png"), dpi=120)
    print(f"\n曲线图: {os.path.abspath(os.path.join(out_dir,'index_ensemble_equity.png'))}")


if __name__ == "__main__":
    main()

"""多参数投票版: 同时跑多组 TRIX(N,M), 每天投票决定满仓/空仓。
金叉口径=TRIX上穿信号线; 金叉看多(买入), 死叉看空(卖出)。
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
PENALTY_7D = 0.015
SLIP = 0.0005
COMBOS = [(9, 9), (9, 12), (12, 9), (12, 12), (15, 9), (15, 12), (20, 9)]


def load():
    idx = ak.stock_zh_index_daily(symbol=INDEX)[["date", "close"]].copy()
    idx["date"] = pd.to_datetime(idx["date"])
    fund = ak.fund_open_fund_info_em(symbol=FUND, indicator="单位净值走势")
    fcols = list(fund.columns)
    fund = fund[[fcols[0], fcols[1]]].copy()
    fund.columns = ["date", "nav"]
    fund["date"] = pd.to_datetime(fund["date"])
    fund["nav"] = pd.to_numeric(fund["nav"], errors="coerce")
    fund = fund.dropna()
    df = pd.merge(idx, fund, on="date", how="inner").sort_values("date").reset_index(drop=True)
    return df


def trix_pos(close, N, M):
    s = pd.Series(close)
    e1 = s.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr = e3.pct_change() * 100
    sig = tr.rolling(M).mean()
    return (tr > sig).astype(int).values  # 1=看多, 0=看空


def simulate(target, nav):
    cash = 1.0
    units = 0.0
    entry = None
    eq = []
    trades = []
    pos = 0
    for i in range(len(nav)):
        nd = nav[i]
        tgt = int(target[i])
        if i > 0 and tgt == 1 and pos == 0:
            fee = cash * SLIP
            units = (cash - fee) / nd
            cash = 0.0
            entry = i
            pos = 1
            trades.append((i, "买入", nd, cash + units * nd))
        elif i > 0 and tgt == 0 and pos == 1:
            pen = PENALTY_7D if (i - entry) < 7 else 0.0
            amt = units * nd * (1 - pen)
            fee = amt * SLIP
            cash = amt - fee
            trades.append((i, f"卖出(罚{pen*100:.1f}%)" if pen else "卖出", nd, cash))
            units = 0.0
            entry = None
            pos = 0
        eq.append(cash + units * nd)
    return np.array(eq), trades


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
    nav = df["nav"].values
    dates = df["date"].values

    # 各组合状态
    states = np.column_stack([trix_pos(close, n, m) for (n, m) in COMBOS])
    vote = states.mean(axis=1) > 0.5  # 过半看多
    ensemble_target = vote.astype(int)

    # 基准们
    bench = nav / nav[0]
    single = trix_pos(close, 12, 9)  # 单组12/9

    eq_ens, tr_ens = simulate(ensemble_target, nav)
    eq_sin, tr_sin = simulate(single, nav)

    re, ae, me = metrics(eq_ens)
    rs, as_, ms = metrics(eq_sin)
    rb, ab, mb = metrics(bench)

    print(f"组合(投票) {COMBOS}  滑点={SLIP}\n")
    print(f"{'指标':<10}{'多参数投票':>14}{'单组12/9':>14}{'持有':>12}")
    print(f"{'累计':<10}{re*100:>12.1f}%{rs*100:>13.1f}%{rb*100:>11.1f}%")
    print(f"{'年化':<10}{ae*100:>12.1f}%{as_*100:>13.1f}%{ab*100:>11.1f}%")
    print(f"{'最大回撤':<10}{me*100:>12.1f}%{ms*100:>13.1f}%{mb*100:>11.1f}%")
    print(f"{'期末权益':<10}{eq_ens[-1]:>14.3f}{eq_sin[-1]:>14.3f}{bench[-1]:>12.3f}")
    print(f"{'切换次数':<10}{len(tr_ens):>14}{len(tr_sin):>14}{'-':>12}")

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
    os.makedirs(out_dir, exist_ok=True)
    # 画图
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(dates, eq_ens, label=f"Ensemble Vote {re*100:.1f}%", lw=1.6, color="tab:blue")
    ax.plot(dates, eq_sin, label=f"Single 12/9 {rs*100:.1f}%", lw=1.2, color="tab:green", alpha=0.8)
    ax.plot(dates, bench, label=f"Buy&Hold {rb*100:.1f}%", lw=1.2, color="gray", alpha=0.7)
    ax.set_title("Hengyue Growth C (010623) — STAR50 TRIX Ensemble Voting")
    ax.set_ylabel("Net Value (start=1)")
    ax.legend(); ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate(); fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "ensemble_equity.png"), dpi=120)
    print(f"\n曲线图: {os.path.abspath(os.path.join(out_dir,'ensemble_equity.png'))}")
    # 投票明细CSV(每日看多占比)
    vote_df = pd.DataFrame({"date": dates, "long_ratio": states.mean(axis=1)})
    vote_df.to_csv(os.path.join(out_dir, "vote_ratio.csv"), index=False)
    print(f"每日看多占比: {os.path.abspath(os.path.join(out_dir,'vote_ratio.csv'))}")

    # 双轴图: 上=净值, 下=看多共识度
    long_ratio = states.mean(axis=1)
    fig2, ax_top = plt.subplots(figsize=(12, 7))
    ax_top.plot(dates, eq_ens, label=f"Ensemble Vote {re*100:.1f}%", lw=1.6, color="tab:blue")
    ax_top.plot(dates, bench, label=f"Buy&Hold {rb*100:.1f}%", lw=1.2, color="gray", alpha=0.7)
    ax_top.set_ylabel("Net Value (start=1)")
    ax_top.legend(loc="upper left"); ax_top.grid(alpha=0.3)
    ax_bot = ax_top.twinx()
    ax_bot.fill_between(dates, long_ratio, 0.5,
                        where=(long_ratio >= 0.5), color="tab:green", alpha=0.25,
                        interpolate=True, label="满仓区(>=0.5)")
    ax_bot.fill_between(dates, long_ratio, 0.5,
                        where=(long_ratio < 0.5), color="tab:red", alpha=0.20,
                        interpolate=True, label="空仓区(<0.5)")
    ax_bot.plot(dates, long_ratio, lw=0.8, color="tab:purple", alpha=0.8)
    ax_bot.axhline(0.5, color="k", ls="--", lw=1.0, label="阈值 0.5")
    ax_bot.set_ylabel("看多共识度 (long_ratio)")
    ax_bot.set_ylim(-0.05, 1.05)
    ax_bot.legend(loc="lower left")
    ax_top.set_title("Ensemble Voting — Net Value (top) & Long Consensus (bottom)")
    ax_top.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig2.autofmt_xdate(); fig2.tight_layout()
    fig2.savefig(os.path.join(out_dir, "ensemble_dual_axis.png"), dpi=120)
    print(f"双轴图: {os.path.abspath(os.path.join(out_dir,'ensemble_dual_axis.png'))}")


if __name__ == "__main__":
    main()

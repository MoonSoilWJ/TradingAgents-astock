"""通用: 以某基金自身净值同时作为 信号源 与 交易标的 (self-signal)。
科创50 TRIX 多参数投票。窗口与恒越一致(2021-02 起)。
"""
import os, sys
os.environ.pop("http_proxy", None); os.environ.pop("https_proxy", None); os.environ["NO_PROXY"]="*"
import akshare as ak
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

FUND = sys.argv[1] if len(sys.argv) > 1 else "161129"
MODE = sys.argv[2] if len(sys.argv) > 2 else "fund"
START = "20210201"
SLIP = 0.0005
COMBOS = [(9, 9), (9, 12), (12, 9), (12, 12), (15, 9), (15, 12), (20, 9)]


def load_nav(code):
    if MODE == "etf":
        from pytdx.hq import TdxHq_API
        from pytdx.params import TDXParams
        api = TdxHq_API()
        ok = False
        for ip, port in [("180.153.18.170", 7709), ("114.80.63.12", 7709), ("123.125.108.2", 7709)]:
            try:
                if api.connect(ip, port, time_out=5):
                    ok = True
                    break
            except Exception:
                pass
        if not ok:
            raise RuntimeError("TDX 行情服务器连接失败")
        mkt = TDXParams.MARKET_SH if code.startswith(("5", "6")) else TDXParams.MARKET_SZ
        PAGES = 8  # 800*8=6400根, 覆盖多年
        frames = []
        for start in range(0, PAGES * 800, 800):
            k = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, mkt, code.encode(), start, 800)
            if k is None:
                break
            d = api.to_df(k)
            if d is None or len(d) == 0:
                break
            frames.append(d)
            if len(d) < 800:
                break
        api.disconnect()
        f = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        cols = {c.lower(): c for c in f.columns}
        date_col = cols.get("datetime", cols.get("date"))
        close_col = cols.get("close")
        f = f[[date_col, close_col]].copy()
        f.columns = ["date", "nav"]
        f["date"] = pd.to_datetime(f["date"])
        f["nav"] = pd.to_numeric(f["nav"], errors="coerce")
        f = f.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    else:
        f = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        cols = list(f.columns)
        f = f[[cols[0], cols[1]]].copy()
        f.columns = ["date", "nav"]
        f["date"] = pd.to_datetime(f["date"])
        f["nav"] = pd.to_numeric(f["nav"], errors="coerce")
    f = f.dropna()
    f = f[f["date"] >= pd.Timestamp(START)].sort_values("date").reset_index(drop=True)
    return f


def trix_pos(close, N, M):
    s = pd.Series(close)
    e1 = s.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr = e3.pct_change() * 100
    sig = tr.rolling(M).mean()
    return (tr > sig).astype(int).values


def simulate(target, price, penalty):
    cash = 1.0; units = 0.0; entry = None; eq = []; pos = 0
    for i in range(len(price)):
        nd = price[i]; tgt = int(target[i])
        if i > 0 and tgt == 1 and pos == 0:
            fee = cash * SLIP
            units = (cash - fee) / nd; cash = 0.0; entry = i; pos = 1
        elif i > 0 and tgt == 0 and pos == 1:
            pen = penalty if (i - entry) < 7 else 0.0
            amt = units * nd * (1 - pen)
            fee = amt * SLIP; cash = amt - fee; units = 0.0; entry = None; pos = 0
        eq.append(cash + units * nd)
    return np.array(eq)


def metrics(eq):
    eq = np.array(eq); ret = eq[-1]/eq[0]-1; yrs = len(eq)/252.0
    ann = (eq[-1]/eq[0])**(1/max(yrs,1e-9))-1
    peak = np.maximum.accumulate(eq); mdd = ((eq-peak)/peak).min()
    return ret, ann, mdd


def main():
    f = load_nav(FUND)
    nav = f["nav"].values; dates = f["date"].values
    print(f"标的={FUND}  区间 {pd.Timestamp(dates[0]).date()} ~ {pd.Timestamp(dates[-1]).date()}  共{len(nav)}日")

    states = np.column_stack([trix_pos(nav, n, m) for (n, m) in COMBOS])
    target = (states.mean(1) > 0.5).astype(int)
    bench = nav / nav[0]
    single = trix_pos(nav, 12, 9)

    for penalty, tag in [(0.0, "无7天费(ETF/LOF二级市价口径)"), (0.015, "含7天赎回费1.5%(基金赎回口径)")]:
        eq_e = simulate(target, nav, penalty)
        eq_s = simulate(single, nav, penalty)
        re, ae, me = metrics(eq_e); rs, as_, ms = metrics(eq_s); rb, ab, mb = metrics(bench)
        print(f"\n--- {tag} ---")
        print(f"{'指标':<10}{'投票':>12}{'单12/9':>12}{'持有':>10}")
        print(f"{'累计':<10}{re*100:>10.1f}%{rs*100:>11.1f}%{rb*100:>9.1f}%")
        print(f"{'年化':<10}{ae*100:>10.1f}%{as_*100:>11.1f}%{ab*100:>9.1f}%")
        print(f"{'最大回撤':<10}{me*100:>10.1f}%{ms*100:>11.1f}%{mb*100:>9.1f}%")
        print(f"{'期末权益':<10}{eq_e[-1]:>12.3f}{eq_s[-1]:>12.3f}{bench[-1]:>10.3f}")

    # 画图(无费版)
    eq_e = simulate(target, nav, 0.0)
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
    os.makedirs(out_dir, exist_ok=True)
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(dates, eq_e, label=f"Vote {metrics(eq_e)[0]*100:.1f}%", lw=1.6, color="tab:blue")
    ax.plot(dates, bench, label=f"Hold {metrics(bench)[0]*100:.1f}%", lw=1.2, color="gray", alpha=0.7)
    ax.set_title(f"{FUND} — self TRIX ensemble voting")
    ax.set_ylabel("Net Value (start=1)"); ax.legend(); ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate(); fig.tight_layout()
    p = os.path.join(out_dir, f"{FUND}_self_signal.png")
    fig.savefig(p, dpi=120)
    print(f"\n曲线图: {os.path.abspath(p)}")


if __name__ == "__main__":
    main()

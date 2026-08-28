# 查看 588000 日线 N12 簇 当前(最近一根日线) 的 TRIX / 信号线 / 金叉状态.
# 盘中运行 -> 最后一根为当日近似收盘; 收盘后运行 -> 为当日定稿收盘.
#
# 用法:
#   python3 scripts/trix_n12_now.py
import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

COMB_N12 = [(10, 9), (10, 12), (12, 9), (12, 12), (14, 9), (14, 12)]


def trix_series(c, N, M):
    s = pd.Series(c, dtype=float)
    e1 = s.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr = e3.pct_change() * 100
    sig = tr.rolling(M).mean()
    return tr.values, sig.values


def fetch_day(n=800):
    api = TdxHq_API()
    api.connect("180.153.18.170", 7709, time_out=5)
    frames = []
    for pg in range(20):
        k = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, TDXParams.MARKET_SH, b"588000", pg * 700, 700)
        if k is None:
            break
        d = api.to_df(k)
        if d is None or len(d) == 0:
            break
        frames.append(d)
        if len(d) < 700:
            break
    api.disconnect()
    f = pd.concat(frames, ignore_index=True)
    f["date"] = pd.to_datetime(f["datetime"]).dt.normalize()
    return f.sort_values("date").tail(n).reset_index(drop=True)


def main():
    f = fetch_day()
    close = f["close"].values.astype(float)
    last_date = pd.to_datetime(f["date"].values[-1])
    last_close = close[-1]
    print(f"标的: 588000 科创50ETF   最后一根日线: {last_date.date()}  收盘(近似): {last_close:.4f}")
    print(f"{'combo':<10}{'TRIX':>10}{'signal':>10}{'TRIX>信号':>12}{'状态':>8}")
    print("-" * 52)

    bull = 0
    for (n, m) in COMB_N12:
        tr, sig = trix_series(close, n, m)
        tv, sv = float(tr[-1]), float(sig[-1])
        is_bull = tv > sv
        bull += int(is_bull)
        state = "金叉(多)" if is_bull else "死叉(空)"
        print(f"({n},{m})".ljust(10) + f"{tv:>10.3f}{sv:>10.3f}{('True' if is_bull else 'False'):>12}{state:>8}")

    ratio = bull / len(COMB_N12)
    pos = "持仓" if ratio > 0.5 else "空仓"
    print("-" * 52)
    print(f"看多占比 long_ratio = {bull}/{len(COMB_N12)} = {ratio:.3f}")
    print(f"当前持仓判定(>0.5看多): {pos}")


if __name__ == "__main__":
    main()

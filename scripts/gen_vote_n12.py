# 生成 N=12 结果簇 的每日看多投票率 CSV, 格式对齐 results/vote_ratio.csv (date, long_ratio)
import os, numpy as np, pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

COMB = [(10,9),(10,12),(12,9),(12,12),(14,9),(14,12)]  # N12 结果簇

def trix_pos(c, N, M):
    s = pd.Series(c, dtype=float)
    e1 = s.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr = e3.pct_change() * 100
    sig = tr.rolling(M).mean()
    return (tr > sig).astype(int).values

def fetch_day(symbol, start="2021-01-01", per=700, maxpages=20):
    api = TdxHq_API()
    api.connect("180.153.18.170", 7709, time_out=5)
    frames = []
    for pg in range(maxpages):
        k = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, TDXParams.MARKET_SH, symbol, pg*per, per)
        if k is None: break
        d = api.to_df(k)
        if d is None or len(d) == 0: break
        frames.append(d)
        if len(d) < per: break
    api.disconnect()
    f = pd.concat(frames, ignore_index=True)
    f["date"] = pd.to_datetime(f["datetime"]).dt.normalize()
    f = f[f["date"] >= pd.Timestamp(start)].sort_values("date").reset_index(drop=True)
    return f

def main():
    f = fetch_day("588000", "2021-01-01")
    close = f["close"].values.astype(float)
    dates = pd.to_datetime(f["date"].values)
    states = np.column_stack([trix_pos(close, n, m) for (n, m) in COMB])
    ratio = states.mean(axis=1)
    out = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "long_ratio": ratio})
    os.makedirs("results", exist_ok=True)
    path = "results/vote_ratio_n12_588000.csv"
    out.to_csv(path, index=False)
    print("写入:", os.path.abspath(path))
    print("总行数:", len(out))
    print(out.head(8).to_string(index=False))
    print("...")
    print(out.tail(8).to_string(index=False))
    # 与是否持仓(>0.5)对比直观统计
    long_days = int((ratio > 0.5).sum())
    print(f"看多占比>0.5 的天数: {long_days}/{len(out)} = {long_days/len(out):.1%}")

if __name__ == "__main__":
    main()

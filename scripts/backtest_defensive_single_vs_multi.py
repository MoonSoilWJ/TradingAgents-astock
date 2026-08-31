#!/usr/bin/env python3
"""防御日: 多只等权 vs 只买一只 对比.
变体:
  V1 等权全部金叉防御标的 (当前生产逻辑)
  V2 防御日只持国债 (≈A1 债券填充, 固定单标的)
  V3 防御日只买"金叉标的中 20日动量最强"的一只
  V4 防御日只买"金叉标的中 TRIX 信号最强(tr-sig 最大)"的一只
核心: 588000 用 N12; 防御日触发条件 = N12空仓 且 588000死叉.
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_588000_n12 import vote_from, COMB_N12, SLIP, trix_series, trix_cross

TDX_HOST, TDX_PORT = "180.153.18.170", 7709
CORE, BOND = "588000", "511260"
DEF = ["511260", "518880", "510880", "515080", "512890"]
NAMES = {"588000": "科创50", "511260": "国债", "518880": "黄金", "510880": "红利",
         "515080": "中证红利", "512890": "红利低波"}


def market_of(code):
    return TDXParams.MARKET_SH if (code.strip() and code[0] in "569") else TDXParams.MARKET_SZ


def fetch_day(code, start="2021-01-01"):
    api = TdxHq_API(); api.connect(TDX_HOST, TDX_PORT, time_out=5)
    fr = []
    for pg in range(20):
        k = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, market_of(code), code.encode(), pg * 700, 700)
        if not k: break
        d = api.to_df(k)
        if d is None or len(d) == 0: break
        fr.append(d)
        if len(d) < 700: break
    api.disconnect()
    f = pd.concat(fr, ignore_index=True)
    f["date"] = pd.to_datetime(f["datetime"]).dt.normalize()
    return f[f["date"] >= pd.Timestamp(start)].sort_values("date").drop_duplicates("date").set_index("date")["close"].astype(float)


def sim_mixed(hold, closes, slip):
    n = len(hold); eq = 1.0; eqs = []; prev = None; idle = 0; sw = 0
    for i in range(n):
        w = hold[i]
        if prev is None:
            eq *= (1 - sum(slip * v for v in w.values())) if w else 1.0
        else:
            turn = sum(abs(w.get(c, 0) - prev.get(c, 0)) for c in set(w) | set(prev))
            ret = sum(v * (closes[c][i] / closes[c][i - 1] - 1) for c, v in w.items())
            eq *= (1 + ret) * (1 - slip * turn)
            if w != prev: sw += 1
        prev = w; idle += (0 if w else 1); eqs.append(eq)
    eq = np.array(eqs); total = eq[-1] / eq[0] - 1
    mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    return total, abs(mdd), idle, sw


def main():
    print("拉取行情 ...")
    s = {c: fetch_day(c) for c in [CORE] + DEF}
    df = pd.DataFrame(s).dropna(); n = len(df)
    closes = {c: df[c].values.astype(float) for c in df.columns}
    kc = closes[CORE]
    core, _ = vote_from(kc, COMB_N12, 0.5)
    kc_cross = trix_cross(kc)
    def_cross = {c: trix_cross(closes[c]) for c in DEF}

    # 预计算信号强度 (tr-sig) 与 20日动量
    sig = {}
    mom = {}
    for c in DEF:
        tr, sg = trix_series(closes[c], 14, 9)
        sig[c] = tr - sg
        r = closes[c]
        mom[c] = np.full(n, np.nan)
        mom[c][20:] = r[20:] / r[:-20] - 1

    def hold_spec(pick):
        out = []
        for i in range(n):
            if core[i] == 1:
                out.append({CORE: 1.0}); continue
            if kc_cross[i] == 0:
                g = [c for c in DEF if def_cross[c][i] == 1]
                if not g:
                    out.append({BOND: 1.0}); continue
                if pick == "equal":
                    out.append({c: 1.0 / len(g) for c in g})
                elif pick == "bond":
                    out.append({BOND: 1.0})
                elif pick == "mom":
                    best = max(g, key=lambda c: mom[c][i] if not np.isnan(mom[c][i]) else -1e9)
                    out.append({best: 1.0})
                elif pick == "sig":
                    best = max(g, key=lambda c: sig[c][i] if not np.isnan(sig[c][i]) else -1e9)
                    out.append({best: 1.0})
            else:
                out.append({BOND: 1.0})
        return out

    print(f"\n区间 {df.index[0].date()}~{df.index[-1].date()}  {n}日  滑点{SLIP*100:.2f}%")
    print(f"{'变体':<28}{'累计':>9}{'年化':>9}{'最大回撤':>11}{'空仓率':>9}{'切换':>7}")
    print("-" * 74)
    for name, pk in [("V1 等权全部金叉(当前)", "equal"),
                     ("V2 防御日只持国债(≈A1)", "bond"),
                     ("V3 只买动量最强的一只", "mom"),
                     ("V4 只买TRIX信号最强一只", "sig")]:
        h = hold_spec(pk)
        total, mdd, idle, sw = sim_mixed(h, closes, SLIP)
        print(f"{name:<28}{total*100:>8.1f}%{((1+total)**(1/(n/365))-1)*100:>9.1f}%{mdd*100:>10.1f}%{idle/n*100:>8.1f}%{sw:>7}")


if __name__ == "__main__":
    main()

"""预估聚宽版 joinquant_kc50_defensive_v3.py 的回测结果。

聚宽脚本口径: run_daily(time='open') → 用 close[t-1] 的信号, 在 open[t] 成交, 持到 open[t+1]。
本地生产口径: close[t-1] 的信号, 从 close[t-1] 持有到 close[t]。
两者决策序列完全相同 (都是 hold_spec[t-1]), 只差执行价 (开盘 vs 收盘)。

本脚本按【聚宽的开盘价口径】在本地预跑一遍, 给出聚宽上应该看到的数字,
用于交叉验证聚宽回测是否复现本地逻辑。

用法: python3 scripts/verify_jq_port_expectation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import backtest_588000_n12 as B

SLIP = B.SLIP
TD_YR = 244


def fetch_oc(code, start="2020-11-16", pages=20):
    market = B._market_of(code)
    api = TdxHq_API()
    api.connect("180.153.18.170", 7709, time_out=5)
    frames = []
    for pg in range(pages):
        k = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, market,
                                  code.encode(), pg * 700, 700)
        if k is None:
            break
        d = api.to_df(k)
        if d is None or len(d) == 0:
            break
        frames.append(d)
        if len(d) < 700:
            break
    api.disconnect()
    if not frames:
        return None
    f = pd.concat(frames, ignore_index=True)
    f["date"] = pd.to_datetime(f["datetime"]).dt.normalize()
    f = f[f["date"] >= pd.Timestamp(start)]
    f = f.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    return f.set_index("date")[["open", "close"]].astype(float)


def run(hs, op, lo, hi, slip=SLIP):
    """聚宽口径: 决策 w=hs[t-1], 在 open[t] 建仓, 持到 open[t+1]。"""
    eq = 1.0
    eqs = []
    prev = None
    sw = 0
    for t in range(lo, hi):
        w = hs[t - 1]
        if prev is None:
            eq *= (1 - slip * sum(w.values()))
        else:
            turn = sum(abs(w.get(c, 0.0) - prev.get(c, 0.0)) for c in set(w) | set(prev))
            ret = sum(v * (op[c][t + 1] / op[c][t] - 1) for c, v in w.items())
            eq *= (1 + ret) * (1 - slip * turn)
            if w != prev:
                sw += 1
        prev = w
        eqs.append(eq)
    eq = np.array(eqs)
    total = eq[-1] / eq[0] - 1
    mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    yrs = len(eq) / TD_YR
    ann = ((1 + total) ** (1 / yrs) - 1) * 100 if total > -1 else -100.0
    return total * 100, ann, abs(mdd) * 100, sw, len(eq)


def main():
    print("拉取行情 (含开盘价) ...")
    codes = ["588000"] + list(B.DEF)
    op_d, cl_d = {}, {}
    for c in codes:
        d = fetch_oc(c)
        if d is None:
            print(f"  ! {c} 拉取失败")
            continue
        op_d[c] = d["open"]
        cl_d[c] = d["close"]
    op = pd.DataFrame(op_d).dropna()
    cl = pd.DataFrame(cl_d).dropna()
    common = op.index.intersection(cl.index)
    op, cl = op.loc[common], cl.loc[common]
    print(f"对齐区间 {common[0].date()} ~ {common[-1].date()} ({len(common)} 日)\n")

    core, kc_cross, def_cross, hs, _ = B.build_defensive_rotation(cl)
    opx = {c: op[c].values.astype(float) for c in ["588000"] + list(B.DEF)}
    clx = {c: cl[c].values.astype(float) for c in ["588000"] + list(B.DEF)}

    W = 90                                  # 与聚宽脚本 WARMUP 一致
    n = len(common)

    tot_o, ann_o, mdd_o, sw_o, days_o = run(hs, opx, W, n - 1)
    eq, tot_c, mdd_c, idle_c, sw_c = B.sim_mixed(hs[W:], {c: v[W:] for c, v in clx.items()}, SLIP)
    yrs_c = (n - W) / TD_YR
    ann_c = ((1 + tot_c) ** (1 / yrs_c) - 1) * 100

    print("=" * 92)
    print("聚宽版预期值 (开盘价成交) vs 本地生产 (收盘价成交)")
    print("=" * 92)
    print(f"{'口径':<26}{'累计%':>10}{'年化%':>10}{'回撤%':>10}{'切换':>10}{'交易日':>10}")
    print("-" * 92)
    print(f"{'聚宽 open成交 (预期)':<26}{tot_o:>10.1f}{ann_o:>10.1f}{mdd_o:>10.1f}{sw_o:>10}{days_o:>10}")
    print(f"{'本地 close成交 (生产)':<26}{tot_c*100:>10.1f}{ann_c:>10.1f}{abs(mdd_c)*100:>10.1f}{sw_c:>10}{n-W:>10}")
    print("-" * 92)
    print(f"差异 (开盘-收盘): 累计 {tot_o - tot_c*100:+.1f}pp  年化 {ann_o - ann_c:+.1f}pp")

    # 分段对照 (IS/OOS)
    split = pd.Timestamp("2024-01-01")
    i_s = int(np.sum(common < split))
    for tag, lo, hi in [("IS  2020-11~2023-12", W, i_s), ("OOS 2024-01~今", i_s, n - 1)]:
        t, a, m, s, d = run(hs, opx, lo, hi)
        print(f"  聚宽口径 {tag}: 累计 {t:>7.1f}%  年化 {a:>6.1f}%  回撤 {m:>5.1f}%  切换 {s}")

    print("\n说明: 聚宽用前复权(fq='pre')含分红, 红利类ETF收益会略高于本地未复权口径;")
    print("      若两者差异在 ±10% 以内即视为逻辑复现成功, 差异主要来自复权与开盘/收盘口径。")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""修正版: 科创50(588000)死叉 -> 轮动到【低相关防御组】(国债/黄金/红利)中仍金叉者
对比: 纯N12持币(S0) / N12+国债(A1) / 同特征轮动(高相关, 已证56%) / 本防御组轮动
金叉死叉 = TRIX(14,9) 交叉. 滑点 0.05%.
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

from backtest_588000_n12 import vote_from, COMB_N12, SLIP, trix_series

TDX_HOST, TDX_PORT = "180.153.18.170", 7709
CORE = "588000"
BOND = "511260"
START = "2021-01-01"

# 防御组: 与科创50低相关/负相关的资产 (国债/黄金/红利)
DEF = ["511260", "518880", "510880", "515080", "512890"]
NAMES = {"588000": "科创50", "511260": "国债ETF", "518880": "黄金ETF",
         "510880": "红利ETF", "515080": "中证红利ETF", "512890": "红利低波ETF"}


def detect_market(code):
    c = code.strip()
    return (TDXParams.MARKET_SH, "SH") if (c and c[0] in "569") else (TDXParams.MARKET_SZ, "SZ")


def fetch_day(code, market, n=2500):
    api = TdxHq_API()
    api.connect(TDX_HOST, TDX_PORT, time_out=5)
    frames = []
    for pg in range(20):
        k = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, market, code.encode(), pg * 700, 700)
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
    f = f[f["date"] >= pd.Timestamp(START)].sort_values("date").tail(n).reset_index(drop=True)
    f["close"] = f["close"].astype(float)
    return f


def load(code):
    s = fetch_day(code, detect_market(code)[0])
    return None if s is None else s.set_index("date")["close"].astype(float)


def trix_cross(close):
    tr, sig = trix_series(np.asarray(close, float), 14, 9)
    return (tr > sig).astype(int)


def sim_mixed(hold_spec, closes, slip):
    n = len(hold_spec)
    eq = 1.0; eqs = []; prev = None; idle = 0; sw = 0
    for i in range(n):
        w = hold_spec[i]
        if prev is None:
            cost = sum(slip * v for v in w.values()) if w else 0.0
            eq *= (1 - cost)
        else:
            turn = 0.0
            for c in set(w) | set(prev):
                turn += abs(w.get(c, 0.0) - prev.get(c, 0.0))
            ret = sum(v * (closes[c][i] / closes[c][i - 1] - 1) for c, v in w.items())
            eq *= (1 + ret) * (1 - slip * turn)
            if w != prev:
                sw += 1
        prev = w
        idle += (0 if w else 1)
        eqs.append(eq)
    eq = np.array(eqs)
    total = eq[-1] / eq[0] - 1
    mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    return eq, total, mdd, idle, sw


def metrics(eq, total, mdd, idle, sw, n):
    years = n / 365.0
    annual = ((1 + total) ** (1 / max(years, 1e-9)) - 1) * 100 if total > -1 else -100.0
    return {"total": total * 100, "annual": annual, "mdd": abs(mdd) * 100,
            "idle_rate": idle / n * 100, "switches": sw}


def main():
    print("拉取行情 ...")
    series = {}
    for c in [CORE] + DEF:
        s = load(c)
        print(f"  {c} {NAMES.get(c,''):<10} {len(s) if s is not None else '失败'} 根")
        if s is not None:
            series[c] = s
    df = pd.DataFrame(series).dropna()
    dates = df.index; n = len(df)
    closes = {c: df[c].values.astype(float) for c in df.columns}

    kc = closes[CORE]
    ret_kc = np.diff(kc) / kc[:-1]

    # 防御组与科创50相关性 (验证"低相关"原则)
    print("\n【防御组 vs 科创50 相关性】")
    print(f"  {'代码':<8}{'名称':<12}{'相关':>7}{'年化波动':>9}")
    for c in DEF:
        rc = np.diff(closes[c]) / closes[c][:-1]
        corr = np.corrcoef(ret_kc, rc)[0, 1]
        vol = np.std(rc) * np.sqrt(252) * 100
        print(f"  {c:<8}{NAMES.get(c,''):<12}{corr:>7.2f}{vol:>8.1f}%")

    kc_cross = trix_cross(kc)
    def_cross = {c: trix_cross(closes[c]) for c in DEF}
    n12 = vote_from(kc, COMB_N12, thr=0.5)[0]

    # 策略: 科创50金叉->588000; 死叉->防御组金叉者等权, 无则国债
    hold_reg = []
    for i in range(n):
        if kc_cross[i] == 1:
            hold_reg.append({CORE: 1.0})
        else:
            g = [c for c in DEF if def_cross[c][i] == 1]
            hold_reg.append({c: 1.0 / len(g) for c in g} if g else {BOND: 1.0})

    hold_s0 = [{CORE: 1.0} if n12[i] == 1 else {} for i in range(n)]
    hold_a1 = [{CORE: 1.0} if n12[i] == 1 else {BOND: 1.0} for i in range(n)]

    r_reg = sim_mixed(hold_reg, closes, SLIP)
    r_s0 = sim_mixed(hold_s0, closes, SLIP)
    r_a1 = sim_mixed(hold_a1, closes, SLIP)

    print("\n" + "=" * 70)
    print(f"【回测】 区间 {dates[0].date()}~{dates[-1].date()}  {n}日  滑点{SLIP*100:.2f}%")
    print("规则: 科创50金叉持588000; 死叉则持'防御组(国债/黄金/红利)中仍金叉者'等权, 无则国债")
    print("=" * 70)
    print(f"{'策略':<24}{'累计':>9}{'年化':>9}{'最大回撤':>11}{'空仓率':>9}{'切换':>7}")
    print("-" * 70)
    for name, r in [("S0 纯N12持币", r_s0), ("A1 N12+国债", r_a1),
                    ("防御组轮动(修正版)", r_reg)]:
        m = metrics(*r, n)
        print(f"{name:<24}{m['total']:>8.1f}%{m['annual']:>9.1f}%{m['mdd']:>10.1f}%{m['idle_rate']:>8.1f}%{m['switches']:>7}")

    out = {"window": f"{dates[0].date()}~{dates[-1].date()}",
           "strategies": {k: {kk: round(vv, 2) for kk, vv in metrics(*v, n).items()}
                          for k, v in [("S0", r_s0), ("A1", r_a1), ("defensive_rotation", r_reg)]}}
    op = Path(__file__).resolve().parent.parent / "results" / "kc50_defensive_rotation.json"
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存: {op}")


if __name__ == "__main__":
    main()

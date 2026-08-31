#!/usr/bin/env python3
"""防御组轮动 V3(单只动量最强) 稳健性检验:
  ① 滑点压力测试: 0.05% / 0.1% / 0.2%
  ② 滚动样本外: 逐年收益 + 滚动 252 日窗口(看是否每段都正)
  ③ 换动量口径: 窗口 10/20/40/60 日 + 波动调整动量
核心 588000 用 N12; 防御日 = N12空仓 且 588000死叉 -> 挑金叉防御标的中动量最强单只; 否则国债.
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_588000_n12 import vote_from, COMB_N12, trix_series, trix_cross

TDX_HOST, TDX_PORT = "180.153.18.170", 7709
CORE, BOND = "588000", "511260"
DEF = ["511260", "518880", "510880", "515080", "512890"]


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
    return eq, total, abs(mdd), idle, sw


def momentum_series(closes, win):
    n = len(closes); m = np.full(n, np.nan)
    if n > win: m[win:] = closes[win:] / closes[:-win] - 1
    return m


def voladj_series(closes, win, vol_win=20):
    n = len(closes); daily = np.concatenate([[0.0], np.diff(closes) / closes[:-1]])
    m = momentum_series(closes, win)
    vol = np.full(n, np.nan)
    if n > vol_win:
        vol[vol_win:] = pd.Series(daily).rolling(vol_win).std().values[vol_win:]
    out = np.where(vol > 0, m / vol, np.nan)
    return out


def build_v3(kc, core, kc_cross, def_cross, closes, score_win, score_kind="mom"):
    """score_kind: 'mom' 普通动量 / 'voladj' 波动调整动量。返回 hold_spec。"""
    n = len(kc)
    if score_kind == "mom":
        score = {c: momentum_series(closes[c], score_win) for c in DEF}
    else:
        score = {c: voladj_series(closes[c], score_win) for c in DEF}
    hold = []
    for i in range(n):
        if core[i] == 1:
            hold.append({CORE: 1.0}); continue
        if kc_cross[i] == 0:
            g = [c for c in DEF if def_cross[c][i] == 1]
            if g:
                best = max(g, key=lambda c: score[c][i] if not np.isnan(score[c][i]) else -1e9)
                hold.append({best: 1.0})
            else:
                hold.append({BOND: 1.0})
        else:
            hold.append({BOND: 1.0})
    return hold


def main():
    print("拉取行情 ...")
    s = {c: fetch_day(c) for c in [CORE] + DEF}
    df = pd.DataFrame(s).dropna(); n = len(df)
    dates = df.index
    closes = {c: df[c].values.astype(float) for c in df.columns}
    kc = closes[CORE]
    core, _ = vote_from(kc, COMB_N12, 0.5)
    kc_cross = trix_cross(kc)
    def_cross = {c: trix_cross(closes[c]) for c in DEF}
    print(f"区间 {dates[0].date()}~{dates[-1].date()}  {n}日\n")

    # ① 滑点压力测试 (动量窗口=20, 普通动量)
    print("=" * 70)
    print("① 滑点压力测试 (防御腿: 20日动量最强单标的)")
    print(f"{'滑点':<8}{'累计':>9}{'年化':>9}{'最大回撤':>11}{'切换':>7}")
    print("-" * 50)
    base_eq = None
    for slip in [0.0005, 0.001, 0.002]:
        h = build_v3(kc, core, kc_cross, def_cross, closes, 20, "mom")
        eq, total, mdd, idle, sw = sim_mixed(h, closes, slip)
        ann = ((1 + total) ** (1 / (n / 365)) - 1) * 100
        if abs(slip - 0.0005) < 1e-9: base_eq = eq
        print(f"{slip*100:>5.2f}%{total*100:>8.1f}%{ann:>9.1f}%{mdd*100:>10.1f}%{sw:>7}")
    print("  (注: 每次切换换手≈2倍, 0.2%滑点下 216 次切换的摩擦约 40%+ )")

    # ② 滚动样本外
    print("\n" + "=" * 70)
    print("② 滚动样本外 (基础滑点0.05%)")
    # 逐年
    print("逐年收益:")
    years = sorted(set(d.year for d in dates))
    for y in years:
        idx = [j for j in range(n) if dates[j].year == y]
        if not idx: continue
        r = base_eq[idx[-1]] / base_eq[idx[0]] - 1
        print(f"  {y}: {r*100:>7.1f}%")
    # 滚动 252 日窗口
    print("滚动 252 日窗口 (步长63日):")
    pos = neg = 0; wins = []
    step = 63
    for start in range(0, n - 252, step):
        r = base_eq[start + 252] / base_eq[start] - 1
        wins.append(r)
        if r > 0: pos += 1
        else: neg += 1
    print(f"  窗口数 {len(wins)}, 正收益 {pos}, 负收益 {neg}, 正收益占比 {pos/len(wins)*100:.0f}%")
    print(f"  最差单窗口: {min(wins)*100:.1f}%   最好: {max(wins)*100:.1f}%")

    # ③ 换动量口径
    print("\n" + "=" * 70)
    print("③ 换动量口径 (滑点0.05%)")
    print(f"{'口径':<22}{'累计':>9}{'年化':>9}{'最大回撤':>11}{'切换':>7}")
    print("-" * 60)
    for label, win, kind in [("动量 10日", 10, "mom"), ("动量 20日(生产)", 20, "mom"),
                             ("动量 40日", 40, "mom"), ("动量 60日", 60, "mom"),
                             ("波动调整 20日", 20, "voladj")]:
        h = build_v3(kc, core, kc_cross, def_cross, closes, win, kind)
        eq, total, mdd, idle, sw = sim_mixed(h, closes, 0.0005)
        ann = ((1 + total) ** (1 / (n / 365)) - 1) * 100
        print(f"{label:<22}{total*100:>8.1f}%{ann:>9.1f}%{mdd*100:>10.1f}%{sw:>7}")


if __name__ == "__main__":
    main()

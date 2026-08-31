#!/usr/bin/env python3
"""三步策略: 以科创50(588000)为"高β成长风格景气望远镜"
  1) 量化总结科创50的特征(收益/波动/beta/相关性/风格)
  2) 在全市场(剔除 创业/北证/科创)里, 按"与科创50相似度"(收益相关性+beta)筛选同特征标的
  3) 回测: 科创50 金叉 -> 持588000; 科创50 死叉 -> 持"同特征且仍金叉"的标的(等权), 无则国债

金叉/死叉 = TRIX(14,9) 金叉(tr>sig) / 死叉(tr<sig), 与 N12 框架同源.
候选池: 主板成长/科技/高β 类 ETF(均非 创业/科创/北证 跟踪), 作为"全市场"的可交易代理;
        真实全市场个股扫描为后续扩展(本脚本聚焦方法验证).
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
SIMILAR_K = 8  # 取相似度最高的 K 个作为可轮动池

# 候选池: 主板成长/科技/高β ETF(剔除 创业/科创/北证 跟踪标的)
CANDIDATES = [
    "512760", "159995", "515050", "515000", "512980", "512660",
    "515790", "515030", "515880", "512010", "512000", "510300",
    "510500", "512100", "510050", "159928", "512690",
]
NAMES = {
    "512760": "芯片ETF", "159995": "芯片ETF华夏", "515050": "5G通信ETF",
    "515000": "科技ETF", "512980": "传媒ETF", "512660": "军工ETF",
    "515790": "光伏ETF", "515030": "新能源车ETF", "515880": "通信ETF",
    "512010": "医药ETF", "512000": "券商ETF", "510300": "沪深300ETF",
    "510500": "中证500ETF", "512100": "中证1000ETF", "510050": "上证50ETF",
    "159928": "消费ETF", "512690": "酒ETF", "511260": "国债ETF",
}


def detect_market(code: str):
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
    """hold_spec[i] = {code: w} (cash={}). 日收益复利, 换手收滑点."""
    n = len(hold_spec)
    eq = 1.0; eqs = []; prev = None; idle = 0; sw = 0
    for i in range(n):
        w = hold_spec[i]
        if prev is None:
            cost = sum(slip * v for v in w.values()) if w else 0.0
            eq *= (1 - cost)
        else:
            turn = 0.0
            keys = set(w) | set(prev)
            for c in keys:
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
    for c in [CORE, BOND] + CANDIDATES:
        s = load(c)
        if s is None:
            print(f"  [失败] {c}")
            continue
        series[c] = s
        print(f"  {c} {NAMES.get(c,''):<10} {len(s)} 根")
    df = pd.DataFrame(series).dropna()
    dates = df.index; n = len(df)
    closes = {c: df[c].values.astype(float) for c in df.columns}

    kc = closes[CORE]
    ret_kc = np.diff(kc) / kc[:-1]
    ret_hs = np.diff(closes["510300"]) / closes["510300"][:-1]
    ret_zz = np.diff(closes["512100"]) / closes["512100"][:-1]
    ret_bond = np.diff(closes[BOND]) / closes[BOND][:-1]

    # ── 1. 科创50 特征 ──────────────────────────────────
    vol = np.std(ret_kc) * np.sqrt(252) * 100
    beta_hs = np.cov(ret_kc, ret_hs)[0, 1] / np.var(ret_hs)
    beta_zz = np.cov(ret_kc, ret_zz)[0, 1] / np.var(ret_zz)
    corr_hs = np.corrcoef(ret_kc, ret_hs)[0, 1]
    corr_zz = np.corrcoef(ret_kc, ret_zz)[0, 1]
    corr_bond = np.corrcoef(ret_kc, ret_bond)[0, 1]
    up = (ret_kc > 0).mean() * 100
    years = n / 365.0
    total_kc = kc[-1] / kc[0] - 1
    ann_kc = ((1 + total_kc) ** (1 / years) - 1) * 100
    peak = np.maximum.accumulate(kc); mdd_kc = ((kc - peak) / peak).min() * 100
    print("\n" + "=" * 64)
    print("【1. 科创50(588000) 特征总结 2021-2026】")
    print("=" * 64)
    print(f"  累计 {total_kc*100:>7.1f}%   年化 {ann_kc:>7.1f}%   年化波动 {vol:>6.1f}%   最大回撤 {mdd_kc:>6.1f}%")
    print(f"  Beta(沪深300)={beta_hs:.2f}  Beta(中证1000)={beta_zz:.2f}  上涨日占比 {up:.1f}%")
    print(f"  与沪深300相关 {corr_hs:.2f}  与中证1000相关 {corr_zz:.2f}  与国债相关 {corr_bond:.2f}")
    print("  风格画像: 高Beta(>1)、高波动、与宽基正相关但弹性更大、与国债负相关")
    print("         → '高β成长/风险偏好'代理, 其金叉死叉可当风格景气开关")

    # ── 2. 相似度筛选 ──────────────────────────────────
    sim = []
    for c in CANDIDATES:
        if c not in closes:
            continue
        rc = np.diff(closes[c]) / closes[c][:-1]
        corr = np.corrcoef(ret_kc, rc)[0, 1]
        beta = np.cov(ret_kc, rc)[0, 1] / np.var(rc)
        sim.append((c, corr, beta, np.std(rc) * np.sqrt(252) * 100))
    sim.sort(key=lambda x: -x[1])
    similar = [c for c, *_ in sim[:SIMILAR_K]]
    print("\n" + "=" * 64)
    print(f"【2. 与科创50最相似的 {SIMILAR_K} 个标的(按收益相关性)】")
    print("=" * 64)
    print(f"  {'代码':<8}{'名称':<12}{'相关':>7}{'Beta':>7}{'年化波动':>9}")
    for c, corr, beta, v in sim[:SIMILAR_K]:
        print(f"  {c:<8}{NAMES.get(c,''):<12}{corr:>7.2f}{beta:>7.2f}{v:>8.1f}%")
    print(f"  (轮动池 = 以上 {SIMILAR_K} 只; 创业/北证/科创 已排除)")

    # ── 3. 策略回测 ──────────────────────────────────
    kc_cross = trix_cross(kc)
    cand_cross = {c: trix_cross(closes[c]) for c in similar}
    n12 = vote_from(kc, COMB_N12, thr=0.5)[0]

    # 策略: 科创50金叉->588000; 死叉->同特征且金叉者等权; 无则国债
    hold_reg = []
    for i in range(n):
        if kc_cross[i] == 1:
            hold_reg.append({CORE: 1.0})
        else:
            g = [c for c in similar if cand_cross[c][i] == 1]
            hold_reg.append({c: 1.0 / len(g) for c in g} if g else {BOND: 1.0})
    # 基线
    hold_s0 = [{CORE: 1.0} if n12[i] == 1 else {} for i in range(n)]
    hold_a1 = [{CORE: 1.0} if n12[i] == 1 else {BOND: 1.0} for i in range(n)]

    r_reg = sim_mixed(hold_reg, closes, SLIP)
    r_s0 = sim_mixed(hold_s0, closes, SLIP)
    r_a1 = sim_mixed(hold_a1, closes, SLIP)

    print("\n" + "=" * 70)
    print(f"【3. 回测】 区间 {dates[0].date()}~{dates[-1].date()}  {n}日  滑点{SLIP*100:.2f}%")
    print(f"规则: 科创50金叉持588000; 死叉则持'同特征且金叉'标的等权, 无则国债")
    print("=" * 70)
    print(f"{'策略':<22}{'累计':>9}{'年化':>9}{'最大回撤':>11}{'空仓率':>9}{'切换':>7}")
    print("-" * 70)
    for name, r in [("S0 纯N12持币", r_s0), ("A1 N12+国债", r_a1), ("策略:死叉轮动同特征", r_reg)]:
        m = metrics(*r, n)
        print(f"{name:<22}{m['total']:>8.1f}%{m['annual']:>9.1f}%{m['mdd']:>10.1f}%{m['idle_rate']:>8.1f}%{m['switches']:>7}")

    out = {
        "kc50_characteristics": {
            "total_pct": round(total_kc * 100, 1), "annual_pct": round(ann_kc, 1),
            "vol_pct": round(vol, 1), "mdd_pct": round(mdd_kc, 1),
            "beta_hs300": round(beta_hs, 2), "beta_zz1000": round(beta_zz, 2),
            "corr_hs300": round(corr_hs, 2), "corr_zz1000": round(corr_zz, 2),
            "corr_bond": round(corr_bond, 2), "up_pct": round(up, 1),
        },
        "similar_top": [{"code": c, "name": NAMES.get(c, ""), "corr": round(cr, 2),
                         "beta": round(bt, 2), "vol": round(vl, 1)}
                        for c, cr, bt, vl in sim[:SIMILAR_K]],
        "strategies": {k: {kk: round(vv, 2) for kk, vv in metrics(*v, n).items()}
                       for k, v in [("S0", r_s0), ("A1", r_a1), ("regime_rotation", r_reg)]},
    }
    op = Path(__file__).resolve().parent.parent / "results" / "kc50_similar_rotation.json"
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存: {op}")


if __name__ == "__main__":
    main()

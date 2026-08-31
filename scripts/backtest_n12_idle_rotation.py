#!/usr/bin/env python3
"""用户定义的轮动: 核心 N12(588000) 保持不变, 仅在 588000 闲置(空仓)时,
把那笔闲置资金轮动到 "其他标的" —— 用项目现有 MA20 动量排名信号(同 8宽基轮动)
在篮子 [510300/510500/159915/518880/513100] 中选最强者持有; 无信号则回退国债ETF。

对比:
  S0   : 纯 N12, 空仓持币
  A1   : N12 + 国债填充(闲置全持债)
  ROT  : N12 核心 + 闲置期轮动到其他标的(无信号则持币)
  ROTB : N12 核心 + 闲置期轮动到其他标的(无信号则回退国债)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

# 注: analyze_regime.py 当前不存在, 这里内联其 fetch_day/detect_market (取自原文件逻辑),
# 使本脚本独立可跑, 不依赖已删除模块。
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

TDX_HOST, TDX_PORT = "180.153.18.170", 7709


def detect_market(code: str):
    """根据代码首位判断市场 (SH/SZ). 5/6/9 开头归沪市, 其余归深市."""
    c = code.strip()
    if c and c[0] in "569":
        return TDXParams.MARKET_SH, "SH"
    return TDXParams.MARKET_SZ, "SZ"


def fetch_day(code, market, n=900):
    """拉取日线, 返回含 date/close 的 DataFrame (升序, 取最近 n 根)."""
    api = TdxHq_API()
    api.connect(TDX_HOST, TDX_PORT, time_out=5)
    frames = []
    for pg in range(20):
        k = api.get_security_bars(
            TDXParams.KLINE_TYPE_DAILY, market, code.encode(), pg * 700, 700
        )
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
        raise RuntimeError("行情拉取失败(无数据), 检查代码或网络")
    f = pd.concat(frames, ignore_index=True)
    f["date"] = pd.to_datetime(f["datetime"]).dt.normalize()
    f = f.sort_values("date").tail(n).reset_index(drop=True)
    f["close"] = f["close"].astype(float)
    return f


from backtest_588000_n12 import vote_from, COMB_N12, SLIP

START = "2021-01-01"
CORE = "588000"
BOND = "511260"
IDLE_BASKET = ["510300", "510500", "159915", "518880", "513100"]  # 沪深300/中证500/创业板/黄金/纳指
ALL = [CORE, BOND] + IDLE_BASKET


def load(code, start=START):
    mkt = detect_market(code)[0]
    df = fetch_day(code, mkt, n=2500)
    df = df[df["date"] >= pd.Timestamp(start)]
    return df.set_index("date")["close"].astype(float)


def ma20_mom(close):
    """返回 (mom=close/MA20-1, above=是否站上MA20), 前20日 NaN。"""
    n = len(close)
    mom = np.full(n, np.nan)
    above = np.zeros(n, bool)
    for i in range(n):
        if i < 20:
            continue
        ma = close[i - 19 : i + 1].mean()
        if ma <= 0:
            continue
        mom[i] = close[i] / ma - 1
        above[i] = close[i] > ma
    return mom, above


def sim_assets(hold, closes, slip):
    n = len(hold)
    cash, units, pos, eq, idle = 1.0, 0.0, None, [], 0
    for i in range(n):
        c = hold[i]
        if c != pos:
            if pos is not None:
                amt = units * closes[pos][i]; fee = amt * slip
                cash = amt - fee; units = 0.0
            if c is not None:
                nd = closes[c][i]; fee = cash * slip
                units = (cash - fee) / nd; cash = 0.0
            pos = c
        idle += (pos is None)
        eq.append(cash + (units * closes[pos][i] if pos else 0.0))
    eq = np.array(eq)
    total = eq[-1] / eq[0] - 1
    mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    sw = int(np.sum([hold[i] != hold[i - 1] for i in range(1, n)]))
    return eq, total, mdd, idle, sw


def metrics(eq, total, mdd, idle, sw, n):
    years = n / 365.0
    annual = ((1 + total) ** (1 / max(years, 1e-9)) - 1) * 100 if total > -1 else -100.0
    return {"total": total * 100, "annual": annual, "mdd": abs(mdd) * 100,
            "idle_rate": idle / n * 100, "switches": sw}


def main():
    print("拉取行情 ...")
    series = {}
    for c in ALL:
        s = load(c)
        series[c] = s
        print(f"  {c}: {len(s)} 根 ({s.index[0].date()}~{s.index[-1].date()})")
    df = pd.DataFrame(series).dropna()
    dates = df.index
    n = len(df)
    closes = {c: df[c].values.astype(float) for c in df.columns}

    # 核心 N12 (588000, 不变)
    n12 = vote_from(closes[CORE], COMB_N12, thr=0.5)[0]

    # 闲置篮子 MA20 动量排名
    mom = {}; above = {}
    for c in IDLE_BASKET:
        mom[c], above[c] = ma20_mom(closes[c])

    def idle_target(i, fallback_bond):
        """闲置期目标: 篮子内站上MA20且动量最强者; 无则 国债/持币。"""
        best, bc = -1e9, None
        for c in IDLE_BASKET:
            if above[c][i] and not np.isnan(mom[c][i]) and mom[c][i] > best:
                best, bc = mom[c][i], c
        if bc is None:
            return BOND if fallback_bond else None
        return bc

    # ── 策略持仓序列 ──────────────────────────────────────
    hold_s0 = [CORE if n12[i] == 1 else None for i in range(n)]
    hold_a1 = [CORE if n12[i] == 1 else BOND for i in range(n)]
    hold_rot = [CORE if n12[i] == 1 else idle_target(i, False) for i in range(n)]
    hold_rotb = [CORE if n12[i] == 1 else idle_target(i, True) for i in range(n)]

    res = {
        "S0 纯N12持币": sim_assets(hold_s0, closes, SLIP),
        "A1 N12+国债": sim_assets(hold_a1, closes, SLIP),
        "ROT N12+闲置轮动": sim_assets(hold_rot, closes, SLIP),
        "ROTB N12+轮动/债回退": sim_assets(hold_rotb, closes, SLIP),
    }

    print("=" * 78)
    print(f"区间 {dates[0].date()}~{dates[-1].date()}  交易日 {n}  滑点 {SLIP*100:.2f}%")
    print("核心=588000 N12(不变); 闲置期: 其他标的=MA20动量最强(同8宽基信号)")
    print("=" * 78)
    print(f"{'策略':<20}{'累计':>9}{'年化':>9}{'最大回撤':>11}{'空仓率':>9}{'切换':>7}")
    print("-" * 78)
    for name, r in res.items():
        m = metrics(*r, n)
        print(f"{name:<20}{m['total']:>8.1f}%{m['annual']:>9.1f}%"
              f"{m['mdd']:>10.1f}%{m['idle_rate']:>8.1f}%{m['switches']:>7}")

    out = {"window": f"{dates[0].date()}~{dates[-1].date()}",
           "strategies": {k: {kk: round(vv, 2) for kk, vv in metrics(*v, n).items()}
                          for k, v in res.items()}}
    op = Path(__file__).resolve().parent.parent / "results" / "n12_idle_rotation.json"
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(__import__("json").dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存: {op}")


if __name__ == "__main__":
    main()

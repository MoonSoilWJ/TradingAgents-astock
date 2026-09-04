#!/usr/bin/env python3
"""科创50(N12簇) 结构检验 + 两种结构的「失效体温计」。

三个检验:
[A] 固定持有期消融 — N12 信号入场后【机械持有 N 天】卖出, 不靠 TRIX 翻空。
    若固定持有也能复制大部分收益 → edge 来自趋势延续本身, TRIX 簇只是执行器
    (与 A/B/R3「随机卖都赢过 TRIX」同源结论); 若崩掉 → N12 选时有真贡献。

[B] 跨品种泛化 — 同一套 N12 簇投票规则, 原样套到多个宽基/行业 ETF。
    若仅 588000 有效 → 5.8 年曲线可能只是这一只标的的特性(伪 edge)。

[C] 结构体温计 — 时间序列动量健康度:
    滚动窗口内 corr(过去20日收益, 未来20日收益)。
    持续为正 = 趋势延续结构在; 转负/归零 = 结构失效(变反转市)。
    这是趋势跟随策略的监测指标, 对应隔夜动量策略的「滚动上午溢价」。

用法:
    python3 scripts/kc50_structure_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_588000_n12 import (  # noqa: E402
    COMB_N12, SLIP, fetch_day_code, trix_series, vote_from,
)

START = "2020-11-16"

# 跨品种对照池: 宽基 + 行业, 覆盖不同风格(检验泛化性)
PEERS = [
    ("588000", "科创50", "基准"),
    ("159915", "创业板", "宽基"),
    ("510300", "沪深300", "宽基"),
    ("510500", "中证500", "宽基"),
    ("512100", "中证1000", "宽基"),
    ("510050", "上证50", "宽基"),
    ("159901", "深证100", "宽基"),
    ("588080", "科创板50", "宽基"),
    ("512480", "半导体", "行业"),
    ("512880", "证券", "行业"),
    ("512690", "酒", "行业"),
    ("512170", "医疗", "行业"),
    ("516160", "新能源", "行业"),
    ("515790", "光伏", "行业"),
]


def sim_hold_n(pos, close, n_hold, slip=SLIP):
    """信号=1 买入并【机械持有 n_hold 天】卖出(忽略中途翻空)。
    返回 (收益序列, 总收益, 最大回撤, 笔数)。"""
    n = len(close)
    eq, cur = [1.0], 1.0
    entry_i = None
    hold_left = 0
    trades = []
    for i in range(n):
        if entry_i is None:
            if pos[i] == 1:
                entry_i = i
                hold_left = n_hold
                cur *= (1 - slip)          # 买入滑点
        else:
            hold_left -= 1
            if hold_left <= 0 or i == n - 1:
                pe, px = close[entry_i], close[i]
                cur *= (px / pe) * (1 - slip)   # 卖出滑点
                trades.append((px / pe - 1) * 100)
                entry_i = None
        eq.append(cur)
    eq = np.array(eq[1:])
    mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    return eq, eq[-1] / eq[0] - 1, mdd, len(trades)


def ts_momentum_health(close, lookback=20, forward=20, win=250):
    """时间序列动量健康度: 滚动 win 日内 corr(过去lookback日收益, 未来forward日收益)。

    > 0 = 趋势延续(趋势跟随结构存活); ≤ 0 = 反转/随机(结构失效)。
    """
    c = pd.Series(np.asarray(close, float))
    r_past = (c / c.shift(lookback) - 1)
    r_fut = (c.shift(-forward) / c - 1)
    df = pd.DataFrame({"p": r_past, "f": r_fut}).dropna()
    if len(df) < win:
        return pd.Series(dtype=float), None
    ic = df["p"].rolling(win).corr(df["f"])
    return ic, df


def main() -> None:
    print("拉取数据 ...", flush=True)
    data: dict[str, pd.Series] = {}
    for code, name, kind in PEERS:
        try:
            s = fetch_day_code(code, START)
        except Exception as e:
            print(f"  {code} {name} 拉取失败: {e}")
            continue
        if s is None or len(s) < 300:
            print(f"  {code} {name} 数据不足({0 if s is None else len(s)}根), 跳过")
            continue
        data[code] = s
        print(f"  {code} {name:<8} {len(s)}根 {s.index[0].date()}~{s.index[-1].date()}")

    if "588000" not in data:
        print("588000 缺失, 终止")
        return

    kc = data["588000"]
    n = len(kc)
    years = (kc.index[-1] - kc.index[0]).days / 365.25
    pos, frac = vote_from(kc.values.astype(float), COMB_N12, thr=0.5)

    # ══ [A] 固定持有期消融 ══
    print("\n" + "=" * 88)
    print("  [A] 固定持有期消融 — 入场后机械持有 N 天, 不靠 TRIX 翻空离场")
    print("       (若固定持有也能复制收益 ⇒ edge 来自趋势延续, TRIX簇只是执行器)")
    print("=" * 88)
    print(f"  {'方案':<26}{'笔数':>6}{'累计收益':>12}{'年化':>9}{'回撤':>9}{'vs现状':>11}")
    print("  " + "-" * 74)

    from backtest_588000_n12 import sim as sim_signal
    eq0, tot0, mdd0, sw0 = sim_signal(pos, kc.values.astype(float))
    ann0 = ((1 + tot0) ** (1 / years) - 1) * 100
    print(f"  {'现状(TRIX簇翻空卖)':<26}{sw0:>6}{tot0 * 100:>+11.2f}%{ann0:>8.2f}%"
          f"{mdd0 * 100:>8.2f}%{'—':>11}")

    for hold_n in (3, 5, 10, 20, 40, 60):
        eq, tot, mdd, cnt = sim_hold_n(pos, kc.values.astype(float), hold_n)
        ann = ((1 + tot) ** (1 / years) - 1) * 100 if tot > -1 else -100
        print(f"  {f'固定持有 {hold_n} 天':<26}{cnt:>6}{tot * 100:>+11.2f}%{ann:>8.2f}%"
              f"{mdd * 100:>8.2f}%{(tot - tot0) * 100:>+10.1f}pp")

    # ══ [B] 跨品种泛化 ══
    print("\n" + "=" * 88)
    print("  [B] 跨品种泛化 — 同一套 N12 簇规则原样套到各标的(信号翻空卖, 同口径)")
    print("=" * 88)
    print(f"  {'标的':<12}{'类别':<6}{'根数':>6}{'笔数':>6}{'累计':>11}{'年化':>8}{'回撤':>8}{'胜率':>7}")
    print("  " + "-" * 66)
    peer_rows = []
    for code, name, kind in PEERS:
        if code not in data:
            continue
        s = data[code]
        p, _ = vote_from(s.values.astype(float), COMB_N12, thr=0.5)
        eq, tot, mdd, sw = sim_signal(p, s.values.astype(float))
        yr = (s.index[-1] - s.index[0]).days / 365.25
        ann = ((1 + tot) ** (1 / yr) - 1) * 100 if tot > -1 else -100
        # 胜率
        closes = s.values.astype(float)
        wins, cnt = 0, 0
        ei = None
        for i in range(len(p)):
            if p[i] == 1 and ei is None:
                ei = i
            elif p[i] == 0 and ei is not None:
                r = closes[i] * (1 - SLIP) / (closes[ei] * (1 + SLIP)) - 1
                cnt += 1
                wins += 1 if r > 0 else 0
                ei = None
        wr = wins / cnt * 100 if cnt else 0
        peer_rows.append((code, name, kind, tot, ann, mdd, wr, cnt))
        flag = "  ←基准" if code == "588000" else ""
        print(f"  {name + '(' + code + ')':<12}{kind:<6}{len(s):>6}{cnt:>6}"
              f"{tot * 100:>+10.2f}%{ann:>7.2f}%{mdd * 100:>7.2f}%{wr:>6.1f}%{flag}")

    pos_n = sum(1 for r in peer_rows if r[3] > 0)
    print(f"\n    泛化率: {pos_n}/{len(peer_rows)} 只正收益")
    ex_kc = [r[3] for r in peer_rows if r[0] != "588000"]
    if ex_kc:
        print(f"    剔除588000后: 中位收益 {np.median(ex_kc) * 100:+.1f}% | "
              f"均值 {np.mean(ex_kc) * 100:+.1f}%")

    # ══ [C] 结构体温计 ══
    print("\n" + "=" * 88)
    print("  [C] 结构体温计 — 时间序列动量健康度 corr(过去20日收益, 未来20日收益)")
    print("       滚动 250 交易日。>0 = 趋势延续结构存活; ≤0 = 结构失效(反转市)")
    print("=" * 88)
    print(f"  {'标的':<12}{'最新值':>9}{'历史中位':>10}{'为正占比':>10}{'最近1年均值':>14}")
    print("  " + "-" * 58)
    health = {}
    for code, name, kind in PEERS:
        if code not in data:
            continue
        s = data[code]
        ic, _ = ts_momentum_health(s.values.astype(float))
        if ic is None or ic.dropna().empty:
            continue
        icd = ic.dropna()
        last = icd.iloc[-1]
        recent = icd.iloc[-250:] if len(icd) >= 250 else icd
        pos_rate = (icd > 0).sum() / len(icd) * 100
        health[code] = (last, icd.median(), pos_rate, recent.mean())
        print(f"  {name:<12}{last:>+9.3f}{icd.median():>+10.3f}{pos_rate:>9.0f}%"
              f"{recent.mean():>+14.3f}")

    print("\n  ── 逐年趋势健康度(588000 基准) ──")
    ic_kc, _ = ts_momentum_health(kc.values.astype(float))
    if ic_kc is not None:
        icd = ic_kc.dropna()
        ser = pd.Series(icd.values, index=kc.index[-len(icd):])
        by_year = ser.groupby(ser.index.year).mean()
        print(f"    {'年份':<8}{'均值IC':>10}{'为正占比':>10}")
        for y, v in by_year.items():
            seg = ser[ser.index.year == y]
            print(f"    {y:<8}{v:>+10.3f}{(seg > 0).sum() / len(seg) * 100:>9.0f}%")

    # 对照: 隔夜动量结构的体温计定义
    print("\n" + "=" * 88)
    print("  [D] 两种结构的失效监测指标对照")
    print("=" * 88)
    print("""
    ┌────────────────┬────────────────────────┬────────────────────────┐
    │                │ 隔夜动量 (A/B/R3)      │ 趋势跟随 (科创50)      │
    ├────────────────┼────────────────────────┼────────────────────────┤
    │ 结构本质       │ 隔夜情绪溢价+上午兑现  │ 趋势延续(时间序列动量) │
    │ 体温计         │ 滚动60笔「上午溢价」   │ 滚动250日「动量IC」    │
    │ 计算           │ 11:05收益 − 14:50收益  │ corr(过去20日,未来20日)│
    │ 健康阈值       │ > 0 (实测 +0.53pp)     │ > 0 (见上表)           │
    │ 失效信号       │ 连续跌破0 → 日内路径   │ 连续转负 → 变反转市,   │
    │                │ 形状反转, 下午不再回吐 │ 追涨杀跌反向收割       │
    │ 采样频率       │ 每 20~60 笔            │ 每 1~3 个月            │
    │ 最小样本       │ 60 笔(信噪比勉强)      │ 250 日×1 标的         │
    └────────────────┴────────────────────────┴────────────────────────┘
    """)
    print("  ★ 关键: 两个指标都在【策略之外】计算 —— 不依赖策略是否开仓,")
    print("    即使策略空仓也能照常更新, 所以能在策略亏损【之前】给出预警。")


if __name__ == "__main__":
    main()

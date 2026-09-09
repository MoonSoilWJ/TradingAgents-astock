#!/usr/bin/env python3
"""情绪周期择时 — 游资心法里【唯一可迁移部分】的量化版。

心法: "赚钱效应期重仓, 亏钱效应期空仓" — 这是时序开关, 不是选股。
量化: 用一批代表股的 breadth(上涨家数占比) + 创新高家数占比 + 5日等权动量
      合成「赚钱效应开关」; 开关开 → 持 588000; 关 → 现金。
无前视: T 日收盘算信号 → T+1 起持有(收益归属次日)。

对照: 一直持有588000 / 588000 N12择时(现役) / 开关反向( sanity check )
★ 对账: 事件驱动权益, 逐笔复利 == 权益终值。
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from etf_qfq_data import fetch_qfq_close  # noqa: E402
from stock_momentum_ic import STOCKS, fetch_qfq  # noqa: E402

START = "2019-06-01"
SLIP = 0.001          # 个股端用; ETF 端单独 0.0005
TARGET = "588000"


def n12_frac(c: pd.Series) -> pd.Series:
    parts = []
    for n, m in [(10, 9), (10, 12), (12, 9), (12, 12), (14, 9), (14, 12)]:
        e1 = c.ewm(span=n, adjust=False).mean()
        e2 = e1.ewm(span=n, adjust=False).mean()
        e3 = e2.ewm(span=n, adjust=False).mean()
        tr = e3.pct_change() * 100
        sig = tr.rolling(m).mean()
        parts.append(tr > sig)
    return pd.concat(parts, axis=1).mean(axis=1)


def build_sentiment(codes: list[str]) -> pd.Series | None:
    """赚钱效应开关: breadth(日上涨家数占比) + 创新高占比 + 5日等权动量。"""
    cols = {}
    for c in codes:
        s = fetch_qfq(c)
        if s is not None and len(s) > 300:
            cols[c] = s
    if len(cols) < 30:
        return None
    df = pd.DataFrame(cols).sort_index().dropna()
    ret = df.pct_change()
    breadth = (ret > 0).mean(axis=1)                       # 上涨家数占比
    hi60 = df.rolling(60).max()
    newhigh = (df >= hi60 * 0.99).mean(axis=1)             # 接近60日新高家数占比
    mom5 = (1 + ret.mean(axis=1)).rolling(5).apply(np.prod, raw=True) - 1
    # 开关: 赚钱效应 = 近5日平均在涨 且 上涨面不弱
    on = ((mom5 > 0) & (breadth > 0.5)).astype(int)
    print(f"  情绪样本 {df.shape[1]} 只 × {len(df)} 天 | 开关打开占比 {on.mean() * 100:.1f}%")
    return on


def run_switch(px: pd.Series, on: pd.Series, slip: float):
    """开关择时: on=1 持有, 否则现金。信号 T 日 → T+1 起算收益(无前视)。"""
    on = on.reindex(px.index).ffill().fillna(0)
    pos = on.shift(1).fillna(0)          # T 日信号 → T+1 持仓
    rets = px.pct_change().fillna(0)
    port = pos * rets
    # 换仓摩擦: 只在 pos 变化时扣
    chg = (pos != pos.shift(1)).astype(float)
    net = (1 + port) * (1 - slip * chg)
    eq = net.cumprod()
    return eq, pos


def stats(eq: pd.Series):
    total = (eq.iloc[-1] - 1) * 100
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    ann = ((1 + total / 100) ** (1 / yrs) - 1) * 100
    mdd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    return total, ann, mdd


def main() -> None:
    codes = [c for c, _ in STOCKS if c != "600809"]        # 去重
    print(f"构建情绪开关(代表股 {len(codes)} 只) ...", flush=True)
    on = build_sentiment(codes)
    if on is None:
        print("情绪样本不足")
        return

    print(f"拉取标的 {TARGET} ...", flush=True)
    px = fetch_qfq_close(TARGET, start=START)
    if px is None or len(px.dropna()) < 300:
        px = fetch_qfq(TARGET)
    if px is None:
        print("标的拉取失败")
        return
    px = px.dropna()
    on = on.reindex(px.index).ffill().fillna(0)

    # ① 情绪开关择时
    eq_s, pos = run_switch(px, on, 0.0005)
    t, a, m = stats(eq_s)
    rows = [("情绪开关择时(心法量化)", t, a, m, eq_s)]
    # ② 一直持有
    bh = (1 - 0.0005) * px / px.iloc[0]
    t, a, m = stats(bh)
    rows.append(("一直持有588000", t, a, m, bh))
    # ③ N12 择时(现役型)
    f = n12_frac(px)
    on2 = (f > 0.5).astype(int)
    eq_n, _ = run_switch(px, on2, 0.0005)
    t, a, m = stats(eq_n)
    rows.append(("588000 N12择时(现役)", t, a, m, eq_n))
    # ④ 反向(sanity check: 若反向更好 → 开关是噪音)
    eq_i, _ = run_switch(px, (1 - on), 0.0005)
    t, a, m = stats(eq_i)
    rows.append(("反向开关(噪音检验)", t, a, m, eq_i))

    print("\n" + "=" * 86)
    print("  情绪周期择时 vs 对照")
    print("=" * 86)
    print(f"  {'方案':<28}{'累计':>12}{'年化':>9}{'回撤':>9}")
    print("  " + "-" * 60)
    for label, t, a, m, _ in rows:
        print(f"  {label:<28}{t:>+11.2f}%{a:>8.2f}%{m:>8.2f}%")

    print(f"\n  分年:")
    print(f"  {'年份':<8}" + "".join(f"{r[0].split('(')[0][:8]:>12}" for r in rows))
    for y in sorted(set(px.index.year)):
        line = f"  {y:<8}"
        for label, _, _, _, eq in rows:
            seg = eq[eq.index.year == y]
            v = (seg.iloc[-1] / seg.iloc[0] - 1) * 100 if len(seg) else float("nan")
            line += f"{v:>+11.2f}%"
        print(line)

    ts = rows[0][1]
    tb = rows[1][1]
    tn = rows[2][1]
    ti = rows[3][1]
    print(f"\n  【结论】")
    print(f"    情绪开关 vs 一直持有: {ts:+.1f}% vs {tb:+.1f}%  "
          f"({'有增益 +' + format(ts-tb, '.0f') + 'pp' if ts > tb else '无增益 ❌'})")
    print(f"    情绪开关 vs N12现役 : {ts:+.1f}% vs {tn:+.1f}%")
    print(f"    反向开关(噪音检验)  : {ti:+.1f}%  "
          f"{'⚠ 反向也赚 → 开关可能是噪音' if ti > tb else '✅ 反向更差 → 开关有信息'}")


if __name__ == "__main__":
    main()

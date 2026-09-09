#!/usr/bin/env python3
"""并联 N12 择时 — 每个标的独立时序开关(无横截面比较), 与轮动的本质区别。

规则: 每日等权持有【所有 N12 簇看多】的标的(每日再平衡近似),
      摩擦按权重变化×slip 扣; 全体看空 = 现金。
      = "N12 信号过滤的等权指数", 无排序/无选最强/无横截面比较。

对照: 纯科创50择时 / 无过滤等权 / 科创50+创业板+新能源+中证500 四只并联
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from backtest_588000_n12 import COMB_N12  # noqa: E402
from etf_qfq_data import fetch_qfq_close  # noqa: E402
from kc50_structure_check import PEERS  # noqa: E402

START = "2019-06-01"
SLIP = 0.0005


def trix_cross(c: pd.Series, n: int, m: int) -> pd.Series:
    e1 = c.ewm(span=n, adjust=False).mean()
    e2 = e1.ewm(span=n, adjust=False).mean()
    e3 = e2.ewm(span=n, adjust=False).mean()
    tr = e3.pct_change() * 100
    sig = tr.rolling(m).mean()
    return tr > sig


def n12_frac(c: pd.Series) -> pd.Series:
    return pd.concat([trix_cross(c, n, m) for n, m in COMB_N12], axis=1).mean(axis=1)


def run_filtered(closes: pd.DataFrame, fracs: pd.DataFrame, slip=SLIP):
    """每日等权持有 N12>0.5 的标的(再平衡近似+摩擦)。"""
    on = fracs > 0.5
    rets = closes.pct_change()
    port, eq, cur, prev_w = [], [], 1.0, pd.Series(dtype=float)
    for d in closes.index[1:]:
        today_on = on.loc[d]
        members = today_on[today_on].index
        if len(members) == 0:
            r = 0.0
            w = pd.Series(dtype=float)
        else:
            r = rets.loc[d, members].mean()
            w = pd.Series(1 / len(members), index=members)
        # 摩擦 = 权重变化
        turn = 0.0
        allc = set(w.index) | set(prev_w.index)
        for c in allc:
            turn += abs(w.get(c, 0.0) - prev_w.get(c, 0.0))
        cur *= (1 + r) * (1 - slip * turn)
        prev_w = w
        eq.append(cur)
        port.append(r * 100)
    return pd.Series(eq, index=closes.index[1:])


def run_single(c: pd.Series, frac: pd.Series, slip=SLIP):
    """单标的择时(事件驱动, 对齐科创50 sim)。"""
    cur, entry, eqs = 1.0, None, []
    for d in c.index:
        on = frac.at[d] > 0.5
        if on and entry is None:
            entry = c.at[d]
            cur *= 1 - slip
        elif not on and entry is not None:
            cur *= (c.at[d] / entry) * (1 - slip)
            entry = None
        eqs.append(cur)
    return pd.Series(eqs, index=c.index)


def stats(eq: pd.Series):
    total = (eq.iloc[-1] - 1) * 100
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    ann = ((1 + total / 100) ** (1 / yrs) - 1) * 100
    mdd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    return total, ann, mdd


def main() -> None:
    print("拉取前复权数据 ...", flush=True)
    cols = {}
    for code, name, _k in PEERS:
        try:
            s = fetch_qfq_close(code, start=START)
        except Exception:
            s = None
        if s is not None and len(s) > 300:
            cols[code] = s
    df = pd.DataFrame(cols).sort_index().dropna()
    print(f"对齐 {len(df)} 天 {df.index[0].date()}~{df.index[-1].date()} | {len(df.columns)} 只\n")

    fracs = pd.DataFrame({c: n12_frac(df[c]) for c in df.columns})

    print("=" * 88)
    print("  [1] 并联 N12 择时 vs 单标的 vs 无过滤")
    print("      (并联=各自独立时序开关, 看多者等权持有; 无横截面排序)")
    print("=" * 88)
    rows = []
    eq_p = run_filtered(df, fracs)
    t, a, m = stats(eq_p)
    rows.append(("全部14只并联N12", t, a, m, eq_p))
    sub4 = [c for c in ["588000", "159915", "516160", "510500"] if c in df.columns]
    eq_4 = run_filtered(df[sub4], fracs[sub4])
    t, a, m = stats(eq_4)
    rows.append(("四只并联(科创/创业/新能/500)", t, a, m, eq_4))
    eq_k = run_single(df["588000"], fracs["588000"])
    t, a, m = stats(eq_k)
    rows.append(("科创50 单标的N12(现役型)", t, a, m, eq_k))
    ew = (1 - SLIP) * (1 + df.pct_change().mean(axis=1)).cumprod()
    t, a, m = stats(ew)
    rows.append(("等权持有(无过滤)", t, a, m, ew))

    print(f"  {'方案':<30}{'累计':>12}{'年化':>9}{'回撤':>9}")
    print("  " + "-" * 62)
    for label, t, a, m, _ in rows:
        print(f"  {label:<30}{t:>+11.2f}%{a:>8.2f}%{m:>8.2f}%")

    print("\n" + "=" * 88)
    print("  [2] 分年")
    print("=" * 88)
    print(f"  {'年份':<8}" + "".join(f"{r[0].split('(')[0][:10]:>14}" for r in rows))
    print("  " + "-" * (8 + 14 * len(rows)))
    for y in sorted(set(df.index.year)):
        line = f"  {y:<8}"
        for label, _, _, _, eq in rows:
            seg = eq[eq.index.year == y]
            v = (seg.iloc[-1] / seg.iloc[0] - 1) * 100 if len(seg) else float("nan")
            line += f"{v:>+13.2f}%"
        print(line)

    print("\n  【结论】")
    tp = rows[0][1]
    te = rows[3][1]
    tk = rows[2][1]
    print(f"    并联过滤 vs 无过滤等权: {tp:+.1f}% vs {te:+.1f}%  "
          f"({'过滤有效 +' + format(tp-te, '.0f') + 'pp' if tp > te else '过滤无增益 ❌'})")
    print(f"    并联 vs 科创50单标的: {tp:+.1f}% vs {tk:+.1f}%  "
          f"(分散降回撤 vs 集中吃趋势的取舍)")


if __name__ == "__main__":
    main()

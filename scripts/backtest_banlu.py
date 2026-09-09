#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""半路板策略【严格版】回测 — 逐日动态选池, 无前视。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
策略
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  T 日盘中触及 +6%(相对昨收) → 以该价位买入 → T+1 收盘卖出。等权持有当日全部触发票。

  ★ 与"打板"的本质区别: 6% 买入时股票尚未涨停 → 必然有卖单 → 一定成交。
    所以 ask1>0 天然满足, 不存在打板那种"一字板买不到"的不可回测陷阱。
    这是半路板相对打板最大的优势: 回测结论可信。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
严格在哪 (相对快速版)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1. 股票池【逐日动态】: 用截至 T-1 的过去 20 日平均成交额排名取前 N,
     而不是用"今天的活跃股名单"回测历史(那有前视: 提前知道谁后来变活跃)
  2. 买入价 = max(开盘价, 昨收×1.06): 高开超 6% 的票只能在开盘价买, 不吃假滑点
  3. 成本敏感度: 0.1% / 0.2% / 0.3% 三档(追涨的滑点比普通买入大)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠ 已知局限 (无法用日K消除)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  · 量比条件用"当日全天成交额" → 盘中不可知 → 含前视, 结果偏乐观(单独标注)
  · 触及 6% 的那一刻能否成交取决于盘口厚度, 日K无法还原(滑点用成本档近似)
  · 未模拟涨停封死前 6% 挂单未成交的情形

用法: python3 scripts/backtest_banlu.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

MB_DAILY = Path.home() / ".tradingagents" / "youzi" / "mb_daily.pkl"
MB_FLOAT = Path.home() / ".tradingagents" / "youzi" / "mb_float.json"
POOL_N = 600
WARMUP = 60          # 60 日高点 + 20 日池


def load():
    df = pd.read_pickle(MB_DAILY)
    piv = lambda v: df.pivot(index="date", columns="code", values=v).sort_index()
    d = {"close": piv("close"), "high": piv("high"),
         "open": piv("open"), "amount": piv("amount")}
    with open(MB_FLOAT) as f:
        fs = pd.Series({k: float(v) for k, v in json.load(f).items()})
    fs = fs.reindex(d["close"].columns)
    return d, fs


def streak_matrix(lim: pd.DataFrame) -> pd.DataFrame:
    """每个 cell = 截至该日(含)的连续涨停天数。"""
    out = pd.DataFrame(0.0, index=lim.index, columns=lim.columns)
    for c in lim.columns:
        s = lim[c].fillna(False).astype(bool)
        out[c] = s.groupby((~s).cumsum()).cumsum()
    return out


def run(close, high, openp, amount, fs, *, thr=0.06, cost=0.002,
        min_vr=0.0, max_streak=99, mv_lo=0, mv_hi=1e9,
        max_dd=200, pool_n=POOL_N, entry="touch", exit_mode="next_close"):
    """返回 (日收益 Series, 触发笔数 Series)。

    entry:  touch = 盘中最高触及(含冲高回落)
            close = 收盘仍在阈值之上(收盘强势确认, ⚠有前视, 仅作方向参考)
    exit_mode: next_close = 次日收盘卖 | next_open = 次日开盘卖(游资竞价出)
    """
    prev = close.shift(1)
    nxt = (close.shift(-1) if exit_mode == "next_close"
           else openp.shift(-1))
    buy = np.maximum(openp, prev * (1 + thr))
    if entry == "touch":
        touch = (high / prev - 1) >= thr
    else:                                   # close: 收盘仍强势
        touch = (close / prev - 1) >= thr

    # 逐日动态池: 截至 T-1 的 20 日均额排名前 pool_n
    avg_amt = amount.rolling(20).mean().shift(1)
    in_pool = avg_amt.rank(axis=1, ascending=False) <= pool_n

    sig = touch & in_pool

    ret = nxt / buy - 1                       # 次日收盘卖

    if min_vr > 0:
        vr = amount / amount.shift(1)         # ⚠ 前视: 当日全天额
        sig &= (vr >= min_vr)
    if max_streak < 99:
        lim = (close.pct_change() >= 0.098)
        st = streak_matrix(lim).shift(1)      # 截至昨日
        sig &= (st <= max_streak)
    if mv_lo > 0 or mv_hi < 1e9:
        mv = fs * buy / 1e8
        sig &= (mv >= mv_lo) & (mv <= mv_hi)
    if max_dd < 200:
        h60 = high.rolling(60).max().shift(1)
        dd = (h60 - buy) / h60 * 100
        sig &= (dd <= max_dd)

    port = (ret.where(sig)).mean(axis=1) - cost   # 双边成本
    cnt = sig.sum(axis=1)
    return port.dropna(), cnt.dropna()


def stats(port: pd.Series, cnt: pd.Series, label: str) -> dict:
    if len(port) == 0:
        return {"label": label, "n": 0}
    eq = (1 + port).cumprod()
    total = (eq.iloc[-1] - 1) * 100
    yrs = (port.index[-1] - port.index[0]).days / 365.25
    ann = ((1 + total / 100) ** (1 / yrs) - 1) * 100
    mdd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    wr = (port > 0).mean() * 100
    return {"label": label, "n": len(port), "total": total, "ann": ann,
            "mdd": mdd, "wr": wr, "dret": port.mean() * 100,
            "npd": cnt.mean(), "eq": eq}


def main() -> int:
    d, fs = load()
    close, high, openp, amount = d["close"], d["high"], d["open"], d["amount"]
    # 预热后开始
    sl = slice(pd.Timestamp("2022-04-01"), None)
    close, high, openp, amount = close.loc[sl], high.loc[sl], openp.loc[sl], amount.loc[sl]

    print("=" * 94)
    print("  半路板策略【严格版】回测 — 逐日动态选池(截至T-1成交额前600), 无前视")
    print(f"  区间 {close.index[0].date()} ~ {close.index[-1].date()} | "
          f"{len(close.columns)} 只主板 | {len(close)} 天")
    print("=" * 94)

    # 基准: 等权持有池内股票
    avg_amt = amount.rolling(20).mean().shift(1)
    in_pool = avg_amt.rank(axis=1, ascending=False) <= POOL_N
    base = (close.pct_change().where(in_pool)).mean(axis=1).dropna()
    bs = stats(base, pd.Series(0, index=base.index), "等权持有池(基准)")

    rows = []
    # 成本敏感度
    for cost, tag in ((0.001, "0.1%"), (0.002, "0.2%"), (0.003, "0.3%")):
        p, c = run(close, high, openp, amount, fs, thr=0.06, cost=cost)
        s = stats(p, c, f"半路板6% (成本{tag})")
        rows.append(s)

    print(f"\n  【一】成本敏感度 (阈值6%, 无其他过滤)")
    print(f"  {'口径':<24}{'日均收益':>10}{'胜率':>8}{'累计':>14}{'年化':>10}{'回撤':>9}{'日均票数':>9}")
    print("  " + "-" * 84)
    print(f"  {bs['label']:<24}{bs['dret']:>+9.3f}%{bs['wr']:>7.1f}%"
          f"{bs['total']:>+13.2f}%{bs['ann']:>9.2f}%{bs['mdd']:>8.2f}%{'—':>9}")
    for s in rows:
        print(f"  {s['label']:<24}{s['dret']:>+9.3f}%{s['wr']:>7.1f}%"
              f"{s['total']:>+13.2f}%{s['ann']:>9.2f}%{s['mdd']:>8.2f}%{s['npd']:>9.1f}")

    # 阈值消融 (成本 0.2%)
    print(f"\n  【二】触发阈值消融 (成本 0.2%)")
    for thr in (0.04, 0.05, 0.06, 0.08, 0.09):
        p, c = run(close, high, openp, amount, fs, thr=thr, cost=0.002)
        s = stats(p, c, f"阈值 {thr*100:.0f}%")
        print(f"  {s['label']:<24}{s['dret']:>+9.3f}%{s['wr']:>7.1f}%"
              f"{s['total']:>+13.2f}%{s['ann']:>9.2f}%{s['mdd']:>8.2f}%{s['npd']:>9.1f}")

    # 筛选条件消融
    print(f"\n  【三】筛选条件消融 (阈值6%, 成本0.2%)")
    abl = [
        ("无过滤(基线)", {}),
        ("量比≥2 ⚠前视", {"min_vr": 2.0}),
        ("量比≥3 ⚠前视", {"min_vr": 3.0}),
        ("排除≥4板", {"max_streak": 3}),
        ("仅首板(streak=0)", {"max_streak": 0}),
        ("流通30~200亿", {"mv_lo": 30, "mv_hi": 200}),
        ("距60日高≤15%", {"max_dd": 15}),
        ("全筛选组合", {"min_vr": 2.0, "max_streak": 3,
                     "mv_lo": 30, "mv_hi": 200, "max_dd": 15}),
    ]
    for label, kw in abl:
        p, c = run(close, high, openp, amount, fs, thr=0.06, cost=0.002, **kw)
        s = stats(p, c, label)
        print(f"  {s['label']:<24}{s['dret']:>+9.3f}%{s['wr']:>7.1f}%"
              f"{s['total']:>+13.2f}%{s['ann']:>9.2f}%{s['mdd']:>8.2f}%{s['npd']:>9.1f}")

    # 分年 (基线 6%, 成本0.2%)
    p, c = run(close, high, openp, amount, fs, thr=0.06, cost=0.002)
    s = stats(p, c, "")
    print(f"\n  【四】分年 (阈值6%, 成本0.2%)")
    print(f"  {'年份':<8}{'半路板':>12}{'等权持有':>12}{'日均票数':>10}{'胜率':>8}")
    for y in sorted(set(p.index.year) | set(base.index.year)):
        py = p[p.index.year == y]
        by = base[base.index.year == y]
        cy = c[c.index.year == y]
        ry = (1 + py).prod() - 1 if len(py) else float("nan")
        rby = (1 + by).prod() - 1 if len(by) else float("nan")
        wr = (py > 0).mean() * 100 if len(py) else float("nan")
        print(f"  {y:<8}{ry*100:>+11.2f}%{rby*100:>+11.2f}%"
              f"{cy.mean() if len(cy) else float('nan'):>10.1f}{wr:>7.1f}%")

    # 变体对比
    print(f"\n  【五】入场/离场口径对比 (阈值6%, 成本0.2%)")
    combos = [
        ("触及6% → 次日收盘", dict(entry="touch", exit_mode="next_close")),
        ("触及6% → 次日开盘", dict(entry="touch", exit_mode="next_open")),
        ("收盘≥6% → 次日收盘 ⚠", dict(entry="close", exit_mode="next_close")),
        ("收盘≥6% → 次日开盘 ⚠", dict(entry="close", exit_mode="next_open")),
        ("收盘≥6%+量比2 → 次日开盘 ⚠",
         dict(entry="close", exit_mode="next_open", min_vr=2.0)),
    ]
    for label, kw in combos:
        p, c = run(close, high, openp, amount, fs, thr=0.06, cost=0.002, **kw)
        s = stats(p, c, label)
        print(f"  {s['label']:<30}{s['dret']:>+9.3f}%{s['wr']:>7.1f}%"
              f"{s['total']:>+13.2f}%{s['ann']:>9.2f}%{s['mdd']:>8.2f}%{s['npd']:>8.1f}")

    # 收盘状态诊断(用 stack 对齐, 避免掩码广播把全样本算进来)
    print(f"\n  【六】诊断: 触及6%买入后, 按【当日收盘形态】分组的次日收益")
    prev = close.shift(1)
    buy = np.maximum(openp, prev * 1.06)
    ret = close.shift(-1) / buy - 1
    touch = (high / prev - 1) >= 0.06
    avg_amt = amount.rolling(20).mean().shift(1)
    in_pool = avg_amt.rank(axis=1, ascending=False) <= POOL_N
    sel = touch & in_pool
    cp = (close / prev - 1).where(sel)       # 当日收盘涨幅(仅触发样本)
    rr = ret.where(sel)
    tb = pd.DataFrame({"cp": cp.stack(), "ret": rr.stack()}).dropna()
    bins = [-100, 0, 3, 6, 9.8, 1000]
    labels = ["收跌(冲高回落)", "收 0~3%", "收 3~6%", "收 6~9.8%", "封板(≥9.8%)"]
    tb["grp"] = pd.cut(tb["cp"] * 100, bins=bins, labels=labels)
    print(f"  {'当日收盘形态':<22}{'占比':>8}{'次日收益':>11}{'胜率':>8}{'笔数':>10}")
    for g in labels:
        sub = tb[tb["grp"] == g]
        if len(sub) == 0:
            continue
        print(f"  {g:<22}{len(sub)/len(tb)*100:>7.1f}%{sub['ret'].mean()*100:>+10.3f}%"
              f"{(sub['ret'] > 0).mean()*100:>7.1f}%{len(sub):>10,}")
    print(f"  {'合计':<22}{100.0:>7.1f}%{tb['ret'].mean()*100:>+10.3f}%"
          f"{(tb['ret'] > 0).mean()*100:>7.1f}%{len(tb):>10,}")

    # 情绪过滤: 只在涨停家数多的日子做(盘中可知)
    print(f"\n  【七】情绪过滤: 只在【昨日涨停家数】高的日子做 (成本0.2%, 次日收盘卖)")
    nlimit = ((close / close.shift(1) - 1) >= 0.098).sum(axis=1)
    med = nlimit.rolling(60).median().shift(1)     # 过去60日中位数(无前视)
    q25 = nlimit.rolling(60).quantile(0.25).shift(1)
    q75 = nlimit.rolling(60).quantile(0.75).shift(1)
    print(f"  {'口径':<26}{'天数':>7}{'日均收益':>10}{'胜率':>8}{'累计':>13}{'日均票数':>9}")
    for label, cond in (("全部日子", pd.Series(True, index=close.index)),
                        ("涨停数 ≥ 60日中位", nlimit.shift(1) >= med),
                        ("涨停数 ≥ 60日75分位", nlimit.shift(1) >= q75),
                        ("涨停数 ≤ 60日25分位", nlimit.shift(1) <= q25)):
        p, c = run(close, high, openp, amount, fs, thr=0.06, cost=0.002)
        pp = p[cond.reindex(p.index).fillna(False)]
        if len(pp) < 20:
            continue
        eq = (1 + pp).cumprod()
        print(f"  {label:<26}{len(pp):>7}{pp.mean()*100:>+9.3f}%"
              f"{(pp > 0).mean()*100:>7.1f}%{(eq.iloc[-1]-1)*100:>+12.2f}%"
              f"{c.reindex(pp.index).mean():>9.1f}")

    # 接力: T日封板 → T+1竞价买入(唯一"有溢价+买得到"的路径?)
    print(f"\n  【八】接力: T日收盘涨停 → T+1【竞价】买入 (排除T+1一字板, 否则买不到)")
    lim_t = (close / close.shift(1) - 1) >= 0.098
    nx_open = openp.shift(-1)
    yizi = (nx_open / close - 1) >= 0.098          # T+1 一字开盘 → 排不上队
    buyable = lim_t & ~yizi & in_pool
    r_same = close.shift(-1) / nx_open - 1          # 竞价买 → T+1收盘卖
    r_hold = close.shift(-2) / nx_open - 1          # 竞价买 → T+2收盘卖
    print(f"  {'口径':<30}{'日均收益':>10}{'胜率':>8}{'累计':>14}{'年均笔数':>10}")
    for label, r in (("竞价买 → T+1收盘卖", r_same), ("竞价买 → T+2收盘卖", r_hold)):
        p = (r.where(buyable).mean(axis=1) - 0.002).dropna()
        if len(p) < 20:
            continue
        s = stats(p, buyable.sum(axis=1).dropna(), label)
        print(f"  {label:<30}{s['dret']:>+9.3f}%{s['wr']:>7.1f}%"
              f"{s['total']:>+13.2f}%{s['npd']*242:>10.0f}")
    nb = buyable.sum(axis=1).mean()
    print(f"  (日均可买 {nb:.1f} 只; T日涨停 {lim_t.sum(axis=1).mean():.1f} 只/日, "
          f"其中 T+1 一字板 {yizi.sum(axis=1).mean():.1f} 只 → 买不到)")

    print(f"\n  ⚠ 量比/收盘口径用当日全天数据(盘中不可知), 含前视 → 标注项结果偏乐观")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

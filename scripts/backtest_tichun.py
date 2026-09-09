#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""题材纯度 × 空间板接力回测 — 游资方向最后一个数据可验证组合。

假设: 空间板接力亏钱, 是因为一半是没有主线题材支撑的"孤板"。
      只接"所属题材当日 ≥3 只热门(主线确认)"的空间板, 是否转正?

题材: 同花顺人工标注(get_hot_stocks 支持历史日期), 逐日缓存。
主线: 空间板自身题材标签中, 最高频标签当日出现 ≥MIN_THEME 次。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass

MB = Path.home() / ".tradingagents" / "youzi" / "mb_daily.pkl"
TCACHE = Path.home() / ".tradingagents" / "youzi" / "topics_cache.json"
COST = 0.002
MIN_THEME = 3
IS_END = pd.Timestamp("2024-12-31")


def streak_matrix(lim):
    out = pd.DataFrame(0.0, index=lim.index, columns=lim.columns)
    for c in lim.columns:
        s = lim[c].fillna(False).astype(bool)
        out[c] = s.groupby((~s).cumsum()).cumsum()
    return out


def perf(port):
    if len(port) < 20:
        return (np.nan,) * 4
    eq = (1 + port).cumprod()
    tot = (eq.iloc[-1] - 1) * 100
    yrs = max((port.index[-1] - port.index[0]).days / 365.25, 0.1)
    ann = ((1 + tot / 100) ** (1 / yrs) - 1) * 100
    mdd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    return tot, ann, port.mean() * 100, mdd


def main() -> int:
    df = pd.read_pickle(MB)
    piv = lambda v: df.pivot(index="date", columns="code", values=v).sort_index()
    close, openp, amount = piv("close"), piv("open"), piv("amount")
    # mb_daily 的 code 列是 pyarrow string: pivot 后 ArrowDtype 列在 .loc 逐格
    # 赋值时会触发"新增列"bug → 统一转成普通 str
    for m in (close, openp, amount):
        m.columns = [str(c) for c in m.columns]
    close = close[close.index >= pd.Timestamp("2022-04-01")]
    openp = openp.reindex(close.index)
    amount = amount.reindex(close.index)
    prev = close.shift(1)
    lim = close / prev - 1 >= 0.098
    st = streak_matrix(lim)
    max_st = st.where(lim).max(axis=1)
    is_space = st.eq(max_st, axis=0) & lim & (st >= 3)
    nx_open, nx_close = openp.shift(-1), close.shift(-1)
    yizi = nx_open / close - 1 >= 0.098
    in_pool = (amount.rolling(20).mean().shift(1)
               .rank(axis=1, ascending=False) <= 600)
    nlimit = lim.sum(axis=1)
    hot_mood = nlimit.shift(1) >= nlimit.rolling(60).median().shift(1)

    base_sig = is_space & in_pool & ~yizi
    need = [str(pd.Timestamp(d).date()) for d in close.index[base_sig.any(axis=1)]]

    cache = json.loads(TCACHE.read_text()) if TCACHE.exists() else {}
    missing = [d for d in need if d not in cache]
    print(f"[题材] 需 {len(need)} 天 | 已缓存 {len(need)-len(missing)} | "
          f"补拉 {len(missing)} 天 ...", flush=True)
    if missing:
        from tradingagents.agents.utils.agent_utils import get_hot_stocks
        for i, ds in enumerate(missing):
            topics = {}
            try:
                for line in str(get_hot_stocks.invoke(
                        {"curr_date": ds})).splitlines():
                    if ":" in line and "|" in line:
                        w = line.split()
                        if w and w[0].isdigit() and len(w[0]) == 6:
                            topics[w[0]] = line.split("|")[-1].strip()
            except Exception:
                topics = {}
            cache[ds] = topics
            if (i + 1) % 50 == 0:
                TCACHE.parent.mkdir(parents=True, exist_ok=True)
                TCACHE.write_text(json.dumps(cache, ensure_ascii=False))
                print(f"  ...{i+1}/{len(missing)}", flush=True)
        TCACHE.parent.mkdir(parents=True, exist_ok=True)
        TCACHE.write_text(json.dumps(cache, ensure_ascii=False))

    # 预计算每日题材频次
    freq_by_day = {}
    for ds in need:
        freq = Counter()
        for tg in (cache.get(ds) or {}).values():
            for tag in str(tg).replace("、", "+").split("+"):
                if tag.strip():
                    freq[tag.strip()] += 1
        freq_by_day[ds] = freq

    def theme_ok(ds: str, code: str, min_t: int) -> bool:
        tags = (cache.get(ds) or {}).get(code)
        if not tags:
            return False
        f = freq_by_day.get(ds) or Counter()
        return max((f.get(t.strip(), 0)
                    for t in str(tags).replace("、", "+").split("+")),
                   default=0) >= min_t

    # 逐日收集坐标 → numpy 一次性构建(绕开 loc 逐格赋值)
    idx_map = {d: i for i, d in enumerate(close.index)}
    col_map = {c: j for j, c in enumerate(close.columns)}
    arr3 = np.zeros(close.shape, dtype=bool)
    arr5 = np.zeros(close.shape, dtype=bool)
    n3 = n5 = 0
    for dt in close.index:
        s = base_sig.loc[dt]
        if not s.any():
            continue
        ds = str(pd.Timestamp(dt).date())
        i = idx_map[dt]
        for c in s.index[s]:
            j = col_map[c]
            if theme_ok(ds, c, 3):
                arr3[i, j] = True
                n3 += 1
                if theme_ok(ds, c, 5):
                    arr5[i, j] = True
                    n5 += 1
    pure3 = pd.DataFrame(arr3, index=close.index, columns=close.columns)
    pure5 = pd.DataFrame(arr5, index=close.index, columns=close.columns)

    r_t1 = nx_close / nx_open - 1
    r_t2 = close.shift(-2) / nx_open - 1

    print("\n" + "=" * 94)
    print("  空间板接力 × 题材纯度 (T+1竞价买排一字; 卖出=T+1收盘 / T+2收盘)")
    print("=" * 94)
    print(f"  {'档位':<30}{'卖出':<9}{'笔数':>6}{'笔均':>9}{'累计':>11}"
          f"{'年化':>9}{'回撤':>8}{'胜率':>7}")

    tiers = [
        ("对照:空间板(无题材过滤)", base_sig),
        (f"主线≥{MIN_THEME}(题材纯度)", base_sig & pure3),
        ("主线≥5(更严)", base_sig & pure5),
        (f"主线≥{MIN_THEME} + 情绪热",
         pd.DataFrame(base_sig.values & pure3.values &
                      np.broadcast_to(hot_mood.values[:, None], close.shape),
                      index=close.index, columns=close.columns)),
    ]
    best_port, best_lab = None, ""
    for label, sig in tiers:
        for sell_lab, r in (("T+1收盘", r_t1), ("T+2收盘", r_t2)):
            masked = pd.DataFrame(np.where(sig.values, r.values, np.nan),
                                  index=r.index, columns=r.columns)
            port = masked.mean(axis=1) - COST
            port = port.dropna()
            t, a, dr, m = perf(port)
            vals = r.values[sig.values]
            vals = vals[np.isfinite(vals)]
            if len(vals) == 0:
                print(f"  {label:<30}{sell_lab:<9}{'0':>6}")
                continue
            print(f"  {label:<30}{sell_lab:<9}{len(vals):>6}"
                  f"{vals.mean()*100:>+8.2f}%{t:>+10.0f}%{a:>+8.1f}%"
                  f"{m:>7.1f}%{(vals>0).mean()*100:>6.1f}%")
            if sell_lab == "T+1收盘" and np.isfinite(a) and \
                    (best_port is None or a > float(perf(best_port)[1])):
                best_port, best_lab = port, label

    if best_port is not None and len(best_port) > 20:
        gi = pd.to_datetime(pd.Index(best_port.index))
        print(f"\n  【最优档: {best_lab} 分年】")
        for y in sorted(set(gi.year)):
            py = best_port[gi.year == y]
            if len(py) < 15:
                continue
            t, a, dr, m = perf(py)
            print(f"    {y}: 累计 {t:+.0f}%  日均 {dr:+.3f}%  胜率 {(py>0).mean()*100:.1f}%")
        isp = perf(best_port[gi <= IS_END])
        osp = perf(best_port[gi > IS_END])
        print(f"    样本内(~2024末) 年化 {isp[1]:+.1f}% | 样本外(2025+) 年化 {osp[1]:+.1f}%")

    print(f"\n  [统计] 空间板总数 {int(base_sig.sum().sum())} | 主线≥3 {n3} | "
          f"主线≥5 {n5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

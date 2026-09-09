#!/usr/bin/env python3
"""打板策略回测 — 游资打法的最纯粹形式。

策略: T 日涨停(主板≥9.8%/创业板≥19.5%)的股票, 以涨停价(=T日收盘)买入,
      T+1 收盘卖出, 等权持有当日全部涨停股。
      = "昨日涨停股等权持有1天" = 游资打板的直接模拟。

对照: 全样本等权持有(同期市场平均)
★ 无前视: T 日收盘确认涨停 → T+1 收益(不是 T 日涨幅!)

⚠ 执行偏差(真实结果显著劣于回测):
  1. 一字板/秒板买不进 — 能买到的多是"烂板/炸板" → 逆向选择
  2. 涨停封单排队: 散户成交概率极低, 好板几乎买不到
  3. 次日卖出按收盘简化(游资常盘中择机卖)
  ⇒ 回测为【乐观上界】, 真实收益应显著更低。

用法: python3 scripts/daban_backtest.py [--n 400]
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

START = "2022-01-01"
SLIP = 0.001


def get_stock_universe(n: int) -> list[tuple[str, str]]:
    """pytdx 全市场 A 股列表 → 随机抽样(非ST/非退市, 主板+创业板)。"""
    from pytdx.hq import TdxHq_API
    from pytdx.params import TDXParams

    api = TdxHq_API()
    if not api.connect("180.153.18.170", 7709, time_out=5):
        raise RuntimeError("pytdx 连接失败")
    out = []
    try:
        for market, _ in ((TDXParams.MARKET_SH, "sh"), (TDXParams.MARKET_SZ, "sz")):
            cnt = api.get_security_count(market)
            for st in range(0, cnt, 1000):
                lst = api.get_security_list(market, st)
                if not lst:
                    break
                for it in lst:
                    code = str(it.get("code", ""))
                    name = str(it.get("name", ""))
                    if not code.startswith(("60", "00", "30")):
                        continue
                    if "ST" in name or "退" in name or "N" == name[:1]:
                        continue
                    out.append((code, name))
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    random.Random(42).shuffle(out)
    return out[:n]


def fetch_series(code: str):
    """pytdx 日K → DataFrame(close, open)(不复权 + 送转修复)。"""
    from pytdx.hq import TdxHq_API
    from pytdx.params import TDXParams
    from backtest_8wide_ma20_rotation import fix_splits

    market = TDXParams.MARKET_SH if code[0] in "569" else TDXParams.MARKET_SZ
    api = TdxHq_API()
    try:
        if not api.connect("180.153.18.170", 7709, time_out=5):
            return None
        frames = []
        for pg in range(2):
            bars = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, market,
                                         code.encode(), pg * 800, 800)
            if not bars:
                break
            d = api.to_df(bars)
            frames.append(d)
            if len(d) < 800:
                break
        if not frames:
            return None
        f = pd.concat(frames, ignore_index=True)
        f["date"] = pd.to_datetime(f["datetime"]).dt.normalize()
        f = (f[f["date"] >= pd.Timestamp(START)].sort_values("date")
             .drop_duplicates("date"))
        if len(f) < 250:
            return None
        rows = [[str(d.date()), float(v)] for d, v in
                zip(f["date"], f["close"].astype(float))]
        cl, _ = fix_splits(rows)
        idx = [pd.Timestamp(d) for d, _ in cl]
        # open 按同一比例缩放(折算修复后保持形态)
        op_raw = f.set_index("date")["open"].astype(float)
        ratio = pd.Series([v for _, v in cl], index=idx) / \
            pd.Series([float(v) for _, v in rows], index=idx)
        op = (op_raw.reindex(idx) * ratio.reindex(idx)).dropna()
        return pd.DataFrame({"close": [v for _, v in cl], "open": op}, index=idx).sort_index()
    except Exception:
        return None
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=400, help="抽样只数")
    args = ap.parse_args()

    print(f"获取全市场股票列表并抽样 {args.n} 只 ...", flush=True)
    uni = get_stock_universe(args.n)
    print(f"  候选 {len(uni)} 只\n")

    print("拉取日线(close+open) ...", flush=True)
    cols = {}
    for k, (code, name) in enumerate(uni):
        s = fetch_series(code)
        if s is not None:
            cols[code] = s
        if (k + 1) % 100 == 0:
            print(f"  ...{k+1}/{len(uni)} (有效 {len(cols)})", flush=True)
    close = pd.DataFrame({c: v["close"] for c, v in cols.items()}).sort_index()
    openp = pd.DataFrame({c: v["open"] for c, v in cols.items()}).sort_index()
    keep = [c for c in close.columns
            if close[c].dropna().shape[0] > 0 and openp[c].dropna().shape[0] > 0]
    close, openp = close[keep], openp[keep]
    close = close.dropna(axis=1, how="any")
    openp = openp.reindex(close.index)[close.columns]
    print(f"\n对齐 {len(close)} 天 {close.index[0].date()}~{close.index[-1].date()} | "
          f"{len(close.columns)} 只\n")

    ret = close.pct_change()
    thr = pd.DataFrame(
        {c: (0.195 if c.startswith("30") else 0.098) for c in close.columns},
        index=close.index)
    limit_up = ret >= thr
    # 一字板 = 开盘即涨停(全天封死) → 散户买不到, 必须剔除
    open_gap = openp / close.shift(1) - 1
    yizi = open_gap >= thr
    buyable = limit_up & ~yizi          # 盘中封板(有成交) 才可能买到

    def run(flag: pd.DataFrame, nxt: pd.DataFrame):
        port, eq, cur, cnt = [], [1.0], 1.0, []
        for d in close.index[1:-1]:
            nm = flag.loc[d]
            nm = nm[nm].index
            if len(nm) == 0:
                continue
            r = nxt.loc[d, nm].mean()
            if np.isnan(r):
                continue
            cur *= (1 + r) * (1 - SLIP)
            eq.append(cur)
            port.append(r * 100)
            cnt.append(len(nm))
        s = pd.Series(eq, index=close.index[1:len(eq) + 1])
        t = (s.iloc[-1] - 1) * 100
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        a = ((1 + t / 100) ** (1 / yrs) - 1) * 100
        m = ((s - s.cummax()) / s.cummax()).min() * 100
        w = sum(1 for r in port if r > 0) / len(port) * 100 if port else 0
        return s, t, a, m, w, np.mean(port), np.mean(cnt)

    nxt = ret.shift(-1)
    s1, t1, a1, m1, w1, d1, c1 = run(limit_up, nxt)   # 乐观上界(含一字板)
    s2, t2, a2, m2, w2, d2, c2 = run(buyable, nxt)    # 剔除一字板

    ew = (1 - SLIP) * (1 + ret.loc[s1.index].mean(axis=1)).cumprod()
    ew_t = (ew.iloc[-1] - 1) * 100
    ew_mdd = ((ew - ew.cummax()) / ew.cummax()).min() * 100

    print("=" * 92)
    print("  【打板策略】T日涨停买入 → T+1收盘卖出 (等权持有当日涨停股)")
    print("=" * 92)
    print(f"  {'口径':<26}{'日均涨停':>9}{'日均收益':>10}{'胜率':>8}{'累计':>16}{'年化':>10}{'回撤':>9}")
    print("  " + "-" * 88)
    print(f"  {'① 全部涨停(含一字板)':<26}{c1:>8.1f}只{d1:>+9.3f}%{w1:>7.1f}%"
          f"{t1:>+15.2f}%{a1:>9.2f}%{m1:>8.2f}%")
    print(f"  {'② 可买到(剔除一字板)':<26}{c2:>8.1f}只{d2:>+9.3f}%{w2:>7.1f}%"
          f"{t2:>+15.2f}%{a2:>9.2f}%{m2:>8.2f}%")
    print(f"  {'③ 等权持有全样本(对照)':<26}{'—':>9}{'—':>10}{'—':>8}"
          f"{ew_t:>+15.2f}%{'—':>10}{ew_mdd:>8.2f}%")

    print(f"\n  ① vs ② 的差距 = 【一字板不可买】的代价: {t1 - t2:,.0f}pp")
    print(f"  → 回测里最肥的那部分收益, 恰恰来自现实中买不到的一字板")
    print(f"\n  分年(口径②可买到):")
    y = s2.groupby(s2.index.year).apply(lambda x: (x.iloc[-1] / x.iloc[0] - 1) * 100)
    ye = ew.groupby(ew.index.year).apply(lambda x: (x.iloc[-1] / x.iloc[0] - 1) * 100)
    print(f"  {'年份':<8}{'打板(可买到)':>16}{'等权持有':>14}")
    for k in sorted(set(y.index) | set(ye.index)):
        print(f"  {k:<8}{y.get(k, float('nan')):>+15.2f}%{ye.get(k, float('nan')):>+13.2f}%")

    print(f"\n  ⚠ 口径②仍是乐观: 封板排队未模拟、烂板逆向选择、次日按收盘卖(游资常低开卖)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""趋势扫描器 回测 — 「每天买 ★★★ 强趋势」历史表现如何?

逐日重演扫描器(无前视):
  信号(t 日收盘后): N12簇≥2/3 且 close>MA20>MA60 且 当日成交额≥门槛
  组合: 候选中买 40日动量最强一只; 持仓掉出候选 → 卖, 换当时最强; 无候选 → 空仓
  成交: 信号日收盘价(与科创50/扫描器口径一致)

★ 引擎纪律: 事件驱动权益 + 对账断言(eq ≡ Π(1+net)), 不符即抛异常。
对照: 等权持有同池(当前活跃 464 只 — ⚠ 含幸存者偏差, 结论只作方向参考)。
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from backtest_588000_n12 import COMB_N12  # noqa: E402

SLIP = 0.0005
START = "2023-01-01"
AMT_THR = 5e7


def batch_fetch(codes: list[str]) -> dict[str, pd.DataFrame]:
    """pytdx 批量日K(close+amount), 单连接复用 + 折算修复。"""
    from pytdx.hq import TdxHq_API
    from pytdx.params import TDXParams
    from backtest_8wide_ma20_rotation import fix_splits

    api = TdxHq_API()
    if not api.connect("180.153.18.170", 7709, time_out=5):
        raise RuntimeError("pytdx 连接失败")
    out: dict[str, pd.DataFrame] = {}
    try:
        for k, code in enumerate(codes):
            market = TDXParams.MARKET_SH if code[0] in "569" else TDXParams.MARKET_SZ
            frames = []
            try:
                for pg in range(3):
                    bars = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, market,
                                                 code.encode(), pg * 700, 700)
                    if not bars:
                        break
                    d = api.to_df(bars)
                    frames.append(d)
                    if len(d) < 700:
                        break
            except Exception:
                try:
                    api.connect("180.153.18.170", 7709, time_out=5)
                except Exception:
                    pass
                continue
            if not frames:
                continue
            f = pd.concat(frames, ignore_index=True)
            f["date"] = pd.to_datetime(f["datetime"]).dt.normalize()
            f = (f[f["date"] >= pd.Timestamp(START)].sort_values("date")
                 .drop_duplicates("date"))
            if len(f) < 70:
                continue
            rows = [[str(d.date()), float(v)] for d, v in
                    zip(f["date"], f["close"].astype(float))]
            rows, _ = fix_splits(rows)
            out[code] = pd.DataFrame({
                "close": [v for _, v in rows],
            }, index=[pd.Timestamp(d) for d, _ in rows])
            # amount 单独(不参与复权)
            amt = f.set_index("date")["amount"].astype(float)
            out[code]["amount"] = amt.reindex(out[code].index)
            if (k + 1) % 100 == 0:
                print(f"  ...{k+1}/{len(codes)}", flush=True)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    return out


def n12_frac_series(c: pd.Series) -> pd.Series:
    parts = []
    for n, m in COMB_N12:
        e1 = c.ewm(span=n, adjust=False).mean()
        e2 = e1.ewm(span=n, adjust=False).mean()
        e3 = e2.ewm(span=n, adjust=False).mean()
        tr = e3.pct_change() * 100
        sig = tr.rolling(m).mean()
        parts.append(tr > sig)
    return pd.concat(parts, axis=1).mean(axis=1)


def main() -> None:
    import akshare as ak
    print("拉取全市场 ETF 快照 ...", flush=True)
    spot = None
    for k in range(3):
        try:
            spot = ak.fund_etf_spot_em()
            if spot is not None and len(spot) > 0:
                break
        except Exception:
            import time
            time.sleep(2)
    amt_col = next(c for c in spot.columns if "成交额" in c)
    liq = spot[spot[amt_col] >= AMT_THR]
    codes = [str(c) for c in liq["代码"].tolist()]
    name_of = {str(r["代码"]): str(r.get("名称", "")) for _, r in liq.iterrows()}
    print(f"当前活跃 {len(codes)} 只\n")

    print("pytdx 批量拉日K(close+amount) ...", flush=True)
    data = batch_fetch(codes)
    print(f"有效 {len(data)} 只\n")

    # ── 逐日信号(无前视) ──
    print("计算逐日扫描信号 ...", flush=True)
    dates = sorted(set().union(*[set(v.index) for v in data.values()]))
    dates = [d for d in dates if d >= pd.Timestamp(START)]
    T = len(dates)
    n12 = {c: n12_frac_series(v["close"]) for c, v in data.items()}
    ma20 = {c: v["close"].rolling(20).mean() for c, v in data.items()}
    ma60 = {c: v["close"].rolling(60).mean() for c, v in data.items()}
    mom = {c: v["close"] / v["close"].shift(40) - 1 for c, v in data.items()}

    cand = {}          # dates[i] -> [codes]
    for i, d in enumerate(dates):
        lst = []
        for c, v in data.items():
            if d not in v.index:
                continue
            px = v.at[d, "close"]
            amt = v.at[d, "amount"]
            if amt < AMT_THR or px <= 0:
                continue
            if d not in n12[c].index or d not in ma20[c].index or d not in ma60[c].index:
                continue
            if (n12[c].at[d] >= 2 / 3 and px > ma20[c].at[d] > ma60[c].at[d]):
                m = mom[c].at[d]
                if not np.isnan(m):
                    lst.append((m, c))
        cand[d] = lst

    # ── 回测(事件驱动) ──
    cur = 1.0
    held = None
    entry_px = 0.0
    entry_i = -1
    trades = []
    eq = [1.0]
    idle = 0
    for i, d in enumerate(dates):
        lst = cand[d]
        if held is not None:
            if d not in data[held].index:
                # 停牌日: 无报价, 保留持仓等恢复
                eq.append(cur)
                continue
            still = any(c == held for _, c in lst)
            if not still or i == T - 1:
                gross = data[held].at[d, "close"] / entry_px - 1
                net = (1 + gross) * (1 - SLIP) ** 2 - 1
                cur *= 1 + net
                trades.append((dates[entry_i], d, held, net * 100))
                held = None
        if held is None and lst:
            m, c = max(lst)
            held = c
            entry_px = data[c].at[d, "close"]
            entry_i = i
        if held is None:
            idle += 1
        eq.append(cur)

    comp = 1.0
    for *_, net in trades:
        comp *= 1 + net / 100
    if abs(comp - cur) / max(cur, 1e-12) > 1e-9:
        raise AssertionError(f"对账失败 eq={cur:.6f} vs 逐笔={comp:.6f}")

    ser = pd.Series(eq[1:], index=dates)
    total = (cur - 1) * 100
    yrs = (dates[-1] - dates[0]).days / 365.25
    ann = ((1 + total / 100) ** (1 / yrs) - 1) * 100
    mdd = ((ser - ser.cummax()) / ser.cummax()).min() * 100
    wr = sum(1 for t in trades if t[3] > 0) / len(trades) * 100 if trades else 0

    # 等权对照(当日有数据的全部标的等权)
    rets = []
    for d in dates[1:]:
        rs = [v["close"].at[d] / v["close"].shift(1).at[d] - 1
              for c, v in data.items() if d in v.index
              and not np.isnan(v["close"].shift(1).at[d])]
        rets.append(np.mean([r for r in rs if not np.isnan(r)]))
    ew = (1 - SLIP) * np.cumprod(1 + np.array(rets))
    ew_t = (ew[-1] / ew[0] - 1) * 100
    ew_ser = pd.Series(ew, index=dates[1:])
    ew_mdd = ((ew_ser - ew_ser.cummax()) / ew_ser.cummax()).min() * 100

    print("\n" + "=" * 84)
    print(f"  【按扫描器★★★买入】 {dates[0].date()}~{dates[-1].date()} ({yrs:.1f}年)")
    print("=" * 84)
    print(f"  笔数 {len(trades)} | 累计 {total:+.2f}% | 年化 {ann:+.2f}% | "
          f"回撤 {mdd:.2f}% | 胜率 {wr:.1f}% | 空仓 {idle} 天")
    print(f"  对账断言 ✅  (eq ≡ 逐笔复利)")
    print(f"  对照: 等权持有同池 {ew_t:+.2f}% / 回撤 {ew_mdd:.2f}%  "
          f"→ 轮动 {'跑赢' if total > ew_t else '跑输'} {abs(total - ew_t):.1f}pp")
    print(f"\n  分年:")
    y = ser.groupby(ser.index.year).apply(lambda s: (s.iloc[-1] / s.iloc[0] - 1) * 100)
    ye = ew_ser.groupby(ew_ser.index.year).apply(lambda s: (s.iloc[-1] / s.iloc[0] - 1) * 100)
    print(f"  {'年份':<8}{'扫描★★★':>12}{'等权持有':>12}")
    for k in sorted(set(y.index) | set(ye.index)):
        print(f"  {k:<8}{y.get(k, float('nan')):>+11.2f}%{ye.get(k, float('nan')):>+11.2f}%")
    print(f"\n  ⚠ 幸存者偏差: 池=当前活跃{len(data)}只, 未含已消亡/已萎缩品种; "
          f"结论作方向参考。")


if __name__ == "__main__":
    main()

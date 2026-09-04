#!/usr/bin/env python3
"""全市场 ETF 趋势扫描器 — 看市场里哪些标的处于趋势上涨中。

扫描范围: 全市场 ETF 中【日均成交额≥5000万】的活跃品种(流动性门槛, 与 auto 池一致)
数据:    东财前复权日K(etf_qfq_data, 消除份额折算假跳空)

每个标的三类信息:
  ① N12 簇看多占比(0~1) — 与科创50 现役策略同款信号, 跨策略可对话
  ② 均线结构: close > MA20 > MA60(多头排列)
  ③ 动量: 20日/60日涨幅 + 距 250 日高点

分级:
  ★★★ 强趋势  N12≥2/3 且 多头排列
  ★★  趋势    N12≥1/2 且 close>MA20
  ★   观察    N12≥1/2 但均线未排列
  ·   其余

⚠️ 这是【观察工具】不是【买入信号】——横截面轮动已被证伪(2026-09-04),
   本工具回答"市场状态", 不构成"按榜买入"的依据。
   另: 「活跃ETF中强趋势占比」本身就是市场宽度指标。

用法:
    python3 scripts/trend_scanner.py                 # 扫描+终端榜+落盘
    python3 scripts/trend_scanner.py --min-amount 1e8  # 提高流动性门槛
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from backtest_588000_n12 import COMB_N12  # noqa: E402
from etf_qfq_data import fetch_qfq_close  # noqa: E402

OUT = Path.home() / ".tradingagents/rotation/trend_scan.json"


def n12_frac(c: pd.Series) -> pd.Series:
    pos = pd.concat([trix_cross(c, n, m) for n, m in COMB_N12], axis=1)
    return pos.mean(axis=1)


def trix_cross(c: pd.Series, n: int, m: int) -> pd.Series:
    e1 = c.ewm(span=n, adjust=False).mean()
    e2 = e1.ewm(span=n, adjust=False).mean()
    e3 = e2.ewm(span=n, adjust=False).mean()
    tr = e3.pct_change() * 100
    sig = tr.rolling(m).mean()
    return tr > sig


def classify(n12: float, above_ma20: bool, aligned: bool) -> str:
    if n12 >= 2 / 3 and aligned:
        return "★★★"
    if n12 >= 0.5 and above_ma20:
        return "★★"
    if n12 >= 0.5:
        return "★"
    return "·"


def batch_pytdx(codes: list[str], start="2023-01-01") -> dict[str, pd.Series]:
    """pytdx 批量日K(单连接复用, 秒级/只) + 折算断点自动接续修复。

    pytdx 为不复权价: ETF 份额折算日价格腰斩(>25% 假跳空), 用 fix_splits 接续。
    分红的小跳空(1~3%)不影响趋势形态判定。
    """
    from pytdx.hq import TdxHq_API
    from pytdx.params import TDXParams
    from backtest_8wide_ma20_rotation import fix_splits

    api = TdxHq_API()
    if not api.connect("180.153.18.170", 7709, time_out=5):
        raise RuntimeError("pytdx 连接失败")
    out: dict[str, pd.Series] = {}
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
            f = (f[f["date"] >= pd.Timestamp(start)].sort_values("date")
                 .drop_duplicates("date"))
            if len(f) < 70:
                continue
            rows = [[str(d.date()), float(v)] for d, v in
                    zip(f["date"], f["close"].astype(float))]
            rows, _ = fix_splits(rows)          # 折算断点接续复权
            out[code] = pd.Series({pd.Timestamp(d): v for d, v in rows}).sort_index()
            if (k + 1) % 100 == 0:
                print(f"  ...pytdx 已拉 {k+1}/{len(codes)}", flush=True)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="全市场 ETF 趋势扫描")
    ap.add_argument("--min-amount", type=float, default=5e7, help="成交额门槛(元)")
    ap.add_argument("--top", type=int, default=25, help="终端显示 Top N")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--source", choices=["pytdx", "qfq"], default="pytdx",
                    help="pytdx=批量快(折算修复) | qfq=东财前复权(慢, 最准)")
    args = ap.parse_args()

    import akshare as ak
    print("拉取全市场 ETF 快照 ...", flush=True)
    spot = None
    for k in range(3):
        try:
            spot = ak.fund_etf_spot_em()
            if spot is not None and len(spot) > 0:
                break
        except Exception as e:
            print(f"  重试{k+1}: {e}")
            time.sleep(2)
    if spot is None:
        print("快照拉取失败")
        return
    amt_col = next(c for c in spot.columns if "成交额" in c)
    liq = spot[spot[amt_col] >= args.min_amount].copy()
    print(f"  全市场 {len(spot)} 只 | 成交额≥{args.min_amount/1e4:.0f}万: {len(liq)} 只\n")

    codes = [str(c) for c in liq["代码"].tolist()]
    name_of = {str(r["代码"]): str(r.get("名称", "")) for _, r in liq.iterrows()}
    n_fail = 0
    if args.source == "pytdx":
        print(f"pytdx 批量拉 {len(codes)} 只 ...", flush=True)
        series = batch_pytdx(codes, start="2023-01-01")
        n_fail = len(codes) - len(series)
    else:
        series = {}
        for j, code in enumerate(codes):
            try:
                s = fetch_qfq_close(code, start="2023-01-01")
            except Exception:
                s = None
            if s is not None and len(s) >= 70:
                series[code] = s
            if (j + 1) % 50 == 0:
                print(f"  ...qfq 已拉 {j+1}/{len(codes)}", flush=True)
        n_fail = len(codes) - len(series)

    rows = []
    for code in codes:
        s = series.get(code)
        if s is None or len(s.dropna()) < 70:
            continue
        c = s.dropna()
        name = name_of.get(code, code)
        try:
            f = n12_frac(c)
            n12 = float(f.iloc[-1])
            ma20 = float(c.rolling(20).mean().iloc[-1])
            ma60 = float(c.rolling(60).mean().iloc[-1])
            px = float(c.iloc[-1])
            mom20 = px / float(c.iloc[-21]) - 1
            mom60 = px / float(c.iloc[-61]) - 1
            hi = float(c.tail(250).max())
            rows.append({
                "code": code, "name": name, "px": px,
                "n12": n12, "above20": px > ma20, "aligned": px > ma20 > ma60,
                "mom20": mom20 * 100, "mom60": mom60 * 100,
                "dist_hi": (px / hi - 1) * 100,
                "grade": classify(n12, px > ma20, px > ma20 > ma60),
            })
        except Exception:
            n_fail += 1
    d = pd.DataFrame(rows)
    d = d.sort_values(["n12", "mom20"], ascending=False).reset_index(drop=True)
    d = pd.DataFrame(rows)
    d = d.sort_values(["n12", "mom20"], ascending=False).reset_index(drop=True)

    # 市场宽度
    n_star3 = (d["grade"] == "★★★").sum()
    n_star2 = (d["grade"] == "★★").sum()
    n_star1 = (d["grade"] == "★").sum()
    print("\n" + "=" * 88)
    print(f"  市场宽度(活跃 {len(d)} 只): ★★★强趋势 {n_star3} | ★★趋势 {n_star2} | "
          f"★观察 {n_star1} | 其余 {len(d)-n_star3-n_star2-n_star1}")
    print(f"  强趋势占比 {n_star3/len(d)*100:.1f}% — "
          f"{'进攻环境' if n_star3/len(d) > 0.15 else '中性环境' if n_star3/len(d) > 0.05 else '无趋势环境(科创50大概率空仓期)'}")
    print("=" * 88)

    strong = d[d["grade"].isin(["★★★", "★★"])]
    print(f"\n  【趋势上涨标的 Top {min(args.top, len(strong))}】(★★★/★★ 按N12+动量排序)")
    print(f"  {'级别':<5}{'代码':<8}{'名称':<12}{'N12':>6}{'20日%':>8}{'60日%':>8}{'距250日高':>10}")
    print("  " + "-" * 62)
    for _, r in strong.head(args.top).iterrows():
        print(f"  {r['grade']:<5}{r['code']:<8}{str(r['name'])[:10]:<12}{r['n12']:>6.2f}"
              f"{r['mom20']:>+8.2f}{r['mom60']:>+8.2f}{r['dist_hi']:>+9.1f}%")

    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "min_amount": args.min_amount,
        "scanned": int(len(d)),
        "breadth": {"star3": int(n_star3), "star2": int(n_star2),
                    "star1": int(n_star1),
                    "star3_pct": round(n_star3 / len(d) * 100, 2)},
        "list": d.to_dict(orient="records"),
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"\n落盘: {args.out}  (数据失败 {n_fail} 只)")


if __name__ == "__main__":
    main()

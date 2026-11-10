#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N12 六组 TRIX 簇值选股 — 全市场扫描。

逻辑(2026-11 与用户共同确认):
  1. 簇值 frac = 6 组 TRIX 中"多头"占比:
     (10,9)(10,12)(12,9)(12,12)(14,9)(14,12), TRIX>signal 记为多
  2. 买入: frac > 0.5 (至少 4/6 看多)
  3. 卖出: frac <= 0.5
  4. 选股偏好: 高波动(>60%) + 过去1年强势(>30%) + 回撤可控 + 趋势确认(波长>=5日)
  5. 剔除 ST/退市风险

用法:
  # 默认: 高波动+强势, 不限波长
  python3 scripts/select_n12_cluster.py

  # 趋势确认型(用户偏好): 波长>=5日
  python3 scripts/select_n12_cluster.py --min-wave 5 --top 15

  # 纯历史收盘(不补今日盘中价)
  python3 scripts/select_n12_cluster.py --no-live

输出:
  results/n12_candidates.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_588000_n12 as B  # noqa: E402

MB = Path.home() / ".tradingagents" / "youzi" / "mb_daily.pkl"
DEFAULT_OUT = ROOT / "results" / "n12_candidates.csv"


def load_live_px() -> dict[str, float]:
    """全市场实时快照(盘中补今日价); 失败返回空."""
    try:
        import akshare as ak
        spot = ak.stock_zh_a_spot_em()
        spot["代码"] = spot["代码"].astype(str).str.zfill(6)
        out = {}
        for _, r in spot.iterrows():
            try:
                out[str(r["代码"])] = float(r["最新价"])
            except (TypeError, ValueError):
                pass
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] 实时快照失败, 仅用历史收盘: {exc}")
        return {}


def load_names() -> dict[str, str]:
    try:
        import akshare as ak
        info = ak.stock_info_a_code_name()
        return dict(zip(info["code"].astype(str).str.zfill(6), info["name"]))
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="N12 簇值全市场选股")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-vol", type=float, default=60.0, help="年化波动率下限(%%), 默认60")
    ap.add_argument("--min-ret", type=float, default=30.0, help="近1年涨幅下限(%%), 默认30")
    ap.add_argument("--min-wave", type=int, default=2,
                    help="当前持仓段已持续天数下限(1=触发当天), 默认2=x>=2 (数据: x=1平均最高, x=2中位略好, 5+明显损失收益)")
    ap.add_argument("--max-dd", type=float, default=-40.0,
                    help="策略回撤下限(%%)默认-40 (策略净值回撤, 非股价回撤)")
    ap.add_argument("--no-live", action="store_true", help="不补今日盘中价")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    if not MB.exists():
        print(f"! 全市场日K不存在: {MB}")
        return 1

    d = pd.read_pickle(MB)
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values(["code", "date"]).reset_index(drop=True)
    live = {} if args.no_live else load_live_px()
    if live:
        print(f"实时快照: {len(live)} 只")

    rows = []
    for code, g in d.groupby("code", sort=False):
        g = g[g["date"] >= "2025-06-01"]
        if len(g) < 180:
            continue
        close = g["close"].values.astype(float)
        dates = g["date"].values
        px = live.get(code)
        if px and px > 0:
            close = np.append(close, px)
            dates = np.append(dates, np.datetime64("today"))
        pos, frac = B.vote_from(close, B.COMB_N12, 0.5)
        m = dates >= np.datetime64("2025-11-09")
        if m.sum() < 150:
            continue
        c1 = close[m]
        _eq, total, mdd, _sw = B.sim(pos[m], c1)
        r = np.diff(c1) / c1[:-1]
        # 当前持仓段波长
        entry = 0
        for i in range(len(pos) - 1, -1, -1):
            if pos[i] == 0:
                entry = i + 1
                break
        # 波长=当前持仓段已持续天数(1-based, 触发当天=1, 与回测 x 对齐)
        wave = len(pos) - entry if pos[-1] == 1 else 0
        rows.append((
            code, round((c1[-1] / c1[0] - 1) * 100, 1), round(total * 100, 1),
            round(mdd * 100, 1), round(float(np.std(r) * np.sqrt(252) * 100), 1),
            round(float(frac[-1]), 2), wave,
        ))

    df = pd.DataFrame(rows, columns=["code", "ret1y", "strat", "mdd", "vol", "frac", "wave"])
    names = load_names()
    df["name"] = df["code"].map(names).fillna("")

    cand = df[
        (df["frac"] > 0.5)
        & (df["vol"] >= args.min_vol)
        & (df["ret1y"] >= args.min_ret)
        & (df["mdd"] >= args.max_dd)
        & (df["wave"] >= args.min_wave)
        & (~df["name"].str.contains("ST|退", na=False))
    ].sort_values("strat", ascending=False).head(args.top)

    print(f"\n候选 {len(cand)} 只 | 条件: frac>0.5, 波动>={args.min_vol}%, "
          f"1年涨幅>={args.min_ret}%, 波长>={args.min_wave}日, 回撤>={args.max_dd}%")
    print(f"{'代码':<8}{'名称':<10}{'簇值':>6}{'波长':>5}{'1年涨幅':>9}{'策略收益':>9}{'回撤':>8}{'波动':>7}")
    for _, r in cand.iterrows():
        print(f"{r['code']:<8}{r['name']:<10}{r['frac']:>6.2f}{r['wave']:>5}"
              f"{r['ret1y']:>+8.1f}%{r['strat']:>+8.1f}%{r['mdd']:>+7.1f}%{r['vol']:>6.1f}%")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    cand.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"\n已写入: {args.out}")
    print("⚠ 盘中值为动态; 正式信号以 14:55 为准; mb_daily 未复权, 个股除权需复核")
    return 0


if __name__ == "__main__":
    sys.exit(main())

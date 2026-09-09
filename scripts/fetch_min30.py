#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取活跃池【30分钟K】→ 验证「盘中实时量比」能否替代前视量比。

背景: 严格版回测显示, 半路板的全部利润来自"量比=当日全天成交额/昨日额"这个
      **盘中不可知**的条件(泄漏)。唯一可能的救法: 用"截至当前时刻的成交额"
      外推全天量能 —— 例如 10:30 时成交额已达昨日全天的 50% → 判定放量。
      这个量比在 10:30 是**真实可知**的, 不含未来信息。

30分钟K时点: 10:00 / 10:30 / 11:00 / 11:30 / 13:30 / 14:00 / 14:30 / 15:00
  → 可在 10:30(第2根收盘) 计算截至当时的成交额, 无前视。

用法: python3 scripts/fetch_min30.py   (约 6~12 分钟)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from pytdx.hq import TdxHq_API          # noqa: E402
from pytdx.params import TDXParams      # noqa: E402

TDX_HOST, TDX_PORT = "180.153.18.170", 7709
OUT_DIR = Path.home() / ".tradingagents" / "youzi"
POOL_JSON = OUT_DIR / "pool.json"
OUT_PKL = OUT_DIR / "min30.pkl"
KT = 2                                   # KLINE_TYPE_30MIN


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="全市场主板(读 mb_daily.pkl 的代码表), 默认读活跃池")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.all:
        codes = sorted(pd.read_pickle(OUT_DIR / "mb_daily.pkl")["code"].unique())
        pool = [{"code": c} for c in codes]
    else:
        with open(POOL_JSON) as f:
            pool = json.load(f)["pool"]
    print(f"[池] {len(pool)} 只 → 拉 30 分钟K ...", flush=True)

    api = TdxHq_API()
    if not api.connect(TDX_HOST, TDX_PORT, time_out=5):
        print("! pytdx 连接失败")
        return 1

    frames, t0 = [], time.time()
    try:
        for i, p in enumerate(pool):
            code = p["code"]
            m = (TDXParams.MARKET_SH if code[0] in "56"
                 else TDXParams.MARKET_SZ)
            got = []
            for st in range(0, 6400, 800):
                try:
                    bars = api.get_security_bars(
                        KT, m, code.encode(), st, 800)
                except Exception:
                    bars = None
                if not bars:
                    break
                got.append(api.to_df(bars))
                if len(bars) < 800:
                    break
            if got:
                d = pd.concat(got, ignore_index=True)
                d["datetime"] = pd.to_datetime(d["datetime"])
                d = (d.drop_duplicates("datetime").sort_values("datetime")
                     .assign(code=code))
                frames.append(d[["code", "datetime", "open", "high",
                                 "low", "close", "amount"]])
            if (i + 1) % 100 == 0:
                el = time.time() - t0
                eta = el / (i + 1) * (len(pool) - i - 1) / 60
                print(f"  ...{i+1}/{len(pool)} 成功 {len(frames)} | "
                      f"已用 {el/60:.1f}min 剩 {eta:.1f}min", flush=True)
            if (i + 1) % 200 == 0 and frames:
                pd.concat(frames, ignore_index=True).to_pickle(
                    OUT_PKL.with_suffix(".part.pkl"))
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    df = pd.concat(frames, ignore_index=True)
    df.to_pickle(OUT_PKL)
    print(f"\n[完成] {len(frames)} 只 | {len(df):,} 根 | "
          f"{df['datetime'].min()} ~ {df['datetime'].max()}")
    print(f"  → {OUT_PKL}")
    part = OUT_PKL.with_suffix(".part.pkl")
    if part.exists():
        part.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

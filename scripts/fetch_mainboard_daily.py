#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取【全市场沪深主板(60/00)】日K + 流通股本 → 本地缓存。

用途(严格版回测的数据底座):
  1. 严格版半路板回测: 逐日用"截至 T-1 的成交额排名"动态选池(无前视)
  2. 实盘选股指标: 连板数 / 流通市值 / 换手率 / 距 60 日高点

输出:
  ~/.tradingagents/youzi/mb_daily.pkl   长表 DataFrame[code,date,open,high,low,close,amount]
  ~/.tradingagents/youzi/mb_float.json  {code: 流通股本(股)}

用法: python3 scripts/fetch_mainboard_daily.py [--start 2022-01-01]
      (后台跑约 60~70 分钟: 3052 只 × 日K + 流通股本)
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

from pytdx.hq import TdxHq_API          # noqa: E402
from pytdx.params import TDXParams      # noqa: E402
from backtest_8wide_ma20_rotation import fix_splits  # noqa: E402

TDX_HOST, TDX_PORT = "180.153.18.170", 7709
OUT_DIR = Path.home() / ".tradingagents" / "youzi"
DAILY_PKL = OUT_DIR / "mb_daily.pkl"
FLOAT_JSON = OUT_DIR / "mb_float.json"


def connect() -> TdxHq_API:
    api = TdxHq_API()
    for _ in range(5):
        try:
            if api.connect(TDX_HOST, TDX_PORT, time_out=5):
                return api
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("pytdx 连接失败")


def list_mainboard(api) -> list[tuple[int, str, str]]:
    """全市场 → 沪深主板(60/00), 排除 ST/退市/次新。"""
    out = []
    for market in (TDXParams.MARKET_SH, TDXParams.MARKET_SZ):
        cnt = api.get_security_count(market)
        for st in range(0, cnt, 1000):
            try:
                lst = api.get_security_list(market, st)
            except Exception:
                lst = None
            if not lst:
                continue          # 部分服务器 start=0 返回空, 不能 break
            for it in lst:
                code = str(it.get("code", ""))
                name = str(it.get("name", "")).strip()
                if not code.startswith(("60", "00")):
                    continue
                if "ST" in name or "退" in name or name.startswith("N"):
                    continue
                out.append((market, code, name))
    return out


def fetch_one(api, market: int, code: str, start: str):
    """单只日K(不复权 + 送转修复) → DataFrame 或 None。"""
    frames = []
    for pg in range(2):                       # start=0 是最新, 2 页覆盖 ~1600 根
        try:
            bars = api.get_security_bars(
                TDXParams.KLINE_TYPE_DAILY, market, code.encode(), pg * 800, 800)
        except Exception:
            bars = None
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
    f = (f[f["date"] >= pd.Timestamp(start)]
         .sort_values("date").drop_duplicates("date"))
    if len(f) < 60:                            # 上市不足 60 日: 指标/连板数不可靠
        return None

    rows = [[str(d.date()), float(v)] for d, v in
            zip(f["date"], f["close"].astype(float))]
    cl, _ = fix_splits(rows)                   # 送转修复(仅 close)
    idx = [pd.Timestamp(d) for d, _ in cl]
    base = pd.Series([v for _, v in rows], index=idx)
    fixed = pd.Series([v for _, v in cl], index=idx)
    ratio = (fixed / base).replace([float("inf"), -float("inf")], 1.0).fillna(1.0)

    g = f.set_index("date")
    out = pd.DataFrame(index=idx)
    out["close"] = fixed
    for col in ("open", "high", "low"):
        out[col] = g[col].astype(float).reindex(idx) * ratio
    out["amount"] = g["amount"].astype(float).reindex(idx)   # 成交额不随送转缩放
    out = out.dropna(subset=["close", "high", "low", "open"])
    if len(out) < 60:
        return None
    out.insert(0, "code", code)
    return out.reset_index().rename(columns={"index": "date"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--limit", type=int, default=0, help="只拉前 N 只(测试)")
    ap.add_argument("--offset", type=int, default=0, help="跳过前 N 只(续传)")
    ap.add_argument("--out", default="", help="输出 pkl 路径(默认 mb_daily.pkl)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_pkl = Path(args.out) if args.out else DAILY_PKL
    api = connect()
    try:
        uni = list_mainboard(api)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    if args.offset:
        uni = uni[args.offset:]
    if args.limit:
        uni = uni[:args.limit]
    print(f"[1/2] 主板标的 {len(uni)} 只 → 拉日K(约 40 分钟) ...", flush=True)

    api = connect()
    frames, floats, t0 = [], {}, time.time()
    try:
        for i, (m, code, name) in enumerate(uni):
            try:
                d = fetch_one(api, m, code, args.start)
                if d is not None:
                    frames.append(d)
                try:
                    fi = api.get_finance_info(m, code)
                    lb = float((fi or {}).get("liutongguben") or 0)
                    if lb > 0:
                        floats[code] = lb
                except Exception:
                    pass
            except Exception as exc:
                print(f"  [warn] {code}: {exc}")
            if (i + 1) % 200 == 0:
                el = time.time() - t0
                eta = el / (i + 1) * (len(uni) - i - 1) / 60
                print(f"  ...{i+1}/{len(uni)} 成功 {len(frames)} | "
                      f"已用 {el/60:.1f}min 预计还剩 {eta:.1f}min", flush=True)
            if (i + 1) % 300 == 0:            # 定期落盘, 中断可续
                if frames:
                    pd.concat(frames, ignore_index=True).to_pickle(
                        out_pkl.with_suffix(".part.pkl"))
                FLOAT_JSON.with_suffix(f".{args.offset}.json").write_text(
                    json.dumps(floats), encoding="utf-8")
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    df = pd.concat(frames, ignore_index=True)
    df.to_pickle(out_pkl)
    fj = FLOAT_JSON if not args.offset else FLOAT_JSON.with_suffix(
        f".{args.offset}.json")
    fj.write_text(json.dumps(floats), encoding="utf-8")
    print(f"\n[完成] {len(frames)} 只 | {len(df):,} 行 | "
          f"{df['date'].min().date()}~{df['date'].max().date()}")
    print(f"  日K  → {out_pkl}")
    print(f"  流通股本 {len(floats)} 只 → {fj}")
    part = out_pkl.with_suffix(".part.pkl")
    if part.exists():
        part.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

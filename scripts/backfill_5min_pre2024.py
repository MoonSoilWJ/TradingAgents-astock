#!/usr/bin/env python3
"""回填 2024-07 之前的【无偏】5分钟数据（全池 × 全交易日，不按表现挑）。

背景:
  - tdx_5min_2y.json 用 get_security_bars 连续拉，只能回溯 ~2 年(2024-07-03 起)。
  - aligned_live_4y.json 的 etf_5min 是 planC 系列「先按当日收盘涨幅排 TopK 再抓」的
    稀疏数据 → 存在数据可得性前视偏差(有5min的样本当日均涨 +1.58%, 无的 -0.69%)，
    任何按 5min 排序选股的回测都系统性虚高。
  - get_history_minute_time_data(mkt, code, date) 按交易日取 1 分钟(240根)，可回溯到
    2015 年。本脚本对【全部 103 只 × 全部交易日】无条件拉取 → 无偏。

输出: ~/.tradingagents/cache/t0_5min/tdx_5min_pre2024.json
      结构与 tdx_5min_2y.json 一致: {"etf_5min": {code: {date: [bars]}}}
      量纲用当日日K收盘对齐(scale = dclose / 末根1min价)，缺日K时回退 code 级系数。

支持断点续跑(已抓过的 code/date 自动跳过)。
用法:
    python scripts/backfill_5min_pre2024.py                    # 2022-06-15 ~ 2024-07-02
    python scripts/backfill_5min_pre2024.py --start 2023-01-01
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_planC_1min_4y import (  # noqa: E402
    connect, daily_close_of, recon_5min, safe_get_minute,
)

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
ALIGNED = CACHE / "aligned_live_4y.json"
TDX2Y = CACHE / "tdx_5min_2y.json"
OUT = CACHE / "tdx_5min_pre2024.json"

DEFAULT_START = "2022-06-15"
DEFAULT_END = "2024-07-02"      # tdx_5min_2y 起点前一天
SAVE_EVERY = 5                  # 每 N 只标的落盘一次


def normalize(bars: list[dict]) -> list[dict]:
    """time 统一成 'HH:MM:00'，与 tdx_5min_2y 口径一致。"""
    for b in bars:
        t = b.get("time", "")
        if len(t) == 5:
            b["time"] = t + ":00"
            b["datetime"] = f"{b['day']} {b['time']}"
    return bars


def main() -> None:
    ap = argparse.ArgumentParser(description="回填 pre-2024 无偏 5min")
    ap.add_argument("--start", type=str, default=DEFAULT_START)
    ap.add_argument("--end", type=str, default=DEFAULT_END)
    ap.add_argument("--limit-codes", type=int, default=0, help="只跑前N只(调试)")
    args = ap.parse_args()

    a = json.loads(ALIGNED.read_text(encoding="utf-8"))
    etf_daily = a["etf_daily"]
    all_dates = [d for d in a["all_dates"] if args.start <= d <= args.end]
    codes = sorted(json.loads(TDX2Y.read_text(encoding="utf-8"))["etf_5min"].keys())
    if args.limit_codes:
        codes = codes[:args.limit_codes]

    out = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"etf_5min": {}}
    five = out["etf_5min"]
    done0 = sum(len(v) for v in five.values())
    print(f">>> 目标 {len(codes)} 只 × {len(all_dates)} 天 "
          f"({all_dates[0]} ~ {all_dates[-1]}) = {len(codes)*len(all_dates)} 个请求")
    print(f">>> 已有 {done0} 个 (code,day)，断点续跑\n", flush=True)

    api = connect()
    t_start = time.time()
    n_ok = n_fail = 0
    try:
        for i, code in enumerate(codes, 1):
            cmap = five.setdefault(code, {})
            code_scale = 1.0
            todo = [d for d in all_dates if d not in cmap]
            if not todo:
                print(f"    [{i}/{len(codes)}] {code}: 已完整，跳过", flush=True)
                continue
            ok = fail = 0
            for day in todo:
                raw = safe_get_minute(api, code, day)
                if not raw:
                    fail += 1
                    continue
                dclose = daily_close_of(etf_daily, code, day)
                raw_last = float(raw[-1]["price"]) if raw else None
                if dclose and dclose > 0 and raw_last and raw_last > 0:
                    scale = dclose / raw_last
                    code_scale = scale
                else:
                    scale = code_scale
                bars = recon_5min(raw, day, scale=scale)
                if not bars:
                    fail += 1
                    continue
                cmap[day] = normalize(bars)
                ok += 1
            n_ok += ok
            n_fail += fail
            el = time.time() - t_start
            print(f"    [{i}/{len(codes)}] {code}: +{ok}天 (失败{fail}) | "
                  f"累计 {n_ok} 天, 用时 {el/60:.1f}min", flush=True)
            if i % SAVE_EVERY == 0:
                OUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
                print(f"        [落盘] {OUT.stat().st_size/1e6:.0f}MB", flush=True)
    finally:
        try:
            api.disconnect()
        except Exception:  # noqa: BLE001
            pass
        OUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    print(f"\n>>> 完成: 新增 {n_ok} 个(code,day), 失败 {n_fail}, "
          f"总计 {sum(len(v) for v in five.values())}")
    print(f">>> 落盘 {OUT} ({OUT.stat().st_size/1e6:.0f}MB)")

    # 校验: 末根 close / 日K close
    import statistics
    ratios, bad = [], 0
    for c in list(five)[:40]:
        for d in list(five[c])[-8:]:
            dc = daily_close_of(etf_daily, c, d)
            if not dc or not five[c][d]:
                continue
            r = five[c][d][-1]["close"] / dc
            ratios.append(r)
            if abs(r - 1) > 0.005:
                bad += 1
    if ratios:
        print(f">>> 对齐校验 n={len(ratios)} bad={bad} 中位比值={statistics.median(ratios):.5f}")


if __name__ == "__main__":
    main()

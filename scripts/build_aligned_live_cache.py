#!/usr/bin/env python3
"""把方案B的权威5分钟缓存 + 千日日K 拼装成 backtest_parallel_pool.py 要求的对齐格式。

对齐目标 = 实盘 t0_monitor.py（hybrid-A: regime→优质/原池, 14:45信号, 14:50买,
次日09:40~11:05 TRIX(5,3)死叉卖）。本脚本只做数据拼装，选股/卖点逻辑全部
由 backtest_parallel_pool.py 复用实盘代码，零近似。

数据窗口: 2022-06-15 ~ 2026-07-28 (全历史5分钟真实覆盖区间; 2015-2021无5分钟K,
无法做与实盘等价的日内回测)。
"""
from __future__ import annotations

import json
from pathlib import Path

CACHE = Path.home() / ".tradingagents/cache/t0_5min"
SRC_5MIN = CACHE / "planC_1min_4y.json"          # 扁平 {CODE_DATE: [bars]}
SRC_DAILY_2015 = CACHE / "backfill_daily_2015.json"  # 10只老牌 2015-2019 日K
SRC_DAILY_1000 = CACHE / "backfill_daily_1000.json"  # 106只 2022-2026 日K
OUT = CACHE / "aligned_live_4y.json"


def _load_daily(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    return d.get("etf_daily", d)


def merge_daily() -> dict:
    """合并两个日K源: 2015源(老牌2015-2019) + 1000源(106只2022-2026)。
    主数据(日K)必须先齐, 5分钟才有意义 —— 对齐回测的前提。
    """
    d15 = _load_daily(SRC_DAILY_2015)
    d10 = _load_daily(SRC_DAILY_1000)
    merged: dict[str, dict] = {}
    for src in (d15, d10):
        for code, val in src.items():
            ref = merged.setdefault(code, {"returns": []})
            seen = {r["date"] for r in ref["returns"]}
            for r in val.get("returns", []):
                if r["date"] not in seen:
                    ref["returns"].append(r)
                    seen.add(r["date"])
            ref["returns"].sort(key=lambda x: x["date"])
    return merged


def main() -> None:
    five = json.loads(SRC_5MIN.read_text(encoding="utf-8"))
    etf_daily_src = merge_daily()

    # 1) 5min 扁平 -> 嵌套 {code: {date: bars}}
    etf_5min: dict[str, dict] = {}
    all_dates: set[str] = set()
    n_bars = 0
    for key, bars in five.items():
        if not bars:
            continue
        code, datestr = key.split("_", 1)
        date = f"{datestr[:4]}-{datestr[4:6]}-{datestr[6:]}"
        etf_5min.setdefault(code, {})[date] = bars
        all_dates.add(date)
        n_bars += len(bars)

    all_dates = sorted(all_dates)

    # 2) proxy_klines = 501018 日K, 供 detect_regime (key 必须是 'day')
    proxy_raw = etf_daily_src.get("501018", {}).get("returns", [])
    proxy_klines = [
        {
            "day": r["date"],
            "open": float(r.get("open", r.get("close", 0))),
            "high": float(r.get("high", r.get("close", 0))),
            "low": float(r.get("low", r.get("close", 0))),
            "close": float(r.get("close", 0)),
            "volume": float(r.get("volume", 0)),
        }
        for r in proxy_raw
    ]

    # 3) etf_daily 直接复用 (已是 {code:{returns:[{date,open,high,low,close,...}]}})
    #    但确保 501018 也在内 (regime 用 proxy_klines, 这里保留全量无妨)
    etf_daily = etf_daily_src

    out = {
        "etf_daily": etf_daily,
        "etf_5min": etf_5min,
        "all_dates": all_dates,
        "proxy_klines": proxy_klines,
        "data_source": "planC_1min_4y(5min)+backfill_daily_1000(daily+501018)",
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    print(f"已生成对齐cache: {OUT}")
    print(f"  etf_5min: {len(etf_5min)} 只 | 5min bar总数: {n_bars}")
    print(f"  etf_daily: {len(etf_daily)} 只 | proxy(501018): {len(proxy_klines)} 条")
    print(f"  all_dates: {len(all_dates)} 天, {all_dates[0]} ~ {all_dates[-1]}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Plan C 从 2015 起全历史精确 5min 回测（106 只 T0 全池）。

设计要点（对齐用户诉求“从2015起 + 保持106只池 + 对齐+951%”）：
  1. 日K(etf_daily)：
       - post 段(2022-06-15 ~ 2026-07-28)：复用权威 backfill_daily_1000.json（原 Plan C 口径）。
       - pre 段(<2022-06-15)：
           * 10 只老牌 T0 ETF：复用 run_2015_full 已聚合的 backfill_daily_2015.json
             （pytdx 聚合、不复权，口径与 run_2015 一致）。
           * 其余 60 只（2022-06-15 前上市但非老牌）：用新浪不复权日K(基金全量)截 pre 段。
     这样 2022-06-15 当天的衔接为 post(backfill) 接管，跨日涨幅口径一致。
  2. 5min 缓存(planC_1min_4y.json，与原 Plan C / run_2015 共用同一文件)：
       - 2022-06-15 起全 106 只：原 Plan C 已写入 → 命中复用。
       - 10 只老牌 2015-2022：run_2015_full 已预填 → 命中复用。
       - 60 只新 ETF 的 pre 段：本脚本批量预填（断点续跑）。
     → 2022+ 段候选池/数据/缓存与原 Plan C 完全一致，精确复现 +951%。
  3. eval_dates 阈值：覆盖 >= 2（动态小阈值，使 pre 段也能交易；
     对齐 run_2015 口径；2022+ 段 106 只全覆盖自动满足 >=2）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(k, None)

import akshare as ak  # noqa: E402
from pytdx.hq import TdxHq_API  # noqa: E402
from backtest_planC_1min_4y import (  # noqa: E402
    run, market_of, date_int, load_cache, save_cache, report, recon_5min,
    daily_close_of, SERVERS,
)
from run_2015_full import fresh_connect, safe_fetch  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
DAILY1000 = CACHE / "backfill_daily_1000.json"
DAILY2015 = CACHE / "backfill_daily_2015.json"
OUT = CACHE / "planC_1min_4y.json"          # 共用 5min 缓存
RESULT = CACHE / "planC_result_2015_full.json"

CUT = "2022-06-15"   # post 段起点（backfill 权威口径）
# 10 只老牌（已被 run_2015_full 预填 pre 段日K+5min）
OLD10 = {'159920', '510900', '513100', '513500', '513030',
         '159901', '518880', '159934', '518800', '162411'}


def sina_daily(code: str) -> dict:
    """新浪基金全量日K → {date_str: {open,high,low,close,volume}}（不复权，截 pre 段）。"""
    try:
        h = ak.fund_etf_hist_sina(symbol=("sh" if code[0] in "56" else "sz") + code)
    except Exception as e:
        print(f"    [warn] sina {code} 拉取失败: {e}", flush=True)
        return {}
    out: dict = {}
    for _, row in h.iterrows():
        d = str(row["date"])[:10]
        if d >= CUT:
            continue
        try:
            out[d] = {
                "date": d,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume") or 0),
            }
        except Exception:
            continue
    return out


def main() -> None:
    api = fresh_connect()
    etfs = get_all_t0_etfs()
    bf = json.loads(DAILY1000.read_text(encoding="utf-8"))
    bd = bf["etf_daily"]
    d2015 = json.loads(DAILY2015.read_text(encoding="utf-8")) if DAILY2015.exists() else {}

    # ---- 1) 构建 106 只全历史 etf_daily ----
    full: dict = {}
    for e in etfs:
        code = e["code"]
        post = {r["date"]: r for r in bd.get(code, {}).get("returns", [])}
        if code in OLD10 and code in d2015:
            pre = {r["date"]: r for r in d2015[code]["returns"]}
        else:
            pre = sina_daily(code)
        merged = {**pre, **post}   # 同一天以 post(backfill) 为准
        if merged:
            full[code] = {"returns": sorted(merged.values(), key=lambda x: x["date"])}
    print(f">>> full 池 {len(full)} 只", flush=True)

    # ---- 2) eval_dates（覆盖 >= 2，动态；2022+ 段 106 只全覆盖自动满足）----
    codes = list(full.keys())
    all_dates = sorted({r["date"] for info in full.values() for r in info["returns"]})
    cover = defaultdict(int)
    for c in codes:
        for r in full[c]["returns"]:
            cover[r["date"]] += 1
    eval_dates = [d for d in all_dates if cover[d] >= 2]
    print(f">>> eval {eval_dates[0]}~{eval_dates[-1]} ({len(eval_dates)} 交易日)", flush=True)

    # ---- 3) 预填 60 只新 ETF 的 pre 段 5min（断点续跑）----
    cache = load_cache()
    etf_list = [e for e in etfs if e["code"] in full]
    need = [e["code"] for e in etfs if e["code"] in full and e["code"] not in OLD10]
    print(f">>> 需预填 pre 段 5min 的 ETF: {len(need)} 只", flush=True)
    for code in need:
        pre_dates = [r["date"] for r in full[code]["returns"] if r["date"] < CUT]
        if not pre_dates:
            continue
        miss = [d for d in pre_dates if f"{code}_{date_int(d)}" not in cache]
        if not miss:
            continue
        t0 = time.time()
        print(f"    预填 {code}: {len(miss)} 天 pre 段 5min", flush=True)
        done = 0
        for i, d in enumerate(miss, 1):
            raw = safe_fetch(api, code, d)
            if not raw:
                continue
            dc = daily_close_of(full, code, d)
            raw_last = float(raw[-1]["price"])
            scale = dc / raw_last if (dc and raw_last > 0) else 1.0
            five = recon_5min(raw, d, scale=scale)
            if five:
                cache[f"{code}_{date_int(d)}"] = five
                done += 1
            if i % 200 == 0:
                save_cache(cache)
                print(f"      {code} [{i}/{len(miss)}] 已补 {done} "
                      f"{(time.time()-t0):.0f}s", flush=True)
        save_cache(cache)
        print(f"    {code} 完成预填 {done} 天", flush=True)
    print(">>> pre 段 5min 预填完成", flush=True)

    # ---- 4) 跑 Plan C（2022+ 全命中 / 10只pre命中 / 60只pre已预填）----
    res = run(api, cache, etf_list, full, all_dates, eval_dates)
    report("PlanC 从2015起全历史(106只T0全池)", res)
    RESULT.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print(f">>> 结果已落盘 {RESULT}", flush=True)
    try:
        api.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    main()

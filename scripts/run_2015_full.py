#!/usr/bin/env python3
"""2015 起全历史精确 5min 回测（老牌 T0 ETF 子集）。

相对上一版修复：
  1. 每次 get_history_minute_time_data 用【线程+超时】包裹，超时即强制断开并
     轮换服务器重连，杜绝 socket 半开时无限阻塞（上一版卡死根因）。
  2. 只为 2015~2022-06 之前（约 1700 天）拉分钟；2022-06 起的真实日K直接复用
     backfill_daily_1000.json，不再重复拉取。
  3. 聚合时把 5min 结果直接写入 planC_1min_4y.json 缓存，run() 命中复用，
     不再对 2015-2021 二次拉取。
  4. 支持断点续跑（DAILY2015 / 缓存已存在的部分不重做）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from pytdx.hq import TdxHq_API  # noqa: E402
from backtest_planC_1min_4y import (  # noqa: E402
    run, market_of, date_int, load_cache, save_cache, report, recon_5min, OUT,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
DAILY1000 = CACHE / "backfill_daily_1000.json"
DAILY2015 = CACHE / "backfill_daily_2015.json"
RESULT = CACHE / "planC_result_2015.json"

SERVERS = [
    ("115.238.56.198", 7709), ("115.238.90.165", 7709),
    ("180.153.18.170", 7709), ("218.108.98.244", 7709),
    ("123.125.108.14", 7709), ("60.28.23.80", 7709),
]
ANCHOR = "159920"
FETCH_TIMEOUT = 10.0
START = date(2015, 1, 1)
END = date(2026, 7, 28)


def fresh_connect() -> TdxHq_API:
    """轮换尝试所有服务器，返回已连接的 API（带 5s 建连超时）。"""
    api = TdxHq_API()
    for h, p in SERVERS:
        try:
            if api.connect(h, p, time_out=5):
                return api
        except Exception:
            continue
    raise RuntimeError("无可用通达信服务器")


def safe_fetch(api: TdxHq_API, code: str, day_str: str,
               timeout: float = FETCH_TIMEOUT) -> list | None:
    """线程包裹的分钟拉取：超时即判失败并轮换重连，绝不阻塞主线程。"""
    mkt = market_of(code)
    di = date_int(day_str)
    last_err = None
    for _ in range(3):
        box: dict = {}
        def _call() -> None:
            try:
                box["v"] = api.get_history_minute_time_data(mkt, code, di)
            except Exception as e:  # noqa: BLE001
                box["e"] = e
        th = threading.Thread(target=_call, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            # 阻塞超时：强制断连并轮换服务器
            try:
                api.disconnect()
            except Exception:
                pass
            try:
                api = fresh_connect()
            except Exception:
                pass
            continue
        if box.get("v"):
            return box["v"]
        if "e" in box:
            last_err = box["e"]
            try:
                api.disconnect()
            except Exception:
                pass
            try:
                api = fresh_connect()
            except Exception:
                pass
    if last_err:
        pass
    return None


def weekday_days(start: date, end: date) -> list[date]:
    d = start
    out = []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def main() -> None:
    api = fresh_connect()
    etfs = get_all_t0_etfs()

    # 1) 子集：2015-01-05 当天能拉到分钟（即 2015 前已上市的老牌 T0 ETF）
    subset: list[dict] = []
    for e in etfs:
        m = safe_fetch(api, e["code"], "20150105")
        if m:
            subset.append(e)
    print(f">>> 2015前上市子集 {len(subset)} 只: "
          f"{[e['code'] for e in subset]}", flush=True)

    # 2) 用新浪日K(backfill_daily_1000) 给每只子集 ETF 定 N（标的级恒定系数）
    bf = json.loads(DAILY1000.read_text(encoding="utf-8"))
    bd = bf["etf_daily"]
    # 2022-06 起的起始日期（用于切分聚合区间）
    d1000_start = min(r["date"] for info in bd.values() for r in info["returns"])
    code_scale: dict[str, float] = {}
    for e in subset:
        code = e["code"]
        info = bd.get(code)
        if not info:
            continue
        rets = info["returns"]
        # 取末 5 个交易日的 N 取中位数，更稳
        ns = []
        for last in rets[-5:]:
            d = last["date"]
            r0 = safe_fetch(api, code, d)
            if r0:
                ns.append(last["close"] / float(r0[-1]["price"]))
        if ns:
            code_scale[code] = sorted(ns)[len(ns) // 2]
    print(f">>> code_scale { {k: round(v, 4) for k, v in code_scale.items()} }", flush=True)
    print(f">>> 日K1000起始 {d1000_start}（此前区间需聚合分钟）", flush=True)

    # 3) 交易日历（锚 159920 逐工作日探测，休市日自动剔除）—— 仅探测 2015~2022-06 前
    cut = date_int(d1000_start)
    cal = weekday_days(START, END)
    real_cal: list[str] = []
    for d in cal:
        ds = d.strftime("%Y-%m-%d")
        if date_int(ds) >= cut:
            break  # 2022-06 起由 backfill_daily_1000 提供，无需探测
        m = safe_fetch(api, ANCHOR, ds)
        if m:
            real_cal.append(ds)
    print(f">>> 聚合日历 {len(real_cal)} 天 ({real_cal[0]}~{real_cal[-1]})", flush=True)

    # 4) 聚合真实值日K（×N 还原）+ 预填 5min 缓存，带断点续存/续跑
    etf_daily: dict = json.loads(DAILY2015.read_text(encoding="utf-8")) if DAILY2015.exists() else {}
    cache = load_cache()  # 复用 planC_1min_4y.json（2022+ 真实值已在内）
    done = {c: len(v["returns"]) for c, v in etf_daily.items()}
    for e in subset:
        code = e["code"]
        N = code_scale.get(code, 1.0)
        if code in done and done[code] >= len(real_cal) * 0.85:
            print(f"    跳过已完成 {code}: {done[code]} 天", flush=True)
            continue
        rets: list[dict] = []
        t0 = time.time()
        for i, d in enumerate(real_cal, 1):
            raw = safe_fetch(api, code, d)
            if not raw:
                continue
            op = float(raw[0]["price"]) * N
            cl = float(raw[-1]["price"]) * N
            hi = max(float(b["price"]) for b in raw) * N
            lo = min(float(b["price"]) for b in raw) * N
            vol = sum(float(b.get("vol", 0)) for b in raw)
            rets.append({"date": d, "open": round(op, 4), "high": round(hi, 4),
                         "low": round(lo, 4), "close": round(cl, 4), "volume": vol})
            # 预填 5min 缓存（run() 将命中复用）
            key = f"{code}_{date_int(d)}"
            if key not in cache:
                five = recon_5min(raw, d, scale=N)
                if five:
                    cache[key] = five
            if i % 200 == 0:
                save_cache(cache)
                print(f"    {code} [{i}/{len(real_cal)}] 已 {len(rets)} 天 "
                      f"{(time.time()-t0):.0f}s", flush=True)
        etf_daily[code] = {"returns": rets}
        DAILY2015.write_text(json.dumps(etf_daily, ensure_ascii=False), encoding="utf-8")
        save_cache(cache)
        print(f"    聚合 {code}: {len(rets)} 天，缓存已存", flush=True)
    print(f">>> 日K聚合完成: { {c: len(v['returns']) for c, v in etf_daily.items()} }", flush=True)

    # 5) 合并 2022-06 起的真实日K（来自 backfill_daily_1000）
    full: dict = {}
    for c, info in etf_daily.items():
        merged = {r["date"]: r for r in info["returns"]}
        if c in bd:
            for r in bd[c]["returns"]:
                merged.setdefault(r["date"], r)
        full[c] = {"returns": sorted(merged.values(), key=lambda x: x["date"])}
    print(f">>> 合并后覆盖 2015~2026: "
          f"{ {c: len(v['returns']) for c, v in list(full.items())[:3]} } ...", flush=True)

    # 6) 跑 Plan C（run 复用缓存，2015-2021 全命中、2022+ 已在内）
    codes = list(full.keys())
    all_dates = sorted({r["date"] for info in full.values() for r in info["returns"]})
    cover = defaultdict(int)
    for c in codes:
        for r in full[c]["returns"]:
            cover[r["date"]] += 1
    eval_dates = [d for d in all_dates if cover[d] >= 2]
    print(f">>> eval {eval_dates[0]}~{eval_dates[-1]} ({len(eval_dates)} 交易日)", flush=True)
    etf_list = [e for e in subset if e["code"] in full]
    res = run(api, cache, etf_list, full, all_dates, eval_dates)
    report("PlanC 2015起精确5min(老牌T0子集)", res)
    RESULT.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print(f">>> 结果已落盘 {RESULT}", flush=True)
    try:
        api.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""回填自动发现层(新)ETF 的【无偏】5分钟数据(2022-06-15~2026-07-31)。

背景:
  - refresh_t0_pool.py 把候选池从 106 只扩到 417 只, 但现有 5min 缓存
    (tdx_5min_pre2024 + tdx_5min_2y) 只覆盖原 103 只 ⇒ 回测 B 无法吃到新 ETF,
    掩盖了"池子扩大"的真实影响。
  - 本脚本对 auto_t0_etfs.json 里的全部新 ETF(上市日~2026-07-31) 用 pytdx
    get_history_minute_time_data 逐日拉 1 分钟聚合 5 分钟, 无偏(不按表现挑)。
  - 对齐用 full_daily_2015_2026.json(10年日K, 已含417只), 口径与 backfill_5min_pre2024 一致。

输出: ~/.tradingagents/cache/t0_5min/tdx_5min_auto.json
      结构 {etf_5min: {code: {date: [bars]}}}, 与 tdx_5min_2y.json 完全一致。

支持断点续跑(已抓过的 code/date 自动跳过)。
用法:
    python scripts/backfill_5min_auto.py
    python scripts/backfill_5min_auto.py --limit 20     # 先试跑前20只验证
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_planC_1min_4y import (  # noqa: E402
    connect, daily_close_of, recon_5min, safe_get_minute,
)
from t0_etf_list import load_auto_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
FULL_DAILY = CACHE / "full_daily_2015_2026.json"
OUT = CACHE / "tdx_5min_auto.json"
DEFAULT_START = "2022-06-15"
DEFAULT_END = date.today().isoformat()   # 动态截止到今天(回测只读历史, 多补几天无害)
SAVE_EVERY = 5


def normalize(bars: list[dict]) -> list[dict]:
    for b in bars:
        t = b.get("time", "")
        if len(t) == 5:
            b["time"] = t + ":00"
            b["datetime"] = f"{b['day']} {b['time']}"
    return bars


def load_daily_for_codes(codes: list[str]) -> dict:
    """取 auto ETF 的日K(用于5min量纲对齐)。优先 full_daily, 缺失的用 sina 补拉。"""
    etf_daily: dict = {}
    if FULL_DAILY.exists():
        d = json.loads(FULL_DAILY.read_text(encoding="utf-8"))
        for c in codes:
            if c in d:
                etf_daily[c] = d[c]
    missing = [c for c in codes if c not in etf_daily]
    if missing:
        import akshare as ak
        print(f">>> sina 补拉 {len(missing)} 只 auto ETF 日K ...", flush=True)
        for c in missing:
            try:
                h = ak.fund_etf_hist_sina(symbol=("sh" if c[0] in "56" else "sz") + c)
            except Exception:
                continue
            recs = []
            for _, row in h.iterrows():
                dd = str(row["date"])[:10]
                if dd < DEFAULT_START:
                    continue
                try:
                    recs.append({"date": dd, "open": float(row["open"]),
                                 "high": float(row["high"]), "low": float(row["low"]),
                                 "close": float(row["close"])})
                except Exception:
                    continue
            if recs:
                etf_daily[c] = {"returns": recs}
    print(f">>> 日K可用 auto ETF: {len(etf_daily)}/{len(codes)}", flush=True)
    return etf_daily


def _update_full_daily(auto_daily: dict) -> None:
    """把 auto 层日K合并进 full_daily(覆盖更新, 不删旧手工层数据), 供回测用417只日K。"""
    d = json.loads(FULL_DAILY.read_text(encoding="utf-8")) if FULL_DAILY.exists() else {}
    for c, info in auto_daily.items():
        d[c] = info
    FULL_DAILY.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    print(f">>> 已更新 full_daily: {len(d)} 只 (含 auto 层日K)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="回填自动发现层ETF无偏5min")
    ap.add_argument("--start", type=str, default=DEFAULT_START)
    ap.add_argument("--end", type=str, default=DEFAULT_END)
    ap.add_argument("--limit", type=int, default=0, help="只跑前N只(调试)")
    args = ap.parse_args()

    auto = load_auto_t0_etfs()
    if not auto:
        print(">>> auto_t0_etfs.json 为空/缺失, 无新ETF可补。先跑 refresh_t0_pool.py")
        return
    auto_codes = [c for c, _, _ in auto]
    print(f">>> 自动层 ETF: {len(auto_codes)} 只", flush=True)

    etf_daily = load_daily_for_codes(auto_codes)
    if not etf_daily:
        print(">>> [错误] 无法获取 auto ETF 日K", flush=True)
        return
    # 全交易日(取交集: auto ETF 各自的交易日)
    all_dates = sorted({r["date"] for info in etf_daily.values()
                        for r in info["returns"]
                        if args.start <= r["date"] <= args.end})
    print(f">>> 交易日窗口: {len(all_dates)} 天 ({all_dates[0]}~{all_dates[-1]})", flush=True)

    # 只补"窗口内至少有一天上市"的 auto ETF
    codes = []
    for c in auto_codes:
        recs = etf_daily.get(c, {}).get("returns", [])
        if not recs:
            continue
        first = min(r["date"] for r in recs)
        if first <= args.end:
            codes.append(c)
    print(f">>> 窗口内有数据的 auto ETF: {len(codes)} 只", flush=True)
    if args.limit:
        codes = codes[:args.limit]

    out = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"etf_5min": {}}
    five = out["etf_5min"]

    done0 = sum(len(v) for v in five.values())
    total = len(codes) * len(all_dates)
    print(f">>> 目标 {len(codes)} 只 × {len(all_dates)} 天 = {total} 个请求")
    print(f">>> 已有 {done0} 个(code,day), 断点续跑\n", flush=True)

    api = connect()
    t_start = time.time()
    n_ok = n_fail = 0
    try:
        for i, code in enumerate(codes, 1):
            cmap = five.setdefault(code, {})
            code_scale = 1.0
            todo = [d for d in all_dates if d not in cmap]
            if not todo:
                print(f"    [{i}/{len(codes)}] {code}: 已完整, 跳过", flush=True)
                continue
            recs = etf_daily.get(code, {}).get("returns", [])
            first = min((r["date"] for r in recs), default=args.start)
            todo = [d for d in all_dates if d >= first and d not in cmap]
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
        _update_full_daily(etf_daily)

    print(f"\n>>> 完成: 新增 {n_ok} 个(code,day), 失败 {n_fail}, "
          f"总计 {sum(len(v) for v in five.values())}")
    print(f">>> 落盘 {OUT} ({OUT.stat().st_size/1e6:.0f}MB)")


if __name__ == "__main__":
    main()

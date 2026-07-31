#!/usr/bin/env python3
"""增量把回测缓存补到最新交易日（日K + 密集5min + aligned 主缓存）。

三块数据:
  1) backfill_daily_1000.json  日K(106只)      ← akshare fund_etf_hist_sina 增量
  2) tdx_5min_2y.json          密集5min(103只) ← pytdx get_security_bars 最近若干根
  3) aligned_live_4y.json      主缓存           ← 用 1) 刷新 etf_daily/all_dates/proxy_klines
     (其 etf_5min 是有前视偏差的稀疏数据, 保持原样不动; 回测一律 --five-min 走 2))

用法:
    python scripts/update_live_cache.py              # 全量三步
    python scripts/update_live_cache.py --skip-5min  # 只补日K
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
DAILY1000 = CACHE / "backfill_daily_1000.json"
TDX5MIN = CACHE / "tdx_5min_2y.json"
ALIGNED = CACHE / "aligned_live_4y.json"

SERVERS = [
    ("115.238.56.198", 7709), ("115.238.90.165", 7709),
    ("180.153.18.170", 7709), ("218.108.98.244", 7709),
    ("123.125.108.14", 7709), ("60.28.23.80", 7709),
]
PAGE = 800          # 单次拉 800 根 5min ≈ 16 个交易日, 增量足够
COVER_THR = 0.5     # all_dates 纳入阈值: 至少一半标的当天有日K


def market_of(code: str) -> int:
    return 1 if code[0] in "56" else 0


# ---------------- 1) 日K 增量 ----------------
def update_daily(dry: bool = False) -> tuple[dict, list[str]]:
    import akshare as ak

    bf = json.loads(DAILY1000.read_text(encoding="utf-8"))
    bd = bf["etf_daily"]
    cur_last = max(r["date"] for v in bd.values() for r in v["returns"])
    print(f">>> [1/3] 日K 现有最新 {cur_last}, 增量拉取 {len(bd)} 只 ...", flush=True)

    added_dates: set[str] = set()
    n_new = 0
    mismatch = []
    for i, code in enumerate(sorted(bd), 1):
        try:
            h = ak.fund_etf_hist_sina(symbol=("sh" if code[0] in "56" else "sz") + code)
        except Exception as e:  # noqa: BLE001
            print(f"    [warn] {code} 拉取失败: {e}", flush=True)
            continue
        rows = {}
        for _, r in h.iterrows():
            d = str(r["date"])[:10]
            try:
                rows[d] = {
                    "date": d, "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": float(r.get("volume") or 0),
                }
            except Exception:  # noqa: BLE001
                continue
        exist = bd[code]["returns"]
        have = {r["date"] for r in exist}
        # 重叠校验(最近3个共同日): 新浪 close 与现有是否一致
        common = sorted(set(rows) & have)[-3:]
        for d in common:
            old = next(r["close"] for r in exist if r["date"] == d)
            if old and abs(rows[d]["close"] / old - 1) > 0.005:
                mismatch.append((code, d, old, rows[d]["close"]))
        new_days = sorted(d for d in rows if d > cur_last)
        if not new_days:
            continue
        prev_close = exist[-1]["close"] if exist else None
        for d in new_days:
            rec = dict(rows[d])
            rec["return_pct"] = ((rec["close"] - prev_close) / prev_close * 100
                                 if prev_close else 0.0)
            exist.append(rec)
            prev_close = rec["close"]
            added_dates.add(d)
            n_new += 1
        exist.sort(key=lambda x: x["date"])
        if i % 25 == 0:
            print(f"    [{i}/{len(bd)}] ...", flush=True)

    print(f"    新增 {n_new} 条, 覆盖日期 {sorted(added_dates)}")
    if mismatch:
        print(f"    ⚠ 重叠日收盘不一致 {len(mismatch)} 处(前5): {mismatch[:5]}")
    else:
        print("    ✓ 重叠日收盘校验一致")
    if added_dates and not dry:
        bf["etf_daily"] = bd
        bf["all_dates"] = sorted({r["date"] for v in bd.values() for r in v["returns"]})
        bf["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        DAILY1000.write_text(json.dumps(bf, ensure_ascii=False), encoding="utf-8")
        print(f"    已落盘 {DAILY1000.name}")
    return bd, sorted(added_dates)


# ---------------- 2) 5min 增量 ----------------
def update_5min(dry: bool = False) -> list[str]:
    from pytdx.hq import TdxHq_API

    data = json.loads(TDX5MIN.read_text(encoding="utf-8"))
    five = data["etf_5min"]
    cur_last = max(d for c in five for d in five[c])
    print(f">>> [2/3] 密集5min 现有最新 {cur_last}, 增量拉取 {len(five)} 只 ...", flush=True)

    api = TdxHq_API(heartbeat=True)
    conn = None
    for host, port in SERVERS:
        try:
            if api.connect(host, port, time_out=8):
                conn = (host, port)
                break
        except Exception:  # noqa: BLE001
            continue
    if not conn:
        raise RuntimeError("无可用通达信服务器")
    print(f"    已连接 {conn[0]}")

    added_dates: set[str] = set()
    n_new = 0
    try:
        for i, code in enumerate(sorted(five), 1):
            try:
                bars = api.get_security_bars(0, market_of(code), code, 0, PAGE)
            except Exception:  # noqa: BLE001
                bars = None
            if not bars:
                print(f"    [warn] {code} 无返回")
                continue
            by_day: dict[str, list[dict]] = {}
            for b in bars:
                dt = str(b.get("datetime", ""))
                if len(dt) < 16:
                    continue
                day, t = dt[:10], dt[11:16] + ":00"
                if day <= cur_last:
                    continue
                by_day.setdefault(day, []).append({
                    "datetime": f"{day} {t}", "day": day, "time": t,
                    "open": float(b["open"]), "high": float(b["high"]),
                    "low": float(b["low"]), "close": float(b["close"]),
                    "volume": float(b.get("vol", 0)),
                })
            for day, bs in by_day.items():
                bs.sort(key=lambda x: x["time"])
                five.setdefault(code, {})[day] = bs
                added_dates.add(day)
                n_new += 1
            if i % 25 == 0:
                print(f"    [{i}/{len(five)}] ...", flush=True)
    finally:
        try:
            api.disconnect()
        except Exception:  # noqa: BLE001
            pass

    print(f"    新增 {n_new} 个(code,day), 覆盖日期 {sorted(added_dates)}")
    for d in sorted(added_dates):
        cov = sum(1 for c in five if d in five[c])
        print(f"      {d}: {cov}/{len(five)} 只有分钟线")
    if added_dates and not dry:
        data["etf_5min"] = five
        TDX5MIN.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(f"    已落盘 {TDX5MIN.name} ({TDX5MIN.stat().st_size/1e6:.0f}MB)")
    return sorted(added_dates)


# ---------------- 3) aligned 主缓存刷新 ----------------
def refresh_aligned(bd: dict, dry: bool = False) -> None:
    print(">>> [3/3] 刷新 aligned_live_4y (etf_daily/all_dates/proxy_klines) ...", flush=True)
    a = json.loads(ALIGNED.read_text(encoding="utf-8"))
    old_last = a["all_dates"][-1]

    # etf_daily: 以新日K为准合并(保留 aligned 里 2015-2021 的老牌历史)
    ed = a["etf_daily"]
    for code, v in bd.items():
        ref = ed.setdefault(code, {"returns": []})
        have = {r["date"] for r in ref["returns"]}
        for r in v["returns"]:
            if r["date"] not in have:
                ref["returns"].append(r)
        ref["returns"].sort(key=lambda x: x["date"])

    # all_dates: 原有 ∪ 覆盖率达标的新日期
    n_codes = len(bd)
    date_cov: dict[str, int] = {}
    for v in bd.values():
        for r in v["returns"]:
            date_cov[r["date"]] = date_cov.get(r["date"], 0) + 1
    new_dates = [d for d, c in date_cov.items()
                 if d > old_last and c >= n_codes * COVER_THR]
    a["all_dates"] = sorted(set(a["all_dates"]) | set(new_dates))

    # proxy_klines(501018 日K)
    proxy_raw = ed.get("501018", {}).get("returns", [])
    a["proxy_klines"] = [{
        "day": r["date"],
        "open": float(r.get("open", r.get("close", 0))),
        "high": float(r.get("high", r.get("close", 0))),
        "low": float(r.get("low", r.get("close", 0))),
        "close": float(r.get("close", 0)),
        "volume": float(r.get("volume", 0)),
    } for r in proxy_raw]

    print(f"    all_dates {old_last} → {a['all_dates'][-1]} (新增 {sorted(new_dates)})")
    print(f"    proxy(501018) {len(a['proxy_klines'])} 条, 末日 "
          f"{a['proxy_klines'][-1]['day'] if a['proxy_klines'] else '—'}")
    if not dry:
        a["data_source"] = a.get("data_source", "") + "+update_live_cache"
        ALIGNED.write_text(json.dumps(a, ensure_ascii=False), encoding="utf-8")
        print(f"    已落盘 {ALIGNED.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description="增量补齐回测缓存到最新交易日")
    ap.add_argument("--skip-daily", action="store_true")
    ap.add_argument("--skip-5min", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bd = None
    if not args.skip_daily:
        bd, _ = update_daily(args.dry_run)
    if not args.skip_5min:
        update_5min(args.dry_run)
    if bd is None:
        bd = json.loads(DAILY1000.read_text(encoding="utf-8"))["etf_daily"]
    refresh_aligned(bd, args.dry_run)
    print("\n完成。")


if __name__ == "__main__":
    main()

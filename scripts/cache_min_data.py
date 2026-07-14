#!/usr/bin/env python3
"""每日缓存 T+0 ETF 的 1 分 K 和 5 分 K 数据。

数据源：
- 1分K: AkShare stock_zh_a_minute (新浪网页接口，每次1970根≈9天)
- 5分K: 新浪 API (scale=5, datalen=5000≈105天)

每天 15:10 运行，缓存当日数据到 ~/.tradingagents/rotation/min_cache/
按 {etf_code}_{period}_{date}.json 存储，便于后续精确回测。

用法:
    python scripts/cache_min_data.py              # 缓存当天
    python scripts/cache_min_data.py --backfill 5 # 补最近5天（用1分K的9天窗口）
    python scripts/cache_min_data.py --dry-run    # 只打印不保存
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# 清除代理（直连国内站点更快）
for k in list(os.environ):
    if "proxy" in k.lower():
        del os.environ[k]

CACHE_DIR = Path.home() / ".tradingagents" / "rotation" / "min_cache"
SINA_INTERVAL = 0.2


def fetch_1min_sina(symbol: str) -> list[dict]:
    """通过 AkShare 拉取 1 分 K（新浪网页接口，每次1970根≈9天）。"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_minute(symbol=symbol, period="1", adjust="")
        bars = []
        for _, row in df.iterrows():
            bars.append({
                "day": row["day"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })
        return bars
    except Exception as e:
        print(f"    1分K拉取失败 {symbol}: {e}")
        return []


def fetch_5min_sina(symbol: str, datalen: int = 5000) -> list[dict]:
    """新浪 API 拉 5 分 K。"""
    import subprocess
    url = (
        f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=5&ma=no&datalen={datalen}"
    )
    r = subprocess.run(
        ["curl", "-s", "--connect-timeout", "10", url],
        capture_output=True, timeout=15,
    )
    raw = r.stdout.decode("utf-8", errors="ignore").strip()
    if not raw or raw in ("null", "[]"):
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def save_cache(bars: list[dict], etf_code: str, period: str, target_date: str):
    """保存某日某 ETF 某周期的 K 线。"""
    if not bars:
        return 0
    # 筛出目标日期的 bars
    day_bars = [b for b in bars if b.get("day", "").startswith(target_date)]
    if not day_bars:
        return 0

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{etf_code}_{period}_{target_date}.json"
    fpath = CACHE_DIR / fname

    # 如果已存在且条数更多，不覆盖
    if fpath.exists():
        try:
            old = json.loads(fpath.read_text(encoding="utf-8"))
            if len(old) >= len(day_bars):
                return len(old)
        except Exception:
            pass

    fpath.write_text(json.dumps(day_bars, ensure_ascii=False), encoding="utf-8")
    return len(day_bars)


def load_etf_list() -> list[dict]:
    """加载 T+0 ETF 列表 + A 股板块 ETF（两者都缓存）。"""
    etfs = []

    # T+0 ETF
    try:
        from t0_etf_list import get_all_t0_etfs
        for e in get_all_t0_etfs():
            etfs.append({"code": e["code"], "sina_symbol": e["sina_symbol"], "name": e["name"], "pool": "t0"})
    except ImportError:
        pass

    # A 股板块 ETF（平安板块池）
    try:
        from sector_etf_map import load_pingan_sectors
        for s in load_pingan_sectors():
            code = s["etf_code"]
            sina = f"sh{code}" if code.startswith("5") else f"sz{code}"
            etfs.append({"code": code, "sina_symbol": sina, "name": s["name"], "pool": "astock"})
    except ImportError:
        pass

    # 去重
    seen = set()
    unique = []
    for e in etfs:
        if e["code"] not in seen:
            seen.add(e["code"])
            unique.append(e)
    return unique


def run_cache(target_date: str | None = None, dry_run: bool = False, backfill: int = 0):
    today = target_date or date.today().isoformat()
    print(f"=== 缓存分钟K线 | {today} | {datetime.now().strftime('%H:%M:%S')} ===")

    # 去重：如果今天已缓存过且非backfill模式，跳过
    if not dry_run and backfill == 0 and not target_date:
        lock_file = CACHE_DIR / f".lock_{today}"
        if lock_file.exists():
            age = (datetime.now() - datetime.fromtimestamp(lock_file.stat().st_mtime)).total_seconds()
            if age < 3600:  # 1小时内的锁文件，跳过
                print(f"今天已缓存过（锁文件 {age:.0f}秒前），跳过")
                return {"skipped": True}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(datetime.now().isoformat())

    etf_list = load_etf_list()
    print(f"ETF 池: {len(etf_list)} 只\n")

    if dry_run:
        print("--dry-run 模式，仅测试数据源连通性")
        # 只测前3只
        etf_list = etf_list[:3]

    stats = {"1min_ok": 0, "1min_fail": 0, "5min_ok": 0, "5min_fail": 0, "bars_1min": 0, "bars_5min": 0, "skipped": 0}

    for i, etf in enumerate(etf_list):
        code = etf["code"]
        sym = etf["sina_symbol"]
        name = etf["name"]

        # ETF级去重：如果当日1分K和5分K文件都已存在，跳过网络请求
        if not dry_run and backfill == 0:
            f1 = CACHE_DIR / f"{code}_1min_{today}.json"
            f5 = CACHE_DIR / f"{code}_5min_{today}.json"
            if f1.exists() and f5.exists():
                stats["skipped"] += 1
                continue

        # 1分K（AkShare，每次返回9天，直接缓存9天）
        bars1 = fetch_1min_sina(sym)
        if bars1:
            # 提取所有日期，缓存每一天
            dates_in_data = sorted(set(b["day"][:10] for b in bars1))
            if backfill > 0:
                # 只缓存最近 backfill 天
                dates_in_data = dates_in_data[-backfill:]
            elif target_date:
                dates_in_data = [d for d in dates_in_data if d == target_date]

            for d in dates_in_data:
                if not dry_run:
                    cnt = save_cache(bars1, code, "1min", d)
                    stats["bars_1min"] += cnt
            stats["1min_ok"] += 1
            if (i + 1) % 20 == 0 or i < 3:
                print(f"  [{i+1}/{len(etf_list)}] {code} {name}: 1分K {len(bars1)}根 ({dates_in_data[0]}~{dates_in_data[-1]})")
        else:
            stats["1min_fail"] += 1

        time.sleep(0.1)  # AkShare 内部有限速

        # 5分K（新浪API）
        bars5 = fetch_5min_sina(sym, datalen=5000)
        if bars5:
            dates5 = sorted(set(b.get("day", "")[:10] for b in bars5))
            if target_date:
                dates5 = [d for d in dates5 if d == target_date]
            elif backfill > 0:
                dates5 = dates5[-backfill:]

            for d in dates5:
                if not dry_run:
                    cnt = save_cache(bars5, code, "5min", d)
                    stats["bars_5min"] += cnt
            stats["5min_ok"] += 1
        else:
            stats["5min_fail"] += 1

        time.sleep(SINA_INTERVAL)

    print(f"\n=== 完成 ===")
    print(f"  1分K: {stats['1min_ok']}成功 {stats['1min_fail']}失败 {stats['bars_1min']}根")
    print(f"  5分K: {stats['5min_ok']}成功 {stats['5min_fail']}失败 {stats['bars_5min']}根")
    if stats.get("skipped", 0):
        print(f"  跳过: {stats['skipped']}只ETF（当日已缓存）")

    if not dry_run:
        # 统计缓存目录
        files = list(CACHE_DIR.glob("*.json"))
        total_size = sum(f.stat().st_size for f in files) / 1024 / 1024
        print(f"  缓存目录: {CACHE_DIR}")
        print(f"  文件数: {len(files)} 总大小: {total_size:.1f}MB")

    return stats


def main():
    parser = argparse.ArgumentParser(description="每日缓存1分K和5分K数据")
    parser.add_argument("--date", type=str, default="", help="指定日期 YYYY-MM-DD")
    parser.add_argument("--backfill", type=int, default=0, help="补最近N天")
    parser.add_argument("--dry-run", action="store_true", help="只测试不保存")
    args = parser.parse_args()

    run_cache(
        target_date=args.date or None,
        dry_run=args.dry_run,
        backfill=args.backfill,
    )


if __name__ == "__main__":
    main()

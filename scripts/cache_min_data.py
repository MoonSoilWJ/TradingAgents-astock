#!/usr/bin/env python3
"""每日缓存 ETF 的 1 分 K 和 5 分 K 数据。

数据源：
- 1分K: AkShare stock_zh_a_minute (新浪网页接口，每次1970根≈9天)
- 5分K: 新浪 API (scale=5, datalen=5000≈105天)

存储（按 {etf_code}_{period}_{date}.json）:
- 小池子(~158只): ~/.tradingagents/rotation/min_cache/        (cron 15:10)
- 全市场(~1733只): ~/.tradingagents/rotation/min_cache_allmarket/ (cron 15:35)

用法:
    python scripts/cache_min_data.py              # 小池子，缓存当天
    python scripts/cache_min_data.py --all-market # 全市场 ~1733 只
    python scripts/cache_min_data.py --backfill 5 # 补最近5天（用1分K的9天窗口）
    python scripts/cache_min_data.py --dry-run    # 只测试不保存
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from threading import Lock

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# 清除代理（直连国内站点更快）
for k in list(os.environ):
    if "proxy" in k.lower():
        del os.environ[k]

CACHE_DIR = Path.home() / ".tradingagents" / "rotation" / "min_cache"
ALLMARKET_CACHE_DIR = Path.home() / ".tradingagents" / "rotation" / "min_cache_allmarket"
DEFAULT_5MIN_POOL = (
    Path.home() / ".tradingagents/cache/t0_5min/pool_20260721_days100_allmarket.json"
)
SINA_INTERVAL = 0.2
AKSHARE_INTERVAL = 0.12


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


def save_cache(
    bars: list[dict],
    etf_code: str,
    period: str,
    target_date: str,
    *,
    cache_dir: Path | None = None,
):
    """保存某日某 ETF 某周期的 K 线。"""
    if not bars:
        return 0
    # 筛出目标日期的 bars
    day_bars = [b for b in bars if b.get("day", "").startswith(target_date)]
    if not day_bars:
        return 0

    root = cache_dir or CACHE_DIR
    root.mkdir(parents=True, exist_ok=True)
    fname = f"{etf_code}_{period}_{target_date}.json"
    fpath = root / fname

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


def load_allmarket_universe(limit: int | None = None) -> list[dict]:
    """全市场可缓存名单：优先用已有 5 分 K 池（~1735 只），否则 mootdx 全市场。"""
    from t0_etf_list import get_all_market_etf_lof, sina_symbol_for  # noqa: PLC0415

    codes: list[str] = []
    if DEFAULT_5MIN_POOL.exists():
        try:
            cached = json.loads(DEFAULT_5MIN_POOL.read_text(encoding="utf-8"))
            codes = sorted(cached.get("etf_5min", {}).keys())
        except Exception:
            codes = []

    if not codes:
        codes = [e["code"] for e in get_all_market_etf_lof()]

    am_map = {e["code"]: e for e in get_all_market_etf_lof()}
    out: list[dict] = []
    for code in codes:
        if code in am_map:
            row = am_map[code]
            out.append({
                "code": code,
                "sina_symbol": row.get("sina_symbol") or sina_symbol_for(code),
                "name": row.get("name") or row.get("etf_name") or code,
                "pool": "allmarket",
            })
        else:
            out.append({
                "code": code,
                "sina_symbol": sina_symbol_for(code),
                "name": code,
                "pool": "allmarket",
            })
    if limit and limit > 0:
        out = out[:limit]
    return out


def _dates_to_save(
    dates_in_data: list[str],
    *,
    target_date: str | None,
    backfill: int,
    daily_only: bool,
) -> list[str]:
    if target_date:
        return [d for d in dates_in_data if d == target_date]
    if backfill > 0:
        return dates_in_data[-backfill:]
    if daily_only:
        return dates_in_data[-1:] if dates_in_data else []
    return dates_in_data


def _cache_5min_only(
    etf: dict,
    today: str,
    *,
    cache_dir: Path,
    dry_run: bool,
    backfill: int,
    target_date: str | None,
    daily_only_5min: bool,
) -> dict:
    code = etf["code"]
    sym = etf["sina_symbol"]
    out = {"code": code, "5min_ok": False, "5min_fail": False, "bars_5min": 0}
    bars5 = fetch_5min_sina(sym, datalen=5000)
    if bars5:
        dates5 = _dates_to_save(
            sorted({b.get("day", "")[:10] for b in bars5 if b.get("day")}),
            target_date=target_date,
            backfill=backfill,
            daily_only=daily_only_5min,
        )
        for d in dates5:
            if not dry_run:
                out["bars_5min"] += save_cache(bars5, code, "5min", d, cache_dir=cache_dir)
        out["5min_ok"] = True
    else:
        out["5min_fail"] = True
    return out


def _cache_1min_only(
    etf: dict,
    *,
    cache_dir: Path,
    dry_run: bool,
    backfill: int,
    target_date: str | None,
    daily_only_1min: bool,
) -> dict:
    code = etf["code"]
    sym = etf["sina_symbol"]
    out = {"code": code, "1min_ok": False, "1min_fail": False, "bars_1min": 0}
    bars1 = fetch_1min_sina(sym)
    if bars1:
        dates1 = _dates_to_save(
            sorted({b["day"][:10] for b in bars1}),
            target_date=target_date,
            backfill=backfill,
            daily_only=daily_only_1min,
        )
        for d in dates1:
            if not dry_run:
                out["bars_1min"] += save_cache(bars1, code, "1min", d, cache_dir=cache_dir)
        out["1min_ok"] = True
    else:
        out["1min_fail"] = True
    time.sleep(AKSHARE_INTERVAL)
    return out


def _cache_one_etf(
    etf: dict,
    today: str,
    *,
    cache_dir: Path,
    dry_run: bool,
    backfill: int,
    target_date: str | None,
    daily_only_1min: bool = False,
    daily_only_5min: bool = False,
    skip_if_cached: bool,
) -> dict:
    code = etf["code"]
    sym = etf["sina_symbol"]
    out = {
        "code": code,
        "1min_ok": False,
        "1min_fail": False,
        "5min_ok": False,
        "5min_fail": False,
        "bars_1min": 0,
        "bars_5min": 0,
        "skipped": False,
    }

    if skip_if_cached and not dry_run and backfill == 0:
        f1 = cache_dir / f"{code}_1min_{today}.json"
        f5 = cache_dir / f"{code}_5min_{today}.json"
        if f1.exists() and f5.exists():
            out["skipped"] = True
            return out

    bars1 = fetch_1min_sina(sym)
    if bars1:
        dates1 = _dates_to_save(
            sorted({b["day"][:10] for b in bars1}),
            target_date=target_date,
            backfill=backfill,
            daily_only=daily_only_1min,
        )
        for d in dates1:
            if not dry_run:
                out["bars_1min"] += save_cache(bars1, code, "1min", d, cache_dir=cache_dir)
        out["1min_ok"] = True
    else:
        out["1min_fail"] = True

    time.sleep(AKSHARE_INTERVAL)

    bars5 = fetch_5min_sina(sym, datalen=5000)
    if bars5:
        dates5 = _dates_to_save(
            sorted({b.get("day", "")[:10] for b in bars5 if b.get("day")}),
            target_date=target_date,
            backfill=backfill,
            daily_only=daily_only_5min,
        )
        for d in dates5:
            if not dry_run:
                out["bars_5min"] += save_cache(bars5, code, "5min", d, cache_dir=cache_dir)
        out["5min_ok"] = True
    else:
        out["5min_fail"] = True

    time.sleep(SINA_INTERVAL)
    return out


def run_cache_allmarket(
    target_date: str | None = None,
    dry_run: bool = False,
    backfill: int = 0,
    *,
    workers: int = 8,
    limit: int | None = None,
):
    today = target_date or date.today().isoformat()
    cache_dir = ALLMARKET_CACHE_DIR
    print(f"=== 全市场分钟K缓存 | {today} | {datetime.now().strftime('%H:%M:%S')} ===")
    print(f"目录: {cache_dir}\n")

    etf_list = load_allmarket_universe(limit=limit)
    only_today_5m = backfill == 0 and not target_date
    print(f"ETF 池: {len(etf_list)} 只 | 5分并行workers={workers} | 1分串行\n")

    stats = {
        "1min_ok": 0,
        "1min_fail": 0,
        "5min_ok": 0,
        "5min_fail": 0,
        "bars_1min": 0,
        "bars_5min": 0,
        "skipped": 0,
    }

    todo_5min: list[dict] = []
    todo_1min: list[dict] = []
    for etf in etf_list:
        code = etf["code"]
        if dry_run or backfill > 0 or target_date:
            todo_5min.append(etf)
            todo_1min.append(etf)
            continue
        f1 = cache_dir / f"{code}_1min_{today}.json"
        f5 = cache_dir / f"{code}_5min_{today}.json"
        has1 = f1.exists()
        has5 = f5.exists()
        if has1 and has5:
            stats["skipped"] += 1
            continue
        if not has5:
            todo_5min.append(etf)
        if not has1:
            todo_1min.append(etf)

    if stats["skipped"]:
        print(f"跳过已缓存: {stats['skipped']} 只")
    if not todo_5min and not todo_1min:
        print("全部已缓存，无需拉取")
        return stats
    print(f"待拉 5分K: {len(todo_5min)} 只 | 待拉 1分K: {len(todo_1min)} 只\n")

    cache_dir.mkdir(parents=True, exist_ok=True)

    # 阶段1: 5分K 并行（curl 线程安全）
    if todo_5min:
        print(f">>> 阶段1: 5分K ({len(todo_5min)} 只)...")
    w = min(workers, max(1, len(todo_5min)))
    done = 0
    print_lock = Lock()

    def _task5(etf: dict) -> dict:
        row = _cache_5min_only(
            etf, today,
            cache_dir=cache_dir,
            dry_run=dry_run,
            backfill=backfill,
            target_date=target_date,
            daily_only_5min=only_today_5m,
        )
        time.sleep(SINA_INTERVAL)
        return row

    with ThreadPoolExecutor(max_workers=w) as pool:
        futs = {pool.submit(_task5, etf): etf for etf in todo_5min}
        for fut in as_completed(futs):
            row = fut.result()
            done += 1
            if row.get("5min_ok"):
                stats["5min_ok"] += 1
            if row.get("5min_fail"):
                stats["5min_fail"] += 1
            stats["bars_5min"] += row.get("bars_5min", 0)
            if done % 100 == 0 or done == len(todo_5min):
                with print_lock:
                    print(f"  5分K {done}/{len(todo_5min)} OK={stats['5min_ok']}")

    # 阶段2: 1分K 串行（AkShare 非线程安全）
    if todo_1min:
        print(f"\n>>> 阶段2: 1分K 串行 ({len(todo_1min)} 只)...")
        for i, etf in enumerate(todo_1min):
            row = _cache_1min_only(
                etf,
                cache_dir=cache_dir,
                dry_run=dry_run,
                backfill=backfill,
                target_date=target_date,
                daily_only_1min=False,
            )
            if row.get("1min_ok"):
                stats["1min_ok"] += 1
            if row.get("1min_fail"):
                stats["1min_fail"] += 1
            stats["bars_1min"] += row.get("bars_1min", 0)
            if (i + 1) % 50 == 0 or i + 1 == len(todo_1min):
                print(f"  1分K {i+1}/{len(todo_1min)} OK={stats['1min_ok']}")

    print(f"\n=== 完成 ===")
    print(f"  1分K: {stats['1min_ok']}成功 {stats['1min_fail']}失败 {stats['bars_1min']}根")
    print(f"  5分K: {stats['5min_ok']}成功 {stats['5min_fail']}失败 {stats['bars_5min']}根")
    if stats["skipped"]:
        print(f"  跳过: {stats['skipped']}只（当日已缓存）")

    if not dry_run:
        files = [f for f in cache_dir.glob("*_*.json") if not f.name.startswith(".")]
        total_size = sum(f.stat().st_size for f in files) / 1024 / 1024
        day_files = len(list(cache_dir.glob(f"*_1min_{today}.json")))
        manifest = {
            "date": today,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "universe": len(etf_list),
            "1min_ok": stats["1min_ok"],
            "5min_ok": stats["5min_ok"],
            "1min_fail": stats["1min_fail"],
            "5min_fail": stats["5min_fail"],
            "skipped": stats["skipped"],
            "files_total": len(files),
            "files_today_1min": day_files,
            "size_mb": round(total_size, 2),
        }
        (cache_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"  缓存: {len(files)} 文件 {total_size:.1f}MB | 今日1分K {day_files} 只")
        print(f"  manifest: {cache_dir / 'manifest.json'}")

    return stats


def run_cache(
    target_date: str | None = None,
    dry_run: bool = False,
    backfill: int = 0,
    *,
    cache_dir: Path | None = None,
):
    today = target_date or date.today().isoformat()
    root = cache_dir or CACHE_DIR
    label = "全市场" if root == ALLMARKET_CACHE_DIR else "小池子"
    print(f"=== 缓存分钟K线({label}) | {today} | {datetime.now().strftime('%H:%M:%S')} ===")

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
            f1 = root / f"{code}_1min_{today}.json"
            f5 = root / f"{code}_5min_{today}.json"
            if f1.exists() and f5.exists():
                stats["skipped"] += 1
                continue

        root.mkdir(parents=True, exist_ok=True)
        row = _cache_one_etf(
            etf, today,
            cache_dir=root,
            dry_run=dry_run,
            backfill=backfill,
            target_date=target_date,
            daily_only_1min=False,
            daily_only_5min=False,
            skip_if_cached=False,
        )
        stats["1min_ok"] += int(row["1min_ok"])
        stats["1min_fail"] += int(row["1min_fail"])
        stats["5min_ok"] += int(row["5min_ok"])
        stats["5min_fail"] += int(row["5min_fail"])
        stats["bars_1min"] += row["bars_1min"]
        stats["bars_5min"] += row["bars_5min"]
        if row["1min_ok"] and ((i + 1) % 20 == 0 or i < 3):
            print(f"  [{i+1}/{len(etf_list)}] {code} {name}: 1分K {row['bars_1min']}根 5分K {row['bars_5min']}根")

    print(f"\n=== 完成 ===")
    print(f"  1分K: {stats['1min_ok']}成功 {stats['1min_fail']}失败 {stats['bars_1min']}根")
    print(f"  5分K: {stats['5min_ok']}成功 {stats['5min_fail']}失败 {stats['bars_5min']}根")
    if stats.get("skipped", 0):
        print(f"  跳过: {stats['skipped']}只ETF（当日已缓存）")

    if not dry_run:
        # 统计缓存目录
        files = list(root.glob("*.json"))
        total_size = sum(f.stat().st_size for f in files) / 1024 / 1024
        print(f"  缓存目录: {root}")
        print(f"  文件数: {len(files)} 总大小: {total_size:.1f}MB")

    return stats


def main():
    parser = argparse.ArgumentParser(description="每日缓存1分K和5分K数据")
    parser.add_argument("--date", type=str, default="", help="指定日期 YYYY-MM-DD")
    parser.add_argument("--backfill", type=int, default=0, help="补最近N天")
    parser.add_argument("--dry-run", action="store_true", help="只测试不保存")
    parser.add_argument("--all-market", action="store_true", help="全市场 ~1733 只（并行）")
    parser.add_argument("--workers", type=int, default=8, help="全市场并行 workers（默认8）")
    parser.add_argument("--limit", type=int, default=0, help="仅拉前 N 只（调试）")
    args = parser.parse_args()

    if args.all_market:
        run_cache_allmarket(
            target_date=args.date or None,
            dry_run=args.dry_run,
            backfill=args.backfill,
            workers=args.workers,
            limit=args.limit or None,
        )
        return

    run_cache(
        target_date=args.date or None,
        dry_run=args.dry_run,
        backfill=args.backfill,
    )


if __name__ == "__main__":
    main()

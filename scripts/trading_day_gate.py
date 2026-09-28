#!/usr/bin/env python3
"""交易日 gate —— A股 cron 的节假日守门员。

问题: crontab 的星期字段(1-5)不认识法定节假日/调休, 2026-09-25(周五,中秋)
全部交易 cron 照跑, watchdog 把 youzi_live 空转拉起 4 次, shadow journal 被
写入休市脏记录。本脚本给所有交易相关 cron 提供统一 gate。

用法:
  trading_day_gate.py                      # 判断今天: 交易日 exit 0, 非交易日 exit 1
  trading_day_gate.py --date 2026-10-01    # 判断指定日期(测试/回填用)

cron 用法:  <本脚本> && <原命令>  —— 仅在"非交易日"写一行日志到 /tmp/trading_gate.log,
交易日静默放行(不写日志, 防止膨胀)。

日历: akshare tool_trade_date_hist_sina() → 按年缓存到
~/.tradingagents/cache/trading_days_<year>.json。缓存优先(离线可用),
缺失时在线拉取; 双双失败 fail-open(exit 0) 并写 WARN —— 宁可假日空转一天,
也不让实盘因日历故障漏交易。每年 11 月起自动预取下一年缓存, 防跨年真空。
"""
import argparse
import datetime as dt
import json
import pathlib
import sys

CACHE_DIR = pathlib.Path.home() / ".tradingagents" / "cache"
LOG = pathlib.Path("/tmp/trading_gate.log")


def _cache_path(year: int) -> pathlib.Path:
    return CACHE_DIR / f"trading_days_{year}.json"


def _load_cache(year: int):
    p = _cache_path(year)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        days = data.get("days") or []
        return set(days) if days else None
    except Exception:
        return None


def _fetch_year(year: int):
    """在线拉某年交易日集合(含全年, 含未来休市日)。失败返回 None。"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        days = sorted(d.strftime("%Y-%m-%d") for d in df["trade_date"]
                      if getattr(d, "year", None) == year)
        return days or None
    except Exception:
        return None


def _save_cache(year: int, days) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(year).write_text(json.dumps(
            {"year": year, "days": days,
             "updated": dt.date.today().isoformat()},
            ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _prefetch_next_year(today: dt.date) -> None:
    """Q4 预取下一年缓存, 防止跨年真空。幂等, 已有则跳过。"""
    if today.month >= 11 and _load_cache(today.year + 1) is None:
        days = _fetch_year(today.year + 1)
        if days:
            _save_cache(today.year + 1, days)


def _log(line: str) -> None:
    try:
        with LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="判断指定日期 YYYY-MM-DD(默认今天, 测试用)")
    args = ap.parse_args()

    if args.date:
        try:
            day = dt.date.fromisoformat(args.date)
        except ValueError:
            print(f"[gate] 非法日期 {args.date}", file=sys.stderr)
            return 2
    else:
        day = dt.date.today()

    days = _load_cache(day.year)
    if days is None:
        fetched = _fetch_year(day.year)
        if fetched:
            _save_cache(day.year, fetched)
            days = set(fetched)

    if days is not None:
        is_open = day.isoformat() in days
        if not args.date:                      # 正式运行才写日志/预取
            _prefetch_next_year(day)
            if not is_open:
                _log(f"{dt.datetime.now():%F %T} [gate] {day} 非交易日(休市), 跳过")
        return 0 if is_open else 1

    # 日历彻底不可用 → fail-open, 但留痕
    _log(f"{dt.datetime.now():%F %T} [gate][WARN] {day} 无法获取交易日历, fail-open 放行")
    return 0


if __name__ == "__main__":
    sys.exit(main())

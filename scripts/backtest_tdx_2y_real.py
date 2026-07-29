#!/usr/bin/env python3
"""pytdx 回填 2 年真实 5min（2024-07 ~ 至今），跑真·实盘双时点全区间回测。

- 通达信行情服务器 5min 深度 ~23850 根 ≈ 2 年（新浪/东财只有 ~4 个月/1.5 个月）
- 拉全 T+0 池归一化成与新浪一致的格式，落盘 tdx_5min_2y.json
- 用回填日K(backfill_daily_1000.json) + 真 5min 跑双时点 14:40 实盘口径
- 同区间日K近似对照，检验校准系数跨年稳定性
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from pytdx.hq import TdxHq_API  # noqa: E402

from backtest_t0_idle_dual import compound  # noqa: E402
from backtest_t0_today1 import FEE_PCT, run_backtest, run_backtest_aligned  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
BACKFILL = CACHE / "backfill_daily_1000.json"
OUT = CACHE / "tdx_5min_2y.json"

SERVERS = [
    ("115.238.56.198", 7709), ("115.238.90.165", 7709),
    ("180.153.18.170", 7709), ("218.108.98.244", 7709),
    ("123.125.108.14", 7709), ("60.28.23.80", 7709),
]
PAGE = 800
MAX_OFFSET = 26400  # 上限 ~23850，留余量


def market_of(code: str) -> int:
    return 1 if code[0] in ("5", "6") else 0


def connect() -> TdxHq_API:
    api = TdxHq_API()
    for h, p in SERVERS:
        try:
            if api.connect(h, p, time_out=5):
                print(f"    已连接 {h}:{p}")
                return api
        except Exception:
            continue
    raise RuntimeError("无可用通达信服务器")


def fetch_symbol(api: TdxHq_API, code: str) -> dict[str, list[dict]]:
    """翻页拉全深度 5min，归一化成 {date: [bars]}（与 normalize_5min_bars 一致）。"""
    mkt = market_of(code)
    raw: list[dict] = []
    offset = 0
    while offset <= MAX_OFFSET:
        try:
            bars = api.get_security_bars(0, mkt, code, offset, PAGE)
        except Exception:
            bars = None
        if not bars:
            break
        raw = list(bars) + raw
        if len(bars) < PAGE:
            break
        offset += PAGE
    by_day: dict[str, list[dict]] = {}
    for b in raw:
        dt = str(b.get("datetime", ""))
        if len(dt) < 16:
            continue
        day, t = dt[:10], dt[11:16] + ":00"
        by_day.setdefault(day, []).append({
            "datetime": f"{day} {t}", "day": day, "time": t,
            "open": float(b["open"]), "high": float(b["high"]),
            "low": float(b["low"]), "close": float(b["close"]),
            "volume": float(b.get("vol", 0)),
        })
    for day in by_day:
        by_day[day].sort(key=lambda x: x["time"])
    return by_day


def load_or_fetch_tdx(codes: list[str]) -> dict[str, dict]:
    if OUT.exists():
        print(f">>> 使用已落盘 {OUT.name}")
        return json.loads(OUT.read_text(encoding="utf-8"))["etf_5min"]
    api = connect()
    etf_5min: dict[str, dict] = {}
    reconnect_count = 0
    for i, code in enumerate(codes, 1):
        bars = fetch_symbol(api, code)
        if not bars and reconnect_count < 5:
            # 可能断线，重连重试一次
            try:
                api.disconnect()
            except Exception:
                pass
            api = connect()
            reconnect_count += 1
            bars = fetch_symbol(api, code)
        if bars:
            days = sorted(bars)
            etf_5min[code] = bars
            print(f"    [{i}/{len(codes)}] {code}: {len(days)}天 "
                  f"{days[0]}~{days[-1]}")
        else:
            print(f"    [{i}/{len(codes)}] {code}: 无数据")
    api.disconnect()
    OUT.write_text(json.dumps({"etf_5min": etf_5min}, ensure_ascii=False),
                   encoding="utf-8")
    print(f">>> 已落盘 {OUT} ({OUT.stat().st_size/1e6:.0f}MB)")
    return etf_5min


def iso_week(d: str) -> str:
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return f"{y}/W{w:02d}"


def mdd(trades: list[dict]) -> float:
    eq, peak, m = 1.0, 1.0, 0.0
    for t in sorted(trades, key=lambda x: x["signal_date"]):
        eq *= 1 + t["return_pct"] / 100
        peak = max(peak, eq)
        m = min(m, (eq - peak) / peak * 100)
    return m


def worst_week(trades: list[dict]) -> tuple[str, float]:
    wr: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        wr[iso_week(t["signal_date"])].append(t["return_pct"])
    if not wr:
        return "-", 0.0
    wk = min(wr, key=lambda k: compound(wr[k]))
    return wk, compound(wr[wk])


def report(tag: str, res: dict) -> None:
    trades = res["trades"]
    wk, wv = worst_week(trades)
    print(f"\n=== {tag} ===")
    print(f"总计: {res['trade_count']} 笔, 累计 {res['final_equity_pct']:+.2f}%, "
          f"最大回撤 {mdd(trades):.2f}%, 最差周 {wk} {wv:+.2f}%")
    by_year: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_year[t["signal_date"][:4]].append(t)
    print(f"{'年':<6}{'笔':>4}{'收益':>10}{'回撤':>9}{'最差周':>18}")
    for y in sorted(by_year):
        ts = by_year[y]
        wk2, wv2 = worst_week(ts)
        print(f"{y:<6}{len(ts):>4}{compound([t['return_pct'] for t in ts]):+10.2f}%"
              f"{mdd(ts):>9.2f}%{wk2:>13}{wv2:>+8.2f}%")


def main() -> None:
    etf_list = get_all_t0_etfs()
    codes = [e["code"] for e in etf_list]
    print(f">>> T+0 池 {len(codes)} 只，pytdx 拉 2 年 5min ...")
    etf_5min = load_or_fetch_tdx(codes)

    bf = json.loads(BACKFILL.read_text(encoding="utf-8"))
    bd = bf["etf_daily"]
    proxy = bf.get("proxy_klines", [])

    # 数据一致性抽检：tdx close vs 日K close
    checked = 0
    for code in list(etf_5min)[:20]:
        days = sorted(etf_5min[code])
        if not days or code not in bd:
            continue
        d = days[-2] if len(days) > 1 else days[-1]
        dc = {r["date"]: r["close"] for r in bd[code].get("returns", [])}.get(d)
        if dc:
            tc = etf_5min[code][d][-1]["close"]
            diff = abs(tc - dc) / dc * 100
            if diff > 1.0:
                print(f"    ⚠️ {code} {d}: tdx={tc} vs 日K={dc} 差{diff:.2f}%")
            checked += 1
    print(f">>> 抽检 {checked} 只收盘价一致性完成")

    all_days = sorted({d for bars in etf_5min.values() for d in bars})
    # 只取有足够标的覆盖的日期（>=池一半）
    cover = {d: sum(1 for c in etf_5min if d in etf_5min[c]) for d in all_days}
    dates = [d for d in all_days if cover[d] >= len(etf_5min) * 0.5]
    print(f">>> 真实 5min 覆盖: {dates[0]} ~ {dates[-1]} ({len(dates)} 交易日, "
          f"{len(etf_5min)} 只)")

    real = run_backtest(etf_list, bd, etf_5min, dates, dates, FEE_PCT,
                        use_filter=True, daily_proxy=False, confirm_time="14:40",
                        proxy_klines=proxy)

    # 实盘对齐口径：用 run_backtest_aligned，信号/买入/确认各后移一个bar，
    # 拿到决策时刻的实时价（price_at_time 只取已完成bar，否则14:50 信号只拿到
    # 14:45 bar，系统性看不到尾盘最后5分钟脉冲，如 2026-07-24 豆粕：实盘 +3.21%
    # 买入亏 -2.55%，滞后口径只有 +2.27% 被 MIN_GAIN 过滤）。
    aligned = run_backtest_aligned(etf_list, bd, etf_5min, dates, dates, FEE_PCT,
                                  use_filter=True, daily_proxy=False,
                                  proxy_klines=proxy)

    approx = run_backtest(etf_list, bd, {}, dates, dates, FEE_PCT,
                          use_filter=True, daily_proxy=True, proxy_klines=proxy)

    report("滞后bar口径（偏乐观，看不到尾盘脉冲）", real)
    report("实盘对齐口径（实时价，决策级数字）", aligned)
    report("日K隔夜近似（同区间对照）", approx)

    r_r, r_a = aligned["final_equity_pct"], approx["final_equity_pct"]
    print(f"\n>>> 2年校准系数(实盘对齐口径): 收益 ×{(r_r / r_a if r_a else 0):.2f}, "
          f"回撤 ×{(mdd(aligned['trades']) / mdd(approx['trades']) if approx['trades'] else 0):.2f}")


if __name__ == "__main__":
    main()

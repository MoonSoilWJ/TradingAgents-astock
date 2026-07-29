#!/usr/bin/env python3
"""全 4 年日K近似回测（2022-06 ~ 今），并按 2 年实测校准系数 ×2.97 外推精确策略。

为什么需要这个脚本：
  pytdx 5min 协议硬上限 ~2 年（2024-07 起），无法更早；但本地
  backfill_daily_1000.json 已有 2022-06-15 ~ 今的 1000 交易日日K。
  日K近似模式( daily_proxy=True )只用日K：选股用当日涨幅排名、买入用信号日
  收盘、卖出用次日 (高+低)/2，可在 4 年窗口上跑。2 年实盘对齐回测已实测
  {实盘对齐收益} / {日K近似收益} ≈ 2.97（见 backtest_tdx_2y_real.py 的校准系数），
  故把日K近似累计收益 ×2.97 作为"精确策略长期形态"的近似外推。

注意：×2.97 是 2 年窗口全局系数，跨年未必严格恒定；2024-07 之前无 5min 数据，
无法逐笔校准，故外推仅作长期形态参考，非决策级数字。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_idle_dual import compound  # noqa: E402
from backtest_t0_today1 import FEE_PCT, run_backtest  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
BACKFILL = CACHE / "backfill_daily_1000.json"
OUT = CACHE / "daily_proxy_4y.json"

# 2 年实盘对齐 / 日K近似 实测校准系数（backtest_tdx_2y_real.py 输出）
CALIB = 2.97


def mdd(trades: list[dict]) -> float:
    eq, peak, m = 1.0, 1.0, 0.0
    for t in sorted(trades, key=lambda x: x["signal_date"]):
        eq *= 1 + t["return_pct"] / 100
        peak = max(peak, eq)
        m = min(m, (eq - peak) / peak * 100)
    return m


def worst_week(trades: list[dict]) -> tuple[str, float]:
    from datetime import datetime
    def iso_week(d: str) -> str:
        y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
        return f"{y}/W{w:02d}"
    wr: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        wr[iso_week(t["signal_date"])].append(t["return_pct"])
    if not wr:
        return "-", 0.0
    wk = min(wr, key=lambda k: compound(wr[k]))
    return wk, compound(wr[wk])


def report(tag: str, res: dict, calib: float | None = None) -> None:
    trades = res["trades"]
    wk, wv = worst_week(trades)
    print(f"\n=== {tag} ===")
    print(f"总计: {res['trade_count']} 笔, 累计 {res['final_equity_pct']:+.2f}%, "
          f"最大回撤 {mdd(trades):.2f}%, 最差周 {wk} {wv:+.2f}%")
    if calib:
        print(f"    ×校准 {calib} → 近似精确策略累计 "
              f"{res['final_equity_pct'] * calib:+.2f}%")
    by_year: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_year[t["signal_date"][:4]].append(t)
    print(f"{'年':<6}{'笔':>4}{'收益':>10}{'回撤':>9}{'最差周':>18}"
          f"{('校准后收益' if calib else ''):>12}")
    for y in sorted(by_year):
        ts = by_year[y]
        wk2, wv2 = worst_week(ts)
        line = (f"{y:<6}{len(ts):>4}"
                f"{compound([t['return_pct'] for t in ts]):+10.2f}%"
                f"{mdd(ts):>9.2f}%{wk2:>13}{wv2:>+8.2f}%")
        if calib:
            line += f"{compound([t['return_pct'] for t in ts]) * calib:>11.2f}%"
        print(line)


def main() -> None:
    etf_list = get_all_t0_etfs()
    codes = [e["code"] for e in etf_list]
    if not BACKFILL.exists():
        raise RuntimeError(f"未找到 {BACKFILL}，请先回填日K")
    bf = json.loads(BACKFILL.read_text(encoding="utf-8"))
    bd = bf["etf_daily"]
    proxy = bf.get("proxy_klines", [])
    print(f">>> 日K池 {len(bd)} 只，窗口 {bd and 'backfill_daily_1000.json'}")

    # 由日K覆盖构建交易日历。权衡：阈值过高(≥90%)窗口只剩2023-11起；
    # 放宽到≥50%(≥53只)可拉回2022-06-15完整4年，53只足够TOP1横截面选股。
    # （2019~2022 虽个别标的有数据，但覆盖<30只，不做选股）
    cover = Counter()
    for c in codes:
        info = bd.get(c)
        if not info:
            continue
        for r in info.get("returns", []):
            cover[r["date"]] += 1
    all_dates = sorted(cover)
    thr = 0.5 * len(codes)
    eval_dates = [d for d in all_dates if cover[d] >= thr]
    print(f">>> 日K覆盖: {eval_dates[0]} ~ {eval_dates[-1]} "
          f"({len(eval_dates)} 交易日, 校准系数 {CALIB})\n")

    res = run_backtest(etf_list, bd, {}, all_dates, eval_dates, FEE_PCT,
                       use_filter=True, daily_proxy=True, proxy_klines=proxy)
    report("日K近似(4年全窗口)", res, CALIB)

    OUT.write_text(json.dumps({
        "window": f"{eval_dates[0]}~{eval_dates[-1]}",
        "calib": CALIB,
        "daily_proxy": res,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n>>> 已落盘 {OUT}")


if __name__ == "__main__":
    main()

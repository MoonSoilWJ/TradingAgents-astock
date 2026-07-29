#!/usr/bin/env python3
"""P0 回填 + 多周期跨 regime 验证。

现实约束（已探明）：
  - 新浪 5min datalen 上限 ~5500 根 ≈ 4~5 个月，无法覆盖 3 年。
  - 新浪日K datalen 上限 ~1000 天 ≈ 4 年，可回填到 2022 年中。

因此本脚本：
  1. 拉全 T+0 池长回溯日K(datalen=1000)，落盘到独立备份文件
     （命名 backfill_daily_1000.json，不干扰现有 pool_*_days100 缓存）。
  2. 用 run_backtest(daily_proxy=True) 做「日K隔夜近似」多周期回测：
       选股 = 当日涨幅动量屏(TOP1, ≥MIN_GAIN)
       买   = 信号日收盘  卖 = 次日 HL2
     *注意*：这是与实盘 14:40 双时点不同的近似，用于跨 regime 方向性判断。
  3. 输出：分年收益/笔数、最差周 Top10、最大回撤、2024 熊市专项。

用法:
    python scripts/_tmp_backfill_multiregime.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    load_market_data,
    run_backtest,
)
from backtest_t0_idle_dual import compound  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"


def iso_week(d: str) -> str:
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return f"W{w:02d}"


def year(d: str) -> str:
    return d[:4]


def max_drawdown(equity_curve: list[float]) -> float:
    peak = -1e9
    mdd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (eq - peak) / peak * 100
            if dd < mdd:
                mdd = dd
    return mdd


def main() -> None:
    etf_list = get_all_t0_etfs()
    print(f">>> 池子: {len(etf_list)} 只 T+0 ETF")

    # 1) 拉长回溯日K（daily_only，datalen=1000 ≈ 4 年）
    print(">>> 拉取日K长回溯 (datalen=1000) ...")
    from backtest_t0_today1 import MIN_GAIN  # noqa: E402
    etf_daily, etf_5min, all_dates, proxy = load_market_data(
        etf_list, lookback=1000, daily_only=True, datalen=1000,
    )
    print(f">>> 日K覆盖: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)} 交易日), "
          f"有效标的 {len(etf_daily)} 只")

    # 落盘独立备份（不影响现有 days100 缓存）
    CACHE.mkdir(parents=True, exist_ok=True)
    backup = CACHE / "backfill_daily_1000.json"
    backup.write_text(json.dumps({
        "etf_daily": etf_daily,
        "all_dates": all_dates,
        "proxy_klines": proxy,
        "data_source": "sina_daily_datalen1000",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "note": "日K长回溯备份；5min因新浪datalen上限无法覆盖3年",
    }, ensure_ascii=False), encoding="utf-8")
    print(f">>> 已落盘日K回填备份: {backup} ({len(etf_daily)} 只)")

    # 2) 多周期日K隔夜近似回测（全历史 eval）
    eval_dates = all_dates
    res = run_backtest(
        etf_list, etf_daily, {}, all_dates, eval_dates,
        fee_pct=FEE_PCT, use_filter=True, daily_proxy=True,
        confirm_time=None, proxy_klines=proxy,
    )
    trades = res["trades"]
    print(f"\n=== 总览 ===")
    print(f"总交易: {len(trades)} 笔 | 累计 {res['final_equity_pct']:+.2f}% | "
          f"跳过 {res['skipped_count']} 天 | 笔均 {res['stats'].get('avg',0):+.2f}% | "
          f"胜率 {res['stats'].get('win_rate',0):.1f}%")

    # 3a) 分年
    by_year: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_year[year(t["signal_date"])].append(t)
    print(f"\n{'年':<6} {'笔':>4} {'年收益':>11} {'最差周':>9} {'日K覆盖'}")
    print("-" * 48)
    yearly_best = {}
    for y in sorted(by_year):
        ts = by_year[y]
        yr = compound([t["return_pct"] for t in ts])
        wr = defaultdict(list)
        for t in ts:
            wr[iso_week(t["signal_date"])].append(t["return_pct"])
        ww = min((compound(v) for v in wr.values()), default=0)
        yearly_best[y] = ww
        print(f"{y:<6} {len(ts):>4} {yr:+11.2f}% {ww:+9.2f}%")
    if not by_year:
        print("(无交易)")

    # 3b) 最差周 Top10
    by_week: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_week[iso_week(t["signal_date"]) + " " + year(t["signal_date"])].append(t)
    # 用年份+周排序需要年份，改用 dict
    by_week2: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        by_week2[(year(t["signal_date"]), iso_week(t["signal_date"]))].append(t)
    wk = []
    for (y, w), ts in by_week2.items():
        wk.append((y, w, compound([t["return_pct"] for t in ts]), ts))
    wk.sort(key=lambda x: x[2])
    print(f"\n=== 最差周 Top10 (日K隔夜近似) ===")
    print(f"{'年/周':<12} {'笔':>4} {'周收益':>9}  主亏标的(类型)")
    print("-" * 64)
    for y, w, comp, ts in wk[:10]:
        worst = sorted(ts, key=lambda t: t["return_pct"])[:3]
        hstr = " ".join(f"{t['etf'][-4:]}({t.get('type','?')}){t['return_pct']:+.2f}"
                        for t in worst)
        print(f"{y+'/'+w:<12} {len(ts):>4} {comp:+9.2f}%  {hstr}")

    # 3c) 最大回撤（按信号日顺序 equity 曲线）
    eq = 1.0
    curve = [eq]
    for t in sorted(trades, key=lambda x: x["signal_date"]):
        eq *= 1 + t["return_pct"] / 100
        curve.append(eq)
    mdd = max_drawdown(curve)
    print(f"\n=== 尾部指标 ===")
    print(f"最大回撤(日K近似): {mdd:.2f}%")
    n_ratio = sum(1 for t in trades if t["return_pct"] < -5)
    print(f"单笔 <-5% 次数: {n_ratio} / {len(trades)} "
          f"({100*n_ratio/max(1,len(trades)):.1f}%)")
    n_neg_week = sum(1 for _, _, c, _ in wk if c < -5)
    print(f"周收益 <-5% 的周数: {n_neg_week} / {len(wk)}")

    # 3d) 2024 熊市专项
    if "2024" in by_year:
        ts = by_year["2024"]
        yr = compound([t["return_pct"] for t in ts])
        wr = defaultdict(list)
        for t in ts:
            wr[iso_week(t["signal_date"])].append(t["return_pct"])
        ww = min((compound(v) for v in wr.values()), default=0)
        print(f"\n=== 2024 熊市专项 ===")
        print(f"2024: {len(ts)} 笔, 年收益 {yr:+.2f}%, 最差周 {ww:+.2f}%")
        # 月度
        bm = defaultdict(list)
        for t in ts:
            bm[t["signal_date"][5:7]].append(t["return_pct"])
        print("  月度: " + " ".join(f"{m}:{compound(v):+.1f}" for m, v in sorted(bm.items())))
    else:
        print("\n=== 2024 熊市专项 ===")
        print("  2024 年无交易（数据覆盖或标的上市原因）")


if __name__ == "__main__":
    main()

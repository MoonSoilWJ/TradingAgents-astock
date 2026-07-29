#!/usr/bin/env python3
"""校准：2026 实盘5min双时点口径 vs 日K隔夜近似，量出偏差系数，反推全仓干的历史真实量级。

- (A) 同区间(2026) 实盘双时点14:40 口径 vs 日K近似，得 收益校准系数 k_rev、回撤校准系数 k_dd
- (B) 读日K回填(全历史)，用系数反推各年"实盘量级"收益/回撤（粗估，标注偏差来源）
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_idle_dual import compound  # noqa: E402
from backtest_t0_idle_pool_search import load_or_fetch  # noqa: E402
from backtest_t0_today1 import FEE_PCT, run_backtest  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
BACKFILL = CACHE / "backfill_daily_1000.json"


def iso_week(d: str) -> str:
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return f"W{w:02d}"


def year(d: str) -> str:
    return d[:4]


def mdd(curve: list[float]) -> float:
    peak = -1e9
    m = 0.0
    for e in curve:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (e - peak) / peak * 100
            if dd < m:
                m = dd
    return m


def equity_curve(trades: list[dict]) -> list[float]:
    eq = 1.0
    c = [eq]
    for t in sorted(trades, key=lambda x: x["signal_date"]):
        eq *= 1 + t["return_pct"] / 100
        c.append(eq)
    return c


def worst_week(trades: list[dict]) -> float:
    wr: dict[tuple[str, str], list[float]] = defaultdict(list)
    for t in trades:
        wr[(year(t["signal_date"]), iso_week(t["signal_date"]))].append(t["return_pct"])
    return min((compound(v) for v in wr.values()), default=0)


def load_5min_cache() -> tuple[dict, list]:
    files = sorted(CACHE.glob("pool_*_days*_allmarket.json"), reverse=True)
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("etf_5min") and len(d["etf_5min"]) >= 50:
                return d["etf_5min"], d.get("proxy_klines", [])
        except Exception:
            continue
    return {}, []


def main() -> None:
    etf_list = get_all_t0_etfs()

    # (A) 2026 同区间校准：同一 etf_daily(回填) + 同一 eval 区间，仅买卖口径不同
    print(">>> 载入日K回填 + 现有5min缓存做同区间校准 ...")
    bf = json.loads(BACKFILL.read_text(encoding="utf-8"))
    bd = bf["etf_daily"]
    etf_5min_2026, proxy = load_5min_cache()
    if not etf_5min_2026:
        print("    无5min缓存，回退拉取 ...")
        _, etf_5min_2026, _, proxy = load_or_fetch(
            etf_list, 100, use_cache=True, write_cache=False, fetch_limit=None)[:4]
    cal_dates = sorted({d for bars in etf_5min_2026.values() for d in bars})
    proxy = proxy or bf.get("proxy_klines", [])
    eval_dates = cal_dates
    print(f"    2026 校准区间: {cal_dates[0]} ~ {cal_dates[-1]} ({len(cal_dates)} 日), "
          f"5min {len(etf_5min_2026)} 只")

    real = run_backtest(etf_list, bd, etf_5min_2026, cal_dates, eval_dates, FEE_PCT,
                        use_filter=True, daily_proxy=False, confirm_time="14:40",
                        proxy_klines=proxy)
    approx = run_backtest(etf_list, bd, {}, cal_dates, eval_dates, FEE_PCT,
                          use_filter=True, daily_proxy=True, proxy_klines=proxy)

    r_real, r_app = real["final_equity_pct"], approx["final_equity_pct"]
    d_real, d_app = mdd(equity_curve(real["trades"])), mdd(equity_curve(approx["trades"]))
    w_real, w_app = worst_week(real["trades"]), worst_week(approx["trades"])
    k_rev = r_real / r_app if r_app else 0.0
    k_dd = d_real / d_app if d_app else 0.0
    print(f"\n=== 2026 同区间校准 ===")
    print(f"{'口径':<16}{'笔':>4}{'收益':>9}{'最大回撤':>10}{'最差周':>9}")
    print("-" * 50)
    print(f"{'实盘5min双时点':<16}{real['trade_count']:>4}{r_real:+9.2f}%{d_real:>10.2f}%{w_real:>9.2f}%")
    print(f"{'日K隔夜近似':<16}{approx['trade_count']:>4}{r_app:+9.2f}%{d_app:>10.2f}%{w_app:>9.2f}%")
    print(f"\n校准系数: 收益 ×{k_rev:.2f}   回撤 ×{k_dd:.2f}")

    # (B) 全历史日K近似 → 反推实盘量级
    print(f"\n>>> 用日K回填做全历史跨regime反推 ...")
    bdates = bf["all_dates"]
    full = run_backtest(etf_list, bd, {}, bdates, bdates, FEE_PCT,
                        use_filter=True, daily_proxy=True,
                        proxy_klines=bf.get("proxy_klines", []))
    print(f"    全历史日K近似: {full['trade_count']} 笔, 累计 "
          f"{full['final_equity_pct']:+.2f}%, 回撤 {mdd(equity_curve(full['trades'])):.2f}%")

    by_year: dict[str, list[dict]] = defaultdict(list)
    for t in full["trades"]:
        by_year[year(t["signal_date"])].append(t)
    print(f"\n=== 全仓干历史真实画像（日K近似 × 校准系数，粗估）===")
    print(f"{'年':<6}{'笔':>4}{'近似收益':>10}{'实盘量级':>11}{'近似回撤':>10}{'实盘量级回撤':>15}")
    print("-" * 62)
    for y in sorted(by_year):
        ts = by_year[y]
        yr = compound([t["return_pct"] for t in ts])
        dd = mdd(equity_curve(ts))
        print(f"{y:<6}{len(ts):>4}{yr:+10.2f}%{yr*k_rev:+11.2f}%{dd:>10.2f}%{dd*k_dd:>15.2f}%")

    # 全样本最坏回撤（实盘量级）
    full_dd_app = mdd(equity_curve(full["trades"]))
    print(f"\n>>> 全仓干最坏情况估计: 全样本回撤 ≈ {full_dd_app*k_dd:.1f}% "
          f"(日K近似 {full_dd_app:.1f}% × 回撤系数 {k_dd:.2f})")
    print("    （注：k_rev/k_dd 仅由2026单年估计，跨年偏差可能不同，量级供参考）")


if __name__ == "__main__":
    main()

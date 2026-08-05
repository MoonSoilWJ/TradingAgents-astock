"""
实验：动量选股加「上升趋势门禁」是否改善震荡年、不伤趋势年
============================================================
方向（用户 2026-08-05）：稀释到防御资产已被证伪(跑输300ETF)；
真正杠杆是『换动量选股质量』——当前入场=当日涨幅Top1≥3%，
震荡年容易被「一日游反弹/死猫跳」骗入次日反转亏钱。

改良：候选除满足 ≥3% 外，还要求该 ETF 当日收盘 > MA(N)（处于短期上升趋势）。
只交易「真突破(在趋势里)」，过滤「噪音尖峰/死猫跳」。

用与 backtest_unified_local / backtest_recent100 完全同一管道：
  B 选股 + 14:40 双时点确认 + 次日 TRIX(5,3)死叉卖/11:05收盘fallback
窗口 2022-11-15~2026-07-31（无偏5min合并 + 日线）。

输出：基线(B) vs 门禁(B+gate) 的逐年收益/笔数/胜率 + 全周期。
"""
import json, sys
from pathlib import Path
from collections import defaultdict

import pandas as pd  # noqa

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa
from backtest_t0_today1 import MIN_GAIN, rank_by_today_gain, passes_gain_filter  # noqa
from backtest_unified_local import (  # noqa
    ALIGNED, PRE2024_FILE, TWOY_FILE, FULL_DAILY,
    get_all_t0_etfs, ALL_DATES, CODES5, START, END, FEE, CONFIRM,
    apply_confirm, etf_daily, etf_5min, etf_list,
)

GATE_MA = 20  # 上升趋势 = 收盘 > MA20


def _ma_close(etf_daily, code, day, n):
    info = etf_daily.get(code)
    if not info:
        return None
    returns = info.get("returns", [])
    idx_map = {r["date"]: i for i, r in enumerate(returns)}
    if day not in idx_map:
        return None
    idx = idx_map[day]
    if idx < n - 1:
        return None
    window = returns[idx - n + 1: idx + 1]
    closes = [r["close"] for r in window if r.get("close")]
    if len(closes) < n:
        return None
    return sum(closes) / len(closes)


def in_uptrend(etf_daily, code, day, n=GATE_MA):
    ma = _ma_close(etf_daily, code, day, n)
    if ma is None:
        return False
    info = etf_daily[code]
    returns = info["returns"]
    idx_map = {r["date"]: i for i, r in enumerate(returns)}
    idx = idx_map[day]
    close = returns[idx]["close"]
    return close > ma


def build_picks_B_gated(eval_dates, etf_list, etf_daily, etf_5min, warmup, n=GATE_MA):
    """B 选股 + 上升趋势门禁：候选需 收盘>MA(n)。"""
    picks = {}
    for i, day in enumerate(eval_dates):
        if i < warmup:
            picks[(SIGNAL_TIME, day)] = None
            continue
        scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, SIGNAL_TIME)
        cands = []
        for g, e in scores:
            if not passes_gain_filter(g):
                continue
            code = e["code"]
            if not in_uptrend(etf_daily, code, day, n):
                continue
            cands.append((g, e))
        if cands:
            g, e = cands[0]
            picks[(SIGNAL_TIME, day)] = (
                e["code"], g, e.get("name") or e.get("etf_name") or e["code"])
        else:
            picks[(SIGNAL_TIME, day)] = None
    return picks


def per_year(trades):
    by = defaultdict(list)
    for t in trades:
        by[t["signal_date"][:4]].append(t["return_pct"])
    out = {}
    for y in sorted(by):
        rs = by[y]
        eq = 1.0
        for x in rs:
            eq *= 1 + x / 100
        out[y] = (round((eq - 1) * 100, 2), len(rs),
                  round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1))
    return out


def run(label, picks):
    picks_cf, n_rej = apply_confirm(picks, etf_daily, etf_5min, CONFIRM)
    res = run_strategy('trix', [d for d in ALL_DATES if START <= d <= END],
                       ALL_DATES, picks_cf, etf_5min, FEE)
    trades = res['trades'] if res else []
    return trades, n_rej


def main():
    test_dates = [d for d in ALL_DATES if START <= d <= END]

    # 基线 B
    from backtest_b_idle_merge import build_picks_B
    b_picks = build_picks_B(test_dates, etf_list, etf_daily, etf_5min, 0)
    b_trades, b_rej = run("B", b_picks)

    # 门禁 B+gate
    g_picks = build_picks_B_gated(test_dates, etf_list, etf_daily, etf_5min, 0)
    g_trades, g_rej = run("B+gate", g_picks)

    print("=" * 70)
    print(f"窗口 {START}~{END} | 入场=B(全市场Top1≥{MIN_GAIN:.0f}%)+确认+TRIX卖")
    print(f"门禁 = 候选额外要求 收盘>MA{GATE_MA}")
    print("=" * 70)

    for label, trades, rej in (("基线 B", b_trades, b_rej),
                               (f"B+gate(MA{GATE_MA})", g_trades, g_rej)):
        eq = 1.0
        for t in sorted(trades, key=lambda x: x["signal_date"]):
            eq *= 1 + t["return_pct"] / 100
        py = per_year(trades)
        print(f"\n### {label}  全周期 {eq*100-100:+.2f}% | 笔数 {len(trades)} | "
              f"确认否决 {rej}天")
        print(f"  {'年':>5} {'收益%':>9} {'笔数':>5} {'胜率%':>7}")
        for y in sorted(py):
            r, n, wr = py[y]
            print(f"  {y:>5} {r:>+9.2f} {n:>5} {wr:>7.1f}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""快速预检: 自动层(新)ETF 的"池子扩大"对 B 策略选股的影响量级。

机制: B 策略 = 每日全市场当日涨幅 Top1(≥3%)。本脚本用【日K收盘涨幅】作为
当日涨幅的代理(口径统一、无需5min), 统计:
  · 原池(103只, codes5覆盖) 每日 Top1 涨幅
  · 自动层(311只新) 每日 Top1 涨幅
  · 自动层抢过 Top1(且≥3%)的天数 → 即"池子扩大会改变选股"的天数

这是量级预检, 不精确(回测B用5min信号价, 此处用日K收盘), 但能直接回答
"新ETF是否经常成为当日最强", 决定要不要花时间 backfill 5min 做精确回测。

用法: python scripts/scan_pool_impact.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents" / "cache" / "t0_5min"
MIN_GAIN = 3.0  # B 策略门槛


def load_codes5() -> set[str]:
    codes = set()
    for f in ("tdx_5min_pre2024.json", "tdx_5min_2y.json"):
        p = CACHE / f
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        codes |= set(d.get("etf_5min", {}).keys())
    return codes


def load_daily(codes: list[str]) -> dict:
    """优先 full_daily(10年); 否则 sina 拉取 post 段(2022-06-15起)。"""
    full = CACHE / "full_daily_2015_2026.json"
    if full.exists():
        d = json.loads(full.read_text(encoding="utf-8"))
        if set(codes) <= set(d.keys()):
            print(f"    日K用 full_daily({len(d)}只)", flush=True)
            return d
    # 回退: sina 拉
    import akshare as ak
    out: dict = {}
    for code in codes:
        try:
            h = ak.fund_etf_hist_sina(
                symbol=("sh" if code[0] in "56" else "sz") + code)
        except Exception:
            continue
        recs = []
        for _, row in h.iterrows():
            d = str(row["date"])[:10]
            if d < "2022-06-15":
                continue
            try:
                recs.append({"date": d, "close": float(row["close"])})
            except Exception:
                continue
        if recs:
            out[code] = recs
    print(f"    日K用 sina 拉取: {len(out)}/{len(codes)} 只", flush=True)
    return out


def main() -> None:
    codes5 = load_codes5()
    allc = get_all_t0_etfs()
    manual = [e["code"] for e in allc if e["code"] in codes5]
    auto = [e["code"] for e in allc if e["code"] not in codes5]
    print(f"原池(有5min): {len(manual)} | 自动层(新): {len(auto)}", flush=True)

    daily = load_daily(manual + auto)
    # 建 code -> {date: close}
    close = {c: {r["date"]: r["close"] for r in recs}
             for c, recs in daily.items()}
    days = sorted({d for m in close.values() for d in m})
    days = [d for d in days if d >= "2022-06-15"]
    print(f"交易日: {len(days)} ({days[0]}~{days[-1]})", flush=True)

    def top1(codes, day):
        best = None
        for c in codes:
            m = close.get(c)
            if not m or day not in m:
                continue
            pc_day = prev_trade_day(days, day)
            pc = m.get(pc_day) if pc_day else None
            if not pc or pc <= 0:
                continue
            g = (m[day] - pc) / pc * 100
            if best is None or g > best[1]:
                best = (c, g)
        return best

    def prev_trade_day(days, day):
        i = days.index(day)
        return days[i - 1] if i > 0 else None

    auto_wins = 0          # 自动层抢 Top1 且 ≥3%
    auto_wins_below = 0    # 自动层抢 Top1 但 <3% (不构成B信号)
    examples = []
    for day in days:
        o = top1(manual, day)
        a = top1(auto, day)
        if a is None:
            continue
        ac, ag = a
        og = o[1] if o else -999
        if ag > og:       # 自动层当日更强
            if ag >= MIN_GAIN:
                auto_wins += 1
                examples.append((day, ac, round(ag, 2),
                                 (o[0] if o else "-"), round(og, 2)))
            else:
                auto_wins_below += 1

    print("\n" + "=" * 70)
    print("  池子扩大影响量级预检 (日K收盘涨幅口径, 近似)")
    print("=" * 70)
    print(f"  自动层抢过 Top1 的天数:")
    print(f"    · 且 ≥3%(会真触发B信号): {auto_wins} 天 / {len(days)} 天")
    print(f"    · 但 <3%(不构成信号):     {auto_wins_below} 天")
    print(f"  占比: {auto_wins/len(days)*100:.1f}% 交易日会因池子扩大而改变B选股")
    if examples:
        print("\n  自动层抢 Top1 的示例(前15):")
        print(f"  {'日期':<12}{'自动层代码':<10}{'自动涨幅':>9}{'原池Top1':<10}{'原池涨幅':>9}")
        for d, c, g, oc, og in sorted(examples)[-15:]:
            print(f"  {d:<12}{c:<10}{g:>+8.2f}%{oc:<10}{og:>+8.2f}%")
    print("\n  结论:")
    if auto_wins == 0:
        print("    · 自动层几乎从不成为当日 Top1 → 池子扩大对B收益影响≈0, 无需backfill")
    elif auto_wins < len(days) * 0.02:
        print("    · 自动层极少抢 Top1 → 池子扩大影响有限, 但建议backfill精确验证")
    else:
        print("    · 自动层频繁抢 Top1 → 池子扩大显著影响选股, 必须backfill 5min精确回测")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""分策略结构检验 — 「上午了结」这个结构在 A / B / R3 三个买入端下是否都成立?

背景: crontab 上 t0_monitor(实盘A) / t0_b_idle_shadow(B) / t0_r3_monitor(R3) 三个策略
      都用 TRIX(5,3) 死叉在 09:40~11:05 卖出。但「上午溢价」是在 B 选股下测出来的,
      不能直接推广 —— 不同买入端选出不同标的, 日内路径形状可能不同。

本脚本对三种买入端分别检验:
  1. 平均日内收益路径(买入14:50 → 次日各时点)
  2. 上午溢价 = 11:05收益 - 14:50收益, 及其分年同号率
  3. 四种卖点(TRIX / 跌破前低 / 固定11:05 / 固定14:50)的净累计收益
  → 只有「上午溢价分年同号率高 + 固定11:05 稳定优于 TRIX」的买入端, 才可改卖点。

用法:
    python3 scripts/structure_per_strategy.py --start 2022-11-15 --end 2026-07-31
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_b_idle_merge import build_picks_B, SIGNAL_TIME  # noqa: E402
from backtest_r3_ab_2022_2026 import build_picks_jq, r3_pool_fn  # noqa: E402
from backtest_t0_etf import bar_time_min, price_at_time  # noqa: E402
from backtest_t0_hybrid_sell import BUY_TIME, SELL_CUTOFF, TRIX_PERIOD, TRIX_SIGNAL_PERIOD  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT, MIN_GAIN, TRIX_MIN_SELL, apply_net_return,
    gain_at_time, time_to_min,
)
from quality_pool import build_picks_hybrid  # noqa: E402
from search_t0_time_combo import bars_until, simulate_exit  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
DENSE = ["tdx_5min_pre2024.json", "tdx_5min_2y.json"]
OUT_DIR = Path.home() / ".tradingagents/cache/t0_5min"
ROLL = 60

AXIS = (list(range(time_to_min("09:35"), time_to_min("11:30") + 1, 5))
        + list(range(time_to_min("13:05"), time_to_min("15:00") + 1, 5)))
KEY_TS = ["09:35", "10:00", "11:05", "11:30", "13:05", "14:00", "14:50", "15:00"]


def path_at(next_bars: list[dict], buy: float) -> dict[int, float]:
    bars = sorted(next_bars, key=bar_time_min)
    out, j, last = {}, 0, None
    for t in AXIS:
        while j < len(bars) and bar_time_min(bars[j]) <= t:
            last = float(bars[j]["close"])
            j += 1
        if last is not None:
            out[t] = (last - buy) / buy * 100
    return out


def sell_trix(buy, day_bars, next_bars, cutoff, min_sell, **_):
    out = simulate_exit(
        "trix0940_cut", buy, day_bars, BUY_TIME, next_bars, cutoff,
        trix_period=TRIX_PERIOD, trix_signal_period=TRIX_SIGNAL_PERIOD,
    )
    return out[0] if isinstance(out, tuple) else out


def sell_prev_low(buy, day_bars, next_bars, cutoff, min_sell, **_):
    lo = time_to_min(min_sell)
    bs = [b for b in bars_until(next_bars, cutoff) if bar_time_min(b) >= lo]
    if not bs:
        return None
    prev = None
    for b in bs:
        l = float(b["low"])
        if prev is not None and l < prev:
            return float(b["close"])
        prev = l
    return float(bs[-1]["close"])


def sell_fixed(buy, day_bars, next_bars, cutoff, min_sell, **_):
    bs = bars_until(next_bars, cutoff)
    return float(bs[-1]["close"]) if bs else None


EXITS = [
    ("trix", "TRIX死叉", sell_trix, SELL_CUTOFF),
    ("prev_low", "跌破前低", sell_prev_low, SELL_CUTOFF),
    ("fixed_1105", "固定11:05", sell_fixed, SELL_CUTOFF),
    ("fixed_1450", "固定14:50", sell_fixed, "14:50"),
]


def apply_confirm(picks, etf_daily, etf_5min, confirm_time, min_gain=MIN_GAIN):
    out, rejected = {}, 0
    for key, val in picks.items():
        if not val:
            out[key] = val
            continue
        g = gain_at_time(etf_daily, etf_5min, val[0], key[1], confirm_time)
        if g is not None and g < min_gain:
            out[key] = None
            rejected += 1
        else:
            out[key] = val
    return out, rejected


def eq_of(rets: list[float]) -> float:
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return (eq - 1) * 100


def analyse(label: str, rows: list[dict], fee: float) -> dict:
    """对一个买入端的交易集合做完整结构检验。"""
    print("\n" + "=" * 100)
    print(f"  【{label}】 {len(rows)} 笔")
    print("=" * 100)

    ts = [time_to_min(t) for t in KEY_TS]
    years = sorted({r["day"][:4] for r in rows})

    # 路径
    print(f"  {'年份':<8}{'笔数':>5}" + "".join(f"{t:>9}" for t in KEY_TS))
    print("  " + "-" * (13 + 9 * len(KEY_TS)))
    path_by_year = {}
    for y in years:
        rs = [r for r in rows if r["day"][:4] == y]
        if len(rs) < 30:
            continue
        vals = []
        line = f"  {y:<8}{len(rs):>5}"
        for t in ts:
            xs = [r["path"][t] for r in rs if t in r["path"]]
            v = sum(xs) / len(xs) if xs else float("nan")
            vals.append(v)
            line += f"{v:>+9.3f}"
        print(line)
        path_by_year[y] = vals

    allv = []
    line = f"  {'全部':<8}{len(rows):>5}"
    for t in ts:
        xs = [r["path"][t] for r in rows if t in r["path"]]
        v = sum(xs) / len(xs) if xs else float("nan")
        allv.append(v)
        line += f"{v:>+9.3f}"
    print("  " + "-" * (13 + 9 * len(KEY_TS)))
    print(line)

    i11, i14 = KEY_TS.index("11:05"), KEY_TS.index("14:50")
    prem_all = allv[i11] - allv[i14]
    pos = sum(1 for v in path_by_year.values() if v[i11] - v[i14] > 0)
    print(f"\n    上午溢价(11:05−14:50): {prem_all:+.3f}pp | 分年同号 {pos}/{len(path_by_year)}")

    # 滚动恒正率
    diffs = sorted(
        (r["day"], r["path"][time_to_min("11:05")] - r["path"][time_to_min("14:50")])
        for r in rows
        if time_to_min("11:05") in r["path"] and time_to_min("14:50") in r["path"]
    )
    roll = [(diffs[i][0], sum(d for _, d in diffs[i:i + ROLL]) / ROLL)
            for i in range(len(diffs) - ROLL + 1)]
    pos_rate = (sum(1 for _, v in roll if v > 0) / len(roll) * 100) if roll else 0
    n = len(roll)
    if n > 2:
        xm, ym = (n - 1) / 2, sum(v for _, v in roll) / n
        slope = (sum((i - xm) * (v - ym) for i, (_, v) in enumerate(roll))
                 / sum((i - xm) ** 2 for i in range(n)))
    else:
        slope = 0.0
    print(f"    滚动{ROLL}笔恒正率: {pos_rate:.0f}% | 趋势斜率 {slope:+.4f}pp"
          f"{'  (无衰减)' if slope >= -0.01 else '  ⚠ 有衰减'}")

    # 卖点对比
    print(f"\n  {'卖点':<14}{'净累计收益':>12}{'vs TRIX':>12}{'胜率':>8}{'均笔':>9}")
    print("  " + "-" * 56)
    res = {}
    base = None
    for key, lb, fn, cut in EXITS:
        rets = []
        for r in rows:
            sp = fn(r["buy"], r["day_bars"], r["next_bars"], cut, TRIX_MIN_SELL)
            if sp and sp > 0:
                rets.append(apply_net_return(r["buy"], sp, fee))
        e = eq_of(rets) if rets else 0.0
        win = sum(1 for x in rets if x > 0) / len(rets) * 100 if rets else 0
        avg = sum(rets) / len(rets) if rets else 0
        if key == "trix":
            base = e
        res[key] = round(e, 2)
        d = "" if base is None or key == "trix" else f"{e - base:+.1f}pp"
        print(f"  {lb:<14}{e:>+11.2f}%{d:>12}{win:>7.1f}%{avg:>+8.3f}%")

    print(f"\n    → 判定: ", end="")
    ok_prem = pos == len(path_by_year) and len(path_by_year) >= 2
    ok_11 = res.get("fixed_1105", 0) > res.get("trix", 0)
    if ok_prem and ok_11:
        print("结构成立 ✅ (上午溢价分年同号 且 固定11:05 > TRIX) → 可改")
    elif ok_11 and not ok_prem:
        print("部分成立 ⚠ (固定11:05 更优, 但上午溢价分年不一致) → 谨慎")
    else:
        print("结构不成立 ❌ → 维持 TRIX, 不要改")

    return {
        "trades": len(rows), "key_times": KEY_TS,
        "path_all": [round(v, 4) for v in allv],
        "path_by_year": {y: [round(v, 4) for v in vs] for y, vs in path_by_year.items()},
        "am_premium_pp": round(prem_all, 4),
        "year_same_sign": f"{pos}/{len(path_by_year)}",
        "roll_positive_rate": round(pos_rate, 1),
        "roll_slope": round(slope, 5),
        "exits": res,
        "verdict": ("OK" if (ok_prem and ok_11) else ("PARTIAL" if ok_11 else "FAIL")),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="分策略结构检验 A/B/R3")
    ap.add_argument("--start", default="2022-11-15")
    ap.add_argument("--end", default="2026-07-31")
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--fee", type=float, default=FEE_PCT)
    ap.add_argument("--confirm-time", default="14:40")
    ap.add_argument("--only", default="", help="只跑某策略: A / B / R3")
    args = ap.parse_args()

    print("载入缓存 ...", flush=True)
    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]
    proxy = cache["proxy_klines"]

    etf_5min: dict = {}
    for name in DENSE:
        p = OUT_DIR / name
        print(f"  载入5min: {p.name} ...", flush=True)
        for c, days in json.loads(p.read_text(encoding="utf-8"))["etf_5min"].items():
            etf_5min.setdefault(c, {}).update(days)

    cnt: Counter = Counter()
    for _c, days in etf_5min.items():
        for d, bars in days.items():
            if bars:
                cnt[d] += 1
    five_dates = sorted(d for d, n in cnt.items() if n >= 20)
    all_dates = sorted(set(all_dates) & set(five_dates))
    all_dates = [d for d in all_dates if args.start <= d <= args.end]

    etf_list = [e for e in get_all_t0_etfs() if e["code"] in set(etf_5min)]
    print(f"\n窗口 {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}天) | "
          f"候选池 {len(etf_list)} 只 | 确认 {args.confirm_time}\n")

    def build_rows(picks, sig_time) -> list[dict]:
        rows = []
        for day in all_dates:
            picked = picks.get((sig_time, day))
            if not picked:
                continue
            code, gain, name = picked
            db = etf_5min.get(code, {}).get(day, [])
            buy = price_at_time(db, BUY_TIME)
            if not buy or buy <= 0 or day not in all_dates:
                continue
            i = all_dates.index(day)
            if i + 1 >= len(all_dates):
                continue
            nb = etf_5min.get(code, {}).get(all_dates[i + 1], [])
            if not nb:
                continue
            r = {"day": day, "code": code, "name": name, "gain": gain,
                 "buy": buy, "day_bars": db, "next_bars": nb}
            r["path"] = path_at(nb, buy)
            rows.append(r)
        return rows

    out: dict = {}
    only = args.only.upper()

    if only in ("", "B"):
        print(">>> B 选股(全市场Top1≥3%) ...", flush=True)
        pb = build_picks_B(all_dates, etf_list, etf_daily, etf_5min, 0)
        pb, _ = apply_confirm(pb, etf_daily, etf_5min, args.confirm_time)
        out["B"] = analyse("B 全市场Top1 (SHADOW)", build_rows(pb, SIGNAL_TIME), args.fee)

    if only in ("", "A"):
        print(">>> A 选股(hybrid-A 滚动优质池, 较慢) ...", flush=True)
        pa = build_picks_hybrid(all_dates, etf_list, etf_daily, etf_5min,
                                all_dates, proxy, lookback=30, warmup=0)
        pa, _ = apply_confirm(pa, etf_daily, etf_5min, args.confirm_time)
        out["A"] = analyse("A hybrid-A (实盘)", build_rows(pa, SIGNAL_TIME), args.fee)

    if only in ("", "R3"):
        print(">>> R3 选股(月度轮动池) ...", flush=True)
        # gate_ma=1 近似 canonical R3「无MA20门禁」: 候选本就要求当日≥3%, 等价于通过
        pr = build_picks_jq(all_dates, [], etf_daily, etf_5min, all_dates, proxy,
                            lookback=30, topn=25, signal_time="14:45",
                            gate_ma=1, pool_fn=r3_pool_fn)
        pr, _ = apply_confirm(pr, etf_daily, etf_5min, args.confirm_time)
        out["R3"] = analyse("R3 月度轮动池 (SHADOW)",
                            build_rows(pr, "14:45"), args.fee)

    # 汇总
    print("\n" + "=" * 100)
    print("  【汇总】三个买入端的结构检验")
    print("=" * 100)
    print(f"  {'买入端':<26}{'笔数':>6}{'上午溢价':>11}{'分年同号':>10}"
          f"{'滚动恒正':>10}{'TRIX':>11}{'固定11:05':>11}{'判定':>9}")
    print("  " + "-" * 96)
    for k in ("A", "B", "R3"):
        d = out.get(k)
        if not d:
            continue
        print(f"  {k + ' 买入端':<26}{d['trades']:>6}{d['am_premium_pp']:>+10.3f}pp"
              f"{d['year_same_sign']:>10}{d['roll_positive_rate']:>9.0f}%"
              f"{d['exits']['trix']:>+10.2f}%{d['exits']['fixed_1105']:>+10.2f}%"
              f"{d['verdict']:>9}")
    print("=" * 100)

    p = OUT_DIR / f"structure_per_strategy_{args.start}_{args.end}.json"
    p.write_text(json.dumps({"window": f"{all_dates[0]}~{all_dates[-1]}",
                             "confirm": args.confirm_time, "strategies": out},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n落盘: {p}")


if __name__ == "__main__":
    main()

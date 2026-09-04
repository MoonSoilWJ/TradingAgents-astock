#!/usr/bin/env python3
"""结构的机制指纹检验 — 回答「怎么判断这是结构而不是另一种过拟合」。

指标与结构的关键区别: 指标只有「历史相关性」, 结构应有「机制指纹」——
即在价格路径上留下可观测、可归因、跨子样本一致的形状。

本脚本做三个检验:

[A] 跨子样本一致性: 同一结论在 逐年 / 逐ETF类别 / 逐信号强度 分组下是否同号。
    指标过拟合的典型症状 = 子样本里翻转; 结构应跨子样本稳定。

[B] 机制指纹(日内收益路径): 每笔交易从买入(14:50)到次日各 5min 时点的平均累计
    收益曲线。若「上午冲高、下午回吐」的形状每年复现, 则 11:05 卖优于 14:50 卖
    有机制解释, 而非数据拟合。

[C] 结构存续监控: 滚动 N 笔的 (11:05收益 - 14:50收益) 溢价, 看是否恒为正、
    有无衰减趋势。这是实盘上判断结构是否还活着的可执行指标。

用法:
    python3 scripts/structure_fingerprint.py --start 2022-11-15 --recent 2000
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
from backtest_t0_etf import bar_time_min, price_at_time  # noqa: E402
from backtest_t0_hybrid_sell import BUY_TIME, SELL_CUTOFF, TRIX_PERIOD, TRIX_SIGNAL_PERIOD  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT, MIN_GAIN, TRIX_MIN_SELL, apply_net_return,
    gain_at_time, time_to_min,
)
from backtest_top1 import _calc_stats  # noqa: E402
from search_t0_time_combo import bars_until, simulate_exit  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
DENSE = "tdx_5min_pre2024.json,tdx_5min_2y.json"
OUT_DIR = Path.home() / ".tradingagents/cache/t0_5min"

ROLL = 60  # 滚动窗口笔数


def t2h(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


# 次日 5min 时间轴
AXIS = (list(range(time_to_min("09:35"), time_to_min("11:30") + 1, 5))
        + list(range(time_to_min("13:05"), time_to_min("15:00") + 1, 5)))
KEY_TS = ["09:35", "09:40", "10:00", "10:30", "11:05", "11:30",
          "13:05", "14:00", "14:50", "15:00"]


def path_at(next_bars: list[dict], buy: float) -> dict[int, float]:
    """每个 5min 时点上的累计毛收益%(前向填充)。"""
    bars = sorted(next_bars, key=bar_time_min)
    out: dict[int, float] = {}
    j, last = 0, None
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


def _window(next_bars, cutoff, min_sell):
    lo = time_to_min(min_sell)
    return [b for b in bars_until(next_bars, cutoff) if bar_time_min(b) >= lo]


def sell_fixed(buy, day_bars, next_bars, cutoff, min_sell, **_):
    bs = bars_until(next_bars, cutoff)
    return float(bs[-1]["close"]) if bs else None


EXITS = [
    ("trix", "TRIX死叉[指标]", sell_trix, SELL_CUTOFF),
    ("prev_low", "跌破前低[价格行为]", sell_prev_low, SELL_CUTOFF),
    ("fixed_1105", "固定11:05[纯时间]", sell_fixed, SELL_CUTOFF),
    ("fixed_1450", "固定14:50[纯时间]", sell_fixed, "14:50"),
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


def main() -> None:
    ap = argparse.ArgumentParser(description="结构机制指纹检验")
    ap.add_argument("--recent", type=int, default=2000)
    ap.add_argument("--start", type=str, default="2022-11-15")
    ap.add_argument("--cache", type=str, default=str(CACHE))
    ap.add_argument("--five-min", type=str, default=DENSE)
    ap.add_argument("--fee", type=float, default=FEE_PCT)
    ap.add_argument("--confirm-time", type=str, default="14:40")
    ap.add_argument("--tag", type=str, default="")
    args = ap.parse_args()

    print("载入缓存 ...", flush=True)
    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]

    etf_5min: dict = {}
    for part in args.five_min.split(","):
        part = part.strip()
        if not part:
            continue
        p = Path(part)
        path = p if p.is_absolute() else OUT_DIR / p.name
        print(f"  载入5min: {path.name} ...", flush=True)
        for c, days in json.loads(path.read_text(encoding="utf-8"))["etf_5min"].items():
            etf_5min.setdefault(c, {}).update(days)

    cnt: Counter = Counter()
    for _c, days in etf_5min.items():
        for d, bars in days.items():
            if bars:
                cnt[d] += 1
    five_dates = sorted(d for d, n in cnt.items() if n >= 20)
    all_dates = sorted(set(all_dates) & set(five_dates))
    if args.start:
        all_dates = [d for d in all_dates if d >= args.start]

    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    type_of = {e["code"]: e.get("type_name") or "?" for e in etf_list}
    test_dates = all_dates[-args.recent:] if args.recent < len(all_dates) else all_dates

    print(f"\n=== 结构机制指纹 | {test_dates[0]} ~ {test_dates[-1]} ({len(test_dates)}天) ===")

    picks = build_picks_B(test_dates, etf_list, etf_daily, etf_5min, 0)
    picks = {k: v for k, v in picks.items() if k[1] in set(test_dates)}
    if args.confirm_time.lower() != "none":
        picks, n_rej = apply_confirm(picks, etf_daily, etf_5min, args.confirm_time)
        print(f"  双时点确认 {args.confirm_time}: 否决 {n_rej} 天")

    rows = []
    for day in test_dates:
        picked = picks.get((SIGNAL_TIME, day))
        if not picked:
            continue
        code, gain, name = picked
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy = price_at_time(day_bars, BUY_TIME)
        if not buy or buy <= 0 or day not in all_dates:
            continue
        i = all_dates.index(day)
        if i + 1 >= len(all_dates):
            continue
        nd = all_dates[i + 1]
        nb = etf_5min.get(code, {}).get(nd, [])
        if not nb:
            continue
        rows.append({"day": day, "code": code, "name": name, "gain": gain,
                     "buy": buy, "day_bars": day_bars, "next_bars": nb,
                     "year": day[:4], "type": type_of.get(code, "?")})
    print(f"  可交易信号 {len(rows)} 笔\n")

    # 预计算路径
    for r in rows:
        r["path"] = path_at(r["next_bars"], r["buy"])

    # ── [A] 跨子样本一致性 ──
    print("=" * 96)
    print("  [A] 跨子样本一致性 — 指标过拟合会在子样本翻转, 结构应稳定同号")
    print("=" * 96)

    def group_report(title: str, keyfn, min_n: int = 20):
        print(f"\n  ▸ 按 {title} 分组 (每组净累计收益%, n>={min_n})")
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            groups[keyfn(r)].append(r)
        hdr = f"  {'分组':<14}{'笔数':>5}"
        for _, lb, _, _ in EXITS:
            hdr += f"{lb:>20}"
        print(hdr)
        print("  " + "─" * 90)
        summary: dict[str, dict] = {}
        for g in sorted(groups):
            rs = groups[g]
            if len(rs) < min_n:
                continue
            line = f"  {str(g):<14}{len(rs):>5}"
            summary[g] = {}
            for key, _, fn, cut in EXITS:
                rets = []
                for r in rs:
                    sp = fn(r["buy"], r["day_bars"], r["next_bars"], cut, TRIX_MIN_SELL)
                    if sp and sp > 0:
                        rets.append(apply_net_return(r["buy"], sp, args.fee))
                e = eq_of(rets) if rets else 0.0
                summary[g][key] = round(e, 2)
                line += f"{e:>+19.2f}%"
            print(line)
        # 一致性判定
        wins = Counter()
        for g, d in summary.items():
            if d:
                wins[max(d, key=d.get)] += 1
        print(f"     → 各分组最优卖点: {dict(wins)}")
        return summary

    by_year = group_report("年份", lambda r: r["year"], 30)
    by_type = group_report("ETF类别", lambda r: r["type"], 25)

    def gain_bucket(r):
        g = r["gain"]
        return "3~4%" if g < 4 else ("4~5%" if g < 5 else "5%+")
    by_gain = group_report("信号强度", gain_bucket, 25)

    # ── [B] 机制指纹: 日内收益路径 ──
    print("\n" + "=" * 96)
    print("  [B] 机制指纹 — 平均累计收益路径%(毛收益, 相对 14:50 买入价)")
    print("      若「上午冲高 → 下午回吐」形状每年复现 = 有机制解释, 非拟合")
    print("=" * 96)

    years = sorted({r["year"] for r in rows})
    ts = [time_to_min(t) for t in KEY_TS]
    print(f"  {'年份':<8}{'笔数':>5}" + "".join(f"{t:>9}" for t in KEY_TS))
    print("  " + "─" * (13 + 9 * len(KEY_TS)))
    path_by_year = {}
    for y in years:
        rs = [r for r in rows if r["year"] == y]
        if len(rs) < 30:
            continue
        line = f"  {y:<8}{len(rs):>5}"
        vals = []
        for t in ts:
            xs = [r["path"][t] for r in rs if t in r["path"]]
            v = sum(xs) / len(xs) if xs else float("nan")
            vals.append(v)
            line += f"{v:>+9.3f}"
        print(line)
        path_by_year[y] = vals

    # 全样本路径
    allv = []
    line = f"  {'全部':<8}{len(rows):>5}"
    for t in ts:
        xs = [r["path"][t] for r in rows if t in r["path"]]
        v = sum(xs) / len(xs) if xs else float("nan")
        allv.append(v)
        line += f"{v:>+9.3f}"
    print("  " + "-" * (13 + 9 * len(KEY_TS)))
    print(line)

    i_1105 = KEY_TS.index("11:05")
    i_1450 = KEY_TS.index("14:50")
    i_0935 = KEY_TS.index("09:35")
    i_1130 = KEY_TS.index("11:30")
    print(f"\n    隔夜跳空(09:35)      : {allv[i_0935]:+.3f}%")
    print(f"    上午了结(11:05)      : {allv[i_1105]:+.3f}%")
    print(f"    上午收盘(11:30)      : {allv[i_1130]:+.3f}%")
    print(f"    尾盘了结(14:50)      : {allv[i_1450]:+.3f}%")
    print(f"    ★ 上午溢价(11:05-14:50): {allv[i_1105] - allv[i_1450]:+.3f}pp "
          f"— 这就是「必须上午了结」的机制来源")
    pos_yrs = sum(1 for y, v in path_by_year.items() if v[i_1105] - v[i_1450] > 0)
    print(f"    ★ 分年同号: {pos_yrs}/{len(path_by_year)} 年上午溢价为"
          f"{'正' if pos_yrs == len(path_by_year) else '不一致'}")

    # ── [C] 结构存续监控 ──
    print("\n" + "=" * 96)
    print(f"  [C] 结构存续监控 — 滚动 {ROLL} 笔的上午溢价(11:05收益 - 14:50收益, 毛收益pp)")
    print("      实盘用法: 这个数跌破 0 并持续 = 结构可能失效, 需重新审视")
    print("=" * 96)

    diffs = []
    for r in rows:
        p = r["path"]
        a = p.get(time_to_min("11:05"))
        b = p.get(time_to_min("14:50"))
        if a is not None and b is not None:
            diffs.append((r["day"], a - b))
    diffs.sort()
    roll = []
    for i in range(len(diffs) - ROLL + 1):
        seg = [d for _, d in diffs[i:i + ROLL]]
        roll.append((diffs[i][0], diffs[i + ROLL - 1][0], sum(seg) / len(seg)))
    if roll:
        step = max(1, len(roll) // 12)
        print(f"  {'窗口起':<12}{'窗口止':<12}{'滚动上午溢价':>14}")
        print("  " + "-" * 40)
        for i in range(0, len(roll), step):
            a, b, v = roll[i]
            print(f"  {a:<12}{b:<12}{v:>+13.3f}pp")
        a, b, v = roll[-1]
        print(f"  {a:<12}{b:<12}{v:>+13.3f}pp   (最新)")
        vals = [v for _, _, v in roll]
        neg = sum(1 for v in vals if v <= 0)
        print(f"\n    ★ 滚动窗口恒正率: {(len(vals) - neg) / len(vals) * 100:.0f}% "
              f"({len(vals) - neg}/{len(vals)})")
        # 线性趋势
        n = len(vals)
        xm = (n - 1) / 2
        ym = sum(vals) / n
        num = sum((i - xm) * (v - ym) for i, v in enumerate(vals))
        den = sum((i - xm) ** 2 for i in range(n))
        slope = num / den if den else 0
        print(f"    ★ 衰减趋势(每窗口斜率): {slope:+.4f}pp  "
              f"{'→ 结构未见衰减' if slope >= -0.01 else '→ 有衰减迹象, 警惕'}")

    tag = args.tag or f"{test_dates[0]}_{test_dates[-1]}"
    out = OUT_DIR / f"structure_fingerprint_{tag}.json"
    out.write_text(json.dumps({
        "window": {"start": test_dates[0], "end": test_dates[-1], "trades": len(rows)},
        "by_year": by_year, "by_type": by_type, "by_gain": by_gain,
        "key_times": KEY_TS,
        "path_all": [round(v, 4) for v in allv],
        "path_by_year": {y: [round(v, 4) for v in vs] for y, vs in path_by_year.items()},
        "am_premium_all_pp": round(allv[i_1105] - allv[i_1450], 4),
        "roll_am_premium": [{"from": a, "to": b, "v": round(v, 4)} for a, b, v in roll],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  落盘: {out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""卖点指标替换实验 — 检验 edge 到底在「结构」还是「指标」。

★ 实验设计(唯一变量原则):
    固定不变 = B 选股(全市场 Top1, ≥3%) + 14:40 双时点确认 + 14:50 买入 + 费率万3
    唯一变量 = 次日用什么触发器卖出

    1 trix          TRIX(5,3) 死叉                    ← 当前 SHADOW 现状(指标族: 三重平滑动量)
    2 ma_cross      MA5 下穿 MA10                     ← 完全不同指标族(简单均线)
    3 prev_low      跌破前一根 5minK 低点              ← 纯价格行为, 无指标
    4 first_down    第一根收盘下跌的 5minK              ← 纯价格行为, 无指标
    5 fixed_1105    固定 11:05 卖                      ← 纯时间, 无指标
    6 fixed_1450    固定次日 14:50 卖                  ← 纯时间, 吃满全天
    7 random        窗口内随机一根 K 卖                ← 安慰剂对照(N 次取中位数)

★ 判据:
    若 1~4 收益量级相近          → edge 不依赖具体指标(指标只是执行器)
    若 5/6 也赚钱甚至更好        → edge 主要来自持仓时间结构
    若 7 也赚钱                  → edge 几乎全在买入端(尾盘选强 + 隔夜)

用法:
    python3 scripts/backtest_exit_indicator_swap.py --recent 390
    python3 scripts/backtest_exit_indicator_swap.py --recent 900 --tag full4y
"""
from __future__ import annotations

import argparse
import json
import random
import sys
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

RANDOM_ROUNDS = 500


# ── 卖出触发器 ────────────────────────────────────────────────────────────────
# 约定: 返回 (sell_price, bar)。成交价一律取「触发那根 K 的收盘价」= 保守口径
# (不使用 high/low 穿价的乐观假设, 避免 hybrid 式的不可兑现收益)。

def _window(next_bars: list[dict], cutoff: str, min_sell: str) -> list[dict]:
    lo = time_to_min(min_sell)
    return [b for b in bars_until(next_bars, cutoff) if bar_time_min(b) >= lo]


def sell_trix(buy, day_bars, next_bars, cutoff, min_sell, **_):
    """现状基线: TRIX(5,3) 死叉, 窗口 09:40~11:05, 未触发则 11:05 收盘 fallback。"""
    out = simulate_exit(
        "trix0940_cut", buy, day_bars, BUY_TIME, next_bars, cutoff,
        trix_period=TRIX_PERIOD, trix_signal_period=TRIX_SIGNAL_PERIOD,
    )
    # search_t0_time_combo.simulate_exit 返回 (price, reason, timing)
    return out[0] if isinstance(out, tuple) else out


def sell_ma_cross(buy, day_bars, next_bars, cutoff, min_sell, fast=5, slow=10, **_):
    """MA5 下穿 MA10。用买入当日 14:50 前的收盘序列做 warmup(无前视)。"""
    bs = _window(next_bars, cutoff, min_sell)
    if not bs:
        return None
    seq = [float(b["close"]) for b in bars_until(day_bars, BUY_TIME)]
    prev = None
    for b in bs:
        seq.append(float(b["close"]))
        if len(seq) >= slow:
            diff = sum(seq[-fast:]) / fast - sum(seq[-slow:]) / slow
            if prev is not None and prev >= 0 and diff < 0:
                return float(b["close"])
            prev = diff
    return float(bs[-1]["close"])


def sell_prev_low(buy, day_bars, next_bars, cutoff, min_sell, **_):
    """跌破前一根 5minK 的低点 → 按该根收盘价成交(保守)。"""
    bs = _window(next_bars, cutoff, min_sell)
    if not bs:
        return None
    prev = None
    for b in bs:
        lo = float(b["low"])
        if prev is not None and lo < prev:
            return float(b["close"])
        prev = lo
    return float(bs[-1]["close"])


def sell_first_down(buy, day_bars, next_bars, cutoff, min_sell, **_):
    """第一根收盘下跌的 5minK。"""
    bs = _window(next_bars, cutoff, min_sell)
    if not bs:
        return None
    prev = None
    for b in bs:
        c = float(b["close"])
        if prev is not None and c < prev:
            return c
        prev = c
    return float(bs[-1]["close"])


def sell_fixed_1105(buy, day_bars, next_bars, cutoff, min_sell, **_):
    bs = bars_until(next_bars, cutoff)
    return float(bs[-1]["close"]) if bs else None


def sell_fixed_1450(buy, day_bars, next_bars, cutoff, min_sell, **_):
    bs = bars_until(next_bars, "14:50")
    return float(bs[-1]["close"]) if bs else None


def sell_random(buy, day_bars, next_bars, cutoff, min_sell, rng=None, **_):
    """安慰剂: 窗口内随机一根 K 的收盘价卖出。"""
    bs = _window(next_bars, cutoff, min_sell)
    if not bs:
        return None
    return float(rng.choice(bs)["close"])


EXITS = [
    ("trix", "TRIX(5,3)死叉 [指标]", sell_trix, SELL_CUTOFF),
    ("ma_cross", "MA5下穿MA10 [指标]", sell_ma_cross, SELL_CUTOFF),
    ("prev_low", "跌破前根5minK低点 [价格行为]", sell_prev_low, SELL_CUTOFF),
    ("first_down", "首根下跌K [价格行为]", sell_first_down, SELL_CUTOFF),
    ("fixed_1105", "固定11:05卖 [纯时间]", sell_fixed_1105, SELL_CUTOFF),
    ("fixed_1450", "固定次日14:50卖 [纯时间]", sell_fixed_1450, "14:50"),
    ("random", f"窗口内随机卖 [安慰剂×{RANDOM_ROUNDS}]", sell_random, SELL_CUTOFF),
]


def apply_confirm(picks, etf_daily, etf_5min, confirm_time, min_gain=MIN_GAIN):
    """双时点确认(复制自 backtest_recent100_live_vs_b_idle)。"""
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


def equity_curve(rets: list[float]) -> tuple[float, float]:
    eq = cur = peak = 1.0
    mdd = 0.0
    for r in rets:
        eq *= 1 + r / 100
        cur *= 1 + r / 100
        peak = max(peak, cur)
        mdd = min(mdd, (cur - peak) / peak * 100)
    return (eq - 1) * 100, mdd


def main() -> None:
    ap = argparse.ArgumentParser(description="卖点指标替换实验")
    ap.add_argument("--recent", type=int, default=390)
    ap.add_argument("--start", type=str, default="",
                    help="起始交易日(如 2022-11-15); 空=不限制。用于剔除 5min 覆盖不全的早期段")
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
        path = p if p.is_absolute() else Path.home() / ".tradingagents/cache/t0_5min" / p.name
        print(f"  载入5min: {path.name} ...", flush=True)
        dense = json.loads(path.read_text(encoding="utf-8"))["etf_5min"]
        for c, days in dense.items():
            etf_5min.setdefault(c, {}).update(days)

    # 只用「真实交易日 ∩ 当日确有 5min 数据」的日期: 避免 next_day 落在无数据日导致丢笔
    from collections import Counter
    cnt: Counter = Counter()
    for _c, days in etf_5min.items():
        for d, bars in days.items():
            if bars:
                cnt[d] += 1
    five_dates = sorted(d for d, n in cnt.items() if n >= 20)
    all_dates = sorted(set(all_dates) & set(five_dates))
    print(f"  合并5min {len(etf_5min)} 只, 有效交易日 {five_dates[0]}~{five_dates[-1]}, "
          f"{len(all_dates)} 天")

    if args.start:
        all_dates = [d for d in all_dates if d >= args.start]
        print(f"  起始过滤 >= {args.start}: 剩余 {len(all_dates)} 天")

    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    N = args.recent
    test_dates = all_dates[-N:] if N < len(all_dates) else all_dates
    test_set = set(test_dates)

    print(f"\n=== 卖点指标替换实验 | {test_dates[0]} ~ {test_dates[-1]} ({len(test_dates)}天) ===")
    print(f"    候选池 {len(etf_list)} 只 | 费率 {args.fee}% | 买点 {BUY_TIME} | 选股 B+{args.confirm_time}确认\n")

    picks_b = build_picks_B(test_dates, etf_list, etf_daily, etf_5min, 0)
    picks_b = {k: v for k, v in picks_b.items() if k[1] in test_set}
    if args.confirm_time.lower() != "none":
        picks_b, n_rej = apply_confirm(picks_b, etf_daily, etf_5min, args.confirm_time)
        print(f"  双时点确认 {args.confirm_time} (≥{MIN_GAIN:.0f}%): 否决 {n_rej} 天")

    # ── 逐日取买卖点 ──
    rows: list[dict] = []
    for day in test_dates:
        picked = picks_b.get((SIGNAL_TIME, day))
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
        next_day = all_dates[i + 1]
        next_bars = etf_5min.get(code, {}).get(next_day, [])
        if not next_bars:
            continue
        rows.append({
            "day": day, "next_day": next_day, "code": code, "name": name,
            "gain": gain, "buy": buy, "day_bars": day_bars, "next_bars": next_bars,
        })

    print(f"  可交易信号 {len(rows)} 笔\n")

    rng = random.Random(20260903)
    results = {}
    for key, label, fn, cutoff in EXITS:
        if key == "random":
            # 安慰剂: 重复 RANDOM_ROUNDS 次, 每次得到一条完整权益曲线
            curve_eq, curve_mdd, avg_win, avg_ret = [], [], [], []
            for _ in range(RANDOM_ROUNDS):
                rets = []
                for r in rows:
                    sp = sell_random(r["buy"], r["day_bars"], r["next_bars"],
                                     cutoff, TRIX_MIN_SELL, rng=rng)
                    if sp and sp > 0:
                        rets.append(apply_net_return(r["buy"], sp, args.fee))
                eq, mdd = equity_curve(rets)
                curve_eq.append(eq)
                curve_mdd.append(mdd)
                avg_win.append(sum(1 for x in rets if x > 0) / len(rets) * 100)
                avg_ret.append(sum(rets) / len(rets))
            curve_eq.sort()
            med_eq = curve_eq[RANDOM_ROUNDS // 2]
            lo_eq, hi_eq = curve_eq[int(RANDOM_ROUNDS * 0.05)], curve_eq[int(RANDOM_ROUNDS * 0.95)]
            results[key] = {
                "label": label, "trades": len(rows),
                "equity_pct": round(med_eq, 2),
                "equity_p5": round(lo_eq, 2), "equity_p95": round(hi_eq, 2),
                "win_rate": round(sum(avg_win) / len(avg_win), 1),
                "avg": round(sum(avg_ret) / len(avg_ret), 3),
                "mdd": round(sum(curve_mdd) / len(curve_mdd), 1),
                "note": "随机卖价, 中位数 + 5%/95% 分位",
            }
        else:
            rets = []
            for r in rows:
                sp = fn(r["buy"], r["day_bars"], r["next_bars"], cutoff, TRIX_MIN_SELL)
                if sp and sp > 0:
                    rets.append(apply_net_return(r["buy"], sp, args.fee))
            if not rets:
                continue
            eq, mdd = equity_curve(rets)
            st = _calc_stats(rets)
            results[key] = {
                "label": label, "trades": len(rets),
                "equity_pct": round(eq, 2),
                "win_rate": round(st.get("win_rate", 0), 1),
                "avg": round(st.get("avg", 0), 3),
                "mdd": round(mdd, 1),
            }

    # ── 输出 ──
    print("=" * 104)
    print(f"  {'卖点方案':<34} {'笔数':>5} {'累计收益':>11} {'胜率':>7} {'均笔':>8} {'回撤':>8}")
    print("  " + "─" * 100)
    base = results.get("trix", {}).get("equity_pct", 0.0)
    for key, _, _, _ in EXITS:
        r = results.get(key)
        if not r:
            continue
        extra = ""
        if key == "random":
            extra = f"  (5%~95%: {r['equity_p5']:+.1f}% ~ {r['equity_p95']:+.1f}%)"
        diff = ""
        if key != "trix" and base:
            diff = f"  vs TRIX {r['equity_pct'] - base:+.1f}pp"
        print(f"  {r['label']:<34} {r['trades']:>5} {r['equity_pct']:>+10.2f}% "
              f"{r['win_rate']:>6.1f}% {r['avg']:>+7.3f}% {r['mdd']:>7.1f}%{extra}{diff}")
    print("=" * 104)

    print("\n  【判据解读】")
    plain = [results[k]["equity_pct"] for k in ("trix", "ma_cross", "prev_low", "first_down") if k in results]
    if plain:
        print(f"    指标/价格行为类(4种): {min(plain):+.1f}% ~ {max(plain):+.1f}%  "
              f"极差 {max(plain) - min(plain):.1f}pp")
    t = results.get("trix", {}).get("equity_pct")
    f1105 = results.get("fixed_1105", {}).get("equity_pct")
    f1450 = results.get("fixed_1450", {}).get("equity_pct")
    rnd = results.get("random", {}).get("equity_pct")
    if t is not None and f1105 is not None:
        print(f"    纯时间卖 11:05: {f1105:+.1f}%  (vs TRIX {f1105 - t:+.1f}pp)")
    if t is not None and f1450 is not None:
        print(f"    纯时间卖 14:50: {f1450:+.1f}%  (vs TRIX {f1450 - t:+.1f}pp)")
    if t is not None and rnd is not None:
        share = (rnd / t * 100) if t else 0
        print(f"    随机卖(安慰剂): {rnd:+.1f}%  →  买入端已解释 TRIX 收益的 {share:.0f}%")
    print()

    tag = args.tag or f"recent{len(test_dates)}"
    out_path = OUT_DIR / f"exit_indicator_swap_{tag}.json"
    out_path.write_text(json.dumps({
        "window": {"start": test_dates[0], "end": test_dates[-1], "days": len(test_dates)},
        "fixed": {"pick": "B(全市场Top1,>=3%)", "confirm": args.confirm_time,
                  "buy_time": BUY_TIME, "fee": args.fee},
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  落盘: {out_path}")


if __name__ == "__main__":
    main()

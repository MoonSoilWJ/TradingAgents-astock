#!/usr/bin/env python3
"""T+0 混合卖点回测 — TRIX  vs  TRIX+追踪回落（谁先触发谁先卖）。

固定买点：14:45 涨幅 TOP1 / 14:50 买入（与实盘一致）
卖点窗口：次日 09:40~11:05

对比方案：
- trix:        仅 5分K TRIX(5,3) 死叉
- hybrid:      TRIX 死叉 或 日内峰值回落 trail_drop%（有浮盈）→ 先到先卖
- trail_only:  仅追踪回落（对照）

用法:
    python scripts/backtest_t0_hybrid_sell.py --days 100
    python scripts/backtest_t0_hybrid_sell.py --days 100 --scan-trail
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import _calc_stats  # noqa: E402
from backtest_top1_minute import calc_trix, calc_trix_signal  # noqa: E402
from backtest_t0_etf import bar_time_min, price_at_time  # noqa: E402
from backtest_t0_grid import segment_stats  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    MIN_GAIN,
    TRIX_MIN_SELL,
    TRIX_PERIOD,
    apply_net_return,
    bars_for_trix,
    bar_clock,
    load_market_data,
    resolve_eval_dates,
    time_to_min,
)
from search_t0_time_combo import bars_until, precompute_picks, simulate_exit  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
SELL_CUTOFF = "11:05"
TRIX_SIGNAL_PERIOD = 3
MIN_TRADES = 10


def simulate_trail_only(
    buy_price: float,
    next_bars: list[dict],
    sell_cutoff: str = SELL_CUTOFF,
    min_sell_time: str = TRIX_MIN_SELL,
    trail_drop_pct: float = 0.5,
) -> tuple[float, str]:
    """次日窗口内：日内峰值回落 trail_drop% 卖（有浮盈），否则定时卖。"""
    window = bars_until(next_bars, sell_cutoff)
    if not window:
        return buy_price, "no_data"

    min_sell_min = time_to_min(min_sell_time)
    peak = buy_price

    for b in window:
        if bar_time_min(b) < min_sell_min:
            continue
        high = float(b["high"])
        low = float(b["low"])
        peak = max(peak, high)
        if peak > buy_price and low <= peak * (1 - trail_drop_pct / 100):
            sell_price = peak * (1 - trail_drop_pct / 100)
            return sell_price, "trail_drop"

    return float(window[-1]["close"]), "time_sell"


def simulate_hybrid(
    buy_price: float,
    day_bars: list[dict],
    next_bars: list[dict],
    sell_cutoff: str = SELL_CUTOFF,
    min_sell_time: str = TRIX_MIN_SELL,
    trix_period: int = TRIX_PERIOD,
    trix_signal_period: int = TRIX_SIGNAL_PERIOD,
    trail_drop_pct: float = 0.5,
) -> tuple[float, str]:
    """TRIX 死叉 或 日内峰值回落 → 先到先卖（同窗口 09:40~cutoff）。"""
    window = bars_until(next_bars, sell_cutoff)
    if not window:
        return buy_price, "no_data"

    all_bars = bars_for_trix(day_bars) + bars_for_trix(window)
    min_warmup = trix_period * 3 + 5
    if len(all_bars) < min_warmup:
        return float(window[-1]["close"]), "close"

    warmup_len = len(bars_for_trix(day_bars))
    closes = [float(b.get("close", 0)) for b in all_bars]
    trix = calc_trix(closes, trix_period)
    signal = calc_trix_signal(trix, trix_signal_period)
    min_sell_min = time_to_min(min_sell_time)

    # 仅在次日卖出窗口内检查；峰值=窗口内最高价
    peak = buy_price
    search_start = max(warmup_len, min_warmup)

    for i in range(search_start, len(all_bars)):
        b = all_bars[i]
        if bar_time_min(b) if "time" in b else time_to_min(bar_clock(b)) < min_sell_min:
            # 兼容 bars_for_trix 格式
            pass
        bt = bar_clock(b) if b.get("day") else b.get("time", "00:00")[:5]
        if time_to_min(bt) < min_sell_min:
            continue

        high = float(b.get("high", closes[i]))
        low = float(b.get("low", closes[i]))
        peak = max(peak, high)

        # 追踪回落（K 内 low 触发，优先于收盘 TRIX）
        if peak > buy_price and low <= peak * (1 - trail_drop_pct / 100):
            sell_price = peak * (1 - trail_drop_pct / 100)
            return sell_price, "trail_drop"

        # TRIX 死叉（收盘判定）
        if i > 0 and trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
            return closes[i], "trix_death_cross"

    return float(window[-1]["close"]), "time_sell"


def _bar_time_for_trix_bar(b: dict) -> int:
    if "time" in b and b["time"]:
        return bar_time_min(b)
    return time_to_min(bar_clock(b))


def simulate_hybrid_v2(
    buy_price: float,
    day_bars: list[dict],
    next_bars: list[dict],
    sell_cutoff: str = SELL_CUTOFF,
    min_sell_time: str = TRIX_MIN_SELL,
    trix_period: int = TRIX_PERIOD,
    trix_signal_period: int = TRIX_SIGNAL_PERIOD,
    trail_drop_pct: float = 0.5,
) -> tuple[float, str]:
    """混合卖点：用完整 5 分 K（含 high/low）做 TRIX + 追踪。"""
    window = bars_until(next_bars, sell_cutoff)
    if not window:
        return buy_price, "no_data"

    # TRIX 用 close 序列；遍历用原始 window bars
    all_bars = day_bars + window
    closes = [float(b["close"]) for b in all_bars]
    trix = calc_trix(closes, trix_period)
    signal = calc_trix_signal(trix, trix_signal_period)
    min_warmup = trix_period * 3 + 5
    min_sell_min = time_to_min(min_sell_time)
    warmup_len = len(day_bars)

    if len(all_bars) < min_warmup:
        return float(window[-1]["close"]), "close"

    peak = buy_price
    search_start = max(warmup_len, min_warmup)

    for i in range(search_start, len(all_bars)):
        b = all_bars[i]
        if bar_time_min(b) < min_sell_min:
            continue

        high = float(b["high"])
        low = float(b["low"])
        peak = max(peak, high)

        if peak > buy_price and low <= peak * (1 - trail_drop_pct / 100):
            return peak * (1 - trail_drop_pct / 100), "trail_drop"

        if i > 0 and trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
            return float(b["close"]), "trix_death_cross"

    return float(window[-1]["close"]), "time_sell"


def run_strategy(
    mode: str,
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
    trail_drop_pct: float = 0.5,
) -> dict | None:
    rets: list[float] = []
    trades: list[dict] = []
    reasons: dict[str, int] = {}

    for day in eval_dates:
        picked = picks.get((SIGNAL_TIME, day))
        if not picked:
            continue
        code, gain, name = picked

        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, BUY_TIME)
        if not buy_price or buy_price <= 0:
            continue

        if day not in all_dates:
            continue
        idx = all_dates.index(day)
        if idx + 1 >= len(all_dates):
            continue
        next_day = all_dates[idx + 1]
        next_bars = etf_5min.get(code, {}).get(next_day, [])
        if not next_bars:
            continue

        if mode == "trix":
            sell_price, sell_reason, _ = simulate_exit(
                "trix0940_cut", buy_price, day_bars, BUY_TIME, next_bars, SELL_CUTOFF,
                trix_period=TRIX_PERIOD, trix_signal_period=TRIX_SIGNAL_PERIOD,
            )
        elif mode == "trail":
            sell_price, sell_reason = simulate_trail_only(
                buy_price, next_bars, SELL_CUTOFF, TRIX_MIN_SELL, trail_drop_pct,
            )
        elif mode == "hybrid":
            sell_price, sell_reason = simulate_hybrid_v2(
                buy_price, day_bars, next_bars, SELL_CUTOFF, TRIX_MIN_SELL,
                TRIX_PERIOD, TRIX_SIGNAL_PERIOD, trail_drop_pct,
            )
        else:
            raise ValueError(mode)

        if sell_price is None or sell_price <= 0:
            continue

        ret = apply_net_return(buy_price, sell_price, fee_pct)
        rets.append(ret)
        reasons[sell_reason] = reasons.get(sell_reason, 0) + 1
        trades.append({
            "signal_date": day,
            "sell_date": next_day,
            "etf": code,
            "name": name,
            "today_gain": round(gain, 2),
            "buy_price": round(buy_price, 4),
            "sell_price": round(sell_price, 4),
            "sell_reason": sell_reason,
            "return_pct": round(ret, 4),
        })

    if len(rets) < MIN_TRADES:
        return None

    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {
        "mode": mode,
        "trail_drop_pct": trail_drop_pct,
        "trade_count": len(rets),
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets),
        "sell_reasons": reasons,
        "trades": trades,
    }


def print_compare(results: list[dict], eval_dates: list[str]):
    labels = {"trix": "仅 TRIX", "hybrid": "TRIX+追踪回落", "trail": "仅追踪回落"}
    print("=" * 95)
    print("  T+0 混合卖点对比（14:45/14:50 买，次日 09:40~11:05 卖）")
    print("=" * 95)
    print(f"  {'方案':<16} {'笔数':>4} {'累计':>9} {'胜率':>6} {'均笔':>7} {'回撤':>8} {'夏普':>6}")
    print("  " + "─" * 85)
    for r in results:
        st = r["stats"]
        label = labels.get(r["mode"], r["mode"])
        if r["mode"] == "hybrid":
            label = f"混合(回落{r['trail_drop_pct']:.1f}%)"
        elif r["mode"] == "trail":
            label = f"仅追踪({r['trail_drop_pct']:.1f}%)"
        print(
            f"  {label:<16} {r['trade_count']:>4} {r['final_equity_pct']:+8.2f}% "
            f"{st.get('win_rate', 0):>5.1f}% {st.get('avg', 0):>+6.2f}% "
            f"{st.get('max_drawdown', 0):>+7.2f}% {st.get('sharpe', 0):>6.2f}"
        )
    print("=" * 95)

    trix_r = next((r for r in results if r["mode"] == "trix"), None)
    hybrid_r = next((r for r in results if r["mode"] == "hybrid"), None)
    if trix_r and hybrid_r:
        diff = hybrid_r["final_equity_pct"] - trix_r["final_equity_pct"]
        print(f"\n  混合 vs 纯 TRIX: {diff:+.2f}%")
        print(f"  混合卖出原因: {hybrid_r['sell_reasons']}")
        print(f"  纯 TRIX 卖出原因: {trix_r['sell_reasons']}")

    if len(eval_dates) >= 9 and trix_r and hybrid_r:
        seg_size = len(eval_dates) // 3
        segs = [
            ("前期", eval_dates[:seg_size]),
            ("中期", eval_dates[seg_size: 2 * seg_size]),
            ("后期", eval_dates[2 * seg_size:]),
        ]
        print("\n  分 3 段（独立起算）:")
        print(f"  {'阶段':<8} {'纯TRIX':>10} {'混合':>10} {'差值':>10}")
        for name, ds in segs:
            t = segment_stats(trix_r["trades"], ds)
            h = segment_stats(hybrid_r["trades"], ds)
            print(f"  {name:<8} {t['total']:+9.2f}% {h['total']:+9.2f}% {h['total']-t['total']:+9.2f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 TRIX vs 混合卖点回测")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="")
    parser.add_argument("--trail-drop", type=float, default=0.5)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--scan-trail", action="store_true",
                        help="扫描混合模式 trail_drop 0.3~1.5%%")
    args = parser.parse_args()

    print("=== T+0 混合卖点回测 ===")
    print(f"买点: {SIGNAL_TIME}/{BUY_TIME} | 卖点窗口: 次日 {TRIX_MIN_SELL}~{SELL_CUTOFF}")
    print(f"TRIX: 5分K({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) | 追踪回落: {args.trail_drop}%")
    print(f"过滤: 涨幅≥{MIN_GAIN}% | 震荡跳过")
    print()

    etf_list = get_all_t0_etfs()
    lookback = args.days if not (args.start_date or args.end_date) else max(args.days, 280)
    etf_daily, etf_5min, all_dates, proxy_klines = load_market_data(etf_list, lookback)
    eval_dates = resolve_eval_dates(all_dates, args.days, args.start_date, args.end_date)
    if len(eval_dates) < 5:
        print("ERROR: 有效交易日不足")
        sys.exit(1)
    print(f"回测 {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日)\n")

    picks = precompute_picks(
        etf_list, etf_daily, etf_5min, eval_dates, [SIGNAL_TIME],
        proxy_klines, use_filter=True, skip_choppy=True,
    )

    common = dict(
        eval_dates=eval_dates, all_dates=all_dates, picks=picks,
        etf_5min=etf_5min, fee_pct=args.fee,
    )

    if args.scan_trail:
        trix = run_strategy("trix", **common)
        if not trix:
            print("ERROR: TRIX 无有效交易")
            sys.exit(1)
        print(f"  纯 TRIX: {trix['final_equity_pct']:+.2f}% ({trix['trade_count']}笔)\n")
        print(f"  {'回落%':>6} {'混合累计':>9} {'胜率':>6} {'回撤':>8} {'vsTRIX':>8} {'追踪占比':>8}")
        print("  " + "─" * 55)
        for drop in [0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.5]:
            h = run_strategy("hybrid", trail_drop_pct=drop, **common)
            if not h:
                continue
            trail_n = h["sell_reasons"].get("trail_drop", 0)
            trail_pct = trail_n / h["trade_count"] * 100
            st = h["stats"]
            diff = h["final_equity_pct"] - trix["final_equity_pct"]
            print(
                f"  {drop:>5.1f}% {h['final_equity_pct']:+8.2f}% "
                f"{st.get('win_rate', 0):>5.1f}% {st.get('max_drawdown', 0):>+7.2f}% "
                f"{diff:>+7.2f}% {trail_pct:>7.1f}%"
            )
        sys.exit(0)

    trix = run_strategy("trix", **common)
    hybrid = run_strategy("hybrid", trail_drop_pct=args.trail_drop, **common)
    trail = run_strategy("trail", trail_drop_pct=args.trail_drop, **common)
    if not trix or not hybrid:
        print("ERROR: 有效交易不足")
        sys.exit(1)

    results = [trix, hybrid]
    if trail:
        results.append(trail)
    print_compare(results, eval_dates)

    # 被混合改变的交易
    if trix and hybrid:
        trix_by_day = {t["signal_date"]: t for t in trix["trades"]}
        changed = []
        for ht in hybrid["trades"]:
            tt = trix_by_day.get(ht["signal_date"])
            if tt and ht["sell_reason"] == "trail_drop":
                changed.append({
                    "date": ht["signal_date"],
                    "etf": ht["etf"],
                    "trix_ret": tt["return_pct"],
                    "hybrid_ret": ht["return_pct"],
                    "diff": round(ht["return_pct"] - tt["return_pct"], 2),
                })
        if changed:
            changed.sort(key=lambda x: x["diff"], reverse=True)
            wins = sum(1 for c in changed if c["diff"] > 0)
            print(f"\n  追踪抢先触发 {len(changed)} 笔（占 {len(changed)/hybrid['trade_count']*100:.0f}%）")
            print(f"  其中混合更优 {wins} 笔，更差 {len(changed)-wins} 笔")
            print(f"  {'信号日':>12} {'ETF':>8} {'TRIX收益':>9} {'混合收益':>9} {'差值':>7}")
            for c in changed[:10]:
                print(
                    f"  {c['date']:>12} {c['etf']:>8} {c['trix_ret']:+8.2f}% "
                    f"{c['hybrid_ret']:+8.2f}% {c['diff']:+6.2f}%"
                )

    out_dir = Path.home() / ".tradingagents" / "rotation"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    payload = {
        "config": {
            "start": eval_dates[0], "end": eval_dates[-1],
            "signal": SIGNAL_TIME, "buy": BUY_TIME,
            "sell_window": f"{TRIX_MIN_SELL}~{SELL_CUTOFF}",
            "trail_drop": args.trail_drop,
        },
        "results": [{k: v for k, v in r.items() if k != "trades"} for r in results],
    }
    out_path = out_dir / f"backtest_t0_hybrid_{tag}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()

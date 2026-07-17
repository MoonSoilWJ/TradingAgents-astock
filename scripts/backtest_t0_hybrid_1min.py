#!/usr/bin/env python3
"""T+0 混合卖点 — 1 分钟 K 精确回测（TRIX vs TRIX+追踪回落）。

相对 5 分 K 回测的改进：
- 追踪回落按 1 分 K **逐分钟**推进峰值，用 close 判定回落（避免 5 分 bar 内路径失真）
- TRIX 仍用 5 分 K（与实盘 t0_monitor 一致），由 1 分 K 重采样得到

用法:
    python scripts/backtest_t0_hybrid_1min.py --ndays 9 --source sina
    python scripts/backtest_t0_hybrid_1min.py --ndays 9 --scan-trail
    python scripts/backtest_t0_hybrid_1min.py --ndays 9 --compare-5m  # 同区间 5 分 K 对照
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
from backtest_t0_1min import load_1min_data  # noqa: E402
from backtest_t0_etf import bar_time_min, price_at_time  # noqa: E402
from backtest_t0_grid import segment_stats  # noqa: E402
from backtest_t0_hybrid_sell import simulate_hybrid_v2, simulate_trail_only  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    MIN_GAIN,
    TRIX_MIN_SELL,
    TRIX_PERIOD,
    apply_net_return,
    next_trading_day,
    time_to_min,
)
from search_t0_time_combo import bars_until, precompute_picks, simulate_exit  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
SELL_CUTOFF = "11:05"
TRIX_SIGNAL_PERIOD = 3
MIN_TRADES = 2


def bar_dt_key(bar: dict) -> tuple[str, int]:
    day = bar.get("day") or str(bar.get("datetime", ""))[:10]
    t = bar.get("time", "00:00:00")[:5]
    return day, time_to_min(t)


def resample_1min_to_5min(bars_1m: list[dict]) -> list[dict]:
    """1 分 K → 5 分 K（按交易日 + 5 分钟桶聚合）。"""
    if not bars_1m:
        return []
    buckets: dict[tuple[str, int], list[dict]] = {}
    for b in bars_1m:
        day, tm = bar_dt_key(b)
        bucket = (tm // 5) * 5
        buckets.setdefault((day, bucket), []).append(b)

    out: list[dict] = []
    for (day, bucket_min), grp in sorted(buckets.items()):
        grp.sort(key=lambda x: bar_dt_key(x)[1])
        h = int(bucket_min // 60)
        m = int(bucket_min % 60)
        out.append({
            "day": day,
            "time": f"{h:02d}:{m:02d}:00",
            "open": float(grp[0]["open"]),
            "high": max(float(x["high"]) for x in grp),
            "low": min(float(x["low"]) for x in grp),
            "close": float(grp[-1]["close"]),
        })
    return out


def first_trix_cross_5m(
    bars_5m: list[dict],
    min_sell_time: str,
    sell_cutoff: str,
    trix_period: int = TRIX_PERIOD,
    trix_signal_period: int = TRIX_SIGNAL_PERIOD,
) -> tuple[int, float] | None:
    """返回 (触发分钟, 卖价) 或 None。"""
    if len(bars_5m) < trix_period * 3 + 5:
        return None
    closes = [float(b["close"]) for b in bars_5m]
    trix = calc_trix(closes, trix_period)
    signal = calc_trix_signal(trix, trix_signal_period)
    min_m = time_to_min(min_sell_time)
    end_m = time_to_min(sell_cutoff)
    start = trix_period * 3 + 5

    for i in range(start, len(bars_5m)):
        tm = bar_time_min(bars_5m[i])
        if tm < min_m or tm > end_m:
            continue
        if trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
            return tm, float(bars_5m[i]["close"])
    return None


def simulate_trail_1min(
    buy_price: float,
    sell_bars_1m: list[dict],
    min_sell_time: str = TRIX_MIN_SELL,
    sell_cutoff: str = SELL_CUTOFF,
    trail_drop_pct: float = 0.5,
    use_close: bool = True,
) -> tuple[float, str]:
    """1 分 K 逐 bar 追踪：peak=running high，回落用 close（默认）或 low 判定。"""
    window = [b for b in sell_bars_1m if time_to_min(b.get("time", "00:00")[:5]) <= time_to_min(sell_cutoff)]
    if not window:
        return buy_price, "no_data"

    min_m = time_to_min(min_sell_time)
    peak = buy_price

    for b in window:
        tm = bar_time_min(b)
        if tm < min_m:
            continue
        high = float(b["high"])
        low = float(b["low"])
        close = float(b["close"])
        peak = max(peak, high)
        trigger_px = close if use_close else low
        if peak > buy_price and trigger_px <= peak * (1 - trail_drop_pct / 100):
            sell_px = trigger_px if use_close else peak * (1 - trail_drop_pct / 100)
            return sell_px, "trail_drop"

    return float(window[-1]["close"]), "time_sell"


def simulate_hybrid_1min(
    buy_price: float,
    buy_day_1m: list[dict],
    sell_day_1m: list[dict],
    min_sell_time: str = TRIX_MIN_SELL,
    sell_cutoff: str = SELL_CUTOFF,
    trail_drop_pct: float = 0.5,
    trix_period: int = TRIX_PERIOD,
    trix_signal_period: int = TRIX_SIGNAL_PERIOD,
) -> tuple[float, str]:
    """1 分 K 追踪 + 5 分 TRIX（1 分重采样），同窗口内先到先卖。"""
    all_1m = list(buy_day_1m) + list(sell_day_1m)
    bars_5m = resample_1min_to_5min(all_1m)
    trix_hit = first_trix_cross_5m(
        bars_5m, min_sell_time, sell_cutoff, trix_period, trix_signal_period,
    )
    trix_min = trix_hit[0] if trix_hit else None
    trix_price = trix_hit[1] if trix_hit else None

    min_m = time_to_min(min_sell_time)
    end_m = time_to_min(sell_cutoff)
    peak = buy_price

    sell_window = [
        b for b in sell_day_1m
        if min_m <= bar_time_min(b) <= end_m
    ]
    if not sell_window:
        return buy_price, "no_data"

    for b in sell_window:
        tm = bar_time_min(b)
        high = float(b["high"])
        close = float(b["close"])
        peak = max(peak, high)

        if peak > buy_price and close <= peak * (1 - trail_drop_pct / 100):
            return close, "trail_drop"

        if trix_min is not None and tm >= trix_min:
            return trix_price or close, "trix_death_cross"

    last = float(sell_window[-1]["close"])
    if trix_min is not None:
        return trix_price or last, "trix_death_cross"
    return last, "time_sell"


def simulate_trix_1min_resampled(
    buy_price: float,
    buy_day_1m: list[dict],
    sell_day_1m: list[dict],
    min_sell_time: str = TRIX_MIN_SELL,
    sell_cutoff: str = SELL_CUTOFF,
) -> tuple[float, str]:
    """纯 TRIX：1 分 K 重采样为 5 分 K 后判定（与实盘一致）。"""
    all_1m = list(buy_day_1m) + list(sell_day_1m)
    bars_5m = resample_1min_to_5min(all_1m)
    hit = first_trix_cross_5m(bars_5m, min_sell_time, sell_cutoff)
    if hit:
        return hit[1], "trix_death_cross"
    window = [b for b in sell_day_1m if bar_time_min(b) <= time_to_min(sell_cutoff)]
    if not window:
        return buy_price, "no_data"
    return float(window[-1]["close"]), "time_sell"


def run_strategy_1min(
    mode: str,
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_1min: dict,
    fee_pct: float,
    trail_drop_pct: float = 0.5,
    etf_5min: dict | None = None,
) -> dict | None:
    rets: list[float] = []
    trades: list[dict] = []
    reasons: dict[str, int] = {}

    for day in eval_dates:
        picked = picks.get((SIGNAL_TIME, day))
        if not picked:
            continue
        code, gain, name = picked

        day_1m = etf_1min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_1m, BUY_TIME)
        if not buy_price or buy_price <= 0:
            continue

        sell_day = next_trading_day(all_dates, day)
        if not sell_day:
            continue
        sell_1m = etf_1min.get(code, {}).get(sell_day, [])
        if len(sell_1m) < 10:
            continue

        if mode == "trix_1m":
            sell_price, reason = simulate_trix_1min_resampled(
                buy_price, day_1m, sell_1m,
            )
        elif mode == "trail_1m":
            sell_price, reason = simulate_trail_1min(
                buy_price, sell_1m, TRIX_MIN_SELL, SELL_CUTOFF, trail_drop_pct,
            )
        elif mode == "hybrid_1m":
            sell_price, reason = simulate_hybrid_1min(
                buy_price, day_1m, sell_1m, TRIX_MIN_SELL, SELL_CUTOFF, trail_drop_pct,
            )
        elif mode == "trix_5m" and etf_5min:
            day_5m = etf_5min.get(code, {}).get(day, [])
            sell_5m = etf_5min.get(code, {}).get(sell_day, [])
            sell_price, reason = simulate_exit(
                "trix0940_cut", buy_price, day_5m, BUY_TIME, sell_5m, SELL_CUTOFF,
                trix_period=TRIX_PERIOD, trix_signal_period=TRIX_SIGNAL_PERIOD,
            )
        elif mode == "hybrid_5m" and etf_5min:
            day_5m = etf_5min.get(code, {}).get(day, [])
            sell_5m = etf_5min.get(code, {}).get(sell_day, [])
            sell_price, reason = simulate_hybrid_v2(
                buy_price, day_5m, sell_5m, SELL_CUTOFF, TRIX_MIN_SELL,
                TRIX_PERIOD, TRIX_SIGNAL_PERIOD, trail_drop_pct,
            )
        elif mode == "trail_5m" and etf_5min:
            sell_5m = etf_5min.get(code, {}).get(sell_day, [])
            sell_price, reason = simulate_trail_only(
                buy_price, sell_5m, SELL_CUTOFF, TRIX_MIN_SELL, trail_drop_pct,
            )
        else:
            raise ValueError(mode)

        if sell_price is None or sell_price <= 0:
            continue

        ret = apply_net_return(buy_price, sell_price, fee_pct)
        rets.append(ret)
        reasons[reason] = reasons.get(reason, 0) + 1
        trades.append({
            "signal_date": day,
            "sell_date": sell_day,
            "etf": code,
            "name": name,
            "today_gain": round(gain, 2),
            "buy_price": round(buy_price, 4),
            "sell_price": round(float(sell_price), 4),
            "sell_reason": reason,
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


def print_results(results: list[dict], title: str):
    labels = {
        "trix_1m": "纯TRIX(1分→5分)",
        "hybrid_1m": "混合(1分追踪)",
        "trail_1m": "纯追踪(1分)",
        "trix_5m": "纯TRIX(5分K)",
        "hybrid_5m": "混合(5分K)",
        "trail_5m": "纯追踪(5分K)",
    }
    print("=" * 95)
    print(f"  {title}")
    print("=" * 95)
    print(f"  {'方案':<22} {'笔数':>4} {'累计':>9} {'胜率':>6} {'均笔':>7} {'回撤':>8}")
    print("  " + "─" * 85)
    for r in results:
        st = r["stats"]
        label = labels.get(r["mode"], r["mode"])
        if "hybrid" in r["mode"] or "trail" in r["mode"]:
            label = f"{label} {r['trail_drop_pct']:.1f}%"
        print(
            f"  {label:<22} {r['trade_count']:>4} {r['final_equity_pct']:+8.2f}% "
            f"{st.get('win_rate', 0):>5.1f}% {st.get('avg', 0):>+6.2f}% "
            f"{st.get('max_drawdown', 0):>+7.2f}%"
        )
    print("=" * 95)
    for r in results:
        print(f"  {labels.get(r['mode'], r['mode'])} 卖出: {r['sell_reasons']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 混合卖点 1 分钟 K 回测")
    parser.add_argument("--ndays", type=int, default=9)
    parser.add_argument("--source", choices=["auto", "em", "sina"], default="sina")
    parser.add_argument("--trail-drop", type=float, default=0.5)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--scan-trail", action="store_true")
    parser.add_argument("--compare-5m", action="store_true", help="同区间附加 5 分 K 对照")
    args = parser.parse_args()

    print("=== T+0 混合卖点 1 分钟 K 回测 ===")
    print(f"数据源: {args.source} ndays={args.ndays} | 追踪回落: {args.trail_drop}%")
    print(f"买点 {SIGNAL_TIME}/{BUY_TIME} | 卖点 {TRIX_MIN_SELL}~{SELL_CUTOFF}")
    print(f"TRIX: 5分K({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) | 追踪: 1分K逐bar close判定")
    print(f"过滤: 涨幅≥{MIN_GAIN}% | 震荡跳过\n")

    etf_list = get_all_t0_etfs()
    etf_daily, etf_1min, all_dates, proxy_klines, data_source = load_1min_data(
        etf_list, args.ndays, source=args.source,
    )
    if len(etf_1min) < 5:
        print("ERROR: 1 分 K 不足")
        sys.exit(1)

    m1_dates = sorted({d for bars in etf_1min.values() for d in bars})
    # 新浪 1 分 K 一次可返回数百日历史；仅取最近 ndays 个交易日
    if len(m1_dates) > args.ndays:
        m1_dates = m1_dates[-args.ndays:]
    eval_dates = m1_dates[:-1]
    if len(eval_dates) < MIN_TRADES:
        print("ERROR: 信号日不足")
        sys.exit(1)
    print(f"数据: {data_source}")
    print(f"1分K: {m1_dates[0]} ~ {m1_dates[-1]} ({len(m1_dates)} 日)")
    print(f"信号日: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日)\n")

    picks = precompute_picks(
        etf_list, etf_daily, etf_1min, eval_dates, [SIGNAL_TIME],
        proxy_klines, use_filter=True, skip_choppy=True,
    )

    etf_5min = None
    if args.compare_5m:
        from backtest_t0_today1 import load_market_data  # noqa: E402
        _, etf_5min, _, _ = load_market_data(etf_list, lookback=max(len(m1_dates) + 30, 60))

    common = dict(
        eval_dates=eval_dates, all_dates=all_dates, picks=picks,
        etf_1min=etf_1min, fee_pct=args.fee, etf_5min=etf_5min,
    )

    if args.scan_trail:
        trix = run_strategy_1min("trix_1m", **common)
        if not trix:
            print("ERROR: TRIX 无交易")
            sys.exit(1)
        print(f"  纯 TRIX(1分→5分): {trix['final_equity_pct']:+.2f}% ({trix['trade_count']}笔)\n")
        print(f"  {'回落%':>6} {'混合累计':>9} {'vsTRIX':>8} {'追踪占比':>8} {'卖出原因'}")
        print("  " + "─" * 60)
        for drop in [0.3, 0.4, 0.5, 0.6, 0.8, 1.0]:
            h = run_strategy_1min("hybrid_1m", trail_drop_pct=drop, **common)
            if not h:
                continue
            trail_n = h["sell_reasons"].get("trail_drop", 0)
            pct = trail_n / h["trade_count"] * 100
            diff = h["final_equity_pct"] - trix["final_equity_pct"]
            print(
                f"  {drop:>5.1f}% {h['final_equity_pct']:+8.2f}% {diff:>+7.2f}% "
                f"{pct:>7.1f}% {h['sell_reasons']}"
            )
        sys.exit(0)

    trix_1m = run_strategy_1min("trix_1m", **common)
    hybrid_1m = run_strategy_1min("hybrid_1m", trail_drop_pct=args.trail_drop, **common)
    trail_1m = run_strategy_1min("trail_1m", trail_drop_pct=args.trail_drop, **common)

    results = [r for r in (trix_1m, hybrid_1m, trail_1m) if r]
    if args.compare_5m and etf_5min:
        for mode in ("trix_5m", "hybrid_5m", "trail_5m"):
            r = run_strategy_1min(mode, trail_drop_pct=args.trail_drop, **common)
            if r:
                results.append(r)

    if not trix_1m or not hybrid_1m:
        print("ERROR: 有效交易不足")
        sys.exit(1)

    print_results(results, f"1 分 K 混合回测 ({len(eval_dates)} 信号日)")

    if trix_1m and hybrid_1m:
        diff = hybrid_1m["final_equity_pct"] - trix_1m["final_equity_pct"]
        print(f"\n  混合(1分) vs 纯TRIX(1分→5分): {diff:+.2f}%")

        trix_by_day = {t["signal_date"]: t for t in trix_1m["trades"]}
        changed = []
        for ht in hybrid_1m["trades"]:
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
            wins = sum(1 for c in changed if c["diff"] > 0)
            print(f"  追踪抢先 {len(changed)} 笔，混合更优 {wins}，更差 {len(changed)-wins}")
            print(f"  {'信号日':>12} {'ETF':>8} {'TRIX':>8} {'混合':>8} {'差':>7}")
            for c in sorted(changed, key=lambda x: -abs(x["diff"]))[:8]:
                print(
                    f"  {c['date']:>12} {c['etf']:>8} {c['trix_ret']:+7.2f}% "
                    f"{c['hybrid_ret']:+7.2f}% {c['diff']:+6.2f}%"
                )

    if args.compare_5m:
        h5 = next((r for r in results if r["mode"] == "hybrid_5m"), None)
        if h5 and hybrid_1m:
            print(f"\n  ★ 5分 vs 1分 混合(回落{args.trail_drop}%): "
                  f"5分{h5['final_equity_pct']:+.2f}% vs 1分{hybrid_1m['final_equity_pct']:+.2f}% "
                  f"(差{hybrid_1m['final_equity_pct']-h5['final_equity_pct']:+.2f}%)")

    out_dir = Path.home() / ".tradingagents" / "rotation"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = out_dir / f"backtest_t0_hybrid_1min_{tag}.json"
    out_path.write_text(json.dumps({
        "config": {
            "ndays": args.ndays, "source": args.source, "data_source": data_source,
            "eval_dates": eval_dates, "trail_drop": args.trail_drop,
        },
        "results": [{k: v for k, v in r.items() if k != "trades"} for r in results],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()

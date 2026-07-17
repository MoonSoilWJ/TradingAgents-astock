#!/usr/bin/env python3
"""T+0 网格策略 — 卖出方式对比：固定 +N% 卖 vs 追踪回落卖。

在相同选股/买入网格下，仅改变「卖出触发」逻辑：
- fixed:  价格每上涨 sell_step% → 减仓 1 单位（对称网格卖）
- trail:  价格创新高后，从峰值回落 trail_drop% → 减仓 1 单位

默认对齐实盘：14:45 选股 / 14:50 开仓 / 次日 11:05 截止 / 买入步长 2%。

用法:
    python scripts/backtest_t0_grid_sell_compare.py --days 100
    python scripts/backtest_t0_grid_sell_compare.py --days 100 --sell-step 2 --trail-drop 0.5
    python scripts/backtest_t0_grid_sell_compare.py --days 100 --scan-trail  # 扫描回落幅度
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
from backtest_t0_etf import bar_time_min, price_at_time  # noqa: E402
from backtest_t0_grid import collect_grid_bars, segment_stats  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    MIN_GAIN,
    load_market_data,
    next_trading_day,
    resolve_eval_dates,
)
from search_t0_time_combo import precompute_picks  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

DEFAULT_SIGNAL = "14:45"
DEFAULT_START = "14:50"
DEFAULT_END_OFFSET = 1
DEFAULT_END = "11:05"
DEFAULT_BUY_STEP = 2.0
DEFAULT_SELL_STEP = 2.0
DEFAULT_TRAIL_DROP = 0.5
DEFAULT_MAX_LAYERS = 5
MIN_TRADES = 10


def _finalize(
    buy_cost_total: float,
    sell_proceeds_total: float,
    position: int,
    end_price: float,
    fee_pct: float,
    buy_count: int,
    sell_count: int,
) -> dict | None:
    if end_price <= 0:
        return None
    final_value = sell_proceeds_total + position * end_price * (1 - fee_pct / 100)
    ret_pct = (final_value - buy_cost_total) / buy_cost_total * 100
    return {
        "return_pct": ret_pct,
        "end_price": end_price,
        "position": position,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "grid_trades": buy_count + sell_count - 1,
    }


def simulate_grid_fixed_sell(
    bars: list[dict],
    start_price: float,
    buy_step_pct: float,
    sell_step_pct: float,
    max_layers: int = DEFAULT_MAX_LAYERS,
    fee_pct: float = FEE_PCT,
) -> dict | None:
    """对称网格：买跌 buy_step%，卖涨 sell_step%。"""
    if not bars or start_price <= 0:
        return None

    position = 1
    buy_cost_total = start_price * (1 + fee_pct / 100)
    sell_proceeds_total = 0.0
    buy_count = 1
    sell_count = 0
    last_buy_anchor = start_price
    last_sell_anchor = start_price

    for bar in bars:
        high = float(bar["high"])
        low = float(bar["low"])

        while position > 0 and high >= last_sell_anchor * (1 + sell_step_pct / 100):
            sell_price = last_sell_anchor * (1 + sell_step_pct / 100)
            position -= 1
            sell_proceeds_total += sell_price * (1 - fee_pct / 100)
            sell_count += 1
            last_sell_anchor = sell_price
            last_buy_anchor = min(last_buy_anchor, sell_price)

        while position < max_layers and low <= last_buy_anchor * (1 - buy_step_pct / 100):
            buy_price = last_buy_anchor * (1 - buy_step_pct / 100)
            position += 1
            buy_cost_total += buy_price * (1 + fee_pct / 100)
            buy_count += 1
            last_buy_anchor = buy_price
            last_sell_anchor = max(last_sell_anchor, buy_price)

    return _finalize(
        buy_cost_total, sell_proceeds_total, position,
        float(bars[-1]["close"]), fee_pct, buy_count, sell_count,
    )


def simulate_grid_trail_sell(
    bars: list[dict],
    start_price: float,
    buy_step_pct: float,
    trail_drop_pct: float,
    max_layers: int = DEFAULT_MAX_LAYERS,
    fee_pct: float = FEE_PCT,
) -> dict | None:
    """网格买入 + 追踪回落卖出：创新高后，从峰值回落 trail_drop% 减仓 1 单位。"""
    if not bars or start_price <= 0:
        return None

    position = 1
    buy_cost_total = start_price * (1 + fee_pct / 100)
    sell_proceeds_total = 0.0
    buy_count = 1
    sell_count = 0
    last_buy_anchor = start_price
    peak = start_price

    for bar in bars:
        high = float(bar["high"])
        low = float(bar["low"])

        if high > peak:
            peak = high

        # 从峰值回落 trail_drop% 卖出（可在一根 K 内连续触发）
        while position > 0 and peak > start_price and low <= peak * (1 - trail_drop_pct / 100):
            sell_price = peak * (1 - trail_drop_pct / 100)
            position -= 1
            sell_proceeds_total += sell_price * (1 - fee_pct / 100)
            sell_count += 1
            last_buy_anchor = min(last_buy_anchor, sell_price)
            peak = sell_price  # 卖出后重置峰值锚点

        while position < max_layers and low <= last_buy_anchor * (1 - buy_step_pct / 100):
            buy_price = last_buy_anchor * (1 - buy_step_pct / 100)
            position += 1
            buy_cost_total += buy_price * (1 + fee_pct / 100)
            buy_count += 1
            last_buy_anchor = buy_price
            peak = max(peak, buy_price)

    return _finalize(
        buy_cost_total, sell_proceeds_total, position,
        float(bars[-1]["close"]), fee_pct, buy_count, sell_count,
    )


def run_strategy(
    sell_mode: str,
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    signal_time: str,
    grid_start: str,
    end_day_offset: int,
    grid_end: str,
    buy_step_pct: float,
    sell_step_pct: float,
    trail_drop_pct: float,
    fee_pct: float,
    max_layers: int,
) -> dict | None:
    sim_fn = simulate_grid_fixed_sell if sell_mode == "fixed" else simulate_grid_trail_sell
    rets: list[float] = []
    trades: list[dict] = []

    for day in eval_dates:
        picked = picks.get((signal_time, day))
        if not picked:
            continue
        code, gain, name = picked
        day_bars = etf_5min.get(code, {}).get(day, [])
        start_price = price_at_time(day_bars, grid_start)
        if not start_price or start_price <= 0:
            continue

        if end_day_offset == 0:
            end_day = day
        else:
            end_day = next_trading_day(all_dates, day)
            if not end_day:
                continue

        grid_bars = collect_grid_bars(etf_5min, code, day, grid_start, end_day, grid_end)
        if len(grid_bars) < 2:
            continue

        if sell_mode == "fixed":
            result = sim_fn(
                grid_bars, start_price, buy_step_pct, sell_step_pct, max_layers, fee_pct,
            )
        else:
            result = sim_fn(
                grid_bars, start_price, buy_step_pct, trail_drop_pct, max_layers, fee_pct,
            )
        if not result:
            continue

        rets.append(result["return_pct"])
        trades.append({
            "signal_date": day,
            "etf": code,
            "name": name,
            "return_pct": round(result["return_pct"], 4),
            "grid_trades": result["grid_trades"],
            "sell_count": result["sell_count"],
        })

    if len(rets) < MIN_TRADES:
        return None

    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    stats = _calc_stats(rets)
    avg_sells = sum(t["sell_count"] for t in trades) / len(trades)
    avg_grid = sum(t["grid_trades"] for t in trades) / len(trades)
    return {
        "sell_mode": sell_mode,
        "trade_count": len(rets),
        "avg_sell_count": round(avg_sells, 1),
        "avg_grid_trades": round(avg_grid, 1),
        "final_equity_pct": (eq - 1) * 100,
        "stats": stats,
        "trades": trades,
    }


def print_compare(fixed: dict, trail: dict, eval_dates: list[str]):
    print("=" * 90)
    print("  网格卖出方式对比")
    print("=" * 90)
    print(f"  {'方式':<28} {'笔数':>4} {'均卖次':>6} {'均网格':>6} {'累计':>9} {'胜率':>6} {'均笔':>7} {'回撤':>8}")
    print("  " + "─" * 80)
    for r, label in [(fixed, "固定 +N% 卖"), (trail, "追踪回落卖")]:
        st = r["stats"]
        print(
            f"  {label:<28} {r['trade_count']:>4} {r['avg_sell_count']:>5.1f} "
            f"{r['avg_grid_trades']:>5.1f} {r['final_equity_pct']:+8.2f}% "
            f"{st.get('win_rate', 0):>5.1f}% {st.get('avg', 0):>+6.2f}% "
            f"{st.get('max_drawdown', 0):>+7.2f}%"
        )
    diff = trail["final_equity_pct"] - fixed["final_equity_pct"]
    print("  " + "─" * 80)
    print(f"  追踪 vs 固定 累计差: {diff:+.2f}%")
    print("=" * 90)

    if len(eval_dates) >= 9:
        seg_size = len(eval_dates) // 3
        segs = [
            ("前期", eval_dates[:seg_size]),
            ("中期", eval_dates[seg_size: 2 * seg_size]),
            ("后期", eval_dates[2 * seg_size:]),
        ]
        print("\n  分 3 段（独立起算）:")
        print(f"  {'阶段':<8} {'固定+N%':>10} {'追踪回落':>10} {'差值':>10}")
        for name, ds in segs:
            f = segment_stats(fixed["trades"], ds)
            t = segment_stats(trail["trades"], ds)
            print(f"  {name:<8} {f['total']:+9.2f}% {t['total']:+9.2f}% {t['total']-f['total']:+9.2f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 网格固定卖 vs 追踪回落卖对比")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="")
    parser.add_argument("--signal", type=str, default=DEFAULT_SIGNAL)
    parser.add_argument("--start", type=str, default=DEFAULT_START)
    parser.add_argument("--end-offset", type=int, default=DEFAULT_END_OFFSET)
    parser.add_argument("--end", type=str, default=DEFAULT_END)
    parser.add_argument("--buy-step", type=float, default=DEFAULT_BUY_STEP)
    parser.add_argument("--sell-step", type=float, default=DEFAULT_SELL_STEP,
                        help="固定卖模式：每上涨 N%% 卖 1 单位")
    parser.add_argument("--trail-drop", type=float, default=DEFAULT_TRAIL_DROP,
                        help="追踪卖模式：从峰值回落 N%% 卖 1 单位")
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--max-layers", type=int, default=DEFAULT_MAX_LAYERS)
    parser.add_argument("--scan-trail", action="store_true",
                        help="扫描 trail_drop 0.3~1.5%%，与固定卖对比")
    args = parser.parse_args()

    print("=== T+0 网格卖出方式对比 ===")
    print(f"选股 {args.signal} | 开仓 {args.start} | 买步长 {args.buy_step}%")
    print(f"截止: {'次日' if args.end_offset else '当日'}{args.end}")
    print(f"固定卖: +{args.sell_step}% | 追踪卖: 峰值回落 {args.trail_drop}%")
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
        etf_list, etf_daily, etf_5min, eval_dates, [args.signal],
        proxy_klines, use_filter=True, skip_choppy=True,
    )

    common = dict(
        eval_dates=eval_dates, all_dates=all_dates, picks=picks, etf_5min=etf_5min,
        signal_time=args.signal, grid_start=args.start,
        end_day_offset=args.end_offset, grid_end=args.end,
        buy_step_pct=args.buy_step, fee_pct=args.fee, max_layers=args.max_layers,
        sell_step_pct=args.sell_step, trail_drop_pct=args.trail_drop,
    )

    if args.scan_trail:
        fixed = run_strategy(sell_mode="fixed", **common)
        if not fixed:
            print("ERROR: 固定卖模式无有效交易")
            sys.exit(1)
        print(f"  固定 +{args.sell_step}% 卖: {fixed['final_equity_pct']:+.2f}% "
              f"({fixed['trade_count']}笔, 均卖{fixed['avg_sell_count']}次)\n")
        print(f"  {'回落%':>6} {'累计':>9} {'胜率':>6} {'均笔':>7} {'均卖次':>6} {'vs固定':>8}")
        print("  " + "─" * 50)
        scan_results = []
        for drop in [0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.5]:
            kw = {k: v for k, v in common.items() if k != "trail_drop_pct"}
            t = run_strategy(sell_mode="trail", trail_drop_pct=drop, **kw)
            if not t:
                continue
            diff = t["final_equity_pct"] - fixed["final_equity_pct"]
            st = t["stats"]
            print(
                f"  {drop:>5.1f}% {t['final_equity_pct']:+8.2f}% "
                f"{st.get('win_rate', 0):>5.1f}% {st.get('avg', 0):>+6.2f}% "
                f"{t['avg_sell_count']:>5.1f} {diff:>+7.2f}%"
            )
            scan_results.append({"trail_drop": drop, **{k: v for k, v in t.items() if k != "trades"}})
        sys.exit(0)

    fixed = run_strategy(sell_mode="fixed", **common)
    trail = run_strategy(sell_mode="trail", **common)
    if not fixed or not trail:
        print("ERROR: 有效交易不足")
        sys.exit(1)

    print_compare(fixed, trail, eval_dates)

    # 卖出次数分布
    f_sells = [t["sell_count"] for t in fixed["trades"]]
    t_sells = [t["sell_count"] for t in trail["trades"]]
    print(f"\n  固定卖: 卖出次数 min={min(f_sells)} max={max(f_sells)} "
          f"零卖={(sum(1 for x in f_sells if x==0))}笔")
    print(f"  追踪卖: 卖出次数 min={min(t_sells)} max={max(t_sells)} "
          f"零卖={(sum(1 for x in t_sells if x==0))}笔")

    out_dir = Path.home() / ".tradingagents" / "rotation"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    payload = {
        "config": {
            "start": eval_dates[0], "end": eval_dates[-1],
            "signal": args.signal, "grid_start": args.start,
            "end": args.end, "buy_step": args.buy_step,
            "sell_step": args.sell_step, "trail_drop": args.trail_drop,
        },
        "fixed": {k: v for k, v in fixed.items() if k != "trades"},
        "trail": {k: v for k, v in trail.items() if k != "trades"},
    }
    out_path = out_dir / f"grid_sell_compare_{tag}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()

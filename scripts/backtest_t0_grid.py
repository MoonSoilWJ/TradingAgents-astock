#!/usr/bin/env python3
"""T+0 选股 + 网格交易策略回测 — 搜索最优信号/买卖时间与网格步长。

策略逻辑：
1. 信号时刻按当日涨幅 TOP1 选股（可选 ≥MIN_GAIN、震荡期跳过）
2. 网格起始时刻建立初始仓位（1 单位）
3. 价格每下跌 step_pct → 加仓 1 单位；每上涨 step_pct → 减仓 1 单位（对称网格）
4. 截止时刻强制平仓，计入手续费

搜索维度：
- signal_time：选股信号时刻
- grid_start：网格开仓时刻（须 > signal_time，同交易时段）
- grid_end_day：0=当日 / 1=次日
- grid_end：网格截止时刻
- step_pct：网格步长 (%)

用法:
    python scripts/backtest_t0_grid.py --days 100 --top 30
    python scripts/backtest_t0_grid.py --days 100 --combo 14:45,14:50,0,15:00,0.5
    python scripts/backtest_t0_grid.py --days 100 --narrow   # 缩小搜索空间
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
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    MIN_GAIN,
    apply_net_return,
    load_market_data,
    next_trading_day,
    rank_by_today_gain,
    regime_on_date,
    resolve_eval_dates,
    select_etf,
    time_to_min,
)
from search_t0_time_combo import (  # noqa: E402
    DEFAULT_BUY_TIMES,
    DEFAULT_SIGNAL_TIMES,
    precompute_picks,
    same_session,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

# 网格步长候选 (%)
DEFAULT_STEPS = [0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0]

# 网格截止时刻（当日 / 次日）
DEFAULT_END_TIMES_SAME = ["11:30", "14:00", "14:30", "14:55", "15:00"]
DEFAULT_END_TIMES_NEXT = [
    "09:35", "09:50", "10:05", "10:20", "10:35", "10:50", "11:05", "11:20",
    "13:00", "13:30", "14:00", "14:30", "15:00",
]

NARROW_SIGNAL = ["14:30", "14:45", "14:50"]
NARROW_START = ["14:35", "14:45", "14:50", "14:55"]
NARROW_STEPS = [0.3, 0.5, 0.8, 1.0, 1.5]
NARROW_END_SAME = ["14:55", "15:00"]
NARROW_END_NEXT = ["09:50", "10:05", "11:05", "14:00", "15:00"]

MIN_TRADES = 10
DEFAULT_MAX_LAYERS = 5

BASELINE = {
    "signal": "14:45",
    "grid_start": "14:50",
    "end_day_offset": 1,
    "grid_end": "11:05",
    "step_pct": 0.5,
    "label": "基线(14:45/14:50/步长0.5%/次日11:05)",
}


def collect_grid_bars(
    etf_5min: dict,
    code: str,
    start_day: str,
    start_time: str,
    end_day: str,
    end_time: str,
) -> list[dict]:
    """收集网格交易窗口内的 5 分 K（可跨日）。"""
    start_min = time_to_min(start_time)
    end_min = time_to_min(end_time)
    bars: list[dict] = []
    cur = start_day
    days_seen: list[str] = []
    while True:
        days_seen.append(cur)
        day_bars = etf_5min.get(code, {}).get(cur, [])
        for b in day_bars:
            tm = bar_time_min(b)
            if cur == start_day and tm < start_min:
                continue
            if cur == end_day and tm > end_min:
                continue
            bars.append(b)
        if cur == end_day:
            break
        nxt = None
        all_days = sorted(etf_5min.get(code, {}).keys())
        if cur in all_days:
            idx = all_days.index(cur)
            if idx + 1 < len(all_days):
                nxt = all_days[idx + 1]
        if not nxt or nxt > end_day:
            break
        cur = nxt
    return bars


def simulate_grid(
    bars: list[dict],
    start_price: float,
    step_pct: float,
    max_layers: int = DEFAULT_MAX_LAYERS,
    fee_pct: float = FEE_PCT,
) -> dict | None:
    """对称等单位网格模拟，返回归一化收益率与交易统计。"""
    if not bars or start_price <= 0 or step_pct <= 0:
        return None

    position = 1  # 初始 1 单位
    buy_cost_total = start_price * (1 + fee_pct / 100)
    sell_proceeds_total = 0.0
    buy_count = 1
    sell_count = 0
    last_buy_anchor = start_price
    last_sell_anchor = start_price

    for bar in bars:
        high = float(bar["high"])
        low = float(bar["low"])

        # 先处理卖出（价格上涨触发）
        while position > 0 and high >= last_sell_anchor * (1 + step_pct / 100):
            sell_price = last_sell_anchor * (1 + step_pct / 100)
            position -= 1
            sell_proceeds_total += sell_price * (1 - fee_pct / 100)
            sell_count += 1
            last_sell_anchor = sell_price
            # 卖出后重置买入锚点为成交价，避免重复触发
            last_buy_anchor = min(last_buy_anchor, sell_price)

        # 再处理买入（价格下跌触发）
        while position < max_layers and low <= last_buy_anchor * (1 - step_pct / 100):
            buy_price = last_buy_anchor * (1 - step_pct / 100)
            position += 1
            buy_cost_total += buy_price * (1 + fee_pct / 100)
            buy_count += 1
            last_buy_anchor = buy_price
            last_sell_anchor = max(last_sell_anchor, buy_price)

    end_price = float(bars[-1]["close"])
    if end_price <= 0:
        return None

    # 强制平仓
    final_value = sell_proceeds_total + position * end_price * (1 - fee_pct / 100)
    ret_pct = (final_value - buy_cost_total) / buy_cost_total * 100

    return {
        "return_pct": ret_pct,
        "end_price": end_price,
        "position": position,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "grid_trades": buy_count + sell_count - 1,  # 不含初始买入
    }


def run_grid_combo(
    signal_time: str,
    grid_start: str,
    end_day_offset: int,
    grid_end: str,
    step_pct: float,
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
    max_layers: int,
) -> dict | None:
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

        grid_bars = collect_grid_bars(
            etf_5min, code, day, grid_start, end_day, grid_end,
        )
        if len(grid_bars) < 2:
            continue

        result = simulate_grid(
            grid_bars, start_price, step_pct, max_layers, fee_pct,
        )
        if not result:
            continue

        rets.append(result["return_pct"])
        trades.append({
            "signal_date": day,
            "end_date": end_day,
            "etf": code,
            "name": name,
            "today_gain": round(gain, 2),
            "start_price": round(start_price, 4),
            "end_price": round(result["end_price"], 4),
            "step_pct": step_pct,
            "grid_trades": result["grid_trades"],
            "buy_count": result["buy_count"],
            "sell_count": result["sell_count"],
            "return_pct": round(result["return_pct"], 4),
        })

    if len(rets) < MIN_TRADES:
        return None

    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    stats = _calc_stats(rets)
    avg_grid = sum(t["grid_trades"] for t in trades) / len(trades)
    return {
        "signal": signal_time,
        "grid_start": grid_start,
        "end_day_offset": end_day_offset,
        "grid_end": grid_end,
        "step_pct": step_pct,
        "label": _combo_label(signal_time, grid_start, end_day_offset, grid_end, step_pct),
        "trade_count": len(rets),
        "avg_grid_trades": round(avg_grid, 1),
        "final_equity_pct": (eq - 1) * 100,
        "stats": stats,
        "trades": trades,
    }


def _combo_label(
    signal: str, start: str, end_offset: int, end: str, step: float,
) -> str:
    day_tag = "当日" if end_offset == 0 else "次日"
    return f"{signal}→{start} 步{step:.1f}% {day_tag}{end}"


def iter_combos(
    signal_times: list[str],
    start_times: list[str],
    steps: list[float],
    end_same: list[str],
    end_next: list[str],
    include_same_day: bool = True,
    include_overnight: bool = True,
) -> list[tuple[str, str, int, str, float]]:
    combos: list[tuple[str, str, int, str, float]] = []
    for sig in signal_times:
        for start in start_times:
            if time_to_min(start) <= time_to_min(sig):
                continue
            if not same_session(sig, start):
                continue
            if time_to_min(start) > time_to_min("14:55"):
                continue
            for step in steps:
                if include_same_day:
                    for end in end_same:
                        if time_to_min(end) <= time_to_min(start):
                            continue
                        combos.append((sig, start, 0, end, step))
                if include_overnight:
                    for end in end_next:
                        combos.append((sig, start, 1, end, step))
    return combos


def segment_stats(trades: list[dict], seg_dates: list[str]) -> dict:
    ds = set(seg_dates)
    rets = [t["return_pct"] for t in trades if t["signal_date"] in ds]
    if not rets:
        return {"count": 0, "total": 0.0, "win_rate": 0.0}
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    st = _calc_stats(rets)
    return {
        "count": len(rets),
        "total": (eq - 1) * 100,
        "win_rate": st.get("win_rate", 0),
        "avg": st.get("avg", 0),
    }


def print_top_results(
    results: list[dict],
    top: int,
    eval_dates: list[str],
    run_one=None,
):
    print("=" * 120)
    print(f"  T+0 网格策略 TOP {top}（按累计收益）")
    print("=" * 120)
    print(
        f"  {'#':>3} {'信号':>6} {'开仓':>6} {'步长':>5} {'截止':>12} "
        f"{'笔数':>4} {'均网格':>6} {'累计':>9} {'胜率':>6} {'均笔':>7} {'回撤':>8}"
    )
    print("  " + "─" * 110)
    for i, r in enumerate(results[:top], 1):
        st = r["stats"]
        end_tag = f"{'当' if r['end_day_offset']==0 else '次'}{r['grid_end']}"
        print(
            f"  {i:>3} {r['signal']:>6} {r['grid_start']:>6} {r['step_pct']:>4.1f}% "
            f"{end_tag:>12} {r['trade_count']:>4} {r['avg_grid_trades']:>5.1f} "
            f"{r['final_equity_pct']:+8.2f}% {st.get('win_rate', 0):>5.1f}% "
            f"{st.get('avg', 0):>+6.2f}% {st.get('max_drawdown', 0):>+7.2f}%"
        )
    print("=" * 120)

    if len(eval_dates) >= 9 and results:
        seg_size = len(eval_dates) // 3
        segs = [
            ("前期", eval_dates[:seg_size]),
            ("中期", eval_dates[seg_size: 2 * seg_size]),
            ("后期", eval_dates[2 * seg_size:]),
        ]
        print("\n  TOP 3 分 3 段（独立起算）:")
        print(f"  {'方案':<42} {'全期':>8} | {'前期':>8} {'中期':>8} {'后期':>8}")
        for r in results[:3]:
            trades = r.get("trades", [])
            if not trades and run_one:
                detail = run_one(
                    signal_time=r["signal"], grid_start=r["grid_start"],
                    end_day_offset=r["end_day_offset"], grid_end=r["grid_end"],
                    step_pct=r["step_pct"],
                )
                trades = detail["trades"] if detail else []
            parts = [segment_stats(trades, ds)["total"] for _, ds in segs]
            name = r["label"][:40]
            print(
                f"  {name:<42} {r['final_equity_pct']:+7.2f}% | "
                f"{parts[0]:+7.2f}% {parts[1]:+7.2f}% {parts[2]:+7.2f}%"
            )


def parse_combo(s: str) -> tuple[str, str, int, str, float]:
    """格式: signal,grid_start,end_day_offset,grid_end,step_pct"""
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 5:
        raise ValueError("combo 格式: signal,grid_start,end_day_offset,grid_end,step_pct")
    return parts[0], parts[1], int(parts[2]), parts[3], float(parts[4])


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 选股 + 网格策略参数搜索")
    parser.add_argument("--days", type=int, default=100, help="回测交易日数")
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--max-layers", type=int, default=DEFAULT_MAX_LAYERS,
                        help="最大持仓层数(默认5)")
    parser.add_argument("--skip-choppy", dest="skip_choppy", action="store_true", default=True)
    parser.add_argument("--no-skip-choppy", dest="skip_choppy", action="store_false")
    parser.add_argument("--no-filter", action="store_true")
    parser.add_argument("--narrow", action="store_true", help="缩小搜索空间(更快)")
    parser.add_argument("--same-day-only", action="store_true", help="仅搜索当日网格")
    parser.add_argument("--overnight-only", action="store_true", help="仅搜索跨日网格")
    parser.add_argument("--combo", type=str, default="",
                        help="单组合: signal,start,offset,end,step 如 14:45,14:50,1,11:05,0.5")
    parser.add_argument("--segments", action="store_true", help="单组合模式下输出分3段")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--save-all", action="store_true")
    args = parser.parse_args()

    use_filter = not args.no_filter
    skip_choppy = args.skip_choppy

    print("=== T+0 选股 + 网格策略回测 ===")
    print(f"过滤: 涨幅≥{MIN_GAIN}%={'是' if use_filter else '否'} | 震荡跳过={'是' if skip_choppy else '否'}")
    print(f"手续费: 万{args.fee * 100:.0f} | 最大层数: {args.max_layers}")
    print()

    etf_list = get_all_t0_etfs()
    lookback = args.days if not (args.start_date or args.end_date) else max(args.days, 280)
    etf_daily, etf_5min, all_dates, proxy_klines = load_market_data(etf_list, lookback)
    eval_dates = resolve_eval_dates(all_dates, args.days, args.start_date, args.end_date)
    if len(eval_dates) < 5:
        print("ERROR: 有效交易日不足")
        sys.exit(1)
    print(f"回测 {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日)\n")

    if args.narrow:
        signal_times = NARROW_SIGNAL
        start_times = NARROW_START
        steps = NARROW_STEPS
        end_same = NARROW_END_SAME
        end_next = NARROW_END_NEXT
    else:
        signal_times = DEFAULT_SIGNAL_TIMES
        start_times = DEFAULT_BUY_TIMES
        steps = DEFAULT_STEPS
        end_same = DEFAULT_END_TIMES_SAME
        end_next = DEFAULT_END_TIMES_NEXT

    picks = precompute_picks(
        etf_list, etf_daily, etf_5min, eval_dates, signal_times,
        proxy_klines, use_filter, skip_choppy,
    )

    run_one = lambda **kw: run_grid_combo(
        eval_dates=eval_dates, all_dates=all_dates, picks=picks,
        etf_5min=etf_5min, fee_pct=args.fee, max_layers=args.max_layers, **kw,
    )

    if args.combo:
        sig, start, offset, end, step = parse_combo(args.combo)
        r = run_one(
            signal_time=sig, grid_start=start, end_day_offset=offset,
            grid_end=end, step_pct=step,
        )
        if not r:
            print("ERROR: 组合无效或交易笔数不足")
            sys.exit(1)
        st = r["stats"]
        print(f"组合: {r['label']}")
        print(f"笔数: {r['trade_count']} | 均网格成交: {r['avg_grid_trades']} | 累计: {r['final_equity_pct']:+.2f}%")
        print(f"胜率: {st.get('win_rate', 0):.1f}% | 均笔: {st.get('avg', 0):+.2f}% | 回撤: {st.get('max_drawdown', 0):+.2f}%")
        if args.segments and len(eval_dates) >= 9:
            seg_size = len(eval_dates) // 3
            for name, ds in [
                ("前期", eval_dates[:seg_size]),
                ("中期", eval_dates[seg_size: 2 * seg_size]),
                ("后期", eval_dates[2 * seg_size:]),
            ]:
                s = segment_stats(r["trades"], ds)
                print(f"  {name}: {s['total']:+.2f}% ({s['count']}笔, 胜率{s['win_rate']:.0f}%)")
        sys.exit(0)

    if args.baseline_only:
        b = BASELINE
        r = run_one(
            signal_time=b["signal"], grid_start=b["grid_start"],
            end_day_offset=b["end_day_offset"], grid_end=b["grid_end"],
            step_pct=b["step_pct"],
        )
        if r:
            st = r["stats"]
            print(f"{b['label']}: {r['final_equity_pct']:+.2f}% | {r['trade_count']}笔 | 回撤{st.get('max_drawdown', 0):+.2f}%")
        sys.exit(0)

    include_same = not args.overnight_only
    include_next = not args.same_day_only
    combos = iter_combos(
        signal_times, start_times, steps, end_same, end_next,
        include_same_day=include_same, include_overnight=include_next,
    )
    print(f">>> 搜索 {len(combos)} 种网格组合...")
    results: list[dict] = []
    for sig, start, offset, end, step in combos:
        r = run_one(
            signal_time=sig, grid_start=start, end_day_offset=offset,
            grid_end=end, step_pct=step,
        )
        if r:
            r_light = {k: v for k, v in r.items() if k != "trades"}
            results.append(r_light)

    results.sort(key=lambda x: x["final_equity_pct"], reverse=True)
    print(f"    有效组合: {len(results)}\n")
    print_top_results(results, args.top, eval_dates, run_one=run_one)

    # 按步长汇总最优
    if results:
        best_by_step: dict[float, dict] = {}
        for r in results:
            s = r["step_pct"]
            if s not in best_by_step or r["final_equity_pct"] > best_by_step[s]["final_equity_pct"]:
                best_by_step[s] = r
        print("\n  各步长最优:")
        for s in sorted(best_by_step):
            r = best_by_step[s]
            print(f"    步长{s:.1f}%: {r['label']} → {r['final_equity_pct']:+.2f}% ({r['trade_count']}笔)")

        best = results[0]
        print(f"\n  ★ 全局最优: {best['label']}")
        print(f"    累计{best['final_equity_pct']:+.2f}% | {best['trade_count']}笔 | "
              f"均网格{best['avg_grid_trades']}次 | 夏普{best['stats'].get('sharpe', 0):.2f}")

    out_dir = Path.home() / ".tradingagents" / "rotation"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    payload = {
        "config": {
            "start": eval_dates[0],
            "end": eval_dates[-1],
            "eval_days": len(eval_dates),
            "use_filter": use_filter,
            "skip_choppy": skip_choppy,
            "fee": args.fee,
            "max_layers": args.max_layers,
            "combos_searched": len(combos),
            "combos_valid": len(results),
        },
        "top": results[: args.top],
    }
    if args.save_all:
        payload["all"] = results
    out_path = out_dir / f"backtest_t0_grid_{tag}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()

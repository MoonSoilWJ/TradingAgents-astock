#!/usr/bin/env python3
"""11:05~14:50 闲置资金午间 T+0 窗口回测（与实盘下午基线同一本金链）。

实盘节奏（不变）:
- 前日 14:50 买 → 当日 09:40~11:05 TRIX/定时卖 → 资金释放
- 14:45 信号 / 14:50 再买（隔夜腿）

本脚本仅在「当日已完成上午卖出、且 14:50 前已平仓」的窗口内加一笔 T+0，
评估午间腿 **单独** 是否正收益，以及叠加后是否不拖累下午基线。

约束:
- 午间最早信号/买入 ≥ 11:05（与 sell_watch 结束对齐）
- 午间最晚买入 < 14:50；卖出 ≤ 14:45（留 5 分钟给下午下单）
- 上午档买卖须在同一上午段且 11:30 前卖出；下午档在 13:00~14:45

用法:
    python scripts/backtest_t0_midday_idle.py --days 100 --search
    python scripts/backtest_t0_midday_idle.py --days 100 --combo 13:30,13:35,time,14:45
    python scripts/backtest_t0_midday_idle.py --days 100 --report-best
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import _calc_stats  # noqa: E402
from backtest_top1_intraday import check_sell_trigger  # noqa: E402
from backtest_t0_etf import apply_net_return, bar_time_min, price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    TRIX_MIN_SELL,
    TRIX_PERIOD,
    bars_for_trix,
    load_market_data,
    resolve_eval_dates,
    simulate_trix_cross_after,
    time_to_min,
)
from search_t0_time_combo import bars_until, precompute_picks, same_session  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

LIVE_SIGNAL = "14:45"
LIVE_BUY = "14:50"
LIVE_SELL_CUTOFF = "11:05"
MID_EARLIEST = "11:05"
MID_LATEST_BUY = "14:45"  # 早于 14:50 下午买
MID_LATEST_SELL = "14:45"

MID_SIGNAL_TIMES = [
    "11:05", "11:15", "11:25",
    "13:00", "13:15", "13:30", "13:45",
    "14:00", "14:15", "14:30", "14:40",
]
MID_BUY_TIMES = [
    "11:10", "11:20", "11:25",
    "13:05", "13:20", "13:35", "13:50",
    "14:05", "14:20", "14:35", "14:45",
]
MID_SELL_CUTOFFS = [
    "11:25", "11:29",
    "13:30", "13:45", "14:00", "14:15", "14:30", "14:45",
]

MIN_MID_TRADES = 8


def _next_day(all_dates: list[str], day: str) -> str | None:
    if day not in all_dates:
        return None
    i = all_dates.index(day)
    if i + 1 >= len(all_dates):
        return None
    return all_dates[i + 1]


def _overnight_sell(
    code: str,
    buy_price: float,
    buy_day: str,
    sell_day: str,
    etf_5min: dict,
    fee_pct: float,
) -> tuple[float, str, str]:
    day_bars = etf_5min.get(code, {}).get(buy_day, [])
    sell_bars = etf_5min.get(code, {}).get(sell_day, [])
    window = bars_until(sell_bars, LIVE_SELL_CUTOFF)
    if not window:
        return 0.0, "no_bars", LIVE_SELL_CUTOFF
    _, reason, detail = simulate_trix_cross_after(
        buy_price,
        bars_for_trix(day_bars),
        bars_for_trix(window),
        trix_period=TRIX_PERIOD,
        min_sell_time=TRIX_MIN_SELL,
    )
    sp = float(detail.get("sell_price") or window[-1]["close"])
    st = str(detail.get("bar") or LIVE_SELL_CUTOFF)
    return apply_net_return(buy_price, sp, fee_pct), reason, st


def _bars_after(bars: list[dict], after_time: str) -> list[dict]:
    am = time_to_min(after_time)
    out: list[dict] = []
    for b in bars:
        if bar_time_min(b) >= am:
            out.append(b)
    return out


def _simulate_midday_exit(
    sell_mode: str,
    buy_price: float,
    day_bars: list[dict],
    buy_time: str,
    sell_cutoff: str,
    fee_pct: float,
) -> tuple[float | None, str]:
    """仅使用 buy_time 之后、sell_cutoff 之前的 bar。"""
    seg = _bars_after(day_bars, buy_time)
    seg = bars_until(seg, sell_cutoff)
    if not seg:
        return None, "no_bars"

    if sell_mode == "time":
        sp = float(seg[-1]["close"])
        return apply_net_return(buy_price, sp, fee_pct), "time_sell"

    if sell_mode == "trail":
        sp, reason, _ = check_sell_trigger(
            seg, buy_price, 0, stop_loss_pct=-1.0,
            trail_trigger_pct=1.5, trail_drop_pct=0.4,
        )
        if sp is None:
            sp = float(seg[-1]["close"])
            reason = "time_fallback"
        return apply_net_return(buy_price, sp, fee_pct), reason

    if sell_mode == "fixed":
        tp, sl = buy_price * 1.015, buy_price * 0.992
        for b in seg:
            if float(b["low"]) <= sl:
                return apply_net_return(buy_price, sl, fee_pct), "stop_loss"
            if float(b["high"]) >= tp:
                return apply_net_return(buy_price, tp, fee_pct), "take_profit"
        sp = float(seg[-1]["close"])
        return apply_net_return(buy_price, sp, fee_pct), "close"

    return None, "unknown"


def _valid_midday_combo(signal: str, buy: str, sell_cutoff: str) -> bool:
    if time_to_min(signal) < time_to_min(MID_EARLIEST):
        return False
    if time_to_min(buy) <= time_to_min(signal):
        return False
    if time_to_min(buy) > time_to_min(MID_LATEST_BUY):
        return False
    if time_to_min(sell_cutoff) <= time_to_min(buy):
        return False
    if time_to_min(sell_cutoff) > time_to_min(MID_LATEST_SELL):
        return False
    if not same_session(signal, buy):
        return False
    # 上午段必须在 11:30 前卖
    if time_to_min(signal) < time_to_min("11:30") and time_to_min(sell_cutoff) > time_to_min("11:29"):
        return False
    return True


def run_capital_chain(
    eval_dates: list[str],
    all_dates: list[str],
    live_picks: dict,
    mid_picks: dict,
    etf_5min: dict,
    fee_pct: float,
    *,
    midday: tuple[str, str, str, str] | None,
) -> dict:
    """同一本金: 隔夜卖 → 可选午间 T+0 → 下午 14:50 买。"""
    equity = 1.0
    overnight_trades: list[dict] = []
    midday_trades: list[dict] = []
    holding: dict | None = None

    signal_t, buy_t, sell_mode, sell_cut = midday or ("", "", "", "")

    for day in eval_dates:
        morning_freed = False

        if holding and holding["sell_day"] == day:
            ret, reason, sell_time = _overnight_sell(
                holding["code"],
                holding["buy_price"],
                holding["buy_day"],
                day,
                etf_5min,
                fee_pct,
            )
            equity *= 1 + ret / 100
            overnight_trades.append({
                "signal_date": holding["signal_day"],
                "sell_date": day,
                "etf": holding["code"],
                "return_pct": round(ret, 4),
                "sell_reason": reason,
                "sell_time": sell_time,
            })
            holding = None
            morning_freed = True

        if midday and morning_freed and holding is None:
            picked = mid_picks.get((signal_t, day))
            if picked:
                code, gain, name = picked
                day_bars = etf_5min.get(code, {}).get(day, [])
                buy_price = price_at_time(day_bars, buy_t)
                if buy_price and buy_price > 0:
                    ret, reason = _simulate_midday_exit(
                        sell_mode, buy_price, day_bars, buy_t, sell_cut, fee_pct
                    )
                    if ret is not None:
                        equity *= 1 + ret / 100
                        midday_trades.append({
                            "date": day,
                            "etf": code,
                            "name": name,
                            "today_gain": round(gain, 2),
                            "signal": signal_t,
                            "buy": buy_t,
                            "sell_cutoff": sell_cut,
                            "return_pct": round(ret, 4),
                            "sell_reason": reason,
                        })

        if holding is not None:
            continue

        picked = live_picks.get((LIVE_SIGNAL, day))
        if not picked:
            continue
        code, gain, name = picked
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, LIVE_BUY)
        if not buy_price or buy_price <= 0:
            continue
        sell_day = _next_day(all_dates, day)
        if not sell_day:
            continue
        holding = {
            "code": code,
            "buy_price": buy_price,
            "buy_day": day,
            "signal_day": day,
            "sell_day": sell_day,
        }

    if holding:
        sell_day = holding["sell_day"]
        ret, reason, sell_time = _overnight_sell(
            holding["code"],
            holding["buy_price"],
            holding["buy_day"],
            sell_day,
            etf_5min,
            fee_pct,
        )
        equity *= 1 + ret / 100
        overnight_trades.append({
            "signal_date": holding["signal_day"],
            "sell_date": sell_day,
            "etf": holding["code"],
            "return_pct": round(ret, 4),
            "sell_reason": reason,
            "sell_time": sell_time,
        })

    o_rets = [t["return_pct"] for t in overnight_trades]
    m_rets = [t["return_pct"] for t in midday_trades]
    o_eq = 1.0
    for r in o_rets:
        o_eq *= 1 + r / 100
    m_eq = 1.0
    for r in m_rets:
        m_eq *= 1 + r / 100

    return {
        "final_equity_pct": (equity - 1) * 100,
        "overnight_only_pct": (o_eq - 1) * 100,
        "midday_only_pct": (m_eq - 1) * 100,
        "overnight_count": len(overnight_trades),
        "midday_count": len(midday_trades),
        "overnight_stats": _calc_stats(o_rets) if o_rets else {},
        "midday_stats": _calc_stats(m_rets) if m_rets else {},
        "overnight_trades": overnight_trades,
        "midday_trades": midday_trades,
    }


def _midday_grid() -> list[tuple[str, str, str, str]]:
    combos: list[tuple[str, str, str, str]] = []
    for sig, buy, cut in itertools.product(MID_SIGNAL_TIMES, MID_BUY_TIMES, MID_SELL_CUTOFFS):
        for mode in ("time", "trail", "fixed"):
            if not _valid_midday_combo(sig, buy, cut):
                continue
            combos.append((sig, buy, mode, cut))
    return combos


def print_chain_report(
    baseline: dict,
    with_mid: dict | None,
    label: str,
    eval_dates: list[str],
) -> None:
    print()
    print("=" * 92)
    print("  11:05~14:50 午间窗口 + 实盘下午基线（同一本金顺序复利）")
    print("=" * 92)
    print(f"  区间: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日) | 手续费万3双边")
    print()
    bo = baseline["overnight_stats"]
    print(
        f"  仅下午基线: 隔夜 {baseline['overnight_count']} 笔 | "
        f"累计 {baseline['overnight_only_pct']:+.2f}% | "
        f"胜率 {bo.get('win_rate', 0):.1f}% | 均笔 {bo.get('avg', 0):+.2f}%"
    )
    if with_mid:
        ms = with_mid["midday_stats"]
        print(
            f"  午间腿单独: {with_mid['midday_count']} 笔 | "
            f"累计 {with_mid['midday_only_pct']:+.2f}% | "
            f"胜率 {ms.get('win_rate', 0):.1f}% | 均笔 {ms.get('avg', 0):+.2f}%"
        )
        print(
            f"  叠加合计:   累计 {with_mid['final_equity_pct']:+.2f}% | "
            f"较基线 {with_mid['final_equity_pct'] - baseline['final_equity_pct']:+.2f} pp"
        )
        print(f"  方案: {label}")
    print("=" * 92)


def main() -> None:
    parser = argparse.ArgumentParser(description="11:05~14:50 闲置资金午间 T+0 回测")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--search", action="store_true", help="网格搜索午间组合")
    parser.add_argument("--report-best", action="store_true", help="搜索并打印 TOP10")
    parser.add_argument(
        "--combo",
        type=str,
        default="",
        help="sig,buy,mode,cut 如 13:30,13:35,time,14:45",
    )
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    print(f"=== 午间闲置窗口回测 ({args.days} 日) ===")
    etf_list = get_all_t0_etfs()
    etf_daily, etf_5min, all_dates, proxy_klines = load_market_data(etf_list, args.days)
    eval_dates = resolve_eval_dates(all_dates, args.days, "", "")
    if len(eval_dates) < 5:
        print("ERROR: 有效交易日不足")
        sys.exit(1)

    signal_times = list(set(MID_SIGNAL_TIMES + [LIVE_SIGNAL]))
    live_picks = precompute_picks(
        etf_list, etf_daily, etf_5min, eval_dates, [LIVE_SIGNAL],
        proxy_klines, use_filter=True, skip_choppy=True,
    )
    mid_picks = precompute_picks(
        etf_list, etf_daily, etf_5min, eval_dates, signal_times,
        proxy_klines, use_filter=True, skip_choppy=True,
    )

    baseline = run_capital_chain(
        eval_dates, all_dates, live_picks, mid_picks, etf_5min, args.fee, midday=None
    )
    baseline["final_equity_pct"] = baseline["overnight_only_pct"]

    if args.combo:
        parts = args.combo.split(",")
        if len(parts) != 4:
            print("ERROR: --combo 需要 sig,buy,mode,cut")
            sys.exit(1)
        midday = tuple(parts)  # type: ignore
        res = run_capital_chain(
            eval_dates, all_dates, live_picks, mid_picks, etf_5min, args.fee,
            midday=midday,
        )
        print_chain_report(baseline, res, args.combo, eval_dates)
        return

    if not args.search and not args.report_best:
        print_chain_report(baseline, None, "", eval_dates)
        print("\n提示: --search 或 --report-best 搜索午间正收益组合")
        return

    results: list[dict] = []
    grid = _midday_grid()
    print(f">>> 网格 {len(grid)} 组午间组合...")
    for i, (sig, buy, mode, cut) in enumerate(grid):
        midday = (sig, buy, mode, cut)
        res = run_capital_chain(
            eval_dates, all_dates, live_picks, mid_picks, etf_5min, args.fee,
            midday=midday,
        )
        if res["midday_count"] < MIN_MID_TRADES:
            continue
        ms = res["midday_stats"]
        results.append({
            "signal": sig,
            "buy": buy,
            "sell_mode": mode,
            "sell_cutoff": cut,
            "midday_count": res["midday_count"],
            "midday_only_pct": res["midday_only_pct"],
            "midday_win_rate": ms.get("win_rate", 0),
            "midday_avg": ms.get("avg", 0),
            "combined_pct": res["final_equity_pct"],
            "delta_vs_baseline": res["final_equity_pct"] - baseline["final_equity_pct"],
        })
        if (i + 1) % 200 == 0:
            print(f"    进度 {i+1}/{len(grid)}")

    results.sort(key=lambda x: (x["midday_only_pct"], x["delta_vs_baseline"]), reverse=True)

    positive = [r for r in results if r["midday_only_pct"] > 0 and r["midday_avg"] > 0]
    print()
    print(f"  基线(仅下午): {baseline['overnight_only_pct']:+.2f}% ({baseline['overnight_count']} 笔)")
    print(f"  有效午间组合: {len(results)} | 午间累计>0: {len(positive)}")
    print()
    print(f"  {'#':>3} {'信号':>6} {'买入':>6} {'卖法':>6} {'卖出≤':>6} "
          f"{'午间笔':>6} {'午间累计':>10} {'午间均笔':>8} {'叠加累计':>10} {'+pp':>8}")
    print("  " + "-" * 88)
    for i, r in enumerate(results[: args.top], 1):
        print(
            f"  {i:>3} {r['signal']:>6} {r['buy']:>6} {r['sell_mode']:>6} {r['sell_cutoff']:>6} "
            f"{r['midday_count']:>6} {r['midday_only_pct']:+9.2f}% {r['midday_avg']:+7.2f}% "
            f"{r['combined_pct']:+9.2f}% {r['delta_vs_baseline']:+7.2f}"
        )

    out = Path.home() / ".tradingagents" / "rotation" / (
        f"backtest_t0_midday_idle_{datetime.now():%Y%m%d_%H%M}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "config": {"days": args.days, "eval_dates": eval_dates, "window": "11:05~14:45"},
        "baseline": {k: v for k, v in baseline.items() if not k.endswith("_trades")},
        "top": results[:50],
        "positive_midday_count": len(positive),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")

    if results and args.report_best:
        best = results[0]
        combo = f"{best['signal']},{best['buy']},{best['sell_mode']},{best['sell_cutoff']}"
        print(f"\n>>> 最优组合详情: --combo {combo}")
        res = run_capital_chain(
            eval_dates, all_dates, live_picks, mid_picks, etf_5min, args.fee,
            midday=tuple(combo.split(",")),  # type: ignore
        )
        print_chain_report(baseline, res, combo, eval_dates)


if __name__ == "__main__":
    main()

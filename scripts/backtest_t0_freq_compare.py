#!/usr/bin/env python3
"""T+0 交易频率对比回测 — 固定本金、单仓位，比较提高频率是否放大收益。

对比方案（均含震荡跳过、涨幅≥3% TOP1、手续费万3双边）:
1. afternoon_only   — 实盘基线: 14:45/14:50 买 → 次日 TRIX(5,3) 09:40~11:05 卖
2. morning_afternoon — 上午加一单: 09:40/09:45 买 → 11:05 定时卖；下午仍走基线
3. day2_trix_multi   — 次日 TRIX 允许金叉再买/死叉再卖（日内多轮 T+0）
4. same_day_close    — 14:45/14:50 买 → 当日 15:00 前卖（纯日内 T+0）

用法:
    python scripts/backtest_t0_freq_compare.py --days 60
    python scripts/backtest_t0_freq_compare.py --days 100
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
from backtest_t0_day2_trix import simulate_day2_trix_t0  # noqa: E402
from backtest_t0_etf import apply_net_return, bar_time_min, price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    TRIX_MIN_SELL,
    TRIX_PERIOD,
    bars_for_trix,
    load_market_data,
    resolve_eval_dates,
    simulate_trix_cross_after,
)
from search_t0_time_combo import bars_until, precompute_picks, simulate_exit  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

AFTERNOON_SIGNAL = "14:45"
AFTERNOON_BUY = "14:50"
MORNING_SIGNAL = "09:40"
MORNING_BUY = "09:45"
MORNING_SELL = "11:05"
TRIX_SELL_CUTOFF = "11:05"


def _sell_overnight_trix(
    code: str,
    buy_price: float,
    buy_day: str,
    sell_day: str,
    etf_5min: dict,
    fee_pct: float,
) -> tuple[float, str, str]:
    day_bars = etf_5min.get(code, {}).get(buy_day, [])
    sell_bars = etf_5min.get(code, {}).get(sell_day, [])
    if not sell_bars:
        return 0.0, "no_bars", ""
    window = bars_until(sell_bars, TRIX_SELL_CUTOFF)
    if not window:
        return 0.0, "no_window", ""
    _, reason, detail = simulate_trix_cross_after(
        buy_price,
        bars_for_trix(day_bars),
        bars_for_trix(window),
        trix_period=TRIX_PERIOD,
        min_sell_time=TRIX_MIN_SELL,
    )
    sell_price = detail.get("sell_price") or float(window[-1]["close"])
    sell_time = detail.get("bar", TRIX_SELL_CUTOFF)
    ret = apply_net_return(buy_price, float(sell_price), fee_pct)
    return ret, reason, str(sell_time)


def _sell_same_day_timed(
    day_bars: list[dict],
    buy_time: str,
    sell_cutoff: str,
    buy_price: float,
    fee_pct: float,
) -> tuple[float, str] | None:
    window = bars_until(day_bars, sell_cutoff)
    idx = 0
    for i, b in enumerate(window):
        if bar_time_min(b) <= bar_time_min({"time": buy_time}):
            idx = i
    if not window or idx >= len(window):
        return None
    sell_price = float(window[-1]["close"])
    if sell_price <= 0:
        return None
    ret = apply_net_return(buy_price, sell_price, fee_pct)
    return ret, "time_sell"


def _next_day(all_dates: list[str], day: str) -> str | None:
    if day not in all_dates:
        return None
    idx = all_dates.index(day)
    if idx + 1 >= len(all_dates):
        return None
    return all_dates[idx + 1]


def run_afternoon_only(
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
) -> dict:
    """单仓位顺序复利：仅下午基线。"""
    equity = 1.0
    trades: list[dict] = []
    holding: dict | None = None

    for day in eval_dates:
        if holding and holding["sell_day"] == day:
            ret, reason, sell_time = _sell_overnight_trix(
                holding["code"],
                holding["buy_price"],
                holding["buy_day"],
                day,
                etf_5min,
                fee_pct,
            )
            equity *= 1 + ret / 100
            trades.append({
                "leg": "overnight",
                "signal_date": holding["signal_day"],
                "sell_date": day,
                "etf": holding["code"],
                "return_pct": round(ret, 4),
                "sell_reason": reason,
                "sell_time": sell_time,
            })
            holding = None

        if holding is not None:
            continue

        picked = picks.get((AFTERNOON_SIGNAL, day))
        if not picked:
            continue
        code, gain, name = picked
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, AFTERNOON_BUY)
        if not buy_price or buy_price <= 0:
            continue
        sell_day = _next_day(all_dates, day)
        if not sell_day:
            continue
        holding = {
            "code": code,
            "name": name,
            "gain": gain,
            "buy_price": buy_price,
            "buy_day": day,
            "signal_day": day,
            "sell_day": sell_day,
        }

    if holding:
        sell_day = holding["sell_day"]
        if sell_day in eval_dates or sell_day > eval_dates[-1]:
            ret, reason, sell_time = _sell_overnight_trix(
                holding["code"],
                holding["buy_price"],
                holding["buy_day"],
                sell_day,
                etf_5min,
                fee_pct,
            )
            equity *= 1 + ret / 100
            trades.append({
                "leg": "overnight",
                "signal_date": holding["signal_day"],
                "sell_date": sell_day,
                "etf": holding["code"],
                "return_pct": round(ret, 4),
                "sell_reason": reason,
                "sell_time": sell_time,
            })

    rets = [t["return_pct"] for t in trades]
    return _pack("afternoon_only", "下午基线(14:45→次日TRIX)", trades, rets, equity)


def run_morning_afternoon(
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
) -> dict:
    """上午动量 + 下午基线，单仓位顺序复利。"""
    equity = 1.0
    trades: list[dict] = []
    holding: dict | None = None

    for day in eval_dates:
        if holding and holding["sell_day"] == day:
            ret, reason, sell_time = _sell_overnight_trix(
                holding["code"],
                holding["buy_price"],
                holding["buy_day"],
                day,
                etf_5min,
                fee_pct,
            )
            equity *= 1 + ret / 100
            trades.append({
                "leg": "overnight",
                "signal_date": holding["signal_day"],
                "sell_date": day,
                "etf": holding["code"],
                "return_pct": round(ret, 4),
                "sell_reason": reason,
                "sell_time": sell_time,
            })
            holding = None

        if holding is None:
            picked = picks.get((MORNING_SIGNAL, day))
            if picked:
                code, gain, name = picked
                day_bars = etf_5min.get(code, {}).get(day, [])
                buy_price = price_at_time(day_bars, MORNING_BUY)
                if buy_price and buy_price > 0:
                    out = _sell_same_day_timed(
                        day_bars, MORNING_BUY, MORNING_SELL, buy_price, fee_pct
                    )
                    if out:
                        ret, reason = out
                        equity *= 1 + ret / 100
                        trades.append({
                            "leg": "morning",
                            "signal_date": day,
                            "sell_date": day,
                            "etf": code,
                            "today_gain": round(gain, 2),
                            "return_pct": round(ret, 4),
                            "sell_reason": reason,
                            "sell_time": MORNING_SELL,
                        })

        if holding is not None:
            continue

        picked = picks.get((AFTERNOON_SIGNAL, day))
        if not picked:
            continue
        code, gain, name = picked
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, AFTERNOON_BUY)
        if not buy_price or buy_price <= 0:
            continue
        sell_day = _next_day(all_dates, day)
        if not sell_day:
            continue
        holding = {
            "code": code,
            "name": name,
            "gain": gain,
            "buy_price": buy_price,
            "buy_day": day,
            "signal_day": day,
            "sell_day": sell_day,
        }

    if holding:
        sell_day = holding["sell_day"]
        ret, reason, sell_time = _sell_overnight_trix(
            holding["code"],
            holding["buy_price"],
            holding["buy_day"],
            sell_day,
            etf_5min,
            fee_pct,
        )
        equity *= 1 + ret / 100
        trades.append({
            "leg": "overnight",
            "signal_date": holding["signal_day"],
            "sell_date": sell_day,
            "etf": holding["code"],
            "return_pct": round(ret, 4),
            "sell_reason": reason,
            "sell_time": sell_time,
        })

    rets = [t["return_pct"] for t in trades]
    return _pack("morning_afternoon", "上午+下午(09:40+14:45)", trades, rets, equity)


def run_day2_trix_multi(
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
) -> dict:
    """下午买、次日 TRIX 多轮往返（单周期复利）。"""
    equity = 1.0
    trades: list[dict] = []

    for day in eval_dates:
        picked = picks.get((AFTERNOON_SIGNAL, day))
        if not picked:
            continue
        code, gain, name = picked
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, AFTERNOON_BUY)
        if not buy_price or buy_price <= 0:
            continue
        sell_day = _next_day(all_dates, day)
        if not sell_day:
            continue
        next_bars = etf_5min.get(code, {}).get(sell_day, [])
        if not next_bars:
            continue

        ret, actions, final_reason = simulate_day2_trix_t0(
            buy_price, day_bars, next_bars, fee_pct
        )
        equity *= 1 + ret / 100
        trades.append({
            "leg": "day2_multi",
            "signal_date": day,
            "sell_date": sell_day,
            "etf": code,
            "today_gain": round(gain, 2),
            "return_pct": round(ret, 4),
            "day2_actions": len(actions),
            "sell_reason": final_reason,
        })

    rets = [t["return_pct"] for t in trades]
    return _pack("day2_trix_multi", "次日TRIX多轮往返", trades, rets, equity)


def run_same_day_close(
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
) -> dict:
    """14:50 买、当日收盘卖 — 纯 T+0 日内。"""
    equity = 1.0
    trades: list[dict] = []

    for day in eval_dates:
        picked = picks.get((AFTERNOON_SIGNAL, day))
        if not picked:
            continue
        code, gain, name = picked
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, AFTERNOON_BUY)
        if not buy_price or buy_price <= 0:
            continue
        sell_price, sell_reason, timing = simulate_exit(
            "same_close", buy_price, day_bars, AFTERNOON_BUY, [], None
        )
        if sell_price is None or sell_price <= 0:
            continue
        ret = apply_net_return(buy_price, float(sell_price), fee_pct)
        equity *= 1 + ret / 100
        trades.append({
            "leg": "same_day",
            "signal_date": day,
            "sell_date": day,
            "etf": code,
            "today_gain": round(gain, 2),
            "return_pct": round(ret, 4),
            "sell_reason": sell_reason,
        })

    rets = [t["return_pct"] for t in trades]
    return _pack("same_day_close", "当日收盘卖(T+0)", trades, rets, equity)


def _pack(mode_id: str, label: str, trades: list[dict], rets: list[float], equity: float) -> dict:
    overnight = [t for t in trades if t.get("leg") == "overnight"]
    morning = [t for t in trades if t.get("leg") == "morning"]
    stats = _calc_stats(rets) if rets else {}
    return {
        "mode": mode_id,
        "label": label,
        "trade_count": len(trades),
        "overnight_legs": len(overnight),
        "morning_legs": len(morning),
        "final_equity_pct": (equity - 1) * 100,
        "stats": stats,
        "trades": trades,
    }


def print_report(results: list[dict], eval_dates: list[str], baseline: dict) -> None:
    print()
    print("=" * 96)
    print("  T+0 交易频率对比（单仓位顺序复利，本金 100% 滚动）")
    print("=" * 96)
    print(f"  区间: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 信号日) | 手续费万3双边")
    print()
    print(f"  {'方案':<28} {'总笔数':>6} {'其中上午':>8} {'累计':>10} {'胜率':>8} {'均笔':>8} {'回撤':>8}")
    print("  " + "-" * 88)
    for r in results:
        st = r.get("stats") or {}
        print(
            f"  {r['label']:<28} {r['trade_count']:>6} {r.get('morning_legs', 0):>8} "
            f"{r['final_equity_pct']:+9.2f}% {st.get('win_rate', 0):7.1f}% "
            f"{st.get('avg', 0):+7.2f}% {st.get('max_drawdown', 0):+7.2f}%"
        )

    base_ret = baseline["final_equity_pct"]
    print()
    print("  相对下午基线:")
    for r in results:
        diff = r["final_equity_pct"] - base_ret
        extra = r["trade_count"] - baseline["trade_count"]
        print(
            f"    {r['label']:<26} 累计 {diff:+.2f} pp | "
            f"多 {extra:+d} 笔 | 均笔 {r['stats'].get('avg', 0):+.2f}% "
            f"(基线均笔 {baseline['stats'].get('avg', 0):+.2f}%)"
        )

    if morning := [t for t in results[1].get("trades", []) if t.get("leg") == "morning"]:
        m_rets = [t["return_pct"] for t in morning]
        m_stats = _calc_stats(m_rets)
        print()
        print(f"  上午额外交易 ({len(morning)} 笔): 累计 {_compound(m_rets):+.2f}% | "
              f"胜率 {m_stats.get('win_rate', 0):.1f}% | 均笔 {m_stats.get('avg', 0):+.2f}%")

    print("=" * 96)


def _compound(rets: list[float]) -> float:
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return (eq - 1) * 100


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 交易频率对比回测")
    parser.add_argument("--days", type=int, default=60, help="回测交易日数")
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    args = parser.parse_args()

    print(f"=== T+0 交易频率对比回测 ({args.days} 日) ===")
    etf_list = get_all_t0_etfs()
    etf_daily, etf_5min, all_dates, proxy_klines = load_market_data(etf_list, args.days)
    eval_dates = resolve_eval_dates(all_dates, args.days, "", "")
    if len(eval_dates) < 5:
        print("ERROR: 有效交易日不足")
        sys.exit(1)

    picks = precompute_picks(
        etf_list,
        etf_daily,
        etf_5min,
        eval_dates,
        [AFTERNOON_SIGNAL, MORNING_SIGNAL],
        proxy_klines,
        use_filter=True,
        skip_choppy=True,
    )

    baseline = run_afternoon_only(eval_dates, all_dates, picks, etf_5min, args.fee)
    results = [
        baseline,
        run_morning_afternoon(eval_dates, all_dates, picks, etf_5min, args.fee),
        run_day2_trix_multi(eval_dates, all_dates, picks, etf_5min, args.fee),
        run_same_day_close(eval_dates, all_dates, picks, etf_5min, args.fee),
    ]

    print_report(results, eval_dates, baseline)

    out = Path.home() / ".tradingagents" / "rotation" / (
        f"backtest_t0_freq_compare_{datetime.now():%Y%m%d_%H%M}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "config": {"days": args.days, "eval_dates": eval_dates, "fee": args.fee},
        "results": [
            {k: v for k, v in r.items() if k != "trades"} for r in results
        ],
        "trades": {r["mode"]: r["trades"] for r in results},
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

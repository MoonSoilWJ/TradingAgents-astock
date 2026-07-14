#!/usr/bin/env python3
"""T+0 ETF 滑动窗口买卖时间网格搜索。

在固定规则下搜索最优「信号时间 × 买入时间 × 卖出策略」组合：
- 选股：信号时刻相对昨收当日涨幅 TOP1（可选 ≥MIN_GAIN 过滤）
- 过滤：501018 震荡期跳过（可选）
- 卖出：次日定时 / TRIX 死叉 / 追踪止盈 / 当日 T+0 收盘 等

约束（与板块 search_time_combo 一致）：
- 买入时间 > 信号时间，且同一交易时段（上午或下午）
- 隔夜卖出时：次日卖出截止时刻 < 当日买入时刻（便于日循环）

用法:
    # 100 天全网格，输出 TOP 30
    python scripts/search_t0_time_combo.py --days 100 --top 30

    # 指定日期区间
    python scripts/search_t0_time_combo.py --start-date 2026-02-10 --end-date 2026-07-14

    # 单组合验证 + 分 3 段
    python scripts/search_t0_time_combo.py --days 100 --combo 14:45,14:50,trix0940_cut,11:05 --segments

    # 对比当前实盘基线
    python scripts/search_t0_time_combo.py --days 100 --baseline-only
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
from backtest_top1_intraday import check_sell_trigger  # noqa: E402
from backtest_t0_etf import bar_time_min, price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    MIN_GAIN,
    TRIX_MIN_SELL,
    TRIX_PERIOD,
    apply_net_return,
    bars_for_trix,
    load_market_data,
    rank_by_today_gain,
    regime_on_date,
    resolve_eval_dates,
    select_etf,
    simulate_trix_cross_after,
    time_to_min,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

# 默认滑动窗口（每 15 分钟一档，跳过午休）
DEFAULT_SIGNAL_TIMES = [
    "09:40", "09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15",
    "13:00", "13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45", "14:50",
]
DEFAULT_BUY_TIMES = [
    "09:45", "09:50", "10:05", "10:20", "10:35", "10:50", "11:05", "11:20",
    "13:05", "13:20", "13:35", "13:50", "14:05", "14:20", "14:35", "14:50", "14:55",
]
DEFAULT_SELL_CUTOFFS = [
    "09:35", "09:50", "10:05", "10:20", "10:35", "10:50", "11:05", "11:20",
    "13:00", "13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45", "15:00",
]

BASELINE = {
    "signal": "14:50",
    "buy": "14:55",
    "sell_mode": "trix_0940",
    "sell_cutoff": None,
    "label": "当前实盘(14:50/14:55/TRIX≥09:40)",
}

MIN_TRADES = 10


def same_session(signal_time: str, buy_time: str) -> bool:
    sig_am = time_to_min(signal_time) < time_to_min("11:30")
    buy_am = time_to_min(buy_time) < time_to_min("11:30")
    return sig_am == buy_am


def bars_until(bars: list[dict], cutoff: str) -> list[dict]:
    cm = time_to_min(cutoff)
    return [b for b in bars if bar_time_min(b) <= cm]


def buy_bar_idx(bars: list[dict], buy_time: str) -> int:
    bm = time_to_min(buy_time)
    idx = 0
    for i, b in enumerate(bars):
        if bar_time_min(b) <= bm:
            idx = i
    return idx


def simulate_exit(
    sell_mode: str,
    buy_price: float,
    day_bars: list[dict],
    buy_time: str,
    next_bars: list[dict],
    sell_cutoff: str | None = None,
) -> tuple[float | None, str]:
    """返回 (sell_price, sell_reason)。"""
    if sell_mode == "same_close":
        if not day_bars:
            return None, ""
        return float(day_bars[-1]["close"]), "same_close"

    if sell_mode == "same_trail":
        if not day_bars:
            return None, ""
        idx = buy_bar_idx(day_bars, buy_time)
        sp, reason, _ = check_sell_trigger(
            day_bars, buy_price, idx, stop_loss_pct=-1.5,
            trail_trigger_pct=2.0, trail_drop_pct=0.5,
        )
        return sp, reason

    if not next_bars:
        return None, ""

    window = bars_until(next_bars, sell_cutoff) if sell_cutoff else next_bars
    if not window:
        return None, ""

    if sell_mode == "time":
        return float(window[-1]["close"]), "time_sell"

    if sell_mode == "trail":
        sp, reason, _ = check_sell_trigger(
            window, buy_price, 0, stop_loss_pct=-1.5,
            trail_trigger_pct=2.0, trail_drop_pct=0.5,
        )
        return sp, reason

    if sell_mode == "fixed":
        tp, sl = buy_price * 1.03, buy_price * 0.985
        for b in window:
            if float(b["low"]) <= sl:
                return sl, "stop_loss"
            if float(b["high"]) >= tp:
                return tp, "take_profit"
        return float(window[-1]["close"]), "close"

    min_sell = TRIX_MIN_SELL if sell_mode in ("trix_0940", "trix0940_cut") else "09:30"
    _, reason, detail = simulate_trix_cross_after(
        buy_price,
        bars_for_trix(day_bars),
        bars_for_trix(window),
        trix_period=TRIX_PERIOD,
        min_sell_time=min_sell,
    )
    sp = detail.get("sell_price")
    if sp is None:
        sp = float(window[-1]["close"])
        reason = "close"
    return float(sp), reason


def sell_mode_label(sell_mode: str, sell_cutoff: str | None) -> str:
    labels = {
        "time": "定时卖",
        "trix": "TRIX死叉(次日全天)",
        "trix_0940": "TRIX≥09:40(次日全天)",
        "trix_cut": "TRIX死叉",
        "trix0940_cut": "TRIX≥09:40",
        "trail": "追踪止盈",
        "fixed": "固定+3%/-1.5%",
        "same_close": "当日收盘卖(T+0)",
        "same_trail": "当日追踪卖(T+0)",
    }
    base = labels.get(sell_mode, sell_mode)
    if sell_cutoff and sell_mode not in ("same_close", "same_trail"):
        return f"{base}≤{sell_cutoff}"
    return base


def precompute_picks(
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    eval_dates: list[str],
    signal_times: list[str],
    proxy_klines: list[dict],
    use_filter: bool,
    skip_choppy: bool,
) -> dict[tuple[str, str], tuple[str, float, str] | None]:
    picks: dict[tuple[str, str], tuple[str, float, str] | None] = {}
    for sig in signal_times:
        for day in eval_dates:
            if skip_choppy:
                regime = regime_on_date(proxy_klines, day)
                if regime and regime.get("skip_choppy"):
                    picks[(sig, day)] = None
                    continue
            scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, sig)
            if len(scores) < 2:
                picks[(sig, day)] = None
                continue
            picked = select_etf(scores, use_filter)
            if not picked:
                picks[(sig, day)] = None
                continue
            gain, info = picked
            picks[(sig, day)] = (info["code"], gain, info["name"])
    return picks


def run_combo(
    signal_time: str,
    buy_time: str,
    sell_mode: str,
    sell_cutoff: str | None,
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
) -> dict | None:
    rets: list[float] = []
    trades: list[dict] = []
    for day in eval_dates:
        picked = picks.get((signal_time, day))
        if not picked:
            continue
        code, gain, name = picked
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, buy_time)
        if not buy_price or buy_price <= 0:
            continue

        if sell_mode in ("same_close", "same_trail"):
            next_bars: list[dict] = []
        else:
            if day not in all_dates:
                continue
            idx = all_dates.index(day)
            if idx + 1 >= len(all_dates):
                continue
            next_day = all_dates[idx + 1]
            next_bars = etf_5min.get(code, {}).get(next_day, [])
            if not next_bars:
                continue

        sell_price, sell_reason = simulate_exit(
            sell_mode, buy_price, day_bars, buy_time, next_bars, sell_cutoff,
        )
        if sell_price is None or sell_price <= 0:
            continue
        ret = apply_net_return(buy_price, sell_price, fee_pct)
        rets.append(ret)
        trades.append({
            "signal_date": day,
            "etf": code,
            "name": name,
            "today_gain": round(gain, 2),
            "buy_price": round(buy_price, 4),
            "sell_price": round(sell_price, 4),
            "sell_reason": sell_reason,
            "return_pct": ret,
        })

    if len(rets) < MIN_TRADES:
        return None

    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    stats = _calc_stats(rets)
    return {
        "signal": signal_time,
        "buy": buy_time,
        "sell_mode": sell_mode,
        "sell_cutoff": sell_cutoff,
        "label": sell_mode_label(sell_mode, sell_cutoff),
        "trade_count": len(rets),
        "final_equity_pct": (eq - 1) * 100,
        "stats": stats,
        "trades": trades,
    }


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


def iter_combos(
    signal_times: list[str],
    buy_times: list[str],
    sell_cutoffs: list[str],
    include_same_day: bool = True,
) -> list[tuple[str, str, str, str | None]]:
    combos: list[tuple[str, str, str, str | None]] = []
    for sig in signal_times:
        for buy in buy_times:
            if time_to_min(buy) <= time_to_min(sig):
                continue
            if not same_session(sig, buy):
                continue
            if time_to_min(buy) > time_to_min("14:55"):
                continue

            for cutoff in sell_cutoffs:
                if time_to_min(cutoff) >= time_to_min(buy):
                    continue
                for mode in ("time", "trix_cut", "trix0940_cut", "trail"):
                    combos.append((sig, buy, mode, cutoff))

            for mode in ("trix", "trix_0940", "trail", "fixed"):
                combos.append((sig, buy, mode, None))

            if include_same_day:
                for mode in ("same_close", "same_trail"):
                    combos.append((sig, buy, mode, None))
    return combos


def print_top_results(
    results: list[dict],
    top: int,
    eval_dates: list[str],
    picks: dict,
    etf_5min: dict,
    all_dates: list[str],
    fee_pct: float,
):
    print("=" * 115)
    print(f"  T+0 时间组合 TOP {top}（按累计收益）")
    print("=" * 115)
    print(f"  {'#':>3} {'信号':>6} {'买入':>6} {'卖出策略':<28} {'笔数':>4} {'累计':>9} {'胜率':>6} {'均笔':>7} {'回撤':>8}")
    print("  " + "─" * 105)
    for i, r in enumerate(results[:top], 1):
        st = r["stats"]
        print(
            f"  {i:>3} {r['signal']:>6} {r['buy']:>6} {r['label']:<28} {r['trade_count']:>4} "
            f"{r['final_equity_pct']:+8.2f}% {st.get('win_rate', 0):>5.1f}% "
            f"{st.get('avg', 0):>+6.2f}% {st.get('max_drawdown', 0):>+7.2f}%"
        )
    print("=" * 115)

    if len(eval_dates) >= 9 and results:
        seg_size = len(eval_dates) // 3
        segs = [
            ("前期", eval_dates[:seg_size]),
            ("中期", eval_dates[seg_size: 2 * seg_size]),
            ("后期", eval_dates[2 * seg_size:]),
        ]
        print("\n  TOP 3 分 3 段（独立起算）:")
        print(f"  {'方案':<36} {'100天':>8} | {'前期':>8} {'中期':>8} {'后期':>8}")
        for r in results[:3]:
            detail = run_combo(
                r["signal"], r["buy"], r["sell_mode"], r["sell_cutoff"],
                eval_dates, all_dates, picks, etf_5min, fee_pct,
            )
            trades = detail["trades"] if detail else []
            parts = [segment_stats(trades, ds)["total"] for _, ds in segs]
            name = f"{r['signal']}/{r['buy']} {r['label'][:18]}"
            print(
                f"  {name:<36} {r['final_equity_pct']:+7.2f}% | "
                f"{parts[0]:+7.2f}% {parts[1]:+7.2f}% {parts[2]:+7.2f}%"
            )


def parse_combo(s: str) -> tuple[str, str, str, str | None]:
    """格式: signal,buy,sell_mode[,sell_cutoff]"""
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 3:
        raise ValueError("combo 格式: signal,buy,sell_mode[,sell_cutoff]")
    cutoff = parts[3] if len(parts) > 3 and parts[3] else None
    return parts[0], parts[1], parts[2], cutoff


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 ETF 买卖时间网格搜索")
    parser.add_argument("--days", type=int, default=100, help="回测交易日数")
    parser.add_argument("--start-date", type=str, default="", help="起始日 YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, default="", help="结束日 YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=30, help="显示前 N 个组合")
    parser.add_argument("--fee", type=float, default=FEE_PCT, help="单边手续费(万3=0.03)")
    parser.add_argument("--skip-choppy", dest="skip_choppy", action="store_true", default=True,
                        help="501018震荡期跳过(默认开启)")
    parser.add_argument("--no-skip-choppy", dest="skip_choppy", action="store_false",
                        help="关闭震荡跳过")
    parser.add_argument("--no-filter", action="store_true", help="关闭涨幅≥3%%过滤")
    parser.add_argument("--no-same-day", action="store_true", help="不搜索当日T+0卖出")
    parser.add_argument("--combo", type=str, default="",
                        help="单组合: signal,buy,sell_mode[,cutoff] 如 14:45,14:50,trix0940_cut,11:05")
    parser.add_argument("--segments", action="store_true", help="单组合模式下输出分3段")
    parser.add_argument("--baseline-only", action="store_true", help="仅跑当前实盘基线")
    parser.add_argument("--save-all", action="store_true", help="保存全部组合(默认只存TOP)")
    args = parser.parse_args()

    skip_choppy = args.skip_choppy
    use_filter = not args.no_filter

    print("=== T+0 ETF 买卖时间网格搜索 ===")
    print(f"过滤: 涨幅≥{MIN_GAIN}%={'是' if use_filter else '否'} | 震荡跳过={'是' if skip_choppy else '否'}")
    print(f"手续费: 万{args.fee * 100:.0f}")
    print()

    etf_list = get_all_t0_etfs()
    lookback = args.days if not (args.start_date or args.end_date) else max(args.days, 280)
    etf_daily, etf_5min, all_dates, proxy_klines = load_market_data(etf_list, lookback)
    eval_dates = resolve_eval_dates(all_dates, args.days, args.start_date, args.end_date)
    if len(eval_dates) < 5:
        print("ERROR: 有效交易日不足")
        sys.exit(1)
    print(f"回测 {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日)\n")

    signal_times = DEFAULT_SIGNAL_TIMES
    picks = precompute_picks(
        etf_list, etf_daily, etf_5min, eval_dates, signal_times,
        proxy_klines, use_filter, skip_choppy,
    )

    if args.combo:
        sig, buy, mode, cutoff = parse_combo(args.combo)
        r = run_combo(sig, buy, mode, cutoff, eval_dates, all_dates, picks, etf_5min, args.fee)
        if not r:
            print("ERROR: 组合无效或交易笔数不足")
            sys.exit(1)
        st = r["stats"]
        print(f"组合: {sig} 信号 | {buy} 买入 | {r['label']}")
        print(f"笔数: {r['trade_count']} | 累计: {r['final_equity_pct']:+.2f}%")
        print(f"胜率: {st.get('win_rate', 0):.1f}% | 均笔: {st.get('avg', 0):+.2f}% | 回撤: {st.get('max_drawdown', 0):+.2f}%")
        if args.segments and len(eval_dates) >= 9:
            seg_size = len(eval_dates) // 3
            for name, ds in [
                ("前期", eval_dates[:seg_size]),
                ("中期", eval_dates[seg_size: 2 * seg_size]),
                ("后期", eval_dates[2 * seg_size:]),
            ]:
                s = segment_stats(r["trades"], ds)
                print(f"  {name}: {s['total']:+.2f}% ({s['count']}笔)")
        sys.exit(0)

    if args.baseline_only:
        b = BASELINE
        r = run_combo(
            b["signal"], b["buy"], b["sell_mode"], b["sell_cutoff"],
            eval_dates, all_dates, picks, etf_5min, args.fee,
        )
        if r:
            st = r["stats"]
            print(f"{b['label']}: {r['final_equity_pct']:+.2f}% | {r['trade_count']}笔 | 回撤{st.get('max_drawdown', 0):+.2f}%")
        sys.exit(0)

    combos = iter_combos(
        DEFAULT_SIGNAL_TIMES, DEFAULT_BUY_TIMES, DEFAULT_SELL_CUTOFFS,
        include_same_day=not args.no_same_day,
    )
    print(f">>> 搜索 {len(combos)} 种组合...")
    results: list[dict] = []
    for sig, buy, mode, cutoff in combos:
        r = run_combo(sig, buy, mode, cutoff, eval_dates, all_dates, picks, etf_5min, args.fee)
        if r:
            # 列表模式不需要存全量 trades，减小内存
            r_light = {k: v for k, v in r.items() if k != "trades"}
            results.append(r_light)

    results.sort(key=lambda x: x["final_equity_pct"], reverse=True)
    print(f"    有效组合: {len(results)}\n")
    print_top_results(results, args.top, eval_dates, picks, etf_5min, all_dates, args.fee)

    # 基线排名
    bl = run_combo(
        BASELINE["signal"], BASELINE["buy"], BASELINE["sell_mode"], BASELINE["sell_cutoff"],
        eval_dates, all_dates, picks, etf_5min, args.fee,
    )
    if bl:
        rank = 1 + next(
            (i for i, r in enumerate(results) if r["final_equity_pct"] <= bl["final_equity_pct"]),
            len(results),
        )
        print(f"\n  当前实盘基线: {bl['final_equity_pct']:+.2f}% | 排名 #{rank}/{len(results)}")

    if results:
        best = results[0]
        print(f"\n  ★ 累计最优: {best['signal']} 买{best['buy']} | {best['label']}")
        print(f"    累计{best['final_equity_pct']:+.2f}% | {best['trade_count']}笔 | "
              f"夏普{best['stats'].get('sharpe', 0):.2f}")

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
            "combos_searched": len(combos),
            "combos_valid": len(results),
        },
        "baseline": bl,
        "top": results[: args.top],
    }
    if args.save_all:
        payload["all"] = results
    out_path = out_dir / f"search_t0_time_{tag}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""板块轮动 + 14:45/14:50 买 + 次日 TRIX(5,3) 卖（09:40~11:05）。

选股:
- t0（默认）: 板块池内当日涨幅≥3% TOP1 + 501018 震荡跳过（与 T+0 选股逻辑一致）
- v6: 平安板块池 v6 partial 得分 TOP1

卖出: 5分/1分 TRIX(5,3) 死叉≥09:40，截止 11:05；无死叉 11:05 定时卖

用法:
    python scripts/backtest_rotation_day2_trix.py --days 30
    python scripts/backtest_rotation_day2_trix.py --days 30 --compare   # 板块池 vs T+0池
    python scripts/backtest_rotation_day2_trix.py --days 30 --pick v6
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_rotation_8way import load_market_data as load_sector_market_data, rank_top1, split_eval_periods, summarize_trades  # noqa: E402
from backtest_t0_day2_trix import fetch_1min_kline_sina  # noqa: E402
from backtest_t0_etf import price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    MIN_GAIN,
    apply_net_return,
    load_market_data as load_t0_market_data,
    resolve_eval_dates,
)
from backtest_top1 import _calc_stats, compute_daily_data, fetch_sina_kline  # noqa: E402
from search_t0_time_combo import precompute_picks, segment_stats, simulate_exit  # noqa: E402
from sector_etf_map import etf_to_sina_symbol, load_pingan_sectors  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402
from t0_regime import REGIME_PROXY  # noqa: E402

SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
SELL_CUTOFF = "11:05"
TRIX_PERIOD = 5
TRIX_SIGNAL = 3
MIN_TRADES = 3


def sectors_to_etf_list(sectors: list[dict]) -> list[dict]:
    """板块池转 T+0 选股用的 etf_list 格式。"""
    seen: set[str] = set()
    out: list[dict] = []
    for s in sectors:
        code = s["etf_code"]
        if code in seen:
            continue
        seen.add(code)
        out.append({
            "code": code,
            "name": s["name"],
            "sina_symbol": etf_to_sina_symbol(s["etf_raw"]),
        })
    return out


def load_proxy_klines(datalen: int = 60) -> list[dict]:
    return fetch_sina_kline(f"sh{REGIME_PROXY}", datalen=datalen)


def build_picks_v6(
    sectors: list[dict],
    eval_dates: list[str],
    etf_daily: dict,
    etf_bars: dict,
) -> dict[tuple[str, str], tuple[str, float, str, str] | None]:
    """v6 得分 TOP1。返回 (code, score_or_gain, sector_name, etf_name)。"""
    picks: dict[tuple[str, str], tuple[str, float, str, str] | None] = {}
    for day in eval_dates:
        top1 = rank_top1(sectors, etf_daily, etf_bars, day, SIGNAL_TIME)
        if not top1:
            picks[(SIGNAL_TIME, day)] = None
            continue
        picks[(SIGNAL_TIME, day)] = (
            top1["etf_code"], 0.0, top1["name"], top1.get("etf_name", top1["etf_code"]),
        )
    return picks


def build_picks_t0(
    etf_list: list[dict],
    code_to_label: dict[str, str],
    eval_dates: list[str],
    etf_daily: dict,
    etf_bars: dict,
    proxy_klines: list[dict],
    skip_choppy: bool,
) -> dict[tuple[str, str], tuple[str, float, str, str] | None]:
    """T+0 式：涨幅≥3% TOP1 + 震荡跳过。返回 (code, gain, label, etf_name)。"""
    raw_picks = precompute_picks(
        etf_list, etf_daily, etf_bars, eval_dates, [SIGNAL_TIME],
        proxy_klines, use_filter=True, skip_choppy=skip_choppy,
    )
    picks: dict[tuple[str, str], tuple[str, float, str, str] | None] = {}
    for key, val in raw_picks.items():
        if not val:
            picks[key] = None
            continue
        code, gain, name = val
        picks[key] = (code, gain, code_to_label.get(code, name), name)
    return picks


def load_sector_1min(sectors: list[dict]) -> tuple[dict, dict, list[str]]:
    """板块池 ETF 日K + 1分K。"""
    etf_codes = sorted({s["etf_code"] for s in sectors})
    etf_daily: dict = {}
    etf_1min: dict = {}
    print(f">>> 拉取 {len(etf_codes)} 只板块 ETF 日K + 1分K...")
    for i, etf in enumerate(etf_codes):
        raw = next(s["etf_raw"] for s in sectors if s["etf_code"] == etf)
        sym = etf_to_sina_symbol(raw)
        daily = fetch_sina_kline(sym, datalen=60)
        if daily and len(daily) > 4:
            etf_daily[etf] = {"returns": compute_daily_data(daily)}
        bars = fetch_1min_kline_sina(sym)
        if bars:
            etf_1min[etf] = bars
        if (i + 1) % 15 == 0:
            print(f"    进度 {i+1}/{len(etf_codes)} 日K={len(etf_daily)} 1分K={len(etf_1min)}")
        time.sleep(0.2)
    m1_dates = sorted({d for bars in etf_1min.values() for d in bars})
    daily_dates = sorted({r["date"] for info in etf_daily.values() for r in info["returns"]})
    all_dates = sorted(set(m1_dates) | set(daily_dates))
    print(f"    完成: 1分K {len(etf_1min)}/{len(etf_codes)} | {m1_dates[0] if m1_dates else '?'} ~ {m1_dates[-1] if m1_dates else '?'}")
    return etf_daily, etf_1min, all_dates


def run_backtest(
    picks: dict[tuple[str, str], tuple[str, float, str, str] | None],
    eval_dates: list[str],
    all_dates: list[str],
    etf_bars: dict,
    fee_pct: float,
    min_trades: int = MIN_TRADES,
) -> dict | None:
    rets: list[float] = []
    trades: list[dict] = []

    for day in eval_dates:
        picked = picks.get((SIGNAL_TIME, day))
        if not picked:
            continue
        code, gain, label, etf_name = picked
        day_bars = etf_bars.get(code, {}).get(day, [])
        buy_price = price_at_time(day_bars, BUY_TIME)
        if not buy_price or buy_price <= 0:
            continue

        if day not in all_dates:
            continue
        idx = all_dates.index(day)
        if idx + 1 >= len(all_dates):
            continue
        next_day = all_dates[idx + 1]
        next_bars = etf_bars.get(code, {}).get(next_day, [])
        if not next_bars:
            continue

        sell_price, sell_reason = simulate_exit(
            "trix0940_cut",
            buy_price,
            day_bars,
            BUY_TIME,
            next_bars,
            SELL_CUTOFF,
            trix_period=TRIX_PERIOD,
            trix_signal_period=TRIX_SIGNAL,
        )
        if sell_price is None or sell_price <= 0:
            continue

        ret = apply_net_return(buy_price, sell_price, fee_pct)
        rets.append(ret)
        trades.append({
            "signal_date": day,
            "sell_date": next_day,
            "sector": label,
            "etf": code,
            "etf_name": etf_name,
            "today_gain": round(gain, 2),
            "buy_price": round(buy_price, 4),
            "sell_price": round(sell_price, 4),
            "return_pct": round(ret, 2),
            "sell_reason": sell_reason,
        })

    if len(rets) < min_trades:
        return None

    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {
        "trade_count": len(rets),
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets),
        "trades": trades,
    }


def print_report(
    result: dict,
    eval_dates: list[str],
    bar_label: str,
    pool_name: str,
    pick_mode: str,
):
    pick_desc = (
        f"当日涨幅≥{MIN_GAIN}% TOP1 + 震荡跳过 @ {SIGNAL_TIME}"
        if pick_mode == "t0"
        else f"v6 partial 得分 TOP1 @ {SIGNAL_TIME}"
    )
    print()
    print("=" * 96)
    print(f"  {pool_name} + {SIGNAL_TIME}/{BUY_TIME}买 + 次日{bar_label} TRIX(5,3) 卖")
    print("=" * 96)
    print(f"  选股: {pick_desc} | 买入: {BUY_TIME} 直买")
    print(f"  卖出: {bar_label}K TRIX({TRIX_PERIOD},{TRIX_SIGNAL}) 死叉≥09:40，截止 {SELL_CUTOFF}；无死叉定时卖")
    print(f"  区间: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日) | 手续费万3双边")
    st = result["stats"]
    print(f"\n  笔数: {result['trade_count']} | 累计: {result['final_equity_pct']:+.2f}%")
    print(f"  胜率: {st.get('win_rate', 0):.1f}% | 均笔: {st.get('avg', 0):+.2f}% | "
          f"回撤: {st.get('max_drawdown', 0):+.2f}% | 夏普: {st.get('sharpe', 0):.2f}")

    from collections import Counter
    print(f"  卖出原因: {dict(Counter(t['sell_reason'] for t in result['trades']))}")

    print(f"\n  {'信号日':>12} {'次日':>12} {'标的':14s} {'ETF':>8} {'涨%':>6} {'买价':>8} {'卖价':>8} {'收益':>8} {'原因':>14}")
    print("  " + "-" * 96)
    eq = 1.0
    for t in result["trades"]:
        eq *= 1 + t["return_pct"] / 100
        print(
            f"  {t['signal_date']:>12} {t['sell_date']:>12} {t['sector'][:14]:14s} {t['etf']:>8} "
            f"{t.get('today_gain', 0):+5.1f}% {t['buy_price']:8.4f} {t['sell_price']:8.4f} "
            f"{t['return_pct']:+7.2f}% {t['sell_reason']:>14} | 累计 {(eq-1)*100:+7.2f}%"
        )
    print("=" * 96)


def print_compare(
    rotation: dict,
    t0: dict,
    eval_dates: list[str],
    bar_label: str,
    sector_pick: str = "v6",
):
    sec_pick_desc = "v6得分TOP1" if sector_pick == "v6" else f"涨幅≥{MIN_GAIN}% TOP1"
    print()
    print("=" * 96)
    print(f"  30 天对比：板块池({sec_pick_desc}) vs T+0池(涨幅TOP1)")
    print(f"  卖出: {SIGNAL_TIME}/{BUY_TIME}买 + TRIX≥09:40≤{SELL_CUTOFF} | {bar_label}K")
    print("=" * 96)
    print(f"  区间: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 信号日)")
    print()
    print(f"  {'池':12s} {'选股':10s} {'笔数':>6} {'累计':>10} {'胜率':>8} {'均笔':>8} {'回撤':>8} {'夏普':>8}")
    print("  " + "-" * 72)
    for label, pick, r in [
        ("板块池", sec_pick_desc[:10], rotation),
        ("T+0池", "涨幅TOP1", t0),
    ]:
        st = r["stats"]
        print(
            f"  {label:12s} {pick:10s} {r['trade_count']:>6} {r['final_equity_pct']:+9.2f}% "
            f"{st.get('win_rate', 0):7.1f}% {st.get('avg', 0):+7.2f}% "
            f"{st.get('max_drawdown', 0):+7.2f}% {st.get('sharpe', 0):7.2f}"
        )
    diff = rotation["final_equity_pct"] - t0["final_equity_pct"]
    print(f"\n  板块池 vs T+0池 累计差: {diff:+.2f} pp")
    print("=" * 96)


def print_segments(
    rotation: dict,
    t0: dict,
    eval_dates: list[str],
    sector_pick: str = "v6",
):
    segs = split_eval_periods(eval_dates, 3)
    if len(segs) < 3:
        return
    sec_pick_desc = "v6" if sector_pick == "v6" else "涨幅TOP1"
    print()
    print("=" * 96)
    print(f"  100 天分 3 段对比（各段独立起算）| 板块池({sec_pick_desc}) vs T+0池(涨幅TOP1)")
    print("=" * 96)
    hdr = f"  {'池':12s} {'100天':>8}"
    for s in segs:
        hdr += f" | {s['label']:>4}({s['start'][5:]}~{s['end'][5:]})"
    print(hdr)
    print("  " + "-" * 88)
    for label, trades in [("板块池", rotation["trades"]), ("T+0池", t0["trades"])]:
        full = summarize_trades(trades)
        row = f"  {label:12s} {full['equity_pct']:+7.2f}%"
        for s in segs:
            seg = summarize_trades(trades, set(s["dates"]))
            row += f" | {seg['equity_pct']:+7.2f}%({seg['trade_count']:>2}笔)"
        print(row)
    print()
    print(f"  {'段':6s} {'板块池':>10} {'T+0池':>10} {'差':>10} {'板块笔':>6} {'T+0笔':>6}")
    print("  " + "-" * 56)
    for s in segs:
        ds = set(s["dates"])
        rs = summarize_trades(rotation["trades"], ds)
        rt = summarize_trades(t0["trades"], ds)
        diff = rs["equity_pct"] - rt["equity_pct"]
        print(
            f"  {s['label']:6s} {rs['equity_pct']:+9.2f}% {rt['equity_pct']:+9.2f}% "
            f"{diff:+9.2f}pp {rs['trade_count']:>6} {rt['trade_count']:>6}"
        )
    print("=" * 96)


def resolve_eval_dates_for_bars(all_dates: list[str], days: int, bar: str, etf_bars: dict) -> list[str]:
    if bar == "1min":
        m_dates = sorted({d for bars in etf_bars.values() for d in bars})
        eval_dates = m_dates[-days:] if days else m_dates
    else:
        eval_dates = all_dates[-days:] if days else all_dates
    return eval_dates[:-1] if len(eval_dates) > 1 else eval_dates


def main() -> None:
    parser = argparse.ArgumentParser(description="板块轮动 T+0 同款选股/卖出回测")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--bar", choices=["5min", "1min"], default="5min")
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--pick", choices=["t0", "v6"], default="t0", help="选股方式（默认 t0 当日涨幅TOP1）")
    parser.add_argument("--compare", action="store_true", help="对比板块池 vs T+0 ETF池（同策略）")
    parser.add_argument("--segments", action="store_true", help="对比模式下输出分3段")
    parser.add_argument("--no-skip-choppy", dest="skip_choppy", action="store_false", default=True)
    args = parser.parse_args()

    bar_label = "1分" if args.bar == "1min" else "5分"
    pick_label = "当日涨幅TOP1" if args.pick == "t0" else "v6得分TOP1"
    print(f"=== 板块轮动 {pick_label} + 次日 {bar_label} TRIX(09:40~{SELL_CUTOFF}) 回测 ===")

    proxy_klines = load_proxy_klines(datalen=args.days + 30)
    min_trades = 2 if args.days <= 8 else MIN_TRADES

    if args.compare:
        # --- 板块池 ---
        sectors = load_pingan_sectors()
        print(f"\n[1/2] 板块池: {len(sectors)} 个")
        if args.bar == "1min":
            sec_daily, sec_bars, sec_all = load_sector_1min(sectors)
        else:
            sec_daily, sec_bars, sec_all = load_sector_market_data(sectors, args.days)

        # --- T+0 池 ---
        t0_list = get_all_t0_etfs()
        print(f"\n[2/2] T+0 ETF池: {len(t0_list)} 只")
        if args.bar == "1min":
            from backtest_t0_day2_trix import load_1min_market_data
            t0_daily, t0_bars, t0_all, t0_proxy = load_1min_market_data(t0_list)
            proxy_klines = t0_proxy or proxy_klines
        else:
            t0_daily, t0_bars, t0_all, t0_proxy = load_t0_market_data(t0_list, args.days)
            proxy_klines = t0_proxy or proxy_klines

        common_dates = sorted(set(sec_all) & set(t0_all))
        eval_dates = resolve_eval_dates(common_dates, args.days, "", "")
        eval_dates = eval_dates[:-1] if len(eval_dates) > 1 else eval_dates
        if len(eval_dates) < min_trades:
            print("ERROR: 有效交易日不足")
            sys.exit(1)
        print(f"\n共同信号日: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日)")

        if args.pick == "v6":
            sec_picks = build_picks_v6(sectors, eval_dates, sec_daily, sec_bars)
        else:
            sec_etf_list = sectors_to_etf_list(sectors)
            sec_labels = {s["etf_code"]: s["name"] for s in sectors}
            sec_picks = build_picks_t0(
                sec_etf_list, sec_labels, eval_dates, sec_daily, sec_bars,
                proxy_klines, args.skip_choppy,
            )
        t0_labels = {e["code"]: e["name"] for e in t0_list}
        t0_picks = build_picks_t0(
            t0_list, t0_labels, eval_dates, t0_daily, t0_bars,
            proxy_klines, args.skip_choppy,
        )

        rot_result = run_backtest(sec_picks, eval_dates, common_dates, sec_bars, args.fee, min_trades)
        t0_result = run_backtest(t0_picks, eval_dates, common_dates, t0_bars, args.fee, min_trades)
        if not rot_result or not t0_result:
            print("ERROR: 有效交易不足")
            sys.exit(1)

        print_report(rot_result, eval_dates, bar_label, "板块池", args.pick)
        print_report(t0_result, eval_dates, bar_label, "T+0 ETF池", "t0")
        print_compare(rot_result, t0_result, eval_dates, bar_label, args.pick)
        if args.segments:
            print_segments(rot_result, t0_result, eval_dates, args.pick)

        out = Path.home() / ".tradingagents" / "rotation" / f"backtest_rotation_vs_t0_{args.bar}_{datetime.now():%Y%m%d_%H%M}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "config": {
                "days": args.days, "bar": args.bar,
                "sector_pick": args.pick, "t0_pick": "t0",
                "signal": SIGNAL_TIME, "buy": BUY_TIME,
                "sell": f"TRIX({TRIX_PERIOD},{TRIX_SIGNAL})≥09:40≤{SELL_CUTOFF}",
                "eval_dates": eval_dates,
            },
            "rotation": {k: v for k, v in rot_result.items() if k != "trades"},
            "t0": {k: v for k, v in t0_result.items() if k != "trades"},
            "rotation_trades": rot_result["trades"],
            "t0_trades": t0_result["trades"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已保存: {out}")
        return

    # 单池回测（板块池）
    sectors = load_pingan_sectors()
    print(f"板块池: {len(sectors)} 个")

    if args.bar == "1min":
        etf_daily, etf_bars, all_dates = load_sector_1min(sectors)
    else:
        etf_daily, etf_bars, all_dates = load_sector_market_data(sectors, args.days)

    eval_dates = resolve_eval_dates_for_bars(all_dates, args.days, args.bar, etf_bars)
    if len(eval_dates) < min_trades:
        print("ERROR: 有效交易日不足")
        sys.exit(1)

    if args.pick == "t0":
        etf_list = sectors_to_etf_list(sectors)
        labels = {s["etf_code"]: s["name"] for s in sectors}
        picks = build_picks_t0(
            etf_list, labels, eval_dates, etf_daily, etf_bars,
            proxy_klines, args.skip_choppy,
        )
    else:
        picks = build_picks_v6(sectors, eval_dates, etf_daily, etf_bars)

    result = run_backtest(picks, eval_dates, all_dates, etf_bars, args.fee, min_trades)
    if not result:
        print("ERROR: 有效交易不足")
        sys.exit(1)

    print_report(result, eval_dates, bar_label, "板块池", args.pick)

    tag = "1min" if args.bar == "1min" else "5min"
    out = Path.home() / ".tradingagents" / "rotation" / f"backtest_rotation_{args.pick}_trix_{tag}_{datetime.now():%Y%m%d_%H%M}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "config": {
            "pool": "pingan_sectors",
            "pick": args.pick,
            "days": args.days,
            "bar": args.bar,
            "signal": SIGNAL_TIME,
            "buy": BUY_TIME,
            "sell": f"TRIX({TRIX_PERIOD},{TRIX_SIGNAL})≥09:40≤{SELL_CUTOFF}",
            "eval_dates": eval_dates,
        },
        "result": {k: v for k, v in result.items() if k != "trades"},
        "trades": result["trades"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

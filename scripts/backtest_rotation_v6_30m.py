#!/usr/bin/env python3
"""板块池 / T+0 池 每 30 分钟轮动回测。

规则（两池共用买卖点）:
- 首日首信号(09:30) TOP1 直接买入
- 之后每 30 分钟重算 TOP1
- TOP1 变化且可卖 → 按信号价卖旧买新
- 板块池: v6 TOP1 + T+1（买入日须早于当日）
- T+0池: 当日涨幅≥3% TOP1 + 可日内换仓（买入时刻早于信号时刻即可卖）
- 回测结束强制平仓

用法:
    python scripts/backtest_rotation_v6_30m.py --days 30
    python scripts/backtest_rotation_v6_30m.py --days 30 --pool t0
    python scripts/backtest_rotation_v6_30m.py --days 30 --compare
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_rotation_8way import (  # noqa: E402
    FEE_PCT,
    apply_net_return,
    bar_time_min,
    load_market_data as load_sector_market_data,
    price_at_time,
    rank_top1,
    time_to_min,
)
from backtest_t0_today1 import (  # noqa: E402
    MIN_GAIN,
    load_market_data as load_t0_market_data,
    rank_by_today_gain,
    select_etf,
)
from backtest_top1 import _calc_stats  # noqa: E402
from sector_etf_map import load_pingan_sectors  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

FIRST_SIGNAL = "09:30"
MARKET_CLOSE = "15:00"


def signal_times_30m() -> list[str]:
    times: list[str] = []
    t = 9 * 60 + 30
    while t <= 11 * 60 + 30:
        times.append(f"{t // 60:02d}:{t % 60:02d}")
        t += 30
    t = 13 * 60
    while t <= 15 * 60:
        times.append(f"{t // 60:02d}:{t % 60:02d}")
        t += 30
    return times


SIGNAL_TIMES = signal_times_30m()


def exec_price_at_time(bars: list[dict], target: str) -> float | None:
    px = price_at_time(bars, target)
    if px and px > 0:
        return px
    target_min = time_to_min(target)
    for b in bars:
        if bar_time_min(b) >= target_min:
            op = b.get("open")
            return float(op) if op and float(op) > 0 else float(b["close"])
    return None


def can_sell_t1(holding: dict, day: str, sig_time: str) -> bool:
    return holding["buy_date"] < day


def can_sell_t0(holding: dict, day: str, sig_time: str) -> bool:
    if holding["buy_date"] < day:
        return True
    if holding["buy_date"] == day:
        return time_to_min(holding["buy_time"]) < time_to_min(sig_time)
    return False


def make_sector_picker(sectors: list[dict], etf_daily: dict, etf_5min: dict):
    def pick(day: str, sig_time: str) -> dict | None:
        top1 = rank_top1(sectors, etf_daily, etf_5min, day, sig_time)
        if not top1:
            return None
        return {"etf_code": top1["etf_code"], "name": top1["name"]}
    return pick


def make_t0_picker(etf_list: list[dict], etf_daily: dict, etf_5min: dict, use_filter: bool):
    def pick(day: str, sig_time: str, *, initial: bool = False) -> dict | None:
        scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, sig_time)
        if len(scores) < 2:
            return None
        picked = select_etf(scores, use_filter and not initial)
        if not picked:
            return None
        gain, info = picked
        return {"etf_code": info["code"], "name": info["name"], "gain": gain}
    return pick


def run_backtest(
    pick_top1: Callable[..., dict | None],
    etf_5min: dict,
    eval_dates: list[str],
    fee_pct: float,
    can_sell: Callable[[dict, str, str], bool],
) -> dict:
    holding: dict | None = None
    trades: list[dict] = []
    rotations: list[dict] = []
    skipped: list[dict] = []

    def close_position(sell_day: str, sell_time: str, reason: str) -> None:
        nonlocal holding
        if not holding:
            return
        bars = etf_5min.get(holding["etf"], {}).get(sell_day, [])
        sell_price = exec_price_at_time(bars, sell_time)
        if not sell_price or sell_price <= 0:
            return
        ret = apply_net_return(holding["buy_price"], sell_price, fee_pct)
        trades.append({
            "name": holding["name"],
            "etf": holding["etf"],
            "buy_date": holding["buy_date"],
            "buy_time": holding["buy_time"],
            "buy_price": round(holding["buy_price"], 4),
            "sell_date": sell_day,
            "sell_time": sell_time,
            "sell_price": round(sell_price, 4),
            "return_pct": round(ret, 2),
            "reason": reason,
        })
        holding = None

    def open_position(day: str, sig_time: str, top1: dict) -> bool:
        nonlocal holding
        etf = top1["etf_code"]
        bars = etf_5min.get(etf, {}).get(day, [])
        buy_price = exec_price_at_time(bars, sig_time)
        if not buy_price or buy_price <= 0:
            return False
        holding = {
            "name": top1["name"],
            "etf": etf,
            "buy_date": day,
            "buy_time": sig_time,
            "buy_price": buy_price,
        }
        return True

    for day_i, day in enumerate(eval_dates):
        for sig_time in SIGNAL_TIMES:
            is_initial = day_i == 0 and sig_time == FIRST_SIGNAL and holding is None
            try:
                top1 = pick_top1(day, sig_time, initial=is_initial)
            except TypeError:
                top1 = pick_top1(day, sig_time)
            if not top1:
                continue

            if holding is None:
                if is_initial:
                    if open_position(day, sig_time, top1):
                        rotations.append({
                            "date": day, "time": sig_time, "action": "初始买入",
                            "name": top1["name"], "etf": top1["etf_code"],
                        })
                continue

            if top1["etf_code"] == holding["etf"]:
                continue

            if not can_sell(holding, day, sig_time):
                skipped.append({
                    "date": day, "time": sig_time,
                    "from": holding["name"], "to": top1["name"],
                    "buy_date": holding["buy_date"], "buy_time": holding["buy_time"],
                })
                continue

            old = holding
            old_bars = etf_5min.get(old["etf"], {}).get(day, [])
            sell_price = exec_price_at_time(old_bars, sig_time)
            if not sell_price or sell_price <= 0:
                continue

            ret = apply_net_return(old["buy_price"], sell_price, fee_pct)
            trades.append({
                "name": old["name"],
                "etf": old["etf"],
                "buy_date": old["buy_date"],
                "buy_time": old["buy_time"],
                "buy_price": round(old["buy_price"], 4),
                "sell_date": day,
                "sell_time": sig_time,
                "sell_price": round(sell_price, 4),
                "return_pct": round(ret, 2),
                "reason": "rotate",
            })

            new_bars = etf_5min.get(top1["etf_code"], {}).get(day, [])
            buy_price = exec_price_at_time(new_bars, sig_time)
            if not buy_price or buy_price <= 0:
                holding = None
                continue

            holding = {
                "name": top1["name"],
                "etf": top1["etf_code"],
                "buy_date": day,
                "buy_time": sig_time,
                "buy_price": buy_price,
            }
            rotations.append({
                "date": day, "time": sig_time, "action": "轮动",
                "from": old["name"], "to": top1["name"],
                "from_etf": old["etf"], "to_etf": top1["etf_code"],
                "closed_ret": round(ret, 2),
            })

    if holding and eval_dates:
        close_position(eval_dates[-1], MARKET_CLOSE, "eod_close")

    rets = [t["return_pct"] for t in trades]
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {
        "trade_count": len(trades),
        "rotation_count": len([r for r in rotations if r["action"] == "轮动"]),
        "skipped_count": len(skipped),
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets) if rets else {},
        "trades": trades,
        "rotations": rotations,
        "skipped": skipped,
    }


def print_report(result: dict, eval_dates: list[str], pool_label: str, pick_label: str, sell_rule: str):
    print()
    print("=" * 96)
    print(f"  {pool_label} 每30分钟轮动回测")
    print("=" * 96)
    print(f"  选股: 每30分钟 {pick_label} | 首日 {FIRST_SIGNAL} 直接买入")
    print(f"  轮动: TOP1变化 + {sell_rule} → 信号价卖旧买新 | 手续费万3双边")
    print(f"  区间: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日)")
    st = result["stats"]
    print(f"\n  完整交易: {result['trade_count']} 笔 | 轮动: {result['rotation_count']} 次 | "
          f"不可卖跳过: {result['skipped_count']} 次")
    print(f"  累计: {result['final_equity_pct']:+.2f}%")
    if st:
        print(f"  胜率: {st.get('win_rate', 0):.1f}% | 均笔: {st.get('avg', 0):+.2f}% | "
              f"回撤: {st.get('max_drawdown', 0):+.2f}% | 夏普: {st.get('sharpe', 0):.2f}")

    print(f"\n  {'买入日':>12} {'卖出日':>12} {'标的':14s} {'ETF':>8} {'买价':>8} {'卖价':>8} {'收益':>8} {'原因':>10}")
    print("  " + "-" * 92)
    eq = 1.0
    for t in result["trades"]:
        eq *= 1 + t["return_pct"] / 100
        print(
            f"  {t['buy_date']:>12} {t['sell_date']:>12} {t['name'][:14]:14s} {t['etf']:>8} "
            f"{t['buy_price']:8.4f} {t['sell_price']:8.4f} {t['return_pct']:+7.2f}% {t['reason']:>10} | "
            f"累计 {(eq-1)*100:+7.2f}%"
        )

    rots = [r for r in result["rotations"] if r["action"] == "轮动"]
    if rots:
        print(f"\n  轮动明细（{len(rots)} 次，显示前20）:")
        print(f"  {'日期':>12} {'时间':>6} {'卖出':14s} {'买入':14s} {'平仓收益':>8}")
        print("  " + "-" * 58)
        for r in rots[:20]:
            print(
                f"  {r['date']:>12} {r['time']:>6} {r['from'][:14]:14s} {r['to'][:14]:14s} "
                f"{r['closed_ret']:+7.2f}%"
            )
        if len(rots) > 20:
            print(f"  ... 共 {len(rots)} 次轮动")
    print("=" * 96)


def print_compare(sector: dict, t0: dict, eval_dates: list[str]):
    print()
    print("=" * 96)
    print("  30分钟轮动对比：板块池(v6) vs T+0池(涨幅TOP1)")
    print("=" * 96)
    print(f"  区间: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日)")
    print(f"\n  {'池':12s} {'笔数':>6} {'轮动':>6} {'跳过':>6} {'累计':>10} {'胜率':>8} {'均笔':>8} {'夏普':>8}")
    print("  " + "-" * 72)
    for label, r in [("板块池", sector), ("T+0池", t0)]:
        st = r["stats"]
        print(
            f"  {label:12s} {r['trade_count']:>6} {r['rotation_count']:>6} {r['skipped_count']:>6} "
            f"{r['final_equity_pct']:+9.2f}% {st.get('win_rate', 0):7.1f}% "
            f"{st.get('avg', 0):+7.2f}% {st.get('sharpe', 0):7.2f}"
        )
    diff = sector["final_equity_pct"] - t0["final_equity_pct"]
    print(f"\n  板块池 vs T+0池 累计差: {diff:+.2f} pp")
    print("=" * 96)


def run_sector(days: int, fee: float) -> tuple[dict, list[str]]:
    sectors = load_pingan_sectors()
    etf_daily, etf_5min, all_dates = load_sector_market_data(sectors, days)
    eval_dates = all_dates[-days:]
    pick = make_sector_picker(sectors, etf_daily, etf_5min)
    result = run_backtest(pick, etf_5min, eval_dates, fee, can_sell_t1)
    return result, eval_dates


def run_t0(days: int, fee: float, use_filter: bool = True) -> tuple[dict, list[str]]:
    etf_list = get_all_t0_etfs()
    etf_daily, etf_5min, all_dates, _ = load_t0_market_data(etf_list, days)
    eval_dates = all_dates[-days:]
    pick = make_t0_picker(etf_list, etf_daily, etf_5min, use_filter)
    result = run_backtest(pick, etf_5min, eval_dates, fee, can_sell_t0)
    return result, eval_dates


def main() -> None:
    parser = argparse.ArgumentParser(description="板块/T+0 每30分钟轮动回测")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--pool", choices=["sector", "t0", "compare"], default="sector")
    parser.add_argument("--no-filter", action="store_true", help="T+0池关闭涨幅≥3%%过滤")
    args = parser.parse_args()

    print(f"=== 每30分钟轮动回测 | {args.days} 天 | pool={args.pool} ===")
    print(f"信号时点: {', '.join(SIGNAL_TIMES)}")

    if args.pool == "compare":
        print("\n[1/2] 板块池 v6...")
        sec_result, eval_dates = run_sector(args.days, args.fee)
        print("\n[2/2] T+0 涨幅TOP1...")
        t0_result, t0_dates = run_t0(args.days, args.fee, use_filter=not args.no_filter)
        eval_dates = eval_dates if len(eval_dates) <= len(t0_dates) else t0_dates

        print_report(sec_result, eval_dates, "板块池", "v6 TOP1", "T+1可卖")
        print_report(
            t0_result, eval_dates, "T+0 ETF池",
            f"涨幅≥{MIN_GAIN}% TOP1（首日09:30不过滤）" if not args.no_filter else "涨幅 TOP1",
            "T+0可日内卖",
        )
        print_compare(sec_result, t0_result, eval_dates)

        out = Path.home() / ".tradingagents" / "rotation" / f"backtest_30m_compare_{datetime.now():%Y%m%d_%H%M}.json"
        payload = {
            "config": {"days": args.days, "signal_times": SIGNAL_TIMES, "eval_dates": eval_dates},
            "sector": {k: v for k, v in sec_result.items() if k not in ("trades", "rotations", "skipped")},
            "t0": {k: v for k, v in t0_result.items() if k not in ("trades", "rotations", "skipped")},
            "sector_trades": sec_result["trades"],
            "t0_trades": t0_result["trades"],
        }
    elif args.pool == "t0":
        print(f"T+0 ETF池: {len(get_all_t0_etfs())} 只")
        result, eval_dates = run_t0(args.days, args.fee, use_filter=not args.no_filter)
        pick_label = f"涨幅≥{MIN_GAIN}% TOP1" if not args.no_filter else "涨幅 TOP1"
        print_report(result, eval_dates, "T+0 ETF池", pick_label, "T+0可日内卖")
        out = Path.home() / ".tradingagents" / "rotation" / f"backtest_t0_30m_{datetime.now():%Y%m%d_%H%M}.json"
        payload = {
            "config": {"pool": "t0", "days": args.days, "signal_times": SIGNAL_TIMES, "eval_dates": eval_dates},
            "result": {k: v for k, v in result.items() if k not in ("trades", "rotations", "skipped")},
            "trades": result["trades"],
            "rotations": result["rotations"],
        }
    else:
        sectors = load_pingan_sectors()
        print(f"板块池: {len(sectors)} 个")
        result, eval_dates = run_sector(args.days, args.fee)
        print_report(result, eval_dates, "板块池", "v6 TOP1", "T+1可卖")
        out = Path.home() / ".tradingagents" / "rotation" / f"backtest_v6_30m_{datetime.now():%Y%m%d_%H%M}.json"
        payload = {
            "config": {"pool": "sector", "days": args.days, "signal_times": SIGNAL_TIMES, "eval_dates": eval_dates},
            "result": {k: v for k, v in result.items() if k not in ("trades", "rotations", "skipped")},
            "trades": result["trades"],
            "rotations": result["rotations"],
        }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

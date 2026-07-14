#!/usr/bin/env python3
"""T+0 ETF 动量回测 — 跨境/黄金/商品 ETF 池，当天买当天卖。

与 backtest_rotation_8way 的核心区别：
1. T+0：买入后当天即可卖出，无隔夜跳空风险
2. 可用固定止损：因为没有跳空，止损能正常生效
3. 信号时点 09:40，买入后剩余时段监控卖出

用法:
    python scripts/backtest_t0_etf.py --days 30
    python scripts/backtest_t0_etf.py --days 30 --daily
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

from backtest_top1 import _calc_stats, curl_get, fetch_sina_kline  # noqa: E402
from backtest_top1_intraday import check_buy_trigger, check_sell_trigger  # noqa: E402
from rotation_v6 import SCORE_WINDOW, score_at_signal  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

SINA_INTERVAL = 0.25
TIMEOUT = 15

SIGNAL_TIME = "09:40"
BUY_UP = 0.5       # 跨境ETF日内波动小，降低追涨门槛
BUY_DOWN = 1.5
BUY_REBOUND = 0.3
STOP_LOSS = -1.5    # 收紧止损
TRAIL_TRIGGER = 2.0 # 降低追踪触发
TRAIL_DROP = 0.5
FEE_PCT = 0.03  # 万分之 3


def time_to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def fetch_5min_kline(symbol: str, datalen: int = 2500) -> list[dict]:
    url = (
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=5&ma=no&datalen={datalen}"
    )
    raw = curl_get(url)
    time.sleep(SINA_INTERVAL)
    if not raw or raw.strip() in ("null", "", "[]"):
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def compute_daily_data(klines: list[dict]) -> list[dict]:
    result = []
    for i, k in enumerate(klines):
        close = float(k.get("close", 0))
        try:
            volume = float(k.get("volume", 0))
        except (ValueError, TypeError):
            volume = 0.0
        if i == 0:
            ret = 0.0
        else:
            prev = float(klines[i - 1].get("close", 0))
            ret = ((close - prev) / prev * 100) if prev else 0.0
        result.append({
            "date": k.get("day", ""),
            "open": float(k.get("open", close)),
            "high": float(k.get("high", close)),
            "low": float(k.get("low", close)),
            "close": close,
            "return_pct": ret,
            "volume": volume,
        })
    return result


def normalize_5min_bars(klines: list[dict]) -> dict[str, list[dict]]:
    by_day: dict[str, list[dict]] = {}
    for k in klines:
        dt = k.get("day", "")
        if not dt:
            continue
        parts = dt.split(" ")
        day = parts[0]
        t = parts[1] if len(parts) > 1 else "00:00:00"
        by_day.setdefault(day, []).append({
            "datetime": dt, "day": day, "time": t,
            "open": float(k.get("open", 0)),
            "high": float(k.get("high", 0)),
            "low": float(k.get("low", 0)),
            "close": float(k.get("close", 0)),
            "volume": float(k.get("volume", 0)),
        })
    for day in by_day:
        by_day[day].sort(key=lambda b: b["time"])
    return by_day


def bar_time_min(bar: dict) -> int:
    parts = bar["time"].split(":")
    if len(parts) < 2:
        return 0
    return int(parts[0]) * 60 + int(parts[1])


def price_at_time(bars: list[dict], target: str) -> float | None:
    target_min = time_to_min(target)
    best = None
    best_diff = 9999
    for b in bars:
        bar_min = bar_time_min(b)
        if bar_min < target_min:  # 只用已完成的 bar
            diff = target_min - bar_min
            if diff < best_diff:
                best_diff = diff
                best = b["close"]
    return best


def _partial_close_vol(bars: list[dict], cutoff: str) -> tuple[float | None, float]:
    cutoff_min = time_to_min(cutoff)
    close = None
    vol = 0.0
    for b in bars:
        bar_min = bar_time_min(b)
        if bar_min < cutoff_min:  # 只用已完成的 bar
            close = b["close"]
            vol += b["volume"]
    return close, vol


def apply_net_return(buy_price: float, sell_price: float, fee_pct: float) -> float:
    buy_cost = buy_price * (1 + fee_pct / 100)
    sell_income = sell_price * (1 - fee_pct / 100)
    return (sell_income - buy_cost) / buy_cost * 100


def next_trading_day(dates: list[str], d: str) -> str | None:
    if d not in dates:
        return None
    i = dates.index(d)
    return dates[i + 1] if i + 1 < len(dates) else None


def rank_etfs(
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    signal_date: str,
    signal_time: str,
) -> list[tuple[float, dict]]:
    scores = []
    for etf_info in etf_list:
        code = etf_info["code"]
        info = etf_daily.get(code)
        if not info:
            continue
        returns = info["returns"]
        idx_map = {r["date"]: i for i, r in enumerate(returns)}
        bars = etf_5min.get(code, {}).get(signal_date, [])
        if signal_date not in idx_map and not bars:
            continue
        partial_close, partial_vol = _partial_close_vol(bars, signal_time)

        if signal_date not in idx_map:
            if len(returns) < SCORE_WINDOW + 1:
                continue
            idx = len(returns) - 1
            score = score_at_signal(returns, idx, partial_close, partial_vol)
            if score is None:
                continue
            scores.append((score, etf_info))
            continue

        idx = idx_map[signal_date]
        if idx < SCORE_WINDOW:
            continue
        score = score_at_signal(returns, idx, partial_close, partial_vol)
        if score is None:
            continue
        scores.append((score, etf_info))
    scores.sort(key=lambda x: x[0], reverse=True)
    return scores


def run_t0_backtest(
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    eval_dates: list[str],
    fee_pct: float,
) -> dict:
    """T+0 回测：当天买当天卖。"""
    trades: list[dict] = []
    daily_log: list[dict] = []

    def log(day: str, time: str, action: str, **extra):
        daily_log.append({"date": day, "time": time, "action": action, **extra})

    sig_min = time_to_min(SIGNAL_TIME)

    for day in eval_dates:
        # 选 TOP1
        scores = rank_etfs(etf_list, etf_daily, etf_5min, day, SIGNAL_TIME)
        if len(scores) < 2:
            continue
        top1 = scores[0][1]
        code = top1["code"]

        # 基准价 = 09:40 前最后一根已完成 bar 的 close
        day_bars = etf_5min.get(code, {}).get(day, [])
        baseline = price_at_time(day_bars, SIGNAL_TIME)
        if not baseline or baseline <= 0:
            # 尝试用日K
            returns = etf_daily.get(code, {}).get("returns", [])
            idx_map = {r["date"]: i for i, r in enumerate(returns)}
            if day in idx_map and idx_map[day] > 0:
                baseline = returns[idx_map[day] - 1]["close"]
            if not baseline or baseline <= 0:
                continue

        log(day, SIGNAL_TIME, "信号",
            sector=top1["name"], etf=code,
            baseline=round(baseline, 4), score=f"{scores[0][0]:.2f}")

        # 买入检查：从 09:40 后的 bars
        buy_min = max(sig_min, time_to_min("09:30"))
        buy_bars = [b for b in day_bars if bar_time_min(b) >= buy_min]
        buy_price, buy_reason, buy_time = check_buy_trigger(
            buy_bars, baseline, BUY_UP, BUY_DOWN, BUY_REBOUND,
        )

        if buy_price is None:
            log(day, "15:00", "观察",
                sector=top1["name"], etf=code,
                baseline=round(baseline, 4), status="未触发买入")
            continue

        buy_bar_min = bar_time_min({"time": buy_time.split(" ")[-1][:5]}) if buy_time else buy_min

        # T+0 卖出：买入后的同日 bars
        sell_bars = [b for b in day_bars if bar_time_min(b) >= buy_bar_min]
        sell_price, sell_reason, sell_time = check_sell_trigger(
            sell_bars, buy_price, 0,
            STOP_LOSS, TRAIL_TRIGGER, TRAIL_DROP,
        )

        ret = apply_net_return(buy_price, sell_price, fee_pct)
        st = sell_time.split(" ")[-1][:5] if sell_time else ""
        log(day, st, "卖出",
            sector=top1["name"], etf=code,
            price=round(sell_price, 4), reason=sell_reason,
            return_pct=round(ret, 2))

        trades.append({
            "signal_date": day,
            "sector": top1["name"],
            "etf": code,
            "type": top1.get("type_name", ""),
            "buy_price": buy_price,
            "buy_time": buy_time,
            "buy_reason": buy_reason,
            "sell_date": day,  # T+0：同日卖出
            "sell_time": sell_time,
            "sell_price": sell_price,
            "sell_reason": sell_reason,
            "return_pct": ret,
        })

    rets = [t["return_pct"] for t in trades]
    stats = _calc_stats(rets) if rets else {}
    equity = 1.0
    for r in rets:
        equity *= 1 + r / 100
    return {
        "trades": trades,
        "trade_count": len(trades),
        "final_equity_pct": (equity - 1) * 100,
        "stats": stats,
        "daily_log": daily_log,
    }


def load_market_data(etf_list: list[dict], lookback: int) -> tuple[dict, dict, list[str]]:
    datalen = lookback + 15
    datalen_5m = min(lookback * 50 + 200, 3000)

    etf_daily: dict = {}
    etf_5min: dict = {}
    print(f">>> 拉取 {len(etf_list)} 只 T+0 ETF 日K + 5分K...")
    for i, etf_info in enumerate(etf_list):
        code = etf_info["code"]
        sym = etf_info["sina_symbol"]
        daily = fetch_sina_kline(sym, datalen=datalen)
        if daily and len(daily) > SCORE_WINDOW + 2:
            etf_daily[code] = {"returns": compute_daily_data(daily)}
        m5 = fetch_5min_kline(sym, datalen=datalen_5m)
        if m5:
            etf_5min[code] = normalize_5min_bars(m5)
        if (i + 1) % 10 == 0:
            print(f"    进度 {i+1}/{len(etf_list)} 日K={len(etf_daily)} 5分K={len(etf_5min)}")

    all_dates = sorted({
        r["date"] for info in etf_daily.values() for r in info["returns"]
    })
    m5_dates = sorted({d for bars in etf_5min.values() for d in bars})
    if m5_dates:
        all_dates = sorted(set(all_dates) | set(m5_dates))
    return etf_daily, etf_5min, all_dates


def print_report(result: dict, eval_days: int):
    print()
    print("=" * 80)
    print("  T+0 ETF 动量回测（跨境/黄金/商品 ETF，当天买当天卖）")
    print("=" * 80)
    print(f"  区间: 最近 {eval_days} 个交易日 | 手续费万3双边 | T+0日内 | 单仓位")
    print(f"  信号: {SIGNAL_TIME} v6 TOP1 | 买入: 涨{BUY_UP}%或跌{BUY_DOWN}%回弹{BUY_REBOUND}%")
    print(f"  卖出: 止{STOP_LOSS}% / 追踪+{TRAIL_TRIGGER}%落{TRAIL_DROP}% / 收盘卖")
    print()

    st = result["stats"]
    print(f"  交易笔数: {result['trade_count']}")
    print(f"  累计收益: {result['final_equity_pct']:+.2f}%")
    if st:
        print(f"  胜率: {st.get('win_rate', 0):.1f}%")
        print(f"  均笔: {st.get('avg', 0):+.2f}%")
        print(f"  最大回撤: {st.get('max_drawdown', 0):+.2f}%")
        print(f"  夏普: {st.get('sharpe', 0):.2f}")

    trades = result["trades"]
    if trades:
        # 卖出原因分布
        from collections import Counter
        reasons = Counter(t["sell_reason"] for t in trades)
        print(f"\n  卖出原因: {dict(reasons)}")

        # 大亏大赚
        losses = [t for t in trades if t["return_pct"] < -3]
        gains = [t for t in trades if t["return_pct"] > 3]
        if losses:
            print(f"\n  大亏(>3%):")
            for t in losses:
                print(f"    {t['signal_date']} {t['sector']:12s} {t['return_pct']:+.2f}% ({t['sell_reason']})")
        if gains:
            print(f"  大赚(>3%):")
            for t in gains:
                print(f"    {t['signal_date']} {t['sector']:12s} {t['return_pct']:+.2f}% ({t['sell_reason']})")

    print("=" * 80)

    # 逐笔明细
    if trades:
        print(f"\n  {'信号日':>12} {'板块':12s} {'ETF':>8s} {'类型':4s} {'买入价':>7s} {'买入因':>4s} {'卖出价':>7s} {'卖出因':>6s} {'收益':>7s}")
        print("  " + "-" * 80)
        eq = 1.0
        for t in trades:
            eq *= (1 + t["return_pct"] / 100)
            print(f"  {t['signal_date']:>12} {t['sector']:12s} {t['etf']:>8s} {t.get('type',''):4s} "
                  f"{t['buy_price']:7.4f} {t['buy_reason']:>4s} {t['sell_price']:7.4f} {t['sell_reason']:>6s} "
                  f"{t['return_pct']:+7.2f}% | 累计 {(eq-1)*100:+7.2f}%")


def main():
    parser = argparse.ArgumentParser(description="T+0 ETF 动量回测")
    parser.add_argument("--days", type=int, default=30, help="回测交易日数（默认30）")
    parser.add_argument("--fee", type=float, default=FEE_PCT, help="单边手续费(默认0.03=万3)")
    parser.add_argument("--daily", action="store_true", help="输出按天明细")
    args = parser.parse_args()

    etf_list = get_all_t0_etfs()
    print(f"=== T+0 ETF 动量回测 ===")
    print(f"ETF池: {len(etf_list)} 只 (跨境{len([e for e in etf_list if e.get('type_name')=='跨境'])} "
          f"黄金{len([e for e in etf_list if e.get('type_name')=='黄金'])} "
          f"商品{len([e for e in etf_list if e.get('type_name')=='商品'])} "
          f"债券{len([e for e in etf_list if e.get('type_name')=='债券'])})")
    print(f"回测 {args.days} 日 | 信号 {SIGNAL_TIME} | T+0日内 | 手续费万{args.fee * 100:.0f}")
    print(f"买入: 涨{BUY_UP}% / 跌{BUY_DOWN}%回弹{BUY_REBOUND}% | 卖出: 止{STOP_LOSS}% 追踪+{TRAIL_TRIGGER}%落{TRAIL_DROP}% 收盘")
    print()

    etf_daily, etf_5min, all_dates = load_market_data(etf_list, args.days)
    if len(etf_daily) < 5:
        print("ERROR: 日K数据不足")
        sys.exit(1)

    eval_dates = all_dates[-args.days:]
    if len(eval_dates) < 5:
        print("ERROR: 有效交易日不足")
        sys.exit(1)
    print(f"    日K {len(etf_daily)} ETF, 5分K {len(etf_5min)} ETF, 回测 {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}日)")

    result = run_t0_backtest(etf_list, etf_daily, etf_5min, all_dates, eval_dates, args.fee)
    print_report(result, len(eval_dates))

    out = Path.home() / ".tradingagents" / "rotation" / f"backtest_t0_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

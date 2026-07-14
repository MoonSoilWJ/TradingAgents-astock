#!/usr/bin/env python3
"""T+0 ETF 当日涨幅动量回测 — 14:50 选当日涨幅最大 → 14:55 买入 → 次日 TRIX 死叉卖。

优化规则（基于 100 天归因）：
1. 当日涨幅 <3% 跳过（弱信号 23 笔合计 -8.22%）
2. TRIX 死叉仅在次日 09:40 之后生效（早盘死叉 25 笔 0 胜率）
3. 不设涨幅上限、不连亏冷却（回测证明会错杀商品趋势单）

用法:
    python scripts/backtest_t0_today1.py --days 30
    python scripts/backtest_t0_today1.py --days 100
    python scripts/backtest_t0_today1.py --days 100 --no-filter  # 对比原版（含早盘TRIX）
    python scripts/backtest_t0_today1.py --daily-proxy --start-date 2025-01-01 --end-date 2025-12-31
    # 日K降级: 14:50≈当日收盘买入, 次日卖出≈(高+低)/2, 含震荡期跳过
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

from backtest_top1 import _calc_stats, fetch_sina_kline  # noqa: E402
from backtest_top1_minute import calc_trix, calc_trix_signal  # noqa: E402
from backtest_t0_etf import (  # noqa: E402
    FEE_PCT,
    apply_net_return,
    bar_time_min,
    compute_daily_data,
    fetch_5min_kline,
    next_trading_day,
    normalize_5min_bars,
    price_at_time,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402
from t0_regime import REGIME_PROXY, detect_regime  # noqa: E402

SINA_INTERVAL = 0.25
SIGNAL_TIME = "14:50"
BUY_TIME = "14:55"
TRIX_PERIOD = 5
TRIX_MIN_SELL = "09:40"   # 忽略早盘 TRIX 死叉（归因：09:40 前 0 胜率）
MIN_GAIN = 3.0            # 全局最低当日涨幅 %
MAX_GAIN = 7.0            # 防脉冲：当日涨幅上限 %
COMMODITY_TREND_PROXY = "501018"  # 南方原油作商品趋势代理
COMMODITY_MA_DAYS = 20


def time_to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def bar_clock(bar: dict) -> str:
    day = bar.get("day", "")
    if " " in day:
        return day.split(" ")[1][:5]
    return bar.get("time", "00:00:00")[:5]


def passes_gain_filter(
    gain: float,
    anti_pulse: bool = False,
) -> bool:
    if gain < MIN_GAIN:
        return False
    if anti_pulse and gain > MAX_GAIN:
        return False
    return True


def commodity_trend_ok(etf_daily: dict, signal_date: str) -> bool:
    """商品趋势期：南方原油收盘价 > 20 日均线。"""
    info = etf_daily.get(COMMODITY_TREND_PROXY)
    if not info:
        return True
    returns = info["returns"]
    idx_map = {r["date"]: i for i, r in enumerate(returns)}
    if signal_date not in idx_map:
        return False
    idx = idx_map[signal_date]
    if idx < COMMODITY_MA_DAYS - 1:
        return False
    window = returns[idx - COMMODITY_MA_DAYS + 1: idx + 1]
    ma = sum(r["close"] for r in window) / len(window)
    return window[-1]["close"] > ma


def select_etf(
    scores: list[tuple[float, dict]],
    use_filter: bool,
    anti_pulse: bool = False,
) -> tuple[float, dict] | None:
    for gain, etf in scores:
        if use_filter and not passes_gain_filter(gain, anti_pulse=anti_pulse):
            continue
        return gain, etf
    return None


def simulate_trix_cross_after(
    buy_cost: float,
    min_bars_today: list[dict],
    min_bars_next: list[dict],
    trix_period: int = TRIX_PERIOD,
    min_sell_time: str = TRIX_MIN_SELL,
) -> tuple[float, str, dict]:
    """次日 TRIX 死叉卖，忽略 min_sell_time 之前的死叉。"""
    all_bars = min_bars_today + min_bars_next
    min_warmup = trix_period * 3 + 5
    if len(all_bars) < min_warmup:
        last_close = float(min_bars_next[-1].get("close", 0)) if min_bars_next else buy_cost
        return (last_close - buy_cost) / buy_cost * 100, "close", {"reason": "insufficient_data", "sell_price": last_close}

    warmup_len = len(min_bars_today)
    closes = [float(b.get("close", 0)) for b in all_bars]
    trix = calc_trix(closes, trix_period)
    signal = calc_trix_signal(trix, max(trix_period // 2, 3))
    min_sell_min = time_to_min(min_sell_time)
    search_start = max(warmup_len, min_warmup)

    for i in range(search_start, len(all_bars)):
        if time_to_min(bar_clock(all_bars[i])) < min_sell_min:
            continue
        if trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
            sell_price = closes[i]
            return (sell_price - buy_cost) / buy_cost * 100, "trix_death_cross", {
                "sell_price": sell_price,
                "bar": all_bars[i].get("day", ""),
                "trix": trix[i],
                "signal": signal[i],
            }

    last_close = closes[-1] if closes else buy_cost
    return (last_close - buy_cost) / buy_cost * 100, "close", {"reason": "no_death_cross", "sell_price": last_close}


def bars_for_trix(bars: list[dict]) -> list[dict]:
    return [{"close": b["close"], "day": b.get("datetime", b["day"])} for b in bars]


def daily_bar(etf_daily: dict, code: str, day: str) -> dict | None:
    info = etf_daily.get(code)
    if not info:
        return None
    for r in info["returns"]:
        if r["date"] == day:
            return r
    return None


def rank_by_today_gain_daily(
    etf_list: list[dict],
    etf_daily: dict,
    signal_date: str,
) -> list[tuple[float, dict]]:
    """日K降级: 用当日收盘价近似 14:50 涨幅排名。"""
    scores: list[tuple[float, dict]] = []
    for etf_info in etf_list:
        code = etf_info["code"]
        info = etf_daily.get(code)
        if not info:
            continue
        returns = info["returns"]
        idx_map = {r["date"]: i for i, r in enumerate(returns)}
        if signal_date not in idx_map or idx_map[signal_date] == 0:
            continue
        prev_close = returns[idx_map[signal_date] - 1]["close"]
        close = returns[idx_map[signal_date]]["close"]
        if not prev_close or prev_close <= 0 or not close or close <= 0:
            continue
        gain = (close - prev_close) / prev_close * 100
        scores.append((gain, etf_info))
    scores.sort(key=lambda x: x[0], reverse=True)
    return scores


def regime_on_date(proxy_klines: list[dict], day: str) -> dict | None:
    if not proxy_klines:
        return None
    idx_map = {k.get("day", ""): i for i, k in enumerate(proxy_klines)}
    if day not in idx_map:
        return None
    window = proxy_klines[: idx_map[day] + 1]
    return detect_regime(window, day)


def rank_by_today_gain(
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    signal_date: str,
    signal_time: str,
) -> list[tuple[float, dict]]:
    """按信号时刻相对昨收的当日涨幅排名。"""
    scores: list[tuple[float, dict]] = []
    for etf_info in etf_list:
        code = etf_info["code"]
        info = etf_daily.get(code)
        if not info:
            continue
        returns = info["returns"]
        idx_map = {r["date"]: i for i, r in enumerate(returns)}
        if signal_date not in idx_map or idx_map[signal_date] == 0:
            continue
        prev_close = returns[idx_map[signal_date] - 1]["close"]
        if not prev_close or prev_close <= 0:
            continue

        bars = etf_5min.get(code, {}).get(signal_date, [])
        partial_close = price_at_time(bars, signal_time)
        if partial_close is None or partial_close <= 0:
            # 修复未来函数：5分K缺失时用前日收盘价（而非当日收盘价）
            partial_close = prev_close
        if not partial_close or partial_close <= 0:
            continue

        gain = (partial_close - prev_close) / prev_close * 100
        scores.append((gain, etf_info))
    scores.sort(key=lambda x: x[0], reverse=True)
    return scores


def run_backtest(
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    eval_dates: list[str],
    fee_pct: float,
    use_filter: bool = True,
    anti_pulse: bool = False,
    commodity_trend: bool = False,
    daily_proxy: bool = False,
    skip_choppy: bool = False,
    proxy_klines: list[dict] | None = None,
) -> dict:
    trades: list[dict] = []
    skipped: list[dict] = []

    for day in eval_dates:
        if skip_choppy:
            regime = regime_on_date(proxy_klines or [], day)
            if regime and regime.get("skip_choppy"):
                top_gain = 0.0
                if daily_proxy:
                    ranked = rank_by_today_gain_daily(etf_list, etf_daily, day)
                    if ranked:
                        top_gain = ranked[0][0]
                else:
                    ranked = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, SIGNAL_TIME)
                    if ranked:
                        top_gain = ranked[0][0]
                skipped.append({
                    "date": day,
                    "reason": f"震荡期跳过(MA穿越{regime['ma_crosses']}次)",
                    "top_gain": top_gain,
                    "regime": regime["mode"],
                })
                continue

        if use_filter and commodity_trend and not commodity_trend_ok(etf_daily, day):
            skipped.append({"date": day, "reason": "非商品趋势期", "top_gain": 0})
            continue

        if daily_proxy:
            scores = rank_by_today_gain_daily(etf_list, etf_daily, day)
        else:
            scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, SIGNAL_TIME)
        if len(scores) < 2:
            continue

        picked = select_etf(scores, use_filter, anti_pulse=anti_pulse)
        if picked is None:
            reason = "无满足条件的ETF"
            if use_filter and anti_pulse and scores[0][0] > MAX_GAIN:
                reason = f"防脉冲(最高{scores[0][0]:.1f}%>{MAX_GAIN:.0f}%)"
            skipped.append({"date": day, "reason": reason, "top_gain": scores[0][0]})
            continue

        gain, top1 = picked
        code = top1["code"]
        sell_day = next_trading_day(all_dates, day)
        if not sell_day:
            continue

        if daily_proxy:
            sig_bar = daily_bar(etf_daily, code, day)
            sell_bar = daily_bar(etf_daily, code, sell_day)
            if not sig_bar or not sell_bar:
                continue
            buy_price = sig_bar["close"]
            sell_price = (sell_bar["high"] + sell_bar["low"]) / 2
            if buy_price <= 0 or sell_price <= 0:
                continue
            sell_reason = "hl2_next_day"
            sell_time = sell_day
        else:
            day_bars = etf_5min.get(code, {}).get(day, [])
            buy_price = price_at_time(day_bars, BUY_TIME)
            if buy_price is None or buy_price <= 0:
                buy_price = price_at_time(day_bars, SIGNAL_TIME)
            if buy_price is None or buy_price <= 0:
                continue

            sell_bars = etf_5min.get(code, {}).get(sell_day, [])
            if not sell_bars:
                continue

            min_sell = "09:30" if not use_filter else TRIX_MIN_SELL
            _, sell_reason, detail = simulate_trix_cross_after(
                buy_price,
                bars_for_trix(day_bars),
                bars_for_trix(sell_bars),
                trix_period=TRIX_PERIOD,
                min_sell_time=min_sell,
            )
            sell_price = detail.get("sell_price")
            if sell_price is None:
                sell_price = float(sell_bars[-1]["close"])
            sell_time = detail.get("bar", sell_day)

        ret = apply_net_return(buy_price, sell_price, fee_pct)

        rank = next((i + 1 for i, (_, e) in enumerate(scores) if e["code"] == code), 1)
        trades.append({
            "signal_date": day,
            "sell_date": sell_day,
            "sector": top1["name"],
            "etf": code,
            "type": top1.get("type_name", ""),
            "rank": rank,
            "today_gain": round(gain, 2),
            "buy_price": round(buy_price, 4),
            "buy_time": "close" if daily_proxy else BUY_TIME,
            "sell_price": round(sell_price, 4),
            "sell_time": sell_time,
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
        "skipped_count": len(skipped),
        "skipped": skipped,
        "final_equity_pct": (equity - 1) * 100,
        "stats": stats,
        "use_filter": use_filter,
        "daily_proxy": daily_proxy,
        "skip_choppy": skip_choppy,
    }


def load_market_data(
    etf_list: list[dict],
    lookback: int,
    daily_only: bool = False,
    datalen: int | None = None,
) -> tuple[dict, dict, list[str], list[dict]]:
    bar_len = datalen or (lookback + 15)
    datalen_5m = min(lookback * 50 + 200, 5500)

    etf_daily: dict = {}
    etf_5min: dict = {}
    label = "日K" if daily_only else "日K + 5分K"
    print(f">>> 拉取 {len(etf_list)} 只 T+0 ETF {label} (datalen={bar_len})...")
    for i, etf_info in enumerate(etf_list):
        code = etf_info["code"]
        sym = etf_info["sina_symbol"]
        daily = fetch_sina_kline(sym, datalen=bar_len)
        if daily and len(daily) > 3:
            etf_daily[code] = {"returns": compute_daily_data(daily)}
        if not daily_only:
            m5 = fetch_5min_kline(sym, datalen=datalen_5m)
            if m5:
                etf_5min[code] = normalize_5min_bars(m5)
        if (i + 1) % 20 == 0:
            extra = f" 5分K={len(etf_5min)}" if not daily_only else ""
            print(f"    进度 {i+1}/{len(etf_list)} 日K={len(etf_daily)}{extra}")
        time.sleep(SINA_INTERVAL * 0.1)

    proxy_etf = next((e for e in etf_list if e["code"] == REGIME_PROXY), None)
    proxy_sym = proxy_etf["sina_symbol"] if proxy_etf else f"sh{REGIME_PROXY}"
    proxy_klines = fetch_sina_kline(proxy_sym, datalen=bar_len)
    time.sleep(SINA_INTERVAL * 0.1)

    all_dates = sorted({
        r["date"] for info in etf_daily.values() for r in info["returns"]
    })
    if not daily_only:
        m5_dates = sorted({d for bars in etf_5min.values() for d in bars})
        if m5_dates:
            all_dates = sorted(set(all_dates) | set(m5_dates))
    return etf_daily, etf_5min, all_dates, proxy_klines


def print_report(result: dict, eval_days: int, start: str, end: str):
    use_filter = result.get("use_filter", True)
    daily_proxy = result.get("daily_proxy", False)
    skip_choppy = result.get("skip_choppy", False)
    print()
    print("=" * 80)
    if daily_proxy:
        title = "T+0 ETF 当日涨幅动量回测（日K降级·实盘规则）"
    elif use_filter:
        title = "T+0 ETF 当日涨幅动量回测（优化版）"
    else:
        title = "T+0 ETF 当日涨幅动量回测（无过滤）"
    print(f"  {title}")
    print("=" * 80)
    print(f"  区间: {start} ~ {end} ({eval_days} 交易日) | 手续费万3双边 | 单仓位")
    if daily_proxy:
        print(f"  选股: 14:50≈当日收盘相对昨收涨幅最大")
        print(f"  买入: 信号日收盘价（近似14:55）")
        print(f"  卖出: 次日 (高+低)/2")
    else:
        print(f"  选股: 14:50 相对昨收当日涨幅最大 | 买入: {BUY_TIME} 直买")
    if use_filter:
        print(f"  过滤: 当日涨幅 ≥{MIN_GAIN}%")
        if skip_choppy:
            print(f"  环境: 501018震荡期(MA20穿越≥2)跳过")
        if daily_proxy:
            print(f"  说明: 无5分K历史时的降级近似，与实盘TRIX卖出有偏差")
        else:
            print(f"  卖出: TRIX({TRIX_PERIOD}) 死叉(≥{TRIX_MIN_SELL}) / 无死叉收盘卖")
    else:
        print(f"  过滤: 无")
        if not daily_proxy:
            print(f"  卖出: TRIX({TRIX_PERIOD}) 死叉(含早盘) / 无死叉则收盘卖")
    print()

    st = result["stats"]
    print(f"  交易笔数: {result['trade_count']} | 跳过: {result.get('skipped_count', 0)} 天")
    print(f"  累计收益: {result['final_equity_pct']:+.2f}%")
    if st:
        print(f"  胜率: {st.get('win_rate', 0):.1f}%")
        print(f"  均笔: {st.get('avg', 0):+.2f}%")
        print(f"  最大回撤: {st.get('max_drawdown', 0):+.2f}%")
        print(f"  夏普: {st.get('sharpe', 0):.2f}")

    trades = result["trades"]
    if trades:
        from collections import Counter
        reasons = Counter(t["sell_reason"] for t in trades)
        print(f"\n  卖出原因: {dict(reasons)}")
        print(f"\n  {'信号日':>12} {'板块':14s} {'ETF':>8s} {'当日涨':>6s} {'买入价':>7s} {'卖出价':>7s} {'卖出因':>6s} {'收益':>7s}")
        print("  " + "-" * 80)
        eq = 1.0
        for t in trades:
            eq *= (1 + t["return_pct"] / 100)
            print(f"  {t['signal_date']:>12} {t['sector']:14s} {t['etf']:>8s} "
                  f"{t['today_gain']:+5.1f}% {t['buy_price']:7.4f} {t['sell_price']:7.4f} "
                  f"{t['sell_reason']:>6s} {t['return_pct']:+7.2f}% | 累计 {(eq-1)*100:+7.2f}%")
    print("=" * 80)


def resolve_eval_dates(
    all_dates: list[str],
    days: int,
    start_date: str,
    end_date: str,
) -> list[str]:
    eval_dates = list(all_dates)
    if start_date:
        eval_dates = [d for d in eval_dates if d >= start_date]
    if end_date:
        eval_dates = [d for d in eval_dates if d <= end_date]
    if not start_date and not end_date and days > 0:
        eval_dates = eval_dates[-days:]
    return eval_dates


def main():
    parser = argparse.ArgumentParser(description="T+0 ETF 当日涨幅动量回测")
    parser.add_argument("--days", type=int, default=30, help="回测交易日数（默认30，与日期范围二选一）")
    parser.add_argument("--start-date", type=str, default="", help="回测起始日 YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, default="", help="回测结束日 YYYY-MM-DD")
    parser.add_argument("--datalen", type=int, default=0, help="拉取日K根数（默认按区间自动）")
    parser.add_argument("--daily-proxy", action="store_true",
                        help="日K降级: 14:50≈收盘买入, 次日(H+L)/2卖出")
    parser.add_argument("--skip-choppy", action="store_true",
                        help="501018震荡期跳过（--daily-proxy 时默认开启）")
    parser.add_argument("--no-skip-choppy", action="store_true", help="关闭震荡期跳过")
    parser.add_argument("--fee", type=float, default=FEE_PCT, help="单边手续费(默认0.03=万3)")
    parser.add_argument("--no-filter", action="store_true", help="关闭优化过滤（对比原版）")
    args = parser.parse_args()

    use_filter = not args.no_filter
    daily_proxy = args.daily_proxy
    if args.no_skip_choppy:
        skip_choppy = False
    elif args.skip_choppy:
        skip_choppy = True
    else:
        skip_choppy = daily_proxy  # 日K降级默认对齐实盘

    etf_list = get_all_t0_etfs()
    mode = "日K降级" if daily_proxy else ("优化版" if use_filter else "原版")
    print(f"=== T+0 ETF 当日涨幅动量回测 ({mode}) ===")
    print(f"ETF池: {len(etf_list)} 只")
    if args.start_date or args.end_date:
        print(f"区间: {args.start_date or '最早'} ~ {args.end_date or '最新'}")
    else:
        print(f"回测 {args.days} 日")
    if daily_proxy:
        print("定价: 信号日收盘买入 | 次日(高+低)/2卖出")
    else:
        print(f"信号 {SIGNAL_TIME} | 买入 {BUY_TIME}")
    if skip_choppy:
        print("震荡期跳过: 开启")
    print(f"手续费万{args.fee * 100:.0f}")
    print()

    lookback = args.days
    if args.start_date or args.end_date:
        lookback = 280
    datalen = args.datalen or (500 if daily_proxy else lookback + 15)

    etf_daily, etf_5min, all_dates, proxy_klines = load_market_data(
        etf_list, lookback, daily_only=daily_proxy, datalen=datalen,
    )
    if len(etf_daily) < 5:
        print("ERROR: 日K数据不足")
        sys.exit(1)

    eval_dates = resolve_eval_dates(all_dates, args.days, args.start_date, args.end_date)
    if len(eval_dates) < 5:
        print("ERROR: 有效交易日不足")
        sys.exit(1)
    m5_note = f", 5分K {len(etf_5min)} ETF" if not daily_proxy else ""
    print(f"    日K {len(etf_daily)} ETF{m5_note}, 代理K {len(proxy_klines)} 根")
    print(f"    回测 {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}日)")

    result = run_backtest(
        etf_list, etf_daily, etf_5min, all_dates, eval_dates, args.fee, use_filter,
        daily_proxy=daily_proxy,
        skip_choppy=skip_choppy,
        proxy_klines=proxy_klines,
    )
    print_report(result, len(eval_dates), eval_dates[0], eval_dates[-1])

    tag = "daily" if daily_proxy else ("opt" if use_filter else "raw")
    if skip_choppy:
        tag += "_skip"
    out = Path.home() / ".tradingagents" / "rotation" / f"backtest_t0_today1_{tag}_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **result,
        "start": eval_dates[0],
        "end": eval_dates[-1],
        "eval_days": len(eval_dates),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

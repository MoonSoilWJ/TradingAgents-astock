#!/usr/bin/env python3
"""板块轮动 TOP1 时间组合搜索

搜索最优的"信号时间 × 买入时间 × 卖出时间 × 策略"组合。
约束：卖出时间(次日) < 买入时间(当日)，保证每天可循环操作。

用法:
    python scripts/search_time_combo.py --lookback 100 --start-date 2026-01-30 --end-date 2026-03-20
    python scripts/search_time_combo.py --lookback 100 --start-date 2026-06-01 --end-date 2026-07-10
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from sector_etf_map import etf_to_sina_symbol, load_pingan_sectors  # noqa: E402

PROXY = os.environ.get("ROTATION_PROXY", "http://127.0.0.1:7890")
SINA_INTERVAL = 0.2
TIMEOUT = 15

SCORE_WINDOW = 3
VOL_THRESHOLD = 1.5
VOL_AVG_PERIOD = 5
VOL_BASE = 0.3


def curl_get(url):
    for use_proxy in [False, True]:
        cmd = ["curl", "-s", "--connect-timeout", str(TIMEOUT)]
        if use_proxy:
            cmd += ["-x", PROXY]
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 10)
            for enc in ["gbk", "utf-8"]:
                try:
                    text = r.stdout.decode(enc)
                    if text and len(text) > 10:
                        return text
                except:
                    continue
        except:
            continue
    return ""


def fetch_daily_kline(symbol, datalen=100):
    sina_sym = etf_to_sina_symbol(symbol)
    url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sina_sym}&scale=240&ma=no&datalen={datalen}"
    raw = curl_get(url)
    time.sleep(SINA_INTERVAL)
    if not raw or raw.strip() in ("null", "", "[]"):
        return []
    try:
        return json.loads(raw)
    except:
        return []


def fetch_5min_kline(symbol, datalen=5000, scale=5):
    sina_sym = etf_to_sina_symbol(symbol)
    datalen = min(datalen, 5000)
    url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sina_sym}&scale={scale}&ma=no&datalen={datalen}"
    raw = curl_get(url)
    time.sleep(SINA_INTERVAL)
    if not raw or raw.strip() in ("null", "", "[]"):
        return []
    try:
        return json.loads(raw)
    except:
        return []


def compute_daily_from_klines(klines):
    result = []
    for i, k in enumerate(klines):
        close = float(k.get("close", 0))
        try:
            volume = float(k.get("volume", 0))
        except:
            volume = 0.0
        if i == 0:
            ret = 0.0
        else:
            prev_close = float(klines[i - 1].get("close", 0))
            ret = ((close - prev_close) / prev_close * 100) if prev_close else 0.0
        result.append({"date": k.get("day", ""), "close": close, "return_pct": ret, "volume": volume})
    return result


def _ema(values, period):
    if not values:
        return []
    ema = [values[0]]
    k = 2 / (period + 1)
    for i in range(1, len(values)):
        ema.append(values[i] * k + ema[-1] * (1 - k))
    return ema


def calc_trix(closes, period=5):
    if len(closes) < period * 3 + 1:
        return [0.0] * len(closes)
    ema1 = _ema(closes, period)
    ema2 = _ema(ema1, period)
    ema3 = _ema(ema2, period)
    trix = [0.0] * len(closes)
    for i in range(1, len(ema3)):
        if ema3[i - 1] != 0:
            trix[i] = (ema3[i] - ema3[i - 1]) / ema3[i - 1] * 100
    return trix


def compute_v6_score(returns, idx):
    if idx < SCORE_WINDOW:
        return 0.0
    ret_w = sum(r["return_pct"] for r in returns[idx - SCORE_WINDOW + 1:idx + 1])
    vol_today = returns[idx].get("volume", 0)
    vol_prev = [returns[j].get("volume", 0) for j in range(max(0, idx - VOL_AVG_PERIOD), idx)]
    avg_vol = sum(vol_prev) / len(vol_prev) if vol_prev and sum(vol_prev) > 0 else vol_today
    vol_ratio = vol_today / avg_vol if avg_vol > 0 else 1.0
    vol_factor = VOL_BASE + (1 - VOL_BASE) * min(vol_ratio / VOL_THRESHOLD, 1.0)
    return ret_w * vol_factor


def time_str_to_min(t):
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def get_partial_close(bars, cutoff_time):
    """获取截止到 cutoff_time 的收盘价和累计成交量。"""
    cutoff_min = time_str_to_min(cutoff_time)
    closes = []
    total_vol = 0.0
    for bar in bars:
        bar_time = bar.get("day", "")
        time_part = bar_time[11:16] if len(bar_time) > 15 else ""
        if time_part:
            bar_min = time_str_to_min(time_part)
            if bar_min <= cutoff_min:
                closes.append(float(bar.get("close", 0)))
                total_vol += float(bar.get("volume", 0))
    if not closes:
        return None, 0.0
    return closes[-1], total_vol


def get_price_at_time(bars, target_time):
    """获取 target_time 时刻的收盘价。"""
    target_min = time_str_to_min(target_time)
    best_price = None
    best_diff = 999
    for bar in bars:
        bar_time = bar.get("day", "")
        time_part = bar_time[11:16] if len(bar_time) > 15 else ""
        if time_part:
            bar_min = time_str_to_min(time_part)
            diff = abs(bar_min - target_min)
            if diff < best_diff:
                best_diff = diff
                best_price = float(bar.get("close", 0))
    return best_price


def get_bars_until_time(bars, cutoff_time):
    """获取次日截止到 cutoff_time 的所有 K 线。"""
    cutoff_min = time_str_to_min(cutoff_time)
    result = []
    for bar in bars:
        bar_time = bar.get("day", "")
        time_part = bar_time[11:16] if len(bar_time) > 15 else ""
        if time_part and time_str_to_min(time_part) <= cutoff_min:
            result.append(bar)
    return result


def simulate_sell(buy_cost, next_bars, strategy):
    """模拟次日卖出，返回 (收益率, 原因)。"""
    if not next_bars:
        return 0.0, "no_data"

    strategy_type = strategy["type"]

    if strategy_type == "time":
        # 定时卖：到指定时间按价格卖
        sell_price = float(next_bars[-1].get("close", 0))
        ret = (sell_price - buy_cost) / buy_cost * 100
        return ret, "time_sell"

    elif strategy_type == "trailing":
        trail_trigger = strategy["trigger"]
        trail_drop = strategy["drop"]
        stop_loss = strategy["stop_loss"]
        trigger_price = buy_cost * (1 + trail_trigger / 100)
        stop_price = buy_cost * (1 + stop_loss / 100) if stop_loss < 0 else 0
        tracking = False
        peak_high = 0.0
        for bar in next_bars:
            bar_high = float(bar.get("high", 0))
            bar_low = float(bar.get("low", 0))
            if stop_loss < 0 and bar_low <= stop_price:
                return stop_loss, "stop_loss"
            if not tracking and bar_high >= trigger_price:
                tracking = True
                peak_high = bar_high
            elif tracking and bar_high > peak_high:
                peak_high = bar_high
            if tracking:
                trail_sell = peak_high * (1 - trail_drop / 100)
                if bar_low <= trail_sell:
                    ret = (trail_sell - buy_cost) / buy_cost * 100
                    return ret, "trailing_stop"
        sell_price = float(next_bars[-1].get("close", 0))
        ret = (sell_price - buy_cost) / buy_cost * 100
        return ret, "close"

    elif strategy_type == "fixed":
        tp_price = buy_cost * (1 + strategy["tp"] / 100) if strategy["tp"] > 0 else 0
        sl_price = buy_cost * (1 + strategy["sl"] / 100) if strategy["sl"] < 0 else 0
        for bar in next_bars:
            bar_high = float(bar.get("high", 0))
            bar_low = float(bar.get("low", 0))
            tp_hit = tp_price > 0 and bar_high >= tp_price
            sl_hit = sl_price > 0 and bar_low <= sl_price
            if tp_hit and sl_hit:
                return strategy["sl"], "stop_loss"
            elif tp_hit:
                return strategy["tp"], "take_profit"
            elif sl_hit:
                return strategy["sl"], "stop_loss"
        sell_price = float(next_bars[-1].get("close", 0))
        ret = (sell_price - buy_cost) / buy_cost * 100
        return ret, "close"

    elif strategy_type == "trix":
        today_bars = strategy.get("today_bars", [])
        all_bars = today_bars + next_bars
        if len(all_bars) < 20:
            sell_price = float(next_bars[-1].get("close", 0))
            ret = (sell_price - buy_cost) / buy_cost * 100
            return ret, "close"
        warmup_len = len(today_bars)
        closes = [float(b.get("close", 0)) for b in all_bars]
        trix = calc_trix(closes, 5)
        signal = _ema(trix, 3)
        for i in range(max(warmup_len, 20), len(all_bars)):
            if trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
                sell_price = closes[i]
                ret = (sell_price - buy_cost) / buy_cost * 100
                return ret, "trix_death_cross"
        sell_price = closes[-1]
        ret = (sell_price - buy_cost) / buy_cost * 100
        return ret, "close"

    return 0.0, "unknown"


def _calc_stats(rets):
    if not rets:
        return {}
    wins = sum(1 for r in rets if r > 0)
    cum = 1.0
    for r in rets:
        cum *= (1 + r / 100)
    cum_pct = (cum - 1) * 100
    avg = sum(rets) / len(rets)
    if len(rets) > 1:
        ann = (cum ** (250 / len(rets)) - 1) * 100
        var = sum((r - avg) ** 2 for r in rets) / len(rets)
        std = var ** 0.5
        sharpe = (avg / std * (250 ** 0.5)) if std > 0 else 0
    else:
        ann = 0
        sharpe = 0
    peak = 1.0
    equity = 1.0
    max_dd = 0.0
    for r in rets:
        equity *= (1 + r / 100)
        if equity > peak:
            peak = equity
        dd = (equity - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd
    return {
        "cum": cum_pct, "ann": ann, "win_rate": wins / len(rets) * 100,
        "sharpe": sharpe, "max_dd": max_dd, "avg": avg, "total": len(rets),
    }


def run_search(etf_daily, etf_5min, signal_times, buy_times, sell_times,
               strategies, start_date, end_date, fee_pct=0.03):
    """搜索所有时间组合 × 策略。"""
    all_dates = set()
    for info in etf_daily.values():
        for r in info["returns"]:
            all_dates.add(r["date"])
    all_dates = sorted(all_dates)

    eval_dates = all_dates[SCORE_WINDOW:-1]
    if start_date:
        eval_dates = [d for d in eval_dates if d >= start_date]
    if end_date:
        eval_dates = [d for d in eval_dates if d <= end_date]

    results = []

    for signal_time in signal_times:
        for buy_time in buy_times:
            # 买入时间必须 > 信号时间
            if time_str_to_min(buy_time) <= time_str_to_min(signal_time):
                continue
            # 买入时间不超过 14:55
            if time_str_to_min(buy_time) > time_str_to_min("14:55"):
                continue
            # 买入时间和信号时间必须在同一交易时段（都上午或都下午）
            signal_is_am = time_str_to_min(signal_time) < time_str_to_min("11:30")
            buy_is_am = time_str_to_min(buy_time) < time_str_to_min("11:30")
            if signal_is_am != buy_is_am:
                continue

            for sell_time in sell_times:
                # 卖出时间(次日)必须 < 买入时间(当日)
                if time_str_to_min(sell_time) >= time_str_to_min(buy_time):
                    continue

                for strat in strategies:
                    rets = []
                    for date in eval_dates:
                        date_idx = all_dates.index(date)
                        next_date = all_dates[date_idx + 1] if date_idx + 1 < len(all_dates) else None
                        if not next_date:
                            continue

                        # 1. 用信号时间前的数据算 v6 得分
                        scores = []
                        for code, info in etf_daily.items():
                            returns = info["returns"]
                            idx_map = {r["date"]: i for i, r in enumerate(returns)}
                            if date not in idx_map:
                                continue
                            idx = idx_map[date]
                            if idx < SCORE_WINDOW:
                                continue

                            min_info = etf_5min.get(code, {})
                            bars_by_date = min_info.get("bars_by_date", {})
                            today_bars = bars_by_date.get(date, [])

                            # 用信号时间前的分钟线重构当日数据
                            partial_close, partial_vol = get_partial_close(today_bars, signal_time)
                            if partial_close and partial_close > 0:
                                modified_returns = list(returns)
                                if idx > 0:
                                    prev_close = returns[idx - 1]["close"]
                                    partial_ret = ((partial_close - prev_close) / prev_close * 100) if prev_close else 0.0
                                else:
                                    partial_ret = 0.0
                                modified_returns[idx] = {
                                    "date": returns[idx]["date"],
                                    "close": partial_close,
                                    "return_pct": partial_ret,
                                    "volume": partial_vol,
                                }
                                score = compute_v6_score(modified_returns, idx)
                            else:
                                score = compute_v6_score(returns, idx)

                            scores.append((code, info["name"], info["etf_code"], score))

                        if len(scores) < 2:
                            continue

                        scores.sort(key=lambda x: x[3], reverse=True)
                        code, name, etf_code, score = scores[0]

                        # 2. 找买入价
                        min_info = etf_5min.get(code, {})
                        bars_by_date = min_info.get("bars_by_date", {})
                        today_bars = bars_by_date.get(date, [])
                        buy_price = get_price_at_time(today_bars, buy_time)

                        if not buy_price or buy_price <= 0:
                            returns = etf_daily[code]["returns"]
                            idx_map = {r["date"]: i for i, r in enumerate(returns)}
                            if date in idx_map:
                                buy_price = returns[idx_map[date]]["close"]
                            else:
                                continue

                        # 3. 次日卖出
                        next_bars_full = bars_by_date.get(next_date, [])
                        if not next_bars_full:
                            continue

                        # 截取到卖出时间的 K 线
                        sell_bars = get_bars_until_time(next_bars_full, sell_time)
                        if not sell_bars:
                            continue

                        # 策略执行
                        strat_copy = dict(strat)
                        if strat_copy["type"] == "trix":
                            strat_copy["today_bars"] = today_bars

                        ret, reason = simulate_sell(buy_price, sell_bars, strat_copy)
                        ret -= fee_pct * 2  # 扣手续费
                        rets.append(ret)

                    if not rets:
                        continue

                    stats = _calc_stats(rets)
                    results.append({
                        "signal": signal_time,
                        "buy": buy_time,
                        "sell": sell_time,
                        "strategy": strat["label"],
                        "stats": stats,
                    })

    return results


def main():
    parser = argparse.ArgumentParser(description="时间组合搜索")
    parser.add_argument("--lookback", type=int, default=100)
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="")
    parser.add_argument("--fee", type=float, default=0.03)
    parser.add_argument("--top", type=int, default=30, help="显示前 N 个最优组合")
    args = parser.parse_args()

    print(f"=== 时间组合搜索 ===")
    print(f"数据: {args.lookback} 天, 日期范围: {args.start_date} ~ {args.end_date}")
    print(f"手续费: 万{args.fee * 100:.0f}")
    print()

    # 1. 获取数据
    print(">>> 获取板块列表（平安证券）...")
    sectors = load_pingan_sectors()
    print(f"    {len(sectors)} 个板块（均有 ETF）")

    etf_sectors = sectors
    print(f">>> 获取 {len(etf_sectors)} 个 ETF 的日K线 + 5分钟K线...")

    etf_daily = {}
    etf_5min = {}
    for i, sec in enumerate(etf_sectors):
        etf_code, etf_name = sec["etf_code"], sec["etf_name"]
        daily_klines = fetch_daily_kline(etf_code, datalen=args.lookback)
        if not daily_klines or len(daily_klines) < SCORE_WINDOW + 1:
            continue
        returns = compute_daily_from_klines(daily_klines)
        etf_daily[sec["code"]] = {"name": sec["name"], "etf_code": etf_code, "returns": returns}

        min_klines = fetch_5min_kline(etf_code, datalen=5000, scale=5)
        if not min_klines:
            continue
        bars_by_date = {}
        for bar in min_klines:
            day = bar.get("day", "")[:10]
            if day:
                bars_by_date.setdefault(day, []).append(bar)
        etf_5min[sec["code"]] = {"name": sec["name"], "etf_code": etf_code, "bars_by_date": bars_by_date}

        if (i + 1) % 10 == 0:
            print(f"    进度: {i+1}/{len(etf_sectors)}")

    print(f"    完成: 日K {len(etf_daily)} 个, 分钟K {len(etf_5min)} 个")

    # 2. 定义搜索空间 — 覆盖全天交易时段
    # 信号时间: 9:30~14:45 每15分钟（不含午休 11:30~13:00）
    signal_times = [
        "09:30", "09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15",
        "13:00", "13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45",
    ]
    # 买入时间: 信号后5分钟
    buy_times = [
        "09:35", "09:50", "10:05", "10:20", "10:35", "10:50", "11:05", "11:20",
        "13:05", "13:20", "13:35", "13:50", "14:05", "14:20", "14:35", "14:50",
    ]
    # 卖出时间(次日): 9:35~14:50 每15分钟
    sell_times = [
        "09:35", "09:50", "10:05", "10:20", "10:35", "10:50", "11:05", "11:20",
        "13:00", "13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45",
    ]

    strategies = [
        {"type": "time", "label": "定时卖"},
        {"type": "trailing", "label": "追踪触3%落0.5%止-0.5%", "trigger": 3.0, "drop": 0.5, "stop_loss": -0.5},
        {"type": "trailing", "label": "追踪触3%落1%止-0.5%", "trigger": 3.0, "drop": 1.0, "stop_loss": -0.5},
        {"type": "trailing", "label": "追踪触3%落0.5%止-1%", "trigger": 3.0, "drop": 0.5, "stop_loss": -1.0},
        {"type": "fixed", "label": "固定+4%/-1%", "tp": 4.0, "sl": -1.0},
        {"type": "fixed", "label": "固定+2%/-1%", "tp": 2.0, "sl": -1.0},
        {"type": "trix", "label": "TRIX死叉"},
    ]

    total_combos = 0
    for s in signal_times:
        for b in buy_times:
            if time_str_to_min(b) <= time_str_to_min(s):
                continue
            for sel in sell_times:
                if time_str_to_min(sel) >= time_str_to_min(b):
                    continue
                total_combos += len(strategies)

    print(f"\n>>> 搜索 {total_combos} 种组合...")
    print(f"    信号时间: {signal_times}")
    print(f"    买入时间: {buy_times}")
    print(f"    卖出时间: {sell_times}")
    print(f"    策略数: {len(strategies)}")

    results = run_search(etf_daily, etf_5min, signal_times, buy_times, sell_times,
                         strategies, args.start_date, args.end_date, args.fee)

    # 3. 输出结果
    print(f"\n{'=' * 130}")
    print(f"  时间组合搜索结果（共 {len(results)} 组有效结果，按累计收益排序前 {args.top}）")
    print(f"{'=' * 130}")
    print(f"  {'信号':>6} {'买入':>6} {'卖出':>6} {'策略':>28} {'累计%':>10} {'年化%':>9} {'胜率%':>6} {'夏普':>5} {'回撤%':>7} {'均笔':>7}")
    print(f"  {'─' * 120}")

    for r in sorted(results, key=lambda x: x["stats"]["cum"], reverse=True)[:args.top]:
        s = r["stats"]
        print(f"  {r['signal']:>6} {r['buy']:>6} {r['sell']:>6} {r['strategy']:>28} "
              f"{s['cum']:+9.2f}% {s['ann']:+8.1f}% {s['win_rate']:5.1f}% "
              f"{s['sharpe']:4.2f} {s['max_dd']:+6.2f}% {s['avg']:+6.3f}%")

    # 4. 最优组合
    best = max(results, key=lambda x: x["stats"]["cum"])
    best_sharpe = max(results, key=lambda x: x["stats"]["sharpe"])
    best_dd = max(results, key=lambda x: x["stats"]["max_dd"])

    print()
    print(f"  ★ 累计最优: 信号{best['signal']} 买{best['buy']} 卖{best['sell']} {best['strategy']}")
    print(f"    累计{best['stats']['cum']:+.2f}% 年化{best['stats']['ann']:+.1f}% 夏普{best['stats']['sharpe']:.2f} 回撤{best['stats']['max_dd']:+.2f}%")
    print(f"  ★ 夏普最优: 信号{best_sharpe['signal']} 买{best_sharpe['buy']} 卖{best_sharpe['sell']} {best_sharpe['strategy']}")
    print(f"    累计{best_sharpe['stats']['cum']:+.2f}% 夏普{best_sharpe['stats']['sharpe']:.2f}")
    print(f"  ★ 回撤最小: 信号{best_dd['signal']} 买{best_dd['buy']} 卖{best_dd['sell']} {best_dd['strategy']}")
    print(f"    累计{best_dd['stats']['cum']:+.2f}% 回撤{best_dd['stats']['max_dd']:+.2f}%")
    print(f"{'=' * 130}")

    # 5. 保存
    cache_dir = Path.home() / ".tradingagents" / "rotation"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"search_combo_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump({
            "config": {
                "lookback": args.lookback,
                "start_date": args.start_date,
                "end_date": args.end_date,
                "fee": args.fee,
            },
            "results": results,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n搜索数据已保存: {cache_file}")


if __name__ == "__main__":
    main()

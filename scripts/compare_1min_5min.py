#!/usr/bin/env python3
"""1分钟线 vs 5分钟线 偏差对比

用东方财富分时API拉最近5天的1分钟线，同时用新浪拉5分钟线，
对比追踪止盈策略在两种粒度下的卖出价差异。

用法:
    python scripts/compare_1min_5min.py
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT = 15


def etf_secid(etf_code):
    """ETF代码转东方财富secid格式。"""
    if etf_code.startswith("5"):
        return f"1.{etf_code}"
    return f"0.{etf_code}"


def fetch_1min_kline_em(etf_code, ndays=5):
    """用东方财富分时API拉1分钟K线。"""
    secid = etf_secid(etf_code)
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/trends2/get?"
        f"fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13&"
        f"fields2=f51,f52,f53,f54,f55,f56,f57,f58&"
        f"ut=7eea3edcaed734bea9cbfc24409ed989&ndays={ndays}&iscr=0&secid={secid}"
    )
    cmd = ["curl", "-s", "--connect-timeout", str(TIMEOUT), "--noproxy", "*",
           "-H", "User-Agent: Mozilla/5.0", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 10)
        text = r.stdout.decode("utf-8")
        d = json.loads(text)
        trends = d.get("data", {}).get("trends", [])
        if not trends:
            return {}
        # 按日期分组
        bars_by_date = {}
        for t in trends:
            parts = t.split(",")
            if len(parts) < 7:
                continue
            datetime_str = parts[0]  # "2026-07-10 09:31"
            day = datetime_str[:10]
            time_part = datetime_str[11:16]
            bars_by_date.setdefault(day, []).append({
                "day": datetime_str,
                "time": time_part,
                "open": float(parts[1]) if parts[1] else 0,
                "close": float(parts[2]) if parts[2] else 0,
                "high": float(parts[3]) if parts[3] else 0,
                "low": float(parts[4]) if parts[4] else 0,
                "volume": float(parts[5]) if parts[5] else 0,
            })
        return bars_by_date
    except Exception as e:
        print(f"  东方财富1分钟线获取失败 {etf_code}: {e}")
        return {}


def curl_get(url):
    for use_proxy in [False, True]:
        cmd = ["curl", "-s", "--connect-timeout", str(TIMEOUT)]
        if use_proxy:
            cmd += ["-x", os.environ.get("ROTATION_PROXY", "http://127.0.0.1:7890")]
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


def fetch_5min_kline_sina(etf_code, datalen=5000):
    """用新浪拉5分钟K线。"""
    sina_sym = f"sh{etf_code}" if etf_code.startswith("5") else f"sz{etf_code}"
    url = (
        f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={sina_sym}&scale=5&ma=no&datalen={datalen}"
    )
    raw = curl_get(url)
    time.sleep(0.2)
    if not raw or raw.strip() in ("null", "", "[]"):
        return {}
    try:
        klines = json.loads(raw)
    except:
        return {}
    bars_by_date = {}
    for k in klines:
        day_full = k.get("day", "")
        day = day_full[:10]
        time_part = day_full[11:16] if len(day_full) > 14 else ""
        bars_by_date.setdefault(day, []).append({
            "day": day_full,
            "time": time_part,
            "open": float(k.get("open", 0)),
            "close": float(k.get("close", 0)),
            "high": float(k.get("high", 0)),
            "low": float(k.get("low", 0)),
            "volume": float(k.get("volume", 0)),
        })
    return bars_by_date


def fetch_daily_kline(etf_code, datalen=100):
    """拉日K线用于算v6得分。"""
    sina_sym = f"sh{etf_code}" if etf_code.startswith("5") else f"sz{etf_code}"
    url = (
        f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={sina_sym}&scale=240&ma=no&datalen={datalen}"
    )
    raw = curl_get(url)
    time.sleep(0.2)
    if not raw or raw.strip() in ("null", "", "[]"):
        return []
    try:
        return json.loads(raw)
    except:
        return []


def compute_daily(klines):
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


def compute_v6_score(returns, idx):
    window = 3
    if idx < window:
        return 0.0
    ret_w = sum(r["return_pct"] for r in returns[idx - window + 1:idx + 1])
    vol_today = returns[idx].get("volume", 0)
    vol_prev = [returns[j].get("volume", 0) for j in range(max(0, idx - 5), idx)]
    avg_vol = sum(vol_prev) / len(vol_prev) if vol_prev and sum(vol_prev) > 0 else vol_today
    vol_ratio = vol_today / avg_vol if avg_vol > 0 else 1.0
    vol_factor = 0.3 + 0.7 * min(vol_ratio / 1.5, 1.0)
    return ret_w * vol_factor


def get_price_at_time(bars, target_time):
    """找最接近 target_time 的 bar 的 close。"""
    target_min = int(target_time[:2]) * 60 + int(target_time[3:5])
    best_price = None
    best_diff = 999
    for bar in bars:
        t = bar.get("time", "")
        if t and len(t) >= 5:
            bar_min = int(t[:2]) * 60 + int(t[3:5])
            diff = abs(bar_min - target_min)
            if diff < best_diff:
                best_diff = diff
                best_price = bar.get("close", 0)
    return best_price


def get_bars_until(bars, cutoff_time):
    cutoff_min = int(cutoff_time[:2]) * 60 + int(cutoff_time[3:5])
    result = []
    for bar in bars:
        t = bar.get("time", "")
        if t and len(t) >= 5:
            bar_min = int(t[:2]) * 60 + int(t[3:5])
            if bar_min <= cutoff_min:
                result.append(bar)
    return result


def simulate_trailing(buy_cost, bars, trigger=3.0, drop=0.5, stop_loss=-0.5):
    """追踪止盈模拟，返回(收益率, 卖出原因, 卖出价)。"""
    trigger_price = buy_cost * (1 + trigger / 100)
    stop_price = buy_cost * (1 + stop_loss / 100) if stop_loss < 0 else 0
    tracking = False
    peak_high = 0.0

    for bar in bars:
        bar_high = bar.get("high", 0)
        bar_low = bar.get("low", 0)

        if stop_loss < 0 and bar_low <= stop_price:
            return stop_loss, "stop_loss", stop_price

        if not tracking and bar_high >= trigger_price:
            tracking = True
            peak_high = bar_high
        elif tracking and bar_high > peak_high:
            peak_high = bar_high

        if tracking:
            trail_sell = peak_high * (1 - drop / 100)
            if bar_low <= trail_sell:
                ret = (trail_sell - buy_cost) / buy_cost * 100
                return ret, "trailing_stop", trail_sell

    sell_price = bars[-1].get("close", 0) if bars else buy_cost
    ret = (sell_price - buy_cost) / buy_cost * 100
    return ret, "close", sell_price


def main():
    print("=== 1分钟线 vs 5分钟线 偏差对比 ===")
    print()

    # 选几只ETF测试
    test_etfs = [
        ("159995", "芯片ETF"),
        ("512400", "有色金属ETF"),
        ("515220", "煤炭ETF"),
        ("159997", "电子ETF"),
        ("512800", "银行ETF"),
    ]

    buy_time = "09:50"
    sell_time = "09:35"

    all_results = []

    for etf_code, etf_name in test_etfs:
        print(f">>> {etf_code} {etf_name}")

        # 拉1分钟线（最近5天）
        bars_1min = fetch_1min_kline_em(etf_code, ndays=5)
        if not bars_1min:
            print(f"    1分钟线获取失败，跳过")
            continue
        print(f"    1分钟线: {len(bars_1min)} 天 ({sorted(bars_1min.keys())})")

        # 拉5分钟线（取同样的天数）
        bars_5min = fetch_5min_kline_sina(etf_code, datalen=5000)
        if not bars_5min:
            print(f"    5分钟线获取失败，跳过")
            continue
        # 只取1分钟线覆盖的日期
        common_dates = sorted(set(bars_1min.keys()) & set(bars_5min.keys()))
        print(f"    5分钟线: {len(bars_5min)} 天, 共同日期: {len(common_dates)} 天")

        if len(common_dates) < 2:
            print(f"    共同日期不足2天，无法回测")
            continue

        # 逐天对比
        for i in range(len(common_dates) - 1):
            buy_date = common_dates[i]
            sell_date = common_dates[i + 1]

            # 买入价（两种粒度都用5分钟线的买入价，保证一致）
            buy_bars_5 = bars_5min.get(buy_date, [])
            buy_price = get_price_at_time(buy_bars_5, buy_time)
            if not buy_price or buy_price <= 0:
                continue

            # 次日卖出bar
            sell_bars_1 = bars_1min.get(sell_date, [])
            sell_bars_5 = bars_5min.get(sell_date, [])

            # 截取到卖出时间
            sell_bars_1_cut = get_bars_until(sell_bars_1, sell_time)
            sell_bars_5_cut = get_bars_until(sell_bars_5, sell_time)

            if not sell_bars_1_cut or not sell_bars_5_cut:
                continue

            # 1分钟线回测
            ret_1, reason_1, price_1 = simulate_trailing(buy_price, sell_bars_1_cut)
            # 5分钟线回测
            ret_5, reason_5, price_5 = simulate_trailing(buy_price, sell_bars_5_cut)

            diff = ret_1 - ret_5
            all_results.append({
                "etf": etf_code,
                "name": etf_name,
                "buy_date": buy_date,
                "sell_date": sell_date,
                "buy_price": buy_price,
                "ret_1min": ret_1,
                "ret_5min": ret_5,
                "diff": diff,
                "reason_1min": reason_1,
                "reason_5min": reason_5,
                "sell_price_1min": price_1,
                "sell_price_5min": price_5,
            })

            print(f"    {buy_date}→{sell_date}: 买={buy_price:.3f} "
                  f"1分钟={ret_1:+.2f}%({reason_1}) 5分钟={ret_5:+.2f}%({reason_5}) "
                  f"偏差={diff:+.2f}%")

    # 汇总
    print()
    print("=" * 90)
    print("  1分钟线 vs 5分钟线 偏差汇总")
    print("=" * 90)

    if not all_results:
        print("  无有效数据")
        return

    print(f"  样本数: {len(all_results)}")
    print()

    diffs = [r["diff"] for r in all_results]
    avg_diff = sum(diffs) / len(diffs)
    abs_diffs = [abs(d) for d in diffs]
    avg_abs_diff = sum(abs_diffs) / len(abs_diffs)
    max_diff = max(diffs, key=abs)

    print(f"  平均偏差(1分钟-5分钟): {avg_diff:+.3f}%")
    print(f"  平均绝对偏差: {avg_abs_diff:.3f}%")
    print(f"  最大偏差: {max_diff:+.3f}%")
    print()

    # 原因对比
    same_reason = sum(1 for r in all_results if r["reason_1min"] == r["reason_5min"])
    print(f"  卖出原因一致: {same_reason}/{len(all_results)} = {same_reason/len(all_results)*100:.0f}%")
    print()

    # 逐笔明细
    print("  逐笔明细:")
    print(f"  {'ETF':>8} {'买入日':>12} {'买入价':>8} {'1分钟收益':>10} {'5分钟收益':>10} {'偏差':>8} {'1分钟原因':>12} {'5分钟原因':>12}")
    for r in all_results:
        print(f"  {r['etf']:>8} {r['buy_date']:>12} {r['buy_price']:8.3f} "
              f"{r['ret_1min']:+9.2f}% {r['ret_5min']:+9.2f}% {r['diff']:+7.2f}% "
              f"{r['reason_1min']:>12} {r['reason_5min']:>12}")

    print()
    print("=" * 90)
    if avg_abs_diff < 0.3:
        print(f"  结论: 偏差很小（平均{avg_abs_diff:.2f}%），5分钟线回测结果可信")
    elif avg_abs_diff < 1.0:
        print(f"  结论: 偏差中等（平均{avg_abs_diff:.2f}%），5分钟线回测有参考价值但需注意误差")
    else:
        print(f"  结论: 偏差较大（平均{avg_abs_diff:.2f}%），5分钟线回测不可信，需要1分钟线")
    print("=" * 90)


if __name__ == "__main__":
    main()

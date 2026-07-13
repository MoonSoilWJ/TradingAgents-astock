#!/usr/bin/env python3
"""T+0 ETF 隔夜跳空信号回测 — 用隔夜涨跌替代 v6 动量。

信号逻辑：T+0 跨境 ETF 的隔夜跳空反映海外市场涨跌。
  - 隔夜跳空 = (今日开盘 - 昨日收盘) / 昨日收盘 × 100
  - 选隔夜跳空最大的 TOP1 ETF（海外利好最显著）
  - 开盘后涨 0.5% 追入，日内追踪止盈 +2% / 止损 -1.5% / 收盘卖

用法:
    python scripts/backtest_t0_overnight.py --days 30
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
from t0_etf_list import get_all_t0_etfs  # noqa: E402

SINA_INTERVAL = 0.25
SIGNAL_TIME = "09:30"  # 用开盘价算隔夜跳空
BUY_UP = 0.5
BUY_DOWN = 99.0  # 不抄底
BUY_REBOUND = 0.3
STOP_LOSS = -1.5
TRAIL_TRIGGER = 2.0
TRAIL_DROP = 0.5
FEE_PCT = 0.03


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
        op = float(k.get("open", close))
        if i == 0:
            ret = 0.0
        else:
            prev = float(klines[i - 1].get("close", 0))
            ret = ((close - prev) / prev * 100) if prev else 0.0
        result.append({"date": k.get("day", ""), "open": op, "close": close, "return_pct": ret})
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


def apply_net_return(buy_price: float, sell_price: float, fee_pct: float) -> float:
    buy_cost = buy_price * (1 + fee_pct / 100)
    sell_income = sell_price * (1 - fee_pct / 100)
    return (sell_income - buy_cost) / buy_cost * 100


def rank_by_overnight_gap(
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    signal_date: str,
) -> list[tuple[float, dict, float, float]]:
    """按隔夜跳空排名。返回 [(gap_pct, etf_info, open_price, prev_close), ...]"""
    scores = []
    for etf_info in etf_list:
        code = etf_info["code"]
        info = etf_daily.get(code)
        if not info:
            continue
        returns = info["returns"]
        idx_map = {r["date"]: i for i, r in enumerate(returns)}
        if signal_date not in idx_map or idx_map[signal_date] == 0:
            continue
        idx = idx_map[signal_date]
        prev_close = returns[idx - 1]["close"]
        today_open = returns[idx].get("open", 0)
        if not today_open or not prev_close:
            # 用5分K第一根open
            bars = etf_5min.get(code, {}).get(signal_date, [])
            if bars:
                today_open = bars[0]["open"]
            else:
                continue
        gap = (today_open - prev_close) / prev_close * 100 if prev_close else 0
        scores.append((gap, etf_info, today_open, prev_close))
    scores.sort(key=lambda x: x[0], reverse=True)
    return scores


def run_overnight_backtest(
    etf_list: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    eval_dates: list[str],
    fee_pct: float,
) -> dict:
    trades: list[dict] = []
    daily_log: list[dict] = []

    def log(day: str, time: str, action: str, **extra):
        daily_log.append({"date": day, "time": time, "action": action, **extra})

    for day in eval_dates:
        # 隔夜跳空排名
        scores = rank_by_overnight_gap(etf_list, etf_daily, etf_5min, day)
        if len(scores) < 2:
            continue
        gap, top1, open_price, prev_close = scores[0]
        code = top1["code"]

        # 只做跳空高开 1~4%（过大跳空容易反转）
        if gap < 1.0 or gap > 4.0:
            log(day, "09:30", "跳过", sector=top1["name"], reason=f"隔夜跳空{gap:+.2f}%不在1~4%")
            continue

        log(day, "09:30", "信号",
            sector=top1["name"], etf=code,
            gap=f"{gap:+.2f}%", open=open_price, prev_close=prev_close)

        # 买入检查：开盘后涨 0.5%
        day_bars = etf_5min.get(code, {}).get(day, [])
        buy_bars = [b for b in day_bars if bar_time_min(b) >= time_to_min("09:30")]
        buy_price, buy_reason, buy_time = check_buy_trigger(
            buy_bars, open_price, BUY_UP, BUY_DOWN, BUY_REBOUND,
        )

        if buy_price is None:
            log(day, "15:00", "观察", sector=top1["name"], etf=code, status="未触发买入")
            continue

        buy_bar_min = bar_time_min({"time": buy_time.split(" ")[-1][:5]}) if buy_time else 570

        # T+0 卖出
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
            "gap_pct": gap,
            "buy_price": buy_price,
            "buy_time": buy_time,
            "buy_reason": buy_reason,
            "sell_date": day,
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
        if daily and len(daily) > 3:
            etf_daily[code] = {"returns": compute_daily_data(daily)}
        m5 = fetch_5min_kline(sym, datalen=datalen_5m)
        if m5:
            etf_5min[code] = normalize_5min_bars(m5)
        if (i + 1) % 20 == 0:
            print(f"    进度 {i+1}/{len(etf_list)} 日K={len(etf_daily)} 5分K={len(etf_5min)}")
    all_dates = sorted({r["date"] for info in etf_daily.values() for r in info["returns"]})
    m5_dates = sorted({d for bars in etf_5min.values() for d in bars})
    if m5_dates:
        all_dates = sorted(set(all_dates) | set(m5_dates))
    return etf_daily, etf_5min, all_dates


def print_report(result: dict, eval_days: int):
    print()
    print("=" * 80)
    print("  T+0 ETF 隔夜跳空信号回测（当天买当天卖）")
    print("=" * 80)
    print(f"  区间: {eval_days} 日 | 手续费万3 | T+0日内 | 仅做跳空高开")
    print(f"  买入: 开盘后涨{BUY_UP}% | 卖出: 止{STOP_LOSS}% 追踪+{TRAIL_TRIGGER}%落{TRAIL_DROP}% 收盘")
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
        from collections import Counter
        reasons = Counter(t["sell_reason"] for t in trades)
        print(f"  卖出原因: {dict(reasons)}")
        print(f"\n  {'信号日':>12} {'板块':14s} {'ETF':>8s} {'跳空':>6s} {'买入价':>7s} {'卖出价':>7s} {'卖出因':>6s} {'收益':>7s}")
        print("  " + "-" * 80)
        eq = 1.0
        for t in trades:
            eq *= (1 + t["return_pct"] / 100)
            print(f"  {t['signal_date']:>12} {t['sector']:14s} {t['etf']:>8s} "
                  f"{t['gap_pct']:+5.1f}% {t['buy_price']:7.4f} {t['sell_price']:7.4f} "
                  f"{t['sell_reason']:>6s} {t['return_pct']:+7.2f}% | 累计 {(eq-1)*100:+7.2f}%")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="T+0 ETF 隔夜跳空信号回测")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    args = parser.parse_args()

    etf_list = get_all_t0_etfs()
    print(f"=== T+0 ETF 隔夜跳空信号回测 ===")
    print(f"ETF池: {len(etf_list)} 只 | 回测 {args.days} 日 | T+0日内\n")

    etf_daily, etf_5min, all_dates = load_market_data(etf_list, args.days)
    if len(etf_daily) < 5:
        print("ERROR: 数据不足")
        sys.exit(1)

    eval_dates = all_dates[-args.days:]
    print(f"    日K {len(etf_daily)} ETF, 5分K {len(etf_5min)} ETF, {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}日)")

    result = run_overnight_backtest(etf_list, etf_daily, etf_5min, all_dates, eval_dates, args.fee)
    print_report(result, len(eval_dates))

    out = Path.home() / ".tradingagents" / "rotation" / f"backtest_t0_gap_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

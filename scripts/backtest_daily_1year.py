#!/usr/bin/env python3
"""日K近似回测 — 用日线 OHLC 近似模拟买卖触发，可回测 1 年+。

精度说明：
- 信号：v6 得分用日 K（与 5 分K版本一致）
- 买入：日 high >= 基准×1.01 则按基准×1.01 买入（近似 09:40 后追涨）
- 卖出：T+1 日用 OHLC 判断追踪止盈/收盘卖
- 过滤：前日涨幅>7%跳过（与 5 分K版本一致）

用法:
    python scripts/backtest_daily_1year.py --days 250
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

from backtest_top1 import _calc_stats, curl_get, fetch_sina_kline, compute_daily_data  # noqa: E402
from rotation_v6 import SCORE_WINDOW, compute_v6_score  # noqa: E402
from sector_etf_map import etf_to_sina_symbol, load_pingan_sectors  # noqa: E402

SINA_INTERVAL = 0.25
BUY_UP = 1.0
STOP_LOSS = -99.0  # 无止损
TRAIL_TRIGGER = 3.0
TRAIL_DROP = 0.5
FEE_PCT = 0.03
PREV_DAY_SURGE_LIMIT = 7.0


def apply_net_return(buy_price: float, sell_price: float, fee_pct: float) -> float:
    buy_cost = buy_price * (1 + fee_pct / 100)
    sell_income = sell_price * (1 - fee_pct / 100)
    return (sell_income - buy_cost) / buy_cost * 100


def run_daily_backtest(
    sectors: list[dict],
    etf_daily: dict,
    all_dates: list[str],
    eval_dates: list[str],
    fee_pct: float,
) -> dict:
    trades: list[dict] = []

    for day in eval_dates:
        # 1. 选 TOP1
        scores = []
        for sec in sectors:
            etf = sec["etf_code"]
            info = etf_daily.get(etf)
            if not info:
                continue
            returns = info["returns"]
            idx_map = {r["date"]: i for i, r in enumerate(returns)}
            if day not in idx_map:
                continue
            idx = idx_map[day]
            if idx < SCORE_WINDOW:
                continue
            # 前日暴涨过滤
            prev_ret = returns[idx - 1]["return_pct"] if idx > 0 else 0
            if prev_ret > PREV_DAY_SURGE_LIMIT:
                continue
            score = compute_v6_score(returns, idx)
            scores.append((score, sec, etf, idx, returns))

        if len(scores) < 2:
            continue

        scores.sort(key=lambda x: x[0], reverse=True)
        top1_sec, top1_etf, top1_idx, top1_returns = scores[0][1], scores[0][2], scores[0][3], scores[0][4]

        # 2. 基准价 = 当日 open（近似 09:40 价格）
        today_data = top1_returns[top1_idx]
        baseline = today_data.get("open", today_data["close"])
        if not baseline or baseline <= 0:
            continue

        # 3. 买入检查：日 high >= baseline * (1 + BUY_UP/100)
        buy_threshold = baseline * (1 + BUY_UP / 100)
        if today_data.get("high", 0) < buy_threshold:
            continue  # 当天没触发追涨

        buy_price = buy_threshold  # 按触发价买入

        # 4. T+1 卖出
        day_idx = all_dates.index(day) if day in all_dates else -1
        if day_idx < 0 or day_idx + 1 >= len(all_dates):
            continue
        next_day = all_dates[day_idx + 1]

        next_info = etf_daily.get(top1_etf, {})
        next_returns = next_info.get("returns", [])
        next_idx_map = {r["date"]: i for i, r in enumerate(next_returns)}
        if next_day not in next_idx_map:
            continue
        next_data = next_returns[next_idx_map[next_day]]

        next_high = next_data.get("high", next_data["close"])
        next_low = next_data.get("low", next_data["close"])
        next_close = next_data["close"]

        # 追踪止盈：high >= buy*1.03 且 low <= high*0.995
        trail_trigger_price = buy_price * (1 + TRAIL_TRIGGER / 100)
        if next_high >= trail_trigger_price:
            trail_sell_price = next_high * (1 - TRAIL_DROP / 100)
            if next_low <= trail_sell_price:
                sell_price = trail_sell_price
                sell_reason = "追踪止盈"
            else:
                sell_price = next_close
                sell_reason = "收盘(追踪未回落)"
        else:
            sell_price = next_close
            sell_reason = "收盘"

        ret = apply_net_return(buy_price, sell_price, fee_pct)
        trades.append({
            "signal_date": day,
            "sell_date": next_day,
            "sector": top1_sec["name"],
            "etf": top1_etf,
            "baseline": round(baseline, 4),
            "buy_price": buy_price,
            "sell_price": round(sell_price, 4),
            "sell_reason": sell_reason,
            "return_pct": ret,
            "prev_ret": top1_returns[top1_idx - 1]["return_pct"] if top1_idx > 0 else 0,
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
    }


def main():
    parser = argparse.ArgumentParser(description="日K近似回测（1年+）")
    parser.add_argument("--days", type=int, default=250, help="回测交易日数")
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    args = parser.parse_args()

    sectors = load_pingan_sectors()
    print(f"=== 日K近似回测 ===")
    print(f"板块池: 平安 {len(sectors)} 个 | 回测 {args.days} 日")
    print(f"买入: 涨{BUY_UP}%追涨(不抄底) | 卖出: 追踪+{TRAIL_TRIGGER}%落{TRAIL_DROP}% / T+1收盘 | 无止损")
    print(f"过滤: 前日涨>{PREV_DAY_SURGE_LIMIT}%跳过\n")

    # 拉日K（支持300+天）
    datalen = args.days + 20
    etf_daily: dict = {}
    print(f">>> 拉取 {len(sectors)} 只 ETF 日K (datalen={datalen})...")
    for i, sec in enumerate(sectors):
        sym = etf_to_sina_symbol(sec["etf_raw"])
        klines = fetch_sina_kline(sym, datalen=datalen)
        if klines and len(klines) > SCORE_WINDOW + 2:
            etf_daily[sec["etf_code"]] = {"returns": compute_daily_data(klines)}
        if (i + 1) % 20 == 0:
            print(f"    进度 {i+1}/{len(sectors)} 有数据{len(etf_daily)}")

    all_dates = sorted({r["date"] for info in etf_daily.values() for r in info["returns"]})
    eval_dates = all_dates[-args.days:]
    print(f"    日K {len(etf_daily)} ETF, {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}日)")

    result = run_daily_backtest(sectors, etf_daily, all_dates, eval_dates, args.fee)

    st = result["stats"]
    print(f"\n{'='*60}")
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

        # 分4段
        total = len(trades)
        seg = total // 4
        print(f"\n  分段收益:")
        for i in range(4):
            s = trades[i*seg:(i+1)*seg if i<3 else total]
            eq = 1.0
            wins = 0
            for t in s:
                eq *= (1 + t["return_pct"]/100)
                if t["return_pct"] > 0: wins += 1
            print(f"    第{i+1}段 {s[0]['signal_date']}~{s[-1]['sell_date']} ({len(s)}笔): {(eq-1)*100:+7.2f}% 胜率{wins}/{len(s)}={wins/len(s)*100:.0f}%")

        # 大亏大赚
        losses = [t for t in trades if t["return_pct"] < -3]
        gains = [t for t in trades if t["return_pct"] > 3]
        if losses:
            print(f"\n  大亏(>3%): {len(losses)}笔")
            for t in losses:
                print(f"    {t['signal_date']} {t['sector']:10s} {t['return_pct']:+.2f}% ({t['sell_reason']})")
        if gains:
            print(f"  大赚(>3%): {len(gains)}笔")
            for t in gains:
                print(f"    {t['signal_date']} {t['sector']:10s} {t['return_pct']:+.2f}% ({t['sell_reason']})")
    print(f"{'='*60}")

    out = Path.home() / ".tradingagents" / "rotation" / f"backtest_daily_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()

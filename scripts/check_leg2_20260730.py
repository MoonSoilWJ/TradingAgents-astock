#!/usr/bin/env python3
"""核对 2026-07-30 段2(leg2) 收益: 513360 买0.443@14:05 卖0.444@14:15 记录return=1.5192%"""
from __future__ import annotations
import sys
from pathlib import Path
from datetime import date

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_etf import fetch_5min_kline, normalize_5min_bars, price_at_time, apply_net_return  # noqa: E402
from backtest_t0_idle_window import sell_trix_mode, sell_time_mode  # noqa: E402

CODE = "513360"
SYM = "sh513360"
BUY_PRICE = 0.443
BUY_TIME = "14:05"
SELL_TIME = "14:15"

bars_by_day = normalize_5min_bars(fetch_5min_kline(SYM, datalen=500))
today = date.today().isoformat()
bars = bars_by_day.get(today, [])
print(f"今日 {CODE} 5min bars 数: {len(bars)}")
if bars:
    times = [b["time"] for b in bars]
    closes = [b["close"] for b in bars]
    print("时间范围:", times[0], "~", times[-1])
    print("14:00附近:", [(b["time"], b["close"]) for b in bars if b["time"] in ("14:00","14:05","14:10","14:15","14:20")])

# 记录中价
print(f"\n记录 buy_price={BUY_PRICE} sell_price=0.444 return_pct=1.5192%")
print(f"  按 0.443->0.444 正确净收益 = {apply_net_return(BUY_PRICE,0.444,0.03):.4f}%")

# 复算
if bars:
    p_buy = price_at_time(bars, BUY_TIME)
    p_sell = price_at_time(bars, SELL_TIME)
    print(f"\nprice_at_time(14:05)={p_buy}  price_at_time(14:15)={p_sell}")
    out_trix = sell_trix_mode(bars, BUY_TIME, SELL_TIME, BUY_PRICE, 0.03)
    out_time = sell_time_mode(bars, BUY_TIME, SELL_TIME, BUY_PRICE, 0.03)
    print(f"  sell_trix_mode -> {out_trix}")
    print(f"  sell_time_mode -> {out_time}")
    if out_trix:
        ret, reason = out_trix
        implied_sp = BUY_PRICE * (1 + ret/100) * (1+0.0003)/(1-0.0003)
        print(f"  trix返回ret={ret} 反推死叉价sp≈{implied_sp:.4f}")

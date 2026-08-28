# 聚宽回测代码 —— 588000 120分钟K TRIX 多参数投票 (run_daily版)
# 起始 2021-02-09, 结束 2026-08-27, 初始资金 100000
# 聚宽无120m, 用60m每2根合成120m。run_daily 15:00 执行=120分钟收盘。
from jqdata import *
import pandas as pd
import numpy as np
from datetime import timedelta

SECURITY = '588000.XSHG'
COMBOS = [(9,9),(9,12),(12,9),(12,12),(15,9),(15,12),(20,9)]

def trix_pos(close, N, M):
    s = pd.Series(close)
    e1 = s.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr = e3.pct_change() * 100.0
    sig = tr.rolling(M).mean()
    return (tr > sig).astype(int).values

def to_120(min60_close):
    arr = np.asarray(min60_close, dtype=float)
    if len(arr) % 2 != 0:
        arr = arr[1:]
    return arr.reshape(-1, 2).mean(axis=1)

def vote_target_120(context):
    # 当日14:58: 当日120分钟K(末根=14:00)已闭合, 用当日信号; 成交按当日价。
    # 取60分钟含当日, 合成120分钟。若末根(15:00)未闭合->丢弃, 避免NaN/错位。
    panel = get_price(SECURITY, end_date=context.current_dt, count=600,
                      frequency='60m', fields='close', skip_paused=True, fq='pre')
    if panel is None or len(panel) == 0:
        log.info("120 NO DATA")
        return 0
    close60 = panel['close'].dropna().values
    # 安全保护: 末根若异常(如15:00未闭合的NaN)则剔除
    if len(close60) == 0:
        log.info("120 EMPTY")
        return 0
    if np.isnan(close60[-1]):
        close60 = close60[:-1]
    if len(close60) < 240:
        log.info("120 TOO SHORT len=%d" % len(close60))
        return 0
    close120 = to_120(close60)
    states = np.column_stack([trix_pos(close120, n, m) for (n, m) in COMBOS])
    lr = float(states.mean(axis=1)[-1])
    log.info("120 vote long_ratio=%.3f -> %s" % (lr, "LONG" if lr > 0.5 else "CASH"))
    return 1 if lr > 0.5 else 0

def initialize(context):
    set_benchmark('588000.XSHG')
    log.info("INIT OK, sec=%s" % SECURITY)
    run_daily(trade, time='14:58')

def trade(context):
    tgt = vote_target_120(context)
    pos = context.portfolio.positions.get(SECURITY)
    holding = pos is not None and pos.closeable_amount > 0
    if tgt == 1 and not holding:
        order_target_value(SECURITY, context.portfolio.total_value)
    elif tgt == 0 and holding:
        order_target_value(SECURITY, 0)

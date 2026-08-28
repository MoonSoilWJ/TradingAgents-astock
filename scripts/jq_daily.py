# 聚宽回测代码 —— 588000 日线 TRIX 多参数投票 (run_daily版)
# 起始 2021-02-09, 结束 2026-08-27, 初始资金 100000
from jqdata import *
import pandas as pd
import numpy as np
from datetime import timedelta

SECURITY = '588000.XSHG'
COMBOS = [(9,9),(9,12),(12,9),(12,12),(15,9),(15,12),(20,9)]
LOOKBACK = 130

def trix_pos(close, N, M):
    s = pd.Series(close)
    e1 = s.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr = e3.pct_change() * 100.0
    sig = tr.rolling(M).mean()
    return (tr > sig).astype(int).values

def vote_target(context):
    # 14:58 取当日实时日线(含当日未收盘bar), 用当日信号判; 不再回看昨日
    panel = get_price(SECURITY, end_date=context.current_dt, count=LOOKBACK,
                      frequency='daily', fields='close', skip_paused=True, fq='pre')
    if panel is None or len(panel) == 0:
        log.info("DAILY NO DATA")
        return 0
    close = panel['close'].dropna().values
    if len(close) < max([n for n, _ in COMBOS]) + 30:
        log.info("DAILY TOO SHORT len=%d" % len(close))
        return 0
    # 当日这根是实时未收盘价, 用于判信号; 若末端为NaN(极少)则剔除
    if np.isnan(close[-1]):
        close = close[:-1]
    states = np.column_stack([trix_pos(close, n, m) for (n, m) in COMBOS])
    lr = float(states.mean(axis=1)[-1])
    log.info("DAILY vote long_ratio=%.3f -> %s" % (lr, "LONG" if lr > 0.5 else "CASH"))
    return 1 if lr > 0.5 else 0

def initialize(context):
    set_benchmark('588000.XSHG')
    log.info("INIT OK, sec=%s" % SECURITY)
    run_daily(trade, time='14:58')

def trade(context):
    tgt = vote_target(context)
    # 仅在状态变化时下单, 空仓时不清零(避免无持仓却下单报ERROR)
    pos = context.portfolio.positions.get(SECURITY)
    holding = pos is not None and pos.closeable_amount > 0
    if tgt == 1 and not holding:
        order_target_value(SECURITY, context.portfolio.total_value)
    elif tgt == 0 and holding:
        order_target_value(SECURITY, 0)

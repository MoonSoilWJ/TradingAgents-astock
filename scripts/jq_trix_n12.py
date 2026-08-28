# 聚宽(JoinQuant)回测策略: 日线 TRIX 多周期投票 (N集中到12周边)
# 多退路下单: order_target_percent / order_target_value / order, 防止名称注入失败
# ============================================================
from jqdata import *
import numpy as np
import pandas as pd

SECURITY = '588000.XSHG'
COMB  = [(10,9),(10,12),(12,9),(12,12),(14,9),(14,12)]  # N12 结果簇 (N集中到12周边, 收益/回撤均优于单N=12)
THR      = 0.5
LOOKBACK = 250

def initialize(context):
    set_option('use_real_price', True)
    set_benchmark(SECURITY)
    set_universe([SECURITY])
    set_order_cost(OrderCost(open_tax=0, close_tax=0,
                             open_commission=0.0003, close_commission=0.0003,
                             min_commission=0.0), type='fund')
    context.in_pos = False          # 用自身状态记录持仓, 不再每天查positions
    run_daily(trade, '15:00')

def trix(close, N, M):
    e1 = close.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr  = e3.pct_change() * 100
    sig = tr.rolling(M).mean()
    return tr, sig

def order_pct(context, sec, pct):
    """尽可能地下单: 优先 order_target_percent, 退化到 order_target_value / order。"""
    if 'order_target_percent' in globals():
        order_target_percent(sec, pct)
        return
    if 'order_target_value' in globals():
        order_target_value(sec, context.portfolio.total_value * pct)
        return
    if 'order' in globals():
        price = get_price(sec, end_date=context.current_dt, count=1,
                          fields='close', frequency='daily', fq=None)['close'].iloc[-1]
        if pct > 0:
            cash = context.portfolio.available_cash * 0.99
            order(sec, int(cash / price / 100) * 100)
        else:
            pos = context.portfolio.positions.get(sec)
            if pos and pos.closeable_amount > 0:
                order(  sec, -int(pos.closeable_amount) )
        return
    log.error("环境缺少下单函数")

def trade(context):
    hist = get_price(SECURITY, end_date=context.current_dt, count=LOOKBACK,
                     fields=['close'], frequency='daily', fq=None)
    if hist is None or len(hist) < LOOKBACK:
        return
    close = hist['close']
    votes = []
    for N, M in COMB:
        tr, sig = trix(close, N, M)
        votes.append(1 if tr.iloc[-1] > sig.iloc[-1] else 0)
    ratio = float(np.mean(votes))
    long_signal = ratio > THR
    log.info('date=%s vote=%.2f long=%s holding=%s' % (context.current_dt.date(), ratio, long_signal, context.in_pos))

    if long_signal and not context.in_pos:
        order_pct(context, SECURITY, 1.0)
        context.in_pos = True
    elif (not long_signal) and context.in_pos:
        order_pct(context, SECURITY, 0.0)
        context.in_pos = False

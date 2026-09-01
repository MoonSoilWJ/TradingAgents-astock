# -*- coding: utf-8 -*-
"""
科创50 N12 结果簇 + 防御组轮动 V3  (聚宽 JoinQuant · 分钟级版)
==============================================================
分钟级版 = 让"委托时点"真正生效。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么要分钟级
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  日线级实测(你的数据): run_daily 从 'open' 改成 '14:55' 后
      策略收益 544.61% → 537.40% (仅 -7.21pp)
      Alpha 0.36 / Beta 0.55 / Sharpe 1.34  完全不变
  若成交价真变了, 不可能只差 7pp。结论:
      **聚宽日线模式下 run_daily 的 time 不改变成交价, 都按当日 bar 撮合。**
      日线级改 time 只影响"信号里是否含当日价", 是装饰性的。
  分钟级才能真正按 14:55 那一刻的价格成交, 从而回答:
      「尾盘委托到底比开盘委托好还是差?」

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
两个开关怎么用 (做 A/B 实验的关键)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ★ 想【单纯对比执行时点】→ 固定 USE_TODAY_PRICE = False, 只改 ORDER_TIME:
        ORDER_TIME='09:31'  跑一次   (开盘委托)
        ORDER_TIME='14:55'  跑一次   (尾盘委托)
    此时两次的信号序列完全相同(都只用至 close[t-1]), 唯一变量就是成交时点 → 干净对照。

  ★ 想【对齐本地回测口径】→ USE_TODAY_PRICE = True:
        序列 = [历史收盘 … close[t-1]] + [14:55 当前价]
    等价于本地"用 close[t] 算信号并以 close[t] 成交", 与日线版 14:55 口径一致。

  注意: USE_TODAY_PRICE=True 时, 改 ORDER_TIME 会同时改变信号(因为"当前价"跟着时点变),
        此时两次结果的差异 = 信号差异 + 成交价差异, 不是干净的执行时点对照。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
聚宽设置 (和日线版不同, 注意改)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  · 回测频率: 【分钟线】 ← 必须改, 否则 time 不生效
  · 起止日期: 建议先用 2024-01-01 ~ 今天 试跑(快), 确认无误再跑 2020-11-16 全区间
  · 初始资金: 100 万
  · 基准: 000688.XSHG (科创50指数)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
预期
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  日线版参考:        开盘 544.61% / 14:55 537.40% / 回撤 20.3%~21.5%
  本地收盘成交:      全区间 655.8% / 跳过前90根 578.1%
  本地开盘价代理模拟: 389.1% (比收盘低 189pp)
  分钟级才是真实答案 —— 若分钟级 14:55 明显优于 09:31, 说明尾盘委托确实有优势;
  若两者接近, 说明策略对日内时点不敏感, 那 189pp 主要是本地代理模拟的偏差。
"""
import numpy as np
import pandas as pd

# ════════════════════════════════════════════════════════════════════
# 参数 (与本地生产脚本 / 日线版完全一致)
# ════════════════════════════════════════════════════════════════════
CORE = '588000.XSHG'                 # 科创50ETF (核心)
BOND = '511260.XSHG'                 # 国债ETF —— 保守位/全死叉兜底

DEF_POOL = [
    '511260.XSHG',                   # 国债ETF
    '518880.XSHG',                   # 黄金ETF
    '510880.XSHG',                   # 红利ETF
    '515080.XSHG',                   # 中证红利ETF
    '512890.XSHG',                   # 红利低波ETF
]

COMB_N12 = [(10, 9), (10, 12), (12, 9),
            (12, 12), (14, 9), (14, 12)]   # N12 结果簇 (6 个 TRIX 组合)
MIN_VOTES = 4                        # 6 票中最少看多票数 (等价本地 thr=0.5 → k≥4)

REGIME_N, REGIME_M = 14, 9           # 科创50 / 防御组 金叉判定: TRIX(14,9)
MOM_WIN = 20                         # 防御组动量窗口(日)

HIST_LEN = 150                       # 日线历史长度 (TRIX 预热 + 动量)
WARMUP = 90                          # 少于 90 根日线 → 不交易

# ── 委托时点 (分钟级下真实生效) ──────────────────────────────────────────
ORDER_TIME = '14:55'                 # '14:55'=尾盘 / '09:31'=开盘 (做 A/B 就改这里)
USE_TODAY_PRICE = False              # False=信号只用至 close[t-1](做时点A/B时必须 False)
                                     # True =把 ORDER_TIME 的当前价当 close[t] 补进信号

FQ = 'pre'                           # 前复权(含分红)
SLIPPAGE = 0.0005                    # 0.05%/单边


# ════════════════════════════════════════════════════════════════════
def initialize(context):
    set_benchmark('000688.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)

    set_slippage(FixedSlippage(SLIPPAGE))
    set_order_cost(OrderCost(
        open_tax=0, close_tax=0,
        open_commission=0, close_commission=0,     # 佣金已折算进滑点
        min_commission=0), type='fund')

    g.core = CORE
    g.pool = DEF_POOL
    g.universe = [CORE] + list(DEF_POOL)
    g.last_target = None

    run_daily(rebalance, time=ORDER_TIME)
    log.info('[分钟级] 初始化 | 委托时点 %s | 信号含当前价 %s | 核心 %s'
             % (ORDER_TIME, USE_TODAY_PRICE, CORE))


# ════════════════════════════════════════════════════════════════════
# TRIX: 三重EMA → pct_change*100 → M 日均线作信号线 (与本地逐字一致)
# ════════════════════════════════════════════════════════════════════
def _trix(close, N, M):
    s = pd.Series(np.asarray(close, dtype=float))
    e1 = s.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr = e3.pct_change() * 100.0
    sig = tr.rolling(M).mean()
    return tr, sig


def _current_price(sec):
    """取 ORDER_TIME 时刻的当前价 (分钟级下是真实盘中价)。

    优先 get_current_data().last_price; 取不到则退回最近一根已完成分钟 bar 的收盘。
    """
    try:
        px = get_current_data()[sec].last_price
        if px is not None and not np.isnan(px) and px > 0:
            return float(px)
    except Exception:
        pass
    try:
        df = attribute_history(sec, 1, unit='1m', fields=['close'],
                               df=True, skip_paused=True, fq=FQ)
        if df is not None and len(df) > 0:
            v = float(df['close'].iloc[-1])
            if v > 0:
                return v
    except Exception:
        pass
    return None


def _close_series(sec, count):
    """日线收盘序列 (+ 可选: 追加 ORDER_TIME 当前价)。"""
    df = attribute_history(sec, count, unit='1d', fields=['close'],
                           df=True, skip_paused=True, fq=FQ)
    if df is None or len(df) == 0:
        return None
    arr = df['close'].astype(float).values
    if USE_TODAY_PRICE:
        px = _current_price(sec)
        if px is not None:
            arr = np.append(arr, px)
    return arr


# ════════════════════════════════════════════════════════════════════
def rebalance(context):
    # ── 1. 取数 ──
    data = {}
    for sec in g.universe:
        c = _close_series(sec, HIST_LEN)
        if c is None or len(c) < WARMUP:
            return
        data[sec] = c

    kc = data[g.core]

    # ── 2. N12 结果簇投票 ──
    votes = 0
    for n, m in COMB_N12:
        tr, sig = _trix(kc, n, m)
        if np.isnan(tr.iloc[-1]) or np.isnan(sig.iloc[-1]):
            return
        if tr.iloc[-1] > sig.iloc[-1]:
            votes += 1
    core_on = (votes >= MIN_VOTES)

    # ── 3. 科创50 金叉状态 ──
    tr_k, sg_k = _trix(kc, REGIME_N, REGIME_M)
    if np.isnan(tr_k.iloc[-1]) or np.isnan(sg_k.iloc[-1]):
        return
    kc_golden = (tr_k.iloc[-1] > sg_k.iloc[-1])

    # ── 4. 定目标持仓 ──
    if core_on:
        target = g.core
        why = 'N12簇 %d/6 看多' % votes
    elif kc_golden:
        target = BOND
        why = '科创50金叉·簇未喊多(%d/6) → 保守国债' % votes
    else:
        best, best_mom = None, -1e18
        for c in g.pool:
            tr_c, sg_c = _trix(data[c], REGIME_N, REGIME_M)
            if np.isnan(tr_c.iloc[-1]) or np.isnan(sg_c.iloc[-1]):
                continue
            if tr_c.iloc[-1] <= sg_c.iloc[-1]:
                continue
            if len(data[c]) <= MOM_WIN:
                continue
            mom = data[c][-1] / data[c][-1 - MOM_WIN] - 1.0
            if not np.isnan(mom) and mom > best_mom:
                best_mom, best = mom, c
        if best is not None:
            target = best
            why = '科创50死叉 → 防御组最强[%s] 动量%.2f%%' % (best, best_mom * 100)
        else:
            target = BOND
            why = '科创50死叉·防御组全死叉 → 国债'

    # ── 5. 调仓: 单标的 100% 仓位 (先卖后买) ──
    if g.last_target != target:
        log.info('%s %s | %s' % (context.current_dt.date(), ORDER_TIME, why))

    for sec in list(context.portfolio.positions.keys()):
        if str(sec) != str(target) and context.portfolio.positions[sec].total_amount > 0:
            order_target_value(sec, 0)
    order_target_value(target, context.portfolio.total_value)

    g.last_target = target
    record(core_pos=1 if target == g.core else 0)

# -*- coding: utf-8 -*-
"""
科创50 N12 结果簇 + 防御组轮动 V3  (聚宽 JoinQuant 版)
======================================================
与本地生产脚本 scripts/backtest_588000_n12.py 逐条对齐, 含 2026-09 修复后的
「无未来函数」口径 + 修正后的年化口径。

本地基准 (2020-11-16 ~ 2026-09-01, 收盘成交, 全区间):
    累计 655.8%  年化 41.8%  最大回撤 19.0%  切换 218 次  空仓率 0%
    (纯 N12 基线: 累计 289.7%  年化 26.5%  回撤 19.0%)

本脚本在聚宽上【预期】看到的数字 (14:55 成交, 且跳过前 90 根预热):
    口径① (USE_TODAY_PRICE=True, 对齐本地)  累计 ~578%  年化 ~42%  回撤 ~19%  切换 ~202
    口径② (USE_TODAY_PRICE=False, 信号滞后一日) 累计 介于 389%~578% 之间
    参考基准:
        本地全区间(不跳过预热, 收盘成交)        累计 655.8%  年化 41.8%  回撤 19.0%
        本地同起点(跳过前90根, 收盘成交)        累计 578.0%  年化 42.6%
        本地同起点(跳过前90根, 开盘成交)        累计 389.1%  年化 34.2%
    IS(2020-11~2023-12) / OOS(2024-01~今) 见 scripts/verify_jq_port_expectation.py
⚠️ 开盘成交比收盘成交低约 -189pp, 这是【执行时序】差异(策略 218 次换仓, 每次进出
   都要承受/错过隔夜跳空, 微差被复利放大), 不是逻辑错误 —— 这也是改用 14:55 的原因。
⚠️ 聚宽用前复权(fq='pre')含分红, 红利类ETF收益会略高于本地未复权口径, 属正常。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
策略逻辑 (一句话)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  核心 588000 用 6 个 TRIX 组合投票 (N∈{10,12,14} × M∈{9,12}), 至少 4 票看多 → 持 588000;
  空仓时: 若科创50 TRIX(14,9) 死叉 → 在【国债/黄金/红利】里挑"仍金叉 且 20 日动量最强"的一只;
          若科创50 金叉但簇未喊多 → 保守持国债; 防御组全死叉 → 持国债。
  每次只持 1 只, 100% 仓位。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 委托时点 14:55 (尾盘) + 信号口径 (最关键, 先看懂再跑)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  【本地回测的真实口径】= "在 t 日收盘, 用 close[t] 算信号, 并以 close[t] 成交"。
  而 JoinQuant 日线模式下 attribute_history **不含当日 bar**(当日未走完),
  所以想在 14:55 复现本地口径, 必须把"当前价"手动补到序列末尾。两种选择:

  ① USE_TODAY_PRICE = True   (默认, 对齐本地 —— 推荐)
       序列 = [历史收盘 … close[t-1]] + [14:55 的当前价]
       → 等价于"拿 14:55 价当 close[t] 算信号并成交", 与本地口径一致。
       注1: 日线模式下 get_current_data().last_price 取到的很可能就是当日收盘价。
       注2: 历史用前复权、当前价是不复权价, 除息日可能有微小跳变(每年1~2次, 影响有限);
            想彻底避免就把 FQ 设为 None(但会丢分红, 收益偏低)。

  ② USE_TODAY_PRICE = False  (严格无未来, 但信号滞后一日)
       序列只用 [历史收盘 … close[t-1]], 仍在 14:55 下单。
       → 信号比本地慢一天, 结果会低于口径①(预期落在 389%~578% 之间)。

  两种都不存在"用尚未发生的价格决定已发生的收益"的未来函数。
  (本地原版曾写成"用 close[t] 的信号吃 t 日收益", 导致 N3-5 快簇虚高 38 倍, 已修。)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
聚宽设置建议
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  · 回测频率: 日线
  · 起止日期: 2020-11-16 ~ 今天 (与本地一致)
  · 初始资金: 100 万 (避免碎股/最小佣金干扰)
  · 基准: 000688.XSHG (科创50指数)
  · 注意: 588000 约 2020-09 上市, 回测前期需要 WARMUP=90 根日线预热,
          因此**实际首次建仓约在 2021 年初**, 之前为现金。这是设计内行为, 非 bug。
"""
import numpy as np
import pandas as pd

# ════════════════════════════════════════════════════════════════════
# 参数 (与本地生产脚本完全一致, 改这里等于改本地)
# ════════════════════════════════════════════════════════════════════
CORE = '588000.XSHG'                 # 科创50ETF (核心)
BOND = '511260.XSHG'                 # 国债ETF —— 保守位/全死叉兜底

# 防御组: 与科创50 低相关/负相关 (国债/黄金/红利)
DEF_POOL = [
    '511260.XSHG',                   # 国债ETF
    '518880.XSHG',                   # 黄金ETF
    '510880.XSHG',                   # 红利ETF
    '515080.XSHG',                   # 中证红利ETF
    '512890.XSHG',                   # 红利低波ETF
]

COMB_N12 = [(10, 9), (10, 12), (12, 9),
            (12, 12), (14, 9), (14, 12)]   # N12 结果簇 (6 个 TRIX 组合)
MIN_VOTES = 4                        # 6 票中最少看多票数 (等价本地 thr=0.5 → k>3 → k≥4)

REGIME_N, REGIME_M = 14, 9           # 科创50 / 防御组 金叉判定: TRIX(14,9)
MOM_WIN = 20                         # 防御组动量窗口(日): close[-1]/close[-21]-1

HIST_LEN = 150                       # 每次拉取的历史长度 (足够 TRIX 预热 + 动量)
WARMUP = 90                          # 少于 90 根日线 → 不交易 (指标未预热)

# ── 委托时点 ──────────────────────────────────────────────────────────────
ORDER_TIME = '14:55'                 # 尾盘委托 (买卖都在这个时点)
USE_TODAY_PRICE = True               # True = 把 14:55 当前价补进序列, 对齐本地"收盘算信号收盘成交"
                                     # False= 只用至 close[t-1] 的历史, 信号滞后一日(更保守)

FQ = 'pre'                           # 复权方式: 'pre' 前复权(含分红, 更真实)
                                     # 若要严格对齐本地(未复权收盘价), 改成 None
                                     # 注: 红利类ETF分红较多, 'pre' 下收益会略高于本地。

# 成本: 滑点 0.05%/单边 (对齐本地 SLIP=0.0005, 一次完整换仓=卖+买≈0.10%)
# 想含真实佣金: 把下面 commission 改成 0.0003, 并把 SLIPPAGE 降到 0.0002 (合计仍≈0.05%/边)
SLIPPAGE = 0.0005


# ════════════════════════════════════════════════════════════════════
def initialize(context):
    set_benchmark('000688.XSHG')                  # 科创50指数
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)         # 聚宽防未来函数开关

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
    log.info('初始化完成 | 委托时点 %s | 当前价口径 %s | 核心 %s | 防御池 %s'
             % (ORDER_TIME, USE_TODAY_PRICE, CORE, ','.join(DEF_POOL)))


# ════════════════════════════════════════════════════════════════════
# TRIX: 三重EMA → pct_change*100 → M 日均线作信号线 (与本地 trix_series 逐字一致)
# ════════════════════════════════════════════════════════════════════
def _trix(close, N, M):
    s = pd.Series(np.asarray(close, dtype=float))
    e1 = s.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr = e3.pct_change() * 100.0
    sig = tr.rolling(M).mean()
    return tr, sig


def _close_series(sec, count):
    """构造收盘序列 (聚宽日线模式下 attribute_history 不含当日 bar)。

    USE_TODAY_PRICE = True : [历史收盘 … close[t-1]] + [14:55 当前价]  → 对齐本地口径
    USE_TODAY_PRICE = False: [历史收盘 … close[t-1]]                   → 信号滞后一日
    """
    df = attribute_history(sec, count, unit='1d', fields=['close'],
                           df=True, skip_paused=True, fq=FQ)
    if df is None or len(df) == 0:
        return None
    arr = df['close'].astype(float).values
    if USE_TODAY_PRICE:
        try:
            px = get_current_data()[sec].last_price
            if px is not None and not np.isnan(px) and px > 0:
                arr = np.append(arr, float(px))          # 把 14:55 价当 close[t]
        except Exception:
            pass                                          # 取不到当前价 → 退回"仅历史"口径
    return arr


# ════════════════════════════════════════════════════════════════════
def rebalance(context):
    # ── 1. 取数 (任一标的历史不足 → 本次不交易, 保持现金) ──
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

    # ── 3. 科创50 金叉状态 (决定闲置资金去哪) ──
    tr_k, sg_k = _trix(kc, REGIME_N, REGIME_M)
    if np.isnan(tr_k.iloc[-1]) or np.isnan(sg_k.iloc[-1]):
        return
    kc_golden = (tr_k.iloc[-1] > sg_k.iloc[-1])

    # ── 4. 定目标持仓 ──
    if core_on:
        target = g.core                                   # 核心开仓
        why = 'N12簇 %d/6 看多' % votes
    elif kc_golden:
        target = BOND                                     # 科创50金叉但簇未喊多 → 保守国债
        why = '科创50金叉·簇未喊多(%d/6) → 保守国债' % votes
    else:
        # 科创50死叉 → 防御组中"仍金叉 且 20日动量最强"的一只
        best, best_mom = None, -1e18
        for c in g.pool:
            tr_c, sg_c = _trix(data[c], REGIME_N, REGIME_M)
            if np.isnan(tr_c.iloc[-1]) or np.isnan(sg_c.iloc[-1]):
                continue
            if tr_c.iloc[-1] <= sg_c.iloc[-1]:
                continue                                   # 死叉, 跳过
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
        log.info('%s | %s' % (context.current_dt.date(), why))

    for sec in list(context.portfolio.positions.keys()):
        if str(sec) != str(target) and context.portfolio.positions[sec].total_amount > 0:
            order_target_value(sec, 0)
    order_target_value(target, context.portfolio.total_value)

    g.last_target = target
    record(core_pos=1 if target == g.core else 0)   # 画图用: 1=持科创50, 0=持防御资产

"""
B 策略 · 聚宽（JoinQuant）回测 — 贴近真实交易版
==================================================
交易标的: T0 ETF 宽基池（手工103 + auto层59质量筛选，共165只）

真实交易流程（t0_monitor.py 实际执行的每一步）:
  1. 每日 14:40 扫全市场 T0 ETF，确认 Top1 今日涨幅 ≥ 3%
  2. 14:45 二次确认（防尾盘脉冲：14:40→14:45 涨幅不能跌破 3%）
  3. 14:50 以市价全仓买入
  4. 次日 09:40~11:05 每 50 秒检查 TRIX(5,3) 死叉（基于 5 分钟 K 线计算）
  5. TRIX 死叉触发 → 立即市价卖出
  6. 11:05 仍未触发 → 强制市价卖出（fallback）

JQ 回测如何模拟真实成交价:
  - 买入：order_value(code, cash) @ 14:50 → JQ 以 14:50 分钟 bar 价格成交
  - 卖出：order_target(code, 0) 在死叉检测时 → JQ 以当前分钟 bar 价格成交
  - TRIX 信号：基于 5 分钟 K 线计算，与真实操作一致
  - 检测频率：每分钟（真实是每 50 秒，差异可忽略）
  - 手续费：万3 双边（对齐真实）
  - 不额外加滑点（ETF 流动性好，市价成交滑点极低）

⚠ 与真实操作的主要差异（回测无法完全消除）:
  - JQ 以分钟 bar close 成交，真实以实时报价成交（最多差 ~1 分钟的波动）
  - JQ 数据源（聚宽自身）与本机 pytdx 数据源不完全一致
  - 回测无流动性冲击（小资金 2w 实际也无冲击，此项差异可忽略）

用法:
  1. 聚宽 → 我的策略 → 新建策略
  2. 复制本文件全部内容到代码编辑器
  3. 回测设置:
     - 频率：分钟（必须，因需日内 14:40/14:50/09:40~11:05 精确时点）
     - 起始日期：2022-06-15
     - 结束日期：2026-08-03
     - 初始资金：20000（对齐实盘 2w）
  4. 点击"运行回测"
"""

import numpy as np
import pandas as pd


# ============================================================
# 参数（对齐真实交易 t0_monitor.py 常量）
# ============================================================

MIN_GAIN = 3.0           # 最低今日涨幅 %（选股门槛，<3% 不买）
TRIX_PERIOD = 5          # TRIX 三重 EMA 周期
TRIX_SIGNAL_PERIOD = 3   # TRIX signal 线平滑周期
SELL_START = "09:40"     # 卖出监控开始（早于此不检查死叉）
SELL_CUTOFF = "11:05"    # 卖出截止（11:05 强制市价卖出 fallback）
CONFIRM_TIME = "14:40"   # 双时点确认（防尾盘脉冲）
BUY_TIME = "14:50"       # 买入执行时刻

# ============================================================
# T0 ETF 候选池（手工 103 只 + auto 层 59 只质量宽基 = 165 只）
# 聚宽格式：上交所 .XSHG，深交所 .XSHE
# ⚠ 部分退市 ETF（513680/513960/159833）在池中但聚宽可能无数据，会自动跳过
# ============================================================

SH_ETFS = [
    "510900.XSHG", "513180.XSHG", "513130.XSHG", "513010.XSHG",
    "513050.XSHG", "513330.XSHG", "513060.XSHG", "513120.XSHG",
    "513190.XSHG", "513200.XSHG", "513260.XSHG", "513280.XSHG",
    "513600.XSHG", "513630.XSHG", "513660.XSHG", "513680.XSHG",
    "513700.XSHG", "513730.XSHG", "513750.XSHG", "513770.XSHG",
    "513800.XSHG", "513880.XSHG", "513900.XSHG", "513950.XSHG",
    "513960.XSHG", "513970.XSHG", "513980.XSHG", "513990.XSHG",
    "513590.XSHG", "513110.XSHG", "513580.XSHG", "513620.XSHG",
    "513690.XSHG", "513720.XSHG", "513100.XSHG", "513500.XSHG",
    "513400.XSHG", "513300.XSHG", "513550.XSHG", "513650.XSHG",
    "513850.XSHG", "513860.XSHG", "513530.XSHG", "513520.XSHG",
    "513030.XSHG", "513080.XSHG",
    "518880.XSHG", "518600.XSHG", "518850.XSHG", "518660.XSHG",
    "518800.XSHG", "517520.XSHG", "562990.XSHG",
    "501018.XSHG", "501312.XSHG",
    "511380.XSHG", "511180.XSHG",
    "511010.XSHG", "511020.XSHG", "511030.XSHG", "511060.XSHG",
    "511070.XSHG", "511090.XSHG", "511100.XSHG", "511110.XSHG",
    "511120.XSHG", "511130.XSHG", "511150.XSHG", "511160.XSHG",
    "511190.XSHG", "511200.XSHG",
    # --- auto 层（25 只）---
    "511220.XSHG", "511260.XSHG", "511270.XSHG", "511360.XSHG",
    "511520.XSHG", "511580.XSHG", "511660.XSHG", "511850.XSHG",
    "511880.XSHG", "511990.XSHG",
    "513000.XSHG", "513020.XSHG", "513040.XSHG", "513150.XSHG",
    "513160.XSHG", "513220.XSHG", "513380.XSHG", "513390.XSHG",
    "513560.XSHG", "513820.XSHG", "513870.XSHG", "513890.XSHG",
    "513910.XSHG", "513920.XSHG",
    "518680.XSHG", "518860.XSHG", "518890.XSHG",
]

SZ_ETFS = [
    "159920.XSHE", "159691.XSHE", "159740.XSHE", "159745.XSHE",
    "159792.XSHE", "159808.XSHE", "159824.XSHE", "159887.XSHE",
    "159687.XSHE", "159632.XSHE", "159991.XSHE", "159941.XSHE",
    "159509.XSHE", "159712.XSHE", "159518.XSHE", "159696.XSHE",
    "159697.XSHE", "161125.XSHE", "159685.XSHE", "159510.XSHE",
    "159541.XSHE", "159658.XSHE", "159612.XSHE", "159615.XSHE",
    "159620.XSHE", "159625.XSHE", "159628.XSHE", "159636.XSHE",
    "159643.XSHE", "159655.XSHE",
    # --- auto 层（37 只）---
    "159723.XSHE", "159833.XSHE", "159840.XSHE", "159856.XSHE",
    "159863.XSHE", "159876.XSHE", "159888.XSHE", "159892.XSHE",
    "159895.XSHE", "159899.XSHE", "159901.XSHE", "159812.XSHE",
    "159934.XSHE", "159562.XSHE", "159985.XSHE", "162411.XSHE",
    "159981.XSHE", "162719.XSHE", "161129.XSHE", "159649.XSHE",
    "159329.XSHE", "159501.XSHE", "159513.XSHE", "159561.XSHE",
    "159569.XSHE", "159605.XSHE", "159607.XSHE", "159659.XSHE",
    "159660.XSHE", "159688.XSHE", "159747.XSHE", "159830.XSHE",
    "159866.XSHE", "161116.XSHE", "161128.XSHE", "161226.XSHE",
    "161815.XSHE",
]

ALL_ETFS = SH_ETFS + SZ_ETFS


# ============================================================
# 工具函数（TRIX 计算，与 t0_monitor.py 完全一致）
# ============================================================

def ema(series, period):
    """EMA（指数移动平均），与 t0_monitor._ema 公式一致"""
    if len(series) < period:
        return [np.nan] * len(series)
    alpha = 2.0 / (period + 1)
    result = [np.nan] * len(series)
    result[period - 1] = np.mean(series[:period])
    for i in range(period, len(series)):
        result[i] = alpha * series[i] + (1 - alpha) * result[i - 1]
    return result


def calc_trix(closes, period=TRIX_PERIOD):
    """TRIX = 三重 EMA 的日变化率"""
    if len(closes) < period * 3 + 1:
        return [0.0] * len(closes)
    e1 = ema(closes, period)
    e2 = ema(e1, period)
    e3 = ema(e2, period)
    trix = [0.0] * len(e3)
    for i in range(1, len(e3)):
        prev = e3[i - 1]
        if prev and not np.isnan(prev) and prev != 0:
            trix[i] = (e3[i] - prev) / prev * 100.0
    return trix


def trix_death_cross(closes, signal_period=TRIX_SIGNAL_PERIOD):
    """
    判断最新一根 bar 是否发生 TRIX 死叉（TRIX 下穿 signal 线）
    返回: (trix_vals, signal_vals, is_death_cross: bool)
    """
    trix_raw = calc_trix(closes)
    sig_raw = ema(trix_raw, signal_period)
    if len(trix_raw) < 3:
        return trix_raw, sig_raw, False
    t_prev, t_curr = trix_raw[-2], trix_raw[-1]
    s_prev, s_curr = sig_raw[-2], sig_raw[-1]
    cross = (t_prev > s_prev) and (t_curr < s_curr)
    return trix_raw, sig_raw, cross


def calc_today_gain(code, current_dt):
    """
    计算某 ETF 的今日涨幅 %。
    用 current_dt（datetime）取 forming daily bar 的实时 close，
    对比昨日收盘价。

    关键：end_date 必须用 datetime 而非日期字符串。
    日期字符串默认 00:00:00 截断 → 取不到今天 forming bar。
    """
    try:
        df = get_price(code, end_date=current_dt, count=2,
                       frequency="daily", fields=["close"], skip_paused=True)
        if len(df) < 2:
            return None
        prev, today = df["close"].iloc[0], df["close"].iloc[1]
        if prev <= 0 or today <= 0:
            return None
        return (today - prev) / prev * 100.0
    except Exception:
        return None


# ============================================================
# 聚宽回测框架
# ============================================================

def initialize(context):
    """初始化：手续费、候选池、定时任务"""

    # -- 基准 --
    set_benchmark("513050.XSHG")

    # -- 手续费：万3 双边（对齐真实交易）--
    set_order_cost(OrderCost(
        open_commission=0.0003,
        close_commission=0.0003,
        min_commission=1,
    ), type="fund")

    # -- 全局状态 --
    g.etfs = ALL_ETFS               # 候选池
    g.pending_buy = None            # 待买入代码（14:40 确认后暂存）
    g.buy_price = 0.0               # 买入成交价
    g.buy_date = None               # 买入日期
    g.hold_code = None              # 当前持仓代码
    g.sold_today = False            # 今日是否已卖
    g.trade_log = []                # 交易记录

    # -- 定时任务（按真实交易的时间点）--
    run_daily(confirm_at_1440, "14:40")    # ① 14:40 初筛 Top1 ≥3%
    run_daily(confirm_at_1445, "14:45")    # ② 14:45 二次确认（防尾盘脉冲）
    run_daily(buy_at_1450, "14:50")        # ③ 14:50 市价买入
    run_daily(sell_monitor, "every_bar")   # ④ 每分钟检查 TRIX 死叉

    log.info(f"B策略(真实版)初始化 | 候选池{len(g.etfs)}只 | 门槛≥{MIN_GAIN}%")
    log.info(f"TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) | 卖出窗口{SELL_START}~{SELL_CUTOFF}")


# ============ ① 14:40 初筛 ============

def confirm_at_1440(context):
    """
    扫描全部候选 ETF，找当日涨幅最高的 Top1，要求 ≥ MIN_GAIN(3%)。
    对标真实：14:40 crontab 触发 run_signal → rank_t0_by_today_gain
    """
    g.pending_buy = None
    best_code = None
    best_gain = -999.0

    for code in g.etfs:
        gain = calc_today_gain(code, context.current_dt)
        if gain is None:
            continue
        if gain > best_gain:
            best_gain = gain
            best_code = code

    if best_code is None or best_gain < MIN_GAIN:
        log.info(f"[14:40] 无达标标的 | Top1={best_code} +{best_gain:.2f}% < {MIN_GAIN}%")
        return

    g.pending_buy = best_code
    log.info(f"[14:40] 初筛通过 | {best_code} +{best_gain:.2f}% ≥ {MIN_GAIN}%")


# ============ ② 14:45 二次确认 ============

def confirm_at_1445(context):
    """
    二次确认：14:40→14:45 这5分钟内，Top1 涨幅不能跌破 3%。
    对标真实：confirm_signal_gain() 用 14:40 的 1 分 K close 回溯验证。
    防尾盘脉冲——有些 ETF 在 14:45 瞬间拉涨到 3%+ 然后回落。
    """
    if g.pending_buy is None:
        return

    code = g.pending_buy
    gain = calc_today_gain(code, context.current_dt)
    if gain is None or gain < MIN_GAIN:
        log.info(f"[14:45] 二次确认失败 | {code} +{gain:.2f}% < {MIN_GAIN}%, 取消买入")
        g.pending_buy = None
        return

    log.info(f"[14:45] 二次确认通过 | {code} +{gain:.2f}% ≥ {MIN_GAIN}%")


# ============ ③ 14:50 市价买入 ============

def buy_at_1450(context):
    """
    14:50 全仓买入已确认的 ETF。
    对标真实：14:50 触发 → 以腾讯实时行情价下单（≈市价）。
    JQ 模拟：order_value(code, cash) → JQ 以 14:50 分钟 bar 的成交价撮合。
    """
    if g.pending_buy is None:
        return

    code = g.pending_buy
    pos = context.portfolio.positions.get(code)
    if pos is not None and pos.total_amount > 0:
        log.info(f"[14:50] 已有 {code} 持仓，跳过")
        g.pending_buy = None
        return

    cash = context.portfolio.available_cash
    if cash < 100:
        log.warning(f"[14:50] 现金不足 {cash:.2f}，跳过")
        g.pending_buy = None
        return

    # 市价全仓买入
    order_value(code, cash)
    g.buy_price = get_current_data()[code].last_price
    g.buy_date = context.current_dt.strftime("%Y-%m-%d")
    g.hold_code = code
    g.sold_today = False
    g.trade_log.append((g.buy_date, "BUY", code, g.buy_price,
                         f"涨幅确认≥{MIN_GAIN}%"))
    log.info(f"[14:50] ✅ 买入 {code} @ {g.buy_price:.4f} | 金额 {cash:.0f}")
    g.pending_buy = None


# ============ ④ 卖出监控（每分钟，09:40~11:05）============

def sell_monitor(context):
    """
    每分钟检查一次（对标真实每 50 秒一次）：
    1. 09:40 之前 → 不检查
    2. 09:40~11:05 → 检查 TRIX(5,3) 死叉（基于 5 分钟 K 线，与真实一致）
       触发 → 立即市价卖出
    3. 11:05 → 强制市价卖出（fallback）

    JQ 卖出价模拟：order_target(code, 0) → JQ 以当前分钟 bar 成交价撮合。
    与真实差距：真实用 resolve_exec_prices(1分K > 实时价 > 5分K close)，
    JQ 分钟 bar close 约等于真实 1 分 K close 级别，差 ≤1 分钟波动。
    """
    if g.buy_date is None or g.hold_code is None:
        return

    today = context.current_dt.strftime("%Y-%m-%d")
    if today == g.buy_date:
        return          # 买入当天不卖

    if g.sold_today:
        return          # 今天已经卖过了

    code = g.hold_code
    pos = context.portfolio.positions.get(code)
    if pos is None or pos.total_amount == 0:
        return

    now = context.current_dt.strftime("%H:%M")

    # --- 11:05 强制 fallback 卖出 ---
    if now >= SELL_CUTOFF:
        order_target(code, 0)
        sell_price = get_current_data()[code].last_price
        ret = (sell_price - g.buy_price) / g.buy_price * 100.0 if g.buy_price else 0
        g.trade_log.append((today, "SELL_FB", code, sell_price,
                            f"11:05强制卖出, ret={ret:.2f}%"))
        g.hold_code = None
        g.buy_date = None
        g.sold_today = True
        log.info(f"[{now}] ✅ 11:05强制卖出 {code} @ {sell_price:.4f} | 收益 {ret:.2f}%")
        return

    # --- 09:40 之前不检查 ---
    if now < SELL_START:
        return

    # --- TRIX(5,3) 死叉检测（基于 5 分钟 K 线，与真实操作一致）---
    try:
        # 取最近 120 根 5 分钟 K 线
        df = get_price(code, end_date=context.current_dt, count=120,
                       frequency="5m", fields=["close"], skip_paused=True)
        if len(df) < TRIX_PERIOD * 3 + 5:
            return

        closes = df["close"].tolist()
        _, _, is_cross = trix_death_cross(closes)

        if is_cross:
            # 市价卖出（JQ 以当前分钟 bar 成交价撮合）
            order_target(code, 0)
            sell_price = get_current_data()[code].last_price
            ret = (sell_price - g.buy_price) / g.buy_price * 100.0 if g.buy_price else 0
            g.trade_log.append((today, "SELL_TRIX", code, sell_price,
                                f"TRIX死叉@{now}, ret={ret:.2f}%"))
            g.hold_code = None
            g.buy_date = None
            g.sold_today = True
            log.info(f"[{now}] ✅ TRIX死叉卖出 {code} @ {sell_price:.4f} | 收益 {ret:.2f}%")

    except Exception as e:
        log.error(f"TRIX 计算异常 {code}: {e}")


# ============================================================
# 使用说明
# ============================================================
# 1. 将本文件全部内容复制到聚宽策略编辑器
# 2. 回测设置:
#    - 频率: 分钟
#    - 起始日期: 建议 2022-06-15
#    - 结束日期: 2026-08-03
#    - 初始资金: 20000
# 3. 点击"运行回测"
# 4. 如果太慢(165只×分钟级)，可临时改为 ALL_ETFS[:50] 快速验证
# ============================================================

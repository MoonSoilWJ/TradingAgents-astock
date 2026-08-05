"""
A 策略 · 聚宽（JoinQuant）回测 — 贴近真实交易版
==================================================
A 策略 = build_picks_hybrid（实盘 t0_monitor.py 现状）

核心逻辑（与 B 策略的区别）:
  1. 每日 14:40 用 501018 日K判定市场 regime（趋势/震荡/中性）
  2. regime = 趋势/震荡 → 用【滚动优质池】（过去30天累计涨幅 Top25）
  3. regime = 中性     → 用【auto 质量池】（59只宽基，剔除主题ETF）
  4. 在选定池子里找 Top1 今日涨幅 ≥ 3%
  5. 14:45 二次确认 + 14:50 买入 + 次日 TRIX(5,3) 死叉卖出

Regime 判定算法（复刻 t0_regime.py detect_regime）:
  - 数据: 501018 日K，取当日+前29天共30根
  - 震荡: 近10日 MA20 穿越次数 ≥ 2  （优先级最高）
  - 趋势: 距 MA20 > 8% 且 ADX(14) > 30
  - 中性: 以上都不满足

用法:
  1. 聚宽 → 新建策略 → 粘贴本文件
  2. 频率: 分钟 | 起始: 2022-06-15 | 结束: 2026-08-03 | 资金: 20000
  3. 运行回测
"""

import numpy as np
import pandas as pd


# ============================================================
# 参数（对齐 t0_monitor.py / quality_pool.py / t0_regime.py）
# ============================================================

MIN_GAIN = 3.0
TRIX_PERIOD = 5
TRIX_SIGNAL_PERIOD = 3
SELL_START = "09:40"
SELL_CUTOFF = "11:05"
STOP_LOSS_PCT = -0.05    # 单笔止损 -5%（截断趋势反转大亏）

# --- A 策略专属参数 ---
LOOKBACK = 30            # 滚动优质池训练窗（天）
POOL_SIZE = 25           # 优质池规模

# --- 大周期指标（决定动量池/反转池，覆盖年度行情）---
BROAD_LOOKBACK = 60      # 大周期回看窗口（天，约一季）
BROAD_THRESHOLD = 0.0    # 全池60天平均涨幅 >0 → 动量市(追涨池), <=0 → 弱市(反转池)
# 注: 不设空仓区——实测空仓区会吃掉95%交易日（仅13笔/4年）
REGIME_PROXY = "501018.XSHG"   # regime 代理标的
CHOPPY_MA_CROSS = 2      # MA20 穿越次数 ≥2 → 震荡
TREND_DIST_MIN = 8.0     # 距 MA20 >8%
TREND_ADX_MIN = 30.0     # ADX >30

# ============================================================
# T0 ETF 候选池（165 只，同 B 策略）
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

# Auto 层质量池（59只宽基，refresh_t0_pool.py 质量筛选产物）
# 中性 regime 用此池替代全池165只（剔除手工层主题ETF：创新药/半导体/油气/军工等）
AUTO_ETFS = [
    # --- SZ (17只) ---
    "159329.XSHE", "159501.XSHE", "159513.XSHE", "159561.XSHE",
    "159569.XSHE", "159605.XSHE", "159607.XSHE", "159659.XSHE",
    "159660.XSHE", "159688.XSHE", "159747.XSHE", "159830.XSHE",
    "159866.XSHE", "161116.XSHE", "161128.XSHE", "161226.XSHE",
    "161815.XSHE",
    # --- SH (42只) ---
    "501312.XSHG", "511010.XSHG", "511020.XSHG", "511030.XSHG",
    "511060.XSHG", "511070.XSHG", "511090.XSHG", "511100.XSHG",
    "511110.XSHG", "511120.XSHG", "511130.XSHG", "511150.XSHG",
    "511160.XSHG", "511190.XSHG", "511200.XSHG", "511220.XSHG",
    "511260.XSHG", "511270.XSHG", "511360.XSHG", "511520.XSHG",
    "511580.XSHG", "511660.XSHG", "511850.XSHG", "511880.XSHG",
    "511990.XSHG", "513000.XSHG", "513020.XSHG", "513040.XSHG",
    "513150.XSHG", "513160.XSHG", "513220.XSHG", "513380.XSHG",
    "513390.XSHG", "513560.XSHG", "513820.XSHG", "513870.XSHG",
    "513890.XSHG", "513910.XSHG", "513920.XSHG", "518680.XSHG",
    "518860.XSHG", "518890.XSHG",
]


# ============================================================
# 工具函数：EMA / TRIX / 死叉（同 B 策略）
# ============================================================

def ema(series, period):
    if len(series) < period:
        return [np.nan] * len(series)
    alpha = 2.0 / (period + 1)
    result = [np.nan] * len(series)
    result[period - 1] = np.mean(series[:period])
    for i in range(period, len(series)):
        result[i] = alpha * series[i] + (1 - alpha) * result[i - 1]
    return result


def calc_trix(closes, period=TRIX_PERIOD):
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
    trix_raw = calc_trix(closes)
    sig_raw = ema(trix_raw, signal_period)
    if len(trix_raw) < 3:
        return trix_raw, sig_raw, False
    t_prev, t_curr = trix_raw[-2], trix_raw[-1]
    s_prev, s_curr = sig_raw[-2], sig_raw[-1]
    cross = (t_prev > s_prev) and (t_curr < s_curr)
    return trix_raw, sig_raw, cross


def calc_today_gain(code, current_dt):
    """当日涨幅 %（forming daily bar vs 昨日收盘）"""
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
# A 策略专属：Regime 判定（复刻 t0_regime.py detect_regime）
# ============================================================

def _calc_adx(highs, lows, closes, period=14):
    """ADX(14) — Wilder 平滑法"""
    if len(closes) < period + 2:
        return 0.0
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        trs.append(tr)

    def wilder(vals):
        s = sum(vals[:period])
        out = [None] * period
        out.append(s)
        for i in range(period, len(vals) - 1):
            s = s - s / period + vals[i + 1]
            out.append(s)
        return out

    atr = wilder(trs)
    pdm = wilder(plus_dm)
    mdm = wilder(minus_dm)
    dxs = []
    for i in range(period, len(trs)):
        if atr[i] and atr[i] > 0:
            pdi = 100 * pdm[i] / atr[i]
            mdi = 100 * mdm[i] / atr[i]
            dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0)
    if len(dxs) < period:
        return 0.0
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def _ma_crosses(closes, ma_days=20, lookback=10):
    """近 lookback 日内 close 穿越 MA20 的次数"""
    if len(closes) < ma_days + lookback:
        return 0
    crosses = 0
    prev = None
    for i in range(len(closes) - lookback, len(closes)):
        ma = sum(closes[i - ma_days + 1:i + 1]) / ma_days
        above = closes[i] > ma
        if prev is not None and above != prev:
            crosses += 1
        prev = above
    return crosses


def detect_regime(current_dt):
    """
    用 501018 日K判定 regime。
    返回: "趋势" / "震荡" / "中性"

    优先级（从高到低）:
      1. 近10日 MA20 穿越次数 ≥ 2 → 震荡
      2. 距 MA20 > 8% 且 ADX(14) > 30 → 趋势
      3. 否则 → 中性
    """
    try:
        df = get_price(REGIME_PROXY, end_date=current_dt, count=35,
                       frequency="daily", fields=["close", "high", "low"],
                       skip_paused=True)
        if len(df) < 30:
            return "中性"
        closes = df["close"].tolist()[-30:]
        highs = df["high"].tolist()[-30:]
        lows = df["low"].tolist()[-30:]

        ma20 = sum(closes[-20:]) / 20
        close = closes[-1]
        dist = abs(close - ma20) / ma20 * 100 if ma20 else 0
        crosses = _ma_crosses(closes, 20, 10)
        adx = _calc_adx(highs, lows, closes, 14)

        if crosses >= CHOPPY_MA_CROSS:
            return "震荡"
        elif dist > TREND_DIST_MIN and adx > TREND_ADX_MIN:
            return "趋势"
        else:
            return "中性"
    except Exception:
        return "中性"


# ============================================================
# A 策略专属：滚动优质池（过去 LOOKBACK 天累计涨幅 Top POOL_SIZE）
# ============================================================

def build_dynamic_pool(current_dt, broad):
    """
    动态池: 根据大周期指标 broad 切换排序方向。
      broad=动量 → 过去 LOOKBACK 天累计涨幅 Top POOL_SIZE（追涨）
      broad=弱市 → 过去 LOOKBACK 天累计涨幅 Bottom POOL_SIZE（抄底反转）
    """
    scored = []
    for code in AUTO_ETFS:
        try:
            df = get_price(code, end_date=current_dt, count=LOOKBACK + 1,
                           frequency="daily", fields=["close"],
                           skip_paused=True)
            if len(df) < LOOKBACK:
                continue
            start_close = df["close"].iloc[0]
            end_close = df["close"].iloc[-1]
            if start_close <= 0:
                continue
            cum_ret = (end_close - start_close) / start_close * 100.0
            scored.append((cum_ret, code))
        except Exception:
            continue

    if broad == "弱市":
        scored.sort()                 # 反转池：跌最多Top25（抄底）
    else:
        scored.sort(reverse=True)     # 动量池：涨最多Top25（追涨）
    return [code for _, code in scored[:POOL_SIZE]]


def detect_broad_regime(current_dt):
    """
    大周期指标: 用 auto 59只过去 BROAD_LOOKBACK 天平均涨幅判断市场大周期。
      平均涨幅 > 0  → 动量市（追涨池：涨最多Top25）
      平均涨幅 <= 0 → 弱市（反转池：跌最多Top25 抄底）
    覆盖年度级别行情，弥补 501018 微观 regime 只看日级噪音的不足。
    始终有池子可交易（无空仓区），保证交易频次。
    """
    rets = []
    for code in AUTO_ETFS:
        try:
            df = get_price(code, end_date=current_dt, count=BROAD_LOOKBACK + 1,
                           frequency="daily", fields=["close"], skip_paused=True)
            if len(df) < BROAD_LOOKBACK:
                continue
            c0 = df["close"].iloc[0]
            c1 = df["close"].iloc[-1]
            if c0 > 0:
                rets.append((c1 - c0) / c0)
        except Exception:
            continue
    if len(rets) < 10:
        return "动量"   # 数据不足时默认动量池（等价原优质池逻辑）
    avg = sum(rets) / len(rets)
    return "动量" if avg > BROAD_THRESHOLD else "弱市"


# ============================================================
# 聚宽回测框架
# ============================================================

def initialize(context):
    set_benchmark("513050.XSHG")

    set_order_cost(OrderCost(
        open_commission=0.0003,
        close_commission=0.0003,
        min_commission=1,
    ), type="fund")

    g.etfs = ALL_ETFS
    g.pending_buy = None
    g.buy_price = 0.0
    g.buy_date = None
    g.hold_code = None
    g.sold_today = False
    g.trade_log = []
    g.current_regime = "中性"
    g.current_pool = "orig"
    g.broad_regime = "中性"
    g.broad_month = ""

    run_daily(confirm_at_1440, "14:40")    # ① regime判定 + 选股
    run_daily(confirm_at_1445, "14:45")    # ② 二次确认
    run_daily(buy_at_1450, "14:50")        # ③ 买入
    run_daily(sell_monitor, "every_bar")   # ④ TRIX 死叉卖出

    log.info(f"A策略(真实版)初始化 | 池子{len(g.etfs)}只 | 优质池Top{POOL_SIZE}/lookback{LOOKBACK}天")


# ============ ① 14:40 Regime 判定 + 选股 ============

def confirm_at_1440(context):
    """
    A 策略核心步骤:
    1. 用 501018 判定 regime
    2. regime = 趋势/震荡 → 滚动优质池(Top25), 中性 → auto池(59只)
       regime = 中性      → auto 质量池(59只)
    3. 在选定池子里找 Top1 今日涨幅 ≥ 3%
    """
    g.pending_buy = None

    # --- Regime 判定（主导选池）---
    regime = detect_regime(context.current_dt)
    g.current_regime = regime

    # --- 根据 regime 选池子 ---
    # 趋势/震荡: auto 59只选过去30天涨幅 Top25 → 当日Top1（动量池）
    # 中性:     auto 59只直接选当日Top1
    # 注: 实测反转池(弱市抄底)/震荡空仓/涨幅4%门槛均为负优化, 弱市无正期望, 不再尝试
    if regime in ("趋势", "震荡"):
        pool = build_dynamic_pool(context.current_dt, "动量")
        g.current_pool = f"动量池({len(pool)})"
    else:  # 中性
        pool = AUTO_ETFS
        g.current_pool = f"auto({len(pool)})"

    # --- 在池子里找 Top1 ---
    best_code = None
    best_gain = -999.0
    for code in pool:
        gain = calc_today_gain(code, context.current_dt)
        if gain is None:
            continue
        if gain > best_gain:
            best_gain = gain
            best_code = code

    if best_code is None or best_gain < MIN_GAIN:
        log.info(f"[14:40] regime={regime} pool={g.current_pool} | "
                 f"无达标 Top1={best_code} +{best_gain:.2f}% < {MIN_GAIN}%")
        return

    g.pending_buy = best_code
    log.info(f"[14:40] regime={regime} pool={g.current_pool} | "
             f"初筛 {best_code} +{best_gain:.2f}% ≥ {MIN_GAIN}%")


# ============ ② 14:45 二次确认 ============

def confirm_at_1445(context):
    if g.pending_buy is None:
        return
    code = g.pending_buy
    gain = calc_today_gain(code, context.current_dt)
    if gain is None or gain < MIN_GAIN:
        log.info(f"[14:45] 二次确认失败 | {code} +{gain:.2f}% < {MIN_GAIN}%, 取消")
        g.pending_buy = None
        return
    log.info(f"[14:45] 二次确认通过 | {code} +{gain:.2f}%")


# ============ ③ 14:50 买入 ============

def buy_at_1450(context):
    if g.pending_buy is None:
        return
    code = g.pending_buy
    pos = context.portfolio.positions.get(code)
    if pos is not None and pos.total_amount > 0:
        g.pending_buy = None
        return

    cash = context.portfolio.available_cash
    if cash < 100:
        g.pending_buy = None
        return

    order_value(code, cash)
    g.buy_price = get_current_data()[code].last_price
    g.buy_date = context.current_dt.strftime("%Y-%m-%d")
    g.hold_code = code
    g.sold_today = False
    g.trade_log.append((g.buy_date, "BUY", code, g.buy_price,
                         f"regime={g.current_regime} pool={g.current_pool}"))
    log.info(f"[14:50] ✅ 买入 {code} @ {g.buy_price:.4f} | "
             f"regime={g.current_regime} pool={g.current_pool} | 金额 {cash:.0f}")
    g.pending_buy = None


# ============ ④ 卖出监控（每分钟，09:40~11:05）============

def sell_monitor(context):
    """每分钟检查 TRIX(5,3) 死叉（基于5min K线），触发即卖；11:05 强制卖"""
    if g.buy_date is None or g.hold_code is None:
        return

    today = context.current_dt.strftime("%Y-%m-%d")
    if today == g.buy_date:
        return
    if g.sold_today:
        return

    code = g.hold_code
    pos = context.portfolio.positions.get(code)
    if pos is None or pos.total_amount == 0:
        return

    now = context.current_dt.strftime("%H:%M")

    # --- 11:05 强制 fallback ---
    if now >= SELL_CUTOFF:
        order_target(code, 0)
        sell_price = get_current_data()[code].last_price
        ret = (sell_price - g.buy_price) / g.buy_price * 100.0 if g.buy_price else 0
        g.trade_log.append((today, "SELL_FB", code, sell_price,
                            f"11:05强制, ret={ret:.2f}%"))
        g.hold_code = None
        g.buy_date = None
        g.sold_today = True
        log.info(f"[{now}] ✅ 11:05强制卖出 {code} @ {sell_price:.4f} | 收益 {ret:.2f}%")
        return

    if now < SELL_START:
        return

    # --- 单笔止损 -5%（截断趋势反转大亏，直击 -35.7% 回撤）---
    current_price = get_current_data()[code].last_price
    ret_now = (current_price - g.buy_price) / g.buy_price if g.buy_price else 0
    if ret_now <= STOP_LOSS_PCT:
        order_target(code, 0)
        ret = ret_now * 100.0
        g.trade_log.append((today, "SELL_STOP", code, current_price,
                            f"止损@{now}, ret={ret:.2f}%"))
        g.hold_code = None
        g.buy_date = None
        g.sold_today = True
        log.info(f"[{now}] ✅ 止损卖出 {code} @ {current_price:.4f} | 收益 {ret:.2f}%")
        return

    # --- TRIX(5,3) 死叉检测（5min K线，与真实操作一致）---
    try:
        df = get_price(code, end_date=context.current_dt, count=120,
                       frequency="5m", fields=["close"], skip_paused=True)
        if len(df) < TRIX_PERIOD * 3 + 5:
            return

        closes = df["close"].tolist()
        _, _, is_cross = trix_death_cross(closes)

        if is_cross:
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
        log.error(f"TRIX计算异常 {code}: {e}")


# ============================================================
# 使用说明
# ============================================================
# 1. 复制本文件全部内容到聚宽策略编辑器
# 2. 回测设置: 频率=分钟, 起始>=2022-06-15, 资金=20000
# 3. 运行回测
# 4. 若太慢(165只×分钟级), 可临时改 ALL_ETFS[:50] 快速验证
#
# A vs B 的关键区别:
#   B = 全池165只直接选Top1（不论市场环境）
#   A = 先判regime → 趋势/震荡用优质池Top25, 中性用auto池59只 → 再选Top1
# 中性用auto质量池剔除主题ETF拖累。
# ============================================================

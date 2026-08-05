"""
大一统策略 · 聚宽（JoinQuant）回测   【v3.0 满仓进攻模式 · 无防守腿】
================================================================================
【核心设计：满仓进攻(A 方案) + 无防守对冲，零 regime 切换】
★ 2026-08-05 改: 用户要求"不要split分仓,满仓干" → 彻底删除防守腿/Overlay。
  进攻有信号即 100% 可用资金买入; 无信号日空仓(持现金)。弱市仅持平(2023 ≈ -0.2%),
  靠 A 进攻腿自身在弱市接近盈亏平衡, 而非防守腿补。

解决的问题:
  进攻腿(A 方案)全周期 +201%，但收益全部集中在 2024-2025 动量年；
  2022-2023 弱市段 +6.71%（不亏，回撤-12%）—— 83% 时间资金空转。
  用户诉求: 需要一个不依赖"猜市场状态"的全周期策略，弱市段也不要负收益。

为什么不做 regime 切换（关键纠偏 2026-08-04）:
  任何"现在是弱市还是动量市"的判定都是回头看的，必然滞后。
  二元切换一旦判错，就会在主升浪里空仓 —— 不可接受的风险。
  本地回测已证明: 对 B 进攻腿做 regime 满仓切换，比"永远满仓 B"少 143pp（负优化）。
  ⇒ 本 v2 修正版【彻底删除 regime 切换】，改用 Overlay 叠加。

本策略的解法 —— C方案「剩余资金模式」:
  1) 进攻腿在弱市【不亏】(+6.71%, 回撤仅12%) → 它可以永远开着
  2) 进攻腿【约62%时间空仓】→ 闲置资金去防守腿
  3) 防守腿(弱市抗跌资产)弱市涨 → 它也可以永远开着
  ⇒ 两条腿常开，零判定、零预测、零滞后。
     市场状态的切换由「资金占用」天然完成:
       弱市   → T0信号稀少 → 62%时间资金停在防守组合 → 吃债牛金牛
       动量市 → T0信号密集 → 资金大部分时间在做隔夜 → 吃动量

  ★ 相对你实跑验证过的 C方案，本修正版只改一处:
    - 防守池【剔除 511380 可转债】。本地日线验证: 511380 在 2022-2023 弱市
      = -7.56%（与沪深300相关的权益类资产，弱市也跌），它把等权防守组合从
      纯黄金+国债的 +21%/+5% 拉到 ≈0%，违背"弱市抗跌"初衷。
      替换后防守池 = 518880黄金 + 511090 30Y国债 + 511260 10Y国债 + 510880红利
      （四只均为弱市正收益/低相关的真正 All-Weather 兜底资产）。
    - 其余逻辑（Overlay 叠加、月首再平衡、只买不卖省手续费、T+1 安全）完全不变。

--------------------------------------------------
【交易时序】(T+1 安全: 每笔持仓都至少隔夜)

  月首 14:35  更新防守组合目标(永久等权持有弱市抗跌资产，无动量/MA过滤)
  每日 14:40  进攻腿选股: regime判定 → 池子 → Top1今日涨幅≥3%
  每日 14:45  二次确认 + 腾资金
              · 进攻腿确认通过 → 清空所有防守持仓(腾全部资金给进攻腿)
              · 进攻腿无信号   → 不动防守持仓(保持)
  每日 14:50  有T0信号 → 满仓买T0 ETF
              无T0信号 → 按防守组合目标权重，把可用现金买入防守标的
                         (只买不卖省手续费; 月首做完整再平衡)
  次日 09:40~11:05  持T0 → 止损-5% / TRIX(5,3)死叉 / 11:05强制 卖出
              (卖出后当天不买回，等 14:50 统一决策，避免 T+1 冲突)

--------------------------------------------------
【C方案资金流】
  进攻腿有信号日(约38%): 资金→T0 ETF隔夜 → 次日卖出→现金→等14:50
  进攻腿无信号日(约62%): 资金→防守组合(等权多资产) → 月度调仓
  ⇒ 防守腿只吃进攻腿的闲置额度，按天动态调整，不撕碎月度动量逻辑

--------------------------------------------------
【用法】
  1. 聚宽 → 新建策略 → 粘贴本文件
  2. 频率: 【分钟】
  3. 起始: 2022-06-15 | 结束: 2026-08-03
  4. 资金: 【100000】← 重要！等权4只防守标的，2万本金买不起一手债券ETF
     等权4只 × 每只1/4仓位 = 每只约25000元（10万本金下）
     2万本金每只只有5000元，买不起一手债券ETF(~10000元) → 防守腿失效

  首行日志必须出现『UNIFIED v2 · C方案 Overlay』，否则说明没粘到最新代码。
"""

import numpy as np
import pandas as pd
from datetime import timedelta


# ============================================================
# 参数 · 进攻腿（对齐 joinquant_a_strategy.py 的 +201% 版本）
# ============================================================

MIN_GAIN = 3.0
TRIX_PERIOD = 5
TRIX_SIGNAL_PERIOD = 3
SELL_START = "09:40"
SELL_CUTOFF = "11:05"
STOP_LOSS_PCT = -0.05

# ---- 进攻腿自适应降险（资金曲线 stop，非 regime 前瞻）----
# 设计：维护进攻腿「已平仓交易」的滚动累计收益；若近 N 日累计 < 阈值，
#       暂停新开仓（资金全留防守腿），待回正后立刻重开。
# 为什么不是 regime 切换：滞后的是「进攻自己的盈亏」而非「市场状态」，
#       主升浪进攻正收益→永不误关；弱市/崩塌月进攻连亏→自动收手。零前视。
#
# ★ 2026-08-05 聚宽实测结论（A选股 · 2018-2026）：此 stop 在 A选股下【净负面】，
#   默认必须关。证据：
#     · 开(True): 收益287.52% / 年化18.73% / 夏普0.725 / MDD23.87%
#     · 关(False):收益597.75% / 年化27.91% / 夏普0.980 / MDD24.73%
#   → 开只把MDD改善0.86pp，却让收益腰斩、夏普从0.98跌到0.725。
#   根因（选股错配）：本地(B选股)弱市进攻持续连亏→stop翻正2023(-13.8%→+7.56%)；
#   但聚宽A选股进攻本就干净、不持续连亏，stop在强市(2024-2025)误刹→漏主升浪
#   (如2025-01 True+54.83% vs False+62.71%)，并把弱势月搞更差(2022-03
#   True-4.91% vs False+6.54%)。即"强市漏赚+弱市误刹"→净腰斩。
#   若改用B选股部署且确想弱市保护，再把 ATTACK_STOP_ENABLED 设 True
#   (LOOKBACK=28/PNL=0.0 为会触发的设置)；A选股下请保持 False。
ATTACK_STOP_ENABLED = False       # A选股默认关(净负面)；B选股想弱市保护再设True
ATTACK_STOP_LOOKBACK = 28         # 滚动窗口(日历日≈20交易日)；旧值60太松永不触发
ATTACK_STOP_PNL = 0.0             # 近窗口累计收益(%)低于此值 → 暂停新开仓；0=转负即停

# ---- 进攻腿趋势门禁（2026-08-05 新增，实测后默认关）----
# 入场再加一道硬性校验: 选出的 Top1 必须「当日收盘 > MA(GATE_MA_DAYS)」(处于上升趋势)。
# 机制与选股池无关(后置过滤器)，但【效果高度依赖选股池】，切勿混为一谈:
#   · B 选股(全市场Top1单池): 门禁 +62pp 有效 —— 全市场池噪音多, 门禁真滤掉亏钱假突破。
#   · A 选股(本策略, regime+滚动优质池): 本地无偏实测(2022-09~2026-07)
#       A 无门禁 +1151.85% / 384笔 → A+门禁 +920.22% / 331笔 = **-231.63pp 净负面**。
#     原因: 滚动优质池已事先筛掉弱标的, 留下的本就在趋势里; MA20 主要砍掉的是
#     本来盈利的有效信号(否决53天多为盈利交易), 而非噪音。
# ⇒ 对本策略(A选股)默认【关】。仅当未来改用 B 选股部署时，才考虑设 True。
# 被门禁否决的日子→退化为 idle→资金自动转防守腿(见 scan_at_1440)。
GATE_ENABLED = False
GATE_MA_DAYS = 20

LOOKBACK = 30            # 滚动优质池训练窗（天）
POOL_SIZE = 25           # 优质池规模

REGIME_PROXY = "501018.XSHG"
CHOPPY_MA_CROSS = 2
TREND_DIST_MIN = 8.0
TREND_ADX_MIN = 30.0


# ============================================================
# 参数 · 防守腿（弱市抗跌资产，无动量/MA过滤）
# ============================================================

DEF_LOOKBACK = 120
MA_WINDOWS = (20, 60)        # 历史遗留，防守腿已不再使用此过滤（保留无害）
DEFENSE_REBAL_DAY = 5        # 每月前几个交易日内更新一次防守目标
TARGET_EXPOSURE = 0.98       # 防守腿总仓位上限（留2%防手续费不足）
REBAL_TOL = 0.15             # 再平衡阈值: 偏离>15%才下单（省最低5元佣金）
MIN_ORDER_VALUE = 800        # 单笔最小下单金额

# ★ 2026-08-04 修正: 剔除 511380 可转债（弱市-7.56%，与权益相关，拖垮防守腿）
#   保留 4 只真正弱市抗跌/低相关的 All-Weather 资产:
#     518880 黄金   — 2022-2023 +21.23%（唯一全弱市段大幅正收益）
#     511090 30Y国债 — 利率下行受益（弱市/降息债券牛市，+4.80%局部）
#     511260 10Y国债 — 久期较短（+5.72%）
#     510880 红利    — A股弱市抗跌（2022 +2.3% 正收益）
DEFENSE_POOL = [
    "518880.XSHG",   # 黄金ETF
    "511090.XSHG",   # 30年国债ETF
    "511260.XSHG",   # 十年国债ETF
    "510880.XSHG",   # 红利ETF
]

# ★ 满仓模式开关 (2026-08-05 用户要求"不要split分仓,满仓干")
#   DEFENSE_ENABLED=False → 彻底删除防守腿: 进攻有信号则 100% 可用资金买入,
#   无信号日空仓(持现金)。即"满仓进攻, 无防守对冲"。
#   进攻腿本身已用 order_value(target, 全部可用现金) → 有信号即满仓。
#   设 True 可恢复 Overlay 防守腿(弱市补收益/降回撤, 但强市收益被稀释)。
DEFENSE_ENABLED = False

# 兜底：短融ETF（不合格份额的现金替代）
# ⚠ 刻意不用货币ETF(511990)：其价格在聚宽恒为100，收益以份额发放，回测0收益
CASH_ETF = "511360.XSHG"


# ============================================================
# 进攻腿候选池（A 方案 AUTO_ETFS，与已验证 +201% 版本一致）
# ============================================================

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
# 工具函数：EMA / TRIX / 死叉
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
    """当日涨幅 %（当前实时价 vs 昨日收盘）。无未来函数（已诊断验证）。"""
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


def calc_momentum(code, current_dt, lookback):
    """过去 lookback 个交易日累计收益率（%）"""
    try:
        df = get_price(code, end_date=current_dt, count=lookback + 1,
                       frequency="daily", fields=["close"], skip_paused=True)
        if df is None:
            return None
        df = df.dropna()
        if len(df) < lookback:
            return None
        c0 = float(df["close"].iloc[0])
        c1 = float(df["close"].iloc[-1])
        if c0 != c0 or c1 != c1 or c0 <= 0 or c1 <= 0:
            return None
        mom = (c1 - c0) / c0 * 100.0
        return None if mom != mom else mom
    except Exception:
        return None


def close_above_ma(code, current_dt, ma_days=GATE_MA_DAYS):
    """当日收盘是否位于 MA(ma_days) 之上（处于上升趋势）。无未来函数。

    用 get_price 日线(含当日当前价作为最后一根 close)，取前 ma_days 根收盘的均值
    与当日收盘比较。与 calc_today_gain 同源机制(end_date 含当日)，口径一致。
    返回 True/False/None(None=数据不足, 视为不通过由调用方决定)。
    """
    try:
        df = get_price(code, end_date=current_dt, count=ma_days + 1,
                       frequency="daily", fields=["close"], skip_paused=True)
        if df is None:
            return None
        df = df.dropna()
        if len(df) < ma_days + 1:
            return None
        closes = df["close"].tolist()
        ma = sum(closes[-(ma_days + 1):-1]) / ma_days   # 前 ma_days 根均值
        today = closes[-1]
        if ma <= 0 or today <= 0 or ma != ma:
            return None
        return today > ma
    except Exception:
        return None


# ============================================================
# 进攻腿：Regime 判定（复刻 t0_regime.py detect_regime，仅用于选股池切换）
# ============================================================

def _calc_adx(highs, lows, closes, period=14):
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
    """501018 日K判定 regime: 趋势 / 震荡 / 中性（仅用于选股池，不做资金切换）"""
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


def build_quality_pool(current_dt):
    """滚动优质池: auto 中过去 LOOKBACK 天累计涨幅 Top POOL_SIZE"""
    scored = []
    for code in AUTO_ETFS:
        mom = calc_momentum(code, current_dt, LOOKBACK)
        if mom is not None:
            scored.append((mom, code))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [code for _, code in scored[:POOL_SIZE]]


# ============================================================
# 聚宽框架
# ============================================================

def initialize(context):
    set_benchmark("510300.XSHG")
    set_order_cost(OrderCost(
        open_tax=0, close_tax=0,
        open_commission=0.0003,
        close_commission=0.0003,
        close_today_commission=0,
        min_commission=5,
    ), type="fund")

    log.set_level("order", "error")

    # --- 进攻腿状态 ---
    g.pending_buy = None
    g.buy_price = 0.0
    g.buy_date = None
    g.hold_code = None
    g.sold_today = False
    g.current_regime = "中性"
    g.current_pool = "auto"

    # --- 防守腿状态 ---
    g.defense_targets = {}          # {code: weight}  权重和=1
    g.last_defense_month = ""       # 每月只更新一次的去重标记
    g.is_defense_rebal_day = False  # 当月是否已做过完整再平衡

    # --- 统计 ---
    g.n_attack = 0
    g.n_attack_days = 0
    g.n_defense_days = 0
    g.n_idle_days = 0

    # --- 进攻腿自适应降险: 已平仓交易 (sell_date, return_pct) ---
    g.attack_closed = []

    # ★ 关键修复: run_daily + 函数内去重，日频/分钟频都100%触发
    if DEFENSE_ENABLED:
        run_daily(update_defense_target, "14:35")
    run_daily(scan_at_1440, "14:40")
    run_daily(prepare_at_1445, "14:45")
    run_daily(trade_at_1450, "14:50")
    run_daily(sell_monitor, "every_bar")

    log.info("=" * 62)
    log.info("UNIFIED v3.0 满仓进攻模式 (无防守腿)")
    if DEFENSE_ENABLED:
        log.info(f"进攻池 {len(AUTO_ETFS)}只 | 防守池 {len(DEFENSE_POOL)}只等权"
                 f"(无MA过滤, 已剔除511380可转债) | 兜底 {CASH_ETF}")
        log.info("零 regime 切换：进攻腿优先，剩余资金→防守组合，按天动态调整")
    else:
        log.info(f"进攻池 {len(AUTO_ETFS)}只 | ★满仓模式: 进攻有信号即100%买入, "
                 f"无信号日空仓 | 无防守对冲")
    log.info("零 regime 切换")
    log.info(f"★ 趋势门禁: {'开' if GATE_ENABLED else '关'} "
             f"(收盘>MA{GATE_MA_DAYS}; 仅B选股有效 +62pp, "
             f"A选股实测 -231pp 故默认关)")
    log.info(f"★ 进攻自适应stop: {'开' if ATTACK_STOP_ENABLED else '关'} "
             f"(A选股净负面, 默认关; 仅B选股弱市保护用)")
    log.info("★ 若首行没有『UNIFIED v2.1+趋势门禁』→ 没粘最新代码")
    log.info("=" * 62)


# ============ 月首 14:35 更新防守组合目标 ============

def update_defense_target(context):
    """
    防守腿: 永久等权持有「弱市抗跌资产」，★完全去掉动量/MA过滤★。
    只排除「当前回测日尚未上市/无行情」的标的，并在已上市标的上重新等权归一。
    """
    mkey = context.current_dt.strftime("%Y-%m")
    if mkey == g.last_defense_month:
        return
    g.last_defense_month = mkey
    g.is_defense_rebal_day = True

    eligible = []
    for code in DEFENSE_POOL:
        try:
            df = get_price(code, end_date=context.current_dt, count=2,
                           frequency="daily", fields=["close"], skip_paused=True)
            if df is None or len(df) < 1:
                continue
            last = float(df["close"].iloc[-1])
            if last != last or last <= 0:
                continue
            eligible.append(code)
        except Exception:
            continue

    if not eligible:
        g.defense_targets = {CASH_ETF: 1.0}
        log.info(f"[防守] 全部未上市/无行情 → 100%现金 {CASH_ETF}")
        return

    w = 1.0 / len(eligible)
    g.defense_targets = {c: w for c in eligible}
    held = " ".join(c.split(".")[0] for c in eligible)
    log.info(f"[防守] 弱市抗跌等权 {len(eligible)}只 [{held}] "
             f"(无动量/MA过滤, 永久持有)")


# ============ ① 14:40 进攻腿选股 ============

def scan_at_1440(context):
    g.pending_buy = None

    # --- 进攻腿自适应降险: 近 N 日已平仓进攻累计收益过低 → 暂停新开仓 ---
    if ATTACK_STOP_ENABLED:
        cutoff = (context.current_dt - timedelta(days=ATTACK_STOP_LOOKBACK))
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        recent = [p for (sd, p) in g.attack_closed if sd >= cutoff_str]
        if recent and sum(recent) < ATTACK_STOP_PNL:
            log.info(f"[14:40] 进攻近{ATTACK_STOP_LOOKBACK}日累计"
             f"{sum(recent):.1f}%<{ATTACK_STOP_PNL}% → 暂停, 资金留现金(idle)")
            return

    regime = detect_regime(context.current_dt)
    g.current_regime = regime

    if regime in ("趋势", "震荡"):
        pool = build_quality_pool(context.current_dt)
        g.current_pool = f"优质池({len(pool)})"
    else:
        pool = AUTO_ETFS
        g.current_pool = f"auto({len(pool)})"

    if context.current_dt.strftime("%Y-%m-%d") <= "2022-06-17":
        diag_code = "511260.XSHG"
        try:
            ddf = get_price(diag_code, end_date=context.current_dt, count=2,
                            frequency="daily", fields=["close"], skip_paused=True)
            gp_prev = float(ddf["close"].iloc[0]) if len(ddf) >= 1 else 0
            gp_today = float(ddf["close"].iloc[1]) if len(ddf) >= 2 else 0
            gp_gain = (gp_today - gp_prev) / gp_prev * 100 if gp_prev > 0 else 0
            ah = attribute_history(diag_code, 1, unit="1d", fields=["close"], skip_paused=True)
            ah_prev = float(ah["close"].iloc[-1]) if ah is not None and len(ah) >= 1 else 0
            cd_today = float(get_current_data()[diag_code].last_price)
            cd_gain = (cd_today - ah_prev) / ah_prev * 100 if ah_prev > 0 else 0
            log.info(f"[诊断] {diag_code} | get_price gain={gp_gain:.2f}% | "
                     f"attr_hist+curData gain={cd_gain:.2f}% | "
                     f"{'⚠一致=可能未来函数' if abs(gp_gain-cd_gain)<0.01 else '✓不同=实时价'}")
        except Exception as e:
            log.info(f"[诊断] {diag_code} 异常: {e}")

    best_code, best_gain = None, -999.0
    for code in pool:
        gain = calc_today_gain(code, context.current_dt)
        if gain is None:
            continue
        if gain > best_gain:
            best_gain, best_code = gain, code

    if best_code is None or best_gain < MIN_GAIN:
        log.info(f"[14:40] regime={regime} pool={g.current_pool} | "
                 f"无达标 Top1={best_code} gain={best_gain:.2f}% < {MIN_GAIN}%")
        return

    # --- 趋势门禁: 选出的 Top1 必须处于上升趋势(收盘>MA), 否则否决(退化为 idle→防守) ---
    if GATE_ENABLED:
        above = close_above_ma(best_code, context.current_dt, GATE_MA_DAYS)
        if above is not True:
            log.info(f"[14:40] 趋势门禁否决 {best_code} "
             f"(收盘≤MA{GATE_MA_DAYS}或数据不足, 不在上升趋势) "
             f"gain={best_gain:.2f}% → 资金留现金(idle)")
            return

    g.pending_buy = best_code
    log.info(f"[14:40] regime={regime} pool={g.current_pool} | "
             f"初筛 {best_code} +{best_gain:.2f}% (门禁通过)")


# ============ ② 14:45 二次确认 + 腾资金 ============

def prepare_at_1445(context):
    """
    1. 进攻腿二次确认（涨幅是否仍达标）
    2. 腾资金:
       · 进攻腿确认通过 → 清空所有非目标持仓（腾全部资金给进攻腿）
       · 进攻腿无信号   → 不动持仓（满仓模式无防守腿, 仅隔夜进攻持仓）
    此刻持仓只可能是隔夜进攻（T0持仓最迟11:05已清），T+1 安全。
    """
    if g.pending_buy is not None:
        gain = calc_today_gain(g.pending_buy, context.current_dt)
        if gain is None or gain < MIN_GAIN:
            log.info(f"[14:45] 二次确认失败 {g.pending_buy} → 取消，资金留现金(idle)")
            g.pending_buy = None
        else:
            log.info(f"[14:45] 二次确认通过 {g.pending_buy} +{gain:.2f}%")

    if g.pending_buy is not None:
        target = g.pending_buy
        for code in list(context.portfolio.positions.keys()):
            if code == target:
                continue
            if code == g.hold_code:
                continue
            pos = context.portfolio.positions[code]
            if pos.closeable_amount <= 0:
                continue
            order_target(code, 0)
            log.info(f"[14:45] 腾资金: 卖出防守 {code}")


# ============ ③ 14:50 买入（T0 或 防守组合）============

def trade_at_1450(context):
    is_attack = g.pending_buy is not None

    if is_attack:
        target = g.pending_buy
        cash = context.portfolio.available_cash
        if cash < 1000:
            g.pending_buy = None
            return

        order_value(target, cash)
        pos = context.portfolio.positions.get(target)
        if pos is None or pos.total_amount <= 0:
            log.info(f"[14:50] 进攻 {target} 资金不足 ({cash:.0f})")
            g.pending_buy = None
            return

        price = get_current_data()[target].last_price
        g.buy_price = price
        g.buy_date = context.current_dt.strftime("%Y-%m-%d")
        g.hold_code = target
        g.sold_today = False
        g.n_attack += 1
        log.info(f"[14:50] 🔺进攻 买入 {target} @ {price:.4f} | "
                 f"regime={g.current_regime} pool={g.current_pool} | {cash:.0f}元")
        g.pending_buy = None
        return

    # --- 防守腿: 按等权组合买入可用资金 (满仓模式 DEFENSE_ENABLED=False 时整段跳过) ---
    if not DEFENSE_ENABLED or not g.defense_targets:
        return

    total = context.portfolio.total_value

    if g.is_defense_rebal_day:
        g.is_defense_rebal_day = False
        _rebalance_defense_full(context, total)
        return

    _topup_defense(context, total)


def _rebalance_defense_full(context, total):
    """月首完整再平衡: 先清非目标，再按目标权重下单"""
    targets = g.defense_targets
    exposed = total * TARGET_EXPOSURE
    target_vals = {code: exposed * w for code, w in targets.items()}
    cur_vals = {}
    for code, pos in context.portfolio.positions.items():
        if code == g.hold_code:
            continue
        cur_vals[code] = pos.value

    for code in list(cur_vals.keys()):
        if code not in target_vals:
            pos = context.portfolio.positions[code]
            if pos.closeable_amount > 0:
                order_target(code, 0)
                log.info(f"[14:50] 防守再平衡 清仓 {code}")

    for code, tv in target_vals.items():
        cv = cur_vals.get(code, 0.0)
        if cv > tv and _need_rebal(cv, tv):
            pos = context.portfolio.positions.get(code)
            if pos and pos.closeable_amount > 0:
                order_target_value(code, tv)

    for code, tv in target_vals.items():
        cv = cur_vals.get(code, 0.0)
        if cv < tv and _need_rebal(cv, tv):
            try:
                price = get_current_data()[code].last_price
                if price > 0 and tv < price * 100:
                    log.info(f"[14:50] 防守再平衡 跳过 {code} "
                             f"目标{tv:.0f}不够1手(需{price*100:.0f})")
                    continue
            except Exception:
                pass
            order_target_value(code, tv)

    held = sorted(c.split(".")[0]
                  for c, p in context.portfolio.positions.items()
                  if p.total_amount > 0 and c != g.hold_code)
    log.info(f"[14:50] 🛡防守 月度再平衡 | 持仓: {' '.join(held)} | "
             f"总资产 {total:.0f}")


def _topup_defense(context, total):
    """非月首: 只把可用现金按权重买入，不卖已有持仓（省手续费）"""
    cash = context.portfolio.available_cash
    if cash < total * 0.10 or cash < MIN_ORDER_VALUE:
        return

    targets = g.defense_targets
    exposed = total * TARGET_EXPOSURE
    total_w = sum(w for w in targets.values())

    bought_any = False
    for code, w in targets.items():
        tv = exposed * w
        cv = context.portfolio.positions.get(code)
        cv = cv.value if cv else 0.0
        if cv >= tv:
            continue
        diff = tv - cv
        if diff < MIN_ORDER_VALUE:
            continue
        alloc = min(diff, cash * w / total_w) if total_w > 0 else diff
        try:
            price = get_current_data()[code].last_price
            if price > 0 and alloc < price * 100:
                continue
        except Exception:
            pass
        order_value(code, alloc)
        cash -= alloc
        bought_any = True
        log.info(f"[14:50] 🛡防守 补仓 {code} +{alloc:.0f}元")


def _need_rebal(cur, target):
    diff = abs(cur - target)
    if diff < MIN_ORDER_VALUE:
        return False
    base = max(target, cur, 1.0)
    return diff / base >= REBAL_TOL


# ============ ④ 卖出监控（每分钟，仅管进攻腿持仓）============

def sell_monitor(context):
    """止损-5% > TRIX(5,3)死叉 > 11:05强制。卖出后当天不买回，等14:50统一决策。"""
    if g.buy_date is None or g.hold_code is None:
        return

    today = context.current_dt.strftime("%Y-%m-%d")
    if today == g.buy_date:
        return
    if g.sold_today:
        return

    code = g.hold_code
    pos = context.portfolio.positions.get(code)
    if pos is None or pos.closeable_amount <= 0:
        return

    now = context.current_dt.strftime("%H:%M")
    now_date = context.current_dt.strftime("%Y-%m-%d")

    def _close(tag, price):
        order_target(code, 0)
        ret = (price - g.buy_price) / g.buy_price * 100.0 if g.buy_price else 0
        g.hold_code = None
        g.buy_date = None
        g.sold_today = True
        g.attack_closed.append((now_date, ret))   # 记录已平仓收益供自适应降险
        log.info(f"[{now}] ✅{tag} 卖出 {code} @ {price:.4f} | 收益 {ret:.2f}%")

    if now >= SELL_CUTOFF:
        _close("11:05强制", get_current_data()[code].last_price)
        return

    if now < SELL_START:
        return

    current_price = get_current_data()[code].last_price
    ret_now = (current_price - g.buy_price) / g.buy_price if g.buy_price else 0
    if ret_now <= STOP_LOSS_PCT:
        _close("止损", current_price)
        return

    try:
        df = get_price(code, end_date=context.current_dt, count=120,
                       frequency="5m", fields=["close"], skip_paused=True)
        if len(df) < TRIX_PERIOD * 3 + 5:
            return
        _, _, is_cross = trix_death_cross(df["close"].tolist())
        if is_cross:
            _close("TRIX死叉", get_current_data()[code].last_price)
    except Exception as e:
        log.error(f"TRIX计算异常 {code}: {e}")


# ============ 收盘统计 ============

def after_trading_end(context):
    if g.hold_code is not None:
        g.n_attack_days += 1
    else:
        has_defense = any(
            p.total_amount > 0 and c != g.hold_code
            for c, p in context.portfolio.positions.items()
        )
        if has_defense:
            g.n_defense_days += 1
        else:
            g.n_idle_days += 1

    dt = context.current_dt
    if dt.day <= 3:
        log.info(f"[月末统计] 累计进攻{g.n_attack}笔 | "
                 f"进攻天数{g.n_attack_days} 防守{g.n_defense_days} "
                 f"空仓{g.n_idle_days} | 总资产{context.portfolio.total_value:.0f}")
    if dt.month == 12 and dt.day >= 28:
        total_days = g.n_attack_days + g.n_defense_days + g.n_idle_days
        atk_pct = g.n_attack_days / total_days * 100 if total_days else 0
        def_pct = g.n_defense_days / total_days * 100 if total_days else 0
        log.info(f"===== {dt.year} 年末 总资产 {context.portfolio.total_value:.2f} | "
                 f"进攻{g.n_attack}笔 | "
                 f"进攻{atk_pct:.0f}% 防守{def_pct:.0f}% "
                 f"空仓{100-atk_pct-def_pct:.0f}% =====")

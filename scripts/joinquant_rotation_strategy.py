# -*- coding: utf-8 -*-
"""
板块轮动 v6 · 聚宽（JoinQuant）回测   【单文件 · 动态池 + 回测一体】
================================================================================
把「板块轮动监控（rotation_monitor / backtest_rotation_8way）」原样搬到聚宽，
做成可直接粘贴运行的回测文件。

【v6 选股公式 · 与本地 rotation_v6.compute_v6_score 逐字一致】
    得分 = 近3日涨幅之和(%) × 量能因子
    量能因子 = VOL_BASE + (1-VOL_BASE) × min(量比/VOL_THRESHOLD, 1)
    量比     = 当日成交量 / 前5日成交量均值           ← 注意是前5日(不含当日)

【交易时序（T+1 安全：每笔隔夜）】
    T日 SIGNAL_TIME(默认09:40)  对所有池内 ETF 算 v6，取 TOP1 → 记 baseline = 当时现价
                                  （持仓中则不重发信号；对齐本地 8way 空仓才发）
    T日 SIGNAL_TIME~15:00   买入观察窗（仅信号日当天，先到先触发）
                追涨: 现价 ≥ baseline × (1 + BUY_UP/100)   抄底默认禁用
                当日未触发则放弃，下一天重新发信号（不兜底、T+1 不买）
    买入后 T+1 起（严格 > 买入日，满足 T+1）  卖出观察窗（先到先触发）
                · TRIX(5,3) 5分钟死叉 → 卖
                · 追踪止盈: 涨幅 ≥ TRAIL_START% 后，自峰值回落 ≥ TRAIL_DROP% → 卖
                · 买入日后首日收盘 → 强制卖（兜底）

【动态池设计 · 解决"79个静态JSON会过期"】
    1) SEED_POOL：内联本地 pingan_sector_etf.json 的 79 个(板块,ETF)对，
       转换为聚宽代码(159697.XSHE / 512730.XSHG)。保证与本地回测口径可对照。
    2) 上市日门禁：每天用 get_all_securities(['fund'], 当日) 过滤掉尚未上市的 ETF，
       池子随 ETF 真实上市日自动"生长"，无前视偏差。
    3) DYNAMIC_EXPAND（默认开）：扫描全市场 fund，凡是名称命中 SECTOR_KEYWORDS
       且非货币/债券类的新发行业 ETF，自动加入池子 → 新主题 ETF（机器人/AI/低空…）
       无需改代码即被纳入。
    ⇒ 本地那套"定期重抓平安JSON"的维护负担，在聚宽侧被彻底绕开。

【用法】
    1. 聚宽 → 新建策略 → 粘贴本文件
    2. 频率: 【分钟】
    3. 起始/结束: 如 2026-04-02 ~ 2026-08-26（本地 8way 仅近~104天5min完整，对账请用同窗口）
    4. 资金: 任意（单一名 ETF，10万起即可）
    5. 滑点: 本文件 set_slippage(FixedSlippage(0))，对齐本地"无滑点"回测；
       若想看含聚宽默认滑点的保守数，把该行注释掉即可。

    首行日志必须出现『ROTATION v6 · JQ 单文件』，否则没粘到最新代码。
"""

import numpy as np
import pandas as pd
from datetime import timedelta, time as dtime

# ============================================================
# 参数（对齐 backtest_rotation_8way.py 默认口径）
# ============================================================
# ---- v6 打分 ----
SCORE_WINDOW   = 3      # 近 N 日涨幅之和
VOL_THRESHOLD  = 1.5    # 量比上限（封顶）
VOL_AVG_PERIOD = 5      # 量比分母：前 N 日量均值
VOL_BASE       = 0.3    # 量能因子下限

# ---- 买卖触发 ----
BUY_UP     = 1.0        # 追涨门槛(%)：现价 ≥ baseline×(1+BUY_UP/100) 才买
BUY_DOWN   = 99.0       # 抄底门槛(%)：99=禁用（本地 8way 实测抄底单亏损率高）
PREV_DAY_SURGE_LIMIT = 7.0  # 前一日涨幅>此值(%)则不买（防追高暴跌，对齐 8way）
SIGNAL_TIME = "09:40"       # 信号时刻（对齐本地 8way 最优固定时点 09:40；可调 "14:50" 等）
_SIG_H, _SIG_M = (int(x) for x in SIGNAL_TIME.split(":"))
SIGNAL_DT = dtime(_SIG_H, _SIG_M)
TRIX_PERIOD       = 5
TRIX_SIGNAL_PERIOD = 3
TRAIL_START = 3.0       # 涨幅触及此值后才启动追踪止盈
TRAIL_DROP  = 0.5       # 自峰值回落此值(%)则止盈卖
MIN_V6      = 0.0       # 信号最低 v6 分（0=TOP1 永远出信号）

# ---- 动态池 ----
DYNAMIC_EXPAND = True   # 自动纳入名称命中板块关键词的新发行业 ETF

# ============================================================
# SEED_POOL —— 内联本地 pingan_sector_etf.json（79 板块 → 场内ETF）
#   格式 (板块名, 聚宽代码)；多个板块命中同一 ETF 会在 build_pool 里去重。
# ============================================================
_SEED_RAW = [
    ("油气开采", "159697.XSHE"), ("中药", "560080.XSHG"), ("股份制银行", "512730.XSHG"),
    ("创新药", "159377.XSHE"), ("生物疫苗", "562860.XSHG"), ("超级品牌", "159736.XSHE"),
    ("房屋建设", "516970.XSHG"), ("猪肉", "159011.XSHE"), ("证券", "159841.XSHE"),
    ("养鸡", "159023.XSHE"), ("生物制品", "562860.XSHG"), ("煤炭开采", "515220.XSHG"),
    ("养殖业", "159011.XSHE"), ("大消费", "159736.XSHE"), ("中特估100", "515110.XSHG"),
    ("白酒", "512690.XSHG"), ("基础建设", "516970.XSHG"), ("医疗器械", "159873.XSHE"),
    ("电力", "159146.XSHE"), ("中字头股票", "515110.XSHG"), ("住房租赁", "512200.XSHG"),
    ("房地产开发", "512200.XSHG"), ("特钢概念", "515210.XSHG"), ("水泥", "159745.XSHE"),
    ("绿色电力", "159625.XSHE"), ("教育", "513360.XSHG"), ("半导体", "512480.XSHG"),
    ("一带一路", "516970.XSHG"), ("AI算力芯片", "589390.XSHG"), ("旅游及景区", "159766.XSHE"),
    ("互联网金融", "159851.XSHE"), ("金融科技", "159851.XSHE"), ("氟化工概念", "516020.XSHG"),
    ("磷化工", "159870.XSHE"), ("金属铜", "159871.XSHE"), ("装修建材", "159745.XSHE"),
    ("人工智能", "515230.XSHG"), ("区块链", "516860.XSHG"), ("网络安全", "562920.XSHG"),
    ("软件开发", "159899.XSHE"), ("物联网", "562920.XSHG"), ("信创", "159036.XSHE"),
    ("汽车零部件", "159565.XSHE"), ("大数据", "159590.XSHE"), ("智慧城市", "159586.XSHE"),
    ("数据中心", "562920.XSHG"), ("云游戏", "517770.XSHG"), ("小米概念股", "159786.XSHE"),
    ("VR&AR", "159786.XSHE"), ("华为产业链", "516520.XSHG"), ("苹果产业链", "159786.XSHE"),
    ("磷酸铁锂", "515030.XSHG"), ("汽车芯片", "562820.XSHG"), ("智能制造", "159770.XSHE"),
    ("大飞机", "512560.XSHG"), ("数字货币", "159851.XSHE"), ("无人驾驶", "515250.XSHG"),
    ("工业自动化", "560630.XSHG"), ("航母", "512810.XSHG"), ("工业母机", "159667.XSHE"),
    ("电子竞技", "159869.XSHE"), ("手机游戏", "512980.XSHG"), ("央企国企改革", "515110.XSHG"),
    ("机器人概念", "159039.XSHE"), ("稀土永磁", "516780.XSHG"), ("苹果概念", "159786.XSHE"),
    ("云计算", "159899.XSHE"), ("芯片概念", "588240.XSHG"), ("网络游戏", "512980.XSHG"),
    ("汽车电子", "516520.XSHG"), ("小金属", "561050.XSHG"), ("消费电子", "159732.XSHE"),
    ("乘用车", "159512.XSHE"), ("光伏设备", "516290.XSHG"), ("电池", "159757.XSHE"),
    ("航空装备", "159208.XSHE"), ("游戏", "159869.XSHE"), ("影视院线", "516620.XSHG"),
    ("通信设备", "159507.XSHE"),
]

# 板块 → 代码（去重，保留首个；同代码多板块不影响排名）
SEED_POOL = {}
for _s, _c in _SEED_RAW:
    SEED_POOL.setdefault(_c, _s)
SEED_CODES = list(SEED_POOL.keys())

# 动态扩展用的板块关键词（从板块名提炼，命中即视为行业 ETF 候选）
SECTOR_KEYWORDS = [
    "油气", "石油", "中药", "银行", "创新药", "疫苗", "生物", "品牌", "猪肉", "证券",
    "养鸡", "养殖", "消费", "中特估", "白酒", "酒", "基建", "医疗", "器械", "电力",
    "中字头", "住房", "房地产", "特钢", "钢铁", "水泥", "建材", "绿色电力", "教育",
    "半导体", "芯片", "算力", "旅游", "互联网金融", "金融科技", "氟化工", "磷化工",
    "金属铜", "铜", "装修", "人工智能", "区块链", "网络", "安全", "软件", "物联网",
    "信创", "汽车", "零部件", "大数据", "智慧城市", "数据中心", "云游戏", "小米",
    "VR", "华为", "苹果", "磷酸铁锂", "锂电", "智能制造", "智能", "大飞机", "数字",
    "货币", "无人驾驶", "工业", "自动化", "航母", "军工", "母机", "电子竞技", "游戏",
    "手机游戏", "央企", "机器人", "稀土", "云计算", "网络游戏", "消费电子", "乘用车",
    "光伏", "电池", "航空", "影视", "传媒", "通信",
]
# 排除词：避免把货币/债券/商品类误当行业 ETF 纳入
EXCLUDE_KEYWORDS = ["货币", "理财", "现金", "短债", "国债", "地方债", "信用债", "转债",
                    "企业债", "金融债", "可转债", "黄金", "白银", "豆粕", "能源", "石油"]


# ============================================================
# 工具函数（与本地 rotation_v6 / joinquant_unified 一致）
# ============================================================
def ema(series, period):
    series = np.asarray(series, dtype=float)
    n = len(series)
    if n == 0:
        return series
    if n < period:
        return np.array([np.nan] * n, dtype=float)
    alpha = 2.0 / (period + 1)
    result = np.array([np.nan] * n, dtype=float)
    result[period - 1] = np.mean(series[:period])
    for i in range(period, n):
        result[i] = alpha * series[i] + (1 - alpha) * result[i - 1]
    return result


def calc_trix(closes, period=TRIX_PERIOD):
    closes = np.asarray(closes, dtype=float)
    if len(closes) < period * 3 + 1:
        return np.zeros(len(closes))
    e1 = ema(closes, period)
    e2 = ema(e1, period)
    e3 = ema(e2, period)
    trix = np.zeros(len(e3))
    for i in range(1, len(e3)):
        prev = e3[i - 1]
        if prev and not np.isnan(prev) and prev != 0:
            trix[i] = (e3[i] - prev) / prev * 100.0
    return trix


def trix_death_cross(closes, signal_period=TRIX_SIGNAL_PERIOD):
    """TRIX 死叉判定（与 unified 一致）。"""
    trix_raw = calc_trix(closes)
    sig_raw = ema(trix_raw, signal_period)
    if len(trix_raw) < 3:
        return False
    t_prev, t_curr = trix_raw[-2], trix_raw[-1]
    s_prev, s_curr = sig_raw[-2], sig_raw[-1]
    if np.isnan(t_prev) or np.isnan(t_curr) or np.isnan(s_prev) or np.isnan(s_curr):
        return False
    return (t_prev > s_prev) and (t_curr < s_curr)


def cur_close(code, now):
    """当前分钟收盘（get_price 1m 最后一根），无则 None。"""
    try:
        df = get_price(code, end_date=now, count=1, frequency="1m",
                       fields=["close"], skip_paused=True)
        if df is not None and len(df) > 0:
            return float(df["close"].values[-1])
    except Exception:
        pass
    return None


def prev_day_return(code, now):
    """买入日前一交易日的涨幅(%)，对齐 8way 的 PREV_DAY_SURGE_LIMIT 过滤。"""
    try:
        df = attribute_history(code, 3, unit="1d", fields=["close"],
                               skip_paused=True)
        if df is None or len(df) < 3:
            return 0.0
        c = np.asarray(df["close"].values, dtype=float)
        if c[-3] <= 0:
            return 0.0
        return (c[-2] / c[-3] - 1) * 100.0
    except Exception:
        return 0.0


def v6_live(code, current_dt):
    """盘中 v6 得分（含当日 live close），与 rotation_v6.compute_v6_score 等价。

    取近 6 根日K：最后一根=当日 live close，前 5 根为历史。
    近3日涨幅之和 = 最近 3 根日收益(%)，量比 = 当日量 / 前5日量均值。
    """
    try:
        df = attribute_history(code, 6, unit="1d",
                               fields=["close", "volume"], skip_paused=True)
    except Exception:
        return 0.0
    if df is None or len(df) < 4:
        return 0.0
    closes = list(np.asarray(df["close"].values, dtype=float))
    vols = np.asarray(df["volume"].values, dtype=float)
    # 用盘中实时价替换当日日K收盘（对齐本地 14:50 用当日价打分）
    c0 = cur_close(code, current_dt)
    if c0 is not None:
        closes[-1] = c0
    closes = np.asarray(closes, dtype=float)
    # 日收益(%)
    rets = [(closes[i] / closes[i - 1] - 1) * 100.0 for i in range(1, len(closes))]
    if len(rets) < SCORE_WINDOW:
        return 0.0
    ret_w = sum(rets[-SCORE_WINDOW:])
    # 量比：当日量 / 前 VOL_AVG_PERIOD 日量均值（不含当日）
    today_vol = vols[-1]
    prev_vols = vols[-(VOL_AVG_PERIOD + 1):-1]
    if len(prev_vols) > 0 and np.sum(prev_vols) > 0:
        avg_vol = np.mean(prev_vols)
    else:
        avg_vol = today_vol
    vol_ratio = (today_vol / avg_vol) if avg_vol > 0 else 1.0
    vol_factor = VOL_BASE + (1 - VOL_BASE) * min(vol_ratio / VOL_THRESHOLD, 1.0)
    return ret_w * vol_factor


def build_pool(context, current_dt):
    """动态池：种子 + 上市日门禁 + (可选)新发行业 ETF 自动扩展。

    返回去重后的 ETF 代码列表。get_all_securities 传入 current_dt 即按上市日过滤，
    未上市代码不会出现，天然无前视。
    """
    try:
        all_funds = get_all_securities(["fund"], current_dt)
    except Exception:
        return list(SEED_CODES)
    listed = set(all_funds.index)
    pool = [c for c in SEED_CODES if c in listed]

    if DYNAMIC_EXPAND:
        for code, row in all_funds.iterrows():
            if code in pool:
                continue
            name = str(row.get("display_name", "") or "")
            if not name:
                continue
            if any(k in name for k in EXCLUDE_KEYWORDS):
                continue
            if any(k in name for k in SECTOR_KEYWORDS):
                pool.append(code)
    return pool


# ============================================================
# 聚宽框架
# ============================================================
def initialize(context):
    log.info("ROTATION v6 · JQ 单文件 · 动态池+回测")
    set_option("use_real_price", True)
    set_benchmark("000300.XSHG")
    # 对齐本地"无滑点"回测；想看含聚宽默认滑点的保守数，注释下面这行
    set_slippage(FixedSlippage(0.0))
    # ETF 免印花税；佣金万3、最低5元，对齐本地 fee=FEE_PCT=0.03
    set_order_cost(OrderCost(open_tax=0, close_tax=0,
                             open_commission=0.0003, close_commission=0.0003,
                             min_commission=5), type="fund")

    g.sig = None        # {code, sector, baseline, sig_date}
    g.pos = None        # {code, buy_price, buy_date, peak}
    g.trades = []       # 成交记录
    g.pool_size_day = 0

    run_daily(signal_at, SIGNAL_TIME)
    # 每分钟检查买卖触发（handle_data 在分钟频率下每根 bar 调用）
    run_daily(every_bar, "every_bar")


def signal_at(context):
    """信号时刻选股：对全池算 v6，取 TOP1（持仓中则不重发，对齐 8way 空仓才发）。"""
    now = context.current_dt
    if g.pos is not None:
        return
    pool = build_pool(context, now)
    g.pool_size_day = len(pool)

    best_code, best_sector, best_score, best_price = None, None, -1e9, 0.0
    for code in pool:
        price = cur_close(code, now)
        if price is None:
            continue
        sc = v6_live(code, now)
        if sc > best_score:
            best_score, best_code, best_price = sc, code, price
            best_sector = SEED_POOL.get(code, code)

    if best_code is None or best_score < MIN_V6:
        g.sig = None
        return

    g.sig = {
        "code": best_code,
        "sector": best_sector,
        "baseline": best_price,
        "sig_date": now.date(),
    }
    log.info("[信号] %s(%s) v6=%.2f baseline=%.3f 池=%d"
             % (best_sector, best_code, best_score, best_price, g.pool_size_day))


def every_bar(context):
    now = context.current_dt
    now_t = now.time()

    # ---------- 买入阶段（仅信号日当天 SIGNAL_DT~15:00 尝试；不兜底、T+1 不买）----------
    # 对齐本地 8way run_one：信号日当天未触发则放弃，下一天 signal_at 重新发信号。
    if g.sig is not None and g.pos is None:
        sig = g.sig
        code = sig["code"]
        # 信号已过期（T+1 及以后）→ 丢弃（signal_at 会重新发新信号）
        if now.date() != sig["sig_date"]:
            g.sig = None
            return
        # 还没到信号时刻，等待
        if now_t < SIGNAL_DT:
            return
        price = cur_close(code, now)
        if price is None:
            return
        # 前日暴涨过滤（防追高暴跌，对齐 8way PREV_DAY_SURGE_LIMIT）
        if prev_day_return(code, now) > PREV_DAY_SURGE_LIMIT:
            g.sig = None
            log.info("[过滤] %s 前日涨%.1f%%>%s%% 跳过"
                     % (code, prev_day_return(code, now), PREV_DAY_SURGE_LIMIT))
            return
        # 追涨 / 抄底（抄底默认禁用）
        buy_triggered = False
        reason = ""
        if BUY_DOWN < 90 and price <= sig["baseline"] * (1 - BUY_DOWN / 100.0):
            buy_triggered, reason = True, "抄底"
        if price >= sig["baseline"] * (1 + BUY_UP / 100.0):
            buy_triggered, reason = True, "追涨"
        if buy_triggered:
            order_target_value(code, context.portfolio.available_cash)
            g.pos = {
                "code": code,
                "buy_price": price,
                "buy_date": now.date(),
                "peak": price,
            }
            g.sig = None
            log.info("[买入] %s @ %.3f (baseline %.3f, %s)"
                     % (code, price, sig["baseline"], reason))
        elif now_t > dtime(15, 0):
            # 信号日收盘仍未触发 → 放弃本次信号，下一天 signal_at 重新发（对齐 8way）
            g.sig = None

    # ---------- 卖出阶段（严格 > 买入日，满足 T+1）----------
    if g.pos is not None:
        pos = g.pos
        code = pos["code"]
        if now.date() <= pos["buy_date"]:
            return
        price = cur_close(code, now)
        if price is None:
            return
        if price > pos["peak"]:
            pos["peak"] = price

        sell = False
        reason = ""
        # 1) TRIX(5,3) 5分钟死叉
        try:
            cdf = get_price(code, end_date=now, count=120,
                            frequency="5m", fields=["close"], skip_paused=True)
            if cdf is not None and len(cdf) >= TRIX_PERIOD * 3 + 2:
                if trix_death_cross(cdf["close"].values):
                    sell, reason = True, "TRIX死叉"
        except Exception:
            pass
        # 2) 追踪止盈
        if not sell:
            up = (price / pos["buy_price"] - 1) * 100.0
            draw = (pos["peak"] / price - 1) * 100.0
            if up >= TRAIL_START and draw >= TRAIL_DROP:
                sell, reason = True, "追踪止盈"
        # 3) 买入日后首日收盘强制卖（兜底）
        if not sell and now.date() >= pos["buy_date"] + timedelta(days=1) \
                and now_t >= dtime(14, 57):
            sell, reason = True, "收盘兜底"

        if sell:
            order_target(code, 0)
            ret = (price / pos["buy_price"] - 1) * 100.0
            g.trades.append({
                "code": code, "buy_date": str(pos["buy_date"]),
                "buy_price": pos["buy_price"], "sell_date": str(now.date()),
                "sell_price": price, "ret": ret,
            })
            log.info("[卖出] %s @ %.3f 收益=%.2f%% (%s)"
                     % (code, price, ret, reason))
            g.pos = None


def on_strategy_end(context):
    n = len(g.trades)
    if n == 0:
        log.info("[SUMMARY] 无成交")
        return
    wins = sum(1 for t in g.trades if t["ret"] > 0)
    value = context.portfolio.portfolio_value
    start = context.portfolio.starting_cash
    log.info("=" * 50)
    log.info("[SUMMARY] 成交=%d 胜率=%.1f%%" % (n, 100.0 * wins / n))
    log.info("[SUMMARY] 组合终值=%.0f 起始=%d 总回报=%.2f%% (=网页策略收益)"
             % (value, start, (value / start - 1) * 100.0))
    log.info("=" * 50)

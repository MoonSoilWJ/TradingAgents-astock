"""
配对收敛薄补充腿 · 聚宽回测（独立验证版 · 双保险 v3）
====================================================================
对齐本地 scripts/backtest_rv_attack_conditional.py 的「D 双gate」方案：
- 仅在「动量熄火(全市场无 ETF 日内涨≥3%) 且 regime≠趋势」时，部署
  GOLD/NASDAQ/HSCEI 双胞胎配对收敛，做多便宜侧，隔夜持有至比值回归/≤3天平。
- 本文件是独立策略(不含 B 核心腿)，用于在聚宽干净数据交叉验证配对腿自身成绩。
- 定位「薄补充点缀」：仓位仅 15%，每天至多开一对。

★ 双保险建对：
  (1) init 用 previous_date 前 260 日历史快速筛 corr>=0.90 双胞胎对；
  (2) 若 init 取数失败(聚宽 previous_date 为 None / get_price 返回空)，
      pair_job 每天累积收盘价、动态补对(约 260 天预热后自动开始交易)。
  两条路径都用回测可用数据，绝不引入回测期之后数据(零前视)。

用法：
  1. 聚宽 → 新建策略 → 粘贴本文件
  2. 频率: 分钟
  3. 起始/结束: 2022-06-15 ~ 2026-08-03
  4. 资金: 100000
  5. 首行日志打印『PAIR v3 · 历史close天数=...』与『双胞胎对=N -> ...』。
     N>0 即可；若 N=0 也会在预热后自动补对(看后续『动态补对』日志)。

参数与本地 D 方案完全一致：
  MIN_GAIN_MOM=0.03  L=30  MINL=15  ENTRY_K=2.5  EXIT_K=0.3
  MIN_ABS=0.005  MAX_DAYS=3  CAPITAL_PCT=0.15

★ 数据获取一律用 df=True(返回真实 DataFrame)。
★ 状态存聚宽全局对象 g；context 仅取内置属性(portfolio/current_dt/previous_date)。
"""
import math
import statistics


# ===================== 参数（对齐本地 D 方案） =====================
MIN_GAIN_MOM = 0.03     # 动量熄火阈值(日内涨幅 <3% 视为无动量行情)
L = 30                  # z-score 滚动窗口
MINL = 15               # 至少需多少历史点才计算
ENTRY_K = 2.5           # 入场 |z| 阈值
EXIT_K = 0.3            # 回归平仓 |z| 阈值(同侧回到±0.3)
MIN_ABS = 0.005         # 最小绝对偏离(比值对数)
MAX_DAYS = 3            # 最长持有交易日数
CAPITAL_PCT = 0.15      # 薄补充腿仓位(全账户15%)
REGIME_PROXY = "501018.XSHG"
WARMUP_DAYS = 260       # 动态补对所需最少历史天数

# 干净子宇宙候选(剔除不回归的 HSTECH/HKINTERNET/SP500/NIKKEI/OIL)
FAMILIES = {
    "GOLD":   ["518880.XSHG", "159934.XSHE", "518600.XSHG", "518660.XSHG",
               "518800.XSHG", "517520.XSHG", "159812.XSHE"],
    "NASDAQ": ["513100.XSHG", "159941.XSHE", "513300.XSHG", "513400.XSHG", "513850.XSHG"],
    "HSCEI":  ["510900.XSHG", "513600.XSHG", "513630.XSHG", "513730.XSHG",
               "513900.XSHG", "513750.XSHG"],
}
CORR_MIN = 0.90         # 双胞胎相关性门槛(与本地一致)


# ===================== 工具函数 =====================
def _corr(xs, ys):
    """日收益相关性(与本地 daily_ret 口径一致)。xs/ys: list of (idx, ret)。"""
    s1 = dict(xs); s2 = dict(ys)
    common = [d for d in s1 if d in s2]
    if len(common) < 150:
        return None
    a = [s1[d] for d in common]
    b = [s2[d] for d in common]
    n = len(a); ma = sum(a) / n; mb = sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((y - mb) ** 2 for y in b) ** 0.5
    return cov / (va * vb) if va > 1e-9 and vb > 1e-9 else None


def _series_from_closes(cls):
    """close 序列 -> list of (i, 日收益)。"""
    return [(i, cls[i] / cls[i - 1] - 1) for i in range(1, len(cls)) if cls[i - 1] > 0]


def _detect_regime(closes):
    """对齐本地 detect_regime: 30日窗口, MA20偏离 + ADX + 穿越数。"""
    if len(closes) < 30:
        return "中性"
    ma20 = sum(closes[-20:]) / 20
    close = closes[-1]
    dist = abs(close - ma20) / ma20 * 100 if ma20 else 0
    crosses = 0
    prev = None
    for i in range(len(closes) - 10, len(closes)):
        above = closes[i] > sum(closes[i - 20 + 1:i + 1]) / 20
        if prev is not None and above != prev:
            crosses += 1
        prev = above

    def adx(cls, p=14):
        if len(cls) < p + 2:
            return 0.0
        trs, pdm, mdm = [], [], []
        for i in range(1, len(cls)):
            up = cls[i] - cls[i - 1]
            dn = cls[i - 1] - cls[i]
            pdm.append(up if up > 0 else 0)
            mdm.append(dn if dn > 0 else 0)
            trs.append(abs(cls[i] - cls[i - 1]))

        def w(v):
            s = sum(v[:p]); out = [None] * p; out.append(s)
            for i in range(p, len(v) - 1):
                s = s - s / p + v[i + 1]
                out.append(s)
            return out
        atr, pdi, mdi = w(trs), w(pdm), w(mdm)
        dx = []
        for i in range(p, len(trs)):
            if atr[i]:
                p1 = 100 * pdi[i] / atr[i]
                m1 = 100 * mdi[i] / atr[i]
                dx.append(100 * abs(p1 - m1) / (p1 + m1) if (p1 + m1) else 0)
        return sum(dx) / len(dx) if dx else 0.0

    a = adx(closes)
    if crosses >= 2:
        return "震荡"
    elif dist > 8.0 and a > 30.0:
        return "趋势"
    return "中性"


def _last_close(code):
    """取 code 最新一个交易日收盘价(float); df=True 返回真实 DataFrame。"""
    h = attribute_history(code, 1, "1d", ["close"], skip_paused=True, df=True)
    if h is None or h.empty:
        return None
    return float(h["close"].iloc[-1])


# ===================== 初始化（聚宽必须用 initialize 作为初始化函数名） =====================
def initialize(context):
    g.day_count = 0
    g.position = None
    g.members = [m for fam, ms in FAMILIES.items() for m in ms]
    g.close_hist = {m: [] for m in g.members}   # 动态累积收盘价(双保险兜底)
    g.pairs, g.lens = _build_pairs_history(context)
    g.pair_keys = set()
    for (A, B, _, _) in g.pairs:
        g.pair_keys.add(tuple(sorted((A, B))))
    g.ratio_hist = {(a, b): [] for (a, b, _, _) in g.pairs}
    try:
        g.all_sec = list(get_all_securities(["etf", "lof"]).index)
    except Exception:
        g.all_sec = []
    g.mom_broken = False
    log.info("PAIR v3 · 历史close天数=%s" % g.lens)
    if not g.pairs:
        log.info("PAIR v3 · init未建出对(历史取数失败), 将在约%d天预热后动态补对" % WARMUP_DAYS)
    else:
        log.info("PAIR v3 · 双胞胎对=%d -> %s"
                 % (len(g.pairs), [f"{a}/{b}" for a, b, _, _ in g.pairs]))
    run_daily(pair_job, "14:55")


def _build_pairs_history(context):
    """init 快路径: 用 previous_date 前 260 日历史筛 corr>=0.90 双胞胎对。"""
    end_str = None
    try:
        if getattr(context, "previous_date", None) is not None:
            end_str = context.previous_date.strftime("%Y-%m-%d")
    except Exception:
        end_str = None
    closes = {}
    lens = {}
    for fam, members in FAMILIES.items():
        for m in members:
            try:
                if end_str:
                    px = get_price(m, end_date=end_str, count=WARMUP_DAYS,
                                   frequency="daily", fields="close",
                                   skip_paused=True, df=True)
                else:
                    px = get_price(m, count=WARMUP_DAYS, frequency="daily",
                                   fields="close", skip_paused=True, df=True)
                if px is None or px.empty:
                    closes[m] = []; lens[m] = 0
                else:
                    s = list(px["close"].dropna())
                    closes[m] = s; lens[m] = len(s)
            except Exception:
                closes[m] = []; lens[m] = -1
    series = {m: _series_from_closes(cl) for m, cl in closes.items()}
    pairs = []
    for fam, members in FAMILIES.items():
        avail = [m for m in members if len(series.get(m, [])) >= 200]
        for i in range(len(avail)):
            for j in range(i + 1, len(avail)):
                c = _corr(series[avail[i]], series[avail[j]])
                if c is not None and c >= CORR_MIN:
                    A, B = sorted((avail[i], avail[j]))
                    pairs.append((A, B, round(c, 3), fam))
    return pairs, lens


def _try_dynamic_pairs(context):
    """双保险兜底: 用回测运行中累积的 close_hist 动态补对(仅预热期内跑)。"""
    if g.day_count > WARMUP_DAYS + 30:
        return
    added = []
    for fam, members in FAMILIES.items():
        avail = [m for m in members if len(g.close_hist.get(m, [])) >= 200]
        for i in range(len(avail)):
            for j in range(i + 1, len(avail)):
                key = tuple(sorted((avail[i], avail[j])))
                if key in g.pair_keys:
                    continue
                c = _corr(_series_from_closes(g.close_hist[avail[i]]),
                          _series_from_closes(g.close_hist[avail[j]]))
                if c is not None and c >= CORR_MIN:
                    A, B = key
                    g.pairs.append((A, B, round(c, 3), fam))
                    g.pair_keys.add(key)
                    g.ratio_hist[(A, B)] = []
                    added.append(f"{A}/{B}(c={c:.2f})")
    if added:
        log.info("  [动态补对] +%d -> %s" % (len(added), added))


# ===================== gate 判定 =====================
def _mom_fired(context):
    """当日 14:55 全市场是否任一 ETF/LOF 日内涨幅≥3%。"""
    if not g.all_sec:
        return False
    try:
        px = get_price(g.all_sec, end_date=context.current_dt, count=2,
                       frequency="1d", fields="close", skip_paused=True, df=True)
    except Exception:
        if not g.mom_broken:
            g.mom_broken = True
            log.warn("  [WARN] _mom_fired 取数异常, 暂按'动量熄火'处理(不拦截配对)")
        return False
    if px is None or px.empty:
        return False
    best = -9.0
    try:
        codes = px.index.get_level_values(1).unique()
    except Exception:
        codes = g.all_sec
    for code in codes:
        try:
            sub = px.xs(code, level=1)["close"]
            if len(sub) < 2:
                continue
            prev, cur = float(sub.iloc[-2]), float(sub.iloc[-1])
            if prev and prev > 0:
                best = max(best, cur / prev - 1)
        except Exception:
            continue
    return best >= MIN_GAIN_MOM


def _regime_non_trend(context):
    h = attribute_history(REGIME_PROXY, 30, "1d", ["close"], skip_paused=True, df=True)
    if h is None or h.empty:
        return True  # 数据不足默认非趋势(允许触发)
    closes = [float(x) for x in h["close"] if x]
    if len(closes) < 30:
        return True
    return _detect_regime(closes) != "趋势"


# ===================== 主循环 =====================
def pair_job(context):
    g.day_count += 1
    today = context.current_dt.date()

    # 0) 累积各 member 当日收盘价(双保险兜底 + 比值更新数据源)
    for m in g.members:
        c = _last_close(m)
        if c and c > 0:
            g.close_hist[m].append(c)
            if len(g.close_hist[m]) > WARMUP_DAYS:
                g.close_hist[m] = g.close_hist[m][-WARMUP_DAYS:]
    _try_dynamic_pairs(context)

    # 1) 更新所有 pair 的今日 log 比值
    for (A, B) in g.ratio_hist:
        ca = _last_close(A); cb = _last_close(B)
        if ca and cb and ca > 0 and cb > 0:
            g.ratio_hist[(A, B)].append(math.log(ca / cb))
            if len(g.ratio_hist[(A, B)]) > L + 10:
                g.ratio_hist[(A, B)] = g.ratio_hist[(A, B)][-L - 10:]

    if g.day_count % 20 == 0 or g.day_count <= 5:
        log.info("[%s] day#%d 配对巡检 (pairs=%d)" % (today, g.day_count, len(g.pairs)))

    # 2) 平仓检查(持有中)
    if g.position:
        (A, B, _, fam) = g.position["pair"]
        hist = g.ratio_hist[(A, B)]
        if len(hist) >= 2:
            s = hist[-1]
            past = hist[-L - 1:-1]
            mu = statistics.mean(past) if past else s
            sd = statistics.pstdev(past) if len(past) >= 2 else 0
            z = (s - mu) / sd if sd > 1e-9 else 0
            held = g.day_count - g.position["entry_count"]
            leg = g.position["leg"]
            cond = (z >= EXIT_K) if leg == "A" else (z <= -EXIT_K)
            if cond or held >= MAX_DAYS:
                order_target_value(g.position["code"], 0)
                log.info("  [%s] 平仓 %s (leg=%s, z=%.2f, held=%d天)"
                         % (today, g.position["code"], leg, z, held))
                g.position = None

    # 3) 开仓检查(空仓 + 双gate 满足)
    if g.position is None:
        if _mom_fired(context):
            return
        if not _regime_non_trend(context):
            return
        for (A, B, _, fam) in g.pairs:
            hist = g.ratio_hist[(A, B)]
            if len(hist) < MINL + 1:
                continue
            s = hist[-1]
            past = hist[-L - 1:-1]
            if len(past) < MINL:
                continue
            mu = statistics.mean(past)
            sd = statistics.pstdev(past)
            if sd <= 1e-9:
                continue
            if abs(s - mu) < MIN_ABS:
                continue
            z = (s - mu) / sd
            if z <= -ENTRY_K:
                code, leg = A, "A"
            elif z >= ENTRY_K:
                code, leg = B, "B"
            else:
                continue
            val = context.portfolio.total_value * CAPITAL_PCT
            order_target_value(code, val)
            g.position = {"pair": (A, B, _, fam), "code": code,
                          "leg": leg, "entry_count": g.day_count}
            log.info("  [%s] 开仓 %s (leg=%s, z=%.2f, 市值=%.0f)"
                     % (today, code, leg, z, val))
            break  # 每天至多一对(薄补充点缀)


# ===================== 收尾 =====================
def on_strategy_end(context):
    # cleanup 阶段 g/context 自定义属性可能已被框架重置,
    # 直接平掉 portfolio 里实际持有的标的(不读任何自定义状态)
    for code in list(context.portfolio.positions.keys()):
        order_target_value(code, 0)

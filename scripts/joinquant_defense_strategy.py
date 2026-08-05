"""
防守腿验证 · 聚宽（JoinQuant）回测   【v3 · 等权多资产】
==================================================
目的: 验证「跨资产防守组合」在 A 股弱市（2022-2023 / 2025-2026）能否
      提供低回撤的正收益，作为大一统策略里进攻腿失效时的兜底引擎。

这是【大一统策略】的第二个引擎。第一个引擎是现有 T0 隔夜动量策略
（joinquant_a_strategy.py, 全周期+201%但收益全集中在 2024-2025 动量年）。

────────────────────────────────────────────────
v2 → v3 的核心改动（为什么必须改）
────────────────────────────────────────────────
v2 = 单品种月度动量（每月满仓押 1 只冠军）。实测问题:
  · 全周期回撤 28.53%，最大回撤区间 2026/01-2026/07
  · 原因: 满仓押黄金 → 1 月尖顶后一路阴跌，单只资产直接决定组合命运
  · 加 200MA 过滤也救不了尖顶快反（慢线滞后，确认跌破时已跌掉大半）
  → 结论: 「单品种 + 慢线」结构天生不是防守，只是换了个赌法

v3 = 等权多资产（All-Weather 思路）:
  1. 不再选冠军，凡是【弱市抗跌】的候选一律等权持有
  2. 权重固定 1/N（N = 候选池大小，未上市标的自动剔除后重新归一）
     → 单只崩盘最多拖累 1/N，回撤由分散度决定
  3. ★ 2026-08-04 修正: 完全去掉动量/MA过滤（原 v3 的 60/200MA 双确认
     在弱市把所有资产过滤成现金 → 防守腿失效，弱市段仍 -12%）
     → 改法: 永久等权持有「弱市抗跌资产」(国债/黄金/红利/可转债)，
       这些资产长期正收益、弱市也涨，不需要任何择时/过滤。

策略逻辑:
  1. 每月第一个交易日调仓一次（handle_data 每日触发 + 函数内去重，无未来函数）
  2. 仅剔除「当前回测日尚未上市/无行情」的标的（如 511090 在 2022 尚未上市）
  3. 其余标的一律等权持有，权重 = 1 / 已上市标的数
  4. 仅当目标仓位偏离超过阈值才下单（省手续费，2万本金最低佣金5元敏感）

候选资产（弱市抗跌、长期正收益，收益来源互不相关）:
  - 511090 30年国债ETF   → 利率下行受益（弱市/降息，债券牛市）
  - 511260 十年国债ETF   → 同上，久期较短
  - 518880 黄金ETF       → 避险/通胀/美元走弱
  - 510880 红利ETF       → A股弱市抗跌（2022年+2.3%正收益！真正的弱市资产）
  - 511380 可转债ETF     → 债底保护（2022年仅-5.6%，远抗跌于沪深300-21%）
  ★ 不含纳指/标普/沪深300: 弱市全跌会拖累；它们由进攻腿在强市捕获
  兜底 511360 短融ETF    → 全部未上市时的现金替代

用法:
  1. 聚宽 → 新建策略 → 粘贴本文件
  2. 频率: 【日】(月度调仓, 日频足够, 跑得快)
  3. 起始: 2022-06-13 | 结束: 2026-07-31 | 资金: 20000
  4. 首行日志必须出现『DEFENSE v3』，否则说明没粘到最新代码

判定标准（防守腿的本职是「低回撤兜底」，不是跑赢进攻腿）:
  ✅ 通过: 全周期回撤 < 15%，且 2022-2023 与 2025-2026 两段弱市均不亏
  ❌ 失败: 仍有单一时段深度回撤 → 说明分散不足，需再加低相关资产
"""


# ============================================================
# 参数
# ============================================================

DEF_LOOKBACK = 120       # 动量回看窗口（日），约半年
MA_WINDOWS = (60, 200)   # ★ 趋势双确认: 必须同时站上这些均线
                         #   200 → 挡长期熊（2022-12 标普）
                         #   60  → 挡尖顶快反（2026-H1 黄金）
                         #   想退回单慢线过滤: 改成 (200,)
TARGET_EXPOSURE = 0.98   # 总仓位上限（留 2% 防手续费不足）
REBAL_TOL = 0.15         # 再平衡阈值: 当前市值与目标偏离 >15% 才下单（省最低佣金）
MIN_ORDER_VALUE = 800    # 单笔最小下单金额（低于此不值当，2万本金 × 万3 + 最低5元）

# 调仓时点: 当月【第一个交易日】（handle_data 每日触发 + mkey 去重）
# ⚠ 踩坑记录（都导致过 0 交易，勿回退）:
#   1) run_monthly(time="14:30") / run_daily(time="open") → 日频回测静默不触发
#   2) handle_data 是框架保留钩子，直接 def handle_data(context, data)，不能手动注册
#   3) 额外加 `current_dt.day > 5 → return`（自然日）→ 长假月(10月/5月)整月不调仓

# 候选资产（弱市抗跌、长期正收益）—— 与 unified 策略一致
# ★ 2026-08-04 结构性修正: 仅保留弱市抗跌资产，去掉纳指/标普/沪深300（弱市会拖累）
DEFENSE_POOL = [
    "511090.XSHG",   # 30年国债ETF  — 利率下行受益
    "511260.XSHG",   # 十年国债ETF  — 久期较短
    "518880.XSHG",   # 黄金ETF      — 避险/通胀
    "510880.XSHG",   # 红利ETF      — A股弱市抗跌（2022年+2.3%正收益！）
    "511380.XSHG",   # 可转债ETF    — 债底保护（2022年仅-5.6%）
]

CASH_ETF = "511360.XSHG"   # 短融ETF（货币替代，价格真实变动，回测可计价；
                             #   511990 货币ETF 价格恒100、收益以份额发放→回测显示0收益，弃用）


# ============================================================
# 初始化
# ============================================================

def initialize(context):
    set_benchmark("510300.XSHG")
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)

    # ETF 手续费: 万3, 无印花税, 最低5元
    set_order_cost(OrderCost(
        open_tax=0, close_tax=0,
        open_commission=0.0003, close_commission=0.0003,
        close_today_commission=0, min_commission=5
    ), type="fund")

    log.set_level("order", "error")

    g.rebalance_log = []      # 调仓记录
    g.last_rebal_month = ""   # 每月只调一次的去重标记

    # ★ 关键修复: 用框架内置 handle_data（日频每天收盘/分钟频每分钟触发）+ 函数内「每月去重」
    #   日频回测下 run_daily(time="open") 静默不触发 → 0 笔交易（已踩坑两次）
    #   注意: handle_data 是聚宽特殊函数名，框架自动调用，无需在 initialize 里注册

    ma_txt = "/".join(str(w) for w in MA_WINDOWS)
    log.info("=" * 60)
    log.info("【DEFENSE v3 · 等权多资产】跨资产防守组合")
    log.info(f"候选: {len(DEFENSE_POOL)} 只 | 动量: {DEF_LOOKBACK}日 | "
             f"趋势: {ma_txt}MA双确认 | 权重: 各 1/{len(DEFENSE_POOL)} | 兜底: {CASH_ETF}")
    log.info("★ 若回测日志首行没有『DEFENSE v3』字样 → 说明你没粘贴最新代码")
    log.info("=" * 60)


# ============================================================
# 信号计算
# ============================================================

def _is_listed(code, current_dt, lookback):
    """标的在当前回测日是否已上市足够久（且未退市）。未上市 → 数据全 nan。"""
    try:
        info = get_security_info(code)
        if info is None:
            return False
        d = current_dt.date()
        # 上市日 + 约 lookback 个交易日（按 1.5 倍自然日折算）之后才有完整动量
        if info.start_date is not None:
            if (d - info.start_date).days < lookback * 1.5:
                return False
        if info.end_date is not None and d >= info.end_date:
            return False
        return True
    except Exception:
        return True   # 取不到信息时不拦截，交给下面的 nan 过滤兜底


def calc_momentum(code, current_dt, lookback):
    """过去 lookback 个交易日的累计收益率（%）。数据不足/含 nan 返回 None。"""
    # ★★ 关键修复1: 未上市标的（如 511090 在 2022 年尚未上市）行情全为 nan，
    #    nan 参与排序会「赢」（nan 的比较恒 False）→ 每月选中它 → 下单失败 → 全程空仓
    if not _is_listed(code, current_dt, lookback):
        return None
    try:
        # 用 attribute_history（以当前回测时间为终点取历史，能取回测前数据）
        df = attribute_history(code, lookback + 1, unit="1d",
                               fields=["close"], skip_paused=True)
        if df is None:
            return None
        # ★★ 关键修复2: 剔除 nan 行后再判断长度
        df = df.dropna()
        n = len(df)
        if n < lookback:
            log.info(f"[动量诊断] {code} 需≥{lookback}行 有效{n}行 → 跳过")
            return None
        c0 = float(df["close"].iloc[0])
        c1 = float(df["close"].iloc[-1])
        # nan 自比不等；同时挡住 0/负价
        if c0 != c0 or c1 != c1 or c0 <= 0 or c1 <= 0:
            log.info(f"[动量诊断] {code} 价格非法 c0={c0} c1={c1} → 跳过")
            return None
        mom = (c1 - c0) / c0 * 100.0
        if mom != mom:
            return None
        return mom
    except Exception as e:
        log.info(f"[动量诊断] {code} 异常: {e}")
        return None


def trend_state(code):
    """双均线趋势确认。

    返回 (ok, flag):
      ok   — 收盘价是否同时站上 MA_WINDOWS 里的每一条均线
      flag — 日志标记，按 MA_WINDOWS 顺序给出 ↑(站上)/↓(跌破)/·(数据不足)

    数据不足的那条均线跳过不判（新上市标的不误杀）；
    若所有均线都算不出 → ok=True，交给动量条件兜底。
    """
    need = max(MA_WINDOWS)
    try:
        df = attribute_history(code, need, unit="1d",
                               fields=["close"], skip_paused=True)
        if df is None:
            return True, "·" * len(MA_WINDOWS)
        closes = df["close"].dropna()
        if len(closes) == 0:
            return True, "·" * len(MA_WINDOWS)
        last = float(closes.iloc[-1])
        if last != last or last <= 0:
            return True, "·" * len(MA_WINDOWS)

        ok = True
        flags = []
        judged = 0
        for w in MA_WINDOWS:
            if len(closes) < w:
                flags.append("·")          # 该窗口数据不足，不判
                continue
            ma = float(closes.iloc[-w:].mean())
            if ma != ma or ma <= 0:
                flags.append("·")
                continue
            judged += 1
            if last > ma:
                flags.append("↑")
            else:
                flags.append("↓")
                ok = False
        if judged == 0:
            return True, "".join(flags)    # 一条都算不出 → 不拦截
        return ok, "".join(flags)
    except Exception:
        return True, "·" * len(MA_WINDOWS)


# ============================================================
# 月度调仓
# ============================================================

def handle_data(context, data):
    today = context.current_dt.strftime("%Y-%m-%d")
    mkey = context.current_dt.strftime("%Y-%m")

    # --- 每月只调仓一次: 当月【第一个交易日】触发，之后靠 mkey 去重跳过 ---
    #    ★★ 关键修复5: 原来还加了 `day > 5 → return`（自然日），遇长假月（10月/5月）
    #       前5个自然日全是假期 → 整月不调仓（日志实证 2022-10/2023-10/2024-05 全缺）
    if mkey == g.last_rebal_month:
        return
    g.last_rebal_month = mkey

    # --- 1. ★ 2026-08-04 修正: 只剔除「未上市/无行情」标的，不做任何动量/MA过滤 ---
    #    弱市抗跌资产长期正收益，永久等权持有即可，过滤反而会在弱市全清成现金
    eligible = []
    for code in DEFENSE_POOL:
        if not _is_listed(code, context.current_dt, DEF_LOOKBACK):
            continue
        try:
            df = attribute_history(code, 2, unit="1d",
                                   fields=["close"], skip_paused=True)
            if df is None:
                continue
            last = df["close"].dropna()
            if len(last) == 0:
                continue
            if float(last.iloc[-1]) <= 0:
                continue
            eligible.append(code)
        except Exception:
            continue

    if not eligible:
        log.info(f"[{today}] 全部未上市/无行情，保持原持仓")
        return

    # --- 2. 已上市标的数等权归一 ---
    n_slot = len(eligible)
    total = context.portfolio.total_value
    slot_value = total * TARGET_EXPOSURE / n_slot

    targets = {c: slot_value for c in eligible}

    # --- 3. 下单: 先清仓/减仓，再加仓（避免资金不足） ---
    #    只对偏离超过 REBAL_TOL 的做调整，省最低5元佣金（2万本金很敏感）
    cur_val = {}
    for code, pos in context.portfolio.positions.items():
        cur_val[code] = pos.value

    # 3a. 清掉不在目标里的
    for code in list(cur_val.keys()):
        if code not in targets:
            order_target(code, 0)

    # 3b. 需要减仓的先执行（释放资金）
    for code, tv in targets.items():
        cv = cur_val.get(code, 0.0)
        if cv > tv and _need_rebal(cv, tv):
            order_target_value(code, tv)

    # 3c. 再执行需要加仓的
    for code, tv in targets.items():
        cv = cur_val.get(code, 0.0)
        if cv < tv and _need_rebal(cv, tv):
            order_target_value(code, tv)

    # --- 4. 复核真实持仓，写日志 ---
    held = sorted(c.split(".")[0] for c, p in context.portfolio.positions.items()
                  if p.total_amount > 0)
    g.rebalance_log.append((today, tuple(held)))
    log.info(f"[{today}] 调仓 | 弱市抗跌等权 {n_slot}只 "
             f"[{' '.join(c.split('.')[0] for c in eligible)}] (无动量/MA过滤)")
    log.info(f"         实际持仓: {' '.join(held) if held else '（空）'} | "
             f"总资产 {total:.0f}")


def _need_rebal(cur, target):
    """偏离是否值得下单: 差额既要超过相对阈值，也要超过绝对最小额。"""
    diff = abs(cur - target)
    if diff < MIN_ORDER_VALUE:
        return False
    base = max(target, cur, 1.0)
    return diff / base >= REBAL_TOL


def _log_ranking(scored):
    """打印动量排名 + 趋势标记（按 MA_WINDOWS 顺序，↑站上/↓跌破/·数据不足）"""
    parts = [f"{c.split('.')[0]}:{m:+.1f}%{flag}" for m, c, _ok, flag in scored]
    log.info(f"         排名: {' | '.join(parts)}")


# ============================================================
# 收盘汇总
# ============================================================

def after_trading_end(context):
    dt = context.current_dt
    # 每年最后一个交易日打印年度汇总
    if dt.month == 12 and dt.day >= 28:
        log.info(f"===== {dt.year} 年末总资产: "
                 f"{context.portfolio.total_value:.2f} =====")

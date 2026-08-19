#!/usr/bin/env python3
"""R3(月度轮动质量池) SHADOW 监控 —— 不替代实盘, 只记录。

与 t0_monitor.py / t0_b_idle_shadow.py 平行运行。选股逻辑与聚宽
joinquant_unified_single.py 对齐 (这是产出 2014-2026 +1861%/夏普1.01 的设计):
  - 先 detect_regime(501018 日K) 判 regime。
  - 趋势/震荡 → build_quality_pool: 从 R3 宇宙按近 LOOKBACK(30) 天动量取 Top POOL_SIZE(25)
    滚动优质池; 中性 → 当月 R3 月度轮动宽池 (<= 当前月最近非空月)。
    (原"宽池永远"是更激进、回测更差的变体, 已弃用以与聚宽一致。)
  - 取当日涨幅 ≥ MIN_GAIN(3%) 的 Top1 (drop_sector 已在池生成时应用)。
  - 日K 经 akshare fund_etf_hist_sina 拉取, 缓存于 r3_daily.json(每天至多一次);
    缺失/网络失败则退化为中性 → 用月度宽池。
  - 卖出用 TRIX(5,3)死叉; 09:40~11:05 内未触发则 11:05 收盘 fallback (对齐回测
    simulate_exit('trix0940_cut'), 全4年 R3 等价 +613% 量级卖点)。
  - 绝不真下单, 不读写实盘 / SHADOW B 状态。独立写入:
      STATE_FILE = ~/.tradingagents/rotation/r3_shadow_state.json
      JOURNAL   = ~/.tradingagents/rotation/r3_journal.jsonl

crontab (与实盘同窗口并行):
  40 14 * * 1-5  cd /path && python3 scripts/t0_r3_monitor.py --pick
  45 14 * * 1-5  cd /path && python3 scripts/t0_r3_monitor.py --signal
  40 9  * * 1-5  cd /path && python3 scripts/t0_r3_monitor.py --sell-loop
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import jq_attack_pools  # noqa: E402 月度轮动攻击池 (R1~R6 内联字面量)
import t0_monitor as TM  # noqa: E402 复用行情/推送/TRIX 函数
from sync_web import sync_to_web  # noqa: E402 买卖信号触发后同步到 Web
from t0_monitor import (  # noqa: E402
    MIN_GAIN, SELL_BAR_LABEL, TRIX_PERIOD, TRIX_SIGNAL_PERIOD,
    TRIX_MIN_SELL, SELL_CUTOFF, SELL_CHECK_START, SELL_CHECK_END,
    SINA_INTERVAL,
    fetch_tencent_quotes, rank_t0_by_today_gain, fetch_sell_kline,
    is_trading_day, time_to_min, trix_death_cross_hit, send_dingtalk,
    resolve_exec_prices, get_all_t0_etfs, scan_etf_universe,
)

STATE_DIR = Path.home() / ".tradingagents/rotation"
STATE_FILE = STATE_DIR / "r3_shadow_state.json"
TRADE_JOURNAL = STATE_DIR / "r3_journal.jsonl"

SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
SELL_LOOP_INTERVAL = 50
CONFIRM_TIME = TM.CONFIRM_TIME  # 14:40 双时点确认


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "strategy": {"name": "R3 月度轮动 SHADOW", "mode": "shadow", "pick": "月度轮动池Top1"},
        "position": None,
        "last_signal_date": None,
        "updated_at": None,
    }


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_journal(rec: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with TRADE_JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---- 与聚宽 joinquant_unified_single.py 对齐的选股逻辑 ----
# 趋势/震荡 → build_quality_pool: 从 R3 宇宙按近 LOOKBACK 天动量取 Top POOL_SIZE(25)
# 中性       → 当月 R3 月度轮动池(缺失回退全并集 R3_UNIVERSE, 对齐聚宽A的 ATTACK_UNIVERSE)
# 这是产出聚宽 2014-2026 +1861%/夏普1.01 的设计; 本地 SHADOW 原先用"宽池永远"是更激进、
# 回测更差的变体, 此处改为与聚宽一致。
REGIME_PROXY = "501018"          # 对应聚宽 REGIME_PROXY(501018.XSHG), 本地用6位
CHOPPY_MA_CROSS = 2
TREND_DIST_MIN = 8.0
TREND_ADX_MIN = 30.0
LOOKBACK = 30                    # 动量训练窗(天)
POOL_SIZE = 25                   # 优质池规模
SELECT_MODE = "a_top1"           # "a_top1"(现状/R3 canonical +1861%) | "hybrid_top5"(14:40锁Top5→14:45幸存者取Top1)
R3_POOLS = jq_attack_pools.JQ_ATTACK_POOLS.get("R3", {})
R3_UNIVERSE = sorted({c for v in R3_POOLS.values() for c in v})  # R3 宇宙并集(jq格式 code.XSHE/XSHG)
R3_DAILY_CACHE = STATE_DIR / "r3_daily.json"


def _fetch_daily_akshare(code6: str) -> list[dict]:
    """用 akshare 拉单只 ETF 日K(对齐 update_live_cache.py), 返回最近若干根。"""
    import akshare as ak
    sym = ("sh" if code6[0] in "56" else "sz") + code6
    h = ak.fund_etf_hist_sina(symbol=sym)
    out = []
    for _, r in h.iterrows():
        try:
            out.append({
                "date": str(r["date"])[:10],
                "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]),
            })
        except Exception:  # noqa: BLE001
            continue
    return out


def ensure_r3_daily() -> dict:
    """确保 R3 宇宙 + 501018 的日K已就绪(每天至多拉一次, 落盘 r3_daily.json)。
    缺失/网络失败 → 返回已有缓存(可能为空), 上层 detect_regime 退化为中性 → 用月度池。"""
    today = date.today().isoformat()
    cache: dict = {}
    if R3_DAILY_CACHE.exists():
        try:
            cache = json.loads(R3_DAILY_CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}
    if cache.get("fetched_at") == today and cache.get("data"):
        return cache["data"]
    data = dict(cache.get("data", {}))
    codes = sorted({"501018"} | {c.split(".")[0] for c in R3_UNIVERSE})
    for code6 in codes:
        try:
            bars = _fetch_daily_akshare(code6)
            if bars:
                data[code6] = bars[-60:]
        except Exception as e:  # noqa: BLE001
            print(f"    [warn] 日K拉取失败 {code6}: {e}", flush=True)
    cache = {"fetched_at": today, "data": data}
    try:
        R3_DAILY_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return data


def _bars_of(code6: str, data: dict) -> list[dict]:
    if code6 in data and data[code6]:
        return data[code6]
    try:
        bars = _fetch_daily_akshare(code6)
        if bars:
            data[code6] = bars[-60:]
            return data[code6]
    except Exception:  # noqa: BLE001
        pass
    return []


def _ma_crosses(closes: list[float], ma_days: int = 20, lookback: int = 10) -> int:
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


def _calc_adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 2:
        return 0.0
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
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


def detect_regime_local(data: dict) -> str:
    """501018 日K 判定 regime(复刻聚宽 detect_regime, 仅用于选股池切换)。"""
    bars = _bars_of(REGIME_PROXY, data)
    if len(bars) < 30:
        return "中性"
    closes = [b["close"] for b in bars[-30:]]
    highs = [b["high"] for b in bars[-30:]]
    lows = [b["low"] for b in bars[-30:]]
    ma20 = sum(closes[-20:]) / 20
    close = closes[-1]
    dist = abs(close - ma20) / ma20 * 100 if ma20 else 0
    crosses = _ma_crosses(closes, 20, 10)
    adx = _calc_adx(highs, lows, closes, 14)
    if crosses >= CHOPPY_MA_CROSS:
        return "震荡"
    elif dist > TREND_DIST_MIN and adx > TREND_ADX_MIN:
        return "趋势"
    return "中性"


def calc_momentum_local(code6: str, data: dict) -> float | None:
    """过去 LOOKBACK 个交易日累计收益率(%)。"""
    bars = _bars_of(code6, data)
    if len(bars) < LOOKBACK + 1:
        return None
    closes = [b["close"] for b in bars]
    c0, c1 = closes[-(LOOKBACK + 1)], closes[-1]
    if c0 <= 0 or c1 <= 0:
        return None
    return (c1 - c0) / c0 * 100.0


def build_quality_pool_local(data: dict) -> list[str]:
    """滚动优质池: R3 宇宙中过去 LOOKBACK 天累计涨幅 Top POOL_SIZE(复刻聚宽)。"""
    scored = []
    for jqcode in R3_UNIVERSE:
        code6 = jqcode.split(".")[0]
        mom = calc_momentum_local(code6, data)
        if mom is not None:
            scored.append((mom, jqcode))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:POOL_SIZE]]


def r3_pool_for_today() -> tuple[list[dict], str, str]:
    """对齐聚宽: 趋势/震荡→动量Top25; 中性→当月R3月度宽池。返回 (etfs, regime, pool_label)。"""
    data = ensure_r3_daily()
    regime = detect_regime_local(data)
    if regime in ("趋势", "震荡"):
        jq_codes = build_quality_pool_local(data)
        label = f"优质池({len(jq_codes)})"
    else:
        # 中性市: 用当月 R3 精确月度池(键=使用月, 值=上月末 pool_as_of, 无未来函数)。
        # 当月缺失则回退到"全并集"(R3_UNIVERSE, 与聚宽A的 ATTACK_UNIVERSE 等价), 而非最近非空月池。
        ym = datetime.now().strftime("%Y-%m")
        monthly = R3_POOLS.get(ym)
        if monthly:
            jq_codes = monthly
            label = f"R3月({len(jq_codes)})[{ym}]"
        else:
            jq_codes = R3_UNIVERSE
            label = f"R3全并集({len(jq_codes)})"
    etfs = []
    for c in jq_codes:
        code, mkt = c.split(".")
        prefix = "sh" if mkt == "XSHG" else "sz"
        etfs.append({"code": code, "sina_symbol": prefix + code, "name": code, "pool_month": regime})
    return etfs, regime, label


def pick_r3_candidate(etfs: list[dict] | None = None) -> dict | None:
    """对齐聚宽: 趋势/震荡扫动量Top25, 中性扫R3月度宽池; 按腾讯实时涨幅取 ≥ MIN_GAIN(3%) Top1。"""
    if etfs is None:
        etfs, _, _ = r3_pool_for_today()
    if not etfs:
        return None
    quotes = fetch_tencent_quotes([e["code"] for e in etfs])
    ranked = rank_t0_by_today_gain(quotes, etfs)
    for row in ranked:
        if row["today_gain"] < MIN_GAIN:
            continue
        return row
    return None


def is_r3_sell_window(now: datetime | None = None) -> bool:
    """R3 卖出监控窗口 09:40~11:05 (对齐回测 simulate_exit('trix0940_cut'))。"""
    now = now or datetime.now()
    if not is_trading_day(now.date()):
        return False
    hm = now.hour * 60 + now.minute
    return time_to_min(SELL_CHECK_START) <= hm <= time_to_min(SELL_CUTOFF)


def run_pick(dry_run: bool = False) -> int:
    """① 14:40 选股(对齐聚宽 scan_at_1440): 锁定当日 ≥ MIN_GAIN(3%) 领头羊, 不成交。

    仅记录 pending_candidate; 真正成交在 14:45 的 --signal 复核通过后(对齐聚宽A)。
    """
    print("=== [SHADOW R3] 14:40 选股(锁领头羊) ===")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    if not is_trading_day():
        print("非交易日，跳过")
        return 0

    state = load_state()
    today = date.today().isoformat()
    if state.get("last_signal_date") == today:
        print("今日已记录信号，跳过")
        return 0

    etfs, regime, pool_label = r3_pool_for_today()
    print(f">>> R3 regime={regime} pool={pool_label}, 候选 {len(etfs)} 只")

    if SELECT_MODE == "hybrid_top5":
        # 杂交: 14:40 锁 ≥ MIN_GAIN 的 Top5 篮子(按涨幅降序), 14:45 再取幸存者 Top1
        quotes = fetch_tencent_quotes([e["code"] for e in etfs])
        ranked = rank_t0_by_today_gain(quotes, etfs)
        basket = [r for r in ranked if r["today_gain"] >= MIN_GAIN][:5]
        if basket:
            state["pending_basket"] = [
                {"etf": r["code"], "name": r["name"],
                 "pick_gain": r["today_gain"], "pick_price": float(r["price"])}
                for r in basket
            ]
            state["pending_candidate"] = None
            codes_str = ", ".join(f"{r['name']}+{r['today_gain']:+.2f}%" for r in basket)
            print(f">>> [14:40] 锁 Top5 篮子: {codes_str}")
            lines = [
                "### [SHADOW R3] 14:40 选股锁定(Top5篮子)",
                f"- regime={regime} pool={pool_label}",
                f"- 锁定篮子: {codes_str}",
                "> ⚠️ 14:45 将在篮内幸存者(仍 ≥3%)中取涨幅 Top1 成交 (SHADOW 仅记录)",
            ]
            if not dry_run:
                save_state(state)
                send_dingtalk("[SHADOW R3] 14:40锁Top5", "\n".join(lines))
            else:
                print("\n".join(lines))
        else:
            state["pending_basket"] = None
            state["pending_candidate"] = None
            print(f">>> [14:40] 无 ≥{MIN_GAIN:.1f}% 标的 → 空仓")
            if not dry_run:
                save_state(state)
        return 0

    top = pick_r3_candidate(etfs)
    if top:
        state["pending_candidate"] = {
            "etf": top["code"], "name": top["name"],
            "pick_gain": top["today_gain"], "pick_price": float(top["price"]),
            "regime": regime, "pool_label": pool_label,
        }
        print(f">>> [14:40] 锁定领头羊 {top['name']}({top['code']}) "
              f"涨幅{top['today_gain']:+.2f}% @ {float(top['price']):.4f}")
        lines = [
            "### [SHADOW R3] 14:40 选股锁定",
            f"- regime={regime} pool={pool_label}",
            f"- 锁定: **{top['name']}** ({top['code']}) 涨幅 {top['today_gain']:+.2f}%",
            "> ⚠️ 14:45 将复核该标的仍 ≥3% 才成交 (SHADOW 仅记录, 不下单)",
        ]
        if not dry_run:
            save_state(state)
            send_dingtalk(f"[SHADOW R3] 14:40锁定{top['name']}", "\n".join(lines))
        else:
            print("\n".join(lines))
    else:
        state["pending_candidate"] = None
        print(f">>> [14:40] 无 ≥{MIN_GAIN:.1f}% 领头羊 → 今日空仓(不锁候选)")
        if not dry_run:
            save_state(state)
    return 0


def run_signal(dry_run: bool = False) -> int:
    """② 14:45 复核 + 成交(对齐聚宽 prepare_at_1445): 14:40 锁定的领头羊, 14:45 仍 ≥3% 才成交。

    若未跑 --pick(无 pending_candidate, 手动/单跑模式), 退化为"现时刻选股+复核"
    (pick 与 confirm 同刻), 便于手动 --dry-run 测试, 行为与 A 一致(同刻锁+同刻复核)。
    """
    print("=== [SHADOW R3] 14:45 复核 + 买入信号 ===")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    if not is_trading_day():
        print("非交易日，跳过")
        return 0

    state = load_state()
    today = date.today().isoformat()
    if state.get("last_signal_date") == today:
        print("今日已记录信号，跳过")
        return 0

    etfs, regime, pool_label = r3_pool_for_today()
    print(f">>> R3 regime={regime} pool={pool_label}, 候选 {len(etfs)} 只")

    pending = state.get("pending_candidate")
    chosen = None
    if SELECT_MODE == "hybrid_top5" and state.get("pending_basket"):
        # 杂交: 14:45 在 Top5 篮子里筛仍 ≥ MIN_GAIN 的, 取涨幅 Top1
        basket = state["pending_basket"]
        quotes = fetch_tencent_quotes([b["etf"] for b in basket])
        ranked = rank_t0_by_today_gain(quotes, [{"code": b["etf"], "name": b["name"]} for b in basket])
        by_code = {r["code"]: r for r in ranked}
        survivors = []
        for b in basket:
            r = by_code.get(b["etf"])
            gain_now = r["today_gain"] if r else None
            price_now = float(r["price"]) if r else 0
            if gain_now is not None and gain_now >= MIN_GAIN and price_now > 0:
                survivors.append((b, gain_now, price_now))
        if survivors:
            b, gain_now, price_now = max(survivors, key=lambda x: x[1])
            chosen = {"code": b["etf"], "name": b["name"], "price": price_now, "today_gain": gain_now}
            print(f">>> [14:45] 篮子幸存 {len(survivors)}/{len(basket)} 只, "
                  f"选 Top1 {b['name']} (14:40锁 {b['pick_gain']:+.2f}% → 14:45 {gain_now:+.2f}%)")
        else:
            print(f">>> [14:45] 篮子全部跌破 {MIN_GAIN:.1f}% → 空仓")
    elif pending:
        # A 逻辑: 14:40 锁定领头羊, 14:45 复核仍 ≥3% 才成交
        # 注意: 腾讯实时行情原始 dict 只有 change_pct, today_gain 需经 rank_t0_by_today_gain 计算
        quotes = fetch_tencent_quotes([pending["etf"]])
        ranked = rank_t0_by_today_gain(quotes, [{"code": pending["etf"], "name": pending["name"]}])
        r = ranked[0] if ranked else None
        gain_now = r["today_gain"] if r else None
        price_now = float(r["price"]) if r else 0
        if gain_now is not None and gain_now >= MIN_GAIN and price_now > 0:
            chosen = {
                "code": pending["etf"], "name": pending["name"],
                "price": price_now, "today_gain": gain_now,
            }
            _pg = f"{pending['pick_gain']:+.2f}%" if isinstance(pending.get('pick_gain'), (int, float)) else "N/A"
            print(f">>> [14:45] 复核通过 {pending['name']} "
                  f"(14:40锁 {_pg} → 14:45 {gain_now:+.2f}%)")
        else:
            gain_str = f"{gain_now:+.2f}%" if isinstance(gain_now, (int, float)) else "N/A"
            print(f">>> [14:45] 复核否决 {pending['name']}({pending['etf']}) "
                  f"14:45增益={gain_str} → 空仓")
    else:
        # 独立运行兜底(手动/未跑 --pick): 现时刻选股(等同 pick 与 confirm 同刻)
        top = pick_r3_candidate(etfs)
        if top:
            chosen = top
            print(f">>> [现时刻] 直接选股 {chosen['name']} 涨幅{chosen['today_gain']:+.2f}%")

    if not chosen:
        print(f">>> R3 未命中 (regime={regime} pool={pool_label} 无 ≥{MIN_GAIN:.1f}% 标的), 今日空仓")
        state["pending_candidate"] = None
        state["pending_basket"] = None
        state["last_signal_date"] = today
        if not dry_run:
            save_state(state)
        return 0

    price = float(chosen["price"])
    leg = "R3_动量精选" if regime in ("趋势", "震荡") else "R3_月度轮动"
    state["position"] = {
        "etf": chosen["code"], "name": chosen["name"], "type": leg,
        "buy_price": price, "buy_date": today, "today_gain": chosen["today_gain"],
        "signal_time": SIGNAL_TIME, "buy_time": BUY_TIME,
        "sold": False,
    }
    state["last_signal_date"] = today
    state["pending_candidate"] = None
    state["pending_basket"] = None
    print(f">>> R3 命中: {chosen['name']}({chosen['code']}) 涨幅{chosen['today_gain']:+.2f}% @ {price:.4f} [SHADOW]")
    lines = [
        "### [SHADOW R3] 核心腿买入信号触发",
        f"- 策略: R3({regime}/{pool_label}) Top1 (≥{MIN_GAIN}%, drop_sector 已应用)",
        f"- 标的: **{chosen['name']}** ({chosen['code']}) 涨幅 {chosen['today_gain']:+.2f}%",
        f"- 计划买价: {price:.4f} | 卖点: TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD})死叉"
        f", {SELL_CHECK_START}~{SELL_CUTOFF} 内未触发则 {SELL_CUTOFF} 收盘平",
        "> ⚠️ 此为 SHADOW 影子策略，仅记录，不下单。",
    ]
    if not dry_run:
        save_state(state)
        append_journal({
            "signal_date": today, "signal_time": SIGNAL_TIME, "buy_time": BUY_TIME,
            "leg": leg,
            "etf": chosen["code"], "name": chosen["name"], "buy_price": price,
            "today_gain": chosen["today_gain"], "note": "SHADOW-未实盘下单",
        })
        send_dingtalk(f"[SHADOW R3] 信号{chosen['name']}", "\n".join(lines))
        sync_to_web()
    else:
        print("\n".join(lines))
    return 0


def run_sell_check(dry_run: bool = False) -> int:
    print("=== [SHADOW R3] 卖出检查 ===")
    if not is_trading_day():
        print("非交易日，跳过")
        return 0
    state = load_state()
    pos = state.get("position")
    if not pos or pos.get("sold"):
        print("无持仓，跳过")
        return 0
    if pos.get("buy_date") == date.today().isoformat():
        print("买入当日不可按隔夜规则卖出，跳过")
        return 0

    now_hm = datetime.now().strftime("%H:%M")
    if not is_r3_sell_window():
        print(f"非卖出监控时段（{SELL_CHECK_START}~{SELL_CUTOFF}），跳过")
        return 0

    etf = pos["etf"]
    buy_price = float(pos["buy_price"])
    etf_list, _ = scan_etf_universe()
    etf_info = next((e for e in etf_list if e["code"] == etf), None)
    if not etf_info:
        etf_info = next((e for e in get_all_t0_etfs() if e["code"] == etf), None)
    if not etf_info:
        print(f"ERROR: 未知 ETF {etf}")
        return 1
    sym = etf_info["sina_symbol"]
    buy_date = pos["buy_date"]
    today = date.today().isoformat()

    print(f">>> 监控 {pos['name']} ({etf}) 买入@{buy_price:.4f} ({buy_date}) [SHADOW]")
    by_day = fetch_sell_kline(sym)
    time.sleep(SINA_INTERVAL)
    if not by_day:
        print(f"ERROR: 无法获取 {SELL_BAR_LABEL}")
        return 1
    bars_today = by_day.get(today, [])
    if not bars_today:
        if time_to_min(now_hm) < time_to_min(SELL_CUTOFF):
            print(f"WARN: 当日 {SELL_BAR_LABEL} 尚未就绪, 下轮重试")
            return 0
        print(f"ERROR: 当日 {SELL_BAR_LABEL} 为空且已过卖出截止")
        return 1

    q = fetch_tencent_quotes([etf]).get(etf, {})
    cur = q.get("price", 0)
    float_ret = (cur - buy_price) / buy_price * 100 if cur and buy_price else 0

    # TRIX 卖点: TRIX(5,3)死叉; 09:40~11:05 内未触发则 11:05 收盘 fallback 平仓
    # (对齐回测 simulate_exit("trix0940_cut"))
    # 第2返回值 = 死叉当根5min收盘价 = 回测 simulate_trix_cross_after 成交口径(理论价)
    hit_trix, theory_dc_price, trix_time_str, _ = trix_death_cross_hit(
        buy_price, by_day.get(buy_date, []), bars_today, now_hm,
    )
    trix_hm = ""
    if hit_trix:
        trix_hm = trix_time_str.split(" ")[-1][:5] if " " in trix_time_str else trix_time_str[:5]

    if hit_trix:
        sell_hm = trix_hm
        prices = resolve_exec_prices(sym, bars_today, sell_hm, cur)
        sell_price = prices["primary"]
        ret_num = (sell_price - buy_price) / buy_price * 100 - 0.02
        reason = "trix_death_cross"
        theory_price = theory_dc_price
        theory_src = "trix_dc_5m_close"
    elif now_hm >= SELL_CUTOFF:
        # 11:05 收盘 fallback: 窗口内无死叉则收盘平
        sell_hm = SELL_CUTOFF
        prices = resolve_exec_prices(sym, bars_today, sell_hm, cur)
        sell_price = prices["primary"]
        ret_num = (sell_price - buy_price) / buy_price * 100 - 0.02
        reason = f"trix_time_sell_{SELL_CUTOFF.replace(':', '')}"
        # 理论价 = 截止时刻最近已完成5minK收盘(回测 window[-1].close 的实时近似)
        theory_price = prices.get("px_5m") or sell_price
        theory_src = "time_sell_5m_close"
    else:
        print(f"TRIX 未触发, 浮盈 {float_ret:+.2f}% [SHADOW 继续持有]")
        return 0

    ret_num = round(ret_num, 4)
    # 理论价(回测口径) vs 实际价(1min/实时), 量化滑点
    theory_ret = round((theory_price - buy_price) / buy_price * 100 - 0.02, 4)
    slippage_pp = round(ret_num - theory_ret, 4)
    actual_src = prices.get("source", "")
    print(f">>> 卖出触发 @{sell_hm} 实际价 {sell_price:.4f}({actual_src}) 收益 {ret_num:+.2f}% | "
          f"理论价 {theory_price:.4f}({theory_src}) 收益 {theory_ret:+.2f}% | 滑点 {slippage_pp:+.2f}pp [SHADOW]")
    pos["sold"] = True
    pos["sell_date"] = today
    pos["sell_price"] = sell_price
    pos["return_pct"] = ret_num
    pos["sell_reason"] = reason
    pos["theory_price"] = round(theory_price, 4)
    pos["theory_return_pct"] = theory_ret
    pos["actual_price_src"] = actual_src
    pos["slippage_pp"] = slippage_pp
    lines = [
        f"### [SHADOW R3] 卖出信号 | {pos['name']} ({etf})",
        f"- 类型: {pos.get('type')} | 卖点: {reason}",
        f"- 买入: {buy_date} @ {buy_price:.4f} | 卖出: {today} @ {sell_price:.4f}({actual_src})",
        f"- **影子收益: {ret_num:+.2f}%** (未实盘下单)",
        f"- 理论价(回测口径): {theory_price:.4f}({theory_src}) 收益 {theory_ret:+.2f}% | 滑点 {slippage_pp:+.2f}pp",
    ]
    if not dry_run:
        state["position"] = None
        save_state(state)
        append_journal({
            # ★ R3 把买卖合并在一条 sell 记录, 显式写 signal_date/buy_date/today_gain
            #   (WebUI r3_shadow.py loader 用 signal_date 做近N天 cutoff + 合并 key,
            #   today_gain 作信号涨幅; 缺字段的旧记录无法被统计/对比)
            "signal_date": pos.get("buy_date", ""), "sell_date": today, "sell_time": sell_hm,
            "leg": pos.get("type"),
            "signal_time": pos.get("signal_time", ""), "buy_date": pos.get("buy_date", ""),
            "buy_time": pos.get("buy_time", ""), "today_gain": pos.get("today_gain", ""),
            "etf": etf, "name": pos["name"], "buy_price": buy_price,
            "sell_price": sell_price, "return_pct": ret_num, "sell_reason": reason,
            "theory_price": round(theory_price, 4), "theory_return_pct": theory_ret,
            "actual_price_src": actual_src, "slippage_pp": slippage_pp,
            "note": "SHADOW-未实盘平仓",
        })
        send_dingtalk(f"[SHADOW R3] 卖出{pos['name']} {ret_num:+.2f}%", "\n".join(lines))
        sync_to_web()
    else:
        print("\n".join(lines))
    return 0


def run_sell_loop(dry_run: bool = False) -> int:
    """09:40~11:05 每50秒循环跑 run_sell_check (TRIX 卖点监控, 对齐回测)。
    持仓平仓后提前退出; 窗口结束自然退出。"""
    print(f"=== [SHADOW R3] 卖出监控循环 | 每 {SELL_LOOP_INTERVAL}s | "
          f"窗口 {SELL_CHECK_START}~{SELL_CUTOFF} ===")
    while is_r3_sell_window():
        run_sell_check(dry_run=dry_run)
        state = load_state()
        if not state.get("position"):
            print("持仓已平, 退出循环")
            return 0
        if not is_r3_sell_window():
            break
        time.sleep(SELL_LOOP_INTERVAL)
    print("=== 卖出监控循环结束 ===")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="R3 月度轮动 SHADOW 监控 (不替代实盘)")
    ap.add_argument("--pick", action="store_true",
                    help="14:40 R3 选股锁定领头羊(对齐聚宽A: 14:40选)")
    ap.add_argument("--signal", action="store_true",
                    help="14:45 R3 复核+成交(对齐聚宽A: 14:45复核)")
    ap.add_argument("--sell-check", action="store_true", help="卖出检查(单次)")
    ap.add_argument("--sell-loop", action="store_true",
                    help="09:40~11:05 每50秒循环检查(TRIX 卖点监控)")
    ap.add_argument("--dry-run", action="store_true", help="仅打印不推送")
    args = ap.parse_args()

    if args.pick:
        raise SystemExit(run_pick(dry_run=args.dry_run))
    if args.signal:
        raise SystemExit(run_signal(dry_run=args.dry_run))
    if args.sell_loop:
        raise SystemExit(run_sell_loop(dry_run=args.dry_run))
    if args.sell_check:
        raise SystemExit(run_sell_check(dry_run=args.dry_run))
    ap.print_help()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""T+0 ETF 当日涨幅动量监控 — 14:45 信号 / 14:50 买入 / 次日 5分K TRIX(5,3) 卖。

策略（9 日 1 分 K 回测验证，无未来函数；5 分 TRIX(5,3) 累计 +36.31% 最优）：
- 501018 近10日 MA20 穿越≥2 → 震荡期跳过买入
- 14:45 选 T+0 池当日涨幅最大且 ≥3% 的 ETF → 14:50 买入
- 次日 09:40~11:05 每 50 秒检查 5 分钟 TRIX(5,3) 死叉 → 卖出；无死叉则 11:05 定时卖

用法:
    python scripts/t0_monitor.py --signal          # 14:45 发买入信号
    python scripts/t0_monitor.py --sell-check      # 次日 TRIX 卖出检查
    python scripts/t0_sell_watch.py                # 09:40~11:05 每 50 秒循环检查
    python scripts/t0_monitor.py --dry-run --signal
    python scripts/t0_monitor.py --test-push
    python scripts/t0_monitor.py --trail-log          # 查看 1 分 K 追踪 shadow 日志
    python scripts/t0_monitor.py --trail-log --days 3

Shadow 日志（实盘卖出仍仅 TRIX/定时，追踪只记录不执行）:
    ~/.tradingagents/rotation/t0_trail_shadow.jsonl

定时（install_crontab.sh）:
    09:40  t0_sell_watch.py（窗口内每 50 秒 --sell-check）
    14:45  --signal
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from backtest_top1 import fetch_sina_kline  # noqa: E402
from backtest_top1_minute import calc_trix, calc_trix_signal  # noqa: E402
from backtest_t0_etf import fetch_5min_kline, normalize_5min_bars, price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    MIN_GAIN,
    TRIX_MIN_SELL,
    TRIX_PERIOD,
    bars_for_trix,
    time_to_min,
    bar_clock,
)
from rotation_monitor import fetch_tencent_quotes, send_dingtalk  # noqa: E402
from t0_etf_list import get_all_t0_etfs, get_quality_etfs  # noqa: E402
from t0_regime import CHOPPY_MA_CROSS, REGIME_PROXY, detect_regime, format_regime_block  # noqa: E402

try:
    from tradingagents.dataflows.instrument import settlement_rule
except ImportError:
    def settlement_rule(code: str, name: str | None = None) -> str:  # type: ignore[misc]
        return "T0"

try:
    from tradingagents.intraday.calendar import is_trading_day
except ImportError:
    def is_trading_day(day: date | None = None) -> bool:  # type: ignore[misc]
        day = day or date.today()
        return day.weekday() < 5

SINA_INTERVAL = 0.25
STATE_DIR = Path.home() / ".tradingagents" / "rotation"
STATE_FILE = STATE_DIR / "t0_monitor_state.json"
TRAIL_SHADOW_LOG = STATE_DIR / "t0_trail_shadow.jsonl"
TRADE_JOURNAL = STATE_DIR / "t0_trade_journal.jsonl"

# 1 分 K 追踪 shadow（与 backtest_t0_hybrid_1min 一致；仅日志，不改实盘卖点）
TRAIL_DROP_PCT = 0.5
TRAIL_SHADOW_VERSION = "trail_shadow_1m_0.5pct_20260716"

SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
TRIX_SIGNAL_PERIOD = 3
SELL_BAR_LABEL = "5分K"
SELL_CUTOFF = "11:05"
# 与 backtest_t0_sell_trix_compare.py 最优方案一致；卖点逻辑与 simulate_trix_cross_after 对齐
STRATEGY_VERSION = "t0_hybrid_quality_orig_20260722"
FEE_NOTE = "手续费: 万3双边"
REGIME_RULE = (
    f"501018近10日MA20穿越≥{CHOPPY_MA_CROSS}=震荡；"
    f"震荡/趋势→优质池仍交易，中性→原T0池"
)
BUY_RULE = f"{SIGNAL_TIME} 混合池涨幅≥{MIN_GAIN:.0f}% TOP1 → {BUY_TIME} 买入"
HYBRID_POOL_RULE = "趋势/震荡→优质池(regime品类过滤)；中性→原T0池"
SELL_RULE = (
    f"次日 {SELL_BAR_LABEL} TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) 死叉"
    f"(≥{TRIX_MIN_SELL}, ≤{SELL_CUTOFF}) / 无死叉 {SELL_CUTOFF} 定时卖"
)

SELL_CHECK_START = "09:40"   # 与 TRIX_MIN_SELL 一致
SELL_CHECK_END = SELL_CUTOFF


def fetch_sell_kline(sina_symbol: str, datalen: int = 2500) -> dict[str, list[dict]]:
    """新浪 5 分 K，按交易日分组（与回测 trix0940_cut 卖点一致）。"""
    try:
        klines = fetch_5min_kline(sina_symbol, datalen=datalen)
        if not klines:
            return {}
        return normalize_5min_bars(klines)
    except Exception as e:
        print(f"ERROR: {SELL_BAR_LABEL}获取失败 {sina_symbol}: {e}")
        return {}


def is_signal_window(now: datetime | None = None) -> bool:
    """正式信号窗口：14:45~14:55（与 crontab 14:45 及 14:50 买入一致）。"""
    now = now or datetime.now()
    hm = now.hour * 60 + now.minute
    return time_to_min(SIGNAL_TIME) <= hm <= time_to_min(BUY_TIME) + 5


def is_sell_check_window(now: datetime | None = None) -> bool:
    """卖出检查有效时段（交易日 09:40~11:05）。"""
    now = now or datetime.now()
    if not is_trading_day(now.date()):
        return False
    hm = now.hour * 60 + now.minute
    return time_to_min(SELL_CHECK_START) <= hm <= time_to_min(SELL_CHECK_END)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.setdefault("strategy", {})
    state["strategy"].update({
        "version": STRATEGY_VERSION,
        "signal": SIGNAL_TIME,
        "buy": BUY_TIME,
        "sell": f"{SELL_BAR_LABEL} TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD})",
        "sell_window": f"{TRIX_MIN_SELL}~{SELL_CUTOFF}",
    })
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_regime() -> dict | None:
    """拉 501018 日 K 并识别震荡/中性/趋势。"""
    sym = f"sh{REGIME_PROXY}"
    klines = fetch_sina_kline(sym, datalen=60)
    if not klines:
        return None
    return detect_regime(klines, date.today().isoformat())


def strategy_header_lines(*, hybrid: bool = False) -> list[str]:
    lines = [
        f"**买入**: {BUY_RULE}",
        f"**卖出**: {SELL_RULE}",
        f"**过滤**: {REGIME_RULE}",
    ]
    if hybrid:
        lines.append(f"**选池**: {HYBRID_POOL_RULE}")
    lines.extend([f"**{FEE_NOTE}**", ""])
    return lines


def format_top5_lines(ranked: list[dict], highlight_code: str | None = None) -> list[str]:
    lines = ["**TOP5 涨幅**:"]
    for i, r in enumerate(ranked[:5], 1):
        tag = ""
        if highlight_code and r["code"] == highlight_code:
            tag = " ← TOP1"
        elif i == 1 and not highlight_code:
            tag = " ← 最高"
        settle = settlement_rule(r["code"], r.get("name"))
        st = "T+0" if settle == "T0" else "T+1"
        lines.append(f"{i}. {r['name']} {r['code']} {r['today_gain']:+.2f}% [{st}]{tag}")
    return lines


def scan_etf_universe() -> tuple[list[dict], str]:
    """返回 (扫描名单, 选股模式)。

    hybrid: 趋势/震荡→优质池，中性→原 T+0 池（需 quality_pool.json）
    quality: 仅 v2 规则过滤（旧模式，保留兼容）
    t0_only: 原 T+0 池 + 仅 T+0 交割
    """
    from quality_pool import get_scan_universe, has_quality_rules  # noqa: PLC0415

    if has_quality_rules():
        uni = get_scan_universe()
        if uni:
            return uni, "hybrid"
    return get_all_t0_etfs(), "t0_only"


def rank_t0_by_today_gain(quotes: dict[str, dict], etf_list: list[dict] | None = None) -> list[dict]:
    """按腾讯实时涨跌幅排名（≈ 14:50 相对昨收当日涨幅）。"""
    universe = etf_list if etf_list is not None else get_all_t0_etfs()
    rows: list[dict] = []
    for etf in universe:
        code = etf["code"]
        q = quotes.get(code)
        if not q:
            continue
        price = q.get("price", 0)
        last_close = q.get("last_close", 0)
        if not price or not last_close:
            continue
        gain = q.get("change_pct")
        if gain is None:
            gain = (price - last_close) / last_close * 100
        live_name = (q.get("name") or "").strip()
        rows.append({
            **etf,
            "name": live_name or etf["name"],
            "etf_name": live_name or etf["name"],
            "price": price,
            "last_close": last_close,
            "today_gain": round(float(gain), 2),
            "quote_time": q.get("quote_time", ""),
        })
    rows.sort(key=lambda x: x["today_gain"], reverse=True)
    return rows


def pick_signal_candidate(ranked: list[dict], *, t0_only: bool = True) -> dict | None:
    for row in ranked:
        if row["today_gain"] < MIN_GAIN:
            continue
        if t0_only and settlement_rule(row["code"], row.get("name")) != "T0":
            continue
        return row
    return None


def price_1min_at_or_before(bars_1m: list[dict], hm: str) -> tuple[float | None, str]:
    """取 ≤ hm 的最后一根 1 分 K 收盘价。"""
    if not bars_1m:
        return None, ""
    target = time_to_min(hm[:5])
    best_px: float | None = None
    best_tm = -1
    for b in bars_1m:
        t = _bar_time_1m(b)
        bt = time_to_min(t)
        if bt > target:
            continue
        if bt >= best_tm:
            best_tm = bt
            best_px = float(b["close"])
    if best_px is None:
        return None, ""
    return best_px, f"{best_tm // 60:02d}:{best_tm % 60:02d}"


def resolve_exec_prices(
    sina_symbol: str,
    bars_5m_today: list[dict],
    hm: str,
    live_price: float = 0,
) -> dict:
    """成交价优先 1 分 K，其次实时价，最后 5 分 K close（与 realistic 回测一致）。"""
    bars_1m = fetch_1min_today(sina_symbol)
    px_1m, tm_1m = price_1min_at_or_before(bars_1m, hm)
    px_5m = price_at_time(bars_5m_today, hm[:5]) if bars_5m_today else None
    px_live = float(live_price) if live_price and live_price > 0 else None
    primary = px_1m or px_live or px_5m or 0.0
    return {
        "primary": primary,
        "px_1m": px_1m,
        "tm_1m": tm_1m,
        "px_5m": px_5m,
        "px_live": px_live,
        "source": "1min" if px_1m else ("live" if px_live else "5min"),
    }


def format_exec_price_lines(prices: dict, buy_price: float) -> list[str]:
    """钉钉正文：主成交价 + 分来源对照。"""
    primary = prices["primary"]
    ret = (primary - buy_price) / buy_price * 100 if primary and buy_price else 0.0
    lines = [
        f"- 卖出参考价: **{primary:.4f}**（来源: {prices['source']}）",
        f"- 预估收益: **{ret:+.2f}%**（{FEE_NOTE}）",
    ]
    refs = []
    if prices.get("px_1m"):
        refs.append(f"1分K {prices['tm_1m']}={prices['px_1m']:.4f}")
    if prices.get("px_5m"):
        refs.append(f"5分K close={prices['px_5m']:.4f}")
    if prices.get("px_live"):
        refs.append(f"实时={prices['px_live']:.4f}")
    if refs:
        lines.append(f"- 价格对照: {' | '.join(refs)}")
    return lines, ret


def fetch_1min_today(sina_symbol: str) -> list[dict]:
    """拉当日 1 分 K（新浪，用于 shadow 峰值追踪）。"""
    try:
        from backtest_t0_1min import fetch_1min_kline_sina  # noqa: E402

        by_day = fetch_1min_kline_sina(sina_symbol)
        return by_day.get(date.today().isoformat(), [])
    except Exception as e:
        print(f"WARN: 1分K shadow 拉取失败 {sina_symbol}: {e}")
        return []


def _bar_time_1m(bar: dict) -> str:
    if " " in bar.get("day", ""):
        return bar["day"].split(" ")[1][:5]
    return bar.get("time", "00:00")[:5]


def peak_from_1min(bars_1m: list[dict], buy_price: float, since: str = TRIX_MIN_SELL) -> float:
    """09:40 起 1 分 K 最高价作为 running peak。"""
    since_m = time_to_min(since)
    peak = buy_price
    for b in bars_1m:
        if time_to_min(_bar_time_1m(b)) < since_m:
            continue
        peak = max(peak, float(b.get("high", 0) or 0))
    return peak


def trail_shadow_would_sell(
    buy_price: float,
    peak: float,
    cur_price: float,
    trail_drop_pct: float = TRAIL_DROP_PCT,
) -> tuple[bool, float]:
    """1 分 K 回测同逻辑：peak>买入价 且 现价≤peak×(1-drop%)。"""
    if peak <= buy_price or cur_price <= 0:
        return False, 0.0
    trigger = peak * (1 - trail_drop_pct / 100)
    if cur_price <= trigger:
        return True, trigger
    return False, trigger


def append_trail_shadow_log(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry.setdefault("shadow_version", TRAIL_SHADOW_VERSION)
    with TRAIL_SHADOW_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_trade_journal(entry: dict) -> None:
    """追加 T+0 实盘交易流水（供 Web 表格展示）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry.setdefault("strategy_version", STRATEGY_VERSION)
    with TRADE_JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_trade_closed(
    pos: dict,
    *,
    sell_date: str,
    sell_time: str,
    sell_reason: str,
    sell_price: float,
    return_pct: float,
    float_pct: float | None = None,
) -> None:
    append_trade_journal({
        "event": "trade_closed",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "etf": pos.get("etf"),
        "name": pos.get("name"),
        "type": pos.get("type", ""),
        "buy_date": pos.get("buy_date"),
        "buy_time": BUY_TIME,
        "buy_price": pos.get("buy_price"),
        "signal_gain_pct": pos.get("today_gain"),
        "sell_date": sell_date,
        "sell_time": sell_time,
        "sell_reason": sell_reason,
        "sell_price": round(sell_price, 4),
        "float_pct": round(float_pct if float_pct is not None else return_pct, 3),
        "return_pct": round(return_pct, 3),
    })


def run_trail_shadow_check(
    pos: dict,
    sym: str,
    buy_price: float,
    bars_buy_day: list[dict],
    bars_today: list[dict],
    now_hm: str,
    cur_price: float,
) -> dict:
    """记录 1 分 K 追踪 vs TRIX 对比（不触发实盘卖出）。"""
    bars_1m = fetch_1min_today(sym)
    time.sleep(SINA_INTERVAL)
    peak_1m = peak_from_1min(bars_1m, buy_price) if bars_1m else buy_price
    if cur_price > 0:
        peak_1m = max(peak_1m, cur_price)

    trail_hit, trail_trigger = trail_shadow_would_sell(buy_price, peak_1m, cur_price)
    trix_hit, trix_price, trix_time, trix_ret = trix_death_cross_hit(
        buy_price, bars_buy_day, bars_today, now_hm,
    )

    float_pct = (cur_price - buy_price) / buy_price * 100 if cur_price and buy_price else 0.0
    trix_ret_num = None
    if trix_hit and trix_price:
        trix_ret_num = (trix_price - buy_price) / buy_price * 100

    hybrid_first = None
    if trail_hit and trix_hit:
        hybrid_first = "trail"  # 同检查点两者皆真时，实盘 hybrid 会优先追踪
    elif trail_hit:
        hybrid_first = "trail"
    elif trix_hit:
        hybrid_first = "trix"

    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "check_time": now_hm,
        "etf": pos.get("etf"),
        "name": pos.get("name"),
        "buy_date": pos.get("buy_date"),
        "buy_price": round(buy_price, 4),
        "price": round(cur_price, 4) if cur_price else None,
        "float_pct": round(float_pct, 3),
        "peak_1m": round(peak_1m, 4),
        "trail_drop_pct": TRAIL_DROP_PCT,
        "trail_trigger": round(trail_trigger, 4),
        "trail_would_sell": trail_hit,
        "trix_would_sell": trix_hit,
        "trix_sell_time": trix_time if trix_hit else None,
        "trix_sell_price": round(trix_price, 4) if trix_hit else None,
        "trix_return_pct": round(trix_ret_num, 3) if trix_ret_num is not None else None,
        "hybrid_first": hybrid_first,
        "live_action": "trix_only",  # 实盘仍仅 TRIX/定时
        "bars_1m": len(bars_1m),
    }
    append_trail_shadow_log(entry)

    if trail_hit and not trix_hit:
        print(
            f"  [shadow] 1分追踪 Would SELL @ {trail_trigger:.4f} "
            f"(peak {peak_1m:.4f}, 现 {cur_price:.4f}) — 实盘仍等 TRIX"
        )
    elif trail_hit and trix_hit:
        print(
            f"  [shadow] 追踪+TRIX 同时触发 | 追踪@{trail_trigger:.4f} TRIX@{trix_price:.4f} {trix_ret}"
        )
    else:
        print(
            f"  [shadow] peak_1m={peak_1m:.4f} trail触发价={trail_trigger:.4f} "
            f"TRIX={'死叉' if trix_hit else '否'}"
        )
    return entry


def print_trail_shadow_log(days: int = 7) -> int:
    """打印最近 shadow 日志摘要。"""
    if not TRAIL_SHADOW_LOG.exists():
        print(f"暂无 shadow 日志: {TRAIL_SHADOW_LOG}")
        return 0

    lines = TRAIL_SHADOW_LOG.read_text(encoding="utf-8").strip().splitlines()
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not entries:
        print("日志为空")
        return 0

    cutoff = date.today().toordinal() - days + 1
    recent = [
        e for e in entries
        if datetime.fromisoformat(e["ts"]).date().toordinal() >= cutoff
    ]
    if not recent:
        recent = entries[-50:]

    print(f"=== T+0 1分K追踪 Shadow 日志 ===")
    print(f"文件: {TRAIL_SHADOW_LOG}")
    print(f"版本: {TRAIL_SHADOW_VERSION} | 回落 {TRAIL_DROP_PCT}% | 显示近 {days} 天 ({len(recent)} 条)\n")
    print(f"  {'时间':>16} {'ETF':>8} {'现价':>7} {'peak':>7} {'浮盈':>7} {'追踪':>4} {'TRIX':>4} {'hybrid':>6}")
    print("  " + "-" * 72)
    for e in recent[-40:]:
        ts = e.get("ts", "")[5:16].replace("T", " ")
        print(
            f"  {ts:>16} {e.get('etf', ''):>8} "
            f"{e.get('price') or 0:7.4f} {e.get('peak_1m') or 0:7.4f} "
            f"{e.get('float_pct') or 0:+6.2f}% "
            f"{'Y' if e.get('trail_would_sell') else 'N':>4} "
            f"{'Y' if e.get('trix_would_sell') else 'N':>4} "
            f"{e.get('hybrid_first') or '-':>6}"
        )

    # 按 sell day 汇总（每个 buy_date+etf 取最后一次检查）
    by_key: dict[str, dict] = {}
    for e in recent:
        key = f"{e.get('buy_date')}_{e.get('etf')}"
        by_key[key] = e
    summaries = list(by_key.values())
    trail_only = sum(1 for e in summaries if e.get("trail_would_sell") and not e.get("trix_would_sell"))
    both = sum(1 for e in summaries if e.get("trail_would_sell") and e.get("trix_would_sell"))
    print(f"\n  汇总({len(summaries)} 个持仓日): 仅追踪会先卖 {trail_only} | 两者同时 {both}")
    print(f"  实盘卖点未改，仍为 TRIX/11:05 定时")
    return 0


def trix_death_cross_hit(
    buy_price: float,
    bars_yesterday: list[dict],
    bars_today: list[dict],
    cutoff_time: str,
) -> tuple[bool, float, str, str]:
    """检查截至 cutoff_time 是否已触发 TRIX 死叉（仅 09:40 后有效）。"""
    cutoff_min = time_to_min(cutoff_time)
    today_cut = []
    for b in bars_today:
        t = b.get("time", "")[:5]
        if t and time_to_min(t) <= cutoff_min:
            today_cut.append(b)
    if not today_cut:
        return False, 0.0, "", ""

    all_bars = bars_for_trix(bars_yesterday) + bars_for_trix(today_cut)
    min_warmup = TRIX_PERIOD * 3 + 5
    if len(all_bars) < min_warmup:
        return False, 0.0, "", ""

    warmup_len = len(bars_for_trix(bars_yesterday))
    closes = [float(b["close"]) for b in all_bars]
    trix = calc_trix(closes, TRIX_PERIOD)
    signal = calc_trix_signal(trix, TRIX_SIGNAL_PERIOD)
    min_sell_min = time_to_min(TRIX_MIN_SELL)
    search_start = max(warmup_len, min_warmup)

    for i in range(search_start, len(all_bars)):
        bar_t = bar_clock(all_bars[i])
        if time_to_min(bar_t) < min_sell_min:
            continue
        if trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
            sell_price = closes[i]
            ret = (sell_price - buy_price) / buy_price * 100
            return True, sell_price, bar_t, f"{ret:+.2f}%"

    return False, 0.0, "", ""


def run_signal(dry_run: bool = False) -> int:
    print("=== T+0 ETF 动量监控 | 买入信号 ===")
    print(f"规则: {BUY_RULE} | {REGIME_RULE}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    if not is_trading_day():
        print("非交易日，跳过")
        return 0

    print(f">>> 拉取 {REGIME_PROXY} 日K 识别市场环境...")
    regime = fetch_regime()
    if regime:
        print(f"    环境: {regime['mode']} | 穿越{regime['ma_crosses']} | ADX {regime['adx']}")
    else:
        print("    环境: 数据不足")

    etf_list, pick_mode = scan_etf_universe()
    codes = [e["code"] for e in etf_list]
    pool_label = {"hybrid": "混合(原T0+优质)", "quality": "优质池", "t0_only": "T+0 ETF"}.get(
        pick_mode, "T+0 ETF",
    )
    print(f">>> 拉取 {len(codes)} 只 {pool_label} 实时行情...")
    quotes = fetch_tencent_quotes(codes)
    if not quotes:
        print("ERROR: 无法获取实时行情")
        return 1

    ranked = rank_t0_by_today_gain(quotes, etf_list)
    if len(ranked) < 2:
        print("ERROR: 有效 ETF 不足")
        return 1

    pool_tag = ""
    if pick_mode == "hybrid":
        from quality_pool import load_quality_pool, pick_hybrid_from_ranked  # noqa: PLC0415

        top, ranked_view, pool_tag = pick_hybrid_from_ranked(
            ranked, regime,
            orig_pool=get_all_t0_etfs(),
            quality_pool=load_quality_pool(),
        )
    elif pick_mode == "t0_only":
        top = pick_signal_candidate(ranked, t0_only=True)
        ranked_view = ranked
    else:
        from quality_pool import pick_from_ranked_live  # noqa: PLC0415

        top = pick_from_ranked_live(ranked, regime, use_regime_filter=True, t0_only=False)
        ranked_view = ranked

    state = load_state()
    pos = state.get("position")

    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"### T+0 ETF 买入信号 | {run_ts}",
        "",
        *strategy_header_lines(hybrid=(pick_mode == "hybrid")),
        f"**扫描**: {len(ranked)} 只有效行情 / {len(codes)} 只 {pool_label}",
    ]
    if pool_tag:
        lines.append(f"**选池分支**: {pool_tag}")
    lines.extend(["", *format_regime_block(regime, hybrid=(pick_mode == "hybrid"))])

    if pos and not pos.get("sold"):
        lines.extend([
            f"⚠️ **持仓提醒**: 仍持有 {pos.get('name')} ({pos.get('etf')})",
            f"   买入日 {pos.get('buy_date')} @ {pos.get('buy_price')} — 请先处理卖出",
            "",
        ])
        print(f"⚠️  仍有持仓: {pos.get('name')} ({pos.get('etf')})")

    if pick_mode == "hybrid":
        from quality_pool import hybrid_should_skip_choppy  # noqa: PLC0415

        skip_choppy = hybrid_should_skip_choppy(regime, hybrid=True)
    else:
        skip_choppy = bool(regime and regime.get("skip_choppy"))

    if skip_choppy:
        best = ranked[0]
        hypo = top or best
        print(f"震荡期跳过 | 若交易候选: {hypo['name']} {hypo['today_gain']:+.2f}%")
        lines.extend([
            "**信号**: ⛔ **震荡期跳过买入**",
            f"- 原因: 501018 近10日 MA20 穿越 {regime['ma_crosses']}次 ≥ {CHOPPY_MA_CROSS}",
            "",
            "**参考候选**（未执行）:",
            f"- {hypo['name']} ({hypo['code']}) 当日 {hypo['today_gain']:+.2f}%",
            f"- 现价 {hypo['price']:.4f}（昨收 {hypo['last_close']:.4f}）",
            "",
            *format_top5_lines(ranked_view, hypo["code"]),
        ])
        title = "T0轮动 震荡跳过"
        state["last_signal"] = {
            "timestamp": datetime.now().isoformat(),
            "skipped": True,
            "reason": "choppy",
            "regime": regime,
            "hypo_etf": hypo["code"],
            "hypo_gain": hypo["today_gain"],
        }
        save_state(state)
    elif not top:
        best = ranked_view[0] if ranked_view else ranked[0]
        if pick_mode == "hybrid":
            msg = f"今日无混合信号（{pool_tag or '混合池'} 最高 {best['name']} {best['today_gain']:+.2f}% < {MIN_GAIN:.0f}%）"
            reason_line = "**信号**: 无（涨幅或 regime 品类过滤未通过）"
        else:
            t0_ranked = [r for r in ranked if settlement_rule(r["code"], r.get("name")) == "T0"]
            hypo = t0_ranked[0] if t0_ranked else best
            if t0_ranked and hypo["today_gain"] < MIN_GAIN:
                msg = f"今日无 T+0 信号（T0最高 {hypo['name']} {hypo['today_gain']:+.2f}% < {MIN_GAIN:.0f}%）"
            elif not t0_ranked:
                msg = f"今日 T+0 池无有效标的（涨幅最高 {best['name']} 为 T+1）"
            else:
                msg = f"今日无有效信号（最高 {best['name']} {best['today_gain']:+.2f}% < {MIN_GAIN:.0f}%）"
            reason_line = "**信号**: 无（涨幅过滤 / T+0 交割过滤未通过）"
        print(msg)
        lines.extend([
            reason_line,
            f"- 分支最高: {best['name']} {best['code']} {best['today_gain']:+.2f}%",
        ])
        if pick_mode != "hybrid":
            t0_ranked = [r for r in ranked if settlement_rule(r["code"], r.get("name")) == "T0"]
            if t0_ranked:
                hypo = t0_ranked[0]
                lines.append(f"- T+0最高: {hypo['name']} {hypo['code']} {hypo['today_gain']:+.2f}%")
        lines.extend(["", *format_top5_lines(ranked_view)])
        title = "T0轮动 无买入信号"
        state["last_signal"] = {
            "timestamp": datetime.now().isoformat(),
            "skipped": True,
            "reason": "min_gain",
            "etf": best["code"],
            "today_gain": best["today_gain"],
        }
        save_state(state)
    else:
        settle = settlement_rule(top["code"], top.get("name"))
        st_label = "T+0" if settle == "T0" else "T+1"
        print(f"TOP1: {top['name']} ({top['code']}) 当日{top['today_gain']:+.2f}% [{st_label}]")
        if pool_tag:
            print(f"      选池: {pool_tag}")
        print(f"      现价 {top['price']:.4f} → 建议 {BUY_TIME} 买入")
        lines.extend([
            f"**信号**: 买入 **{top['name']}** ({top['code']})",
            f"- 交割: **{st_label}**",
            f"- 当日涨幅: **{top['today_gain']:+.2f}%**",
            f"- 现价: {top['price']:.4f}（昨收 {top['last_close']:.4f}）",
            f"- 操作: **{BUY_TIME} 买入**",
            f"- 类型: {top.get('type_name', '')}",
        ])
        if pool_tag:
            lines.append(f"- 选池: {pool_tag}")
        lines.extend([
            "",
            *format_top5_lines(ranked_view, top["code"]),
        ])
        for i, r in enumerate(ranked_view[:5], 1):
            tag = " ← TOP1" if r["code"] == top["code"] else ""
            print(f"  {i}. {r['name']:14s} {r['code']} {r['today_gain']:+.2f}%{tag}")

        title = f"T0轮动 买入{top['name']}"
        in_window = is_signal_window()
        if not in_window:
            print(f"WARN: 当前未到正式信号窗口（{SIGNAL_TIME}~{BUY_TIME}），仅记录候选，不写入持仓")
        if in_window and not (pos and not pos.get("sold")):
            state["position"] = {
                "etf": top["code"],
                "name": top["name"],
                "type": top.get("type_name", ""),
                "buy_date": date.today().isoformat(),
                "buy_price": top["price"],
                "today_gain": top["today_gain"],
                "sold": False,
            }
        state["last_signal"] = {
            "timestamp": datetime.now().isoformat(),
            "skipped": False,
            "pending_buy": not in_window,
            "etf": top["code"],
            "name": top["name"],
            "today_gain": top["today_gain"],
            "price": top["price"],
            "regime": regime,
            "pool_tag": pool_tag,
            "pick_mode": pick_mode,
        }
        save_state(state)

    if dry_run:
        print("\n>>> --dry-run，跳过推送")
        return 0

    webhook = (os.getenv("DINGTALK_ROTATION_WEBHOOK") or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    if not webhook:
        print("\n>>> 钉钉未配置，跳过推送")
        return 0

    print("\n>>> 推送钉钉...")
    ok = send_dingtalk(title, "\n".join(lines))
    print(f"    {'成功' if ok else '失败'}")
    return 0 if ok else 1


def run_sell_check(dry_run: bool = False) -> int:
    print("=== T+0 ETF 动量监控 | TRIX 卖出检查 ===")
    print(f"规则: {SELL_RULE}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    if not is_trading_day():
        print("非交易日，跳过")
        return 0

    if not is_sell_check_window():
        print(f"非卖出检查时段（{SELL_CHECK_START}~{SELL_CHECK_END}），跳过")
        return 0

    state = load_state()
    pos = state.get("position")
    if not pos or pos.get("sold"):
        print("无持仓，跳过")
        return 0

    if pos.get("buy_date") == date.today().isoformat():
        print("买入当日不可按隔夜规则卖出，跳过")
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
    now_hm = datetime.now().strftime("%H:%M")

    print(f">>> 拉取 {REGIME_PROXY} 日K 识别市场环境...")
    regime = fetch_regime()

    print(f">>> 监控 {pos['name']} ({etf}) 买入@{buy_price:.4f} ({buy_date})")
    by_day = fetch_sell_kline(sym)
    time.sleep(SINA_INTERVAL)
    if not by_day:
        print(f"ERROR: 无法获取 {SELL_BAR_LABEL}")
        return 1

    bars_buy_day = by_day.get(buy_date, [])
    bars_today = by_day.get(today, [])
    if not bars_today:
        if time_to_min(now_hm) < time_to_min(SELL_CUTOFF):
            print(f"WARN: 当日 {SELL_BAR_LABEL} 尚未就绪（{etf}），下轮重试")
            return 0
        print(f"ERROR: 当日 {SELL_BAR_LABEL} 为空且已过卖出截止")
        return 1

    q = fetch_tencent_quotes([etf]).get(etf, {})
    cur = q.get("price", 0)
    float_ret = (cur - buy_price) / buy_price * 100 if cur and buy_price else 0

    run_trail_shadow_check(
        pos, sym, buy_price, bars_buy_day, bars_today, now_hm, float(cur or 0),
    )

    hit, _, sell_time, _ = trix_death_cross_hit(
        buy_price, bars_buy_day, bars_today, now_hm,
    )

    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    from quality_pool import has_quality_rules  # noqa: PLC0415

    hybrid_live = has_quality_rules()
    header = [
        f"### T+0 ETF 卖出检查 | {run_ts}",
        "",
        *strategy_header_lines(hybrid=hybrid_live),
        f"**检查时点**: {now_hm} | {SELL_BAR_LABEL} TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) "
        f"有效窗口 {TRIX_MIN_SELL}~{SELL_CUTOFF}",
        "",
        *format_regime_block(regime, hybrid=hybrid_live),
        f"**持仓**: {pos['name']} ({etf}) | 类型 {pos.get('type', '')}",
        f"- 买入: {buy_date} @ {buy_price:.4f}（信号日涨幅 {pos.get('today_gain', '—')}%）",
        f"- 现价: {cur:.4f} | 浮盈 **{float_ret:+.2f}%**",
        "",
    ]

    if hit:
        sell_hm = sell_time.split(" ")[-1][:5] if " " in sell_time else sell_time[:5]
        prices = resolve_exec_prices(sym, bars_today, sell_hm, cur)
        sell_price = prices["primary"]
        price_lines, ret_num = format_exec_price_lines(prices, buy_price)
        ret_str = f"{ret_num:+.2f}%"
        print(f"TRIX 死叉触发 @ {sell_time} 价 {sell_price:.4f} ({prices['source']}) 收益 {ret_str}")
        title = f"T0 ⚠️ TRIX死叉卖出{pos['name']}"
        body = [
            *header,
            f"## ⚠️ {SELL_BAR_LABEL} TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) 死叉 — 请立即卖出",
            "",
            f"- 死叉时点: **{sell_time}**（5分K信号）",
            *price_lines,
        ]
        if sell_time and sell_time != now_hm:
            body.append(f"- 检测时间: {now_hm}（死叉发生于 {sell_time}）")
        body.extend([
            f"- 操作: **尽快按市价或限价卖出 {pos['name']} ({etf})**",
            f"- 规则: {SELL_RULE}",
        ])
        text = "\n".join(body)
        pos["sold"] = True
        pos["sell_date"] = today
        pos["sell_price"] = sell_price
        pos["sell_time"] = sell_time
        pos["sell_reason"] = "trix_death_cross"
        pos["alert_pushed"] = True
        state["position"] = pos
        state["last_sell_alert"] = {
            "timestamp": datetime.now().isoformat(),
            "type": "trix_death_cross",
            "etf": etf,
            "sell_time": sell_time,
            "sell_price": sell_price,
            "return_pct": ret_str,
        }
        append_trail_shadow_log({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": "live_sell",
            "sell_reason": "trix_death_cross",
            "etf": etf,
            "name": pos.get("name"),
            "type": pos.get("type", ""),
            "buy_date": buy_date,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "return_pct": ret_str,
            "note": "实盘 TRIX 卖出（shadow 未介入）",
        })
        ret_num = (sell_price - buy_price) / buy_price * 100 if sell_price and buy_price else 0.0
        log_trade_closed(
            pos,
            sell_date=today,
            sell_time=sell_time or now_hm,
            sell_reason="trix_death_cross",
            sell_price=sell_price,
            return_pct=ret_num,
            float_pct=float_ret,
        )
        save_state(state)
    else:
        if time_to_min(now_hm) >= time_to_min(SELL_CUTOFF):
            prices = resolve_exec_prices(sym, bars_today, SELL_CUTOFF, cur)
            cutoff_price = prices["primary"]
            price_lines, cutoff_ret = format_exec_price_lines(prices, buy_price)
            print(
                f"无 TRIX 死叉，{SELL_CUTOFF} 定时卖 @ {cutoff_price:.4f} "
                f"({prices['source']}) 收益 {cutoff_ret:+.2f}%"
            )
            title = f"T0轮动 {SELL_CUTOFF}定时卖{pos['name']}"
            text = "\n".join([
                *header,
                f"**{SELL_CUTOFF} 定时卖出提醒**",
                "",
                f"- 截至 {now_hm} 未触发 {SELL_BAR_LABEL} TRIX({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) 死叉",
                f"- **{SELL_CUTOFF} 定时卖出**",
                *price_lines,
            ])
            pos["sold"] = True
            pos["sell_date"] = today
            pos["sell_price"] = cutoff_price
            pos["sell_time"] = now_hm
            pos["sell_reason"] = "time_sell"
            pos["alert_pushed"] = True
            state["position"] = pos
            state["last_sell_alert"] = {
                "timestamp": datetime.now().isoformat(),
                "type": "time_sell",
                "etf": etf,
                "sell_price": cutoff_price,
                "return_pct": f"{cutoff_ret:+.2f}%",
            }
            append_trail_shadow_log({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": "live_sell",
                "sell_reason": "time_sell",
                "etf": etf,
                "name": pos.get("name"),
                "type": pos.get("type", ""),
                "buy_date": buy_date,
                "buy_price": buy_price,
                "sell_price": cutoff_price,
                "return_pct": f"{cutoff_ret:+.2f}%",
                "note": "实盘 11:05 定时卖出（shadow 未介入）",
            })
            log_trade_closed(
                pos,
                sell_date=today,
                sell_time=now_hm,
                sell_reason="time_sell",
                sell_price=cutoff_price,
                return_pct=cutoff_ret,
                float_pct=float_ret,
            )
            save_state(state)
        else:
            print(f"截至 {now_hm} 未触发 TRIX 死叉，继续持仓（不推送）")
            return 0

    if dry_run:
        print("\n>>> --dry-run，跳过推送")
        return 0

    webhook = (os.getenv("DINGTALK_ROTATION_WEBHOOK") or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    if not webhook:
        print("\n>>> 钉钉未配置，跳过推送")
        return 0

    print("\n>>> 推送钉钉...")
    ok = send_dingtalk(title, text)
    print(f"    {'成功' if ok else '失败'}")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 ETF 动量监控")
    parser.add_argument("--signal", action="store_true", help="14:45 买入信号")
    parser.add_argument("--sell-check", action="store_true", help="次日 TRIX 卖出检查")
    parser.add_argument("--dry-run", action="store_true", help="仅打印不推送")
    parser.add_argument("--test-push", action="store_true", help="测试钉钉推送")
    parser.add_argument("--trail-log", action="store_true", help="查看 1 分 K 追踪 shadow 日志")
    parser.add_argument("--days", type=int, default=7, help="--trail-log 显示最近 N 天")
    args = parser.parse_args()

    if args.trail_log:
        sys.exit(print_trail_shadow_log(days=args.days))

    if args.test_push:
        regime = fetch_regime()
        from quality_pool import has_quality_rules  # noqa: PLC0415

        hybrid_live = has_quality_rules()
        ok = send_dingtalk(
            "T0轮动测试",
            "\n".join([
                "### T0轮动监控测试",
                "",
                *strategy_header_lines(hybrid=hybrid_live),
                *format_regime_block(regime, hybrid=hybrid_live),
                "这是一条测试消息，确认 T+0 ETF 推送配置正确。",
            ]),
        )
        print("成功" if ok else "失败")
        sys.exit(0 if ok else 1)

    if sum([args.signal, args.sell_check, args.trail_log]) > 1:
        print("ERROR: --signal / --sell-check / --trail-log 只能选一个")
        sys.exit(1)
    if not args.signal and not args.sell_check:
        print("ERROR: 请指定 --signal 或 --sell-check（或 --trail-log 查看日志）")
        sys.exit(1)

    code = run_signal(dry_run=args.dry_run) if args.signal else run_sell_check(dry_run=args.dry_run)
    sys.exit(code)


if __name__ == "__main__":
    main()

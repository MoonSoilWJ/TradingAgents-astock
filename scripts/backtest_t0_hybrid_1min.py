#!/usr/bin/env python3
"""T+0 混合卖点 — 1 分钟 K 精确回测（TRIX vs TRIX+追踪回落）。

相对 5 分 K 回测的改进：
- 追踪回落按 1 分 K **逐分钟**推进峰值，用 close 判定回落（避免 5 分 bar 内路径失真）
- TRIX 仍用 5 分 K（与实盘 t0_monitor 一致），由 1 分 K 重采样得到

用法:
    python scripts/backtest_t0_hybrid_1min.py --ndays 9 --source sina
    python scripts/backtest_t0_hybrid_1min.py --ndays 9 --scan-trail
    python scripts/backtest_t0_hybrid_1min.py --ndays 9 --compare-5m  # 同区间 5 分 K 对照
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import _calc_stats  # noqa: E402
from backtest_top1_minute import calc_trix, calc_trix_signal  # noqa: E402
from backtest_t0_1min import load_1min_data  # noqa: E402
from backtest_t0_etf import bar_time_min, price_at_time  # noqa: E402
from backtest_t0_grid import segment_stats  # noqa: E402
from backtest_t0_hybrid_sell import simulate_hybrid_v2, simulate_trail_only  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    MIN_GAIN,
    TRIX_MIN_SELL,
    TRIX_PERIOD,
    apply_net_return,
    bar_clock,
    bars_for_trix,
    next_trading_day,
    time_to_min,
)
from search_t0_time_combo import bars_until, precompute_picks, simulate_exit  # noqa: E402
from t0_etf_list import (  # noqa: E402
    get_all_market_etf_lof,
    get_all_t0_etfs,
    get_t0_only_etfs,
    pool_stats,
)
from tradingagents.dataflows.instrument import settlement_rule  # noqa: E402

SIGNAL_TIME = "14:45"
BUY_TIME = "14:50"
SELL_CUTOFF = "11:05"
TRIX_SIGNAL_PERIOD = 3
MIN_TRADES = 2


def load_5min_parallel(etf_list: list[dict], lookback: int, workers: int = 16) -> dict:
    """并行拉 5 分 K（全市场试跑用）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from backtest_t0_etf import fetch_5min_kline, normalize_5min_bars  # noqa: PLC0415

    datalen_5m = min(lookback * 50 + 200, 5500)
    etf_5min: dict = {}

    def _one(info: dict) -> tuple[str, dict | None]:
        m5 = fetch_5min_kline(info["sina_symbol"], datalen=datalen_5m)
        if m5:
            return info["code"], normalize_5min_bars(m5)
        return info["code"], None

    w = min(workers, max(4, len(etf_list) // 200))
    print(f">>> 并行拉取 {len(etf_list)} 只 5 分 K (workers={w})...")
    done = 0
    with ThreadPoolExecutor(max_workers=w) as pool:
        futs = [pool.submit(_one, info) for info in etf_list]
        for fut in as_completed(futs):
            code, bars = fut.result()
            if bars:
                etf_5min[code] = bars
            done += 1
            if done % 500 == 0 or done == len(etf_list):
                print(f"    5分K 进度 {done}/{len(etf_list)} | 有效={len(etf_5min)}")
    return etf_5min


def resolve_etf_pool(pool: str, etf_1min: dict) -> tuple[list[dict], str]:
    """选股池: current=原106名单 | t0_only=交割T+0过滤 | all_market=全市场∩有1分K缓存。"""
    cached_codes = set(etf_1min.keys())
    if pool == "t0_only":
        lst = get_t0_only_etfs()
        label = f"T+0交割过滤 ({len(lst)}只)"
    elif pool == "all_market":
        lst = [e for e in get_all_market_etf_lof() if e["code"] in cached_codes]
        label = f"全市场ETF/LOF∩缓存 ({len(lst)}只有1分K)"
    else:
        lst = get_all_t0_etfs()
        label = f"原T+0池 ({len(lst)}只)"
    return lst, label


def analyze_picks_settlement(
    picks: dict,
    eval_dates: list[str],
    signal_time: str = SIGNAL_TIME,
) -> list[dict]:
    """统计 precompute 选中的 TOP1 里 T+0 / T+1 占比（用 picks 内名称）。"""
    rows: list[dict] = []
    for day in eval_dates:
        p = picks.get((signal_time, day))
        if not p:
            continue
        code, gain, name = p
        settle = settlement_rule(code, name)
        rows.append({
            "signal_date": day, "code": code, "name": name,
            "gain": gain, "settlement": settle,
        })
    return rows


def compare_exec_error(live: dict, old: dict) -> dict:
    """逐笔对比 live_monitor(1分) vs 旧5分close 的收益误差。"""
    old_by = {(t["signal_date"], t["etf"]): t for t in old.get("trades", [])}
    diffs: list[dict] = []
    for t in live.get("trades", []):
        o = old_by.get((t["signal_date"], t["etf"]))
        if not o:
            continue
        d = t["return_pct"] - o["return_pct"]
        diffs.append({
            "signal_date": t["signal_date"],
            "etf": t["etf"],
            "live_ret": t["return_pct"],
            "old_ret": o["return_pct"],
            "diff_pp": round(d, 2),
            "sell_reason": t["sell_reason"],
        })
    if not diffs:
        return {"count": 0, "avg_abs_pp": 0, "max_abs_pp": 0, "details": []}
    abs_d = [abs(x["diff_pp"]) for x in diffs]
    return {
        "count": len(diffs),
        "avg_abs_pp": round(sum(abs_d) / len(abs_d), 2),
        "max_abs_pp": round(max(abs_d), 2),
        "avg_signed_pp": round(sum(x["diff_pp"] for x in diffs) / len(diffs), 2),
        "details": diffs,
    }


def bar_dt_key(bar: dict) -> tuple[str, int]:
    day = bar.get("day") or str(bar.get("datetime", ""))[:10]
    t = bar.get("time", "00:00:00")[:5]
    return day, time_to_min(t)


def resample_1min_to_5min(bars_1m: list[dict]) -> list[dict]:
    """1 分 K → 5 分 K（按交易日 + 5 分钟桶聚合）。"""
    if not bars_1m:
        return []
    buckets: dict[tuple[str, int], list[dict]] = {}
    for b in bars_1m:
        day, tm = bar_dt_key(b)
        bucket = (tm // 5) * 5
        buckets.setdefault((day, bucket), []).append(b)

    out: list[dict] = []
    for (day, bucket_min), grp in sorted(buckets.items()):
        grp.sort(key=lambda x: bar_dt_key(x)[1])
        h = int(bucket_min // 60)
        m = int(bucket_min % 60)
        out.append({
            "day": day,
            "time": f"{h:02d}:{m:02d}:00",
            "open": float(grp[0]["open"]),
            "high": max(float(x["high"]) for x in grp),
            "low": min(float(x["low"]) for x in grp),
            "close": float(grp[-1]["close"]),
        })
    return out


def first_trix_cross_5m(
    bars_5m: list[dict],
    min_sell_time: str,
    sell_cutoff: str,
    trix_period: int = TRIX_PERIOD,
    trix_signal_period: int = TRIX_SIGNAL_PERIOD,
) -> tuple[int, float, float] | None:
    """返回 (触发分钟, 5分K close, 5分K close 作对照) 或 None。"""
    if len(bars_5m) < trix_period * 3 + 5:
        return None
    closes = [float(b["close"]) for b in bars_5m]
    trix = calc_trix(closes, trix_period)
    signal = calc_trix_signal(trix, trix_signal_period)
    min_m = time_to_min(min_sell_time)
    end_m = time_to_min(sell_cutoff)
    start = trix_period * 3 + 5

    for i in range(start, len(bars_5m)):
        tm = bar_time_min(bars_5m[i])
        if tm < min_m or tm > end_m:
            continue
        if trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
            px5 = float(bars_5m[i]["close"])
            return tm, px5, px5
    return None


def morning_1min_ok(bars_1m: list[dict], need_by: str = TRIX_MIN_SELL) -> tuple[bool, str]:
    """卖出日 1 分 K 须覆盖 need_by 之前，否则 TRIX/定时成交价不可信。"""
    if not bars_1m:
        return False, "no_1min"
    first_m = min(bar_time_min(b) for b in bars_1m)
    need_m = time_to_min(need_by)
    if first_m > need_m:
        h, m = divmod(first_m, 60)
        return False, f"late_start_{h:02d}:{m:02d}"
    return True, "ok"


def morning_5min_ok(bars_5m: list[dict], need_by: str = TRIX_MIN_SELL) -> tuple[bool, str]:
    if not bars_5m:
        return False, "no_5min"
    first_m = min(bar_time_min(b) for b in bars_5m)
    if first_m > time_to_min(need_by):
        h, m = divmod(first_m, 60)
        return False, f"late_5m_{h:02d}:{m:02d}"
    return True, "ok"


def sell_day_data_ok(
    sell_1m: list[dict],
    sell_5m: list[dict],
    *,
    need_through: str = SELL_CUTOFF,
) -> tuple[bool, str]:
    """实盘对齐回测：卖出日须有足够 1 分 K（至定时卖截止）且 5 分 K 覆盖 TRIX 窗口起点。"""
    ok_1m, tag_1m = morning_1min_ok(sell_1m, need_through)
    if not ok_1m:
        return False, tag_1m
    ok_5m, tag_5m = morning_5min_ok(sell_5m, TRIX_MIN_SELL)
    if not ok_5m:
        return False, tag_5m
    return True, "ok"


def first_trix_cross_native_5m(
    bars_buy_day: list[dict],
    bars_sell_day: list[dict],
    cutoff_time: str = SELL_CUTOFF,
    trix_period: int = TRIX_PERIOD,
    trix_signal_period: int = TRIX_SIGNAL_PERIOD,
) -> tuple[str, float] | None:
    """与 t0_monitor.trix_death_cross_hit 一致：原生 5 分 K TRIX 死叉。"""
    cutoff_min = time_to_min(cutoff_time[:5])
    today_cut = [b for b in bars_sell_day if bar_time_min(b) <= cutoff_min]
    if not today_cut:
        return None

    all_bars = bars_for_trix(bars_buy_day) + bars_for_trix(today_cut)
    min_warmup = trix_period * 3 + 5
    if len(all_bars) < min_warmup:
        return None

    warmup_len = len(bars_for_trix(bars_buy_day))
    closes = [float(b["close"]) for b in all_bars]
    trix = calc_trix(closes, trix_period)
    signal = calc_trix_signal(trix, trix_signal_period)
    min_sell_min = time_to_min(TRIX_MIN_SELL)
    search_start = max(warmup_len, min_warmup)

    raw_bars = list(bars_buy_day) + list(today_cut)
    for i in range(search_start, len(all_bars)):
        bar_t = bar_clock(raw_bars[i])
        if time_to_min(bar_t) < min_sell_min:
            continue
        if trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
            return bar_t, float(closes[i])
    return None


def simulate_live_monitor_exec(
    buy_price: float,
    buy_day_1m: list[dict],
    sell_day_1m: list[dict],
    buy_day_5m: list[dict],
    sell_day_5m: list[dict],
    min_sell_time: str = TRIX_MIN_SELL,
    sell_cutoff: str = SELL_CUTOFF,
) -> tuple[float, str, str, float | None, str]:
    """实盘对齐：5 分 K TRIX 信号 + 1 分 K 成交价（含 11:05 定时卖）。"""
    sell_date = str(sell_day_1m[0].get("day", ""))[:10] if sell_day_1m else ""
    ok, cov = morning_1min_ok(sell_day_1m, min_sell_time)
    data_note = cov if not ok else "ok"

    hit = first_trix_cross_native_5m(
        buy_day_5m, sell_day_5m, sell_cutoff,
    )
    if hit:
        bar_t, px_5m = hit
        hm = bar_t.split(" ")[-1][:5] if " " in bar_t else bar_t[:5]
        px_1m, exec_tm = exec_1min_at(sell_day_1m, hm)
        sell_price = px_1m if px_1m and px_1m > 0 else px_5m
        return sell_price, "trix_death_cross", exec_tm or hm, px_5m, data_note

    sell_price, exec_tm = exec_1min_at(sell_day_1m, sell_cutoff)
    if not sell_price:
        window = [b for b in sell_day_1m if bar_time_min(b) <= time_to_min(sell_cutoff)]
        if window:
            sell_price = float(window[-1]["close"])
            exec_tm = window[-1].get("time", "")[:5]
        elif sell_day_5m:
            sell_price = price_at_time(sell_day_5m, sell_cutoff) or buy_price
            exec_tm = sell_cutoff
            data_note = f"{data_note}+5m_fallback" if data_note != "ok" else "5m_fallback"
        else:
            return buy_price, "no_data", "", None, data_note
    return float(sell_price), "time_sell", exec_tm or sell_cutoff, None, data_note


def exec_1min_at(bars_1m: list[dict], hm: str) -> tuple[float | None, str]:
    """取 ≤ hm 的最后一根 1 分 K 收盘价（含 hm 整点 bar）。"""
    if not bars_1m:
        return None, ""
    target = time_to_min(hm)
    best_px: float | None = None
    best_tm = -1
    for b in bars_1m:
        bt = bar_time_min(b)
        if bt > target:
            continue
        if bt >= best_tm:
            best_tm = bt
            best_px = float(b["close"])
    if best_px is None:
        return None, ""
    return best_px, f"{best_tm // 60:02d}:{best_tm % 60:02d}"


def simulate_trix_5m_signal_1m_exec(
    buy_price: float,
    buy_day_1m: list[dict],
    sell_day_1m: list[dict],
    min_sell_time: str = TRIX_MIN_SELL,
    sell_cutoff: str = SELL_CUTOFF,
    trix_period: int = TRIX_PERIOD,
    trix_signal_period: int = TRIX_SIGNAL_PERIOD,
) -> tuple[float, str, str, float | None]:
    """5 分 TRIX 死叉信号 + 1 分 K 成交价（实盘信号逻辑，执行价更贴近真实）。"""
    all_1m = list(buy_day_1m) + list(sell_day_1m)
    bars_5m = resample_1min_to_5min(all_1m)
    hit = first_trix_cross_5m(
        bars_5m, min_sell_time, sell_cutoff, trix_period, trix_signal_period,
    )
    sell_day = str(sell_day_1m[0].get("day", ""))[:10] if sell_day_1m else ""

    if hit:
        tm, px_5m, _ = hit
        hm = f"{tm // 60:02d}:{tm % 60:02d}"
        px_1m, exec_tm = exec_1min_at(sell_day_1m, hm)
        sell_price = px_1m if px_1m and px_1m > 0 else px_5m
        return sell_price, "trix_death_cross", exec_tm or hm, px_5m

    sell_price, exec_tm = exec_1min_at(sell_day_1m, sell_cutoff)
    if not sell_price:
        window = [b for b in sell_day_1m if bar_time_min(b) <= time_to_min(sell_cutoff)]
        if not window:
            return buy_price, "no_data", "", None
        sell_price = float(window[-1]["close"])
        exec_tm = window[-1].get("time", "")[:5]
    return sell_price, "time_sell", exec_tm, None


def simulate_trail_1min(
    buy_price: float,
    sell_bars_1m: list[dict],
    min_sell_time: str = TRIX_MIN_SELL,
    sell_cutoff: str = SELL_CUTOFF,
    trail_drop_pct: float = 0.5,
    use_close: bool = True,
) -> tuple[float, str]:
    """1 分 K 逐 bar 追踪：peak=running high，回落用 close（默认）或 low 判定。"""
    window = [b for b in sell_bars_1m if time_to_min(b.get("time", "00:00")[:5]) <= time_to_min(sell_cutoff)]
    if not window:
        return buy_price, "no_data"

    min_m = time_to_min(min_sell_time)
    peak = buy_price

    for b in window:
        tm = bar_time_min(b)
        if tm < min_m:
            continue
        high = float(b["high"])
        low = float(b["low"])
        close = float(b["close"])
        peak = max(peak, high)
        trigger_px = close if use_close else low
        if peak > buy_price and trigger_px <= peak * (1 - trail_drop_pct / 100):
            sell_px = trigger_px if use_close else peak * (1 - trail_drop_pct / 100)
            return sell_px, "trail_drop"

    return float(window[-1]["close"]), "time_sell"


def simulate_hybrid_1min(
    buy_price: float,
    buy_day_1m: list[dict],
    sell_day_1m: list[dict],
    min_sell_time: str = TRIX_MIN_SELL,
    sell_cutoff: str = SELL_CUTOFF,
    trail_drop_pct: float = 0.5,
    trix_period: int = TRIX_PERIOD,
    trix_signal_period: int = TRIX_SIGNAL_PERIOD,
) -> tuple[float, str]:
    """1 分 K 追踪 + 5 分 TRIX（1 分重采样），同窗口内先到先卖。"""
    all_1m = list(buy_day_1m) + list(sell_day_1m)
    bars_5m = resample_1min_to_5min(all_1m)
    trix_hit = first_trix_cross_5m(
        bars_5m, min_sell_time, sell_cutoff, trix_period, trix_signal_period,
    )
    trix_min = trix_hit[0] if trix_hit else None
    trix_price_5m = trix_hit[1] if trix_hit else None

    min_m = time_to_min(min_sell_time)
    end_m = time_to_min(sell_cutoff)
    peak = buy_price

    sell_window = [
        b for b in sell_day_1m
        if min_m <= bar_time_min(b) <= end_m
    ]
    if not sell_window:
        return buy_price, "no_data"

    for b in sell_window:
        tm = bar_time_min(b)
        high = float(b["high"])
        close = float(b["close"])
        peak = max(peak, high)

        if peak > buy_price and close <= peak * (1 - trail_drop_pct / 100):
            return close, "trail_drop"

        if trix_min is not None and tm >= trix_min:
            hm = f"{trix_min // 60:02d}:{trix_min % 60:02d}"
            px_1m, _ = exec_1min_at(sell_day_1m, hm)
            return (px_1m if px_1m else trix_price_5m) or close, "trix_death_cross"

    last = float(sell_window[-1]["close"])
    if trix_min is not None:
        hm = f"{trix_min // 60:02d}:{trix_min % 60:02d}"
        px_1m, _ = exec_1min_at(sell_day_1m, hm)
        return (px_1m if px_1m else trix_price_5m) or last, "trix_death_cross"
    return last, "time_sell"


def simulate_trix_5m_close_only(
    buy_price: float,
    buy_day_1m: list[dict],
    sell_day_1m: list[dict],
    min_sell_time: str = TRIX_MIN_SELL,
    sell_cutoff: str = SELL_CUTOFF,
) -> tuple[float, str]:
    """旧逻辑：5 分 TRIX 信号且以 5 分 K close 成交（偏乐观）。"""
    all_1m = list(buy_day_1m) + list(sell_day_1m)
    bars_5m = resample_1min_to_5min(all_1m)
    hit = first_trix_cross_5m(bars_5m, min_sell_time, sell_cutoff)
    if hit:
        return hit[1], "trix_death_cross"
    window = [b for b in sell_day_1m if bar_time_min(b) <= time_to_min(sell_cutoff)]
    if not window:
        return buy_price, "no_data"
    return float(window[-1]["close"]), "time_sell"


def simulate_trix_1min_resampled(
    buy_price: float,
    buy_day_1m: list[dict],
    sell_day_1m: list[dict],
    min_sell_time: str = TRIX_MIN_SELL,
    sell_cutoff: str = SELL_CUTOFF,
) -> tuple[float, str]:
    """5 分 TRIX 信号 + 1 分 K 成交价（推荐，贴近实盘）。"""
    px, reason, _, _ = simulate_trix_5m_signal_1m_exec(
        buy_price, buy_day_1m, sell_day_1m, min_sell_time, sell_cutoff,
    )
    return px, reason


def run_strategy_1min(
    mode: str,
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_1min: dict,
    fee_pct: float,
    trail_drop_pct: float = 0.5,
    etf_5min: dict | None = None,
) -> dict | None:
    rets: list[float] = []
    trades: list[dict] = []
    reasons: dict[str, int] = {}

    for day in eval_dates:
        picked = picks.get((SIGNAL_TIME, day))
        if not picked:
            continue
        code, gain, name = picked

        day_1m = etf_1min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_1m, BUY_TIME)
        if not buy_price or buy_price <= 0:
            continue

        sell_day = next_trading_day(all_dates, day)
        if not sell_day:
            continue
        sell_1m = etf_1min.get(code, {}).get(sell_day, [])
        if len(sell_1m) < 10:
            continue

        sell_time = ""
        px_5m: float | None = None
        data_note = "ok"

        if mode == "trix_1m":
            sell_price, reason, sell_time, px_5m = simulate_trix_5m_signal_1m_exec(
                buy_price, day_1m, sell_1m,
            )
            data_note = morning_1min_ok(sell_1m, SELL_CUTOFF)[1]
            if data_note != "ok":
                continue
        elif mode == "live_monitor":
            if not etf_5min:
                continue
            buy_5m = etf_5min.get(code, {}).get(day, [])
            sell_5m = etf_5min.get(code, {}).get(sell_day, [])
            if len(sell_5m) < 5:
                continue
            data_ok, data_note = sell_day_data_ok(sell_1m, sell_5m)
            if not data_ok:
                continue
            sell_price, reason, sell_time, px_5m, exec_note = simulate_live_monitor_exec(
                buy_price, day_1m, sell_1m, buy_5m, sell_5m,
            )
            if exec_note != "ok":
                data_note = exec_note
        elif mode == "trix_5m_close":
            sell_price, reason = simulate_trix_5m_close_only(
                buy_price, day_1m, sell_1m,
            )
        elif mode == "trail_1m":
            sell_price, reason = simulate_trail_1min(
                buy_price, sell_1m, TRIX_MIN_SELL, SELL_CUTOFF, trail_drop_pct,
            )
        elif mode == "hybrid_1m":
            sell_price, reason = simulate_hybrid_1min(
                buy_price, day_1m, sell_1m, TRIX_MIN_SELL, SELL_CUTOFF, trail_drop_pct,
            )
        elif mode == "trix_5m" and etf_5min:
            day_5m = etf_5min.get(code, {}).get(day, [])
            sell_5m = etf_5min.get(code, {}).get(sell_day, [])
            sell_price, reason, _ = simulate_exit(
                "trix0940_cut", buy_price, day_5m, BUY_TIME, sell_5m, SELL_CUTOFF,
                trix_period=TRIX_PERIOD, trix_signal_period=TRIX_SIGNAL_PERIOD,
            )
        elif mode == "hybrid_5m" and etf_5min:
            day_5m = etf_5min.get(code, {}).get(day, [])
            sell_5m = etf_5min.get(code, {}).get(sell_day, [])
            sell_price, reason = simulate_hybrid_v2(
                buy_price, day_5m, sell_5m, SELL_CUTOFF, TRIX_MIN_SELL,
                TRIX_PERIOD, TRIX_SIGNAL_PERIOD, trail_drop_pct,
            )
        elif mode == "trail_5m" and etf_5min:
            sell_5m = etf_5min.get(code, {}).get(sell_day, [])
            sell_price, reason = simulate_trail_only(
                buy_price, sell_5m, SELL_CUTOFF, TRIX_MIN_SELL, trail_drop_pct,
            )
        else:
            raise ValueError(mode)

        if sell_price is None or sell_price <= 0:
            continue

        ret = apply_net_return(buy_price, sell_price, fee_pct)
        rets.append(ret)
        reasons[reason] = reasons.get(reason, 0) + 1
        row = {
            "signal_date": day,
            "sell_date": sell_day,
            "etf": code,
            "name": name,
            "today_gain": round(gain, 2),
            "buy_time": BUY_TIME,
            "buy_price": round(buy_price, 4),
            "sell_price": round(float(sell_price), 4),
            "sell_reason": reason,
            "return_pct": round(ret, 4),
        }
        if mode in ("trix_1m", "live_monitor"):
            row["sell_time"] = sell_time
            row["sell_price_5m_ref"] = round(px_5m, 4) if px_5m else None
            row["data_1min"] = data_note
        trades.append(row)

    if len(rets) < MIN_TRADES:
        return None

    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {
        "mode": mode,
        "trail_drop_pct": trail_drop_pct,
        "trade_count": len(rets),
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets),
        "sell_reasons": reasons,
        "trades": trades,
    }


def report_skipped_realistic(
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_1min: dict,
    etf_5min: dict,
    fee_pct: float,
    live_trades: list[dict] | None = None,
) -> list[dict]:
    """列出因卖出日 K 线不完整而未纳入 live_monitor 的笔，并给出 11:05 1分/5分对照。"""
    live_days = {(t["signal_date"], t["etf"]) for t in (live_trades or [])}
    rows: list[dict] = []
    for day in eval_dates:
        picked = picks.get((SIGNAL_TIME, day))
        if not picked:
            continue
        code, _, name = picked
        if (day, code) in live_days:
            continue
        day_1m = etf_1min.get(code, {}).get(day, [])
        buy_price = price_at_time(day_1m, BUY_TIME)
        if not buy_price:
            continue
        sell_day = next_trading_day(all_dates, day)
        if not sell_day:
            continue
        sell_1m = etf_1min.get(code, {}).get(sell_day, [])
        sell_5m = etf_5min.get(code, {}).get(sell_day, [])
        ok, note = sell_day_data_ok(sell_1m, sell_5m)
        if ok:
            continue
        px_1m, _ = exec_1min_at(sell_1m, SELL_CUTOFF)
        px_5m = price_at_time(sell_5m, SELL_CUTOFF) if sell_5m else None
        row: dict = {
            "signal_date": day,
            "etf": code,
            "name": name,
            "buy_price": round(buy_price, 4),
            "data_note": note,
        }
        if px_1m:
            row["sell_1m_1105"] = round(px_1m, 4)
            row["ret_1m_1105"] = round(apply_net_return(buy_price, px_1m, fee_pct), 2)
        if px_5m:
            row["sell_5m_1105"] = round(float(px_5m), 4)
            row["ret_5m_1105"] = round(apply_net_return(buy_price, float(px_5m), fee_pct), 2)
        rows.append(row)
    return rows


def print_results(results: list[dict], title: str):
    labels = {
        "trix_1m": "纯TRIX(1分→5分)",
        "live_monitor": "实盘对齐(5分信号1分价)",
        "trix_5m_close": "旧版(5分close成交)",
        "hybrid_1m": "混合(1分追踪)",
        "trail_1m": "纯追踪(1分)",
        "trix_5m": "纯TRIX(5分K)",
        "hybrid_5m": "混合(5分K)",
        "trail_5m": "纯追踪(5分K)",
    }
    print("=" * 95)
    print(f"  {title}")
    print("=" * 95)
    print(f"  {'方案':<22} {'笔数':>4} {'累计':>9} {'胜率':>6} {'均笔':>7} {'回撤':>8}")
    print("  " + "─" * 85)
    for r in results:
        st = r["stats"]
        label = labels.get(r["mode"], r["mode"])
        if "hybrid" in r["mode"] or "trail" in r["mode"]:
            label = f"{label} {r['trail_drop_pct']:.1f}%"
        print(
            f"  {label:<22} {r['trade_count']:>4} {r['final_equity_pct']:+8.2f}% "
            f"{st.get('win_rate', 0):>5.1f}% {st.get('avg', 0):>+6.2f}% "
            f"{st.get('max_drawdown', 0):>+7.2f}%"
        )
    print("=" * 95)
    for r in results:
        print(f"  {labels.get(r['mode'], r['mode'])} 卖出: {r['sell_reasons']}")


def print_trade_table(result: dict, title: str):
    print("\n" + "=" * 115)
    print(f"  {title}")
    print("=" * 115)
    print(
        f"  {'信号日':>10} {'卖日':>10} {'买':>5} {'卖':>5} {'ETF':>8} "
        f"{'买价':>7} {'卖价1m':>8} {'卖价5m':>8} {'原因':>12} {'收益':>7} {'1分K':>12}"
    )
    print("  " + "-" * 110)
    eq = 1.0
    for t in result.get("trades") or []:
        eq *= 1 + t["return_pct"] / 100
        p5 = t.get("sell_price_5m_ref")
        p5s = f"{p5:8.4f}" if p5 is not None else "       —"
        print(
            f"  {t['signal_date']:>10} {t.get('sell_date',''):>10} "
            f"{t.get('buy_time', BUY_TIME):>5} {t.get('sell_time',''):>5} {t['etf']:>8} "
            f"{t['buy_price']:7.4f} {t['sell_price']:8.4f} {p5s} "
            f"{t['sell_reason']:>12} {t['return_pct']:+6.2f}% {t.get('data_1min',''):>12} | cum {(eq-1)*100:+6.2f}%"
        )
    print("=" * 115)


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 混合卖点 1 分钟 K 回测")
    parser.add_argument("--ndays", type=int, default=9)
    parser.add_argument("--source", choices=["auto", "em", "sina"], default="sina")
    parser.add_argument("--trail-drop", type=float, default=0.5)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--scan-trail", action="store_true")
    parser.add_argument("--compare-5m", action="store_true", help="同区间附加 5 分 K 对照")
    parser.add_argument(
        "--realistic", action="store_true",
        help="实盘对齐：原生5分TRIX + 1分成交价，并对比旧5分close回测",
    )
    parser.add_argument(
        "--pool", choices=["current", "t0_only", "all_market"], default="current",
        help="选股池: current=原名单 | t0_only=交割T+0 | all_market=全市场∩已拉1分K",
    )
    parser.add_argument(
        "--compare-pools", action="store_true",
        help="对比三池 + 1分成交价 vs 旧5分close 误差",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="不读池缓存，直连新浪/东财拉取",
    )
    parser.add_argument(
        "--no-write-cache", action="store_true",
        help="拉取完成后不写缓存（默认会写，供下次加速）",
    )
    parser.add_argument(
        "--fetch-universe", choices=["t0_pool", "all_market"], default=None,
        help="拉取数据的标的范围（默认: all_market池→全市场，否则→原106池）",
    )
    parser.add_argument(
        "--fetch-limit", type=int, default=None,
        help="限制拉取只数（调试用；默认全量）",
    )
    parser.add_argument("--trades", action="store_true", help="打印逐笔明细")
    args = parser.parse_args()

    print("=== T+0 混合卖点 1 分钟 K 回测 ===")
    print(f"数据源: {args.source} ndays={args.ndays} | 追踪回落: {args.trail_drop}%")
    print(f"买点 {SIGNAL_TIME}/{BUY_TIME} | 卖点 {TRIX_MIN_SELL}~{SELL_CUTOFF}")
    print(f"TRIX: 5分K({TRIX_PERIOD},{TRIX_SIGNAL_PERIOD}) | 追踪: 1分K逐bar close判定")
    print(f"过滤: 涨幅≥{MIN_GAIN}% | 震荡跳过\n")

    fetch_univ = args.fetch_universe
    if fetch_univ is None:
        fetch_univ = "all_market" if (args.no_cache or args.pool == "all_market") else "t0_pool"
    if fetch_univ == "all_market":
        fetch_list = get_all_market_etf_lof()
        fetch_label = f"全市场ETF/LOF ({len(fetch_list)}只)"
    else:
        fetch_list = get_all_t0_etfs()
        fetch_label = f"原T+0池 ({len(fetch_list)}只)"
    if args.no_cache:
        print(f">>> 直连拉取（--no-cache）| {fetch_label} | source={args.source}")
    else:
        print(f">>> 数据范围: {fetch_label} | source={args.source}")

    cache_suffix = "_allmarket" if fetch_univ == "all_market" else ""
    min_write = 100 if fetch_univ == "all_market" else 50
    etf_daily, etf_1min, all_dates, proxy_klines, data_source = load_1min_data(
        fetch_list, args.ndays, source=args.source,
        use_cache=not args.no_cache,
        write_cache=not args.no_write_cache,
        fetch_limit=args.fetch_limit,
        cache_suffix=cache_suffix,
        min_write_count=min_write,
    )
    if len(etf_1min) < 5:
        print("ERROR: 1 分 K 不足")
        sys.exit(1)

    m1_dates = sorted({d for bars in etf_1min.values() for d in bars})
    # 新浪 1 分 K 一次可返回数百日历史；仅取最近 ndays 个交易日
    if len(m1_dates) > args.ndays:
        m1_dates = m1_dates[-args.ndays:]
    eval_dates = m1_dates[:-1]
    if len(eval_dates) < MIN_TRADES:
        print("ERROR: 信号日不足")
        sys.exit(1)
    print(f"数据: {data_source}")
    print(f"1分K缓存: {len(etf_1min)} 只 | 交易日 {m1_dates[0]} ~ {m1_dates[-1]} ({len(m1_dates)} 日)")
    print(f"信号日: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日)\n")

    from backtest_t0_today1 import load_market_data  # noqa: E402

    codes_1m = {e["code"] for e in fetch_list if e["code"] in etf_1min}
    list_5m = [e for e in fetch_list if e["code"] in codes_1m]
    lookback = max(len(m1_dates) + 30, 60)
    if len(list_5m) > 150:
        etf_5min = load_5min_parallel(list_5m, lookback)
    else:
        _, etf_5min, _, _ = load_market_data(list_5m, lookback=lookback)
    m5_dates = {d for bars in etf_5min.values() for d in bars}
    all_dates = sorted(set(all_dates) | m5_dates)

    if args.compare_pools:
        pool_rows: list[dict] = []
        pick_audit: list[dict] = []
        print("=" * 100)
        print("  三池对比 — 实盘对齐(5分TRIX+1分价) vs 旧5分close | 买/卖均1分K")
        print("=" * 100)
        print(
            f"  {'池子':<28} {'选股':>4} {'T1选':>4} {'live笔':>5} {'live累计':>9} "
            f"{'旧5分':>9} {'均误差':>7} {'最大误差':>8}"
        )
        print("  " + "─" * 95)
        for pool_key in ("current", "t0_only", "all_market"):
            plst, plabel = resolve_etf_pool(pool_key, etf_1min)
            picks = precompute_picks(
                plst, etf_daily, etf_1min, eval_dates, [SIGNAL_TIME],
                proxy_klines, use_filter=True, skip_choppy=True,
            )
            audit = analyze_picks_settlement(picks, eval_dates)
            t1_picks = sum(1 for a in audit if a["settlement"] != "T0")
            pick_audit.extend([{**a, "pool": pool_key} for a in audit])
            common = dict(
                eval_dates=eval_dates, all_dates=all_dates, picks=picks,
                etf_1min=etf_1min, fee_pct=args.fee, etf_5min=etf_5min,
            )
            live = run_strategy_1min("live_monitor", **common)
            old = run_strategy_1min("trix_5m_close", **common)
            err = compare_exec_error(live or {}, old or {})
            live_cum = live["final_equity_pct"] if live else 0.0
            old_cum = old["final_equity_pct"] if old else 0.0
            live_n = live["trade_count"] if live else 0
            print(
                f"  {plabel:<28} {len(audit):>4} {t1_picks:>4} {live_n:>5} "
                f"{live_cum:+8.2f}% {old_cum:+8.2f}% {err['avg_abs_pp']:>6.2f}pp {err['max_abs_pp']:>7.2f}pp"
            )
            pool_rows.append({
                "pool": pool_key, "label": plabel, "picks": audit,
                "live": live, "old_close": old, "error": err,
            })
        print("=" * 100)
        print("\n  原池选股交割审计（precompute TOP1，未过滤T+0）:")
        for a in pick_audit:
            if a["pool"] != "current":
                continue
            st = "T+0" if a["settlement"] == "T0" else "T+1"
            print(f"    {a['signal_date']} {a['code']} {a['name'][:12]:<12} +{a['gain']:.1f}% [{st}]")
        cur = next(r for r in pool_rows if r["pool"] == "current")
        if cur["live"] and cur["old_close"]:
            print(f"\n  原池逐笔误差 (live_1m - 旧5分close):")
            print(f"  {'信号日':>10} {'ETF':>8} {'live':>8} {'旧5分':>8} {'差pp':>7} {'卖因'}")
            for d in cur["error"]["details"]:
                print(
                    f"  {d['signal_date']:>10} {d['etf']:>8} {d['live_ret']:+7.2f}% "
                    f"{d['old_ret']:+7.2f}% {d['diff_pp']:+6.2f} {d['sell_reason']}"
                )
        skipped = report_skipped_realistic(
            eval_dates, all_dates,
            precompute_picks(
                get_all_t0_etfs(), etf_daily, etf_1min, eval_dates, [SIGNAL_TIME],
                proxy_klines, use_filter=True, skip_choppy=True,
            ),
            etf_1min, etf_5min, args.fee,
            live_trades=(cur.get("live") or {}).get("trades"),
        )
        if skipped:
            print(f"\n  ⚠ 数据不完整剔除 {len(skipped)} 笔（1分/5分缺上午K线，参考 11:05 对照）:")
            print(f"  {'信号日':>10} {'ETF':>8} {'买价':>8} {'11:05_1m':>9} {'11:05_5m':>9} {'1m%':>7} {'5m%':>7}")
            for row in skipped:
                print(
                    f"  {row['signal_date']:>10} {row['etf']:>8} {row['buy_price']:8.4f} "
                    f"{row.get('sell_1m_1105') or 0:9.4f} {row.get('sell_5m_1105') or 0:9.4f} "
                    f"{row.get('ret_1m_1105', 0):+6.2f}% {row.get('ret_5m_1105', 0):+6.2f}%"
                )
        market_total = len(get_all_market_etf_lof())
        print(
            f"\n  说明: 全市场池= mootdx {market_total} 只 ETF/LOF 与 1分K缓存 {len(etf_1min)} 只求交；"
            "扩大全市场回测需先对更多标的跑 cache_min_data"
        )
        if args.trades:
            for row in pool_rows:
                if row["live"]:
                    print_trade_table(row["live"], f"{row['label']} — 实盘对齐逐笔")
        out_dir = Path.home() / ".tradingagents" / "rotation"
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d_%H%M")
        out_path = out_dir / f"backtest_t0_pool_compare_{tag}.json"
        out_path.write_text(json.dumps({
            "config": {
                "ndays": args.ndays, "source": args.source, "data_source": data_source,
                "m1_dates": m1_dates, "eval_dates": eval_dates,
                "cached_symbols": len(etf_1min),
            },
            "pools": [{
                "pool": r["pool"], "label": r["label"],
                "picks": r["picks"],
                "live_summary": {k: v for k, v in (r["live"] or {}).items() if k != "trades"},
                "old_summary": {k: v for k, v in (r["old_close"] or {}).items() if k != "trades"},
                "error": r["error"],
                "trades_live": (r["live"] or {}).get("trades"),
            } for r in pool_rows],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已保存: {out_path}")
        sys.exit(0)

    etf_list, pool_label = resolve_etf_pool(args.pool, etf_1min)
    print(f"选股池: {pool_label}\n")
    picks = precompute_picks(
        etf_list, etf_daily, etf_1min, eval_dates, [SIGNAL_TIME],
        proxy_klines, use_filter=True, skip_choppy=True,
    )

    if args.compare_5m or args.realistic:
        pass  # etf_5min 已在上方加载
    else:
        etf_5min = None

    common = dict(
        eval_dates=eval_dates, all_dates=all_dates, picks=picks,
        etf_1min=etf_1min, fee_pct=args.fee, etf_5min=etf_5min,
    )

    if args.scan_trail:
        trix = run_strategy_1min("trix_1m", **common)
        if not trix:
            print("ERROR: TRIX 无交易")
            sys.exit(1)
        print(f"  纯 TRIX(1分→5分): {trix['final_equity_pct']:+.2f}% ({trix['trade_count']}笔)\n")
        print(f"  {'回落%':>6} {'混合累计':>9} {'vsTRIX':>8} {'追踪占比':>8} {'卖出原因'}")
        print("  " + "─" * 60)
        for drop in [0.3, 0.4, 0.5, 0.6, 0.8, 1.0]:
            h = run_strategy_1min("hybrid_1m", trail_drop_pct=drop, **common)
            if not h:
                continue
            trail_n = h["sell_reasons"].get("trail_drop", 0)
            pct = trail_n / h["trade_count"] * 100
            diff = h["final_equity_pct"] - trix["final_equity_pct"]
            print(
                f"  {drop:>5.1f}% {h['final_equity_pct']:+8.2f}% {diff:>+7.2f}% "
                f"{pct:>7.1f}% {h['sell_reasons']}"
            )
        sys.exit(0)

    if args.realistic:
        if not etf_5min:
            print("ERROR: 需要 5 分 K")
            sys.exit(1)
        live = run_strategy_1min("live_monitor", **common)
        old_close = run_strategy_1min("trix_5m_close", **common)
        resampled = run_strategy_1min("trix_1m", **common)
        if not live:
            print("ERROR: live_monitor 无有效交易")
            sys.exit(1)
        results = [r for r in (live, old_close, resampled) if r]
        print_results(
            results,
            f"实盘对齐回测 ({len(eval_dates)} 信号日) — 买1分K / 信号原生5分TRIX / 卖1分K",
        )
        print(
            "\n  说明: 「旧5分close」≈ backtest_t0_1min 偏乐观；"
            "「live_monitor」= 原生5分TRIX + 1分成交价（11:05 定时卖用1分 close）"
        )
        print(
            "  数据门槛: 卖出日 1分K须覆盖至11:05 且 5分K须覆盖09:40（缺上午K线的笔会剔除，避免假回测）"
        )
        skipped = report_skipped_realistic(
            eval_dates, all_dates, picks, etf_1min, etf_5min, args.fee,
            live_trades=live.get("trades"),
        )
        if skipped:
            print(f"\n  ⚠ 剔除 {len(skipped)} 笔（卖出日分钟线不完整），参考估算:")
            print(f"  {'信号日':>10} {'ETF':>8} {'买价1m':>8} {'11:05_1m':>9} {'11:05_5m':>9} {'1m收益':>8} {'5m收益':>8} {'原因'}")
            for row in skipped:
                print(
                    f"  {row['signal_date']:>10} {row['etf']:>8} {row['buy_price']:8.4f} "
                    f"{row.get('sell_1m_1105') or 0:9.4f} {row.get('sell_5m_1105') or 0:9.4f} "
                    f"{row.get('ret_1m_1105', 0):+7.2f}% {row.get('ret_5m_1105', 0):+7.2f}% {row['data_note']}"
                )
        if args.trades:
            print_trade_table(live, "实盘对齐 — 逐笔（卖价1m / 对照卖价5m）")
        if old_close and live:
            by_day = {t["signal_date"]: t for t in old_close["trades"]}
            print(f"\n  {'信号日':>10} {'ETF':>8} {'旧5分close':>10} {'live_1m':>10} {'差pp':>7}")
            for t in live["trades"]:
                o = by_day.get(t["signal_date"])
                if not o or o["etf"] != t["etf"]:
                    continue
                d = t["return_pct"] - o["return_pct"]
                print(
                    f"  {t['signal_date']:>10} {t['etf']:>8} {o['return_pct']:+9.2f}% "
                    f"{t['return_pct']:+9.2f}% {d:+6.2f}"
                )
        out_dir = Path.home() / ".tradingagents" / "rotation"
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d_%H%M")
        out_path = out_dir / f"backtest_t0_realistic_{tag}.json"
        out_path.write_text(json.dumps({
            "config": {
                "ndays": args.ndays, "source": args.source, "data_source": data_source,
                "eval_dates": eval_dates, "exec": "5m_trix_native_1m_price",
            },
            "live_monitor": live,
            "trix_5m_close": old_close,
            "trix_1m_resampled": resampled,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已保存: {out_path}")
        sys.exit(0)

    trix_1m = run_strategy_1min("trix_1m", **common)
    hybrid_1m = run_strategy_1min("hybrid_1m", trail_drop_pct=args.trail_drop, **common)
    trail_1m = run_strategy_1min("trail_1m", trail_drop_pct=args.trail_drop, **common)

    results = [r for r in (trix_1m, hybrid_1m, trail_1m) if r]
    if args.compare_5m and etf_5min:
        for mode in ("trix_5m", "hybrid_5m", "trail_5m"):
            r = run_strategy_1min(mode, trail_drop_pct=args.trail_drop, **common)
            if r:
                results.append(r)

    if not trix_1m or not hybrid_1m:
        print("ERROR: 有效交易不足")
        sys.exit(1)

    print_results(results, f"1 分 K 混合回测 ({len(eval_dates)} 信号日)")

    if trix_1m and hybrid_1m:
        diff = hybrid_1m["final_equity_pct"] - trix_1m["final_equity_pct"]
        print(f"\n  混合(1分) vs 纯TRIX(1分→5分): {diff:+.2f}%")

        trix_by_day = {t["signal_date"]: t for t in trix_1m["trades"]}
        changed = []
        for ht in hybrid_1m["trades"]:
            tt = trix_by_day.get(ht["signal_date"])
            if tt and ht["sell_reason"] == "trail_drop":
                changed.append({
                    "date": ht["signal_date"],
                    "etf": ht["etf"],
                    "trix_ret": tt["return_pct"],
                    "hybrid_ret": ht["return_pct"],
                    "diff": round(ht["return_pct"] - tt["return_pct"], 2),
                })
        if changed:
            wins = sum(1 for c in changed if c["diff"] > 0)
            print(f"  追踪抢先 {len(changed)} 笔，混合更优 {wins}，更差 {len(changed)-wins}")
            print(f"  {'信号日':>12} {'ETF':>8} {'TRIX':>8} {'混合':>8} {'差':>7}")
            for c in sorted(changed, key=lambda x: -abs(x["diff"]))[:8]:
                print(
                    f"  {c['date']:>12} {c['etf']:>8} {c['trix_ret']:+7.2f}% "
                    f"{c['hybrid_ret']:+7.2f}% {c['diff']:+6.2f}%"
                )

    if args.compare_5m:
        h5 = next((r for r in results if r["mode"] == "hybrid_5m"), None)
        if h5 and hybrid_1m:
            print(f"\n  ★ 5分 vs 1分 混合(回落{args.trail_drop}%): "
                  f"5分{h5['final_equity_pct']:+.2f}% vs 1分{hybrid_1m['final_equity_pct']:+.2f}% "
                  f"(差{hybrid_1m['final_equity_pct']-h5['final_equity_pct']:+.2f}%)")

    out_dir = Path.home() / ".tradingagents" / "rotation"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = out_dir / f"backtest_t0_hybrid_1min_{tag}.json"
    out_path.write_text(json.dumps({
        "config": {
            "ndays": args.ndays, "source": args.source, "data_source": data_source,
            "eval_dates": eval_dates, "trail_drop": args.trail_drop,
        },
        "results": [{k: v for k, v in r.items() if k != "trades"} for r in results],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()

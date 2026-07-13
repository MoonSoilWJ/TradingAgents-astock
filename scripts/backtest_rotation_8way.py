#!/usr/bin/env python3
"""板块轮动 6 组回测：5 个固定信号时点 + 1 组卖出后动态信号。

信号组 1~5: 09:10 / 09:25 / 09:40 / 14:40 / 14:50（平安 79 板块 v6 TOP1）
信号组 6: 卖出后即时发信号（首日空仓 09:30 发首信号）
买入: 上涨 1.0% 追涨（不抄底）
卖出: 追踪触 +3% 回落 0.5%，T+1 收盘卖（无固定止损）
过滤: 前一日涨幅 >7% 则跳过（防追高次日暴跌）
约束: 单仓位、先卖后买、手续费万分之 3（双边）
未买入: 信号日当天未触发追涨则放弃（不兜底）
开盘前信号(09:10/09:25/09:30): v6 用 T-1 日完整得分（无 5 分 K 时不偷看当日收盘）

用法:
    python scripts/backtest_rotation_8way.py
    python scripts/backtest_rotation_8way.py --days 30 --daily
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import (  # noqa: E402
    _calc_stats,
    curl_get,
    fetch_sina_kline,
)
from backtest_top1_intraday import check_buy_trigger, check_sell_trigger  # noqa: E402
from rotation_v6 import SCORE_WINDOW, score_at_signal  # noqa: E402
from sector_etf_map import etf_to_sina_symbol, load_pingan_sectors  # noqa: E402

PROXY = __import__("os").environ.get("ROTATION_PROXY", "http://127.0.0.1:7890")
SINA_INTERVAL = 0.25
TIMEOUT = 15

SIGNAL_TIMES = ("09:10", "09:25", "09:40", "14:40", "14:50")
BUY_UP = 1.0
BUY_DOWN = 99.0  # 禁用抄底（回测显示抄底单亏损率高）
BUY_REBOUND = 0.3
STOP_LOSS = -99.0  # 禁用固定止损（靠追踪止盈+T+1收盘卖）
TRAIL_TRIGGER = 3.0
TRAIL_DROP = 0.5
FEE_PCT = 0.03  # 万分之 3
MAX_3D_RETURN = 99.0  # 禁用过热过滤（设极高值）
PREV_DAY_SURGE_LIMIT = 7.0  # 前一日涨幅超过此值(%)则不买（防追高暴跌）


def time_to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def fetch_5min_kline(symbol: str, datalen: int = 2500) -> list[dict]:
    url = (
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=5&ma=no&datalen={datalen}"
    )
    raw = curl_get(url)
    time.sleep(SINA_INTERVAL)
    if not raw or raw.strip() in ("null", "", "[]"):
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def compute_daily_data(klines: list[dict]) -> list[dict]:
    result = []
    for i, k in enumerate(klines):
        close = float(k.get("close", 0))
        try:
            volume = float(k.get("volume", 0))
        except (ValueError, TypeError):
            volume = 0.0
        if i == 0:
            ret = 0.0
        else:
            prev = float(klines[i - 1].get("close", 0))
            ret = ((close - prev) / prev * 100) if prev else 0.0
        result.append({"date": k.get("day", ""), "close": close, "return_pct": ret, "volume": volume})
    return result


def normalize_5min_bars(klines: list[dict]) -> dict[str, list[dict]]:
    by_day: dict[str, list[dict]] = {}
    for k in klines:
        dt = k.get("day", "")
        if not dt:
            continue
        parts = dt.split(" ")
        day = parts[0]
        t = parts[1] if len(parts) > 1 else "00:00:00"
        by_day.setdefault(day, []).append({
            "datetime": dt,
            "day": day,
            "time": t,
            "open": float(k.get("open", 0)),
            "high": float(k.get("high", 0)),
            "low": float(k.get("low", 0)),
            "close": float(k.get("close", 0)),
            "volume": float(k.get("volume", 0)),
        })
    for day in by_day:
        by_day[day].sort(key=lambda b: b["time"])
    return by_day


def price_at_time(bars: list[dict], target: str) -> float | None:
    target_min = time_to_min(target)
    best = None
    best_diff = 9999
    for b in bars:
        parts = b["time"].split(":")
        if len(parts) < 2:
            continue
        bar_min = int(parts[0]) * 60 + int(parts[1])
        if bar_min < target_min:  # 只用已完成的 bar，不含当前未完成 bar
            diff = target_min - bar_min
            if diff < best_diff:
                best_diff = diff
                best = b["close"]
    return best


def bar_time_min(bar: dict) -> int:
    parts = bar["time"].split(":")
    if len(parts) < 2:
        return 0
    return int(parts[0]) * 60 + int(parts[1])


def quote_time_min(dt: str) -> int:
    """'2026-07-13 10:05:00' → 分钟数"""
    if " " not in dt:
        return 0
    return time_to_min(dt.split(" ")[1][:5])


def bars_from_min(bars: list[dict], min_from: int, inclusive: bool = True) -> list[dict]:
    out = []
    for b in bars:
        t = bar_time_min(b)
        if t > min_from or (inclusive and t >= min_from):
            out.append(b)
    return out


def bars_after_time(bars: list[dict], target: str, inclusive: bool = False) -> list[dict]:
    return bars_from_min(bars, time_to_min(target), inclusive=inclusive)


def rank_sectors(
    sectors: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    signal_date: str,
    signal_time: str,
) -> list[tuple[float, dict]]:
    scores = []
    for sec in sectors:
        etf = sec["etf_code"]
        info = etf_daily.get(etf)
        if not info:
            continue
        returns = info["returns"]
        idx_map = {r["date"]: i for i, r in enumerate(returns)}
        bars = etf_5min.get(etf, {}).get(signal_date, [])
        if signal_date not in idx_map and not bars:
            continue
        partial_close, partial_vol = _partial_close_vol(bars, signal_time)

        if signal_date not in idx_map:
            if len(returns) < SCORE_WINDOW + 1:
                continue
            idx = len(returns) - 1
            # 过热过滤（盘中无日K当日的情况）
            ret_3d = sum(r["return_pct"] for r in returns[idx - SCORE_WINDOW + 1 : idx + 1])
            if ret_3d > MAX_3D_RETURN:
                continue
            score = score_at_signal(returns, idx, partial_close, partial_vol)
            if score is None:
                continue
            scores.append((score, sec))
            continue

        idx = idx_map[signal_date]
        if idx < SCORE_WINDOW:
            continue
        # 过热过滤：3日累计涨幅过高则跳过（防跳空回落）
        ret_3d = sum(r["return_pct"] for r in returns[idx - SCORE_WINDOW + 1 : idx + 1])
        if ret_3d > MAX_3D_RETURN:
            continue
        score = score_at_signal(returns, idx, partial_close, partial_vol)
        if score is None:
            continue
        scores.append((score, sec))
    scores.sort(key=lambda x: x[0], reverse=True)
    return scores


def rank_top1(
    sectors: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    signal_date: str,
    signal_time: str,
) -> dict | None:
    scores = rank_sectors(sectors, etf_daily, etf_5min, signal_date, signal_time)
    if len(scores) < 2:
        return None
    return scores[0][1]


def _partial_close_vol(bars: list[dict], cutoff: str) -> tuple[float | None, float]:
    cutoff_min = time_to_min(cutoff)
    close = None
    vol = 0.0
    for b in bars:
        parts = b["time"].split(":")
        if len(parts) < 2:
            continue
        bar_min = int(parts[0]) * 60 + int(parts[1])
        if bar_min < cutoff_min:  # 只用已完成的 bar，不含当前未完成 bar
            close = b["close"]
            vol += b["volume"]
    return close, vol


def signal_baseline(
    etf: str,
    signal_date: str,
    signal_time: str,
    etf_daily: dict,
    etf_5min: dict,
) -> float | None:
    bars = etf_5min.get(etf, {}).get(signal_date, [])
    if time_to_min(signal_time) < time_to_min("09:30"):
        returns = etf_daily[etf]["returns"]
        idx_map = {r["date"]: i for i, r in enumerate(returns)}
        if signal_date in idx_map and idx_map[signal_date] > 0:
            return returns[idx_map[signal_date] - 1]["close"]
        if signal_date in idx_map:
            return returns[idx_map[signal_date]]["close"]
    px = price_at_time(bars, signal_time)
    if px and px > 0:
        return px
    returns = etf_daily[etf]["returns"]
    idx_map = {r["date"]: i for i, r in enumerate(returns)}
    if signal_date in idx_map:
        return returns[idx_map[signal_date]]["close"]
    prior = [r for r in returns if r["date"] < signal_date]
    if prior:
        return prior[-1]["close"]
    return None


def sell_time_min(sell_time: str) -> int:
    """'2026-07-10 10:05:00' → 分钟数"""
    if not sell_time:
        return 0
    t = sell_time.split(" ")[-1][:5]
    return time_to_min(t)


def bars_from_minute(bars: list[dict], min_min: int) -> list[dict]:
    return [b for b in bars if bar_time_min(b) >= min_min]


def next_trading_day(dates: list[str], d: str) -> str | None:
    if d not in dates:
        return None
    i = dates.index(d)
    return dates[i + 1] if i + 1 < len(dates) else None


def apply_net_return(buy_price: float, sell_price: float, fee_pct: float) -> float:
    buy_cost = buy_price * (1 + fee_pct / 100)
    sell_income = sell_price * (1 - fee_pct / 100)
    return (sell_income - buy_cost) / buy_cost * 100


def buy_bars_for_check(
    obs_bars: list[dict],
    day: str,
    signal_date: str,
    signal_time: str,
    sold_at_min: int | None,
    max_min: int | None = None,
) -> list[dict]:
    """仅保留资金可用时段的 bar（卖出日须晚于卖出时刻）。"""
    sig_start = (
        time_to_min("09:30")
        if time_to_min(signal_time) < time_to_min("09:30")
        else time_to_min(signal_time)
    )
    out: list[dict] = []
    for b in obs_bars:
        bday = b["day"]
        if bday < signal_date:
            continue
        bt = bar_time_min(b)
        if bday < day:
            out.append(b)
        elif bday == day:
            start = sig_start if bday == signal_date else 0
            if sold_at_min is not None:
                start = max(start, sold_at_min)
            if bt >= start and (max_min is None or bt < max_min):
                out.append(b)
    return out


def _bars_in_window(
    day_bars: list[dict],
    min_min: int,
    max_min: int | None = None,
) -> list[dict]:
    out = []
    for b in day_bars:
        bt = bar_time_min(b)
        if bt >= min_min and (max_min is None or bt < max_min):
            out.append(b)
    return out


def run_one(
    sectors: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    eval_dates: list[str],
    signal_time: str,
    fee_pct: float,
) -> dict:
    holding: dict | None = None
    pending: dict | None = None
    trades: list[dict] = []
    daily_log: list[dict] = []
    cash_blocked_days = 0

    def log(day: str, time: str, action: str, **extra):
        daily_log.append({"date": day, "time": time, "action": action, **extra})

    sig_min = time_to_min(signal_time)
    open_min = time_to_min("09:30")

    for day in eval_dates:
        sold_at_min: int | None = None

        # 先卖
        if holding and day == holding["sell_date"]:
            sell_bars = etf_5min.get(holding["etf"], {}).get(day, [])
            if sell_bars:
                sell_price, sell_reason, sell_time = check_sell_trigger(
                    sell_bars, holding["buy_price"], 0,
                    STOP_LOSS, TRAIL_TRIGGER, TRAIL_DROP,
                )
                sold_at_min = sell_time_min(sell_time)
                ret = apply_net_return(holding["buy_price"], sell_price, fee_pct)
                st = sell_time.split(" ")[-1][:5] if sell_time else ""
                log(day, st, "卖出",
                    sector=holding["sector"], etf=holding["etf"],
                    price=round(sell_price, 4), reason=sell_reason,
                    return_pct=round(ret, 2),
                    buy_time=holding.get("buy_time", ""))
                trades.append({
                    "signal_date": holding["signal_date"],
                    "sector": holding["sector"],
                    "etf": holding["etf"],
                    "buy_date": holding["buy_date"],
                    "buy_time": holding.get("buy_time", ""),
                    "buy_reason": holding.get("buy_reason", ""),
                    "buy_price": holding["buy_price"],
                    "sell_date": day,
                    "sell_time": sell_time,
                    "sell_price": sell_price,
                    "sell_reason": sell_reason,
                    "return_pct": ret,
                })
            holding = None

        if holding:
            log(day, "15:00", "持仓",
                sector=holding["sector"], etf=holding["etf"],
                sell_date=holding["sell_date"])
            continue

        # 信号（无持仓且无待买）
        if not pending:
            top1 = rank_top1(sectors, etf_daily, etf_5min, day, signal_time)
            if top1:
                etf = top1["etf_code"]
                baseline = signal_baseline(etf, day, signal_time, etf_daily, etf_5min)
                if baseline and baseline > 0:
                    # 前一日涨幅检查：暴涨则跳过（防追高次日暴跌）
                    info = etf_daily.get(etf, {})
                    returns = info.get("returns", [])
                    idx_map = {r["date"]: i for i, r in enumerate(returns)}
                    if day in idx_map and idx_map[day] > 0:
                        prev_ret = returns[idx_map[day] - 1]["return_pct"]
                        if prev_ret > PREV_DAY_SURGE_LIMIT:
                            log(day, signal_time, "信号跳过",
                                sector=top1["name"], etf=etf,
                                reason=f"前日涨{prev_ret:+.1f}%>{PREV_DAY_SURGE_LIMIT}%")
                            continue
                    pending = {
                        "sector": top1["name"],
                        "etf": etf,
                        "baseline": baseline,
                        "signal_date": day,
                        "obs_bars": [],
                    }
                    log(day, signal_time, "信号",
                        sector=top1["name"], etf=etf,
                        baseline=round(baseline, 4), top1_score="v6")

        # 仅信号日当天尝试买入（未触发则收盘兜底）
        if not pending or day != pending["signal_date"]:
            continue

        if sold_at_min is not None and sold_at_min > (open_min if sig_min < open_min else sig_min):
            cash_blocked_days += 1

        holding, pending = _try_buy_same_day(
            pending, day, signal_time, sold_at_min, etf_5min,
            fee_pct, all_dates, log, close_fallback=False,
            etf_daily=etf_daily,
        )

    bought = [t for t in trades if t.get("return_pct") is not None]
    rets = [t["return_pct"] for t in bought]
    stats = _calc_stats(rets) if rets else {}
    equity = 1.0
    for r in rets:
        equity *= 1 + r / 100
    return {
        "signal_time": signal_time,
        "trades": trades,
        "trade_count": len(bought),
        "cash_blocked_days": cash_blocked_days,
        "final_equity_pct": (equity - 1) * 100,
        "stats": stats,
        "daily_log": daily_log,
    }


def _close_price(etf: str, day: str, etf_daily: dict, etf_5min: dict) -> float | None:
    day_bars = etf_5min.get(etf, {}).get(day, [])
    if day_bars:
        return day_bars[-1]["close"]
    returns = etf_daily.get(etf, {}).get("returns", [])
    idx_map = {r["date"]: i for i, r in enumerate(returns)}
    if day in idx_map:
        return returns[idx_map[day]]["close"]
    return None


def _issue_signal(
    sectors: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    day: str,
    signal_time: str,
    log,
) -> dict | None:
    scores = rank_sectors(sectors, etf_daily, etf_5min, day, signal_time)
    if len(scores) < 2:
        return None
    for _, top1 in scores:
        etf = top1["etf_code"]
        baseline = signal_baseline(etf, day, signal_time, etf_daily, etf_5min)
        if not baseline or baseline <= 0:
            continue
        # 前一日涨幅检查
        info = etf_daily.get(etf, {})
        returns = info.get("returns", [])
        idx_map = {r["date"]: i for i, r in enumerate(returns)}
        if day in idx_map and idx_map[day] > 0:
            prev_ret = returns[idx_map[day] - 1]["return_pct"]
            if prev_ret > PREV_DAY_SURGE_LIMIT:
                log(day, signal_time, "信号跳过",
                    sector=top1["name"], etf=etf,
                    reason=f"前日涨{prev_ret:+.1f}%>{PREV_DAY_SURGE_LIMIT}%")
                continue
        log(day, signal_time, "信号",
            sector=top1["name"], etf=etf,
            baseline=round(baseline, 4), top1_score="v6")
        return {
            "sector": top1["name"],
            "etf": etf,
            "baseline": baseline,
            "signal_date": day,
            "signal_time": signal_time,
            "obs_bars": [],
        }
    return None


def _try_buy_same_day(
    pending: dict,
    day: str,
    signal_time: str,
    sold_at_min: int | None,
    etf_5min: dict,
    fee_pct: float,
    all_dates: list[str],
    log,
    *,
    close_fallback: bool,
    etf_daily: dict | None = None,
) -> tuple[dict | None, dict | None]:
    """尝试信号日买入。返回 (holding, pending_remain)。"""
    sig_min = time_to_min(signal_time)
    open_min = time_to_min("09:30")
    min_buy_min = open_min if sig_min < open_min else sig_min
    if sold_at_min is not None:
        if sold_at_min > min_buy_min:
            sell_hm = f"{sold_at_min // 60:02d}:{sold_at_min % 60:02d}"
            log(day, signal_time, "资金等待",
                reason=f"须先卖出，{sell_hm} 后方可买",
                pending_sector=pending["sector"])
        min_buy_min = max(min_buy_min, sold_at_min)

    day_bars = etf_5min.get(pending["etf"], {}).get(day, [])
    new_bars = bars_from_minute(day_bars, min_buy_min)
    pending["obs_bars"].extend(new_bars)
    check_bars = buy_bars_for_check(
        pending["obs_bars"], day, pending["signal_date"], signal_time, sold_at_min,
    )
    buy_price, buy_reason, buy_time = check_buy_trigger(
        check_bars, pending["baseline"], BUY_UP, BUY_DOWN, BUY_REBOUND,
    )

    if buy_price is None and close_fallback:
        close_px = _close_price(pending["etf"], day, etf_daily or {}, etf_5min)
        if close_px and close_px > 0:
            if sold_at_min is None or sold_at_min <= time_to_min("15:00"):
                buy_price = close_px
                buy_reason = "收盘兜底"
                buy_time = f"{day} 15:00:00"

    if buy_price is None:
        log(day, "15:00", "观察",
            sector=pending["sector"], etf=pending["etf"],
            baseline=round(pending["baseline"], 4),
            status="未触发买入")
        return None, None

    buy_dt_min = sell_time_min(buy_time) if buy_time else min_buy_min
    if sold_at_min is not None and buy_dt_min < sold_at_min:
        if close_fallback:
            close_px = _close_price(pending["etf"], day, etf_daily or {}, etf_5min)
            if close_px and close_px > 0:
                buy_price = close_px
                buy_reason = "收盘兜底"
                buy_time = f"{day} 15:00:00"
            else:
                log(day, "15:00", "观察",
                    sector=pending["sector"], etf=pending["etf"],
                    baseline=round(pending["baseline"], 4),
                    status="未触发买入")
                return None, None
        else:
            log(day, "15:00", "观察",
                sector=pending["sector"], etf=pending["etf"],
                baseline=round(pending["baseline"], 4),
                status="未触发买入")
            return None, None

    buy_date = buy_time.split(" ")[0] if buy_time else day
    bt = buy_time.split(" ")[-1][:5] if buy_time else ""
    sell_date = next_trading_day(all_dates, buy_date)
    if not sell_date:
        return None, None

    log(buy_date, bt, "买入",
        sector=pending["sector"], etf=pending["etf"],
        price=round(buy_price, 4), reason=buy_reason,
        signal_date=pending["signal_date"],
        sell_date=sell_date)
    holding = {
        "sector": pending["sector"],
        "etf": pending["etf"],
        "signal_date": pending["signal_date"],
        "baseline": pending["baseline"],
        "buy_price": buy_price,
        "buy_date": buy_date,
        "buy_time": buy_time,
        "buy_reason": buy_reason,
        "sell_date": sell_date,
    }
    return holding, None


def run_one_post_sell(
    sectors: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    eval_dates: list[str],
    fee_pct: float,
) -> dict:
    """组 6：卖出后发信号；信号日未触发则收盘兜底买入。"""
    holding: dict | None = None
    pending: dict | None = None
    trades: list[dict] = []
    daily_log: list[dict] = []
    cash_blocked_days = 0
    need_flat_signal = True

    def log(day: str, time: str, action: str, **extra):
        daily_log.append({"date": day, "time": time, "action": action, **extra})

    for day in eval_dates:
        sold_at_min: int | None = None

        if holding and day == holding["sell_date"]:
            sell_bars = etf_5min.get(holding["etf"], {}).get(day, [])
            if sell_bars:
                sell_price, sell_reason, sell_time = check_sell_trigger(
                    sell_bars, holding["buy_price"], 0,
                    STOP_LOSS, TRAIL_TRIGGER, TRAIL_DROP,
                )
                sold_at_min = sell_time_min(sell_time)
                ret = apply_net_return(holding["buy_price"], sell_price, fee_pct)
                st = sell_time.split(" ")[-1][:5] if sell_time else ""
                log(day, st, "卖出",
                    sector=holding["sector"], etf=holding["etf"],
                    price=round(sell_price, 4), reason=sell_reason,
                    return_pct=round(ret, 2),
                    buy_time=holding.get("buy_time", ""))
                trades.append({
                    "signal_date": holding["signal_date"],
                    "sector": holding["sector"],
                    "etf": holding["etf"],
                    "buy_date": holding["buy_date"],
                    "buy_time": holding.get("buy_time", ""),
                    "buy_reason": holding.get("buy_reason", ""),
                    "buy_price": holding["buy_price"],
                    "sell_date": day,
                    "sell_time": sell_time,
                    "sell_price": sell_price,
                    "sell_reason": sell_reason,
                    "return_pct": ret,
                })
                signal_time = f"{sold_at_min // 60:02d}:{sold_at_min % 60:02d}"
                pending = _issue_signal(
                    sectors, etf_daily, etf_5min, day, signal_time, log,
                )
                need_flat_signal = pending is None
            holding = None

        if holding:
            log(day, "15:00", "持仓",
                sector=holding["sector"], etf=holding["etf"],
                sell_date=holding["sell_date"])
            continue

        if pending and day > pending["signal_date"]:
            log(day, "09:30", "放弃信号",
                sector=pending["sector"], etf=pending["etf"],
                reason="信号日未成交")
            pending = None
            need_flat_signal = True

        if need_flat_signal and not pending:
            pending = _issue_signal(
                sectors, etf_daily, etf_5min, day, "09:30", log,
            )
            need_flat_signal = pending is None

        if not pending or day != pending["signal_date"]:
            continue

        if sold_at_min is not None and sold_at_min > time_to_min(pending["signal_time"]):
            cash_blocked_days += 1

        holding, pending = _try_buy_same_day(
            pending, day, pending["signal_time"], sold_at_min, etf_5min,
            fee_pct, all_dates, log, close_fallback=False,
            etf_daily=etf_daily,
        )
        if not holding:
            need_flat_signal = True

    bought = [t for t in trades if t.get("return_pct") is not None]
    rets = [t["return_pct"] for t in bought]
    stats = _calc_stats(rets) if rets else {}
    equity = 1.0
    for r in rets:
        equity *= 1 + r / 100
    return {
        "signal_time": "卖出后发信号",
        "trades": trades,
        "trade_count": len(bought),
        "cash_blocked_days": cash_blocked_days,
        "final_equity_pct": (equity - 1) * 100,
        "stats": stats,
        "daily_log": daily_log,
    }


def run_all_groups(
    sectors: list[dict],
    etf_daily: dict,
    etf_5min: dict,
    all_dates: list[str],
    eval_dates: list[str],
    fee_pct: float,
) -> list[dict]:
    results = []
    for signal_time in SIGNAL_TIMES:
        results.append(run_one(
            sectors, etf_daily, etf_5min, all_dates, eval_dates,
            signal_time, fee_pct,
        ))
    results.append(run_one_post_sell(
        sectors, etf_daily, etf_5min, all_dates, eval_dates, fee_pct,
    ))
    return results


def load_market_data(sectors: list[dict], lookback: int) -> tuple[dict, dict, list[str]]:
    etf_codes = sorted({s["etf_code"] for s in sectors})
    datalen = lookback + 15
    datalen_5m = min(lookback * 50 + 200, 5000)

    etf_daily: dict = {}
    etf_5min: dict = {}
    print(f">>> 拉取 {len(etf_codes)} 只 ETF 日K + 5分K (约 {lookback} 交易日)...")
    for i, etf in enumerate(etf_codes):
        raw = next(s["etf_raw"] for s in sectors if s["etf_code"] == etf)
        sym = etf_to_sina_symbol(raw)
        daily = fetch_sina_kline(sym, datalen=datalen)
        if daily and len(daily) > SCORE_WINDOW + 2:
            etf_daily[etf] = {"returns": compute_daily_data(daily)}
        m5 = fetch_5min_kline(sym, datalen=datalen_5m)
        if m5:
            etf_5min[etf] = normalize_5min_bars(m5)
        if (i + 1) % 10 == 0:
            print(f"    进度 {i+1}/{len(etf_codes)} 日K={len(etf_daily)} 5分K={len(etf_5min)}")

    all_dates = sorted({
        r["date"] for info in etf_daily.values() for r in info["returns"]
    })
    m5_dates = sorted({d for bars in etf_5min.values() for d in bars})
    if m5_dates:
        all_dates = sorted(set(all_dates) | set(m5_dates))
    return etf_daily, etf_5min, all_dates


def format_daily_report(result: dict) -> str:
    """按天格式化操作明细。"""
    lines = [
        f"### {result['signal_time']} "
        f"(累计 {result['final_equity_pct']:+.2f}%, {result['trade_count']}笔)",
        "",
    ]
    by_day: dict[str, list[dict]] = {}
    for ev in result.get("daily_log", []):
        by_day.setdefault(ev["date"], []).append(ev)

    cum = 1.0
    for day in sorted(by_day):
        lines.append(f"#### {day}")
        for ev in by_day[day]:
            t = ev.get("time") or "—"
            act = ev["action"]
            if act == "信号":
                lines.append(
                    f"  {t} 【信号】TOP1 {ev['sector']} {ev['etf']} "
                    f"基准价 {ev['baseline']}"
                )
            elif act == "信号跳过":
                lines.append(
                    f"  {t} 【信号跳过】{ev['reason']} "
                    f"(持仓 {ev['sector']} {ev['etf']})"
                )
            elif act == "资金等待":
                lines.append(f"  {t} 【资金等待】{ev['reason']} → 待买 {ev['pending_sector']}")
            elif act == "买入":
                lines.append(
                    f"  {t} 【买入】{ev['sector']} {ev['etf']} "
                    f"@{ev['price']} ({ev['reason']}) "
                    f"→ 计划 {ev['sell_date']} 卖"
                )
            elif act == "卖出":
                cum *= 1 + ev["return_pct"] / 100
                lines.append(
                    f"  {t} 【卖出】{ev['sector']} {ev['etf']} "
                    f"@{ev['price']} ({ev['reason']}) "
                    f"收益 {ev['return_pct']:+.2f}% | 累计 {(cum-1)*100:+.2f}%"
                )
            elif act == "持仓":
                lines.append(
                    f"  {t} 【持仓】{ev['sector']} {ev['etf']} "
                    f"→ {ev['sell_date']} 卖"
                )
            elif act == "观察":
                lines.append(
                    f"  {t} 【观察】{ev['sector']} {ev['etf']} "
                    f"基准 {ev['baseline']} — 未触发买入"
                )
            elif act == "放弃信号":
                lines.append(
                    f"  {t} 【放弃】{ev['sector']} {ev['etf']} — {ev['reason']}"
                )
            else:
                lines.append(f"  {t} {act} {ev}")
        lines.append("")
    return "\n".join(lines)


def save_daily_reports(results: list[dict], eval_days: int) -> Path:
    out_dir = Path.home() / ".tradingagents" / "rotation"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out = out_dir / f"backtest_6sig_daily_{ts}.md"
    parts = [
        f"# 板块轮动 6 组回测 — 按天明细",
        f"",
        f"区间: 最近 {eval_days} 个交易日 | 手续费万3 | T+1 | 先卖后买 | 未触发则放弃",
        f"",
    ]
    for r in results:
        parts.append(format_daily_report(r))
        parts.append("---")
        parts.append("")
    out.write_text("\n".join(parts), encoding="utf-8")
    return out


def print_report(results: list[dict], eval_days: int):
    print()
    print("=" * 80)
    print("  板块轮动 6 组回测对比（平安板块池，未触发则放弃）")
    print("=" * 80)
    print(f"  区间: 最近 {eval_days} 个交易日 | 手续费万3双边 | T+1卖出 | 单仓位 | 先卖后买")
    sl_display = "无止损" if STOP_LOSS <= -50 else f"止{STOP_LOSS}%"
    dip_display = "不抄底" if BUY_DOWN > 50 else f"跌{BUY_DOWN}%回弹"
    surge_display = f"前日涨>{PREV_DAY_SURGE_LIMIT}%跳过" if PREV_DAY_SURGE_LIMIT < 50 else ""
    print(f"  买入: 涨{BUY_UP}% {dip_display} | 卖出: 追踪+{TRAIL_TRIGGER}%落{TRAIL_DROP}% {sl_display}")
    note = "未触发则放弃"
    if surge_display:
        note += f" | {surge_display}"
    print(f"  注: {note}")
    print()
    print(f"  {'信号':>6} {'成交':>4} {'累计收益':>10} {'胜率':>8} {'均笔':>8} {'资金阻塞':>6}")
    print("  " + "-" * 58)
    for r in sorted(results, key=lambda x: (-x["final_equity_pct"], x["signal_time"])):
        st = r["stats"]
        print(
            f"  {r['signal_time']:>6} {r['trade_count']:4d} "
            f"{r['final_equity_pct']:+9.2f}% "
            f"{st.get('win_rate', 0):7.1f}% "
            f"{st.get('avg', 0):+7.2f}% "
            f"{r.get('cash_blocked_days', 0):6d}天"
        )
    print("=" * 80)

    best = max(results, key=lambda x: x["final_equity_pct"])
    print(
        f"\n最优: {best['signal_time']} "
        f"→ 累计 {best['final_equity_pct']:+.2f}% ({best['trade_count']} 笔)"
    )


def main():
    parser = argparse.ArgumentParser(description="板块轮动 6 组回测")
    parser.add_argument("--days", type=int, default=30, help="回测交易日数（默认30）")
    parser.add_argument("--fee", type=float, default=FEE_PCT, help="单边手续费(默认0.03=万3)")
    parser.add_argument("--daily", action="store_true", help="输出按天明细到 markdown 文件")
    args = parser.parse_args()

    sectors = load_pingan_sectors()
    print(f"=== 板块轮动 6 组回测 ===")
    print(f"板块池: 平安 {len(sectors)} 个 | 回测 {args.days} 日 | 手续费万{args.fee * 100:.0f}")
    print(f"信号: {', '.join(SIGNAL_TIMES)} + 卖出后动态 | 未触发则放弃")
    print(f"选股: 盘中 partial v6；开盘前 T-1 日 v6")
    print()

    etf_daily, etf_5min, all_dates = load_market_data(sectors, args.days)
    if len(etf_daily) < 10:
        print("ERROR: 日K数据不足")
        sys.exit(1)

    eval_dates = all_dates[-args.days:]
    if len(eval_dates) < 5:
        print("ERROR: 有效交易日不足")
        sys.exit(1)
    print(f"    日K {len(etf_daily)} ETF, 5分K {len(etf_5min)} ETF, 回测 {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}日)")

    results = run_all_groups(
        sectors, etf_daily, etf_5min, all_dates, eval_dates, args.fee,
    )
    print_report(results, len(eval_dates))

    out = Path.home() / ".tradingagents" / "rotation" / f"backtest_6sig_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = [{
        "signal_time": r["signal_time"],
        "final_equity_pct": r["final_equity_pct"],
        "trade_count": r["trade_count"],
        "cash_blocked_days": r["cash_blocked_days"],
        "stats": r["stats"],
        "trades": r["trades"],
        "daily_log": r.get("daily_log", []),
    } for r in results]
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")

    if args.daily:
        daily_path = save_daily_reports(results, len(eval_dates))
        print(f"按天明细: {daily_path}")


if __name__ == "__main__":
    main()

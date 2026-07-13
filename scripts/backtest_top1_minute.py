#!/usr/bin/env python3
"""板块轮动 TOP1 分钟线精确回测

用 5 分钟 K 线模拟真实盘中走势，精确回测追踪止盈策略。

策略：
1. 每日 14:50，根据 v6 公式选出 TOP1 板块的 ETF
2. 以当日收盘价买入
3. 次日盘中逐根 5 分钟 K 线模拟：
   a. 检查是否触发止损（最低价 ≤ 买入价 × (1 + stop_loss%)）
   b. 检查是否触发追踪止盈（最高价 ≥ 触发价后，从最高点回落 trail_drop%）
   c. 都没触发则收盘卖

用法:
    python scripts/backtest_top1_minute.py
    python scripts/backtest_top1_minute.py --lookback 30 --trail-trigger 3.0 --trail-drop 0.5 --stop-loss -0.5
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from sector_etf_map import etf_to_sina_symbol, load_pingan_sectors  # noqa: E402

PROXY = os.environ.get("ROTATION_PROXY", "http://127.0.0.1:7890")
SINA_INTERVAL = 0.2
TIMEOUT = 15

SCORE_WINDOW = 3
VOL_THRESHOLD = 1.5
VOL_AVG_PERIOD = 5
VOL_BASE = 0.3


def curl_get(url: str) -> str:
    for use_proxy in [False, True]:
        cmd = ["curl", "-s", "--connect-timeout", str(TIMEOUT)]
        if use_proxy:
            cmd += ["-x", PROXY]
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 10)
            for enc in ["gbk", "utf-8"]:
                try:
                    text = r.stdout.decode(enc)
                    if text and len(text) > 10:
                        return text
                except (UnicodeDecodeError, AttributeError):
                    continue
        except subprocess.TimeoutExpired:
            continue
    return ""


def fetch_daily_kline(symbol: str, datalen: int = 30) -> list[dict]:
    """拉日 K 线用于计算 v6 得分选 TOP1。"""
    sina_sym = etf_to_sina_symbol(symbol)
    url = (
        f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={sina_sym}&scale=240&ma=no&datalen={datalen}"
    )
    raw = curl_get(url)
    time.sleep(SINA_INTERVAL)
    if not raw or raw.strip() in ("null", "", "[]"):
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def fetch_5min_kline(symbol: str, datalen: int = 1440, scale: int = 5) -> list[dict]:
    """拉分钟 K 线用于精确模拟次日盘中走势。

    scale: 5=5分钟, 15=15分钟, 30=30分钟, 60=60分钟
    datalen: 最大 5000（API 上限）
    """
    sina_sym = etf_to_sina_symbol(symbol)
    datalen = min(datalen, 5000)
    url = (
        f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={sina_sym}&scale={scale}&ma=no&datalen={datalen}"
    )
    raw = curl_get(url)
    time.sleep(SINA_INTERVAL)
    if not raw or raw.strip() in ("null", "", "[]"):
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def compute_daily_from_klines(klines: list[dict]) -> list[dict]:
    """从日 K 线计算日收益率 + 成交量。"""
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
            prev_close = float(klines[i - 1].get("close", 0))
            ret = ((close - prev_close) / prev_close * 100) if prev_close else 0.0
        result.append({
            "date": k.get("day", ""), "close": close,
            "return_pct": ret, "volume": volume,
        })
    return result


def _ema(values: list[float], period: int) -> list[float]:
    """计算 EMA。"""
    if not values:
        return []
    ema = [values[0]]
    k = 2 / (period + 1)
    for i in range(1, len(values)):
        ema.append(values[i] * k + ema[-1] * (1 - k))
    return ema


def calc_trix(closes: list[float], period: int = 12) -> list[float]:
    """计算 TRIX 指标。

    TRIX = 三重平滑 EMA 的变化率
    1. EMA1 = EMA(close, period)
    2. EMA2 = EMA(EMA1, period)
    3. EMA3 = EMA(EMA2, period)
    4. TRIX = (EMA3 - EMA3_prev) / EMA3_prev * 100

    TRIX 死叉: TRIX 下穿其 signal（TRIX 的 EMA）
    """
    if len(closes) < period * 3 + 1:
        return [0.0] * len(closes)

    ema1 = _ema(closes, period)
    ema2 = _ema(ema1, period)
    ema3 = _ema(ema2, period)

    trix = [0.0] * len(closes)
    for i in range(1, len(ema3)):
        if ema3[i - 1] != 0:
            trix[i] = (ema3[i] - ema3[i - 1]) / ema3[i - 1] * 100

    return trix


def calc_trix_signal(trix: list[float], signal_period: int = 9) -> list[float]:
    """计算 TRIX 的 signal 线（TRIX 的 EMA）。"""
    return _ema(trix, signal_period)


def calc_obv(bars: list[dict]) -> list[float]:
    """计算 OBV（On Balance Volume）。

    OBV 逻辑：
    - 收盘价上涨 → OBV += 成交量
    - 收盘价下跌 → OBV -= 成交量
    - 收盘价不变 → OBV 不变
    """
    obv = [0.0]
    for i in range(1, len(bars)):
        prev_close = float(bars[i - 1].get("close", 0))
        curr_close = float(bars[i].get("close", 0))
        vol = float(bars[i].get("volume", 0))
        if curr_close > prev_close:
            obv.append(obv[-1] + vol)
        elif curr_close < prev_close:
            obv.append(obv[-1] - vol)
        else:
            obv.append(obv[-1])
    return obv


def find_first_obv_death_cross(bars: list[dict], ma_period: int = 5):
    """找第一个 OBV 死叉（OBV 下穿其 MA）。

    OBV 死叉 = OBV 从上方下穿 OBV 的移动平均线。
    """
    if len(bars) < ma_period + 1:
        return None

    obv = calc_obv(bars)
    obv_ma = []
    for i in range(len(obv)):
        if i < ma_period - 1:
            obv_ma.append(obv[i])
        else:
            window = obv[i - ma_period + 1:i + 1]
            obv_ma.append(sum(window) / len(window))

    for i in range(ma_period, len(obv)):
        if obv[i - 1] >= obv_ma[i - 1] and obv[i] < obv_ma[i]:
            return i, float(bars[i].get("close", 0))

    return None


def find_first_trix_death_cross(bars: list[dict], period: int = 12,
                                 signal_period: int = 9):
    """在 5 分钟 K 线中找到第一个 TRIX 死叉（TRIX 下穿 signal）。

    需要足够的历史 bar 来预热 EMA，我们用前一日收盘前 + 当日全部 bar。
    bars: 包含前一日最后 N 根 + 当日全部 5 分钟 K 线

    返回: (死叉发生的 bar 索引, 死叉时的收盘价) 或 None
    """
    if len(bars) < period * 4:
        return None

    closes = [float(b.get("close", 0)) for b in bars]
    trix = calc_trix(closes, period)
    signal = calc_trix_signal(trix, signal_period)

    # 找第一个死叉：前一根 TRIX >= signal，当前根 TRIX < signal
    for i in range(max(period * 4, 1), len(bars)):
        if trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
            return i, closes[i]

    return None


def compute_v6_score(returns: list[dict], idx: int) -> float:
    if idx < SCORE_WINDOW:
        return 0.0
    ret_w = sum(r["return_pct"] for r in returns[idx - SCORE_WINDOW + 1:idx + 1])
    vol_today = returns[idx].get("volume", 0)
    vol_prev = [returns[j].get("volume", 0)
                for j in range(max(0, idx - VOL_AVG_PERIOD), idx)]
    avg_vol = sum(vol_prev) / len(vol_prev) if vol_prev and sum(vol_prev) > 0 else vol_today
    vol_ratio = vol_today / avg_vol if avg_vol > 0 else 1.0
    vol_factor = VOL_BASE + (1 - VOL_BASE) * min(vol_ratio / VOL_THRESHOLD, 1.0)
    return ret_w * vol_factor


# ── 分钟线精确回测引擎 ────────────────────────────────

def simulate_next_day_trailing(buy_cost: float, min_bars: list[dict],
                               trail_trigger: float, trail_drop: float,
                               stop_loss: float, fee_pct: float = 0.0) -> tuple[float, str, dict]:
    """用 5 分钟 K 线精确模拟次日盘中追踪止盈。

    min_bars: 次日所有 5 分钟 K 线 [{day, open, high, low, close, volume}, ...]
    返回: (收益率%, 卖出原因, 详情)
    """
    trigger_price = buy_cost * (1 + trail_trigger / 100)
    stop_price = buy_cost * (1 + stop_loss / 100) if stop_loss < 0 else 0

    tracking = False  # 是否已触发追踪
    peak_high = 0.0   # 触发追踪后的最高价

    for bar in min_bars:
        bar_high = float(bar.get("high", 0))
        bar_low = float(bar.get("low", 0))
        bar_close = float(bar.get("close", 0))

        # 检查这根K线是否同时触及止损和触发价（K线内路径不确定）
        stop_hit = stop_loss < 0 and bar_low <= stop_price
        trigger_hit = bar_high >= trigger_price

        if stop_hit and trigger_hit and not tracking:
            # K线内既可能先止损也可能先触发追踪，用平均价模拟
            avg_price = (stop_price + trigger_price) / 2
            ret = (avg_price - buy_cost) / buy_cost * 100
            return ret, "ambiguous", {"sell_price": avg_price, "bar": bar["day"],
                                       "stop_price": stop_price, "trigger_price": trigger_price}

        # 1. 先检查止损（每根 K 线的最低价）
        if stop_loss < 0 and bar_low <= stop_price:
            ret = (stop_price - buy_cost) / buy_cost * 100
            return ret, "stop_loss", {"sell_price": stop_price, "bar": bar["day"]}

        # 2. 检查是否触发追踪
        if not tracking and bar_high >= trigger_price:
            tracking = True
            peak_high = bar_high
        elif tracking:
            if bar_high > peak_high:
                peak_high = bar_high

        # 3. 如果在追踪模式，检查是否从最高点回落
        if tracking:
            trail_sell_price = peak_high * (1 - trail_drop / 100)
            if bar_low <= trail_sell_price:
                # 检查是否同时创了新高（K线内先涨后跌 vs 先跌后涨不确定）
                if bar_high > peak_high:
                    # 用新高和回落价的平均
                    avg_sell = (bar_high + trail_sell_price) / 2
                    ret = (avg_sell - buy_cost) / buy_cost * 100
                    return ret, "trailing_stop", {"sell_price": avg_sell,
                                                   "peak_high": bar_high, "bar": bar["day"],
                                                   "note": "avg_high_low"}
                ret = (trail_sell_price - buy_cost) / buy_cost * 100
                return ret, "trailing_stop", {"sell_price": trail_sell_price,
                                               "peak_high": peak_high, "bar": bar["day"]}

    # 4. 到收盘都没触发，按收盘价卖
    last_close = float(min_bars[-1].get("close", 0)) if min_bars else buy_cost
    ret = (last_close - buy_cost) / buy_cost * 100
    return ret, "close", {"sell_price": last_close, "bar": min_bars[-1]["day"] if min_bars else ""}


def simulate_next_day_fixed(buy_cost: float, min_bars: list[dict],
                            take_profit: float, stop_loss: float) -> tuple[float, str]:
    """用 5 分钟 K 线精确模拟次日固定止盈止损。"""
    tp_price = buy_cost * (1 + take_profit / 100) if take_profit > 0 else 0
    sl_price = buy_cost * (1 + stop_loss / 100) if stop_loss < 0 else 0

    for bar in min_bars:
        bar_high = float(bar.get("high", 0))
        bar_low = float(bar.get("low", 0))

        tp_hit = tp_price > 0 and bar_high >= tp_price
        sl_hit = sl_price > 0 and bar_low <= sl_price

        if tp_hit and sl_hit:
            return stop_loss, "stop_loss"
        elif tp_hit:
            return take_profit, "take_profit"
        elif sl_hit:
            return stop_loss, "stop_loss"

    last_close = float(min_bars[-1].get("close", 0)) if min_bars else buy_cost
    ret = (last_close - buy_cost) / buy_cost * 100
    return ret, "close"


def simulate_next_day_trix_cross(buy_cost: float, min_bars_today: list[dict],
                                  min_bars_next: list[dict],
                                  trix_period: int = 5) -> tuple[float, str, dict]:
    """用分钟 K 线模拟次日 TRIX 死叉卖出。

    策略：
    1. 用前一日全部 K 线 + 次日全部 K 线计算 TRIX
    2. 次日开盘后逐根检查，第一个 TRIX 死叉就卖出
    3. 如果全天没有死叉，收盘卖

    trix_period: TRIX EMA 周期（默认 5，适合分钟线）
    """
    # 拼接：前一日全部 + 次日全部
    all_bars = min_bars_today + min_bars_next

    # TRIX 需要三重 EMA，period=5 → 至少需要 5*3=15 根预热
    min_warmup = trix_period * 3 + 5
    if len(all_bars) < min_warmup:
        last_close = float(min_bars_next[-1].get("close", 0)) if min_bars_next else buy_cost
        ret = (last_close - buy_cost) / buy_cost * 100
        return ret, "close", {"reason": "insufficient_data"}

    warmup_len = len(min_bars_today)

    closes = [float(b.get("close", 0)) for b in all_bars]
    trix = calc_trix(closes, trix_period)
    signal = calc_trix_signal(trix, max(trix_period // 2, 3))

    # 在次日 bars 范围内找第一个死叉
    search_start = max(warmup_len, min_warmup)
    for i in range(search_start, len(all_bars)):
        if trix[i - 1] >= signal[i - 1] and trix[i] < signal[i]:
            sell_price = closes[i]
            ret = (sell_price - buy_cost) / buy_cost * 100
            bar_time = all_bars[i].get("day", "")
            return ret, "trix_death_cross", {
                "sell_price": sell_price, "bar": bar_time,
                "trix": trix[i], "signal": signal[i],
            }

    # 没有死叉，收盘卖
    last_close = closes[-1] if closes else buy_cost
    ret = (last_close - buy_cost) / buy_cost * 100
    return ret, "close", {"reason": "no_death_cross"}


def simulate_next_day_obv_cross(buy_cost: float, min_bars_today: list[dict],
                                  min_bars_next: list[dict],
                                  ma_period: int = 5) -> tuple[float, str, dict]:
    """用分钟 K 线模拟次日 OBV 死叉卖出。

    策略：
    1. 用前一日 K 线 + 次日 K 线计算 OBV
    2. 次日开盘后逐根检查，第一个 OBV 死叉（OBV 下穿其 MA）就卖出
    3. 如果全天没有死叉，收盘卖
    """
    all_bars = min_bars_today + min_bars_next
    if len(all_bars) < ma_period + 2:
        last_close = float(min_bars_next[-1].get("close", 0)) if min_bars_next else buy_cost
        ret = (last_close - buy_cost) / buy_cost * 100
        return ret, "close", {"reason": "insufficient_data"}

    warmup_len = len(min_bars_today)
    obv = calc_obv(all_bars)
    obv_ma = []
    for i in range(len(obv)):
        if i < ma_period - 1:
            obv_ma.append(obv[i])
        else:
            window = obv[i - ma_period + 1:i + 1]
            obv_ma.append(sum(window) / len(window))

    search_start = max(warmup_len, ma_period + 1)
    for i in range(search_start, len(obv)):
        if obv[i - 1] >= obv_ma[i - 1] and obv[i] < obv_ma[i]:
            sell_price = float(all_bars[i].get("close", 0))
            ret = (sell_price - buy_cost) / buy_cost * 100
            bar_time = all_bars[i].get("day", "")
            return ret, "obv_death_cross", {
                "sell_price": sell_price, "bar": bar_time,
                "obv": obv[i], "obv_ma": obv_ma[i],
            }

    last_close = float(all_bars[-1].get("close", 0)) if all_bars else buy_cost
    ret = (last_close - buy_cost) / buy_cost * 100
    return ret, "close", {"reason": "no_death_cross"}


def run_minute_backtest(etf_daily: dict, etf_5min: dict, top_n: int = 1,
                        trail_trigger: float = 3.0, trail_drop: float = 0.5,
                        stop_loss: float = -0.5,
                        take_profit: float = 0.0,
                        use_trix: bool = False,
                        use_obv: bool = False,
                        fee_pct: float = 0.0,
                        sell_time: str = "",
                        start_date: str = "",
                        end_date: str = "",
                        buy_time: str = "14:55") -> list[dict]:
    """分钟线精确回测。

    流程：
    1. 14:50 根据当日 v6 得分选 TOP1 ETF
    2. 14:55 以当日 14:55 的 5 分钟 K 线收盘价买入
    3. 次日全天 5 分钟 K 线模拟卖出

    支持策略：
    - 收盘卖（默认）
    - 追踪止盈（trail_trigger > 0）
    - 固定止盈止损（take_profit > 0）
    - TRIX 死叉卖出（use_trix=True）
    """
    all_dates = set()
    for info in etf_daily.values():
        for r in info["returns"]:
            all_dates.add(r["date"])
    all_dates = sorted(all_dates)

    eval_dates = all_dates[SCORE_WINDOW:]
    if start_date:
        eval_dates = [d for d in eval_dates if d >= start_date]
    if end_date:
        eval_dates = [d for d in eval_dates if d <= end_date]

    trades = []
    for date in eval_dates:
        date_idx = all_dates.index(date)
        next_date = all_dates[date_idx + 1] if date_idx + 1 < len(all_dates) else None

        # 计算 v6 得分选 TOP1
        # 关键：如果指定了 buy_time（如 14:55），信号计算时间 = buy_time - 5min
        # 用当日该时间点之前的分钟 K 线拼接出"部分日 K 线"来算得分
        # 而不是用完整日 K 线（含尾盘 5 分钟）
        signal_time = ""
        if buy_time and buy_time != "15:00":
            # 信号时间 = 买入时间 - 5 分钟
            parts = buy_time.split(":")
            signal_min = int(parts[0]) * 60 + int(parts[1]) - 5
            signal_time = f"{signal_min // 60:02d}:{signal_min % 60:02d}"

        scores = []
        for code, info in etf_daily.items():
            returns = info["returns"]
            idx_map = {r["date"]: i for i, r in enumerate(returns)}
            if date not in idx_map:
                continue
            idx = idx_map[date]
            if idx < SCORE_WINDOW:
                continue

            if signal_time:
                # 用分钟线重构当日部分 K 线数据
                min_info = etf_5min.get(code, {})
                bars_by_date = min_info.get("bars_by_date", {})
                today_bars = bars_by_date.get(date, [])

                # 找到信号时间点之前的所有 bar
                partial_bars = []
                for bar in today_bars:
                    bar_time = bar.get("day", "")
                    time_part = bar_time[11:16] if len(bar_time) > 15 else ""
                    if time_part and time_part <= signal_time:
                        partial_bars.append(bar)

                if partial_bars and len(partial_bars) >= 5:
                    # 用部分 bar 构建当日"截至信号时间"的 OHLCV
                    closes = [float(b.get("close", 0)) for b in partial_bars]
                    vols = [float(b.get("volume", 0)) for b in partial_bars]
                    partial_close = closes[-1]
                    partial_vol = sum(vols)

                    # 替换当日数据
                    modified_returns = list(returns)  # 浅拷贝
                    if idx > 0:
                        prev_close = returns[idx - 1]["close"]
                        partial_ret = ((partial_close - prev_close) / prev_close * 100) if prev_close else 0.0
                    else:
                        partial_ret = 0.0
                    modified_returns[idx] = {
                        "date": returns[idx]["date"],
                        "close": partial_close,
                        "return_pct": partial_ret,
                        "volume": partial_vol,
                    }

                    score = compute_v6_score(modified_returns, idx)
                else:
                    score = compute_v6_score(returns, idx)
            else:
                score = compute_v6_score(returns, idx)

            scores.append((code, info["name"], info["etf_code"], score))

        if len(scores) < top_n * 2:
            print(f"    [DEBUG] {date} 跳过: 可计算v6得分的ETF只有 {len(scores)} 个，需要至少 {top_n * 2} 个")
            continue

        scores.sort(key=lambda x: x[3], reverse=True)

        for rank in range(top_n):
            code, name, etf_code, score = scores[rank]

            min_info = etf_5min.get(code, {})
            bars_by_date = min_info.get("bars_by_date", {})

            # 获取当日分钟 K 线，根据 buy_time 找买入价
            today_bars = bars_by_date.get(date, [])
            buy_price = None

            if buy_time == "15:00":
                if today_bars:
                    buy_price = float(today_bars[-1].get("close", 0))
            else:
                # 找 buy_time 对应的 K 线收盘价
                for bar in today_bars:
                    bar_time = bar.get("day", "")
                    time_part = bar_time[11:16] if len(bar_time) > 15 else ""
                    if time_part == buy_time or (time_part and time_part <= buy_time and time_part >= "14:3"):
                        buy_price = float(bar.get("close", 0))
                # 如果没找到，用最接近的
                if buy_price is None and today_bars:
                    buy_price = float(today_bars[-1].get("close", 0))

            if buy_price is None or buy_price <= 0:
                # 回退到日 K 收盘价
                returns = etf_daily[code]["returns"]
                idx_map = {r["date"]: i for i, r in enumerate(returns)}
                if date in idx_map:
                    buy_price = returns[idx_map[date]]["close"]
                else:
                    continue

            buy_cost = buy_price

            # 最后一天（无次日数据）：只记录买入，卖点显示"-"
            if not next_date:
                trades.append({
                    "date": date,
                    "next_date": "",
                    "sector": name,
                    "etf_code": etf_code,
                    "score": score,
                    "buy_price": buy_cost,
                    "buy_time": buy_time,
                    "ret_high": 0.0,
                    "ret_close": 0.0,
                    "ret_minute": 0.0,
                    "sell_reason": "pending",
                    "detail": {},
                })
                continue

            # 获取次日分钟 K 线
            next_bars = bars_by_date.get(next_date, [])
            if not next_bars:
                print(f"    [DEBUG] {date} 跳过: TOP1 {etf_code} {name} 在次日 {next_date} 无分钟K线数据")
                continue

            # 如果指定了卖出时间（如 09:50），只取到该时间点的 K 线
            if sell_time:
                cutoff_bars = []
                for bar in next_bars:
                    bar_time = bar.get("day", "")
                    # 提取时间部分 HH:MM
                    time_part = bar_time[11:16] if len(bar_time) > 15 else ""
                    if time_part and time_part <= sell_time:
                        cutoff_bars.append(bar)
                if not cutoff_bars:
                    # 没有该时间点的数据，用前几根
                    cutoff_bars = next_bars[:3] if len(next_bars) >= 3 else next_bars
                next_bars_for_strategy = cutoff_bars
            else:
                next_bars_for_strategy = next_bars

            # 日 K 线的 high/close（用于对比）
            daily_high = None
            daily_close = None
            for r in etf_daily[code]["returns"]:
                if r["date"] == next_date:
                    daily_high = r.get("high", r["close"])
                    daily_close = r["close"]
                    break

            # 日 K 线粗略回测（对比用，不含手续费）
            ret_high = (daily_high - buy_cost) / buy_cost * 100 if daily_high else 0
            ret_close = (daily_close - buy_cost) / buy_cost * 100 if daily_close else 0

            # 分钟线精确回测（不含手续费）
            # 优先级：TRIX > OBV > 固定止盈 > 追踪止盈 > 收盘卖
            # 注意：take_profit 显式指定时优先于 trail_trigger（因 trail_trigger 默认 3.0）
            if use_trix:
                ret_minute, reason, detail = simulate_next_day_trix_cross(
                    buy_cost, today_bars, next_bars_for_strategy)
            elif use_obv:
                ret_minute, reason, detail = simulate_next_day_obv_cross(
                    buy_cost, today_bars, next_bars_for_strategy)
            elif take_profit > 0:
                ret_minute, reason = simulate_next_day_fixed(
                    buy_cost, next_bars_for_strategy, take_profit, stop_loss)
                detail = {}
            elif trail_trigger > 0:
                ret_minute, reason, detail = simulate_next_day_trailing(
                    buy_cost, next_bars_for_strategy, trail_trigger, trail_drop, stop_loss)
            else:
                last_close = float(next_bars_for_strategy[-1].get("close", 0)) if next_bars_for_strategy else buy_cost
                ret_minute = (last_close - buy_cost) / buy_cost * 100
                reason = "close"
                detail = {}

            # 统一扣除手续费（买入 + 卖出各扣万 fee_pct）
            # 总成本 = fee_pct × 2（买+卖）
            ret_minute -= fee_pct * 2

            trades.append({
                "date": date,
                "next_date": next_date,
                "sector": name,
                "etf_code": etf_code,
                "score": score,
                "buy_price": buy_cost,
                "buy_time": "14:55",
                "ret_high": ret_high,
                "ret_close": ret_close,
                "ret_minute": ret_minute,
                "sell_reason": reason,
                "detail": detail,
            })

    return trades


def _calc_stats(rets: list[float], total_days: int = 0) -> dict:
    if not rets:
        return {}
    wins = sum(1 for r in rets if r > 0)
    cum = 1.0
    for r in rets:
        cum *= (1 + r / 100)
    cum_pct = (cum - 1) * 100
    avg = sum(rets) / len(rets)
    # 年化：用实际交易日数（total_days）而非交易笔数
    # 过滤后很多天没交易，但时间仍在流逝，必须用实际天数
    days = total_days if total_days > 0 else len(rets)
    if days > 1:
        ann = (cum ** (250 / days) - 1) * 100
        var = sum((r - avg) ** 2 for r in rets) / len(rets)
        std = var ** 0.5
        sharpe = (avg / std * (250 ** 0.5)) if std > 0 else 0
    else:
        ann = 0
        sharpe = 0
    peak = 1.0
    equity = 1.0
    max_dd = 0.0
    for r in rets:
        equity *= (1 + r / 100)
        if equity > peak:
            peak = equity
        dd = (equity - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd
    return {
        "wins": wins, "total": len(rets), "win_rate": wins / len(rets) * 100,
        "cum": cum_pct, "ann": ann, "avg": avg,
        "max_loss": min(rets), "max_gain": max(rets), "sharpe": sharpe,
        "max_drawdown": max_dd,
    }


def print_minute_report(trades: list[dict], trail_trigger: float,
                        trail_drop: float, stop_loss: float,
                        take_profit: float = 0.0):
    print("=" * 80)
    print("  板块轮动 TOP1 分钟线精确回测报告")
    print("=" * 80)
    print(f"策略: 14:50 选 v6 TOP1 ETF → 14:55 买入 → 次日 5 分钟线模拟卖出")
    print(f"公式: {SCORE_WINDOW}日涨幅 × 量能因子(阈{VOL_THRESHOLD} 均{VOL_AVG_PERIOD} 底{VOL_BASE})")
    if trail_trigger > 0:
        print(f"追踪止盈: 触发+{trail_trigger}% 回落{trail_drop}% 止损{stop_loss}%")
    elif take_profit > 0:
        print(f"固定止盈: +{take_profit}% 止损{stop_loss}%")
    else:
        print(f"策略: 收盘卖")
    print(f"交易天数: {len(trades)}")
    print()

    if not trades:
        print("无交易记录")
        return

    # 计算实际交易日跨度（从首笔信号日到末笔卖出日）
    all_trade_dates = set()
    for t in trades:
        all_trade_dates.add(t["date"])
        if t.get("next_date"):
            all_trade_dates.add(t["next_date"])
    total_days = len(all_trade_dates) if all_trade_dates else len(trades)

    # A. 分钟线精确（排除 pending 未平仓）
    closed_trades = [t for t in trades if t["sell_reason"] != "pending"]
    stats_min = _calc_stats([t["ret_minute"] for t in closed_trades], total_days)
    print("─" * 60)
    print("A. 分钟线精确回测（追踪止盈）")
    print("─" * 60)
    print(f"  胜率: {stats_min['wins']}/{stats_min['total']} = {stats_min['win_rate']:.1f}%")
    print(f"  累计收益率: {stats_min['cum']:+.2f}%")
    print(f"  年化收益率: {stats_min['ann']:+.2f}%")
    print(f"  平均每笔: {stats_min['avg']:+.3f}%")
    print(f"  最大单笔亏损: {stats_min['max_loss']:+.3f}%")
    print(f"  最大单笔盈利: {stats_min['max_gain']:+.3f}%")
    print(f"  最大回撤: {stats_min['max_drawdown']:+.2f}%")
    print(f"  夏普比率: {stats_min['sharpe']:.2f}")

    tp = sum(1 for t in trades if t["sell_reason"] == "trailing_stop")
    tp_fixed = sum(1 for t in trades if t["sell_reason"] == "take_profit")
    sl = sum(1 for t in trades if t["sell_reason"] == "stop_loss")
    cl = sum(1 for t in trades if t["sell_reason"] == "close")
    print(f"  追踪触发: {tp} 次 | 止盈触发: {tp_fixed} 次 | 止损触发: {sl} 次 | 收盘卖: {cl} 次")
    print()

    # B. 日 K 线粗略（对比，排除 pending）
    stats_high = _calc_stats([t["ret_high"] for t in closed_trades], total_days)
    stats_close = _calc_stats([t["ret_close"] for t in closed_trades], total_days)
    print("─" * 60)
    print("B. 日 K 线粗略回测（对比）")
    print("─" * 60)
    print(f"  盘中最高卖: 累计 {stats_high['cum']:+.2f}% 胜率 {stats_high['win_rate']:.1f}% 回撤 {stats_high['max_drawdown']:+.2f}%")
    print(f"  收盘卖:     累计 {stats_close['cum']:+.2f}% 胜率 {stats_close['win_rate']:.1f}% 回撤 {stats_close['max_drawdown']:+.2f}%")
    print()

    # C. 逐笔明细
    print("─" * 60)
    print("C. 逐笔交易明细")
    print("─" * 60)
    print(f"  {'日期':>12s} {'板块':10s} {'ETF':>8s} {'买入':>8s} "
          f"{'日K最高':>8s} {'分钟精确':>8s} {'收盘':>8s} {'原因':>10s}")
    for t in trades:
        reason_cn = {"trailing_stop": "追踪止盈", "stop_loss": "止损",
                     "take_profit": "止盈", "close": "收盘",
                     "pending": "-"}.get(t["sell_reason"], t["sell_reason"])
        if t["sell_reason"] == "pending":
            print(f"  {t['date']:>12s} {t['sector']:10s} {t['etf_code']:>8s} "
                  f"{t['buy_price']:8.3f} {'-':>8s} {'-':>8s} "
                  f"{'-':>8s} {reason_cn:>10s}")
        else:
            print(f"  {t['date']:>12s} {t['sector']:10s} {t['etf_code']:>8s} "
                  f"{t['buy_price']:8.3f} {t['ret_high']:+7.2f}% {t['ret_minute']:+7.2f}% "
                  f"{t['ret_close']:+7.2f}% {reason_cn:>10s}")

    print()
    print("=" * 80)
    print("对比结论:")
    print(f"  分钟线精确: 累计 {stats_min['cum']:+.2f}% 回撤 {stats_min['max_drawdown']:+.2f}% 夏普 {stats_min['sharpe']:.2f}")
    print(f"  日K线最高卖: 累计 {stats_high['cum']:+.2f}% (理论天花板)")
    print(f"  日K线收盘卖: 累计 {stats_close['cum']:+.2f}% (保守下限)")
    print(f"  分钟线 vs 日K线最高 差距: {stats_min['cum'] - stats_high['cum']:+.2f}%")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="板块轮动 TOP1 分钟线精确回测")
    parser.add_argument("--lookback", type=int, default=30, help="历史天数（默认 30）")
    parser.add_argument("--trail-trigger", type=float, default=3.0,
                        help="追踪止盈触发涨幅（默认 3.0）")
    parser.add_argument("--trail-drop", type=float, default=0.5,
                        help="追踪止盈回落幅度（默认 0.5）")
    parser.add_argument("--stop-loss", type=float, default=-0.5,
                        help="止损百分比（默认 -0.5）")
    parser.add_argument("--take-profit", type=float, default=0.0,
                        help="固定止盈（默认 0=用追踪止盈）")
    parser.add_argument("--fee", type=float, default=0.0,
                        help="单边手续费百分比（默认 0，万3填 0.03）")
    parser.add_argument("--sell-time", type=str, default="",
                        help="次日卖出时间，如 09:50 表示开盘后到9:50就卖（默认空=全天策略）")
    parser.add_argument("--start-date", type=str, default="",
                        help="回测起始日期 YYYY-MM-DD（默认空=不限制）")
    parser.add_argument("--end-date", type=str, default="",
                        help="回测结束日期 YYYY-MM-DD（默认空=不限制）")
    parser.add_argument("--buy-time", type=str, default="14:55",
                        help="买入时间 14:55 或 15:00（收盘价，默认 14:55）")
    parser.add_argument("--compare", action="store_true",
                        help="多组参数对比模式")
    args = parser.parse_args()

    print(f"=== 板块轮动 TOP1 分钟线精确回测 ===")
    print(f"公式: {SCORE_WINDOW}日涨幅 × 量能因子(阈{VOL_THRESHOLD} 均{VOL_AVG_PERIOD} 底{VOL_BASE})")
    print(f"历史天数: {args.lookback}")
    print()

    # 1. 获取板块列表（平安证券）
    print(">>> 获取板块列表（平安证券）...")
    sectors = load_pingan_sectors()
    print(f"    {len(sectors)} 个板块（均有 ETF）")

    # 2. 获取日 K 线（选 TOP1）+ 分钟 K 线（精确模拟）
    etf_sectors = sectors

    # 根据回测时长选择分钟 K 线周期
    # 5分钟: 每日48根, 5000根上限≈104天
    # 15分钟: 每日16根, 5000根上限≈313天
    # 30分钟: 每日8根, 5000根上限≈625天
    # 60分钟: 每日4根, 5000根上限≈1250天
    if args.lookback <= 100:
        scale = 5
        bars_per_day = 48
        scale_label = "5分钟"
    elif args.lookback <= 300:
        scale = 15
        bars_per_day = 16
        scale_label = "15分钟"
    elif args.lookback <= 600:
        scale = 30
        bars_per_day = 8
        scale_label = "30分钟"
    else:
        scale = 60
        bars_per_day = 4
        scale_label = "60分钟"

    total_min = min(args.lookback * bars_per_day, 5000)
    print(f">>> 获取 {len(etf_sectors)} 个 ETF 的日K线 + {scale_label}K线...")
    print(f"    每个 ETF: {args.lookback} 日K + {total_min} 根{scale_label}K")
    print(f"    预计耗时: ~{len(etf_sectors) * SINA_INTERVAL * 2:.0f} 秒")

    etf_daily = {}
    etf_5min = {}
    for i, sec in enumerate(etf_sectors):
        etf_code, etf_name = sec["etf_code"], sec["etf_name"]

        # 日 K 线
        daily_klines = fetch_daily_kline(etf_code, datalen=args.lookback)
        if not daily_klines or len(daily_klines) < SCORE_WINDOW + 1:
            continue
        returns = compute_daily_from_klines(daily_klines)
        etf_daily[sec["code"]] = {
            "name": sec["name"], "etf_code": etf_code,
            "etf_name": etf_name, "returns": returns,
        }

        # 分钟 K 线
        min_klines = fetch_5min_kline(etf_code, datalen=total_min, scale=scale)
        if not min_klines:
            continue

        # 按日期分组
        bars_by_date = {}
        for bar in min_klines:
            day = bar.get("day", "")[:10]
            if day:
                if day not in bars_by_date:
                    bars_by_date[day] = []
                bars_by_date[day].append(bar)

        etf_5min[sec["code"]] = {
            "name": sec["name"], "etf_code": etf_code,
            "bars_by_date": bars_by_date,
        }

        if (i + 1) % 10 == 0:
            print(f"    进度: {i+1}/{len(etf_sectors)} (日K:{len(etf_daily)} 分钟K:{len(etf_5min)})")

    print(f"    完成: 日K {len(etf_daily)} 个, 分钟K {len(etf_5min)} 个")

    if args.compare:
        run_minute_compare(etf_daily, etf_5min, args)
        return

    # 3. 回测
    print(f"\n>>> 运行分钟线精确回测...")
    print(f"    追踪止盈: 触发+{args.trail_trigger}% 回落{args.trail_drop}% 止损{args.stop_loss}%")
    trades = run_minute_backtest(
        etf_daily, etf_5min,
        trail_trigger=args.trail_trigger, trail_drop=args.trail_drop,
        stop_loss=args.stop_loss, take_profit=args.take_profit,
        fee_pct=args.fee, sell_time=args.sell_time,
        start_date=args.start_date, end_date=args.end_date,
        buy_time=args.buy_time)

    # 4. 报告
    print()
    print_minute_report(trades, args.trail_trigger, args.trail_drop,
                        args.stop_loss, args.take_profit)

    # 5. 保存
    cache_dir = Path.home() / ".tradingagents" / "rotation"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"backtest_minute_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump({
            "config": {
                "lookback": args.lookback,
                "trail_trigger": args.trail_trigger,
                "trail_drop": args.trail_drop,
                "stop_loss": args.stop_loss,
            },
            "trades": trades,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n回测数据已保存: {cache_file}")


def run_minute_compare(etf_daily: dict, etf_5min: dict, args):
    """多组参数分钟线对比。"""
    param_sets = [
        # (trail_trigger, trail_drop, stop_loss, take_profit, use_trix, use_obv)
        # 固定止盈止损
        (0, 0, 0, 0, False, False),           # 收盘卖（基线）
        (0, 0, -1.0, 4.0, False, False),      # 止盈+4% 止损-1%
        # 追踪止盈（最优策略）
        (3.0, 0.5, -0.5, 0, False, False),    # 追踪触3%落0.5%止-0.5%
        # TRIX 死叉
        (0, 0, 0, 0, True, False),            # TRIX 死叉卖出
        # OBV 死叉
        (0, 0, 0, 0, False, True),            # OBV 死叉卖出
    ]

    print(f"\n>>> 运行 {len(param_sets)} 组参数分钟线对比...")
    all_results = []

    for tt, td, sl, tp, use_trix, use_obv in param_sets:
        trades = run_minute_backtest(etf_daily, etf_5min,
                                     trail_trigger=tt, trail_drop=td,
                                     stop_loss=sl, take_profit=tp,
                                     use_trix=use_trix, use_obv=use_obv,
                                     fee_pct=args.fee,
                                     sell_time=args.sell_time,
                                     start_date=args.start_date,
                                     end_date=args.end_date,
                                     buy_time=args.buy_time)
        if not trades:
            continue
        # 计算实际交易日跨度
        all_trade_dates = set()
        for t in trades:
            all_trade_dates.add(t["date"])
            if t.get("next_date"):
                all_trade_dates.add(t["next_date"])
        td_actual = len(all_trade_dates) if all_trade_dates else len(trades)
        stats = _calc_stats([t["ret_minute"] for t in trades], td_actual)
        trail_h = sum(1 for t in trades if t["sell_reason"] == "trailing_stop")
        tp_h = sum(1 for t in trades if t["sell_reason"] == "take_profit")
        sl_h = sum(1 for t in trades if t["sell_reason"] == "stop_loss")
        trix_h = sum(1 for t in trades if t["sell_reason"] == "trix_death_cross")
        obv_h = sum(1 for t in trades if t["sell_reason"] == "obv_death_cross")
        cl = sum(1 for t in trades if t["sell_reason"] == "close")

        stats_high = _calc_stats([t["ret_high"] for t in trades], td_actual)
        stats_close = _calc_stats([t["ret_close"] for t in trades], td_actual)

        all_results.append({
            "tt": tt, "td": td, "sl": sl, "tp": tp,
            "use_trix": use_trix, "use_obv": use_obv,
            "stats": stats,
            "stats_high": stats_high,
            "stats_close": stats_close,
            "trail_hits": trail_h, "tp_hits": tp_h,
            "sl_hits": sl_h, "trix_hits": trix_h, "obv_hits": obv_h,
            "close_hits": cl,
            "rets": [t["ret_minute"] for t in trades],
        })

    print("\n" + "=" * 125)
    print(f"  分钟线精确回测参数对比（14:55 买入，手续费万{args.fee * 100:.0f}）")
    print("=" * 125)
    print(f"  {'策略':>24} {'分钟累计%':>10} {'年化%':>9} {'胜率%':>6} "
          f"{'夏普':>5} {'回撤%':>7} {'均笔':>7} {'日K最高%':>9} {'日K收盘%':>9} {'TRIX':>4} {'OBV':>4} {'止盈':>4} {'追踪':>4} {'止损':>4} {'收盘':>4}")
    print("  " + "─" * 125)

    for r in sorted(all_results, key=lambda x: x["stats"]["cum"], reverse=True):
        s = r["stats"]
        if r["use_trix"]:
            label = "TRIX 死叉卖"
        elif r["use_obv"]:
            label = "OBV 死叉卖"
        elif r["tt"] > 0:
            label = f"追踪 触{r['tt']}% 落{r['td']}% 止{r['sl']}"
        elif r["tp"] > 0:
            label = f"固定 止盈+{r['tp']}% 止损{r['sl']}"
        else:
            label = "收盘卖"
        print(f"  {label:>24} {s['cum']:+9.2f}% {s['ann']:+8.1f}% "
              f"{s['win_rate']:5.1f}% {s['sharpe']:4.2f} {s['max_drawdown']:+6.2f}% "
              f"{s['avg']:+6.3f}% "
              f"{r['stats_high']['cum']:+8.2f}% {r['stats_close']['cum']:+8.2f}% "
              f"{r['trix_hits']:4d} {r['obv_hits']:4d} {r['tp_hits']:4d} {r['trail_hits']:4d} {r['sl_hits']:4d} {r['close_hits']:4d}")

    best = max(all_results, key=lambda x: x["stats"]["cum"])
    best_sharpe = max(all_results, key=lambda x: x["stats"]["sharpe"])
    print()
    if best["use_trix"]:
        bl = "TRIX 死叉卖"
    elif best["use_obv"]:
        bl = "OBV 死叉卖"
    elif best["tt"] > 0:
        bl = f"追踪 触{best['tt']}% 落{best['td']}% 止{best['sl']}"
    elif best["tp"] > 0:
        bl = f"固定 止盈+{best['tp']}% 止损{best['sl']}"
    else:
        bl = "收盘卖"
    print(f"  ★ 累计最优: {bl} → 累计{best['stats']['cum']:+.2f}% 回撤{best['stats']['max_drawdown']:+.2f}%")
    print(f"  ★ 夏普最优: 累计{best_sharpe['stats']['cum']:+.2f}% 夏普{best_sharpe['stats']['sharpe']:.2f}")
    print("=" * 125)


if __name__ == "__main__":
    main()

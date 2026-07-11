#!/usr/bin/env python3
"""板块轮动动量验证脚本（新浪数据源版）

完全使用新浪财经 API，不依赖东财（避免 IP 封禁问题）。

数据链路：
1. 新浪板块列表 → 49 个行业板块 + 领涨股
2. 新浪领涨股 K 线（30 天）→ 作为板块价格代理
3. 计算动量得分 → 验证持续性 / 排名稳定性 / 轮动信号质量

用法:
    python scripts/validate_rotation_momentum.py
    python scripts/validate_rotation_momentum.py --lookback 30 --top-n 5
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

PROXY = os.environ.get("ROTATION_PROXY", "http://127.0.0.1:7890")
SINA_INTERVAL = 0.3  # 新浪不限流，但仍加小延迟
TIMEOUT = 15

# ── 板块 → 代表性 ETF 映射 ────────────────────────────
SECTOR_ETF_MAP: dict[str, tuple[str, str]] = {
    "电子信息": ("159997", "电子ETF"),
    "电子器件": ("159995", "芯片ETF"),
    "生物制药": ("512010", "医药ETF"),
    "医疗器械": ("159883", "医疗器械ETF"),
    "钢铁行业": ("515210", "钢铁ETF"),
    "煤炭行业": ("515220", "煤炭ETF"),
    "有色金属": ("512400", "有色金属ETF"),
    "电力行业": ("159611", "电力ETF"),
    "发电设备": ("159637", "电力设备ETF"),
    "电器行业": ("159996", "家电ETF"),
    "家电行业": ("159996", "家电ETF"),
    "酿酒行业": ("512690", "酒ETF"),
    "食品行业": ("515170", "食品ETF"),
    "化工行业": ("516020", "化工ETF"),
    "化纤行业": ("516020", "化工ETF"),
    "农药化肥": ("516020", "化工ETF"),
    "建筑建材": ("159745", "建材ETF"),
    "水泥行业": ("159745", "建材ETF"),
    "玻璃行业": ("159745", "建材ETF"),
    "陶瓷行业": ("159745", "建材ETF"),
    "机械行业": ("159883", "机械ETF"),
    "仪器仪表": ("159883", "机械ETF"),
    "汽车制造": ("516110", "汽车ETF"),
    "摩托车":   ("516110", "汽车ETF"),
    "金融行业": ("512800", "银行ETF"),
    "房地产":   ("512200", "房地产ETF"),
    "交通运输": ("159662", "交运ETF"),
    "公路桥梁": ("159662", "交运ETF"),
    "酒店旅游": ("159766", "旅游ETF"),
    "农林牧渔": ("159825", "农业ETF"),
    "环保行业": ("512580", "环保ETF"),
    "传媒娱乐": ("512980", "传媒ETF"),
    "船舶制造": ("512660", "军工ETF"),
    "飞机制造": ("512660", "军工ETF"),
    "石油行业": ("162419", "石油基金"),
    "商业百货": ("159928", "消费ETF"),
    "服装鞋类": ("159928", "消费ETF"),
}


def etf_to_sina_symbol(etf_code: str) -> str:
    """ETF 代码转新浪格式: 5开头→sh, 1开头→sz。"""
    if etf_code.startswith("5"):
        return f"sh{etf_code}"
    elif etf_code.startswith("1"):
        return f"sz{etf_code}"
    return f"sh{etf_code}"


def curl_get(url: str) -> str:
    """用 curl 获取数据，先不走代理（新浪国内直连），失败再走代理。"""
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


# ── 数据采集 ──────────────────────────────────────────

def fetch_sina_sectors() -> list[dict]:
    """获取新浪行业板块列表。

    返回: [{code, name, stock_count, avg_chg_pct, leader_code, leader_name}, ...]
    """
    raw = curl_get("http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php")
    if not raw or "=" not in raw:
        return []

    json_str = raw.split("=", 1)[1].strip().rstrip(";")
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return []

    sectors = []
    for key, val in data.items():
        parts = val.split(",")
        if len(parts) < 13:
            continue
        try:
            sectors.append({
                "code": parts[0],
                "name": parts[1],
                "stock_count": int(parts[2]) if parts[2] else 0,
                "avg_pe": float(parts[3]) if parts[3] else 0,
                "avg_chg_pct": float(parts[4]) if parts[4] else 0,
                "volume": parts[6],
                "turnover": parts[7],
                "leader_code": parts[8],
                "leader_chg_pct": float(parts[9]) if parts[9] else 0,
                "leader_price": float(parts[10]) if parts[10] else 0,
                "leader_name": parts[12],
            })
        except (ValueError, IndexError):
            continue

    return sectors


def fetch_sina_kline(symbol: str, datalen: int = 30) -> list[dict]:
    """获取新浪股票日 K 线。

    symbol: 新浪格式代码，如 sz002623, sh600519
    返回: [{day, open, high, close, low, volume}, ...]
    """
    url = (
        f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
    )
    raw = curl_get(url)
    time.sleep(SINA_INTERVAL)
    if not raw or raw.strip() in ("null", "", "[]"):
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def compute_daily_returns(klines: list[dict]) -> list[dict]:
    """从 K 线计算日收益率 + 成交量 + OHLC。

    返回: [{date, open, high, low, close, return_pct, high_return_pct, volume}, ...]
      - return_pct:     昨收→今收（收盘收益率）
      - high_return_pct: 昨收→今高（盘中最高收益率）
    """
    result = []
    for i, k in enumerate(klines):
        close = float(k.get("close", 0))
        high = float(k.get("high", close))
        low = float(k.get("low", close))
        open_p = float(k.get("open", close))
        date = k.get("day", "")
        try:
            volume = float(k.get("volume", 0))
        except (ValueError, TypeError):
            volume = 0.0
        if i == 0:
            ret = 0.0
            high_ret = 0.0
        else:
            prev_close = float(klines[i - 1].get("close", 0))
            ret = ((close - prev_close) / prev_close * 100) if prev_close else 0.0
            high_ret = ((high - prev_close) / prev_close * 100) if prev_close else 0.0
        result.append({
            "date": date, "open": open_p, "high": high, "low": low,
            "close": close, "return_pct": ret, "high_return_pct": high_ret,
            "volume": volume,
        })
    return result


def compute_cmf(returns: list[dict], idx: int, period: int = 10) -> float:
    """计算 Chaikin Money Flow（资金流代理指标）。

    通过收盘价在当日振幅中的位置推算资金流向：
    - MFM = ((close - low) - (high - close)) / (high - low)
    - CMF = sum(MFM × volume) / sum(volume)  过去 period 天
    - 范围: -1（全部流出）~ +1（全部流入）

    无需额外 API，从 OHLCV 数据计算。
    """
    if idx < period:
        period = idx + 1
    sum_mfv = 0.0
    sum_vol = 0.0
    for i in range(idx - period + 1, idx + 1):
        r = returns[i]
        high = r.get("high", r["close"])
        low = r.get("low", r["close"])
        close = r["close"]
        vol = r.get("volume", 0)
        if high > low:
            mfm = ((close - low) - (high - close)) / (high - low)
        else:
            mfm = 0.0
        sum_mfv += mfm * vol
        sum_vol += vol
    return sum_mfv / sum_vol if sum_vol > 0 else 0.0


# ── 动量计算 ──────────────────────────────────────────

def compute_momentum(returns: list[dict], idx: int, window: int = 1) -> float:
    """计算 idx 位置的动量得分（基于过去 window 天的收益率）。

    调整后公式（v2）— 基于 v1 验证数据优化：
    - 1 日窗口命中率 64.3%（最佳）→ 默认 window 改为 1，加大短期权重
    - 3 日窗口命中率 53.8%（最差）→ 不再作为默认
    - 5 日窗口命中率 62.5% → 用于中期参考
    - 10 日窗口命中率 63.2% → 保持但降权

    = 0.50 * 短期累计涨幅（window 日，默认 1 日）
    + 0.20 * 中期累计涨幅（5 日）
    + 0.15 * 加速度（今日 - 昨日涨幅）
    + 0.15 * 波动率调整（涨幅 / 波动率）
    """
    if idx < 1:
        return 0.0

    # 短期累计涨幅
    start = max(0, idx - window + 1)
    short_ret = sum(r["return_pct"] for r in returns[start:idx + 1])

    # 中期累计涨幅（5 日，原 10 日降为 5 日）
    mid_start = max(0, idx - 5 + 1)
    mid_ret = sum(r["return_pct"] for r in returns[mid_start:idx + 1])

    # 加速度
    if idx >= 2:
        accel = returns[idx]["return_pct"] - returns[idx - 1]["return_pct"]
    else:
        accel = 0.0

    # 波动率
    window_returns = [r["return_pct"] for r in returns[start:idx + 1]]
    if len(window_returns) > 1:
        avg = sum(window_returns) / len(window_returns)
        vol = (sum((x - avg) ** 2 for x in window_returns) / len(window_returns)) ** 0.5
    else:
        vol = 1.0

    # 波动率调整（避免除零）
    risk_adj = short_ret / max(vol, 0.5) if vol > 0 else short_ret

    score = (
        0.50 * short_ret
        + 0.20 * mid_ret
        + 0.15 * accel
        + 0.15 * risk_adj
    )
    return score


# ── 验证逻辑 ──────────────────────────────────────────

def validate_momentum(all_returns: dict, top_n: int = 5):
    """验证动量信号。

    all_returns: {sector_code: {name, leader, returns: [{date, close, return_pct}]}}
    """
    # 收集所有日期
    all_dates = set()
    for info in all_returns.values():
        for r in info["returns"]:
            all_dates.add(r["date"])
    all_dates = sorted(all_dates)

    if len(all_dates) < 5:
        print(f"⚠️ 历史数据不足（仅 {len(all_dates)} 天），至少需要 5 天")
        return None

    # 从第 1 天开始（window=1 只需 1 天历史）
    eval_dates = all_dates[1:-1]

    results = {
        "momentum_persistence": [],
        "rank_stability": [],
        "rotation_quality": [],
        "rotation_quality_10d": [],
        "rotation_quality_10d_vol": [],
        "rotation_quality_10d_vol_cmf": [],
        "filtered_rotation_quality": [],
        "window_comparison": {},
    }

    prev_top5 = set()
    # v3: 10 日窗口的 TOP5 追踪（含连续天数和过滤）
    prev_top5_10d: set[str] = set()
    top5_streak_10d: dict[str, int] = {}  # code → 连续在 TOP5 的天数
    # v4: 10 日窗口 + 量价配合的 TOP5 追踪
    prev_top5_10d_vol: set[str] = set()
    # v5: 10 日窗口 + 量价 + 资金流(CMF) 的 TOP5 追踪
    prev_top5_10d_vol_cmf: set[str] = set()

    for date in eval_dates:
        date_idx = all_dates.index(date)
        next_date = all_dates[date_idx + 1] if date_idx + 1 < len(all_dates) else None
        if not next_date:
            continue

        # 计算每个板块在 date 的动量分
        scores = []
        for code, info in all_returns.items():
            returns = info["returns"]
            # 找到 date 在 returns 中的索引
            idx_map = {r["date"]: i for i, r in enumerate(returns)}
            if date not in idx_map:
                continue
            idx = idx_map[date]
            if idx < 1:
                continue
            score = compute_momentum(returns, idx, window=1)
            scores.append((code, info["name"], score))

        if len(scores) < top_n * 2:
            continue

        scores.sort(key=lambda x: x[2], reverse=True)
        top5 = set(code for code, _, _ in scores[:top_n])
        bottom5 = set(code for code, _, _ in scores[-top_n:])

        # T+1 日收益率（收盘 & 盘中最高）
        def get_t1_return(code, field="return_pct"):
            info = all_returns[code]
            for r in info["returns"]:
                if r["date"] == next_date:
                    return r.get(field)
            return None

        top5_t1 = [get_t1_return(c) for c, _, _ in scores[:top_n]]
        bottom5_t1 = [get_t1_return(c) for c, _, _ in scores[-top_n:]]
        top5_t1 = [x for x in top5_t1 if x is not None]
        bottom5_t1 = [x for x in bottom5_t1 if x is not None]

        # 1. 动量持续性
        if top5_t1 and bottom5_t1:
            top5_avg = sum(top5_t1) / len(top5_t1)
            bottom5_avg = sum(bottom5_t1) / len(bottom5_t1)
            results["momentum_persistence"].append({
                "date": date,
                "next_date": next_date,
                "top5_avg_ret": top5_avg,
                "bottom5_avg_ret": bottom5_avg,
                "top5_outperforms": top5_avg > bottom5_avg,
                "top5_positive": top5_avg > 0,
            })

        # 2. 排名稳定性
        if prev_top5:
            overlap = len(top5 & prev_top5) / len(top5 | prev_top5) if (top5 | prev_top5) else 0
            results["rank_stability"].append({
                "date": date,
                "overlap": overlap,
                "new_entries": len(top5 - prev_top5),
                "exits": len(prev_top5 - top5),
            })

        # 3. 轮动信号质量
        if prev_top5:
            new_entries = top5 - prev_top5
            for code in new_entries:
                t1_ret = get_t1_return(code)
                if t1_ret is not None:
                    results["rotation_quality"].append({
                        "date": date,
                        "sector": all_returns[code]["name"],
                        "t1_return": t1_ret,
                        "positive": t1_ret > 0,
                    })

        prev_top5 = top5

        # ── v3: 10 日窗口轮动信号质量（含过滤） ──
        scores_10d = []
        for code, info in all_returns.items():
            returns = info["returns"]
            idx_map = {r["date"]: i for i, r in enumerate(returns)}
            if date not in idx_map:
                continue
            idx = idx_map[date]
            if idx < 10:
                continue
            # 纯 10 日累计涨幅
            ret_10d = sum(r["return_pct"] for r in returns[idx - 9:idx + 1])
            scores_10d.append((code, info["name"], ret_10d, returns[idx]["return_pct"]))

        if len(scores_10d) >= top_n * 2:
            scores_10d.sort(key=lambda x: x[2], reverse=True)
            top5_10d = set(c for c, _, _, _ in scores_10d[:top_n])

            # 更新连续天数
            for c in top5_10d:
                top5_streak_10d[c] = top5_streak_10d.get(c, 0) + 1
            for c in list(top5_streak_10d.keys()):
                if c not in top5_10d:
                    top5_streak_10d[c] = 0

            # 3b. 10 日窗口轮动信号质量（无过滤，用于对比）
            if prev_top5_10d:
                new_10d = top5_10d - prev_top5_10d
                for code in new_10d:
                    t1_ret = get_t1_return(code)
                    if t1_ret is not None:
                        results["rotation_quality_10d"].append({
                            "date": date,
                            "sector": all_returns[code]["name"],
                            "t1_return": t1_ret,
                            "positive": t1_ret > 0,
                        })

            # 3c. 过滤后轮动信号质量
            #    过滤条件: 连续 2 日在 TOP5 + 当日涨幅 ≤ 5%（排除极端暴涨）
            if prev_top5_10d:
                for code in top5_10d:
                    if top5_streak_10d.get(code, 0) == 2:  # 刚好第 2 天连续
                        today_ret = None
                        for c, _, _, r1d in scores_10d:
                            if c == code:
                                today_ret = r1d
                                break
                        if today_ret is not None and today_ret <= 5.0:
                            t1_ret = get_t1_return(code)
                            if t1_ret is not None:
                                results["filtered_rotation_quality"].append({
                                    "date": date,
                                    "sector": all_returns[code]["name"],
                                    "t1_return": t1_ret,
                                    "positive": t1_ret > 0,
                                    "today_ret": today_ret,
                                })

            prev_top5_10d = top5_10d

        # ── v4: 10 日窗口 + 量价配合 ──
        # 量价配合评分 = 10日涨幅 × 量能因子
        # 量能因子 = 0.5 + 0.5 × min(volume_ratio / 2.0, 1.0)
        # 放量(volume_ratio≥2) → 因子=1.0(满权), 平量(1.0) → 0.75, 缩量(0.5) → 0.625
        scores_10d_vol = []
        for code, info in all_returns.items():
            returns = info["returns"]
            idx_map = {r["date"]: i for i, r in enumerate(returns)}
            if date not in idx_map:
                continue
            idx = idx_map[date]
            if idx < 10:
                continue
            ret_10d = sum(r["return_pct"] for r in returns[idx - 9:idx + 1])
            # 量比: 今日成交量 / 过去5日平均成交量
            vol_today = returns[idx].get("volume", 0)
            vol_5d = [returns[j].get("volume", 0) for j in range(max(0, idx - 5), idx)]
            avg_vol_5d = sum(vol_5d) / len(vol_5d) if vol_5d and sum(vol_5d) > 0 else vol_today
            vol_ratio = vol_today / avg_vol_5d if avg_vol_5d > 0 else 1.0
            vol_factor = 0.5 + 0.5 * min(vol_ratio / 2.0, 1.0)
            score_vol = ret_10d * vol_factor
            scores_10d_vol.append((code, info["name"], score_vol, returns[idx]["return_pct"]))

        if len(scores_10d_vol) >= top_n * 2:
            scores_10d_vol.sort(key=lambda x: x[2], reverse=True)
            top5_10d_vol = set(c for c, _, _, _ in scores_10d_vol[:top_n])

            # 3d. 量价配合轮动信号质量
            if prev_top5_10d_vol:
                new_vol = top5_10d_vol - prev_top5_10d_vol
                for code in new_vol:
                    t1_ret = get_t1_return(code, "return_pct")
                    t1_high = get_t1_return(code, "high_return_pct")
                    if t1_ret is not None:
                        results["rotation_quality_10d_vol"].append({
                            "date": date,
                            "sector": all_returns[code]["name"],
                            "t1_return": t1_ret,
                            "t1_high_return": t1_high,
                            "positive": t1_ret > 0,
                            "high_positive": (t1_high or 0) > 0,
                        })

            prev_top5_10d_vol = top5_10d_vol

        # ── v5: 10 日窗口 + 量价 + 资金流(CMF) ──
        # v5 评分 = 10日涨幅 × 量能因子 × 资金流因子
        # 资金流因子 = max(0.3, 1.0 + CMF)
        #   CMF=+0.3(流入) → 因子=1.3(加成)
        #   CMF= 0  (中性) → 因子=1.0
        #   CMF=-0.3(流出) → 因子=0.7(打折)
        scores_10d_vol_cmf = []
        for code, info in all_returns.items():
            returns = info["returns"]
            idx_map = {r["date"]: i for i, r in enumerate(returns)}
            if date not in idx_map:
                continue
            idx = idx_map[date]
            if idx < 10:
                continue
            ret_10d = sum(r["return_pct"] for r in returns[idx - 9:idx + 1])
            vol_today = returns[idx].get("volume", 0)
            vol_5d = [returns[j].get("volume", 0) for j in range(max(0, idx - 5), idx)]
            avg_vol_5d = sum(vol_5d) / len(vol_5d) if vol_5d and sum(vol_5d) > 0 else vol_today
            vol_ratio = vol_today / avg_vol_5d if avg_vol_5d > 0 else 1.0
            vol_factor = 0.5 + 0.5 * min(vol_ratio / 2.0, 1.0)
            cmf = compute_cmf(returns, idx, period=10)
            cmf_factor = max(0.3, 1.0 + cmf)
            score_v5 = ret_10d * vol_factor * cmf_factor
            scores_10d_vol_cmf.append((code, info["name"], score_v5, returns[idx]["return_pct"]))

        if len(scores_10d_vol_cmf) >= top_n * 2:
            scores_10d_vol_cmf.sort(key=lambda x: x[2], reverse=True)
            top5_v5 = set(c for c, _, _, _ in scores_10d_vol_cmf[:top_n])

            # 3e. 量价+资金流 轮动信号质量
            if prev_top5_10d_vol_cmf:
                new_v5 = top5_v5 - prev_top5_10d_vol_cmf
                for code in new_v5:
                    t1_ret = get_t1_return(code, "return_pct")
                    t1_high = get_t1_return(code, "high_return_pct")
                    if t1_ret is not None:
                        results["rotation_quality_10d_vol_cmf"].append({
                            "date": date,
                            "sector": all_returns[code]["name"],
                            "t1_return": t1_ret,
                            "t1_high_return": t1_high,
                            "positive": t1_ret > 0,
                            "high_positive": (t1_high or 0) > 0,
                        })

            prev_top5_10d_vol_cmf = top5_v5

    # 4. 不同动量窗口对比（含 v1 默认 3 日作为对照）
    for window in [1, 3, 5, 10]:
        hits = 0
        total = 0
        for date in eval_dates:
            date_idx = all_dates.index(date)
            next_date = all_dates[date_idx + 1] if date_idx + 1 < len(all_dates) else None
            if not next_date:
                continue
            scores_w = []
            for code, info in all_returns.items():
                returns = info["returns"]
                idx_map = {r["date"]: i for i, r in enumerate(returns)}
                if date not in idx_map:
                    continue
                idx = idx_map[date]
                if idx < max(window, 1):
                    continue
                # 用各窗口的纯累计涨幅做简单排名对比
                start_w = max(0, idx - window + 1)
                simple_ret = sum(r["return_pct"] for r in returns[start_w:idx + 1])
                scores_w.append((code, simple_ret))
            if len(scores_w) < top_n * 2:
                continue
            scores_w.sort(key=lambda x: x[1], reverse=True)

            top_rets = [get_t1_return(c) for c, _ in scores_w[:top_n]]
            bot_rets = [get_t1_return(c) for c, _ in scores_w[-top_n:]]
            top_rets = [x for x in top_rets if x is not None]
            bot_rets = [x for x in bot_rets if x is not None]
            if top_rets and bot_rets:
                total += 1
                if sum(top_rets) / len(top_rets) > sum(bot_rets) / len(bot_rets):
                    hits += 1

        results["window_comparison"][window] = {
            "hit_rate": hits / total if total > 0 else 0,
            "total_days": total,
        }

    return results


# ── 报告输出 ──────────────────────────────────────────

def print_report(sectors, all_returns, results, top_n):
    print("=" * 70)
    print("          板块轮动动量验证报告")
    print("=" * 70)
    print(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"板块总数: {len(sectors)}（新浪行业分类）")
    print(f"有 K 线数据的板块: {len(all_returns)}")
    print(f"数据源: 新浪财经（板块列表 + 领涨股 K 线代理）")
    print()

    if all_returns:
        sample = next(iter(all_returns.values()))
        dates = [r["date"] for r in sample["returns"]]
        print(f"数据时间范围: {dates[0]} ~ {dates[-1]} ({len(dates)} 个交易日)")
    print()

    if not results:
        print("⚠️ 数据不足，无法验证")
        return

    # 1. 动量持续性
    mp = results["momentum_persistence"]
    if mp:
        outperform = sum(1 for m in mp if m["top5_outperforms"])
        positive = sum(1 for m in mp if m["top5_positive"])
        print("─" * 50)
        print("1. 动量持续性（TOP5 vs BOTTOM5 次日表现）")
        print("─" * 50)
        print(f"  验证天数: {len(mp)}")
        print(f"  TOP5 跑赢 BOTTOM5 的比例: {outperform}/{len(mp)} = {outperform/len(mp)*100:.1f}%")
        print(f"  TOP5 次日平均涨幅为正的比例: {positive}/{len(mp)} = {positive/len(mp)*100:.1f}%")
        avg_top = sum(m["top5_avg_ret"] for m in mp) / len(mp)
        avg_bot = sum(m["bottom5_avg_ret"] for m in mp) / len(mp)
        print(f"  TOP5 平均次日涨幅: {avg_top:+.3f}%")
        print(f"  BOTTOM5 平均次日涨幅: {avg_bot:+.3f}%")
        print(f"  超额收益: {avg_top - avg_bot:+.3f}%/日")
        print()

    # 2. 排名稳定性
    rs = results["rank_stability"]
    if rs:
        avg_overlap = sum(r["overlap"] for r in rs) / len(rs)
        avg_new = sum(r["new_entries"] for r in rs) / len(rs)
        print("─" * 50)
        print("2. 排名稳定性")
        print("─" * 50)
        print(f"  验证天数: {len(rs)}")
        print(f"  TOP5 连续日平均重叠率: {avg_overlap*100:.1f}%")
        print(f"  平均每日新进入 TOP5 的板块数: {avg_new:.1f}")
        if avg_overlap > 0.6:
            print(f"  → 重叠率高（>60%），主线延续性强，轮动较慢")
        elif avg_overlap > 0.3:
            print(f"  → 重叠率中等，存在结构性轮动")
        else:
            print(f"  → 重叠率低（<30%），轮动频繁，切换快")
        print()

    # 3. 轮动信号质量
    rq = results["rotation_quality"]
    if rq:
        pos = sum(1 for r in rq if r["positive"])
        print("─" * 50)
        print("3a. 轮动信号质量 — v2（1日窗口，无过滤）")
        print("─" * 50)
        print(f"  新进入 TOP5 的事件数: {len(rq)}")
        print(f"  次日上涨的比例: {pos}/{len(rq)} = {pos/len(rq)*100:.1f}%")
        avg_ret = sum(r["t1_return"] for r in rq) / len(rq)
        print(f"  平均次日涨幅: {avg_ret:+.3f}%")
        print()

    # 3b. 10 日窗口轮动信号质量（无过滤）
    rq10 = results.get("rotation_quality_10d", [])
    if rq10:
        pos10 = sum(1 for r in rq10 if r["positive"])
        print("─" * 50)
        print("3b. 轮动信号质量 — 10日窗口，无过滤")
        print("─" * 50)
        print(f"  新进入 TOP5 的事件数: {len(rq10)}")
        print(f"  次日上涨的比例: {pos10}/{len(rq10)} = {pos10/len(rq10)*100:.1f}%")
        avg_ret10 = sum(r["t1_return"] for r in rq10) / len(rq10)
        print(f"  平均次日涨幅: {avg_ret10:+.3f}%")
        print()

    # 3c. 过滤后轮动信号质量
    frq = results.get("filtered_rotation_quality", [])
    if frq:
        fpos = sum(1 for r in frq if r["positive"])
        print("─" * 50)
        print("3c. 轮动信号质量 — 10日窗口 + 过滤（连续2日 + 涨幅≤5%）")
        print("─" * 50)
        print(f"  过滤后信号事件数: {len(frq)}")
        print(f"  次日上涨的比例: {fpos}/{len(frq)} = {fpos/len(frq)*100:.1f}%")
        avg_fret = sum(r["t1_return"] for r in frq) / len(frq)
        print(f"  平均次日涨幅: {avg_fret:+.3f}%")
        print()

    # 3d. 量价配合轮动信号质量
    rqv = results.get("rotation_quality_10d_vol", [])
    if rqv:
        vpos = sum(1 for r in rqv if r["positive"])
        vpos_h = sum(1 for r in rqv if r.get("high_positive"))
        print("─" * 50)
        print("3d. 轮动信号质量 — 10日窗口 × 量价配合（放量加权）")
        print("─" * 50)
        print(f"  新进入 TOP5 的事件数: {len(rqv)}")
        print(f"  次日收盘上涨比例: {vpos}/{len(rqv)} = {vpos/len(rqv)*100:.1f}%")
        print(f"  次日盘中最高上涨比例: {vpos_h}/{len(rqv)} = {vpos_h/len(rqv)*100:.1f}%")
        avg_vret = sum(r["t1_return"] for r in rqv) / len(rqv)
        avg_vhret = sum(r.get("t1_high_return", 0) for r in rqv) / len(rqv)
        print(f"  平均次日收盘涨幅: {avg_vret:+.3f}%")
        print(f"  平均次日盘中最高涨幅: {avg_vhret:+.3f}%")
        print()

    # 3e. 量价+资金流 轮动信号质量
    rqvc = results.get("rotation_quality_10d_vol_cmf", [])
    if rqvc:
        vcpos = sum(1 for r in rqvc if r["positive"])
        vcpos_h = sum(1 for r in rqvc if r.get("high_positive"))
        print("─" * 50)
        print("3e. 轮动信号质量 — 10日窗口 × 量价 × 资金流(CMF)")
        print("─" * 50)
        print(f"  新进入 TOP5 的事件数: {len(rqvc)}")
        print(f"  次日收盘上涨比例: {vcpos}/{len(rqvc)} = {vcpos/len(rqvc)*100:.1f}%")
        print(f"  次日盘中最高上涨比例: {vcpos_h}/{len(rqvc)} = {vcpos_h/len(rqvc)*100:.1f}%")
        avg_vcret = sum(r["t1_return"] for r in rqvc) / len(rqvc)
        avg_vchret = sum(r.get("t1_high_return", 0) for r in rqvc) / len(rqvc)
        print(f"  平均次日收盘涨幅: {avg_vcret:+.3f}%")
        print(f"  平均次日盘中最高涨幅: {avg_vchret:+.3f}%")
        print()

        # 对比汇总
        print("─" * 50)
        print("  ★ 信号质量对比汇总")
        print("─" * 50)
        if rq:
            r1 = pos / len(rq) * 100
            print(f"  v2  1日窗口 无过滤:          {r1:5.1f}% 收盘 ({len(rq)} 样本)")
        if rq10:
            r2 = pos10 / len(rq10) * 100
            print(f"      10日窗口 无过滤:          {r2:5.1f}% 收盘 ({len(rq10)} 样本)")
        if frq:
            r3 = fpos / len(frq) * 100
            print(f"  v3  10日窗口 +入场过滤:      {r3:5.1f}% 收盘 ({len(frq)} 样本)")
        if rqv:
            r4 = vpos / len(rqv) * 100
            r4h = vpos_h / len(rqv) * 100
            print(f"  v4  10日窗口 量价配合:       {r4:5.1f}% 收盘 / {r4h:5.1f}% 盘中 ({len(rqv)} 样本)")
        r5 = vcpos / len(rqvc) * 100
        r5h = vcpos_h / len(rqvc) * 100
        print(f"  v5  10日 量价+资金流:        {r5:5.1f}% 收盘 / {r5h:5.1f}% 盘中 ({len(rqvc)} 样本)")
        if rq and rqvc:
            improvement = r5 - pos / len(rq) * 100
            print(f"  v5 vs v2 收盘提升: {improvement:+.1f}pp")
        if rqv and rqvc:
            improvement_vol = r5 - vpos / len(rqv) * 100
            print(f"  v5 vs v4 收盘提升: {improvement_vol:+.1f}pp")
        print()

    # 4. 动量窗口对比
    wc = results["window_comparison"]
    if wc:
        print("─" * 50)
        print("4. 动量窗口对比（TOP5 跑赢 BOTTOM5 的命中率）")
        print("─" * 50)
        for window in sorted(wc.keys()):
            w = wc[window]
            print(f"  {window:2d} 日窗口: {w['hit_rate']*100:5.1f}% ({w['total_days']} 天)")
        best = max(wc.items(), key=lambda x: x[1]["hit_rate"])
        print(f"  → 最佳窗口: {best[0]} 日（命中率 {best[1]['hit_rate']*100:.1f}%）")
        print()

    # 5. 当前 TOP10
    print("─" * 50)
    print("5. 当前动量 TOP10 板块")
    print("─" * 50)
    current_scores = []
    for code, info in all_returns.items():
        returns = info["returns"]
        if len(returns) < 2:
            continue
        score = compute_momentum(returns, len(returns) - 1, window=1)
        last = returns[-1]
        current_scores.append((info["name"], score, last))
    current_scores.sort(key=lambda x: x[1], reverse=True)
    print(f"  {'排名':>4} {'板块':10s} {'动量分':>10s} {'最新涨幅':>10s} {'日期':>12s}")
    for i, (name, score, last) in enumerate(current_scores[:10]):
        print(f"  {i+1:4d} {name:10s} {score:10.2f} {last['return_pct']:+.2f}%{'':>5} {last['date']:>12s}")

    print()
    print("=" * 70)
    print("验证结论:")
    if mp:
        rate = sum(1 for m in mp if m["top5_outperforms"]) / len(mp) * 100
        if rate > 60:
            print(f"  ✅ 动量信号有效（TOP5 跑赢率 {rate:.1f}%），可以用于轮动监控")
        elif rate > 50:
            print(f"  ⚠️ 动量信号弱有效（TOP5 跑赢率 {rate:.1f}%），建议结合其他指标")
        else:
            print(f"  ❌ 动量信号无效（TOP5 跑赢率 {rate:.1f}%），需要调整公式")
    if wc:
        best = max(wc.items(), key=lambda x: x[1]["hit_rate"])
        print(f"  → 推荐动量窗口: {best[0]} 日（命中率 {best[1]['hit_rate']*100:.1f}%）")
    print("=" * 70)


# ── 主流程 ──────────────────────────────────────────

def tune_etf(etf_returns: dict, top_n: int = 5) -> list[dict]:
    """ETF 专项参数搜索：遍历窗口 × 量价 × 资金流组合，找最优配置。"""

    all_dates = set()
    for info in etf_returns.values():
        for r in info["returns"]:
            all_dates.add(r["date"])
    all_dates = sorted(all_dates)

    max_window = 10
    eval_dates = all_dates[max_window:-1]

    def _score(returns, idx, window, use_vol, use_cmf):
        start = max(0, idx - window + 1)
        ret_w = sum(r["return_pct"] for r in returns[start:idx + 1])
        if use_vol:
            vol_today = returns[idx].get("volume", 0)
            vol_5d = [returns[j].get("volume", 0) for j in range(max(0, idx - 5), idx)]
            avg_vol = sum(vol_5d) / len(vol_5d) if vol_5d and sum(vol_5d) > 0 else vol_today
            vr = vol_today / avg_vol if avg_vol > 0 else 1.0
            vf = 0.5 + 0.5 * min(vr / 2.0, 1.0)
        else:
            vf = 1.0
        if use_cmf:
            cmf = compute_cmf(returns, idx, period=min(10, max(window, 3)))
            cf = max(0.3, 1.0 + cmf)
        else:
            cf = 1.0
        return ret_w * vf * cf

    # 预建 idx_map
    idx_maps = {}
    for code, info in etf_returns.items():
        idx_maps[code] = {r["date"]: i for i, r in enumerate(info["returns"])}

    def get_t1(code, field):
        for r in etf_returns[code]["returns"]:
            if r["date"] == next_date:
                return r.get(field)
        return None

    results = []
    for window in [1, 2, 3, 5, 7, 10]:
        for use_vol in [False, True]:
            for use_cmf in [False, True]:
                p_hits = p_total = 0
                rot_events = []
                prev_top5: set[str] = set()

                for date in eval_dates:
                    date_idx = all_dates.index(date)
                    next_date = all_dates[date_idx + 1] if date_idx + 1 < len(all_dates) else None
                    if not next_date:
                        continue

                    scores = []
                    for code, info in etf_returns.items():
                        im = idx_maps.get(code, {})
                        if date not in im:
                            continue
                        idx = im[date]
                        if idx < window:
                            continue
                        s = _score(info["returns"], idx, window, use_vol, use_cmf)
                        scores.append((code, s))

                    if len(scores) < top_n * 2:
                        continue

                    scores.sort(key=lambda x: x[1], reverse=True)

                    # Persistence
                    top_r = [get_t1(c, "return_pct") for c, _ in scores[:top_n]]
                    bot_r = [get_t1(c, "return_pct") for c, _ in scores[-top_n:]]
                    top_r = [x for x in top_r if x is not None]
                    bot_r = [x for x in bot_r if x is not None]
                    if top_r and bot_r:
                        p_total += 1
                        if sum(top_r) / len(top_r) > sum(bot_r) / len(bot_r):
                            p_hits += 1

                    # Rotation quality
                    top5 = set(c for c, _ in scores[:top_n])
                    if prev_top5:
                        for code in top5 - prev_top5:
                            tc = get_t1(code, "return_pct")
                            th = get_t1(code, "high_return_pct")
                            if tc is not None:
                                rot_events.append((tc, th))
                    prev_top5 = top5

                vlabel = ("+量" if use_vol else "") + ("+资" if use_cmf else "") or "纯涨幅"
                n = len(rot_events)
                results.append({
                    "window": window,
                    "variant": vlabel,
                    "persistence": p_hits / p_total * 100 if p_total > 0 else 0,
                    "p_total": p_total,
                    "close_up": sum(1 for c, _ in rot_events if c > 0) / n * 100 if n else 0,
                    "high_up": sum(1 for _, h in rot_events if h and h > 0) / n * 100 if n else 0,
                    "avg_close": sum(c for c, _ in rot_events) / n if n else 0,
                    "avg_high": sum(h for _, h in rot_events if h) / n if n else 0,
                    "events": n,
                })

    return results


def print_tuning(results: list[dict]):
    print("=" * 70)
    print("  [C] ETF 参数搜索（窗口 × 量价 × 资金流）")
    print("=" * 70)
    print(f"  {'窗口':>4} {'组合':>8} {'持续性%':>8} {'收盘涨%':>8} {'盘中高%':>8} "
          f"{'均收盘':>8} {'均最高':>8} {'样本':>4}")
    print("  " + "─" * 66)

    # 按 收盘涨% + 盘中高% 综合排序
    for r in sorted(results, key=lambda x: x["close_up"] + x["high_up"], reverse=True):
        print(f"  {r['window']:4d} {r['variant']:>8s} {r['persistence']:7.1f}% "
              f"{r['close_up']:7.1f}% {r['high_up']:7.1f}% "
              f"{r['avg_close']:+7.3f}% {r['avg_high']:+7.3f}% {r['events']:4d}")

    best = max(results, key=lambda x: x["close_up"] + x["high_up"])
    best_persist = max(results, key=lambda x: x["persistence"])
    print()
    print(f"  ★ 轮动信号最优: {best['window']}日 {best['variant']} → "
          f"收盘{best['close_up']:.1f}% / 盘中{best['high_up']:.1f}% ({best['events']}样本)")
    print(f"  ★ 持续性最优:   {best_persist['window']}日 {best_persist['variant']} → "
          f"{best_persist['persistence']:.1f}% ({best_persist['p_total']}天)")
    print("=" * 70)


def tune_etf_deep(etf_returns: dict, window: int = 3, top_n: int = 5) -> list[dict]:
    """针对固定窗口做深度参数搜索。

    测试参数:
    - 量比阈值: [1.5, 2.0, 3.0]
    - 量均周期: [3, 5, 10]
    - 量能底数: [0.3, 0.5, 0.7]
    - CMF 周期: [3, 5, 10]
    - CMF 乘数: [0.5, 1.0, 1.5, 2.0]
    - CMF 底线: [0.3, 0.5, 0.7]
    - 结构: 乘法 / 加法
    """
    all_dates = set()
    for info in etf_returns.values():
        for r in info["returns"]:
            all_dates.add(r["date"])
    all_dates = sorted(all_dates)
    eval_dates = all_dates[window:-1]

    idx_maps = {}
    for code, info in etf_returns.items():
        idx_maps[code] = {r["date"]: i for i, r in enumerate(info["returns"])}

    def _run_score(score_fn, label):
        """用给定评分函数跑一轮验证，返回指标。"""
        p_hits = p_total = 0
        rot_events = []
        prev_top5: set[str] = set()

        for date in eval_dates:
            di = all_dates.index(date)
            nd = all_dates[di + 1] if di + 1 < len(all_dates) else None
            if not nd:
                continue
            scores = []
            for code, info in etf_returns.items():
                im = idx_maps.get(code, {})
                if date not in im:
                    continue
                idx = im[date]
                if idx < window:
                    continue
                s = score_fn(info["returns"], idx)
                scores.append((code, s))
            if len(scores) < top_n * 2:
                continue
            scores.sort(key=lambda x: x[1], reverse=True)

            def gt1(code, field):
                for r in etf_returns[code]["returns"]:
                    if r["date"] == nd:
                        return r.get(field)
                return None

            top_r = [gt1(c, "return_pct") for c, _ in scores[:top_n]]
            bot_r = [gt1(c, "return_pct") for c, _ in scores[-top_n:]]
            top_r = [x for x in top_r if x is not None]
            bot_r = [x for x in bot_r if x is not None]
            if top_r and bot_r:
                p_total += 1
                if sum(top_r) / len(top_r) > sum(bot_r) / len(bot_r):
                    p_hits += 1

            top5 = set(c for c, _ in scores[:top_n])
            if prev_top5:
                for code in top5 - prev_top5:
                    tc = gt1(code, "return_pct")
                    th = gt1(code, "high_return_pct")
                    if tc is not None:
                        rot_events.append((tc, th))
            prev_top5 = top5

        n = len(rot_events)
        return {
            "label": label,
            "persistence": p_hits / p_total * 100 if p_total > 0 else 0,
            "p_total": p_total,
            "close_up": sum(1 for c, _ in rot_events if c > 0) / n * 100 if n else 0,
            "high_up": sum(1 for _, h in rot_events if h and h > 0) / n * 100 if n else 0,
            "avg_close": sum(c for c, _ in rot_events) / n if n else 0,
            "avg_high": sum(h for _, h in rot_events if h) / n if n else 0,
            "events": n,
        }

    results = []

    # 0. 基线: 纯涨幅
    results.append(_run_score(
        lambda rets, idx: sum(r["return_pct"] for r in rets[idx - window + 1:idx + 1]),
        f"{window}日 纯涨幅(基线)",
    ))

    # 1. 量价参数搜索 (乘法结构)
    for vt in [1.5, 2.0, 3.0]:
        for vap in [3, 5, 10]:
            for vb in [0.3, 0.5, 0.7]:
                def score_fn(rets, idx, _vt=vt, _vap=vap, _vb=vb):
                    ret_w = sum(r["return_pct"] for r in rets[idx - window + 1:idx + 1])
                    v_today = rets[idx].get("volume", 0)
                    v_prev = [rets[j].get("volume", 0) for j in range(max(0, idx - _vap), idx)]
                    avg_v = sum(v_prev) / len(v_prev) if v_prev and sum(v_prev) > 0 else v_today
                    vr = v_today / avg_v if avg_v > 0 else 1.0
                    vf = _vb + (1 - _vb) * min(vr / _vt, 1.0)
                    return ret_w * vf
                results.append(_run_score(score_fn,
                    f"{window}日 量× 阈{vt} 均{vap} 底{vb}"))

    # 2. CMF 参数搜索 (乘法结构, 纯涨幅 × CMF因子)
    for cp in [3, 5, 10]:
        for cm in [0.5, 1.0, 1.5, 2.0]:
            for cf in [0.3, 0.5, 0.7]:
                def score_fn(rets, idx, _cp=cp, _cm=cm, _cf=cf):
                    ret_w = sum(r["return_pct"] for r in rets[idx - window + 1:idx + 1])
                    cmf = compute_cmf(rets, idx, period=_cp)
                    cfac = max(_cf, 1.0 + _cm * cmf)
                    return ret_w * cfac
                results.append(_run_score(score_fn,
                    f"{window}日 资× 周{cp} 乘{cm} 底{cf}"))

    # 3. 加法结构: ret + alpha * vol_ratio_norm + beta * cmf
    for alpha in [1.0, 3.0, 5.0]:
        for beta in [3.0, 5.0, 10.0]:
            def score_fn(rets, idx, _a=alpha, _b=beta):
                ret_w = sum(r["return_pct"] for r in rets[idx - window + 1:idx + 1])
                v_today = rets[idx].get("volume", 0)
                v_prev = [rets[j].get("volume", 0) for j in range(max(0, idx - 5), idx)]
                avg_v = sum(v_prev) / len(v_prev) if v_prev and sum(v_prev) > 0 else v_today
                vr = v_today / avg_v if avg_v > 0 else 1.0
                vr_norm = min(vr / 2.0, 1.0)  # 0~1
                cmf = compute_cmf(rets, idx, period=5)
                return ret_w + _a * vr_norm + _b * cmf
            results.append(_run_score(score_fn,
                f"{window}日 加法 a{alpha} b{beta}"))

    return results


def print_deep_tuning(results: list[dict], window: int):
    print("=" * 80)
    print(f"  [D] ETF 深度调参（{window}日窗口，{len(results)} 种组合）")
    print("=" * 80)
    print(f"  {'组合':>40s} {'持续%':>7} {'收盘涨%':>8} {'盘中高%':>8} "
          f"{'均收盘':>8} {'均最高':>8} {'样本':>4}")
    print("  " + "─" * 76)

    for r in sorted(results, key=lambda x: x["close_up"] + x["high_up"], reverse=True)[:20]:
        print(f"  {r['label']:>40s} {r['persistence']:6.1f}% {r['close_up']:7.1f}% "
              f"{r['high_up']:7.1f}% {r['avg_close']:+7.3f}% {r['avg_high']:+7.3f}% {r['events']:4d}")

    best = max(results, key=lambda x: x["close_up"] + x["high_up"])
    best_p = max(results, key=lambda x: x["persistence"])
    baseline = results[0] if results else None
    print()
    print(f"  ★ 信号最优: {best['label']}")
    print(f"    收盘{best['close_up']:.1f}% / 盘中{best['high_up']:.1f}% / "
          f"持续{best['persistence']:.1f}% ({best['events']}样本)")
    print(f"  ★ 持续最优: {best_p['label']}")
    print(f"    持续{best_p['persistence']:.1f}% / 收盘{best_p['close_up']:.1f}% ({best_p['p_total']}天)")
    if baseline:
        print(f"  ★ 基线:     {baseline['label']}")
        print(f"    收盘{baseline['close_up']:.1f}% / 盘中{baseline['high_up']:.1f}% ({baseline['events']}样本)")
        imp = best["close_up"] - baseline["close_up"]
        print(f"  信号最优 vs 基线 收盘提升: {imp:+.1f}pp")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="板块轮动动量验证（新浪数据源）")
    parser.add_argument("--top-n", type=int, default=5, help="TOP N 板块数（默认 5）")
    parser.add_argument("--lookback", type=int, default=30, help="历史天数（默认 30）")
    args = parser.parse_args()

    print(f"代理: {PROXY}")
    print(f"数据源: 新浪财经")
    print(f"TOP N: {args.top_n}, 历史天数: {args.lookback}")
    print()

    # 1. 获取板块列表
    print(">>> 获取新浪行业板块列表...")
    sectors = fetch_sina_sectors()
    print(f"    获取到 {len(sectors)} 个行业板块")
    if not sectors:
        print("❌ 无法获取板块列表，请检查网络/代理")
        sys.exit(1)

    for s in sectors[:5]:
        print(f"    {s['name']:10s} 涨幅={s['avg_chg_pct']:+.2f}% 领涨={s['leader_code']} {s['leader_name']}")
    print()

    # 2. 获取每个板块领涨股的 K 线
    print(f">>> 获取 {len(sectors)} 个板块领涨股的 {args.lookback} 日 K 线...")
    print(f"    预计耗时: ~{len(sectors) * SINA_INTERVAL:.0f} 秒")
    all_returns = {}
    for i, sec in enumerate(sectors):
        leader = sec["leader_code"]
        if not leader or len(leader) < 4:
            continue
        klines = fetch_sina_kline(leader, datalen=args.lookback)
        if klines and len(klines) > 3:
            returns = compute_daily_returns(klines)
            all_returns[sec["code"]] = {
                "name": sec["name"],
                "leader": leader,
                "leader_name": sec["leader_name"],
                "returns": returns,
            }
        if (i + 1) % 10 == 0:
            print(f"    进度: {i+1}/{len(sectors)} ({len(all_returns)} 有数据)")

    print(f"    完成: {len(all_returns)}/{len(sectors)} 个板块有 K 线数据")

    # 3. 验证动量（领涨股）
    print("\n>>> [A] 领涨股验证: 计算动量得分并验证...")
    results = validate_momentum(all_returns, top_n=args.top_n)

    # 4. 输出领涨股报告
    print()
    print("=" * 70)
    print("  [A] 领涨股验证报告")
    print("=" * 70)
    print_report(sectors, all_returns, results, args.top_n)

    # 5. ETF 验证（独立计算，不与领涨股混合）
    etf_returns = {}
    etf_sectors = [s for s in sectors if s["name"] in SECTOR_ETF_MAP]
    print(f"\n\n>>> [B] ETF 验证: 获取 {len(etf_sectors)} 个板块的 ETF K 线...")
    for i, sec in enumerate(etf_sectors):
        etf_code, etf_name = SECTOR_ETF_MAP[sec["name"]]
        sina_sym = etf_to_sina_symbol(etf_code)
        klines = fetch_sina_kline(sina_sym, datalen=args.lookback)
        if klines and len(klines) > 3:
            returns = compute_daily_returns(klines)
            etf_returns[sec["code"]] = {
                "name": sec["name"],
                "leader": etf_code,
                "leader_name": f"{etf_name}({etf_code})",
                "returns": returns,
            }
        if (i + 1) % 10 == 0:
            print(f"    进度: {i+1}/{len(etf_sectors)} ({len(etf_returns)} 有数据)")

    print(f"    完成: {len(etf_returns)}/{len(etf_sectors)} 个 ETF 有 K 线数据")

    if len(etf_returns) >= args.top_n * 2:
        print("\n>>> [B] ETF 验证: 计算动量得分并验证...")
        etf_results = validate_momentum(etf_returns, top_n=args.top_n)
        print()
        print("=" * 70)
        print("  [B] ETF 验证报告（独立计算，不与领涨股混合）")
        print("=" * 70)
        print_report(etf_sectors, etf_returns, etf_results, args.top_n)
    else:
        etf_results = None
        print("    ETF 数据不足，跳过 ETF 验证")

    # 6. 对比汇总
    print("\n\n" + "=" * 70)
    print("  [A] vs [B] 领涨股 vs ETF 对比汇总")
    print("=" * 70)

    def _summary(res, label):
        if not res:
            print(f"  {label}: 无数据")
            return
        rqv = res.get("rotation_quality_10d_vol", [])
        rqvc = res.get("rotation_quality_10d_vol_cmf", [])
        if rqvc:
            pos = sum(1 for r in rqvc if r["positive"])
            pos_h = sum(1 for r in rqvc if r.get("high_positive"))
            avg = sum(r["t1_return"] for r in rqvc) / len(rqvc)
            avg_h = sum(r.get("t1_high_return", 0) for r in rqvc) / len(rqvc)
            print(f"  {label} v5 量价+资金流:")
            print(f"    收盘上涨: {pos}/{len(rqvc)} = {pos/len(rqvc)*100:.1f}%  "
                  f"平均 {avg:+.3f}%")
            print(f"    盘中最高: {pos_h}/{len(rqvc)} = {pos_h/len(rqvc)*100:.1f}%  "
                  f"平均 {avg_h:+.3f}%")
        if rqv:
            pos = sum(1 for r in rqv if r["positive"])
            print(f"  {label} v4 量价:     {pos}/{len(rqv)} = {pos/len(rqv)*100:.1f}%")

    _summary(results, "领涨股")
    print()
    _summary(etf_results, "ETF    ")
    print("=" * 70)

    # 7. ETF 参数搜索
    if etf_returns:
        print("\n>>> [C] ETF 参数搜索...")
        tune_results = tune_etf(etf_returns, top_n=args.top_n)
        print()
        print_tuning(tune_results)

        # 深度调参: 取基础搜索中持续性最优的窗口
        best_w = max(tune_results, key=lambda x: x["persistence"])["window"]
        print(f"\n>>> [D] ETF 深度调参（{best_w}日窗口）...")
        deep_results = tune_etf_deep(etf_returns, window=best_w, top_n=args.top_n)
        print()
        print_deep_tuning(deep_results, best_w)
    else:
        tune_results = None

    # 7. 保存原始数据
    cache_dir = os.path.expanduser("~/.tradingagents/rotation")
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"validation_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump({
            "sectors": sectors,
            "all_returns": all_returns,
            "results": results,
            "etf_returns": etf_returns,
            "etf_results": etf_results,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n原始数据已保存: {cache_file}")


if __name__ == "__main__":
    main()

"""板块轮动 v6 得分 — 回测 / 监控 / 手动运行共用。

公式:
    得分 = N日累计涨幅 × 量能因子
    量能因子 = VOL_BASE + (1-VOL_BASE) × min(量比/VOL_THRESHOLD, 1)

选股规则（与 backtest_rotation_8way 一致）:
    - 有信号时刻前 partial 行情 → partial_score_at（盘中）
    - 开盘前 / 无 partial → premarket_v6_score（T-1 日完整 v6，不看当日收盘）
"""

from __future__ import annotations

from datetime import date

SCORE_WINDOW = 3
VOL_THRESHOLD = 1.5
VOL_AVG_PERIOD = 5
VOL_BASE = 0.3


def compute_v6_score(returns: list[dict], idx: int) -> float:
    """计算 v6 得分（3日涨幅 × 量能因子）。"""
    if idx < SCORE_WINDOW:
        return 0.0
    ret_w = sum(r["return_pct"] for r in returns[idx - SCORE_WINDOW + 1 : idx + 1])
    vol_today = returns[idx].get("volume", 0)
    vol_prev = [
        returns[j].get("volume", 0)
        for j in range(max(0, idx - VOL_AVG_PERIOD), idx)
    ]
    avg_vol = sum(vol_prev) / len(vol_prev) if vol_prev and sum(vol_prev) > 0 else vol_today
    vol_ratio = vol_today / avg_vol if avg_vol > 0 else 1.0
    vol_factor = VOL_BASE + (1 - VOL_BASE) * min(vol_ratio / VOL_THRESHOLD, 1.0)
    return ret_w * vol_factor


def partial_score_at(
    returns: list[dict],
    idx: int,
    partial_close: float,
    partial_vol: float,
) -> float:
    """用信号时刻前的 close/volume 替换当日 bar 再算 v6。"""
    modified = list(returns)
    prev_close = returns[idx - 1]["close"] if idx > 0 else partial_close
    partial_ret = ((partial_close - prev_close) / prev_close * 100) if prev_close else 0.0
    modified[idx] = {
        "date": returns[idx]["date"],
        "close": partial_close,
        "return_pct": partial_ret,
        "volume": partial_vol,
    }
    return compute_v6_score(modified, idx)


def premarket_v6_score(returns: list[dict], idx: int) -> float | None:
    """开盘前、无 partial：T-1 日完整 v6（避免偷看当日收盘）。"""
    if idx <= SCORE_WINDOW:
        return None
    return compute_v6_score(returns, idx - 1)


def score_at_signal(
    returns: list[dict],
    idx: int,
    partial_close: float | None,
    partial_vol: float | None,
) -> float | None:
    """回测 rank_top1 统一入口。"""
    if idx < SCORE_WINDOW:
        return None
    if partial_close and partial_close > 0:
        return partial_score_at(returns, idx, partial_close, partial_vol or 0.0)
    return premarket_v6_score(returns, idx)


def _vol_metrics(returns: list[dict], idx: int) -> tuple[float, float, float]:
    ret_w = sum(r["return_pct"] for r in returns[idx - SCORE_WINDOW + 1 : idx + 1])
    vol_today = returns[idx].get("volume", 0)
    vol_prev = [
        returns[j].get("volume", 0)
        for j in range(max(0, idx - VOL_AVG_PERIOD), idx)
    ]
    avg_vol = sum(vol_prev) / len(vol_prev) if vol_prev and sum(vol_prev) > 0 else vol_today
    vol_ratio = vol_today / avg_vol if avg_vol > 0 else 1.0
    vol_factor = VOL_BASE + (1 - VOL_BASE) * min(vol_ratio / VOL_THRESHOLD, 1.0)
    return ret_w, vol_ratio, vol_factor


def compute_v6_metrics(returns: list[dict]) -> dict:
    """监控 / 手动运行：对 returns 最后一根 bar 算 v6 及明细。"""
    idx = len(returns) - 1
    if idx < SCORE_WINDOW:
        return {}

    last = returns[idx]
    last_date = str(last.get("date", ""))[:10]
    today = date.today().isoformat()

    if last.get("intraday"):
        score_idx = idx
        score = compute_v6_score(returns, idx)
    elif last_date >= today:
        score = premarket_v6_score(returns, idx)
        if score is None:
            return {}
        score_idx = idx - 1
    else:
        score_idx = idx
        score = compute_v6_score(returns, idx)

    ret_w, vol_ratio, vol_factor = _vol_metrics(returns, score_idx)
    return {
        "score": score,
        "ret_3d": ret_w,
        "vol_ratio": vol_ratio,
        "vol_factor": vol_factor,
        "last_bar_ret": returns[score_idx]["return_pct"],
        "date": returns[score_idx]["date"],
        "intraday": bool(last.get("intraday")),
    }

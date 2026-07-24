"""T+0 策略市场环境识别 — 南方原油 501018 作商品代理。"""

from __future__ import annotations

REGIME_PROXY = "501018"
CHOPPY_MA_CROSS = 2       # 近 10 日 MA20 穿越 ≥2 → 震荡
TREND_DIST_MIN = 8.0      # 距 MA20 >8%
TREND_ADX_MIN = 30.0


def _calc_adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 2:
        return None
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        trs.append(tr)
    def wilder(vals: list[float]) -> list[float | None]:
        s = sum(vals[:period])
        out: list[float | None] = [None] * period
        out.append(s)
        for i in range(period, len(vals) - 1):
            s = s - s / period + vals[i + 1]
            out.append(s)
        return out
    atr = wilder(trs)
    pdm = wilder(plus_dm)
    mdm = wilder(minus_dm)
    dxs: list[float] = []
    for i in range(period, len(trs)):
        if atr[i] and atr[i] > 0:
            pdi = 100 * pdm[i] / atr[i]  # type: ignore[operator]
            mdi = 100 * mdm[i] / atr[i]  # type: ignore[operator]
            dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0)
    if len(dxs) < period:
        return None
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def _ma_crosses(closes: list[float], ma_days: int = 20, lookback: int = 10) -> int:
    if len(closes) < ma_days + lookback:
        return 0
    crosses = 0
    prev: bool | None = None
    for i in range(len(closes) - lookback, len(closes)):
        ma = sum(closes[i - ma_days + 1:i + 1]) / ma_days
        above = closes[i] > ma
        if prev is not None and above != prev:
            crosses += 1
        prev = above
    return crosses


def detect_regime(daily_klines: list[dict], as_of_date: str | None = None) -> dict | None:
    """返回 {mode, dist_ma20, ma_crosses, adx, close, ma20, skip_choppy}。"""
    if not daily_klines:
        return None
    idx_map = {k.get("day", ""): i for i, k in enumerate(daily_klines)}
    if as_of_date and as_of_date in idx_map:
        idx = idx_map[as_of_date]
    else:
        idx = len(daily_klines) - 1
    if idx < 29:
        return None
    closes = [float(daily_klines[j].get("close", 0)) for j in range(idx - 29, idx + 1)]
    highs = [float(daily_klines[j].get("high", daily_klines[j].get("close", 0))) for j in range(idx - 29, idx + 1)]
    lows = [float(daily_klines[j].get("low", daily_klines[j].get("close", 0))) for j in range(idx - 29, idx + 1)]
    ma20 = sum(closes[-20:]) / 20
    close = closes[-1]
    dist = abs(close - ma20) / ma20 * 100 if ma20 else 0
    crosses = _ma_crosses(closes, 20, 10)
    adx = _calc_adx(highs, lows, closes, 14) or 0.0
    if crosses >= CHOPPY_MA_CROSS:
        mode = "震荡"
    elif dist > TREND_DIST_MIN and adx > TREND_ADX_MIN:
        mode = "趋势"
    else:
        mode = "中性"
    return {
        "mode": mode,
        "dist_ma20": round(dist, 2),
        "ma_crosses": crosses,
        "adx": round(adx, 1),
        "close": close,
        "ma20": round(ma20, 4),
        "skip_choppy": mode == "震荡",
        "proxy": REGIME_PROXY,
        "as_of": daily_klines[idx].get("day", as_of_date or ""),
    }


def regime_action_line(regime: dict, *, hybrid: bool = False) -> str:
    """Hybrid 实盘：震荡/趋势不跳过，只切换选池；纯 T+0 模式震荡日跳过买入。"""
    mode = regime["mode"]
    if hybrid:
        if mode == "中性":
            return "中性 → **原T0池**，继续交易"
        if mode == "震荡":
            return "震荡 → **优质池**，继续交易（Hybrid 不跳过）"
        if mode == "趋势":
            return "趋势 → **优质池**，继续交易"
        return "✅ 可正常交易"
    if regime.get("skip_choppy"):
        return "⛔ 震荡期跳过买入"
    return "✅ 可正常交易"


def format_regime_block(regime: dict | None, *, hybrid: bool = False) -> list[str]:
    if not regime:
        suffix = "（Hybrid：中性→原T0池）" if hybrid else ""
        return [f"**市场环境**: 数据不足，按中性处理{suffix}"]
    action = regime_action_line(regime, hybrid=hybrid)
    return [
        "**市场环境**（南方原油 501018）",
        f"- 状态: **{regime['mode']}** | {action}",
        f"- 收盘: {regime['close']:.4f} | MA20: {regime['ma20']:.4f} | 距MA20: {regime['dist_ma20']:.2f}%",
        f"- 近10日MA20穿越: **{regime['ma_crosses']}次**（≥{CHOPPY_MA_CROSS}=震荡）",
        f"- ADX(14): {regime['adx']:.1f}",
        "",
    ]

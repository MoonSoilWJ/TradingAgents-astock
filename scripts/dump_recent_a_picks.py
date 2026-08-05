#!/usr/bin/env python3
"""逐行复刻聚宽 scan_at_1440 选股逻辑, 用本地缓存数据跑, 打印最近 N 天命中标的。

目的: 隔离「为什么聚宽和本地不一致」。本脚本 = 聚宽选股算法 + 本地缓存行情。
若本脚本输出与聚宽日志不一致 → 差异 100% 来自【行情源】(聚宽 get_price vs 本地
tdx 5min缓存) 或【数据截止日】; 若本脚本输出与聚宽一致 → 说明算法已对齐, 差异
只是你贴代码时漏了某段。

复刻的聚宽参数(与 joinquant_unified_strategy.py 完全一致):
  MIN_GAIN=3.0, LOOKBACK=30, POOL_SIZE=25
  REGIME_PROXY=501018, CHOPPY_MA_CROSS=2, TREND_DIST_MIN=8.0, TREND_ADX_MIN=30.0
  GATE_ENABLED=False(默认)

用法: python3 scripts/dump_recent_a_picks.py [--days 30]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from t0_etf_list import get_all_t0_etfs  # noqa: E402

# ---- 聚宽同款常量 ----
MIN_GAIN = 3.0
LOOKBACK = 30
POOL_SIZE = 25
REGIME_PROXY = "501018"
CHOPPY_MA_CROSS = 2
TREND_DIST_MIN = 8.0
TREND_ADX_MIN = 30.0

CACHE = Path.home() / ".tradingagents/cache/t0_5min"
ALIGNED = CACHE / "aligned_live_4y.json"
DENSE = [CACHE / "tdx_5min_pre2024.json",
         CACHE / "tdx_5min_2y.json",
         CACHE / "tdx_5min_auto.json"]
DENSE_START = "2022-06-15"


def load():
    cache = json.loads(ALIGNED.read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    proxy = cache["proxy_klines"]
    etf_daily = cache["etf_daily"]
    etf_5min: dict = {}
    for p in DENSE:
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))["etf_5min"]
        for c, days in d.items():
            etf_5min.setdefault(c, {}).update(days)
    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    code2name = {e["code"]: (e.get("name") or e.get("etf_name") or e["code"])
                 for e in etf_list}
    return all_dates, proxy, etf_daily, etf_5min, etf_list, code2name


# ---- 聚宽同款数学 ----
def _calc_adx(highs, lows, closes, period=14):
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
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

    atr = wilder(trs); pdm = wilder(plus_dm); mdm = wilder(minus_dm)
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


def _ma_crosses(closes, ma_days=20, lookback=10):
    if len(closes) < ma_days + lookback:
        return 0
    crosses, prev = 0, None
    for i in range(len(closes) - lookback, len(closes)):
        ma = sum(closes[i - ma_days + 1:i + 1]) / ma_days
        above = closes[i] > ma
        if prev is not None and above != prev:
            crosses += 1
        prev = above
    return crosses


def detect_regime(proxy, day):
    """复刻聚宽 detect_regime, 用本地 proxy_klines(501018 日K list)。"""
    seq = [r for r in proxy if r["day"] <= day]
    if len(seq) < 30:
        return "中性"
    last = seq[-30:]
    closes = [r["close"] for r in last]
    highs = [r["high"] for r in last]
    lows = [r["low"] for r in last]
    ma20 = sum(closes[-20:]) / 20
    close = closes[-1]
    dist = abs(close - ma20) / ma20 * 100 if ma20 else 0
    crosses = _ma_crosses(closes, 20, 10)
    adx = _calc_adx(highs, lows, closes, 14)
    if crosses >= CHOPPY_MA_CROSS:
        return "震荡"
    elif dist > TREND_DIST_MIN and adx > TREND_ADX_MIN:
        return "趋势"
    else:
        return "中性"


def prev_close_of(etf_daily, code, day):
    rs = etf_daily.get(code, {}).get("returns", [])
    prev = None
    for r in rs:
        if r["date"] < day:
            prev = r["close"]
        elif r["date"] >= day:
            break
    return prev


def price_at_1440(etf_5min, code, day):
    bars = etf_5min.get(code, {}).get(day)
    if not bars:
        return None
    best = None
    for bar in bars:
        t = bar.get("time", "")
        if t <= "14:40:00":
            best = bar["close"]
    return best


def calc_today_gain(etf_daily, etf_5min, code, day):
    prev = prev_close_of(etf_daily, code, day)
    px = price_at_1440(etf_5min, code, day)
    if not prev or px is None or prev <= 0:
        return None
    return (px / prev - 1) * 100


def calc_momentum(etf_daily, code, day, lookback):
    rs = etf_daily.get(code, {}).get("returns", [])
    seq = [r for r in rs if r["date"] <= day][-lookback - 1:]
    if len(seq) < lookback + 1:
        return None
    return (seq[-1]["close"] / seq[0]["close"] - 1) * 100


def build_quality_pool(etf_daily, etf_list, day):
    scored = []
    for e in etf_list:
        m = calc_momentum(etf_daily, e["code"], day, LOOKBACK)
        if m is not None:
            scored.append((m, e["code"]))
    scored.sort(reverse=True)
    return [c for _, c in scored[:POOL_SIZE]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    all_dates, proxy, etf_daily, etf_5min, etf_list, code2name = load()
    print(f"本地候选池(∩codes5): {len(etf_list)} 只  "
          f"(聚宽 AUTO_ETFS=get_all_t0_etfs() 同源, 也是 {len(get_all_t0_etfs())} 只)")
    print("★ 本脚本=聚宽选股算法+本地缓存行情; 与聚宽不一致→只剩【行情源】或【数据截止】差异\n")

    recent = [d for d in all_dates if d >= DENSE_START][-(args.days + 5):]
    rows = []
    for day in recent:
        regime = detect_regime(proxy, day)
        if regime in ("趋势", "震荡"):
            pool = build_quality_pool(etf_daily, etf_list, day)
            pname = f"优质池({len(pool)})"
        else:
            pool = [e["code"] for e in etf_list]
            pname = f"auto({len(pool)})"
        cands = []
        for code in pool:
            g = calc_today_gain(etf_daily, etf_5min, code, day)
            if g is not None:
                cands.append((g, code))
        cands.sort(reverse=True)
        top = cands[0] if cands else (None, None)
        if top[0] is not None and top[0] >= MIN_GAIN:
            rows.append((day, regime, pname, top, cands[:3]))
        else:
            rows.append((day, regime, pname, (top[0], None), cands[:3]))

    rows = [r for r in rows if r[3][1] is not None][-args.days:]

    print(f"{'日期':<11}{'regime':<8}{'池':<12}{'命中':<9}{'名':<12}{'14:40%':>8}"
          f"   Top3(标的:涨幅%)")
    print("-" * 78)
    for day, regime, pname, top, top3 in rows:
        code = top[1]
        nm = code2name.get(code, code)
        t3 = "  ".join(f"{c}:{g:+.2f}" for g, c in top3)
        print(f"{day:<11}{regime:<8}{pname:<12}{code:<9}{nm:<12}{top[0]:>7.2f}%  {t3}")

    print(f"\n共 {len(rows)} 个交易日命中标的 (MIN_GAIN={MIN_GAIN}%, 排名时点14:40)")
    print("对照聚宽: 把这段贴到聚宽每日选中标的旁逐日比对。若某天不一致, 多半是")
    print("14:40 那一刻涨幅排序因行情源微差导致 Top1 易主(看 Top3 差距是否极小)。")


if __name__ == "__main__":
    main()

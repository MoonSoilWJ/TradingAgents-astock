#!/usr/bin/env python3
"""本地 R3 回测: A vs hybrid 时序选股对照。

对齐聚宽 joinquant_unified_single.py (canonical R3 +1861% / 2014-2026)。
复刻其选股引擎:
  - detect_regime(501018 日K) -> 趋势/震荡/中性
  - 趋势/震荡: ATTACK_UNIVERSE 近 LOOKBACK(30) 天动量 Top POOL_SIZE(25)
  - 中性: 当月 R3 月度池(缺失->全并集)
  - A     : 14:40 锁绝对涨幅 Top1, 14:45 复核该只仍 ≥3% 才成交
  - hybrid: 14:40 锁 ≥3% 的 Top5 篮子, 14:45 在幸存者(仍 ≥3%)里取涨幅 Top1
  - 卖出: 次日 TRIX(5,3) 死叉 / 11:05 收盘 fallback (对齐 simulate_exit('trix0940_cut'))

数据: full_daily_2015_2026.json(日K) + 合并 tdx_5min_pre2024 + tdx_5min_2y(无偏5min)。
覆盖 2022-2026 无偏段。A 与 hybrid 仅在「时序选股」环节不同, 公平对照。

用法:
  python3 scripts/backtest_r3_hybrid.py --mode a_top1
  python3 scripts/backtest_r3_hybrid.py --mode hybrid_top5
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_today1 import gain_at_time, simulate_trix_cross_after  # noqa: E402
from backtest_t0_etf import price_at_time  # noqa: E402
import jq_attack_pools  # noqa: E402

# ---- 常量(对齐聚宽 joinquant_unified_single.py) ----
MIN_GAIN = 3.0
TRIX_PERIOD = 5
TRIX_SIGNAL_PERIOD = 3
SELL_CUTOFF = "11:05"          # 对齐 simulate_exit('trix0940_cut')
LOOKBACK = 30                  # 滚动优质池训练窗(天)
POOL_SIZE = 25                 # 优质池规模
REGIME_PROXY = "501018"        # 本地纯数字 code
CHOPPY_MA_CROSS = 2
TREND_DIST_MIN = 8.0
TREND_ADX_MIN = 30.0
FEE_PCT_PT = 0.03              # 万3(百分比点); 买卖各一次 -> 每笔 2*FEE_PCT_PT

CACHE = Path.home() / ".tradingagents/cache/t0_5min"
START, END = "2022-06-15", "2026-07-31"

# ---- R3 池(聚宽格式 code.XSHE/XSHG -> 本地纯数字) ----
R3 = jq_attack_pools.JQ_ATTACK_POOLS["R3"]


def _loc(c: str) -> str:
    return c.split(".")[0]


ATTACK_POOL_BY_MONTH = {ym: [_loc(c) for c in v] for ym, v in R3.items()}
ATTACK_UNIVERSE = sorted({c for v in ATTACK_POOL_BY_MONTH.values() for c in v})


def load_data():
    daily = json.loads((CACHE / "full_daily_2015_2026.json").read_text(encoding="utf-8"))
    m5_pre = json.loads((CACHE / "tdx_5min_pre2024.json").read_text(encoding="utf-8"))["etf_5min"]
    m5_2y = json.loads((CACHE / "tdx_5min_2y.json").read_text(encoding="utf-8"))["etf_5min"]
    etf_5min: dict = {}
    for src in (m5_pre, m5_2y):
        for code, byday in src.items():
            etf_5min.setdefault(code, {}).update(byday)
    return daily, etf_5min


# ============ regime(移植聚宽 detect_regime) ============
def _ma_crosses(closes, ma_days=20, lookback=10):
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


def _calc_adx(highs, lows, closes, period=14):
    if len(closes) < period + 2:
        return 0.0
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
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


def detect_regime(daily, day):
    info = daily.get(REGIME_PROXY)
    if not info:
        return "中性"
    returns = info["returns"]
    idx = {r["date"]: i for i, r in enumerate(returns)}
    if day not in idx or idx[day] < 30:
        return "中性"
    i = idx[day]
    window = returns[max(0, i - 34):i + 1]
    closes = [r["close"] for r in window][-30:]
    highs = [r["high"] for r in window][-30:]
    lows = [r["low"] for r in window][-30:]
    if len(closes) < 30:
        return "中性"
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


# ============ 动量优质池(移植聚宽 build_quality_pool) ============
def calc_momentum(daily, code, day, lookback=LOOKBACK):
    info = daily.get(code)
    if not info:
        return None
    returns = info["returns"]
    idx = {r["date"]: i for i, r in enumerate(returns)}
    if day not in idx or idx[day] < lookback:
        return None
    c0 = returns[idx[day] - lookback]["close"]
    c1 = returns[idx[day]]["close"]
    if not c0 or c0 <= 0 or not c1 or c1 <= 0:
        return None
    return (c1 - c0) / c0 * 100


def build_quality_pool(daily, day):
    scored = []
    for code in ATTACK_UNIVERSE:
        if code not in daily:
            continue
        mom = calc_momentum(daily, code, day)
        if mom is not None:
            scored.append((mom, code))
    scored.sort(reverse=True)
    return [c for _, c in scored[:POOL_SIZE]]


# ============ 候选池 + 双时点选股 ============
def scan_1440(daily, etf_5min, day):
    """返回 (regime, pool) —— A 与 hybrid 共用"""
    regime = detect_regime(daily, day)
    if regime in ("趋势", "震荡"):
        pool = build_quality_pool(daily, day)
    else:
        ym = day[:7]
        pool = ATTACK_POOL_BY_MONTH.get(ym) or ATTACK_UNIVERSE
    pool = [c for c in pool if c in etf_5min and day in etf_5min[c]]
    return regime, pool


def pick_at_1440(daily, etf_5min, pool, day, mode):
    """14:40 选股。返回:
       A     : (gain, code) 单只 或 None
       hybrid: [(gain, code), ...] Top5 篮子 或 []
    """
    scored = []
    for code in pool:
        g = gain_at_time(daily, etf_5min, code, day, "14:40")
        if g is not None:
            scored.append((g, code))
    if mode == "hybrid_top5":
        return sorted([s for s in scored if s[0] >= MIN_GAIN], reverse=True)[:5]
    best = max(scored, key=lambda x: x[0]) if scored else None
    if best and best[0] >= MIN_GAIN:
        return best
    return None


def confirm_1445(daily, etf_5min, pick, day, mode):
    """14:45 复核/重选。返回 (gain_1445, code) 或 None"""
    if not pick:
        return None
    if mode == "hybrid_top5":
        survivors = []
        for g, code in pick:
            g2 = gain_at_time(daily, etf_5min, code, day, "14:45")
            if g2 is not None and g2 >= MIN_GAIN:
                survivors.append((g2, code))
        survivors.sort(reverse=True)
        return survivors[0] if survivors else None
    g, code = pick
    g2 = gain_at_time(daily, etf_5min, code, day, "14:45")
    if g2 is not None and g2 >= MIN_GAIN:
        return (g2, code)
    return None


def buy_price(etf_5min, code, day):
    bars = etf_5min[code].get(day, [])
    p = price_at_time(bars, "14:50")
    if p is None:
        p = price_at_time(bars, "14:45")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="a_top1", choices=["a_top1", "hybrid_top5"])
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    args = ap.parse_args()
    mode = args.mode

    daily, etf_5min = load_data()
    if REGIME_PROXY not in daily:
        print(f"[warn] REGIME_PROXY {REGIME_PROXY} 不在日K缓存 -> 全部按中性处理")

    base = "510900" if "510900" in etf_5min else next(iter(etf_5min))
    all_days = sorted(etf_5min[base].keys())
    eval_dates = [d for d in all_days if args.start <= d <= args.end]
    day_pos = {d: i for i, d in enumerate(all_days)}

    equity = 1.0
    trades: list[dict] = []
    eq_curve: list[tuple[str, float]] = []

    for day in eval_dates:
        regime, pool = scan_1440(daily, etf_5min, day)
        pick = pick_at_1440(daily, etf_5min, pool, day, mode)
        final = confirm_1445(daily, etf_5min, pick, day, mode)
        if final is None:
            eq_curve.append((day, equity))
            continue
        g, code = final
        bp = buy_price(etf_5min, code, day)
        if bp is None or bp <= 0:
            eq_curve.append((day, equity))
            continue
        pos = day_pos[day]
        if pos + 1 >= len(all_days):
            eq_curve.append((day, equity))
            continue
        next_day = all_days[pos + 1]
        today_bars = etf_5min[code].get(day, [])
        next_bars = etf_5min[code].get(next_day, [])
        if not next_bars:
            eq_curve.append((day, equity))
            continue
        ret, reason, _detail = simulate_trix_cross_after(
            bp, today_bars, next_bars,
            trix_period=TRIX_PERIOD, trix_signal_period=TRIX_SIGNAL_PERIOD,
            min_sell_time="09:40", max_sell_time=SELL_CUTOFF,
        )
        net = ret - 2 * FEE_PCT_PT
        equity *= (1 + net / 100)
        trades.append({
            "buy": day, "sell": next_day, "code": code, "regime": regime,
            "buy_gain_1445": g, "ret": ret, "net": net, "reason": reason,
        })
        eq_curve.append((day, equity))

    # ---- 统计 ----
    n = len(trades)
    wins = sum(1 for t in trades if t["net"] > 0)
    peak = 1.0
    mdd = 0.0
    for _d, eq in eq_curve:
        peak = max(peak, eq)
        mdd = min(mdd, (eq - peak) / peak)

    print(f"\n=== R3 本地回测 mode={mode} ({args.start}~{args.end}) ===")
    print(f"候选宇宙 ATTACK_UNIVERSE={len(ATTACK_UNIVERSE)} 只 | "
          f"评估交易日={len(eval_dates)}")
    print(f"累计收益={(equity - 1) * 100:+.2f}%  笔数={n}  "
          f"胜率={wins / n * 100 if n else 0:.1f}%  MDD={mdd * 100:.1f}%")

    yr_first: dict[str, float] = {}
    yr_last: dict[str, float] = {}
    for d, eq in eq_curve:
        y = d[:4]
        if y not in yr_first:
            yr_first[y] = eq
        yr_last[y] = eq
    print("逐年(区间末 equity 相对年初):")
    for y in sorted(yr_first):
        yret = (yr_last[y] / yr_first[y] - 1) * 100
        nb = sum(1 for t in trades if t["buy"][:4] == y)
        print(f"  {y}: {yret:+.2f}%  (笔数 {nb})")

    print(f"命中 regime 分布: {dict(Counter(t['regime'] for t in trades))}")
    print(f"卖点原因: {dict(Counter(t['reason'] for t in trades))}")


if __name__ == "__main__":
    main()

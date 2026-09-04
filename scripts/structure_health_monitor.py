#!/usr/bin/env python3
"""结构健康度监控 — 两种结构的「失效体温计」周更落盘。

★ 设计原则(两个指标共同遵守):
  1. 测【机制】不测【盈亏】——盈亏滞后, 机制领先
  2. 用差值/相关【对冲掉市场方向】——隔离结构本身, 与 beta 正交
  3. 零是天然阈值——正=结构在, 负=结构反
  4. 在【策略之外】可计算——空仓也能更新, 故能提前预警
  5. 看分布和趋势, 不看单点

指标一 隔夜动量(A/B/R3) — 滚动 N 笔「上午溢价」
    单笔   d_i = (P_11:05 − P_14:50) / P_buy × 100      单位 pp
    滚动   A_t = mean(d_{t-N+1..t})                     N=60
    为什么是差值: 对冲掉市场方向。2023 熊市全程为负(11:05 −0.156 / 14:50 −0.308),
                  但溢价 +0.152pp 仍为正 → 形状没变, 只是水位低。
    健康基准: 实测 +0.53pp, 滚动恒正率 99%, 斜率 +0.0015(无衰减)
    失效含义: 直接亏钱(每天都交易, 无处可躲) → 必须主动减仓

指标二 趋势跟随(科创50) — 滚动 W 日「动量 IC」
    r_past = P_t/P_{t-20} − 1 ;  r_fut = P_{t+20}/P_t − 1
    IC_t   = corr({r_past}, {r_fut})  over W=250 交易日
    为什么是相关: 测「过去涨得多的未来是否继续涨」的信息含量, 与绝对涨跌无关。
    注意: 相关系数 SE ≈ 1/√(W−3) ≈ 0.064, 【单点读数不显著】, 必须看分布/趋势。
    失效含义: 收益停滞但不亏(N12簇自动翻空保护) → 什么都别做, 别放宽参数凑交易

用法:
    # 首次全量建基准(较慢, 5~10分钟)
    python3 scripts/structure_health_monitor.py --days 3000
    # cron 周更(增量, 约2分钟)
    python3 scripts/structure_health_monitor.py --days 250

输出: ~/.tradingagents/rotation/structure_health.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
DENSE_DIR = Path.home() / ".tradingagents/cache/t0_5min"
DENSE = ["tdx_5min_pre2024.json", "tdx_5min_2y.json"]
OUT = Path.home() / ".tradingagents/rotation/structure_health.json"

ROLL_TRADES = 60      # 指标一 滚动笔数
IC_WIN = 250          # 指标二 滚动天数
IC_LB = 20            # 指标二 过去/未来 天数
TREND_CODES = [("588000", "科创50"), ("588080", "科创板50"), ("516160", "新能源"),
               ("512480", "半导体"), ("159915", "创业板")]


# ══════════════════════════════════════════════════════════════════
# 指标一: 隔夜动量「上午溢价」
# ══════════════════════════════════════════════════════════════════
def close_at(bars: list[dict], tstr: str) -> float | None:
    from backtest_t0_etf import bar_time_min
    from backtest_t0_today1 import time_to_min
    cm = time_to_min(tstr)
    last = None
    for b in sorted(bars, key=bar_time_min):
        if bar_time_min(b) <= cm:
            last = float(b["close"])
    return last


def compute_am_premium(dates, etf_list, etf_daily, etf_5min):
    """返回 (滚动序列[(end_date, value)], 单笔序列[(day, d)])。

    用 B 选股产生信号 —— 但【不依赖策略是否真的开仓】, 故空仓也能更新。
    三策略溢价实测接近(B +0.532 / A +0.537 / R3 +0.600), 以 B 为代表。
    """
    from backtest_b_idle_merge import SIGNAL_TIME, build_picks_B
    from backtest_t0_etf import price_at_time

    picks = build_picks_B(dates, etf_list, etf_daily, etf_5min, 0)
    diffs: list[tuple[str, float]] = []
    dset = set(dates)
    for i, day in enumerate(dates):
        p = picks.get((SIGNAL_TIME, day))
        if not p:
            continue
        code = p[0]
        db = etf_5min.get(code, {}).get(day, [])
        buy = price_at_time(db, "14:50")
        if not buy or buy <= 0:
            continue
        if i + 1 >= len(dates):
            continue
        nb = etf_5min.get(code, {}).get(dates[i + 1], [])
        if not nb:
            continue
        a = close_at(nb, "11:05")
        b = close_at(nb, "14:50")
        if a is None or b is None:
            continue
        diffs.append((day, (a - b) / buy * 100))

    roll = []
    for i in range(len(diffs) - ROLL_TRADES + 1):
        seg = diffs[i:i + ROLL_TRADES]
        roll.append((seg[-1][0], sum(d for _, d in seg) / ROLL_TRADES))
    return roll, diffs


# ══════════════════════════════════════════════════════════════════
# 指标二: 趋势跟随「动量 IC」
# ══════════════════════════════════════════════════════════════════
def momentum_ic(close: np.ndarray, lb=IC_LB, fwd=IC_LB, win=IC_WIN):
    """滚动 win 日 corr(过去 lb 日收益, 未来 fwd 日收益)。返回 (index_list, ic_list)。"""
    c = pd.Series(np.asarray(close, float))
    r_past = (c / c.shift(lb) - 1)
    r_fut = (c.shift(-fwd) / c - 1)
    df = pd.DataFrame({"p": r_past, "f": r_fut}).dropna()
    if len(df) < win:
        return [], []
    ic = df["p"].rolling(win).corr(df["f"]).dropna()
    return list(ic.index), [round(float(v), 4) for v in ic.values]


def fetch_daily(code: str, start="2020-11-16"):
    """前复权日K(东财) — pytdx 原始价在 ETF 份额折算日有假跳空, 会污染 IC。"""
    try:
        from etf_qfq_data import fetch_qfq_close
        return fetch_qfq_close(code, start=start)
    except Exception:
        try:
            from backtest_588000_n12 import fetch_day_code
            return fetch_day_code(code, start)
        except Exception:
            return None


def main() -> None:
    ap = argparse.ArgumentParser(description="结构健康度监控")
    ap.add_argument("--days", type=int, default=250,
                    help="计算最近 N 个交易日(增量用 250, 首次建基准用 3000)")
    ap.add_argument("--no-5min", action="store_true", help="跳过指标一(只算趋势IC, 快)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    out_path = Path(args.out)
    prev: dict = {}
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            prev = {}

    result: dict = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "params": {"roll_trades": ROLL_TRADES, "ic_win": IC_WIN, "ic_lb": IC_LB},
    }

    # ── 指标一 ──
    if not args.no_5min:
        print("载入缓存(5min, 较慢) ...", flush=True)
        cache = json.loads(Path(CACHE).read_text(encoding="utf-8"))
        etf_daily = cache["etf_daily"]
        etf_5min: dict = {}
        for name in DENSE:
            p = DENSE_DIR / name
            print(f"  {p.name} ...", flush=True)
            for c, days in json.loads(p.read_text(encoding="utf-8"))["etf_5min"].items():
                etf_5min.setdefault(c, {}).update(days)

        cnt: Counter = Counter()
        for _c, days in etf_5min.items():
            for d, bars in days.items():
                if bars:
                    cnt[d] += 1
        five_dates = sorted(d for d, n in cnt.items() if n >= 20)
        all_dates = sorted(set(cache["all_dates"]) & set(five_dates))

        from t0_etf_list import get_all_t0_etfs
        etf_list = [e for e in get_all_t0_etfs() if e["code"] in set(etf_5min)]
        dates = all_dates[-args.days:] if args.days < len(all_dates) else all_dates

        print(f"计算上午溢价: {dates[0]} ~ {dates[-1]} ({len(dates)}天) ...", flush=True)
        roll, diffs = compute_am_premium(dates, etf_list, etf_daily, etf_5min)

        # 与历史合并(按 end_date 去重)
        hist = {d: v for d, v in (prev.get("overnight_momentum", {}).get("history") or [])}
        for d, v in roll:
            hist[d] = round(v, 4)
        series = sorted(hist.items())

        if series:
            vals = [v for _, v in series]
            cur = vals[-1]
            pos_rate = sum(1 for v in vals if v > 0) / len(vals) * 100
            n = len(vals)
            if n > 2:
                xm, ym = (n - 1) / 2, sum(vals) / n
                slope = (sum((i - xm) * (v - ym) for i, v in enumerate(vals))
                         / sum((i - xm) ** 2 for i in range(n)))
            else:
                slope = 0.0
            # 最近连续为负窗口数
            streak = 0
            for v in reversed(vals):
                if v <= 0:
                    streak += 1
                else:
                    break
            if cur > 0.4 and streak == 0:
                verdict, level = "健康", "ok"
            elif streak == 0:
                verdict, level = "减弱(仍为正)", "warn"
            elif streak < 3:
                verdict, level = f"预警(连续{streak}个窗口≤0)", "warn"
            else:
                verdict, level = f"结构反转(连续{streak}个窗口≤0)", "stop"

            by_year: dict[str, list[float]] = {}
            for d, v in series:
                by_year.setdefault(d[:4], []).append(v)
            result["overnight_momentum"] = {
                "label": "隔夜动量结构 (A/B/R3)",
                "metric": f"滚动{ROLL_TRADES}笔上午溢价 (11:05收益 − 14:50收益)",
                "unit": "pp",
                "current": round(cur, 4),
                "positive_rate": round(pos_rate, 1),
                "slope": round(slope, 5),
                "neg_streak": streak,
                "verdict": verdict,
                "level": level,
                "window_count": len(series),
                "sample_trades": len(diffs),
                "by_year": {y: round(sum(v) / len(v), 4) for y, v in sorted(by_year.items())},
                "history": [[d, v] for d, v in series],
                "thresholds": {"healthy": 0.4, "warn": 0.0, "stop_streak": 3},
                "note": "代表 B 选股口径(三策略溢价接近: B +0.532 / A +0.537 / R3 +0.600)。"
                        "不依赖策略是否开仓, 空仓期照常更新。",
            }
            print(f"  ✓ 当前 {cur:+.4f}pp | 恒正率 {pos_rate:.0f}% | "
                  f"连续负窗口 {streak} | 判定 {verdict}")
    else:
        om = prev.get("overnight_momentum")
        if om:
            result["overnight_momentum"] = om

    # ── 指标二 ──
    print("计算动量 IC(日线) ...", flush=True)
    ic_block: dict = {"label": "趋势跟随结构 (科创50 及对照)",
                      "metric": f"滚动{IC_WIN}日动量IC corr(过去{IC_LB}日, 未来{IC_LB}日)",
                      "unit": "corr", "series": {}}
    for code, name in TREND_CODES:
        s = fetch_daily(code)
        if s is None or len(s) < IC_WIN + IC_LB * 2:
            print(f"  {code} {name} 数据不足, 跳过")
            continue
        idx, ics = momentum_ic(s.values.astype(float))
        if not ics:
            continue
        ser = pd.Series(ics, index=[s.index[i].strftime("%Y-%m-%d") for i in idx])
        by_year = {str(y): round(float(v), 4) for y, v in ser.groupby(ser.index.str[:4]).mean().items()}
        pos_rate = float((ser > 0).sum() / len(ser) * 100)
        ic_block["series"][code] = {
            "name": name,
            "current": round(float(ics[-1]), 4),
            "median": round(float(np.median(ics)), 4),
            "positive_rate": round(pos_rate, 1),
            "recent_250_mean": round(float(ser.iloc[-250:].mean()) if len(ser) >= 250
                                     else float(ser.mean()), 4),
            "by_year": by_year,
            "history": [[str(d), v] for d, v in ser.items()],
        }
        print(f"  ✓ {name:<8} 当前 {ics[-1]:+.3f} | 中位 {np.median(ics):+.3f} | "
              f"为正占比 {pos_rate:.0f}%")

    if ic_block["series"]:
        base = ic_block["series"].get("588000") or list(ic_block["series"].values())[0]
        cur = base["current"]
        # 单点不显著(SE≈0.064), 需结合分布判定
        if cur > 0.1:
            verdict, level = "趋势市(结构活跃)", "ok"
        elif cur > -0.1:
            verdict, level = "中性/噪音区(单点不显著, 看分布)", "warn"
        else:
            streak_y = [y for y, v in sorted(base["by_year"].items()) if v < -0.1]
            verdict = f"反转市(IC {cur:+.3f})"
            level = "stop" if len(streak_y) >= 2 else "warn"
        ic_block["verdict"] = verdict
        ic_block["level"] = level
        ic_block["primary_code"] = "588000"
        ic_block["thresholds"] = {"healthy": 0.1, "warn": -0.1}
        ic_block["note"] = ("相关系数 SE≈1/√(W−3)≈0.064, 单点读数不显著, 必须结合"
                            "「为中位/为正占比/逐年」判断。IC 转负 → 策略自动空仓、"
                            "收益停滞但不亏, 正确做法是【别放宽参数凑交易】。")
    result["trend_following"] = ic_block

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n落盘: {out_path}")
    om = result.get("overnight_momentum")
    if om:
        print(f"  指标一 上午溢价 : {om['current']:+.4f}pp  {om['verdict']}")
    print(f"  指标二 动量IC   : {ic_block.get('verdict', '—')}")


if __name__ == "__main__":
    main()

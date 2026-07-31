#!/usr/bin/env python3
"""动量隔夜腿 + 指标动态卖出探索 —— 直接回答「固定 14:50 vs TRIX/OBV/trail 指标卖出哪个更高」。

框架与 H1(search_idle_overnight_reversal) 一致(抗过拟合):
    idle 日(核心 14:45 未触发) 14:30 买当日最强涨幅 ≥ thr 的 T0 ETF,
    隔夜持有, 次日用不同卖出模式平仓:
      - time:<固定时点>  (H1 的 9 档固定卖出)
      - trix            (次日 TRIX(5,3) 死叉, 与实盘核心同逻辑)
      - trail           (移动止盈/止损)
      - obv             (次日 OBV 拐头)
    walk-forward 60/40 + 多重检验 + 扣双边费(万3). 基准=现金 0%.

目的: 给「合并回测」选最优卖出模块——若指标卖出 > 固定 14:50, 合并时用指标。

用法:
    python scripts/search_idle_overnight_signal_sell.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_etf import apply_net_return, bar_time_min, price_at_time  # noqa: E402
from backtest_t0_idle_window import (  # noqa: E402
    sell_time_mode,
    sell_trail_mode,
    sell_trix_mode,
)
from backtest_top1_minute import calc_obv  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    TRIX_MIN_SELL,
    next_trading_day,
    regime_on_date,
    resolve_eval_dates,
    time_to_min,
)
from search_t0_time_combo import bars_until  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
OUT_FILE = Path.home() / ".tradingagents/cache/t0_5min/idle_overnight_signal_sell.json"

BUY_TIME = "14:50"          # 修正: idle日只能14:45核心判定后才知道, 故动量腿也14:50买
MIN_TRADES = 25             # 训练段最少笔数

# 动量选股阈值（买当日最强涨幅 ≥ thr）
THR_MOMENTUM = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]
# 固定卖出时点（早盘→尾盘）
TIME_SELLS = ["09:40", "09:50", "10:00", "10:30", "11:00",
              "11:30", "13:30", "14:00", "14:50"]
REGIME_FILTERS = ["off", "trend_up"]

# 卖出模式规范: ("time", 时点) / ("trix", None) / ("trail", None) / ("obv", None)
SELL_SPECS = ([("time", s) for s in TIME_SELLS]
              + [("trix", None), ("trail", None), ("obv", None)])


# ─────────────────── 预建昨收索引 ───────────────────

def build_prev_close(etf_list: list[dict], etf_daily: dict) -> dict[str, dict[str, float]]:
    pc: dict[str, dict[str, float]] = {}
    for etf in etf_list:
        code = etf["code"]
        info = etf_daily.get(code)
        if not info:
            continue
        d: dict[str, float] = {}
        returns = info.get("returns", [])
        for i in range(1, len(returns)):
            p = returns[i - 1].get("close")
            if p and p > 0:
                d[returns[i]["date"]] = float(p)
        pc[code] = d
    return pc


def pick_momentum(
    etf_list: list[dict],
    etf_5min: dict,
    prev_close: dict[str, dict[str, float]],
    day: str,
    threshold: float,
) -> tuple[str, float] | None:
    """取 14:30 时刻「当日涨幅最高(Top1 最强)」的 T0 ETF；要求涨幅 ≥ threshold。"""
    cands: list[tuple[float, str]] = []
    for etf in etf_list:
        code = etf["code"]
        prev = prev_close.get(code, {}).get(day)
        if not prev or prev <= 0:
            continue
        bars = etf_5min.get(code, {}).get(day, [])
        px = price_at_time(bars, BUY_TIME)
        if px is None or px <= 0:
            continue
        gain = (px - prev) / prev * 100
        cands.append((gain, code))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0], reverse=True)
    best_gain, best_code = cands[0]
    if best_gain < threshold:
        return None
    return best_code, best_gain


# ─────────────────── OBV 拐头卖出（自写，复用 calc_obv） ───────────────────

def sell_obv_mode(
    day_bars: list[dict],
    buy_time: str,
    sell_cutoff: str,
    buy_price: float,
    fee_pct: float,
) -> tuple[float, str] | None:
    """次日 OBV 拐头(升转降)卖出；若全天未拐头则尾盘卖。

    OBV 比价格更领先地反映资金流向，拐头往往早于价格见顶。
    """
    window = bars_until(day_bars, sell_cutoff)
    if not window:
        return None
    obv = calc_obv(window)
    if len(obv) < 3 or max(obv) == min(obv):
        sp = float(window[-1]["close"])
        if sp > 0:
            return apply_net_return(buy_price, sp, fee_pct), "obv_hold"
        return None
    min_sell = time_to_min(TRIX_MIN_SELL)  # 与实盘一致，忽略早盘
    for i in range(2, len(obv)):
        if bar_time_min(window[i]) < min_sell:
            continue
        # 局部顶后拐头: obv[i-1] 是顶, obv[i] 回落
        if obv[i - 1] >= obv[i - 2] and obv[i] < obv[i - 1]:
            sp = float(window[i]["close"])
            if sp > 0:
                return apply_net_return(buy_price, sp, fee_pct), "obv_turn"
    sp = float(window[-1]["close"])
    if sp > 0:
        return apply_net_return(buy_price, sp, fee_pct), "obv_hold"
    return None


def do_sell(mode: tuple[str, str | None], next_bars: list[dict],
            bp: float, fee: float) -> tuple[float, str] | None:
    kind, arg = mode
    if kind == "time":
        return sell_time_mode(next_bars, "14:30", arg, bp, fee)
    if kind == "trix":
        return sell_trix_mode(next_bars, "09:35", "14:55", bp, fee)
    if kind == "trail":
        return sell_trail_mode(next_bars, "09:35", "14:55", bp, fee)
    if kind == "obv":
        return sell_obv_mode(next_bars, "09:35", "14:55", bp, fee)
    return None


def mode_label(mode: tuple[str, str | None]) -> str:
    kind, arg = mode
    if kind == "time":
        return f"time:{arg}"
    return kind


# ─────────────────── 统计 ───────────────────

def stats_of(rets: list[float]) -> dict:
    n = len(rets)
    if n == 0:
        return {"n": 0}
    avg = sum(rets) / n
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    if n > 1:
        var = sum((r - avg) ** 2 for r in rets) / (n - 1)
        std = math.sqrt(var)
    else:
        std = 0.0
    t = avg / std * math.sqrt(n) if std > 0 else 0.0
    peak = cur = 1.0
    mdd = 0.0
    for r in rets:
        cur *= 1 + r / 100
        peak = max(peak, cur)
        mdd = min(mdd, (cur - peak) / peak * 100)
    return {
        "n": n, "avg": round(avg, 4), "std": round(std, 4), "t": round(t, 3),
        "cum_pct": round((eq - 1) * 100, 2),
        "win_rate": round(sum(1 for r in rets if r > 0) / n * 100, 1),
        "mdd_pct": round(mdd, 2),
    }


# ─────────────────── 主流程 ───────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="动量隔夜腿 指标卖出探索")
    ap.add_argument("--cache", type=str, default=str(CACHE_FILE))
    ap.add_argument("--start", type=str, default="2022-06-15")
    ap.add_argument("--end", type=str, default="")
    ap.add_argument("--split", type=float, default=0.6)
    ap.add_argument("--fee", type=float, default=FEE_PCT)
    args = ap.parse_args()

    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    all_dates = cache["all_dates"]
    proxy = cache.get("proxy_klines", [])
    eval_dates = resolve_eval_dates(all_dates, 0, args.start, args.end)
    etf_list = get_all_t0_etfs()
    prev_close = build_prev_close(etf_list, etf_daily)

    print("=== 动量隔夜腿 · 指标动态卖出探索 ===")
    print(f"    区间 {eval_dates[0]} ~ {eval_dates[-1]}（{len(eval_dates)} 交易日）")
    print(f"    买 {BUY_TIME} 最强涨幅≥thr | 隔夜 | 次晨卖出模式{TIME_SELLS}+trix/trail/obv")

    # ① eligible：核心 14:45(≥3%) 未触发 = 资金隔夜闲置日
    print("\n>>> [1/5] 计算核心信号与 idle 日 ...")
    base_pick: dict[str, bool] = {}
    for day in eval_dates:
        reg = regime_on_date(proxy, day)
        if reg and reg.get("skip_choppy"):
            base_pick[day] = True
            continue
        scores = []
        for etf in etf_list:
            code = etf["code"]
            prev = prev_close.get(code, {}).get(day)
            if not prev or prev <= 0:
                continue
            bars = etf_5min.get(code, {}).get(day, [])
            px = price_at_time(bars, "14:45")
            if px is None or px <= 0:
                continue
            scores.append(((px - prev) / prev * 100, code))
        scores.sort(key=lambda x: x[0], reverse=True)
        base_pick[day] = bool(scores) and scores[0][0] >= 3.0

    idle_days = [d for d in eval_dates if not base_pick.get(d)]
    print(f"    idle(隔夜闲置)日: {len(idle_days)} 天 / {len(eval_dates)}")

    split_at = int(len(idle_days) * args.split)
    train_days = set(idle_days[:split_at])
    test_days = set(idle_days[split_at:])
    print(f"    训练 {len(train_days)} 天 | 验证 {len(test_days)} 天")

    # ② 预计算各 (threshold, regime) 动量选股
    print("\n>>> [2/5] 预计算各 (阈值,regime) 动量选股 ...")
    picks: dict[tuple[float, str], dict[str, tuple[str, float] | None]] = {}
    for thr in THR_MOMENTUM:
        for rf in REGIME_FILTERS:
            key = (thr, rf)
            picks[key] = {}
            for day in idle_days:
                if rf == "trend_up":
                    reg = regime_on_date(proxy, day)
                    if not (reg and reg.get("regime") == "up"):
                        picks[key][day] = None
                        continue
                picks[key][day] = pick_momentum(
                    etf_list, etf_5min, prev_close, day, thr
                )
    print(f"    预计算 {len(THR_MOMENTUM) * len(REGIME_FILTERS)} 个 (阈值×regime) 选股表")

    # ③ 网格模拟
    print("\n>>> [3/5] 网格模拟 ...")
    results: list[dict] = []
    sim_cache: dict[tuple, tuple[float, str] | None] = {}

    def simulate(code: str, day: str, mode) -> tuple[float, str] | None:
        key = (code, day, mode)
        if key in sim_cache:
            return sim_cache[key]
        nday = next_trading_day(all_dates, day)
        if not nday:
            sim_cache[key] = None
            return None
        bars_b = etf_5min.get(code, {}).get(day, [])
        bars_s = etf_5min.get(code, {}).get(nday, [])
        bp = price_at_time(bars_b, BUY_TIME)
        out = None
        if bp and bp > 0 and bars_s:
            out = do_sell(mode, bars_s, bp, args.fee)
        sim_cache[key] = out
        return out

    combos = [(thr, rf, m) for thr in THR_MOMENTUM
              for rf in REGIME_FILTERS for m in SELL_SPECS]
    print(f"    组合数: {len(combos)}")
    for ci, (thr, rf, mode) in enumerate(combos, 1):
        tr_rets: list[float] = []
        te_rets: list[float] = []
        by_day: dict[str, float] = {}
        for day, pk in picks[(thr, rf)].items():
            if not pk:
                continue
            code = pk[0]
            out = simulate(code, day, mode)
            if not out:
                continue
            ret, _ = out
            by_day[day] = ret
            (tr_rets if day in train_days else te_rets).append(ret)
        if len(tr_rets) < MIN_TRADES:
            continue
        results.append({
            "threshold": thr, "regime_filter": rf, "sell_mode": mode_label(mode),
            "mode_kind": mode[0],
            "label": f"动:涨≥{thr:.1f}%→{BUY_TIME}买→次晨[{mode_label(mode)}]卖[{rf}]",
            "train": stats_of(tr_rets),
            "test": stats_of(te_rets),
            "full": stats_of(tr_rets + te_rets),
            "by_day": by_day,
        })
        if ci % 60 == 0:
            print(f"    {ci}/{len(combos)}")

    if not results:
        print("无满足最小笔数的组合")
        sys.exit(1)

    n_eff = len(results)
    t_noise = math.sqrt(2 * math.log(n_eff))

    results.sort(key=lambda r: r["test"]["avg"], reverse=True)

    print("\n" + "=" * 120)
    print(f"  [4/5] 样本外均笔 TOP 16 | N={n_eff} | 噪声门槛 t_noise={t_noise:.2f} | 基准=现金0%")
    print("=" * 120)
    head = (f"  {'#':>2} {'组合':<46} {'IS笔':>4} {'IS均笔':>7} {'IS_t':>6} "
            f"{'OOS笔':>5} {'OOS均笔':>8} {'OOS_t':>6} {'OOS累计':>9} {'过噪声':>6}")
    print(head)
    print("  " + "-" * 116)
    for i, r in enumerate(results[:16], 1):
        tr, te = r["train"], r["test"]
        flag = "✓" if tr["t"] > t_noise else "✗"
        print(
            f"  {i:>2} {r['label']:<46} {tr['n']:>4} {tr['avg']:>+7.3f} {tr['t']:>6.2f} "
            f"{te.get('n', 0):>5} {te.get('avg', 0):>+8.3f} {te.get('t', 0):>6.2f} "
            f"{te.get('cum_pct', 0):>+8.1f}% {flag:>6}"
        )
    print("=" * 120)

    # ⑤ 分组对比：每种卖出模式的最佳 OOS 组合（直接回答「指标 vs 固定」）
    print("\n>>> [5/5] 各卖出模式最佳 OOS 对比（回答「指标卖出会不会更高」）")
    print(f"  {'卖出模式':<10}{'最佳阈值':>9}{'regime':>8}{'OOS笔':>6}{'OOS均笔':>9}"
          f"{'OOS累计':>10}{'IS均笔':>9}{'IS_t':>7}{'胜率':>6}")
    print("  " + "-" * 78)
    mode_rows = []
    for mode in SELL_SPECS:
        ml = mode_label(mode)
        grp = [r for r in results if r["sell_mode"] == ml]
        if not grp:
            continue
        b = max(grp, key=lambda r: r["test"].get("avg", 0))
        te = b["test"]
        mode_rows.append((ml, b, te))
        print(f"  {ml:<10}{b['threshold']:>+8.1f}%{b['regime_filter']:>8}{te['n']:>6}"
              f"{te['avg']:>+8.3f}%{te['cum_pct']:>+9.1f}%{b['train']['avg']:>+8.3f}%"
              f"{b['train']['t']:>7.2f}{te.get('win_rate', 0):>5.1f}%")
    # 高亮固定 14:50 vs 三个指标
    print("\n  ── 固定 14:50 vs 指标动态 ──")
    fixed = next((r for r in mode_rows if r[0] == "time:14:50"), None)
    for kind in ("trix", "trail", "obv"):
        row = next((r for r in mode_rows if r[0] == kind), None)
        if fixed and row:
            fixed_oos = fixed[2]["avg"]
            delta = row[2]["avg"] - fixed_oos
            verdict = "指标更优✓" if delta > 0 else "固定更优"
            print(f"    {kind:<6} OOS均笔 {row[2]['avg']:>+7.3f}%  vs  固定14:50 "
                  f"{fixed_oos:>+7.3f}%  (Δ{delta:>+6.3f}%)  → {verdict}")

    is_avgs = sorted(r["train"]["avg"] for r in results)
    oos_avgs = sorted(r["test"].get("avg", 0) for r in results if r["test"].get("n", 0) >= 10)

    def pctl(xs: list[float], q: float) -> float:
        if not xs:
            return 0.0
        return xs[min(len(xs) - 1, int(len(xs) * q))]

    print("\n  ── 全 %d 组合均笔收益分布（%%/笔，已扣双边费，基准=0%%现金）──" % n_eff)
    print(f"    {'':<8}{'p10':>9}{'p25':>9}{'中位':>9}{'p75':>9}{'p90':>9}{'正比例':>9}")
    for tag, xs in (("训练段", is_avgs), ("样本外", oos_avgs)):
        pos = sum(1 for x in xs if x > 0) / len(xs) * 100 if xs else 0
        print(f"    {tag:<8}{pctl(xs,0.1):>+9.3f}{pctl(xs,0.25):>+9.3f}{pctl(xs,0.5):>+9.3f}"
              f"{pctl(xs,0.75):>+9.3f}{pctl(xs,0.9):>+9.3f}{pos:>8.1f}%")

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "window": [args.start, args.end or eval_dates[-1]],
        "idle_days": len(idle_days),
        "split": args.split,
        "n_combos": n_eff,
        "t_noise": round(t_noise, 3),
        "mode_best": [{
            "sell_mode": ml,
            "threshold": b["threshold"],
            "regime_filter": b["regime_filter"],
            "train": b["train"], "test": b["test"], "full": b["full"],
        } for ml, b, te in mode_rows],
        "oos_top": [{k: v for k, v in r.items() if k != "by_day"} for r in results[:20]],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {OUT_FILE}")


if __name__ == "__main__":
    main()

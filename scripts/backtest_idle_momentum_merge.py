#!/usr/bin/env python3
"""合并回测: 核心(hybrid-A) + 动量隔夜腿(idle日) —— 把闲置资金从 0% 拉起来。

逻辑:
    信号日 (build_picks_hybrid 选出 ≥3% Top1): 跑核心腿 (14:50买→次日09:40~11:05 TRIX卖)
    idle 日 (核心 14:45 未触发):               跑动量隔夜腿 (14:50买当日最强涨幅≥thr→次日固定14:50卖)

两腿都在 14:50 决策、按 idle/signal 路由到不同腿, 资金每天最多一笔, 按买入日排序复利即等价单资金串行。
【修正 look-ahead】动量腿买入时间从 14:30 改为 14:50 —— 因为是否 idle 只能由 14:45 核心信号判定决定,
14:30 时无法预知。选股与买入价均按 14:50 计算, 与实盘一致, 无未来函数。
动量腿卖出已用 search_idle_overnight_signal_sell.py 验证: 固定14:50 优于 TRIX/OBV/trail 指标动态。

用法:
    python scripts/backtest_idle_momentum_merge.py
    python scripts/backtest_idle_momentum_merge.py --momentum-sell 11:00   # 用11:00折中卖
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_etf import apply_net_return, price_at_time  # noqa: E402
from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa: E402
from backtest_t0_idle_window import sell_time_mode, sell_trix_mode  # noqa: E402
from search_idle_overnight_signal_sell import do_sell, SELL_SPECS  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    next_trading_day,
    resolve_eval_dates,
)
from quality_pool import build_picks_hybrid  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
OUT = Path.home() / ".tradingagents/cache/t0_5min/idle_momentum_merge.json"
START = "2022-06-15"
LB = 30  # 与 report_live_validated 一致

MOM_BUY = "14:50"     # 修正: 必须等 14:45 核心信号判定后才知道是否 idle, 故动量腿也 14:50 买
MOM_THR = 0.5          # 动量选股阈值(已验证最优)
MOM_SELL_DEFAULT = "14:50"
THR_LIST = [0.5, 1.0, 1.5, 2.0, 3.0]   # walk-forward 选参用的阈值网格


def sell_by_mode(mode: str, next_bars: list[dict], bp: float, fee: float):
    """动量腿卖出统一入口:
       "trix"            → 纯TRIX死叉卖(兜底14:55)
       "trix:11:05"/"trix:14:50" → TRIX死叉卖, 未触发则到指定时刻兜底
       其他 "HH:MM"      → 固定时刻卖
    """
    if mode == "trix":
        return do_sell(("trix", None), next_bars, bp, fee)
    if mode.startswith("trix:"):
        fb = mode.split(":", 1)[1]
        return sell_trix_mode(next_bars, "09:35", fb, bp, fee)
    return sell_time_mode(next_bars, MOM_BUY, mode, bp, fee)



def build_prev_close(etf_list, etf_daily) -> dict:
    pc = {}
    for etf in etf_list:
        code = etf["code"]
        info = etf_daily.get(code)
        if not info:
            continue
        d = {}
        returns = info.get("returns", [])
        for i in range(1, len(returns)):
            p = returns[i - 1].get("close")
            if p and p > 0:
                d[returns[i]["date"]] = float(p)
        pc[code] = d
    return pc


def pick_momentum(etf_list, etf_5min, prev_close, day, threshold):
    """idle 日 14:30 取当日涨幅最高(Top1 最强)的 T0 ETF；要求涨幅 ≥ threshold。"""
    cands = []
    for etf in etf_list:
        code = etf["code"]
        prev = prev_close.get(code, {}).get(day)
        if not prev or prev <= 0:
            continue
        bars = etf_5min.get(code, {}).get(day, [])
        px = price_at_time(bars, MOM_BUY)
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


def per_year(trades):
    by = defaultdict(list)
    for t in trades:
        by[t["signal_date"][:4]].append(t["return_pct"])
    out = {}
    for y in sorted(by):
        rs = by[y]
        eq = 1.0
        for x in rs:
            eq *= 1 + x / 100
        out[y] = {
            "trades": len(rs),
            "year_return_pct": round((eq - 1) * 100, 2),
            "win_rate": round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1),
        }
    return out


def merge_equity(trades):
    ts = sorted(trades, key=lambda t: t["signal_date"])
    eq = 1.0
    for t in ts:
        eq *= 1 + t["return_pct"] / 100
    return (eq - 1) * 100


def main():
    ap = argparse.ArgumentParser(description="核心+动量隔夜腿 合并回测")
    ap.add_argument("--cache", type=str, default=str(CACHE_FILE))
    ap.add_argument("--start", type=str, default=START)
    ap.add_argument("--momentum-thr", type=float, default=MOM_THR)
    ap.add_argument("--momentum-sell", type=str, default=MOM_SELL_DEFAULT)
    ap.add_argument("--mode", type=str, default="both",
                    choices=["full", "wf", "both", "cmp", "idle_only"],
                    help="full=样本内全量; wf=walk-forward隔离验证; both=两者; "
                         "cmp=三种动量卖点WF-OOS对比; "
                         "idle_only=全跑idle动量腿(不与核心混合), 评估idle策略独立收益")
    ap.add_argument("--fee", type=float, default=FEE_PCT)
    args = ap.parse_args()

    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    proxy = cache.get("proxy_klines", [])
    codes5 = set(etf_5min.keys())
    orig_pool = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    etf_list = get_all_t0_etfs()
    prev_close = build_prev_close(etf_list, etf_daily)

    eval_dates = [d for d in all_dates if d >= args.start]
    print(f"=== 合并回测 核心(hybrid-A)+动量隔夜腿(idle日) ===")
    print(f"    区间 {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}天)")
    if args.momentum_sell == "trix":
        sell_label = "次日TRIX(5,3)死叉卖(兜底14:55)"
    elif args.momentum_sell.startswith("trix:"):
        fb = args.momentum_sell.split(":", 1)[1]
        sell_label = f"次日TRIX(5,3)死叉卖, 未触发则{fb}兜底"
    else:
        sell_label = f"次日固定{args.momentum_sell}卖"
    print(f"    动量腿: {MOM_BUY}买当日最强≥{args.momentum_thr:.1f}% → {sell_label}")
    print(f"    数据: 5min+日K+proxy 完整(实盘等价窗口)\n")

    # ① 核心腿 (hybrid-A 同款)
    picks_a = build_picks_hybrid(
        eval_dates, orig_pool, etf_daily, etf_5min, all_dates, proxy,
        lookback=LB, warmup=LB,
    )
    core = run_strategy("trix", eval_dates, all_dates, picks_a, etf_5min, FEE_PCT)
    core_trades = core["trades"] if core else []
    core_eq = core["final_equity_pct"] if core else 0.0
    print(f"★ 纯核心 hybrid-A:  {core_eq:+.2f}%  {len(core_trades)}笔  "
          f"胜{core['stats']['win_rate']:.0f}%  回撤{core['stats']['max_drawdown']:+.1f}%")
    for y, s in per_year(core_trades).items():
        print(f"    {y}: {s['year_return_pct']:+.2f}%  {s['trades']}笔 胜{s['win_rate']:.0f}%")

    # ② 动量隔夜腿 (idle 日, 与核心 warmup 对齐) —— 顶层计算, wf/cmp 复用
    idle_days = [d for d in eval_dates[LB:] if not picks_a.get((SIGNAL_TIME, d))]
    print(f"\n>>> idle(隔夜闲置)日: {len(idle_days)} 天（已排除核心 warmup 前 {LB} 天）")

    momentum_trades = []
    mom_skipped_no_pick = 0
    for day in idle_days:
        pk = pick_momentum(etf_list, etf_5min, prev_close, day, args.momentum_thr)
        if not pk:
            mom_skipped_no_pick += 1
            continue
        code, gain = pk
        day_bars = etf_5min.get(code, {}).get(day, [])
        bp = price_at_time(day_bars, MOM_BUY)
        if not bp or bp <= 0:
            continue
        nday = next_trading_day(all_dates, day)
        if not nday:
            continue
        next_bars = etf_5min.get(code, {}).get(nday, [])
        out = sell_by_mode(args.momentum_sell, next_bars, bp, args.fee)
        if not out:
            continue
        ret, reason = out
        momentum_trades.append({
            "signal_date": day, "sell_date": nday, "etf": code,
            "today_gain": round(gain, 2), "return_pct": round(ret, 4),
            "sell_reason": "momentum_" + reason,
        })
    mom_eq = merge_equity(momentum_trades)
    print(f"    动量腿触发: {len(momentum_trades)} 笔（idle日中 {mom_skipped_no_pick} 天无≥{args.momentum_thr:.1f}%标的）")
    print(f"    动量腿单独:    {mom_eq:+.2f}%  {len(momentum_trades)}笔")
    for y, s in per_year(momentum_trades).items():
        print(f"    {y}: {s['year_return_pct']:+.2f}%  {s['trades']}笔 胜{s['win_rate']:.0f}%")

    # ③ 合并
    merged = core_trades + momentum_trades
    merged_eq = merge_equity(merged)
    # 合并统计
    all_rets = [t["return_pct"] for t in merged]
    n = len(all_rets)
    win = sum(1 for r in all_rets if r > 0) / n * 100 if n else 0
    eq = 1.0
    peak = cur = 1.0
    mdd = 0.0
    for r in all_rets:
        eq *= 1 + r / 100
        cur *= 1 + r / 100
        peak = max(peak, cur)
        mdd = min(mdd, (cur - peak) / peak * 100)
    print(f"\n>>> 合并 (核心+动量): {merged_eq:+.2f}%  {n}笔  "
          f"胜{win:.0f}%  回撤{mdd:+.1f}%")
    print(f"    = 纯核心 {core_eq:+.2f}%  +  动量增量 {merged_eq - core_eq:+.2f}%")
    py_c = per_year(core_trades)
    py_m = per_year(momentum_trades)
    py_all = per_year(merged)
    print(f"\n  {'年':<6}{'核心':>10}{'动量腿':>10}{'合并':>10}")
    print("  " + "-" * 36)
    for y in sorted(py_all):
        c = py_c.get(y, {"year_return_pct": 0})
        m = py_m.get(y, {"year_return_pct": 0})
        a = py_all[y]
        print(f"  {y:<6}{c['year_return_pct']:>+9.1f}%{m['year_return_pct']:>+9.1f}%{a['year_return_pct']:>+9.1f}%")

    # ── Walk-Forward 隔离验证(动量参数不被全样本拟合) ──
    # ── 三种动量卖点 Walk-Forward OOS 对比(独立分支, 与其他模式互斥) ──
    if args.mode == "cmp":
        split = int(len(idle_days) * 0.6)
        train_idle = idle_days[:split]
        test_idle = idle_days[split:]
        test_start = test_idle[0]
        core_test = [t for t in core_trades if t["signal_date"] >= test_start]
        eq_core_test = merge_equity(core_test)
        print(f"\n>>> 三种动量卖点 · Walk-Forward OOS 对比")
        print(f"    训练段 {train_idle[0]}~{train_idle[-1]} 选 thr | "
              f"验证段 {test_start}~{test_idle[-1]}")
        print(f"    验证段纯核心 = {eq_core_test:+.2f}% / {len(core_test)}笔\n")
        modes = ["14:50", "trix:11:05", "trix:14:50"]
        labels = {"14:50": "纯14:50固定卖",
                  "trix:11:05": "TRIX死叉+11:05兜底",
                  "trix:14:50": "TRIX死叉+14:50兜底"}
        print(f"  {'卖点':<16}{'训练thr':>9}{'训练均笔':>9}{'OOS笔':>6}"
              f"{'OOS增量':>11}{'验证段合并':>13}")
        print("  " + "-" * 64)
        cmp_rows = []
        for mode in modes:
            # 训练段独立优选 thr
            best_thr, best_score, best_n = None, -1e9, 0
            for thr in THR_LIST:
                rets = []
                for day in train_idle:
                    pk = pick_momentum(etf_list, etf_5min, prev_close, day, thr)
                    if not pk:
                        continue
                    code, gain = pk
                    bp = price_at_time(etf_5min.get(code, {}).get(day, []), MOM_BUY)
                    if not bp or bp <= 0:
                        continue
                    nd = next_trading_day(all_dates, day)
                    if not nd:
                        continue
                    out = sell_by_mode(mode, etf_5min.get(code, {}).get(nd, []), bp, args.fee)
                    if out:
                        rets.append(out[0])
                if len(rets) >= 20:
                    sc = sum(rets) / len(rets)
                    if sc > best_score:
                        best_score, best_thr, best_n = sc, thr, len(rets)
            # 验证段用选中 thr
            mom_test = []
            for day in test_idle:
                pk = pick_momentum(etf_list, etf_5min, prev_close, day, best_thr)
                if not pk:
                    continue
                code, gain = pk
                bp = price_at_time(etf_5min.get(code, {}).get(day, []), MOM_BUY)
                if not bp or bp <= 0:
                    continue
                nd = next_trading_day(all_dates, day)
                if not nd:
                    continue
                out = sell_by_mode(mode, etf_5min.get(code, {}).get(nd, []), bp, args.fee)
                if out:
                    mom_test.append({"return_pct": out[0], "signal_date": day})
            eq_merged = merge_equity(core_test + mom_test)
            oos = eq_merged - eq_core_test
            cmp_rows.append((mode, best_thr, best_score, len(mom_test), oos, eq_merged))
            print(f"  {labels[mode]:<16}{best_thr:>+8.1f}%{best_score:>+8.3f}%"
                  f"{len(mom_test):>6}{oos:>+10.2f}%{eq_merged:>+12.2f}%")
        cmp_result = {
            "window": f"{eval_dates[0]}~{eval_dates[-1]}",
            "test_start": test_start,
            "test_core_equity_pct": round(eq_core_test, 2),
            "test_core_trades": len(core_test),
            "modes": {labels[m]: {
                "best_thr": c[1], "train_avg": round(c[2], 4),
                "oos_trades": c[3], "oos_increment": round(c[4], 2),
                "test_merged_equity_pct": round(c[5], 2),
            } for m, c in zip(modes, cmp_rows)},
        }
        OUT_CMP = OUT.with_name("idle_momentum_cmp.json")
        OUT_CMP.write_text(json.dumps(cmp_result, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        print(f"\n对比结论已落盘: {OUT_CMP}")
        return

    # ── 全跑 idle 动量腿(不与核心混合), 评估 idle 策略独立收益 ──
    if args.mode == "idle_only":
        print(f"\n>>> 全跑 idle 动量腿(独立, 不与核心混合)")
        print(f"    规则: idle日14:50买当日最强≥thr → 次日{args.momentum_sell}卖")
        print(f"    (注: idle日=核心14:45未触发, 共{len(idle_days)}天)\n")
        print(f"  {'thr':>6}{'笔数':>6}{'独立累计':>12}{'年均笔':>8}"
              f"{'胜率':>7}{'最大回撤':>10}")
        print("  " + "-" * 49)
        for thr in THR_LIST:
            trades = []
            for day in idle_days:
                pk = pick_momentum(etf_list, etf_5min, prev_close, day, thr)
                if not pk:
                    continue
                code, gain = pk
                day_bars = etf_5min.get(code, {}).get(day, [])
                bp = price_at_time(day_bars, MOM_BUY)
                if not bp or bp <= 0:
                    continue
                nd = next_trading_day(all_dates, day)
                if not nd:
                    continue
                out = sell_by_mode(args.momentum_sell,
                                   etf_5min.get(code, {}).get(nd, []), bp, args.fee)
                if not out:
                    continue
                trades.append({"signal_date": day, "etf": code,
                               "return_pct": out[0]})
            if not trades:
                continue
            eq = 1.0
            peak = 1.0
            mdd = 0.0
            for t in sorted(trades, key=lambda x: x["signal_date"]):
                eq *= 1 + t["return_pct"] / 100
                peak = max(peak, eq)
                mdd = min(mdd, (eq - peak) / peak * 100)
            win = sum(1 for t in trades if t["return_pct"] > 0) / len(trades) * 100
            yrs = (eval_dates[-1] + "~") and max(1, len({t["signal_date"][:4] for t in trades}))
            print(f"  {thr:>+5.1f}%{len(trades):>6}{eq*100-100:>+11.2f}%"
                  f"{len(trades)/yrs:>8.1f}{win:>6.1f}%{mdd:>+9.1f}%")
        print(f"\n    (对照) 纯核心 hybrid-A 独立 = {core_eq:+.2f}% / {len(core_trades)}笔")
        return

    wf_result = None
    if args.mode in ("wf", "both"):
        split = int(len(idle_days) * 0.6)
        train_idle = idle_days[:split]
        test_idle = idle_days[split:]
        # 训练段选动量参数 (thr × sell模式), 选训练段均笔最高者
        best = None
        best_score = -1e9
        best_n = 0
        for thr in THR_LIST:
            for spec in SELL_SPECS:
                rets = []
                for day in train_idle:
                    pk = pick_momentum(etf_list, etf_5min, prev_close, day, thr)
                    if not pk:
                        continue
                    code, gain = pk
                    bp = price_at_time(etf_5min.get(code, {}).get(day, []), MOM_BUY)
                    if not bp or bp <= 0:
                        continue
                    nd = next_trading_day(all_dates, day)
                    if not nd:
                        continue
                    out = do_sell(spec, etf_5min.get(code, {}).get(nd, []), bp, args.fee)
                    if out:
                        rets.append(out[0])
                if len(rets) >= 20:
                    sc = sum(rets) / len(rets)
                    if sc > best_score:
                        best_score = sc
                        best = (thr, spec)
                        best_n = len(rets)
        if best:
            bthr, bspec = best
            btag = bspec[1] if bspec[0] == "time" else bspec[0]
            print(f"\n>>> Walk-Forward 隔离验证")
            print(f"    训练段({train_idle[0]}~{train_idle[-1]}) 选出动量参数: "
                  f"涨≥{bthr:.1f}% / 卖{btag} (训练均笔{best_score:+.3f}% / {best_n}笔)")
            # 验证段应用
            mom_test = []
            for day in test_idle:
                pk = pick_momentum(etf_list, etf_5min, prev_close, day, bthr)
                if not pk:
                    continue
                code, gain = pk
                bp = price_at_time(etf_5min.get(code, {}).get(day, []), MOM_BUY)
                if not bp or bp <= 0:
                    continue
                nd = next_trading_day(all_dates, day)
                if not nd:
                    continue
                out = do_sell(bspec, etf_5min.get(code, {}).get(nd, []), bp, args.fee)
                if out:
                    mom_test.append({
                        "signal_date": day, "sell_date": nd, "etf": code,
                        "today_gain": round(gain, 2), "return_pct": round(out[0], 4),
                        "sell_reason": "momentum_wf_" + out[1],
                    })
            test_start = test_idle[0]
            core_test = [t for t in core_trades if t["signal_date"] >= test_start]
            eq_core_test = merge_equity(core_test)
            eq_merged_test = merge_equity(core_test + mom_test)
            print(f"    验证段({test_start}~): 纯核心 {eq_core_test:+.2f}% / {len(core_test)}笔")
            print(f"    验证段合并:    {eq_merged_test:+.2f}% / {len(core_test)+len(mom_test)}笔")
            print(f"    ★ 动量 OOS 增量(WF选中卖点{btag}) = {eq_merged_test - eq_core_test:+.2f}% "
                  f"({len(mom_test)}笔, 这才是实盘等价增强)")
            # 额外: 用 WF 选中阈值 + TRIX卖(与核心同规则, 稳健) 的 OOS 增量, 做对照
            mom_test_trix = []
            for day in test_idle:
                pk = pick_momentum(etf_list, etf_5min, prev_close, day, bthr)
                if not pk:
                    continue
                code, gain = pk
                bp = price_at_time(etf_5min.get(code, {}).get(day, []), MOM_BUY)
                if not bp or bp <= 0:
                    continue
                nd = next_trading_day(all_dates, day)
                if not nd:
                    continue
                out = do_sell(("trix", None), etf_5min.get(code, {}).get(nd, []), bp, args.fee)
                if out:
                    mom_test_trix.append({
                        "signal_date": day, "sell_date": nd, "etf": code,
                        "today_gain": round(gain, 2), "return_pct": round(out[0], 4),
                        "sell_reason": "momentum_wf_trix_" + out[1],
                    })
            eq_merged_trix = merge_equity(core_test + mom_test_trix)
            print(f"    ◆ 对照(同阈值+TRIX卖) OOS 增量 = {eq_merged_trix - eq_core_test:+.2f}% "
                  f"({len(mom_test_trix)}笔)")
            wf_result = {
                "best_thr": bthr, "best_sell": btag,
                "train_avg": round(best_score, 4), "train_n": best_n,
                "test_core_equity_pct": round(eq_core_test, 2),
                "test_merged_equity_pct": round(eq_merged_test, 2),
                "test_momentum_increment": round(eq_merged_test - eq_core_test, 2),
                "test_momentum_trades": len(mom_test),
            }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "window": f"{eval_dates[0]}~{eval_dates[-1]}",
        "momentum": {"thr": args.momentum_thr, "sell": args.momentum_sell,
                     "trades": len(momentum_trades)},
        "core_equity_pct": round(core_eq, 2),
        "momentum_equity_pct": round(mom_eq, 2),
        "merged_equity_pct": round(merged_eq, 2),
        "merged_trades": n,
        "merged_win_rate": round(win, 1),
        "merged_max_drawdown": round(mdd, 2),
        "per_year": {y: {"core": py_c.get(y, {}), "momentum": py_m.get(y, {}),
                         "merged": py_all[y]} for y in sorted(py_all)},
        "walk_forward": wf_result,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结论已落盘: {OUT}")


if __name__ == "__main__":
    main()

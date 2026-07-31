#!/usr/bin/env python3
"""闲置日隔夜反向腿（H1）walk-forward 搜索 —— 用与段1/段2 完全相同的抗过拟合框架。

核心问题：
    交易日的 11:05-14:45 盘中块已被证伪（同日翻仓 = 动量反转枪口，全负）。
    但「闲置资金」还有一扇没开的门：
        idle 日（核心 14:45 信号未触发 → 资金隔夜闲置）若做「隔夜反向腿」：
            14:30 买当日最超卖(跌幅最大)的 T0 ETF → 次日 09:40 卖
        这既不是同日追涨，也不与核心抢资金（核心日不做此腿），结构上与已验证的
        隔夜形态(核心本就是隔夜)一致，是真正可能翻盘的方向。

本脚本：
    - eligible 日 = 核心 14:45(≥3%) 未发出信号的日子（即资金隔夜闲置日）
    - 选股 = 14:30 时刻「当日涨幅最低(最超卖)」的 T0 ETF，要求跌幅 ≥ 阈值
    - 持有 = 隔夜；次晨 sell 时刻卖出
    - walk-forward 60/40 + 邻域稳健 + 多重检验 + 扣双边费(万3)
    - 额外维度：regime 过滤（off / 仅趋势上行日做）

判定主指标 t 值（自动惩罚低样本/高波动）。基准 = 现金 0%（闲置资金本就是 0%）。

用法：
    python scripts/search_idle_overnight_reversal.py
    python scripts/search_idle_overnight_reversal.py --start 2022-06-15 --split 0.6
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

from backtest_t0_etf import apply_net_return, price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    next_trading_day,
    regime_on_date,
    resolve_eval_dates,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
OUT_FILE = Path.home() / ".tradingagents/cache/t0_5min/idle_overnight_reversal.json"

BUY_TIME = "14:30"          # idle 日尾盘买入超卖标的
MIN_TRADES = 25             # 训练段最少笔数

# ── 搜索空间 ──
# 反转腿：候选需「当日涨幅 ≤ thr」（thr 越负 = 要求跌得越深才买）
THR_REVERSAL = [-0.5, -1.0, -1.5, -2.0, -3.0]
# 控制腿(动量)：候选需「当日涨幅 ≥ thr」（买当日最强，隔夜持有）
THR_MOMENTUM = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]
# 次日卖出时刻（加密：补密早盘，覆盖 09:40~14:50）
SELL_TIMES = ["09:40", "09:50", "10:00", "10:30", "11:00", "11:30",
              "13:30", "14:00", "14:50"]
# regime 过滤维度
REGIME_FILTERS = ["off", "trend_up"]


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


# ─────────────────── 反向选股（14:30 最超卖） ───────────────────

def pick_reversal(
    etf_list: list[dict],
    etf_5min: dict,
    prev_close: dict[str, dict[str, float]],
    day: str,
    threshold: float,
) -> tuple[str, float] | None:
    """取 14:30 时刻当日涨幅最低(最超卖)的 T0 ETF；若最超卖者涨幅 > threshold 则不交易。"""
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
    cands.sort(key=lambda x: x[0])  # 最超卖 = 涨幅最小(最负)排前
    best_gain, best_code = cands[0]
    if best_gain > threshold:
        return None  # 不够超卖
    return best_code, best_gain


def pick_momentum(
    etf_list: list[dict],
    etf_5min: dict,
    prev_close: dict[str, dict[str, float]],
    day: str,
    threshold: float,
) -> tuple[str, float] | None:
    """控制腿：取 14:30 时刻「当日涨幅最高(Top1 最强)」的 T0 ETF；要求涨幅 ≥ threshold。

    若 H1 反转腿超额收益只是「隔夜 beta」，则本腿也应正（甚至更高）——
    那样 H1 逻辑就坐不实。若本腿平/负而反转腿正，则 H1 的反转逻辑成立。
    """
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
    cands.sort(key=lambda x: x[0], reverse=True)  # 最强涨幅排前
    best_gain, best_code = cands[0]
    if best_gain < threshold:
        return None  # 不够强
    return best_code, best_gain


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
        "n": n,
        "avg": round(avg, 4),
        "std": round(std, 4),
        "t": round(t, 3),
        "cum_pct": round((eq - 1) * 100, 2),
        "win_rate": round(sum(1 for r in rets if r > 0) / n * 100, 1),
        "mdd_pct": round(mdd, 2),
    }


# ─────────────────── 主流程 ───────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="闲置日隔夜反向腿(H1) walk-forward 搜索")
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

    print("=== 闲置日隔夜反向腿 (H1) Walk-Forward 搜索 ===")
    print(f"    区间 {eval_dates[0]} ~ {eval_dates[-1]}（{len(eval_dates)} 交易日）")
    print(f"    买 {BUY_TIME} 最超卖(反)/最强(动) | 隔夜 | 次晨卖 | "
          f"反阈{THR_REVERSAL} 动阈{THR_MOMENTUM} | regime{REGIME_FILTERS}")

    # ① eligible：核心 14:45(≥3%) 未触发 = 资金隔夜闲置日
    print("\n>>> [1/5] 计算核心信号与 idle 日 ...")
    base_pick: dict[str, bool] = {}
    for day in eval_dates:
        reg = regime_on_date(proxy, day)
        if reg and reg.get("skip_choppy"):
            base_pick[day] = True  # choppy → 核心不触发 → 也算闲置
            continue
        # 当日 14:45 涨幅≥3% Top1 是否存在
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

    # ② 预算每个 (direction, threshold, regime_filter) 的选股
    #    reversal = 反转腿（买最超卖）；momentum = 控制腿（买最强涨幅）
    TASKS = [
        ("reversal", THR_REVERSAL, pick_reversal),
        ("momentum", THR_MOMENTUM, pick_momentum),
    ]
    THR_MAP = {d: tl for d, tl, _ in TASKS}
    PICK_FN = {d: fn for d, _, fn in TASKS}
    print("\n>>> [2/5] 预计算各 (方向,阈值,regime) 选股 ...")
    picks: dict[tuple[str, float, str], dict[str, tuple[str, float] | None]] = {}
    ti_total = 0
    for direction, thr_list, fn in TASKS:
        for thr in thr_list:
            for rf in REGIME_FILTERS:
                key = (direction, thr, rf)
                picks[key] = {}
                for day in idle_days:
                    if rf == "trend_up":
                        reg = regime_on_date(proxy, day)
                        if not (reg and reg.get("regime") == "up"):
                            picks[key][day] = None
                            continue
                    picks[key][day] = fn(
                        etf_list, etf_5min, prev_close, day, thr
                    )
                ti_total += 1
    print(f"    预计算 {ti_total} 个 (方向×阈值×regime) 选股表")

    # ③ 网格模拟（隔夜：买当日 14:30，卖次晨 sell_time）
    sim_cache: dict[tuple, tuple[float, str] | None] = {}

    def simulate(code: str, day: str, sell_time: str):
        key = (code, day, sell_time)
        if key in sim_cache:
            return sim_cache[key]
        nday = next_trading_day(all_dates, day)
        if not nday:
            sim_cache[key] = None
            return None
        bars_b = etf_5min.get(code, {}).get(day, [])
        bars_s = etf_5min.get(code, {}).get(nday, [])
        bp = price_at_time(bars_b, BUY_TIME)
        sp = price_at_time(bars_s, sell_time)
        out = None
        if bp and bp > 0 and sp and sp > 0:
            ret = apply_net_return(bp, sp, args.fee)
            out = (ret, "overnight")
        sim_cache[key] = out
        return out

    combos = [(d, thr, st, rf) for d, tl, _ in TASKS
              for thr in tl for st in SELL_TIMES for rf in REGIME_FILTERS]

    print(f"\n>>> [3/5] 网格模拟 {len(combos)} 个组合 ...")
    results: list[dict] = []
    for ci, (direction, thr, st, rf) in enumerate(combos, 1):
        tr_rets: list[float] = []
        te_rets: list[float] = []
        by_day: dict[str, float] = {}
        for day, pk in picks[(direction, thr, rf)].items():
            if not pk:
                continue
            code = pk[0]
            out = simulate(code, day, st)
            if not out:
                continue
            ret, _ = out
            by_day[day] = ret
            (tr_rets if day in train_days else te_rets).append(ret)
        if len(tr_rets) < MIN_TRADES:
            continue
        dname = "反转" if direction == "reversal" else "动量"
        if direction == "reversal":
            slabel = f"反:跌≤{thr:.1f}%→{BUY_TIME}买→次晨{st}卖[{rf}]"
        else:
            slabel = f"动:涨≥{thr:.1f}%→{BUY_TIME}买→次晨{st}卖[{rf}]"
        results.append({
            "direction": direction,
            "threshold": thr, "sell_time": st, "regime_filter": rf,
            "label": slabel,
            "train": stats_of(tr_rets),
            "test": stats_of(te_rets),
            "full": stats_of(tr_rets + te_rets),
            "by_day": by_day,
        })
        if ci % 50 == 0:
            print(f"    {ci}/{len(combos)}")

    if not results:
        print("无满足最小笔数的组合")
        sys.exit(1)

    n_eff = len(results)
    t_noise = math.sqrt(2 * math.log(n_eff))

    results.sort(key=lambda r: r["train"]["t"], reverse=True)

    print("\n" + "=" * 116)
    print(f"  [4/5] 训练段 TOP 14（按 t 值） | N={n_eff} | 噪声门槛 t_noise={t_noise:.2f} | 基准=现金0%")
    print("=" * 116)
    head = (f"  {'#':>2} {'组合':<40} {'IS笔':>4} {'IS均笔':>7} {'IS_t':>6} "
            f"{'IS累计':>9} | {'OOS笔':>5} {'OOS均笔':>8} {'OOS_t':>6} {'OOS累计':>9} {'过噪声':>6}")
    print(head)
    print("  " + "-" * 112)
    for i, r in enumerate(results[:14], 1):
        tr, te = r["train"], r["test"]
        flag = "✓" if tr["t"] > t_noise else "✗"
        print(
            f"  {i:>2} {r['label']:<40} {tr['n']:>4} {tr['avg']:>+7.3f} {tr['t']:>6.2f} "
            f"{tr['cum_pct']:>+8.1f}% | {te.get('n', 0):>5} {te.get('avg', 0):>+8.3f} "
            f"{te.get('t', 0):>6.2f} {te.get('cum_pct', 0):>+8.1f}% {flag:>6}"
        )
    print("=" * 116)

    # ⑤ 稳健性判定
    print("\n>>> [5/5] 稳健性判定 ...")
    # 邻域 = 同方向/sell_time/regime 下相邻 threshold 训练均笔为正的比例
    by_key = {(r["direction"], r["threshold"], r["sell_time"], r["regime_filter"]): r
              for r in results}

    def neighbor_score(r: dict) -> tuple[float, int, int]:
        tl = THR_MAP[r["direction"]]
        ti = tl.index(r["threshold"])
        st = r["sell_time"]
        rf = r["regime_filter"]
        d = r["direction"]
        neigh = []
        for dt in (-1, 1):
            t2 = ti + dt
            if not (0 <= t2 < len(tl)):
                continue
            nb = by_key.get((d, tl[t2], st, rf))
            if nb:
                neigh.append(nb)
        if not neigh:
            return 0.0, 0, 0
        pos = sum(1 for nb in neigh if nb["train"]["avg"] > 0)
        return pos / len(neigh), pos, len(neigh)

    survivors = []
    for r in results:
        tr, te = r["train"], r["test"]
        ratio, pos, tot = neighbor_score(r)
        checks = {
            "过多重检验门槛": tr["t"] > t_noise,
            "样本外均笔为正": te.get("n", 0) >= 10 and te.get("avg", 0) > 0,
            "样本外t≥1": te.get("t", 0) >= 1.0,
            "邻域≥75%为正": ratio >= 0.75,
            "训练均笔>0.1%": tr["avg"] > 0.1,
        }
        r["checks"] = checks
        r["neighbor"] = f"{pos}/{tot}"
        r["passed"] = sum(checks.values())
        if all(checks.values()):
            survivors.append(r)

    print(f"\n  五重检验全通过的组合: {len(survivors)} / {n_eff}")
    if survivors:
        survivors.sort(key=lambda r: r["full"]["cum_pct"], reverse=True)
        print(f"\n  {'组合':<40} {'全样本笔':>7} {'均笔':>7} {'t':>6} {'累计':>10} {'胜率':>6} {'邻域':>6}")
        print("  " + "-" * 90)
        for r in survivors[:10]:
            f = r["full"]
            print(f"  {r['label']:<40} {f['n']:>7} {f['avg']:>+7.3f} {f['t']:>6.2f} "
                  f"{f['cum_pct']:>+9.1f}% {f['win_rate']:>5.1f}% {r['neighbor']:>6}")
    else:
        print("  ⚠ 没有任何组合能同时通过五重检验（隔夜反向腿也走不通）")
        near = sorted(results, key=lambda r: (-r["passed"], -r["train"]["t"]))[:5]
        for r in near:
            failed = [k for k, v in r["checks"].items() if not v]
            print(f"    {r['label']:<40} 通过 {r['passed']}/5，未过: {', '.join(failed)}")

    # ── 卖出时点轮廓：直接回答「14:50 会不会太晚」──
    # 把 reversal / momentum 各自拆成 早盘(≤11:00) / 午后(≥13:30)，
    # 取各组 OOS 均笔最优的组合，看「早盘落袋」是否也能吃到盈利。
    EARLY = {t for t in SELL_TIMES if t <= "11:00"}
    LATE = {t for t in SELL_TIMES if t >= "13:30"}

    def best_oos(group_results: list[dict], pool: set[str]):
        cands = [r for r in group_results
                 if r["sell_time"] in pool and r["test"].get("n", 0) >= 10]
        if not cands:
            return None
        return max(cands, key=lambda r: r["test"]["avg"])

    print("\n  ── 卖出时点轮廓（直接回答「14:50 太晚？」）──")
    print(f"    {'方向':<6}{'时段':<8}{'最佳卖出':<8}{'OOS笔':>5}{'OOS均笔':>9}"
          f"{'OOS累计':>9}{'IS均笔':>9}")
    print("    " + "-" * 60)
    for direction, dname in (("reversal", "反转"), ("momentum", "动量(对照)")):
        grp = [r for r in results if r["direction"] == direction]
        for tag, pool in (("早盘≤11:00", EARLY), ("午后≥13:30", LATE)):
            b = best_oos(grp, pool)
            if not b:
                print(f"    {dname:<6}{tag:<8}{'—':<8}{0:>5}{'—':>9}{'—':>9}{'—':>9}")
                continue
            te = b["test"]
            print(f"    {dname:<6}{tag:<8}{b['sell_time']:<8}{te['n']:>5}"
                  f"{te['avg']:>+8.3f}%{te['cum_pct']:>+8.1f}%{b['train']['avg']:>+8.3f}%")

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
        "survivors": [{k: v for k, v in r.items() if k != "by_day"} for r in survivors[:20]],
        "train_top": [{k: v for k, v in r.items() if k != "by_day"} for r in results[:20]],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {OUT_FILE}")


if __name__ == "__main__":
    main()

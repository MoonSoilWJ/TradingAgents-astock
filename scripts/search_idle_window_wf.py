#!/usr/bin/env python3
"""闲置窗口（11:05~14:45）最优时点搜索 —— Walk-Forward + 抗过拟合。

为什么不用 backtest_t0_idle_window.py --grid:
    那是在同一份 100 天数据上跑 2000+ 组合挑第一名 = 教科书式过拟合。
    纯随机序列在 2000 次抽样里也能挑出惊人的"最优"。

本脚本的判定流程（每一步都在淘汰假信号）:
    1) 物理先验   TRIX(5,3) 需足够 bar 才可能触发死叉；买卖间隔太短的配置
                  结构上退化为定时卖 —— 直接剔除，不浪费搜索预算。
    2) 长样本     用 aligned_live_4y.json（2022-06-15~2026-07-28，1000 天，
                  5min/日K/proxy 数据完整），而非 100 天。
    3) Walk-Fwd   前 60% 训练选参 → 后 40% 样本外验证，看 IS→OOS 衰减。
    4) 邻域稳健   最优点相邻档（signal±1 / sell±1）也须正期望，否则是噪声尖峰。
    5) 多重检验   N 个组合下"最优"的运气门槛 t_noise ≈ sqrt(2·ln N)。
                  训练段 t 值打不过它 = 无法与噪声区分。
    6) 交易成本   全程 apply_net_return 扣双边费。

用法:
    python scripts/search_idle_window_wf.py
    python scripts/search_idle_window_wf.py --start 2022-06-15 --split 0.6
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_etf import price_at_time  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    regime_on_date,
    resolve_eval_dates,
    time_to_min,
)
from backtest_t0_idle_window import (  # noqa: E402
    IDLE_END,
    IDLE_START,
    LIVE_BUY,
    LIVE_SELL_CUTOFF,
    LIVE_SIGNAL,
    prev_trading_day,
    sell_time_mode,
    sell_trail_mode,
    sell_trix_mode,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
OUT_FILE = Path.home() / ".tradingagents/cache/t0_5min/idle_window_wf.json"

# A股 5 分钟 bar 时刻表
BAR_TIMES = (
    [f"{9:02d}:{m:02d}" for m in (35, 40, 45, 50, 55)]
    + [f"10:{m:02d}" for m in range(0, 60, 5)]
    + [f"11:{m:02d}" for m in range(0, 35, 5)]
    + [f"13:{m:02d}" for m in range(5, 60, 5)]
    + [f"14:{m:02d}" for m in range(0, 60, 5)]
    + ["15:00"]
)
BAR_IDX = {t: i for i, t in enumerate(BAR_TIMES)}

# 搜索空间（信号点：闲置窗口内的整点/准整点；买入 = 信号后一根 bar）
GRID_SIGNALS = [
    "11:05", "11:15", "11:25",
    "13:05", "13:20", "13:35", "13:50",
    "14:05", "14:20",
]
GRID_SELLS = ["11:30", "13:30", "14:00", "14:15", "14:30", "14:40", "14:45"]
GRID_MIN_GAINS = [0.0, 1.0, 2.0, 3.0, 4.0]  # 0.0 = 纯 Top1 无门槛；3.0 = 实盘 filter
GRID_MODES = ["time", "trail", "trix"]

MIN_TRADES = 25          # 训练段最少笔数
TRIX_MIN_DECISIONS = 4   # trix 模式：买入后至少 4 根 bar 才有判死叉的余地


# ────────────────────────── 快速排名（语义等价 rank_by_today_gain） ──────────

class FastRanker:
    """预建索引的当日涨幅排名器。

    与 backtest_t0_today1.rank_by_today_gain 语义完全一致：
        gain = (signal_time 前最后一根 bar 的 close - 昨收) / 昨收
        5min 缺失时回退昨收（gain=0），不使用当日收盘（避免未来函数）。
    """

    def __init__(self, etf_list: list[dict], etf_daily: dict, etf_5min: dict) -> None:
        self.etf_list = etf_list
        self.prev_close: dict[str, dict[str, float]] = {}
        self.bar_tmins: dict[str, dict[str, list[int]]] = defaultdict(dict)
        self.bar_closes: dict[str, dict[str, list[float]]] = defaultdict(dict)

        for etf in etf_list:
            code = etf["code"]
            info = etf_daily.get(code)
            if not info:
                continue
            returns = info.get("returns", [])
            pc: dict[str, float] = {}
            for i in range(1, len(returns)):
                prev = returns[i - 1].get("close")
                if prev and prev > 0:
                    pc[returns[i]["date"]] = float(prev)
            self.prev_close[code] = pc

            for day, bars in etf_5min.get(code, {}).items():
                tm: list[int] = []
                cl: list[float] = []
                for b in bars:
                    parts = str(b.get("time", "")).split(":")
                    if len(parts) < 2:
                        continue
                    tm.append(int(parts[0]) * 60 + int(parts[1]))
                    cl.append(float(b["close"]))
                if tm:
                    self.bar_tmins[code][day] = tm
                    self.bar_closes[code][day] = cl

    def price_at(self, code: str, day: str, tmin: int) -> float | None:
        tms = self.bar_tmins.get(code, {}).get(day)
        if not tms:
            return None
        i = bisect_left(tms, tmin) - 1  # 最后一根 time < target 的 bar
        if i < 0:
            return None
        return self.bar_closes[code][day][i]

    def rank(self, day: str, signal_time: str) -> list[tuple[float, dict]]:
        tmin = time_to_min(signal_time)
        scores: list[tuple[float, dict]] = []
        for etf in self.etf_list:
            code = etf["code"]
            prev = self.prev_close.get(code, {}).get(day)
            if not prev or prev <= 0:
                continue
            px = self.price_at(code, day, tmin)
            if px is None or px <= 0:
                px = prev  # 与原实现一致：缺 5min 用昨收
            scores.append(((px - prev) / prev * 100, etf))
        scores.sort(key=lambda x: x[0], reverse=True)
        return scores


def derive_pick(
    scores: list[tuple[float, dict]], min_gain: float
) -> tuple[str, float, str] | None:
    """min_gain=0 → 纯 Top1；否则取第一个 gain >= min_gain。"""
    if len(scores) < 2:
        return None
    for gain, etf in scores:
        if gain >= min_gain:
            return etf["code"], gain, etf["name"]
        if min_gain <= 0:
            break
    return None


# ────────────────────────────── 组合校验 ──────────────────────────────

def buy_after(signal: str) -> str | None:
    """买入 = 信号后一根 bar（与实盘 14:45 信号→14:50 买同构）。"""
    i = BAR_IDX.get(signal)
    if i is None or i + 1 >= len(BAR_TIMES):
        return None
    return BAR_TIMES[i + 1]


def in_idle(t: str) -> bool:
    return time_to_min(IDLE_START) <= time_to_min(t) <= time_to_min(IDLE_END)


def bars_between(buy: str, sell: str) -> int:
    bi, si = BAR_IDX.get(buy), BAR_IDX.get(sell)
    if bi is None or si is None:
        return -1
    return si - bi


def valid_combo(signal: str, buy: str, sell: str, mode: str) -> tuple[bool, str]:
    if not (in_idle(signal) and in_idle(buy) and in_idle(sell)):
        return False, "越界"
    if time_to_min(buy) <= time_to_min(signal):
        return False, "买不晚于信号"
    if time_to_min(sell) <= time_to_min(buy):
        return False, "卖不晚于买"
    gap = bars_between(buy, sell)
    if mode == "trix" and gap < TRIX_MIN_DECISIONS:
        # 物理先验：买入后 bar 太少，TRIX 无判定空间 → 必然退化为定时卖
        return False, f"trix判定空间不足({gap}根)"
    return True, ""


# ────────────────────────────── 统计 ──────────────────────────────

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
    peak = 1.0
    cur = 1.0
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


# ────────────────────────────── 主流程 ──────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="闲置窗口 walk-forward 时点搜索")
    ap.add_argument("--cache", type=str, default=str(CACHE_FILE))
    ap.add_argument("--start", type=str, default="2022-06-15", help="数据完整段起点")
    ap.add_argument("--end", type=str, default="")
    ap.add_argument("--split", type=float, default=0.6, help="训练段占比")
    ap.add_argument("--fee", type=float, default=FEE_PCT)
    args = ap.parse_args()

    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    all_dates = cache["all_dates"]
    proxy = cache.get("proxy_klines", [])
    eval_dates = resolve_eval_dates(all_dates, 0, args.start, args.end)
    etf_list = get_all_t0_etfs()

    print("=== 闲置窗口 Walk-Forward 搜索 ===")
    print(f"    区间 {eval_dates[0]} ~ {eval_dates[-1]}（{len(eval_dates)} 交易日）")
    print(f"    窗口 {IDLE_START} ~ {IDLE_END}（{LIVE_BUY} 实盘买之前必须清仓）")

    ranker = FastRanker(etf_list, etf_daily, etf_5min)

    # ① eligible 日：前一日 14:45 有基线信号（= 当日 11:05 已平仓、资金闲置）
    print("\n>>> [1/5] 计算基线信号与 eligible 日 ...")
    base_pick: dict[str, tuple[str, float, str] | None] = {}
    for day in eval_dates:
        reg = regime_on_date(proxy, day)
        if reg and reg.get("skip_choppy"):
            base_pick[day] = None
            continue
        base_pick[day] = derive_pick(ranker.rank(day, LIVE_SIGNAL), 3.0)

    idle_days = [
        d for d in eval_dates
        if (p := prev_trading_day(all_dates, d)) and base_pick.get(p)
    ]
    print(f"    eligible: {len(idle_days)} 天 / {len(eval_dates)}")

    split_at = int(len(idle_days) * args.split)
    train_days = set(idle_days[:split_at])
    test_days = set(idle_days[split_at:])
    print(f"    训练 {len(train_days)} 天（{idle_days[0]}~{idle_days[split_at-1]}）")
    print(f"    验证 {len(test_days)} 天（{idle_days[split_at]}~{idle_days[-1]}）")

    # ② 预算 picks
    print("\n>>> [2/5] 预计算各信号时点选股 ...")
    picks: dict[tuple[str, float], dict[str, tuple[str, float, str]]] = {
        (s, g): {} for s in GRID_SIGNALS for g in GRID_MIN_GAINS
    }
    for si, signal in enumerate(GRID_SIGNALS, 1):
        for day in idle_days:
            scores = ranker.rank(day, signal)
            for mg in GRID_MIN_GAINS:
                p = derive_pick(scores, mg)
                if p:
                    picks[(signal, mg)][day] = p
        print(f"    {si}/{len(GRID_SIGNALS)} {signal} 完成")

    # ③ 网格模拟（卖出结果按 code/day/buy/sell/mode 缓存）
    sim_cache: dict[tuple, tuple[float, str] | None] = {}

    def simulate(code: str, day: str, buy: str, sell: str, mode: str):
        key = (code, day, buy, sell, mode)
        if key in sim_cache:
            return sim_cache[key]
        bars = etf_5min.get(code, {}).get(day, [])
        bp = price_at_time(bars, buy)
        out = None
        if bp and bp > 0:
            if mode == "trail":
                out = sell_trail_mode(bars, buy, sell, bp, args.fee)
            elif mode == "trix":
                out = sell_trix_mode(bars, buy, sell, bp, args.fee)
            else:
                out = sell_time_mode(bars, buy, sell, bp, args.fee)
        sim_cache[key] = out
        return out

    combos = []
    for signal in GRID_SIGNALS:
        buy = buy_after(signal)
        if not buy:
            continue
        for sell in GRID_SELLS:
            for mode in GRID_MODES:
                ok, _ = valid_combo(signal, buy, sell, mode)
                if not ok:
                    continue
                for mg in GRID_MIN_GAINS:
                    combos.append((signal, buy, sell, mode, mg))

    print(f"\n>>> [3/5] 网格模拟 {len(combos)} 个组合 ...")
    results: list[dict] = []
    for ci, (signal, buy, sell, mode, mg) in enumerate(combos, 1):
        tr_rets: list[float] = []
        te_rets: list[float] = []
        reasons: dict[str, int] = defaultdict(int)
        by_day: dict[str, float] = {}
        for day, (code, _gain, _name) in picks[(signal, mg)].items():
            out = simulate(code, day, buy, sell, mode)
            if not out:
                continue
            ret, reason = out
            reasons[reason] += 1
            by_day[day] = ret
            (tr_rets if day in train_days else te_rets).append(ret)
        if len(tr_rets) < MIN_TRADES:
            continue
        results.append({
            "signal": signal, "buy": buy, "sell": sell,
            "mode": mode, "min_gain": mg,
            "label": f"{signal}/{buy}→{sell} [{mode}] ≥{mg:.0f}%",
            "train": stats_of(tr_rets),
            "test": stats_of(te_rets),
            "full": stats_of(tr_rets + te_rets),
            "reasons": dict(reasons),
            "by_day": by_day,
        })
        if ci % 50 == 0:
            print(f"    {ci}/{len(combos)}")

    if not results:
        print("无满足最小笔数的组合")
        sys.exit(1)

    n_eff = len(results)
    t_noise = math.sqrt(2 * math.log(n_eff))  # 多重检验：最优者的运气门槛

    # ④ 训练段按 t 值排序（t 自动惩罚低样本/高波动，比累计收益稳健）
    results.sort(key=lambda r: r["train"]["t"], reverse=True)

    print("\n" + "=" * 108)
    print(f"  [4/5] 训练段 TOP 12（按 t 值） | 有效组合 N={n_eff} | 噪声门槛 t_noise={t_noise:.2f}")
    print("=" * 108)
    head = (f"  {'#':>2} {'组合':<34} {'IS笔':>4} {'IS均笔':>7} {'IS_t':>6} "
            f"{'IS累计':>9} | {'OOS笔':>5} {'OOS均笔':>8} {'OOS_t':>6} {'OOS累计':>9} {'过噪声':>6}")
    print(head)
    print("  " + "-" * 104)
    for i, r in enumerate(results[:12], 1):
        tr, te = r["train"], r["test"]
        flag = "✓" if tr["t"] > t_noise else "✗"
        print(
            f"  {i:>2} {r['label']:<34} {tr['n']:>4} {tr['avg']:>+7.3f} {tr['t']:>6.2f} "
            f"{tr['cum_pct']:>+8.1f}% | {te.get('n', 0):>5} {te.get('avg', 0):>+8.3f} "
            f"{te.get('t', 0):>6.2f} {te.get('cum_pct', 0):>+8.1f}% {flag:>6}"
        )
    print("=" * 108)

    # ⑤ 稳健性判定
    print("\n>>> [5/5] 稳健性判定 ...")
    by_key = {(r["signal"], r["sell"], r["mode"], r["min_gain"]): r for r in results}

    def neighbor_score(r: dict) -> tuple[float, int, int]:
        """相邻档（signal±1 / sell±1）中训练段均笔为正的比例。"""
        si = GRID_SIGNALS.index(r["signal"])
        se = GRID_SELLS.index(r["sell"])
        neigh = []
        for ds, dl in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            s2, e2 = si + ds, se + dl
            if not (0 <= s2 < len(GRID_SIGNALS) and 0 <= e2 < len(GRID_SELLS)):
                continue
            nb = by_key.get((GRID_SIGNALS[s2], GRID_SELLS[e2], r["mode"], r["min_gain"]))
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
        print(f"\n  {'组合':<34} {'全样本笔':>7} {'均笔':>7} {'t':>6} {'累计':>10} {'胜率':>6} {'邻域':>6}")
        print("  " + "-" * 84)
        for r in survivors[:10]:
            f = r["full"]
            print(f"  {r['label']:<34} {f['n']:>7} {f['avg']:>+7.3f} {f['t']:>6.2f} "
                  f"{f['cum_pct']:>+9.1f}% {f['win_rate']:>5.1f}% {r['neighbor']:>6}")
    else:
        print("  ⚠ 没有任何组合能同时通过五重检验")
        print("\n  最接近的 5 个（按通过项数）:")
        near = sorted(results, key=lambda r: (-r["passed"], -r["train"]["t"]))[:5]
        for r in near:
            failed = [k for k, v in r["checks"].items() if not v]
            print(f"    {r['label']:<34} 通过 {r['passed']}/5，未过: {', '.join(failed)}")

    # 全组合分布：判断是"窗口选错"还是"整个闲置窗口无 alpha"
    is_avgs = sorted(r["train"]["avg"] for r in results)
    oos_avgs = sorted(r["test"].get("avg", 0) for r in results if r["test"].get("n", 0) >= 10)

    def pctl(xs: list[float], q: float) -> float:
        if not xs:
            return 0.0
        return xs[min(len(xs) - 1, int(len(xs) * q))]

    print("\n  ── 全 %d 组合的均笔收益分布（%%/笔，已扣双边费）──" % n_eff)
    print(f"    {'':<8}{'p10':>9}{'p25':>9}{'中位':>9}{'p75':>9}{'p90':>9}{'正比例':>9}")
    for tag, xs in (("训练段", is_avgs), ("样本外", oos_avgs)):
        pos = sum(1 for x in xs if x > 0) / len(xs) * 100 if xs else 0
        print(f"    {tag:<8}{pctl(xs,0.1):>+9.3f}{pctl(xs,0.25):>+9.3f}{pctl(xs,0.5):>+9.3f}"
              f"{pctl(xs,0.75):>+9.3f}{pctl(xs,0.9):>+9.3f}{pos:>8.1f}%")

    # 现役段2配置（信号与买入分离：11:05 选 → 14:05 买 → 14:15 trix 卖）
    print("\n  ── 现役段2 配置（11:05 选 → 14:05 买 → 14:15 trix 卖）精确评估 ──")
    cur_tr: list[float] = []
    cur_te: list[float] = []
    cur_reasons: dict[str, int] = defaultdict(int)
    for day, (code, _g, _n) in picks[("11:05", 3.0)].items():
        out = simulate(code, day, "14:05", "14:15", "trix")
        if not out:
            continue
        cur_reasons[out[1]] += 1
        (cur_tr if day in train_days else cur_te).append(out[0])
    cs_tr, cs_te = stats_of(cur_tr), stats_of(cur_tr + cur_te)
    print(f"    训练段: {cs_tr.get('n',0)} 笔 均笔 {cs_tr.get('avg',0):+.3f}% t={cs_tr.get('t',0):.2f}")
    print(f"    全样本: {cs_te.get('n',0)} 笔 均笔 {cs_te.get('avg',0):+.3f}% "
          f"t={cs_te.get('t',0):.2f} 累计 {cs_te.get('cum_pct',0):+.1f}% 胜率 {cs_te.get('win_rate',0):.1f}%")
    print(f"    卖出原因分布: {dict(cur_reasons)}")
    ok, why = valid_combo("11:05", "14:05", "14:15", "trix")
    if not ok:
        print(f"    ⚠ 物理先验判定不合格: {why} → TRIX 结构上退化为定时卖")

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "window": [args.start, args.end or eval_dates[-1]],
        "eligible_days": len(idle_days),
        "split": args.split,
        "n_combos": n_eff,
        "t_noise": round(t_noise, 3),
        "survivors": [{k: v for k, v in r.items() if k != "by_day"} for r in survivors[:20]],
        "train_top": [{k: v for k, v in r.items() if k != "by_day"} for r in results[:20]],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {OUT_FILE}")


if __name__ == "__main__":
    main()

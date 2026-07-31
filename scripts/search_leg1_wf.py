#!/usr/bin/env python3
"""段1（v6 信号）最优时点搜索 —— Walk-Forward + 抗过拟合。

段1 是旧 t0_idle_shadow（双段闲置，已退役）里唯一保留的闲置腿:
    11:25 v6选 → 13:05 买 → 13:30 定时(time)卖
（注：双段闲置 Shadow 整体已被 B+idle SHADOW 取代，本脚本仅作历史研究。）

本脚本严格复现 shadow 的选股口径（旧 scripts/t0_idle_shadow.py:pick_v6_top1）:
    - 信号时刻对全 T+0 池取「当日涨幅 ≥ 2%」的标的（与实盘 MIN_GAIN_V6=2.0 一致）
    - 取涨幅前 25，逐个算 v6 partial_score_at(信号时刻 close + 累计 volume)
    - 取 v6 分最高者（score>0）作为当日标的

然后 walk-forward 搜信号/买入/卖出时点:
    - 卖出模式: time（定时，对应现役段1）| trail（移动止盈，检验"延长持有"有无空间）
    - 物理先验: 段1 持仓很短（25 分钟级），trix 在此无意义，故不纳入
    - 长样本: aligned_live_4y.json（2022-06-15~2026-07-28，1000 交易日）
    - 抗过拟合五关: 物理先验 / 长样本 / walk-forward / 邻域稳健 / 多重检验 / 扣费

判定主指标用 t 值（自动惩罚低样本与高波动），而非累计收益。

用法:
    python scripts/search_leg1_wf.py
    python scripts/search_leg1_wf.py --start 2022-06-15 --split 0.6
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
from backtest_t0_idle_window import (  # noqa: E402
    IDLE_END,
    IDLE_START,
    LIVE_BUY,
    LIVE_SIGNAL,
    prev_trading_day,
    sell_time_mode,
    sell_trail_mode,
)
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    regime_on_date,
    resolve_eval_dates,
    time_to_min,
)
from rotation_v6 import partial_score_at  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402
from backtest_t0_idle_window import rank_by_today_gain  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
OUT_FILE = Path.home() / ".tradingagents/cache/t0_5min/leg1_wf.json"

MIN_GAIN_V6 = 2.0  # 与 shadow 一致

# 段1 时点的合理搜索空间（午前选股；午后买；最晚 14:45 前清仓，不挤占 14:50 实盘买）
GRID_SIGNALS = ["11:05", "11:15", "11:25", "11:35"]
GRID_BUYS = ["13:00", "13:05", "13:15", "13:30"]
GRID_SELLS = ["13:30", "14:00", "14:15", "14:30", "14:45"]
GRID_MODES = ["time", "trail"]

MIN_TRADES = 25  # 训练段最少笔数


# ─────────────────────────── v6 选股（复现 shadow pick_v6_top1） ──────────────

class V6Ranker:
    """预建索引，严格复现 shadow 段1 选股口径。"""

    def __init__(self, etf_list: list[dict], etf_daily: dict, etf_5min: dict) -> None:
        self.etf_list = etf_list
        self.etf_daily = etf_daily
        self.etf_5min = etf_5min
        # prev_close[code][day] = 昨收
        self.prev_close: dict[str, dict[str, float]] = {}
        # idx_map[code][day] = 在 returns 中的位置
        self.idx_map: dict[str, dict[str, int]] = {}
        for etf in etf_list:
            code = etf["code"]
            info = etf_daily.get(code)
            if not info:
                continue
            returns = info.get("returns", [])
            # cache 的 returns 缺 return_pct，按 compute_daily_data 口径补上
            for i, r in enumerate(returns):
                if "return_pct" not in r:
                    prev = float(returns[i - 1]["close"]) if i > 0 else 0.0
                    r["return_pct"] = ((float(r["close"]) - prev) / prev * 100) if prev else 0.0
            pcm: dict[str, float] = {}
            im: dict[str, int] = {}
            for i in range(1, len(returns)):
                pc = returns[i - 1].get("close")
                if pc and pc > 0:
                    pcm[returns[i]["date"]] = float(pc)
                im[returns[i]["date"]] = i
            self.prev_close[code] = pcm
            self.idx_map[code] = im

    def _cum_vol(self, bars: list[dict], tmin: int) -> float:
        c = 0.0
        for b in bars:
            parts = str(b.get("time", "")).split(":")
            if len(parts) < 2:
                continue
            bt = int(parts[0]) * 60 + int(parts[1])
            if bt <= tmin:
                c += float(b.get("volume") or 0)
            else:
                break
        return c

    def rank(self, day: str, signal_time: str) -> list[tuple[float, dict]]:
        tmin = time_to_min(signal_time)
        # 第一关：当日涨幅 ≥ MIN_GAIN_V6
        pre: list[tuple[float, dict]] = []
        for etf in self.etf_list:
            code = etf["code"]
            prev = self.prev_close.get(code, {}).get(day)
            if not prev or prev <= 0:
                continue
            bars = self.etf_5min.get(code, {}).get(day, [])
            px = price_at_time(bars, signal_time)
            if px is None or px <= 0:
                continue  # 无信号时刻价 → 无法算涨幅 → 排除（同 shadow 无行情）
            gain = (px - prev) / prev * 100
            if gain >= MIN_GAIN_V6:
                pre.append((gain, etf, px, bars))
        pre.sort(key=lambda x: x[0], reverse=True)
        pre = pre[:25]
        # 第二关：v6 partial_score_at，取最高分（>0）
        best: tuple[float, dict] | None = None
        for gain, etf, px, bars in pre:
            code = etf["code"]
            im = self.idx_map.get(code, {})
            if day not in im or im[day] < 3:
                continue
            idx = im[day]
            returns = self.etf_daily[code]["returns"]
            pvol = self._cum_vol(bars, tmin) or returns[idx].get("volume", 0)
            score = partial_score_at(returns, idx, float(px), float(pvol))
            if score > 0 and (best is None or score > best[0]):
                best = (score, etf)
        if best is None:
            return []
        return [best]


# ────────────────────────────── 组合校验 ──────────────────────────────

def in_idle(t: str) -> bool:
    return time_to_min(IDLE_START) <= time_to_min(t) <= time_to_min(IDLE_END)


def valid_combo(signal: str, buy: str, sell: str) -> bool:
    if not (in_idle(signal) and in_idle(buy) and in_idle(sell)):
        return False
    if time_to_min(buy) <= time_to_min(signal):
        return False
    if time_to_min(sell) <= time_to_min(buy):
        return False
    return True


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


def derive_pick(ranked: list[tuple[float, dict]]) -> tuple[str, float, str] | None:
    if not ranked:
        return None
    score, etf = ranked[0]
    return etf["code"], score, etf.get("name", etf["code"])


# ────────────────────────────── 主流程 ──────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="段1(v6) walk-forward 时点搜索")
    ap.add_argument("--cache", type=str, default=str(CACHE_FILE))
    ap.add_argument("--start", type=str, default="2022-06-15")
    ap.add_argument("--end", type=str, default="")
    ap.add_argument("--split", type=float, default=0.6)
    ap.add_argument("--fee", type=float, default=FEE_PCT)
    # 样本宇宙：idle = 旧口径(前一日有核心信号)；all = 全部交易日(含核心日)
    #   → 用于严谨回答「交易日 11:05-14:45 盘中块到底要不要动」
    ap.add_argument("--universe", type=str, default="idle", choices=["idle", "all"])
    args = ap.parse_args()

    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    all_dates = cache["all_dates"]
    proxy = cache.get("proxy_klines", [])
    eval_dates = resolve_eval_dates(all_dates, 0, args.start, args.end)
    etf_list = get_all_t0_etfs()

    print("=== 段1(v6 信号) Walk-Forward 时点搜索 ===")
    print(f"    区间 {eval_dates[0]} ~ {eval_dates[-1]}（{len(eval_dates)} 交易日）")
    print(f"    信号口径: 当日涨幅≥{MIN_GAIN_V6:.0f}% → v6 TOP1 | 卖: time/trail")

    ranker = V6Ranker(etf_list, etf_daily, etf_5min)

    # ① eligible 日
    #    idle: 前一日 14:45 有基线信号（旧口径，约 1/3 样本）
    #    all : 全部交易日（含核心日）—— 真正回答「交易日盘中块要不要动」
    print("\n>>> [1/5] 计算 eligible 日 (universe=%s) ..." % args.universe)
    base_pick: dict[str, tuple[str, float, str] | None] = {}
    for day in eval_dates:
        reg = regime_on_date(proxy, day)
        if reg and reg.get("skip_choppy"):
            base_pick[day] = None
            continue
        # 实盘基线选股 = 当日涨幅≥3% Top1（与 t0_monitor 一致）
        tg = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, LIVE_SIGNAL)
        cand = None
        for gain, etf in tg:
            if gain >= 3.0:
                cand = (etf["code"], gain, etf.get("name", etf["code"]))
                break
        base_pick[day] = cand

    if args.universe == "all":
        idle_days = list(eval_dates)  # 全部交易日
    else:
        idle_days = [
            d for d in eval_dates
            if (p := prev_trading_day(all_dates, d)) and base_pick.get(p)
        ]
    print(f"    eligible: {len(idle_days)} 天 / {len(eval_dates)}（universe={args.universe}）")

    split_at = int(len(idle_days) * args.split)
    train_days = set(idle_days[:split_at])
    test_days = set(idle_days[split_at:])
    print(f"    训练 {len(train_days)} 天 | 验证 {len(test_days)} 天")

    # ② 预计算各信号时点 v6 选股
    print("\n>>> [2/5] 预计算各信号时点 v6 选股 ...")
    picks: dict[str, dict[str, tuple[str, float, str] | None]] = {
        s: {} for s in GRID_SIGNALS
    }
    for si, signal in enumerate(GRID_SIGNALS, 1):
        for day in idle_days:
            ranked = ranker.rank(day, signal)
            picks[signal][day] = derive_pick(ranked)
        print(f"    {si}/{len(GRID_SIGNALS)} {signal} 完成")

    # ③ 网格模拟
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
            else:
                out = sell_time_mode(bars, buy, sell, bp, args.fee)
        sim_cache[key] = out
        return out

    combos = []
    for signal in GRID_SIGNALS:
        for buy in GRID_BUYS:
            for sell in GRID_SELLS:
                for mode in GRID_MODES:
                    if not valid_combo(signal, buy, sell):
                        continue
                    combos.append((signal, buy, sell, mode))

    print(f"\n>>> [3/5] 网格模拟 {len(combos)} 个组合 ...")
    results: list[dict] = []
    for ci, (signal, buy, sell, mode) in enumerate(combos, 1):
        tr_rets: list[float] = []
        te_rets: list[float] = []
        reasons: dict[str, int] = defaultdict(int)
        by_day: dict[str, float] = {}
        for day, pk in picks[signal].items():
            if not pk:
                continue
            code = pk[0]
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
            "signal": signal, "buy": buy, "sell": sell, "mode": mode,
            "label": f"{signal}选→{buy}买→{sell}[{mode}]",
            "train": stats_of(tr_rets),
            "test": stats_of(te_rets),
            "full": stats_of(tr_rets + te_rets),
            "reasons": dict(reasons),
            "by_day": by_day,
        })
        if ci % 40 == 0:
            print(f"    {ci}/{len(combos)}")

    if not results:
        print("无满足最小笔数的组合")
        sys.exit(1)

    n_eff = len(results)
    t_noise = math.sqrt(2 * math.log(n_eff))

    results.sort(key=lambda r: r["train"]["t"], reverse=True)

    print("\n" + "=" * 112)
    print(f"  [4/5] 训练段 TOP 12（按 t 值） | N={n_eff} | 噪声门槛 t_noise={t_noise:.2f}")
    print("=" * 112)
    head = (f"  {'#':>2} {'组合':<28} {'IS笔':>4} {'IS均笔':>7} {'IS_t':>6} "
            f"{'IS累计':>9} | {'OOS笔':>5} {'OOS均笔':>8} {'OOS_t':>6} {'OOS累计':>9} {'过噪声':>6}")
    print(head)
    print("  " + "-" * 108)
    for i, r in enumerate(results[:12], 1):
        tr, te = r["train"], r["test"]
        flag = "✓" if tr["t"] > t_noise else "✗"
        print(
            f"  {i:>2} {r['label']:<28} {tr['n']:>4} {tr['avg']:>+7.3f} {tr['t']:>6.2f} "
            f"{tr['cum_pct']:>+8.1f}% | {te.get('n', 0):>5} {te.get('avg', 0):>+8.3f} "
            f"{te.get('t', 0):>6.2f} {te.get('cum_pct', 0):>+8.1f}% {flag:>6}"
        )
    print("=" * 112)

    # ⑤ 稳健性判定
    print("\n>>> [5/5] 稳健性判定 ...")
    by_key = {(r["signal"], r["buy"], r["sell"], r["mode"]): r for r in results}

    def neighbor_score(r: dict) -> tuple[float, int, int]:
        si = GRID_SIGNALS.index(r["signal"])
        bi = GRID_BUYS.index(r["buy"])
        se = GRID_SELLS.index(r["sell"])
        mo = GRID_MODES.index(r["mode"])
        neigh = []
        for ds, db, dl, dm in (
            (-1, 0, 0, 0), (1, 0, 0, 0),
            (0, -1, 0, 0), (0, 1, 0, 0),
            (0, 0, -1, 0), (0, 0, 1, 0),
            (0, 0, 0, 1),
        ):
            s2, b2, e2, m2 = si + ds, bi + db, se + dl, mo + dm
            if not (0 <= s2 < len(GRID_SIGNALS) and 0 <= b2 < len(GRID_BUYS)
                    and 0 <= e2 < len(GRID_SELLS) and 0 <= m2 < len(GRID_MODES)):
                continue
            nb = by_key.get((GRID_SIGNALS[s2], GRID_BUYS[b2], GRID_SELLS[e2], GRID_MODES[m2]))
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
        print(f"\n  {'组合':<28} {'全样本笔':>7} {'均笔':>7} {'t':>6} {'累计':>10} {'胜率':>6} {'邻域':>6}")
        print("  " + "-" * 84)
        for r in survivors[:10]:
            f = r["full"]
            print(f"  {r['label']:<28} {f['n']:>7} {f['avg']:>+7.3f} {f['t']:>6.2f} "
                  f"{f['cum_pct']:>+9.1f}% {f['win_rate']:>5.1f}% {r['neighbor']:>6}")
    else:
        print("  ⚠ 没有任何组合能同时通过五重检验")
        near = sorted(results, key=lambda r: (-r["passed"], -r["train"]["t"]))[:5]
        for r in near:
            failed = [k for k, v in r["checks"].items() if not v]
            print(f"    {r['label']:<28} 通过 {r['passed']}/5，未过: {', '.join(failed)}")

    # 全组合分布
    is_avgs = sorted(r["train"]["avg"] for r in results)
    oos_avgs = sorted(r["test"].get("avg", 0) for r in results if r["test"].get("n", 0) >= 10)

    def pctl(xs: list[float], q: float) -> float:
        if not xs:
            return 0.0
        return xs[min(len(xs) - 1, int(len(xs) * q))]

    print("\n  ── 全 %d 组合均笔收益分布（%%/笔，已扣费）──" % n_eff)
    print(f"    {'':<8}{'p10':>9}{'p25':>9}{'中位':>9}{'p75':>9}{'p90':>9}{'正比例':>9}")
    for tag, xs in (("训练段", is_avgs), ("样本外", oos_avgs)):
        pos = sum(1 for x in xs if x > 0) / len(xs) * 100 if xs else 0
        print(f"    {tag:<8}{pctl(xs,0.1):>+9.3f}{pctl(xs,0.25):>+9.3f}{pctl(xs,0.5):>+9.3f}"
              f"{pctl(xs,0.75):>+9.3f}{pctl(xs,0.9):>+9.3f}{pos:>8.1f}%")

    # 现役段1 精确成绩 + 排名
    print("\n  ── 现役段1（11:25 选 → 13:05 买 → 13:30 time 卖）精确评估 ──")
    leg1_rets: list[float] = []
    leg1_reasons: dict[str, int] = defaultdict(int)
    for day, pk in picks["11:25"].items():
        if not pk:
            continue
        out = simulate(pk[0], day, "13:05", "13:30", "time")
        if not out:
            continue
        leg1_reasons[out[1]] += 1
        leg1_rets.append(out[0])
    ls = stats_of(leg1_rets)
    # 在结果集中找匹配项
    leg1_row = next((r for r in results
                    if r["signal"] == "11:25" and r["buy"] == "13:05"
                    and r["sell"] == "13:30" and r["mode"] == "time"), None)
    rank = (results.index(leg1_row) + 1) if leg1_row else None
    print(f"    全样本: {ls.get('n',0)} 笔 均笔 {ls.get('avg',0):+.3f}% t={ls.get('t',0):.2f} "
          f"累计 {ls.get('cum_pct',0):+.1f}% 胜率 {ls.get('win_rate',0):.1f}%")
    print(f"    卖出原因: {dict(leg1_reasons)}")
    if rank:
        print(f"    在 {n_eff} 组合中按训练 t 排名: {rank}（{'已接近最优' if rank <= 8 else '非最优，有调整空间'}）")
    else:
        print("    ⚠ 未进入搜索集（笔数不足）")

    OUT_FILE = Path.home() / f".tradingagents/cache/t0_5min/leg1_wf_{args.universe}.json"
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "window": [args.start, args.end or eval_dates[-1]],
        "universe": args.universe,
        "eligible_days": len(idle_days),
        "split": args.split,
        "n_combos": n_eff,
        "t_noise": round(t_noise, 3),
        "leg1": {"full": ls, "rank": rank},
        "survivors": [{k: v for k, v in r.items() if k != "by_day"} for r in survivors[:20]],
        "train_top": [{k: v for k, v in r.items() if k != "by_day"} for r in results[:20]],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {OUT_FILE}")


if __name__ == "__main__":
    main()

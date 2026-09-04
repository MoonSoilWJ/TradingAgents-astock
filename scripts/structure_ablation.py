#!/usr/bin/env python3
"""结构归因消融 — 三个策略的盈利到底来自哪一层结构?

把「尾盘买强 + 隔夜 + 次日上午了结」拆成四层, 逐层消融看贡献:

  L1 选股门槛: 当日涨幅 Top1 ≥3%(现状) vs ≥0% / ≥1.5% / ≥5% / 反向(买当日跌幅最大)
              → 检验「动量筛选」本身是否有 edge
  L3 持仓周期: 同一笔买入, 在不同时点卖 → 看收益在时间轴上如何累积
              D日15:00(无隔夜,仅尾盘10分钟) / D+1 09:35(仅隔夜跳空) /
              D+1 11:05(现状最优) / D+1 14:50(隔夜+全天)
              → 检验「隔夜」与「上午了结」各自贡献多少

(L2 买入时点 14:45 无法无前视地消融: 当日涨幅 14:45 才确定, 更早买入=未来函数。
 L4 卖出时点已由 structure_per_strategy.py 验证: 三策略分年同号 4/4)

用法:
    python3 scripts/structure_ablation.py --start 2022-11-15 --end 2026-07-31
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_b_idle_merge import SIGNAL_TIME, rank_by_today_gain  # noqa: E402
from backtest_t0_etf import bar_time_min, price_at_time  # noqa: E402
from backtest_t0_hybrid_sell import BUY_TIME  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT, MIN_GAIN, apply_net_return, gain_at_time, time_to_min,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
DENSE = ["tdx_5min_pre2024.json", "tdx_5min_2y.json"]
OUT_DIR = Path.home() / ".tradingagents/cache/t0_5min"


def build_picks_thr(dates, etf_list, etf_daily, etf_5min, thr, reverse=False):
    """按当日涨幅门槛选 Top1。reverse=True 选当日跌幅最大(反向对照)。"""
    picks = {}
    for day in dates:
        scores = rank_by_today_gain(etf_list, etf_daily, etf_5min, day, SIGNAL_TIME)
        chosen = None
        if reverse:
            cands = [(g, e) for g, e in scores if g is not None and g < 0]
            if cands:
                g, e = cands[-1]
                chosen = (e["code"], g, e.get("name") or e["code"])
        else:
            cands = [(g, e) for g, e in scores if g is not None and g >= thr]
            if cands:
                g, e = cands[0]
                chosen = (e["code"], g, e.get("name") or e["code"])
        picks[(SIGNAL_TIME, day)] = chosen
    return picks


def apply_confirm(picks, etf_daily, etf_5min, confirm_time, min_gain=MIN_GAIN):
    out, rejected = {}, 0
    for key, val in picks.items():
        if not val:
            out[key] = val
            continue
        g = gain_at_time(etf_daily, etf_5min, val[0], key[1], confirm_time)
        if g is not None and g < min_gain:
            out[key] = None
            rejected += 1
        else:
            out[key] = val
    return out, rejected


def eq_of(rets: list[float]) -> float:
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return (eq - 1) * 100


def main() -> None:
    ap = argparse.ArgumentParser(description="结构归因消融")
    ap.add_argument("--start", default="2022-11-15")
    ap.add_argument("--end", default="2026-07-31")
    ap.add_argument("--fee", type=float, default=FEE_PCT)
    args = ap.parse_args()

    print("载入缓存 ...", flush=True)
    cache = json.loads(Path(CACHE).read_text(encoding="utf-8"))
    etf_daily = cache["etf_daily"]

    etf_5min: dict = {}
    for name in DENSE:
        p = OUT_DIR / name
        print(f"  载入5min: {p.name} ...", flush=True)
        for c, days in json.loads(p.read_text(encoding="utf-8"))["etf_5min"].items():
            etf_5min.setdefault(c, {}).update(days)

    from collections import Counter
    cnt: Counter = Counter()
    for _c, days in etf_5min.items():
        for d, bars in days.items():
            if bars:
                cnt[d] += 1
    five_dates = sorted(d for d, n in cnt.items() if n >= 20)
    all_dates = sorted(set(cache["all_dates"]) & set(five_dates))
    all_dates = [d for d in all_dates if args.start <= d <= args.end]
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in set(etf_5min)]
    print(f"\n窗口 {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}天) | 池 {len(etf_list)} 只\n")

    def rows_for(picks):
        rows = []
        for day in all_dates:
            p = picks.get((SIGNAL_TIME, day))
            if not p:
                continue
            code, gain, name = p
            db = etf_5min.get(code, {}).get(day, [])
            buy = price_at_time(db, BUY_TIME)
            if not buy or buy <= 0 or day not in all_dates:
                continue
            i = all_dates.index(day)
            if i + 1 >= len(all_dates):
                continue
            nb = etf_5min.get(code, {}).get(all_dates[i + 1], [])
            if not nb:
                continue
            rows.append({"day": day, "code": code, "buy": buy,
                         "day_bars": db, "next_bars": nb, "gain": gain})
        return rows

    def close_at(bars, tstr):
        """tstr 时刻前最后一根已完成 bar 的收盘价。"""
        cm = time_to_min(tstr)
        last = None
        for b in sorted(bars, key=bar_time_min):
            if bar_time_min(b) <= cm:
                last = float(b["close"])
        return last

    # ══ L1 选股门槛消融(关闭 confirm, 纯净门槛对比; 卖出统一 D+1 11:05) ══
    print("=" * 92)
    print("  [L1] 选股门槛消融 — 检验「动量筛选」本身是否有 edge")
    print("       (关闭 14:40 双时点确认, 否则 ≥0% 组会被确认回 ≥3%; 卖出统一 次日11:05)")
    print("=" * 92)
    print(f"  {'门槛':<28}{'笔数':>6}{'净累计':>12}{'胜率':>8}{'均笔':>9}")
    print("  " + "-" * 63)
    l1 = {}
    for label, thr, rev in [("买当日涨幅Top1 ≥0%(无门槛)", 0.0, False),
                            ("买当日涨幅Top1 ≥1.5%", 1.5, False),
                            ("买当日涨幅Top1 ≥3%【现状】", 3.0, False),
                            ("买当日涨幅Top1 ≥5%", 5.0, False),
                            ("买当日跌幅最大Top1(反向对照)", 0.0, True)]:
        pk = build_picks_thr(all_dates, etf_list, etf_daily, etf_5min, thr, rev)
        rows = rows_for(pk)
        rets = []
        for r in rows:
            sp = close_at(r["next_bars"], "11:05")
            if sp and sp > 0:
                rets.append(apply_net_return(r["buy"], sp, args.fee))
        e = eq_of(rets) if rets else 0.0
        win = sum(1 for x in rets if x > 0) / len(rets) * 100 if rets else 0
        avg = sum(rets) / len(rets) if rets else 0
        l1[label] = round(e, 2)
        print(f"  {label:<28}{len(rets):>6}{e:>+11.2f}%{win:>7.1f}%{avg:>+8.3f}%")

    # ══ L3 持仓周期消融(开启 confirm, 保持现状口径) ══
    print("\n" + "=" * 92)
    print("  [L3] 持仓周期消融 — 同一笔买入(D日14:50), 在不同时点卖, 看收益如何累积")
    print("=" * 92)
    pk = build_picks_thr(all_dates, etf_list, etf_daily, etf_5min, 3.0)
    pk, rej = apply_confirm(pk, etf_daily, etf_5min, "14:40")
    rows = rows_for(pk)
    print(f"  (含 14:40 双时点确认, 否决 {rej} 天 → {len(rows)} 笔)\n")
    print(f"  {'卖点时点':<34}{'笔数':>6}{'净累计':>12}{'胜率':>8}{'均笔':>9}")
    print("  " + "-" * 69)
    l3 = {}
    variants = [
        ("D日 15:00 卖(无隔夜,仅尾盘10分钟)", "same"),
        ("D+1 09:35 卖(仅吃隔夜跳空)", "0935"),
        ("D+1 11:05 卖【现状最优】", "1105"),
        ("D+1 14:50 卖(隔夜+次日全天)", "1450"),
    ]
    for label, mode in variants:
        rets = []
        for r in rows:
            if mode == "same":
                sp = float(r["day_bars"][-1]["close"]) if r["day_bars"] else None
            else:
                sp = close_at(r["next_bars"], {"0935": "09:35", "1105": "11:05",
                                               "1450": "14:50"}[mode])
            if sp and sp > 0:
                rets.append(apply_net_return(r["buy"], sp, args.fee))
        e = eq_of(rets) if rets else 0.0
        win = sum(1 for x in rets if x > 0) / len(rets) * 100 if rets else 0
        avg = sum(rets) / len(rets) if rets else 0
        l3[label] = round(e, 2)
        print(f"  {label:<34}{len(rets):>6}{e:>+11.2f}%{win:>7.1f}%{avg:>+8.3f}%")

    # 归因
    print("\n  ── 收益在时间轴上的累积(由 L3 反推) ──")
    g = lambda k: l3.get(k, 0.0)  # noqa: E731
    same = g("D日 15:00 卖(无隔夜,仅尾盘10分钟)")
    a935 = g("D+1 09:35 卖(仅吃隔夜跳空)")
    a1105 = g("D+1 11:05 卖【现状最优】")
    a1450 = g("D+1 14:50 卖(隔夜+次日全天)")
    print(f"    尾盘10分钟(无隔夜)      : {same:+.2f}%   ← 日内几乎无 edge")
    print(f"    + 隔夜跳空(到09:35)     : {a935:+.2f}%   ← 隔夜贡献")
    print(f"    + 上午(09:35→11:05)     : {a1105:+.2f}%   ← 上午再增厚")
    print(f"    + 下午(11:05→14:50)     : {a1450:+.2f}%   ← 下午全部回吐")

    print("\n" + "=" * 92)
    print("  【归因结论】")
    print("=" * 92)
    no_thr = l1.get("买当日涨幅Top1 ≥0%(无门槛)", 0)
    thr3 = l1.get("买当日涨幅Top1 ≥3%【现状】", 0)
    rev = l1.get("买当日跌幅最大Top1(反向对照)", 0)
    print(f"    L1 动量筛选: ≥0% {no_thr:+.2f}% → ≥3% {thr3:+.2f}% "
          f"({'门槛有效 +' + format(thr3 - no_thr, '.1f') + 'pp' if thr3 > no_thr else '门槛无增益/负面'})")
    print(f"    L1 反向对照: 买当日跌幅最大 = {rev:+.2f}% "
          f"({'反向亏损 → 动量方向正确' if rev < 0 else '⚠ 反向也赚 → 不是动量, 是别的机制'})")
    print(f"    L3 隔夜    : 无隔夜 {same:+.2f}% → 仅隔夜 {a935:+.2f}% "
          f"({'隔夜是主要来源' if a935 > same else '⚠ 隔夜无贡献'})")
    print(f"    L4 上午了结: 11:05 {a1105:+.2f}% vs 14:50 {a1450:+.2f}% "
          f"({'上午了结必要' if a1105 > a1450 else '⚠ 下午更好'})")

    p = OUT_DIR / f"structure_ablation_{args.start}_{args.end}.json"
    p.write_text(json.dumps({"window": f"{all_dates[0]}~{all_dates[-1]}",
                             "L1_threshold": l1, "L3_holding": l3},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n落盘: {p}")


if __name__ == "__main__":
    main()

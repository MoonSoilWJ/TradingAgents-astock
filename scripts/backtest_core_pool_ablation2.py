#!/usr/bin/env python3
"""补充消融: D = A池(优质/原池路由) + 无 regime 品类过滤 + 不skip
   对照已落盘的 A/B/P (trix卖点), 隔离"去掉品类白名单过滤"这一维度的纯贡献。
   D - A = 在A池上去掉品类过滤的增量
   B - P = 在全市场池上去掉品类过滤的增量 (已算: OOS +68%)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa: E402
from quality_pool import (  # noqa: E402
    regime_uses_quality_pool, _quality_pool_for_day, load_quality_pool,
    pick_top1_from_pool, regime_on_date,
)
from backtest_t0_today1 import FEE_PCT, rank_by_today_gain, passes_gain_filter  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
START = "2022-06-15"
LB = 30


def stats_of(trades):
    if not trades:
        return {"trades": 0, "equity_pct": 0.0, "win_rate": 0.0, "max_drawdown": 0.0}
    eq = cur = 1.0
    peak = 1.0
    mdd = 0.0
    for t in sorted(trades, key=lambda x: x["signal_date"]):
        r = t["return_pct"]
        eq *= 1 + r / 100
        cur *= 1 + r / 100
        peak = max(peak, cur)
        mdd = min(mdd, (cur - peak) / peak * 100)
    win = sum(1 for t in trades if t["return_pct"] > 0) / len(trades) * 100
    return {"trades": len(trades), "equity_pct": round((eq - 1) * 100, 2),
            "win_rate": round(win, 1), "max_drawdown": round(mdd, 1)}


def build_picks_D(eval_dates, orig_pool, etf_daily, etf_5min, all_dates, proxy, lookback, warmup):
    sq = load_quality_pool()
    picks = {}
    for i, day in enumerate(eval_dates):
        if i < warmup:
            picks[(SIGNAL_TIME, day)] = None
            continue
        reg = regime_on_date(proxy, day)
        if regime_uses_quality_pool(reg):   # 趋势/震荡 → 优质池, 去品类过滤, 不skip
            pool = _quality_pool_for_day(
                day, eval_dates, etf_daily, etf_5min, all_dates, proxy,
                lookback=lookback, static_quality=sq, orig_pool=orig_pool)
            picks[(SIGNAL_TIME, day)] = pick_top1_from_pool(
                pool, day, etf_daily, etf_5min, proxy,
                skip_choppy=False, use_regime_filter=False)
        else:                                # 中性 → 原T0池(本就无品类过滤), 不skip
            scores = rank_by_today_gain(orig_pool, etf_daily, etf_5min, day, SIGNAL_TIME)
            cands = [(g, e) for g, e in scores if passes_gain_filter(g)]
            if cands:
                g, e = cands[0]
                picks[(SIGNAL_TIME, day)] = (e["code"], g, e.get("name") or e.get("etf_name") or e["code"])
            else:
                picks[(SIGNAL_TIME, day)] = None
    return picks


def main():
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    proxy = cache.get("proxy_klines", [])
    codes5 = set(etf_5min.keys())
    orig_pool = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    eval_dates = [d for d in all_dates if d >= START]

    print("构造 D 变体 (A池 + 无品类过滤 + 不skip) ...", flush=True)
    pD = build_picks_D(eval_dates, orig_pool, etf_daily, etf_5min, all_dates, proxy, LB, LB)
    r = run_strategy("trix", eval_dates, all_dates, pD, etf_5min, FEE_PCT)
    tD = r["trades"] if r else []
    sD = stats_of(tD)

    abl = json.loads(
        (Path.home() / ".tradingagents/cache/t0_5min/core_pool_ablation.json").read_text(encoding="utf-8"))

    print(f"\n=== 补充消融: D = A池 + 无品类过滤 + 不skip (trix卖点) ===")
    print(f"窗口 {eval_dates[0]}~{eval_dates[-1]}")
    print(f"  D in-sample: {sD['equity_pct']:+.2f}%  笔{sD['trades']}  胜{sD['win_rate']:.1f}%  "
          f"MDD{sD['max_drawdown']:+.1f}%")

    core_eval = eval_dates[LB:]
    split = int(len(core_eval) * 0.6)
    split_date = core_eval[split]

    def on(tr, lo=None, hi=None):
        return [t for t in tr
                if (lo is None or t["signal_date"] >= lo)
                and (hi is None or t["signal_date"] < hi)]

    oosD = stats_of(on(tD, lo=split_date))
    oosA = abl["walk_forward"]["oos"]["A"]
    oosB = abl["walk_forward"]["oos"]["B"]
    oosP = abl["walk_forward"]["oos"]["P"]
    print(f"\n  OOS段(分界{split_date}):")
    print(f"    A={oosA['equity_pct']:+.2f}%(n{oosA['trades']})  "
          f"D={oosD['equity_pct']:+.2f}%(n{oosD['trades']})  "
          f"B={oosB['equity_pct']:+.2f}%(n{oosB['trades']})  "
          f"P={oosP['equity_pct']:+.2f}%(n{oosP['trades']})")

    d_cat = round(oosD["equity_pct"] - oosA["equity_pct"], 2)
    print(f"\n  Δ(品类过滤) = D - A = {d_cat:+.2f}%  (+{oosD['trades']-oosA['trades']}笔)  "
          f"← 在A池上去掉regime品类白名单的纯贡献")
    print(f"  对照 B - P (全市场池上去掉品类过滤) = "
          f"{round(oosB['equity_pct']-oosP['equity_pct'],2):+.2f}%")
    print(f"  D vs B 差 (候选集大小贡献) = "
          f"{round(oosB['equity_pct']-oosD['equity_pct'],2):+.2f}%  "
          f"(+{oosB['trades']-oosD['trades']}笔)")

    abl["D_in_sample"] = sD
    abl["D_oos"] = oosD
    abl["ablation_cat"] = {
        "d_cat_oos": d_cat,
        "b_minus_p_oos": round(oosB["equity_pct"] - oosP["equity_pct"], 2),
        "pool_size_contrib_oos": round(oosB["equity_pct"] - oosD["equity_pct"], 2),
        "pool_size_contrib_n": oosB["trades"] - oosD["trades"],
    }
    OUT = Path.home() / ".tradingagents/cache/t0_5min/core_pool_ablation.json"
    OUT.write_text(json.dumps(abl, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已合并落盘: {OUT}")


if __name__ == "__main__":
    main()

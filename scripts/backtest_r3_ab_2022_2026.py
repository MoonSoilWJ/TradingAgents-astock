#!/usr/bin/env python3
"""生成 A / B / R3 三策略 2022-2026 全4年回测逐笔 + 摘要, 落盘供 export_to_web.py 填充网站回测。

复用现有无偏回测引擎 (与 backtest_recent100_live_vs_b_idle.py 同口径):
  A  = build_picks_hybrid(scheme A 现状) + apply_confirm(14:40) + run_strategy("trix")
  B  = build_picks_B(全市场Top1≥3%)       + apply_confirm(14:40) + run_strategy("trix")
  R3 = build_picks_jq(pool_fn=R3月度池)    + apply_confirm(14:40) + run_strategy("trix")

数据: 无偏5min = tdx_5min_pre2024 + tdx_5min_2y
      etf_daily / all_dates / proxy 取自主缓存 aligned_live_4y.json

输出: ~/.tradingagents/rotation/backtest_22_26.json
  {
    "window": "2022-06-15~2026-07-31",
    "trading_days": N,
    "A":  {"stats": {...}, "trades": [...]},
    "B":  {"stats": {...}, "trades": [...]},
    "R3": {"stats": {...}, "trades": [...]},
  }
其中 trades 字段对齐 run_strategy 输出 (signal_date/sell_date/etf/name/buy_price/
sell_price/return_pct/sell_reason/signal_time/buy_time/sell_time/today_gain),
stats 字段对齐 stats_of (equity_pct/trades/win_rate/max_drawdown/...)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import backtest_t0_hybrid_sell as BH  # noqa: E402
BH.MIN_TRADES = 1  # 短窗口不做最小笔数门槛

from backtest_t0_hybrid_sell import run_strategy  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    MIN_GAIN,
    gain_at_time,
    rank_by_today_gain,
    passes_gain_filter,
)
from backtest_b_idle_merge import build_picks_B, stats_of  # noqa: E402
from quality_pool import (  # noqa: E402
    build_picks_hybrid,
    regime_on_date,
    BLACKLIST_CODES,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402
import jq_attack_pools  # noqa: E402

CACHE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
DENSE = [
    Path.home() / ".tradingagents/cache/t0_5min/tdx_5min_pre2024.json",
    Path.home() / ".tradingagents/cache/t0_5min/tdx_5min_2y.json",
]
OUT = Path.home() / ".tradingagents/rotation/backtest_22_26.json"
START, END = "2022-06-15", "2026-07-31"

R3_POOLS = jq_attack_pools.JQ_ATTACK_POOLS.get("R3", {})
R3_UNIVERSE = sorted({c for v in R3_POOLS.values() for c in v})
NAME = {e["code"]: e.get("name") or e["code"] for e in get_all_t0_etfs()}


def r3_pool_fn(day: str) -> list[dict]:
    """R3 月度轮动池: 使用月 -> 上月末池(无未来函数), 缺失回退全并集。

    聚宽 JQ_ATTACK_POOLS["R3"] 的键已经是「使用月 / 值=上月末池」, 故直接取。
    code 从 jq 格式 '159129.XSHE' 转本地 '159129'。
    """
    ym = day[:7]
    codes = R3_POOLS.get(ym) or R3_UNIVERSE
    return [
        {"code": c.split(".")[0], "name": NAME.get(c.split(".")[0], c.split(".")[0])}
        for c in codes
    ]


def build_picks_jq(eval_dates, pool, etf_daily, etf_5min, all_dates, proxy,
                   lookback=30, topn=25, signal_time="14:45", gate_ma=20, pool_fn=None):
    """Faithful 复刻聚宽 scan_at_1440 选股 (对齐实盘 R3 必需)。

    趋势/震荡 -> 动量 Top topn 优质池; 中性 -> pool_fn(day) 月度池。
    复制自 backtest_unified_2022_2026.build_picks_jq (避免引入 matplotlib 重依赖)。
    """
    def close_above_ma(code, day):
        info = etf_daily.get(code)
        if not info:
            return True
        rets = info["returns"]
        idm = {r["date"]: i for i, r in enumerate(rets)}
        if day not in idm:
            return True
        idx = idm[day]
        if idx < gate_ma:
            return True
        window = rets[idx - gate_ma: idx + 1]   # 含当日共 gate_ma+1 根
        ma = sum(r["close"] for r in window[:-1]) / gate_ma
        today = window[-1]["close"]
        if ma <= 0 or today <= 0:
            return True
        return today > ma

    def momentum_top(day):
        idx = all_dates.index(day)
        start = max(0, idx - lookback + 1)
        wset = set(all_dates[start: idx + 1])
        scored = []
        base = pool_fn(day) if pool_fn else pool
        for e in base:
            code = e["code"]
            info = etf_daily.get(code)
            if not info:
                continue
            rets = info["returns"]
            closes = [r["close"] for r in rets if r["date"] in wset]
            if len(closes) < 2:
                continue
            mom = (closes[-1] - closes[0]) / closes[0] * 100
            scored.append((mom, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:topn]]

    picks = {}
    for day in eval_dates:
        reg = regime_on_date(proxy, day)
        mode = (reg or {}).get("mode", "中性")
        qpool = momentum_top(day) if mode in ("趋势", "震荡") else (pool_fn(day) if pool_fn else pool)
        scores = rank_by_today_gain(qpool, etf_daily, etf_5min, day, signal_time)
        chosen = None
        for g, e in scores:
            if not passes_gain_filter(g):
                continue
            if e["code"] in BLACKLIST_CODES:
                continue
            if not close_above_ma(e["code"], day):
                continue
            chosen = (e["code"], g, e.get("name") or e["code"])
            break
        picks[(signal_time, day)] = chosen
    return picks


def apply_confirm(picks, etf_daily, etf_5min, confirm_time, min_gain=MIN_GAIN):
    """双时点确认: confirm_time 时刻涨幅也须 ≥ min_gain (对齐实盘 t0_monitor CONFIRM_TIME)。"""
    out, rejected = {}, 0
    for key, val in picks.items():
        if not val:
            out[key] = val
            continue
        code = val[0]
        g = gain_at_time(etf_daily, etf_5min, code, key[1], confirm_time)
        if g is not None and g < min_gain:
            out[key] = None
            rejected += 1
        else:
            out[key] = val
    return out, rejected


def main() -> None:
    ap = argparse.ArgumentParser(description="A/B/R3 三策略 2022-2026 全4年回测")
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument("--five-min", default=",".join(str(p) for p in DENSE))
    ap.add_argument("--fee", type=float, default=FEE_PCT)
    ap.add_argument("--confirm-time", default="14:40")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    cf_time = args.confirm_time

    print("载入缓存 ...", flush=True)
    cache = json.loads(Path(CACHE).read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]
    proxy = cache["proxy_klines"]

    etf_5min = {}
    for part in args.five_min.split(","):
        part = part.strip()
        if not part:
            continue
        print(f"  载入密集5min: {Path(part).name} ...", flush=True)
        d = json.loads(Path(part).read_text(encoding="utf-8"))
        dense = d.get("etf_5min", d)
        for c, days in dense.items():
            etf_5min.setdefault(c, {}).update(days)
    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]

    test_dates = [d for d in all_dates if args.start <= d <= args.end]
    eval_dates = test_dates  # warmup=0; hybrid 动量/train 基于全局 all_dates
    print(f"\n=== 窗口 {test_dates[0]} ~ {test_dates[-1]} ({len(test_dates)} 天) | "
          f"候选池 {len(etf_list)} 只 | 费率 {args.fee*100:.3f}% | "
          f"确认 {cf_time} ===\n", flush=True)

    # ① A: hybrid-A 选股 + 确认 + TRIX 卖
    print(">>> A: hybrid-A 选股 ... (滚动优质池)", flush=True)
    pa_full = build_picks_hybrid(
        eval_dates, etf_list, etf_daily, etf_5min, all_dates, proxy,
        lookback=args.lookback, warmup=0,
    )
    pa = {k: v for k, v in pa_full.items() if k[1] in set(test_dates)}
    pa_cf, rej_a = apply_confirm(pa, etf_daily, etf_5min, cf_time)
    print(f"    A 双时点确认否决 {rej_a} 天", flush=True)
    A = run_strategy("trix", test_dates, all_dates, pa_cf, etf_5min, args.fee)

    # ② B: 全市场 Top1 + 确认 + TRIX 卖
    print(">>> B: 全市场Top1 选股 ...", flush=True)
    pb = build_picks_B(test_dates, etf_list, etf_daily, etf_5min, 0)
    pb_cf, rej_b = apply_confirm(pb, etf_daily, etf_5min, cf_time)
    print(f"    B 双时点确认否决 {rej_b} 天", flush=True)
    B = run_strategy("trix", test_dates, all_dates, pb_cf, etf_5min, args.fee)

    # ③ R3: 月度轮动池 + 确认 + TRIX 卖
    print(">>> R3: 月度轮动池 选股 ...", flush=True)
    pr_full = build_picks_jq(
        eval_dates, [], etf_daily, etf_5min, all_dates, proxy,
        lookback=30, topn=25, signal_time="14:45", pool_fn=r3_pool_fn,
    )
    pr = {k: v for k, v in pr_full.items() if k[1] in set(test_dates)}
    pr_cf, rej_r3 = apply_confirm(pr, etf_daily, etf_5min, cf_time)
    print(f"    R3 双时点确认否决 {rej_r3} 天", flush=True)
    R3 = run_strategy("trix", test_dates, all_dates, pr_cf, etf_5min, args.fee)

    result = {
        "window": f"{test_dates[0]}~{test_dates[-1]}",
        "trading_days": len(test_dates),
        "five_min_source": [Path(p.strip()).name for p in args.five_min.split(",") if p.strip()],
        "confirm_time": cf_time,
        "A": {"stats": stats_of(A["trades"]), "trades": A["trades"]},
        "B": {"stats": stats_of(B["trades"]), "trades": B["trades"]},
        "R3": {"stats": stats_of(R3["trades"]), "trades": R3["trades"]},
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已落盘: {out_path}")
    for k in ("A", "B", "R3"):
        s = result[k]["stats"]
        print(f"  {k}: 累计 {s['equity_pct']:+9.2f}%  笔数 {s['trades']:>4}  "
              f"胜率 {s['win_rate']:5.1f}%  MDD {s['max_drawdown']:6.1f}%")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""导出 22-26 窗口 A/B 每日最终选股 (含 apply_confirm 14:40) 供聚宽重放成交。

复用 backtest_r3_ab_2022_2026 的本地回测引擎口径:
  A = build_picks_hybrid(scheme A) + apply_confirm(14:40)
  B = build_picks_B(全市场Top1)    + apply_confirm(14:40)
成交引擎差异交给聚宽独立复现 (本地 run_strategy("trix") 对齐: 买14:50, TRIX(5,3)死叉+11:05)。

输出 scripts/jq_ab_pools.py:
  JQ_BACKTEST_WINDOW, JQ_LOCAL_CANDIDATE{code:name},
  JQ_LOCAL_A_PICKS{date:code|None}, JQ_LOCAL_B_PICKS{date:code|None}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import backtest_r3_ab_2022_2026 as AB  # 复用 CACHE/DENSE/START/END 及全部引擎
from backtest_b_idle_merge import build_picks_B  # noqa: E402
from quality_pool import build_picks_hybrid  # noqa: E402

CF_TIME = "14:40"


def main() -> None:
    cache = json.loads(Path(AB.CACHE).read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]
    proxy = cache["proxy_klines"]

    etf_5min: dict = {}
    for part in AB.DENSE:
        d = json.loads(Path(part).read_text(encoding="utf-8"))
        dense = d.get("etf_5min", d)
        for c, days in dense.items():
            etf_5min.setdefault(c, {}).update(days)
    codes5 = set(etf_5min.keys())
    etf_list = [e for e in AB.get_all_t0_etfs() if e["code"] in codes5]
    NAME = {e["code"]: e.get("name") or e["code"] for e in etf_list}

    test_dates = [d for d in all_dates if AB.START <= d <= AB.END]

    # ② B: 全市场 Top1 + 确认
    pb = build_picks_B(test_dates, etf_list, etf_daily, etf_5min, 0)
    pb_cf, rej_b = AB.apply_confirm(pb, etf_daily, etf_5min, CF_TIME)

    # ① A: hybrid-A 选股 + 确认
    pa_full = build_picks_hybrid(
        test_dates, etf_list, etf_daily, etf_5min, all_dates, proxy,
        lookback=30, warmup=0,
    )
    pa = {k: v for k, v in pa_full.items() if k[1] in set(test_dates)}
    pa_cf, rej_a = AB.apply_confirm(pa, etf_daily, etf_5min, CF_TIME)

    def to_codes(picks):
        return {day: (val[0] if val else None) for (st, day), val in picks.items()}

    A = to_codes(pa_cf)
    B = to_codes(pb_cf)
    cand = {e["code"]: e.get("name") or e["code"] for e in etf_list}

    out = SCRIPT_DIR / "jq_ab_pools.py"
    import re
    py = lambda d: re.sub(r"\bnull\b", "None", json.dumps(d, ensure_ascii=False))
    lines = [
        "# 自动生成 by scripts/export_jq_ab_pools.py — 勿手改",
        f"# 窗口 {AB.START}~{AB.END} | 候选池 {len(cand)} 只 | 确认 {CF_TIME}",
        f'JQ_BACKTEST_WINDOW = "{AB.START}~{AB.END}"',
        f"JQ_LOCAL_CANDIDATE = {py(cand)}",
        f"JQ_LOCAL_A_PICKS = {py(A)}",
        f"JQ_LOCAL_B_PICKS = {py(B)}",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    a_trades = sum(1 for v in A.values() if v)
    b_trades = sum(1 for v in B.values() if v)
    print(f"候选池 {len(cand)} 只")
    print(f"A 选股交易日 {a_trades} (确认否决 {rej_a})")
    print(f"B 选股交易日 {b_trades} (确认否决 {rej_b})")
    print(f"已写入 {out}")


if __name__ == "__main__":
    main()

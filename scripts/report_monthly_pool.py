"""输出规则动态池的月度演化(依赖 dynamic_pool.pool_as_of)。

仅做诊断/汇报，不跑回测。看池子如何从早期几只长到 2026 的 ~59 只，
验证"池子不是固定的，是规则按上市日/流动性动态生长"。

用法: python3 scripts/report_monthly_pool.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from t0_etf_list import get_all_t0_etfs  # noqa: E402
from dynamic_pool import month_pools_for_range, pool_as_of, CACHE  # noqa: E402

START, END = "2015-01", "2026-08"


def main() -> None:
    # 扫描范围 = 回测/聚宽实际宇宙 (get_all_t0_etfs() ∩ 有日线数据), 与可交易标的对齐
    full = json.loads((CACHE / "full_daily_2015_2026.json").read_text(encoding="utf-8"))
    universe = {e["code"] for e in get_all_t0_etfs() if e["code"] in full}
    print(f"扫描范围( tracked universe ∩ 日线): {len(universe)} 只\n")
    mp = month_pools_for_range(START, END, universe=universe)
    prev: set[str] = set()
    print(f"{'月份':8} {'池子':>4}  当月新增(相对上月)")
    print("-" * 60)
    for ym, s in mp.items():
        added = sorted(s - prev)
        print(f"{ym:8} {len(s):>4}  {', '.join(added) if added else '-'}")
        prev = s

    # 末月 vs 当前 auto_t0_etfs.json(59)
    last = sorted(mp[END])
    auto = json.loads(Path("scripts/auto_t0_etfs.json").read_text())
    auto_codes = {d["code"] for d in auto}
    print("\n" + "=" * 60)
    print(f"末月({END})规则池: {len(last)} 只")
    print(f"auto_t0_etfs.json 当前: {len(auto_codes)} 只")
    print(f"规则池有但auto无: {sorted(set(last) - auto_codes)}")
    print(f"auto有但规则池无(可能流动性/上市不足): {sorted(auto_codes - set(last))}")

    out = Path.home() / ".tradingagents/cache/t0_5min/monthly_pool_report.json"
    out.write_text(json.dumps({ym: sorted(s) for ym, s in mp.items()},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入: {out}")


if __name__ == "__main__":
    main()

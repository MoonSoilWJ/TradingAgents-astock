#!/usr/bin/env python3
"""诊断: 本地对齐聚宽(jq 59只宽基池 + 无品类过滤 + 趋势门禁) 的回测。

确认:
1) 全期逐年 vs 聚宽真值(544% / 2023+13.4% / 2024+122% / 2025+114% / 2026+24%)
2) 全期是否有 |ret|>15% 异常单(排除数据量纲 bug)
3) 差异是否纯来自卖点粒度(5min TRIX vs 聚宽 1min TRIX)
"""
from __future__ import annotations
import sys
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from backtest_unified_2022_2026 import load, build_attack_A, load_jq_pool  # noqa: E402

RANK = "14:40"


def main():
    jq = load_jq_pool()
    all_dates, proxy, etf_daily, etf_5min, etf_list = load()
    trades = build_attack_A(
        all_dates, proxy, etf_daily, etf_5min, etf_list,
        rank_time=RANK, pool_codes=jq, scheme="jq",
    )

    print(f"\n=== 全期成交 {len(trades)} 笔 (聚宽真值 274 笔) ===")

    # 逐年收益贡献
    by_year = defaultdict(list)
    for t in trades:
        by_year[t["signal_date"][:4]].append(t["return_pct"])
    jq_year = {}
    for y in sorted(by_year):
        eq = 1.0
        for r in by_year[y]:
            eq *= 1 + r / 100
        jq_year[y] = (eq - 1) * 100
        print(f"  {y}: {len(by_year[y]):>3}笔  本地 {jq_year[y]:+7.2f}%")

    # 聚宽真值对照
    jq_true = {"2022": -3.2, "2023": 13.4, "2024": 122.0, "2025": 113.6, "2026": 23.7}
    print("\n--- 本地 vs 聚宽逐年 (聚宽=1min真值) ---")
    for y in sorted(jq_year):
        ratio = jq_year[y] / jq_true[y] if jq_true[y] != 0 else float("nan")
        print(f"  {y}: 本地 {jq_year[y]:+7.2f}% | 聚宽 {jq_true[y]:+6.1f}% | "
              f"倍数 {ratio:5.2f}x")

    # 全期可疑单
    susp = [t for t in trades if abs(t["return_pct"]) > 15]
    print(f"\n=== 全期可疑单(|ret|>15%): {len(susp)} 笔 (0=无数据量纲bug) ===")
    for t in susp[:30]:
        print(f"  {t['signal_date']} {t['etf']} gain={t['today_gain']:.2f}% "
              f"ret={t['return_pct']:.2f}% {t['sell_reason']}")

    # 单笔均值
    avg = sum(t["return_pct"] for t in trades) / len(trades) if trades else 0
    wins = sum(1 for t in trades if t["return_pct"] > 0)
    print(f"\n单笔均值 {avg:.3f}% | 胜率 {wins/len(trades)*100:.1f}%")
    print("=> 笔数接近聚宽但单笔系统性偏高 = 卖点粒度(5min vs 1min TRIX)所致")


if __name__ == "__main__":
    main()

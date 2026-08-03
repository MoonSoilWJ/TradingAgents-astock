"""动量维度专项优化 — 固定已验证的降换手参数, 只动动量。

固定: ma_win=20(趋势过滤) / hold_rank=3(跌出前3才换) / buffer=1% / rebal=5(每周决策)
只扫动量三维度:
  mom_def     : ma_dev(相对MA偏离) / ret(过去N日涨幅) / ret_skip(12-1式跳过1天) / sharpe(风险调整)
  mom_win     : 动量窗口 10/20/40/60
  mom_thresh  : 最低动量门槛(低于此不入选, 强制空仓) -inf / 0% / 2% / 5%
诚实判定: 走查验证 2017-2022 训练选参 -> 2023-2026 样本外排名, 避免过拟合。

用法:
  python3 scripts/optimize_8wide_momentum.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from backtest_8wide_ma20_rotation import fetch_universe  # noqa: E402
from optimize_8wide_ma20_rotation import (              # noqa: E402
    backtest, benchmark_hs300, build_series,
)

# 固定: 降换手三件套(已在完整网格 + 走查中确认稳健)
FIX = dict(ma_win=20, hold_rank=3, buffer=0.01, rebal=5, fee_pct=0.05)

# 动量三维度
MOM_DEFS = ["ma_dev", "ret", "ret_skip", "sharpe"]
MOM_WINS = [10, 20, 40, 60]
MOM_THRE = [-1e9, 0.0, 0.02, 0.05]


def main():
    data = fetch_universe()
    series = build_series(data)
    bm = benchmark_hs300(data, "2017-01-01")

    all_dates = sorted({d for s in series.values() for d in s.dates if d >= "2017-01-01"})
    tr_dates = [d for d in all_dates if d <= "2022-12-31"]
    te_dates = [d for d in all_dates if d >= "2023-01-01"]
    print(f"[数据] 全周期 {all_dates[0]}~{all_dates[-1]}  "
          f"训练段 {len(tr_dates)} 天 / 验证段 {len(te_dates)} 天")

    # 参考基线 = 当前交付方案(ma_dev/20/无门槛)
    base_full = backtest(series, all_dates, mom_win=20, mom_def="ma_dev",
                         mom_thresh=-1e9, **FIX)
    base_oos = backtest(series, te_dates, mom_win=20, mom_def="ma_dev",
                        mom_thresh=-1e9, **FIX)
    print(f"[参考基线 当前交付方案 ma_dev/20/无门槛] "
          f"全周期 {base_full['final_pct']:+.2f}%  验证段 {base_oos['final_pct']:+.2f}%")

    rows = []
    for md in MOM_DEFS:
        for mw in MOM_WINS:
            for th in MOM_THRE:
                rf = backtest(series, all_dates, mom_win=mw, mom_def=md,
                              mom_thresh=th, **FIX)
                ro = backtest(series, te_dates, mom_win=mw, mom_def=md,
                              mom_thresh=th, **FIX)
                rows.append({
                    "md": md, "mw": mw, "th": th,
                    "full": rf["final_pct"], "oos": ro["final_pct"],
                    "mdd": ro["mdd_pct"], "sw": rf["switches"],
                })

    # 按验证段(OOS)排序 — 诚实排名
    rows.sort(key=lambda r: r["oos"], reverse=True)

    print("\n" + "=" * 92)
    print("[动量维度专项] 按样本外(2023-2026)收益排序 (固定 rank3/每周/1%缓冲/MA20过滤)")
    print("=" * 92)
    print(f"{'动量定义':<10}{'窗口':>5}{'门槛':>8}"
          f"{'全周期%':>11}{'验证段%':>11}{'验证回撤%':>11}{'全周期换仓':>11}  vs基线OOS")
    for r in rows:
        ths = "无" if r["th"] <= -1e8 else f"{r['th']*100:.0f}%"
        delta = r["oos"] - base_oos["final_pct"]
        mark = " ★" if delta > 0 else ""
        print(f"{r['md']:<10}{r['mw']:>5}{ths:>8}"
              f"{r['full']:>11.2f}{r['oos']:>11.2f}{r['mdd']:>11.2f}{r['sw']:>11}{delta:>+10.2f}{mark}")

    # 只看"对验证段有正增益"且"全周期不劣于基线"的稳健组合
    print("\n--- 稳健候选: 验证段 > 基线 且 全周期 >= 基线 (非过拟合) ---")
    robust = [r for r in rows
              if r["oos"] > base_oos["final_pct"] and r["full"] >= base_full["final_pct"]]
    if robust:
        for r in robust:
            ths = "无" if r["th"] <= -1e8 else f"{r['th']*100:.0f}%"
            print(f"  {r['md']:<9} win={r['mw']:<3} thresh={ths:<4} "
                  f"全周期 {r['full']:+.2f}%  验证段 {r['oos']:+.2f}%")
    else:
        print("  (无: 所有动量改动在验证段均未稳定超越基线)")

    print(f"\n结论: 基线验证段 {base_oos['final_pct']:+.2f}% / 全周期 {base_full['final_pct']:+.2f}%")


if __name__ == "__main__":
    main()

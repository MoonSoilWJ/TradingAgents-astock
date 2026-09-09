#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游资半路板 — 规则层基线回测(AI 层的对照基线)。

口径(对齐实盘 youzi_live 的规则召回层):
  · 池: 沪深主板(60/00 开头), 排除科创688/创业板30/北证/ST
  · 触发: 30分钟K 涨幅 ≥ thr 且该K未封板(能买到) —— 每日每票只取首次触发
  · 买入: 触发K的收盘价
  · 卖出: 次日收盘(对照: 次日开盘), 成本 0.2%
  · 封板: 当日 15:00 收盘是否涨停

输出: 每日笔数分布 / 封板率 / 次日收益 / 分时段 / 分月 / 配额(每日前3)对照

用法: python3 scripts/backtest_youzi_baseline.py [thr=8]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

MIN30 = Path.home() / ".tradingagents" / "youzi" / "min30.pkl"
COST = 0.002
LABEL = {1: "10:00", 2: "10:30", 3: "11:00", 4: "11:30",
         5: "13:30", 6: "14:00", 7: "14:30", 8: "15:00"}


def stat(name: str, g: pd.DataFrame, col: str = "ret_close") -> str:
    if len(g) == 0:
        return f"{name:<14}{0:>7}{'—':>9}{'—':>9}{'—':>9}{'—':>9}"
    seal = g["sealed"].mean() * 100
    o, c = g["ret_open"].mean() * 100, g[col].mean() * 100
    # 按日等权再复利 — 直接连乘每笔会爆炸(2万笔连乘无意义)
    dr = g.groupby("date")[col].mean().fillna(0)
    eq = ((1 + dr).prod() - 1) * 100
    return (f"{name:<14}{len(g):>7}{seal:>8.1f}%{o:>+8.2f}%{c:>+8.2f}%"
            f"{eq:>+9.1f}%")


def main() -> int:
    thr = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    thr /= 100
    print(f"读取 {MIN30} ...", flush=True)
    df = pd.read_pickle(MIN30)
    df = df[df["code"].astype(str).str[0].isin(["6", "0"])].copy()
    df = df[~df["code"].astype(str).str.startswith("688")]   # 排除科创
    df["date"] = df["datetime"].dt.date
    df = df.sort_values(["code", "datetime"])
    df["idx"] = df.groupby(["code", "date"]).cumcount() + 1

    daily = df.groupby(["code", "date"]).agg(
        d_open=("open", "first"), d_close=("close", "last")).reset_index()
    daily["prev_close"] = daily.groupby("code")["d_close"].shift(1)
    daily = daily.sort_values(["code", "date"])
    daily["nxt_open"] = daily.groupby("code")["d_open"].shift(-1)
    daily["nxt_close"] = daily.groupby("code")["d_close"].shift(-1)

    df = df.merge(daily[["code", "date", "prev_close"]], on=["code", "date"],
                  how="left")
    df = df.dropna(subset=["prev_close"])
    df["pct"] = df["close"] / df["prev_close"] - 1
    df["limit_px"] = (df["prev_close"] * 1.10).round(2)
    df["is_limit"] = df["close"] >= df["limit_px"] - 0.001
    print(f"K线 {len(df):,} 行 | 交易日 {df['date'].nunique()} | "
          f"股票 {df['code'].nunique()}", flush=True)

    # 当日最终是否封板(15:00 收盘涨停)
    sealed = df[df["idx"] == 8].groupby(["code", "date"])["is_limit"].max()
    sealed.name = "sealed"

    sig = df[(df["pct"] >= thr) & (~df["is_limit"])].copy()
    sig = sig.sort_values(["code", "date", "idx"]).groupby(
        ["code", "date"]).head(1)                 # 每日每票首次触发
    sig = sig.merge(sealed, on=["code", "date"], how="left")
    sig = sig.merge(daily[["code", "date", "nxt_open", "nxt_close"]],
                    on=["code", "date"], how="left")
    sig = sig.dropna(subset=["nxt_close"])
    sig["ret_open"] = sig["nxt_open"] / sig["close"] - 1 - COST
    sig["ret_close"] = sig["nxt_close"] / sig["close"] - 1 - COST

    n_days = sig["date"].nunique()
    per_day = sig.groupby("date").size()
    out = [f"\n{'='*66}",
           f"游资半路板 规则层基线 (阈值 {thr*100:.0f}%, 未封板触发, 次日收盘卖)",
           f"交易日 {n_days} | 总笔数 {len(sig):,} | 日均 {len(sig)/n_days:.1f} 笔",
           f"{'='*66}", "",
           "【每日笔数分布】",
           f"  日均 {per_day.mean():.1f} | 中位数 {per_day.median():.0f} | "
           f"最多 {per_day.max()} | 最少 {per_day.min()} | "
           f"0笔天数占比 {(per_day==0).mean()*100:.0f}%",
           f"  分位: p25={per_day.quantile(.25):.0f} p75={per_day.quantile(.75):.0f} "
           f"p90={per_day.quantile(.90):.0f}", "",
           f"{'分组':<14}{'笔数':>7}{'封板率':>9}{'次日开':>9}{'次日收':>9}{'累计':>9}",
           "-" * 66]
    out.append(stat("全部", sig))

    out.append("")
    out.append("【分时段(首次触发时点)】")
    for i in sorted(sig["idx"].unique()):
        out.append(stat(LABEL.get(int(i), str(i)), sig[sig["idx"] == i]))

    out.append("")
    out.append("【卖法 A/B/C 对比】(次日收列=该卖法下的收益)")
    sig["ret_mix"] = np.where(sig["idx"] >= 6,        # 14:00后触发 → 次日开卖
                              sig["ret_open"], sig["ret_close"])
    out.append(stat("A 统一次日收卖", sig))
    out.append(stat("B 尾盘改次日开卖", sig, col="ret_mix"))
    out.append(stat("C 全部次日开卖", sig, col="ret_open"))

    out.append("")
    out.append("【分封板状态 × 卖法】(判断: 卖法该不该按是否封板分开)")
    for lbl, sub in (("当日封板", sig[sig["sealed"]]),
                     ("当日未封", sig[~sig["sealed"]])):
        out.append(stat(f"{lbl}-次日开卖", sub, col="ret_open"))
        out.append(stat(f"{lbl}-次日收卖", sub))

    out.append("")
    out.append("【配额对照: 每日按涨幅取前N笔】(AI层实际会再筛掉约9成)")
    for n in (3, 5, 10):
        top = sig.sort_values(["date", "pct"], ascending=[True, False]) \
                 .groupby("date").head(n)
        out.append(stat(f"每日前{n}笔", top))

    out.append("")
    out.append("【分年】")
    sig["year"] = pd.to_datetime(sig["date"]).dt.year
    for y in sorted(sig["year"].unique()):
        out.append(stat(f"{y}", sig[sig["year"] == y]))

    out.append("")
    out.append("【近 12 个月】")
    sig["ym"] = pd.to_datetime(sig["date"]).dt.strftime("%Y-%m")
    for m in sorted(sig["ym"].unique())[-12:]:
        out.append(stat(m, sig[sig["ym"] == m]))

    # 逐日权益(等权, 每日全仓)
    day_ret = sig.groupby("date")["ret_close"].mean()
    eq = (1 + day_ret.fillna(0)).cumprod()
    out += ["", "【累计净值(每日等权全仓, 起点1.0)】",
            f"  最终 {eq.iloc[-1]:.2f}x | 最大回撤 "
            f"{((eq / eq.cummax() - 1).min()) * 100:.1f}%"]
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 【事前指标验证 · 外盘/产业链传导 → A股ETF 事件研究】
#
# 检验逻辑(诚实的检验, 重点看"看得到的是否吃得到"):
#   1) IC: 外盘 T-1 日收益 与 A股ETF T 日收益 的秩相关(全样本/分年, 看衰减)
#   2) 事件研究: 外盘单日涨幅 > 阈值 时, A股ETF 当日的
#        跳空(prev_close->open) / 日内(open->close) / 全天(prev_close->close) / 次日
#      若"跳空显著、日内不显著" → 信息在开盘被消化, 散户无法执行(典型结论)
#   3) 对照: 无条件基准收益 + t 检验
#
# 传导链: COMEX黄金->518880/517400, WTI原油->162719, 纳指->513100
# 用法: python3 scripts/event_lead_indicators.py
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

CACHE = Path("/Users/licheng/.tradingagents/cache/lead_indicators")
START = "2022-01-01"

CHAINS = [
    ("COMEX黄金", "GC", "sh518880", "黄金ETF"),
    ("COMEX黄金", "GC", "sh517400", "黄金股ETF"),
    ("WTI原油", "CL", "sz162719", "石油LOF"),
    ("纳指", ".IXIC", "sh513100", "纳指ETF"),
]


def _cached(tag, fn):
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (tag + ".json")
    if f.exists():
        return pd.read_json(f)
    df = fn()
    df.to_json(f, orient="records")
    return df


def load_lead(kind, symbol):
    import akshare as ak
    if kind == "future":
        df = _cached("fut_" + symbol, lambda: ak.futures_foreign_hist(symbol=symbol))
        df["date"] = pd.to_datetime(df["date"])
        return df[["date", "open", "close"]].sort_values("date").reset_index(drop=True)
    df = _cached("us_" + symbol.replace(".", ""),
                 lambda: ak.index_us_stock_sina(symbol=symbol))
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "open", "close"]].sort_values("date").reset_index(drop=True)


def load_etf(symbol):
    import akshare as ak
    df = _cached("etf_" + symbol.replace("sh", "").replace("sz", ""),
                 lambda: ak.fund_etf_hist_sina(symbol=symbol))
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"] >= START].sort_values("date").reset_index(drop=True)
    return df[["date", "open", "close"]]


def align_lead_rets(lead_dates, lead_rets, a_dates):
    """对每个 A股日 t, 取 lead 中最后一个 < t 的交易日的收益(信息在开盘前已知)."""
    idx = np.searchsorted(lead_dates, a_dates, side="left") - 1
    out = np.full(len(a_dates), np.nan)
    ok = idx >= 0
    out[ok] = lead_rets[idx[ok]]
    return out


def ic_stats(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 30:
        return np.nan, np.nan, 0
    rho, p = stats.spearmanr(x[m], y[m])
    return rho, p, int(m.sum())


def tstat(x):
    x = x[np.isfinite(x)]
    if len(x) < 10:
        return np.nan, np.nan
    t, p = stats.ttest_1samp(x, 0)
    return t, p


def stars(p):
    return "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else ""


def run_chain(lead_name, lead, etf_name, etf, thr=0.01):
    ld = lead["date"].to_numpy()
    lr = (lead["close"] / lead["close"].shift(1) - 1).to_numpy()
    a_dates = etf["date"].to_numpy()
    o, c = etf["open"].to_numpy(), etf["close"].to_numpy()
    prev_c = np.roll(c, 1)
    prev_c[0] = np.nan
    gap = o / prev_c - 1          # 跳空
    intra = c / o - 1             # 日内
    day = c / prev_c - 1          # 全天
    nxt = np.roll(day, -1)
    nxt[-1] = np.nan              # 次日

    x = align_lead_rets(ld, lr, a_dates)

    print("\n" + "=" * 92)
    print("%s -> %s (%s)   %s ~ %s  N=%d" % (
        lead_name, etf_name, etf_name, pd.Timestamp(a_dates[0]).date(),
        pd.Timestamp(a_dates[-1]).date(), len(a_dates)))
    print("-" * 92)

    # IC
    rho, p, n = ic_stats(x, day)
    print("IC(外盘T-1 收益 vs ETF T日全天) Spearman=%.3f  p=%.4f  n=%d %s"
          % (rho, p, n, stars(p)))
    yr = pd.DatetimeIndex(a_dates).year
    per_yr = []
    for y in sorted(set(yr)):
        m = yr == y
        r2, p2, n2 = ic_stats(x[m], day[m])
        per_yr.append("%d:%+.2f%s" % (y, r2, stars(p2)))
    print("分年 IC: " + "  ".join(per_yr))

    # 基准
    print("基准(无条件日均): 跳空 %+.3f%%  日内 %+.3f%%  全天 %+.3f%%"
          % (np.nanmean(gap) * 100, np.nanmean(intra) * 100, np.nanmean(day) * 100))

    # 事件
    ev = np.isfinite(x) & (x > thr)
    print("事件: 外盘前一日涨幅 > %.0f%%  事件日 %d 个 (占比 %.1f%%)"
          % (thr * 100, ev.sum(), ev.mean() * 100))
    for tag, arr in (("跳空(昨收→开盘)", gap), ("日内(开盘→收盘)", intra),
                     ("全天(昨收→收盘)", day), ("次日", nxt)):
        e = arr[ev]
        e = e[np.isfinite(e)]
        t, p = tstat(e)
        base = np.nanmean(arr)
        print("  %-16s 事件均值 %+7.3f%%  基准 %+7.3f%%  超额 %+7.3f%%  t=%5.2f %s"
              % (tag, np.nanmean(e) * 100, base * 100, (np.nanmean(e) - base) * 100, t, stars(p)))
    # 反向对称
    evn = np.isfinite(x) & (x < -thr)
    e = day[evn]
    t, p = tstat(e)
    print("  反向(外盘 < -%.0f%%, %d 个): ETF全天均值 %+.3f%%  t=%5.2f %s"
          % (thr * 100, evn.sum(), np.nanmean(e) * 100, t, stars(p)))

    # 定价充分度条件化: 开盘只补了一部分 → 日内是否补涨?
    m = np.isfinite(x) & (x > thr) & np.isfinite(gap)
    ratio = np.where(m, gap / x, np.nan)
    print("  [条件化] 开盘定价充分度 = A股跳空 / 外盘涨幅")
    for tag, sel, note in (
            ("开盘定价不足(<50%)", m & (ratio < 0.5), "若日内显著为正 → 可执行残肉"),
            ("开盘定价充分(50~100%)", m & (ratio >= 0.5) & (ratio <= 1.0), ""),
            ("开盘过度定价(>100%)", m & (ratio > 1.0), "若日内显著为负 → 反向做空残肉")):
        e = intra[sel]
        t, p = tstat(e)
        if len(e) < 10:
            print("    %-22s 样本不足(%d)" % (tag, len(e)))
            continue
        print("    %-22s %4d 个  日内均值 %+7.3f%%  t=%5.2f %s   %s"
              % (tag, len(e), np.nanmean(e) * 100, t, stars(p), note))


def main():
    leads = {}
    for lead_name, symbol, kind in (("COMEX黄金", "GC", "future"),
                                    ("WTI原油", "CL", "future"),
                                    ("纳指", ".IXIC", "us")):
        leads[lead_name] = load_lead(kind, symbol)

    print("=" * 92)
    print("外盘/产业链 → A股ETF 事前指标事件研究   (2022-01 ~ 2026-11)")
    print("判读: 若 跳空 显著为正 而 日内 不显著 → 信息开盘即被消化, 散户不可执行")
    print("=" * 92)

    for lead_name, symbol, etf_sym, etf_name in CHAINS:
        try:
            etf = load_etf(etf_sym)
            if len(etf) < 200:
                print("\n[SKIP] %s 数据太少(%d)" % (etf_name, len(etf)))
                continue
            run_chain(lead_name, leads[lead_name], etf_sym, etf)
        except Exception as e:
            print("\n[FAIL] %s: %s" % (etf_name, repr(e)[:120]))


if __name__ == "__main__":
    main()

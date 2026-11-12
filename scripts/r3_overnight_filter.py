#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 【R3 外盘隔夜过滤器验证】
# 数据: ~/.tradingagents/rotation/r3_jq_backtest.json (聚宽 canonical R3 全量交易 2014-2026, 554笔)
# 外盘: COMEX黄金(GC) / WTI原油(CL) / 纳指(.IXIC), 取 T-1 日收益(A股T日开盘前已知)
#
# 检验: 买入前夜外盘大跌时, R3 当笔交易是否显著更差?
#   · 分组统计(按外盘收益分位)
#   · 过滤扫描: 跳过"外盘 < 阈值"的日子后, 胜率/均笔/累计的变化
#   · 对照: 外盘大跌日 R3 原本就空仓吗?(笔数占比) → 判断过滤器是否有增量
#
# 用法: python3 scripts/r3_overnight_filter.py
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROT = Path("/Users/licheng/.tradingagents/rotation")
CACHE = Path("/Users/licheng/.tradingagents/cache/lead_indicators")

LEADS = [("纳指", "us_IXIC"), ("黄金", "fut_GC"), ("原油", "fut_CL")]


def load_lead(tag):
    f = CACHE / (tag + ".json")
    df = pd.read_json(f)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    ret = df["close"].pct_change()
    return df["date"].to_numpy(), df["close"].to_numpy(), ret.to_numpy()


def lead_ret_at(ld, lr, day, strict_before=True):
    """取最后一个 < day (或 <= day) 的外盘交易日收益."""
    side = "left" if strict_before else "right"
    i = np.searchsorted(ld, np.datetime64(day), side=side) - 1
    if i < 0 or i >= len(lr):
        return np.nan
    return lr[i]


def group_stats(rets, tag, base_mean):
    r = np.array(rets)
    if len(r) < 5:
        return "  %-24s 样本不足(%d)" % (tag, len(r))
    t, p = stats.ttest_1samp(r, base_mean)
    return ("  %-24s %4d 笔  胜率 %4.0f%%  均笔 %+7.3f%%  累计 %+8.1f%%  t=%5.2f %s"
            % (tag, len(r), (r > 0).mean() * 100, r.mean() * 100,
               (np.prod(1 + r) - 1) * 100, t,
               "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else ""))


def main():
    d = json.load(open(ROT / "r3_jq_backtest.json"))
    trades = d["trades"]
    sig = np.array([t["signal_date"] for t in trades])
    rets = np.array([t["return_pct"] / 100 for t in trades])
    etfs = np.array([t["etf"] for t in trades])
    names = np.array([t.get("name", "") for t in trades])
    buy_px = np.array([t["buy_price"] for t in trades])
    expo = np.array([t.get("today_gain") for t in trades], dtype=float)

    print("=" * 100)
    print("R3 外盘隔夜过滤器验证   聚宽 canonical 交易 %d 笔  %s ~ %s"
          % (len(trades), d["window"].split("~")[0], d["window"].split("~")[1]))
    print("全样本: 胜率 %.1f%%  均笔 %+.3f%%  中位 %+.3f%%  累计 %+.1f%%"
          % ((rets > 0).mean() * 100, rets.mean() * 100, np.median(rets) * 100,
             (np.prod(1 + rets) - 1) * 100))
    print("=" * 100)

    lead_series = {name: load_lead(tag) for name, tag in LEADS}

    # 每笔交易的外盘条件
    conds = {}
    for name, (ld, lc, lr) in lead_series.items():
        conds[name] = np.array([lead_ret_at(ld, lr, s) for s in sig])
        cov = np.isfinite(conds[name]).mean()
        print("\n[%s] 覆盖 %.1f%% (外盘数据 %s ~ %s)"
              % (name, cov * 100,
                 pd.Timestamp(ld[0]).date(), pd.Timestamp(ld[-1]).date()))

        x = conds[name]
        print("  按外盘 T-1 收益分组:")
        for lo, hi, tag in ((-9, -0.02, "< -2%"), (-0.02, -0.01, "-2~-1%"),
                            (-0.01, 0.0, "-1~0%"), (0.0, 0.01, "0~1%"),
                            (0.01, 0.02, "1~2%"), (0.02, 9, "> 2%")):
            m = np.isfinite(x) & (x >= lo) & (x < hi)
            if m.sum() >= 5:
                print(group_stats(rets[m], tag, np.nanmean(rets)))

        # 关键: 外盘大跌日 R3 原本就空仓吗? → 条件日 vs 非条件日的"交易发生概率"
        if name == "纳指":
            ld_i = np.searchsorted(ld, np.array([np.datetime64(s) for s in sig]), side="left") - 1
            valid = (ld_i >= 0) & (ld_i < len(lr))
            # 按外盘日聚合: 该外盘日对应的"下一个A股交易日"是否成交
            traded_on = np.zeros(len(lr), bool)
            traded_on[ld_i[valid]] = True
            # A股交易日近似 = 外盘有数据的交易日(2014-2026重合度>95%)
            has_bar = np.isfinite(lr)
            print("  交易发生概率(判断 R3 是否内生回避大跌日):")
            for lo, tag in ((-0.015, "外盘<-1.5%"), (-0.02, "外盘<-2%"), (-0.01, "外盘<-1%")):
                cond = has_bar & (lr < lo)
                rest = has_bar & (lr >= lo)
                p_cond = traded_on[cond].mean() * 100 if cond.sum() else np.nan
                p_rest = traded_on[rest].mean() * 100 if rest.sum() else np.nan
                print("    %-12s 条件日 %4d 天 交易概率 %.1f%%  |  其余日 %4d 天 交易概率 %.1f%%  → %s"
                      % (tag, cond.sum(), p_cond, rest.sum(), p_rest,
                         "R3已回避" if p_cond < p_rest * 0.7 else "未回避"))

        # 过滤扫描
        print("  过滤扫描(跳过外盘 < 阈值的信号日):")
        for thr in (-0.03, -0.02, -0.015, -0.01, -0.005):
            keep = ~(np.isfinite(x) & (x < thr))
            k = rets[keep]
            drop = rets[~keep]
            if len(drop) < 5:
                continue
            print("    跳过 %-6s: 剔除 %3d 笔(均笔 %+6.3f%%) → 剩余 %3d 笔 胜率 %.1f%% 均笔 %+.3f%% 累计 %+.1f%%"
                  % ("%.1f%%" % (thr * 100), len(drop), drop.mean() * 100,
                     len(k), (k > 0).mean() * 100, k.mean() * 100,
                     (np.prod(1 + k) - 1) * 100))

    # 组合过滤器: 纳指 + 黄金同时大跌(风险偏好收缩)
    print("\n[组合过滤器] 纳指 T-1 与 黄金 T-1 同时为负:")
    nd = conds["纳指"]
    gd = conds["黄金"]
    for thr in (-0.01, -0.015):
        m = np.isfinite(nd) & np.isfinite(gd) & (nd < thr) & (gd < 0)
        if m.sum() >= 5:
            print("  纳指<%.0f%% 且黄金<0: %d 笔  胜率 %.0f%%  均笔 %+.3f%%  累计 %+.1f%%"
                  % (thr * 100, m.sum(), (rets[m] > 0).mean() * 100,
                     rets[m].mean() * 100, (np.prod(1 + rets[m]) - 1) * 100))
            keep = ~m
            k = rets[keep]
            print("    剔除后: %d 笔 胜率 %.1f%% 均笔 %+.3f%% 累计 %+.1f%%"
                  % (len(k), (k > 0).mean() * 100, k.mean() * 100,
                     (np.prod(1 + k) - 1) * 100))

    # 分标的类别(按名称关键词)看外盘影响
    print("\n[分标的类别] 纳指 T-1 < -1.5% 时各类 ETF 的表现:")
    cats = {
        "黄金类": ("黄金", "金", "白银"),
        "油气类": ("油", "能源", "天然气"),
        "纳指/美股": ("纳指", "标普", "美国", "纳斯达克"),
        "港股类": ("港", "恒生", "恒指", "H股", "中概"),
    }
    for cat, kws in cats.items():
        sel = np.array([any(k in n for k in kws) for n in names])
        m = sel & np.isfinite(nd) & (nd < -0.015)
        if m.sum() >= 3:
            print("  %-8s 事件日 %2d 笔  均笔 %+7.3f%%  胜率 %3.0f%%   | 全时段 %3d 笔 均笔 %+.3f%%"
                  % (cat, m.sum(), rets[m].mean() * 100, (rets[m] > 0).mean() * 100,
                     sel.sum(), rets[sel].mean() * 100))
    print("\n" + "=" * 100)
    print("判读: 若过滤后 胜率/均笔 提升且剔除笔数不多 → 外盘过滤器有正增量;")
    print("      若各分组差异不显著(t<2) → 外盘信息在 R3 上已被候选池选择内生消化, 不可加过滤。")


if __name__ == "__main__":
    main()

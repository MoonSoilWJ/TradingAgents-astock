#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 回测任意 A股标的 的「N12 结果簇」趋势择时策略, 检验该结构是否适用.
# 策略口径与 backtest_588000_n12.py / verify_generality.py 完全一致:
#   6 组合 TRIX [(10,9),(10,12),(12,9),(12,12),(14,9),(14,12)] 逐日投票,
#   簇看多占比 > 0.5 → 持仓, 否则空仓. 信号当日收盘同价成交(14:55可执行), 滑点 SLIP.
#
# 用法:
#   python3 scripts/backtest_n12_stock.py 湖南白银
#   python3 scripts/backtest_n12_stock.py 002716 --recent 12
#
# ⚠️ 个股特有风险(回测未模拟): 涨跌停无法成交 / 停牌跳空 / 个股暴雷退市,
#    且样本内含强趋势段时买入持有可能天然占优 — 结论需结合逐年分解看.
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from n12_cluster_now import COMB_N12, THR, connect_tdx, fetch_daily, resolve_symbol, trix_series


def vote_target(close, combos=COMB_N12, thr=THR):
    """簇逐日看多占比 → 目标仓位(1/0). 前 100 根视为预热期强制空仓."""
    trs, sigs = [], []
    for n, m in combos:
        tr, sig = trix_series(close, n, m)
        trs.append(tr)
        sigs.append(sig)
    frac = (np.array(trs) > np.array(sigs)).astype(int).mean(0)
    tgt = (frac > thr).astype(int)
    tgt[:100] = 0  # 预热: TRIX(14,12) 未稳
    return tgt, frac


def sim_close(target, close, slip):
    """信号当日收盘同价成交(同 588000 N12 口径). 返回 eq, total, mdd, sw, trades."""
    cash, units, pos, eq, sw, prev = 1.0, 0.0, 0, [], 0, 0
    trades, entry = [], None
    for i in range(len(close)):
        t = int(target[i]); nd = float(close[i])
        if t != prev:
            sw += 1
            if t == 1 and pos == 0:
                fee = cash * slip
                units = (cash - fee) / nd; cash = 0.0; pos = 1
                entry = (i, nd * (1 + slip))
            elif t == 0 and pos == 1:
                amt = units * nd; fee = amt * slip
                cash = amt - fee; units = 0.0; pos = 0
                trades.append({"bi": entry[0], "bpx": entry[1], "si": i,
                               "spx": nd * (1 - slip), "open": False})
                entry = None
        prev = t
        eq.append(cash + units * nd)
    if pos == 1 and entry is not None:
        trades.append({"bi": entry[0], "bpx": entry[1], "si": len(close) - 1,
                       "spx": float(close[-1]), "open": True})
    eq = np.array(eq)
    total = eq[-1] / eq[0] - 1
    mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    return eq, total, mdd, sw, trades


def sim_open_next(target, opens, closes, slip):
    """保守执行: T 日信号, T+1 开盘成交, 权益按 T+1 收盘计. 返回 eq(对齐 dates[1:]), ..."""
    cash, units, pos, eq, sw, prev = 1.0, 0.0, 0, [], 0, 0
    for i in range(1, len(closes)):
        t = int(target[i - 1]); op = float(opens[i])
        if t != prev:
            sw += 1
            if t == 1 and pos == 0:
                fee = cash * slip
                units = (cash - fee) / op; cash = 0.0; pos = 1
            elif t == 0 and pos == 1:
                amt = units * op; fee = amt * slip
                cash = amt - fee; units = 0.0; pos = 0
        prev = t
        eq.append(cash + units * float(closes[i]))
    eq = np.array(eq)
    total = eq[-1] / eq[0] - 1
    mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    return eq, total, mdd, sw


def yearly_table(dates, eq, close):
    """逐年收益: 策略(eq曲线) vs 买入持有(close)."""
    de = pd.Series(eq, index=dates).resample("YE").last()
    dc = pd.Series(np.asarray(close, float), index=dates).resample("YE").last()
    r_s = de.pct_change(); r_s.iloc[0] = de.iloc[0] / eq[0] - 1
    r_h = dc.pct_change(); r_h.iloc[0] = dc.iloc[0] / close[0] - 1
    return [(d.year, float(a), float(b)) for d, a, b in zip(de.index, r_s, r_h)]


def fmt_row(tag, total, ann, mdd, sw, expo=""):
    return "%-22s 累计 %10s  年化 %8s  回撤 %8s  切换 %4d  %s" % (
        tag, "%.1f%%" % (total * 100), "%.2f%%" % (ann * 100),
        "%.1f%%" % (mdd * 100), sw, expo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--recent", type=int, default=10, help="显示最近多少笔交易")
    args = ap.parse_args()

    code, name, _ = resolve_symbol(args.symbol)
    api = connect_tdx()
    try:
        f, mkt = fetch_daily(api, code)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    if f is None:
        print("拉取 %s 日线失败" % code)
        raise SystemExit(1)

    label = "%s(%s)" % (name, code) if name else code
    dates = pd.to_datetime(f["date"].values)
    close = f["close"].values.astype(float)
    opens = f["open"].values.astype(float)

    tgt, frac = vote_target(close)
    w = 100  # 预热起点
    d, c, o, t = dates[w:], close[w:], opens[w:], tgt[w:]

    span_y = max((d[-1] - d[0]).days / 365.25, 1e-9)

    def ann(x):
        return (1 + x) ** (1 / span_y) - 1

    def sim_single(c, slip):
        """单 (12,9) TRIX 金叉持仓对照, 与簇同口径."""
        tr, sig = trix_series(c, 12, 9)
        tgt1 = (tr > sig).astype(int)
        tgt1[:100] = 0
        eqs, tots, mdds, sws, _ = sim_close(tgt1, c, slip)
        return tots, ann(tots), mdds, sws

    print("=" * 78)
    print("N12 结果簇 个股回测  标的: %s  [%s]   区间 %s ~ %s (%d 交易日, %.1f 年)" %
          (label, mkt, d[0].date(), d[-1].date(), len(c), span_y))
    print("规则: 6组合TRIX簇投票>0.5持仓, 否则空仓 (同 588000 N12 口径)")
    print("=" * 78)

    # ---- [1] 主对照: 当日收盘成交 ----
    SLIP = 0.001  # 个股: 佣金+印花税约 万10/回合
    eq, tot, mdd, sw, trades = sim_close(t, c, SLIP)
    eq1, tot1, mdd1, sw1, tr1 = sim_close(t, c, 0.0005)
    eq2, tot2, mdd2, sw2, tr2 = sim_close(t, c, 0.002)
    hold = c[-1] / c[0] - 1
    hold_dd = ((c - np.maximum.accumulate(c)) / np.maximum.accumulate(c)).min()
    expo = float(np.mean(t))
    closed = [x for x in trades if not x["open"]]
    rets = [x["spx"] / x["bpx"] - 1 for x in closed]
    wins = sum(1 for r in rets if r > 0)
    print("[1] 主对照 (当日收盘成交, 滑点万10)")
    print("  " + fmt_row("N12簇策略", tot, ann(tot), mdd, sw,
                          "持仓占比 %.0f%%" % (expo * 100)))
    print("  " + fmt_row("买入持有", hold, ann(hold), hold_dd, 0, ""))
    print("  " + fmt_row("单(12,9)对照", *sim_single(c, SLIP)))
    print("-" * 78)

    # ---- [2] 敏感性 ----
    print("[2] 敏感性")
    print("  滑点 万5 : " + fmt_row("N12簇策略", tot1, ann(tot1), mdd1, sw1))
    print("  滑点 万10: " + fmt_row("N12簇策略", tot, ann(tot), mdd, sw))
    print("  滑点 万20: " + fmt_row("N12簇策略", tot2, ann(tot2), mdd2, sw2))
    eqo, toto, mddo, swo = sim_open_next(t, o, c, SLIP)
    print("  保守执行(T日信号,T+1开盘成交): 累计 %.1f%%  年化 %.2f%%  回撤 %.1f%%  切换 %d" %
          (toto * 100, ann(toto) * 100, mddo * 100, swo))
    print("-" * 78)

    # ---- [3] 逐年 ----
    print("[3] 逐年对照 (策略 vs 买入持有)")
    print("  %-6s %12s %12s %10s" % ("年份", "N12策略", "买入持有", "超额"))
    for y, rs, rh in yearly_table(d, eq, c):
        mark = "  ←策略占优" if rs > rh else ""
        print("  %-6d %11.1f%% %11.1f%% %+9.1f%%%s" % (y, rs * 100, rh * 100, (rs - rh) * 100, mark))
    print("-" * 78)

    # ---- [4] 交易明细 ----
    print("[4] 交易台账 (共 %d 笔平仓, 胜率 %.0f%%, 均值 %+.2f%%/笔)" %
          (len(closed), wins / len(closed) * 100 if closed else 0,
           float(np.mean(rets)) * 100 if rets else 0))
    print("  %-12s %10s  %-12s %10s %8s %8s" % ("买入日", "买价", "卖出日", "卖价", "收益", "持有天"))
    for x in trades[-args.recent:]:
        print("  %s  %10.3f  %s  %10.3f %+7.2f%% %7d%s" %
              (d[x["bi"]].date(), x["bpx"], d[x["si"]].date(), x["spx"],
               (x["spx"] / x["bpx"] - 1) * 100, x["si"] - x["bi"],
               "  (未平仓)" if x["open"] else ""))
    print("=" * 78)
    print("判读要点: ①若策略年化>持有且回撤更低 → N12结构对该标的适用;")
    print("          ②若只在强趋势年跑输(如近年白银牛市)但弱年回撤小 → 属防御型择时, 看你取舍;")
    print("          ③个股注意: 回测未含涨跌停/停牌/暴雷模拟, 强趋势段买入持有收益含幸存者效应。")


if __name__ == "__main__":
    main()

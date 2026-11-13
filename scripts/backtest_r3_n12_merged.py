#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 【R3 + 科创N12 合并回测 · 最近3年】
#
# R3  : 聚宽 canonical 交易(含滑点, 2023-11~2026-08) + 本地 SHADOW 实测(2026-08~11) 拼接
#       单笔全仓, 收益按持有天数均摊到日; 空仓现金按货基 1.8% 计息
# N12 : 588000 日线, N12 六簇 frac>0.5 持有 588000, 否则空仓货基; 收盘信号收盘成交滑点万10
# 组合: 日收益加权, 季度再平衡
#
# 用法: python3 scripts/backtest_r3_n12_merged.py
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_n12_stock import vote_target

ROT = Path("/Users/licheng/.tradingagents/rotation")
ETF_JSON = Path("/Users/licheng/.tradingagents/cache/idx_sh588000.json")
START = "2020-11-16"  # 588000 上市日(科创板50ETF成立)
MMF_APY = 0.018
SLIP = 0.001


def load_r3_trades():
    """聚宽交易 + SHADOW 实测, 返回 [(buy_date, sell_date, ret)]."""
    out = []
    jq = json.load(open(ROT / "r3_jq_backtest.json"))["trades"]
    for t in jq:
        sd, xd = t.get("signal_date"), t.get("sell_date")
        if sd and xd and sd >= START:
            out.append((sd, xd, t["return_pct"] / 100))
    shadow_start = max(x[1] for x in out) if out else START
    n_sh = 0
    for line in open(ROT / "r3_journal.jsonl"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if "sell_date" in r and "return_pct" in r and r["sell_date"] > shadow_start:
            out.append((r.get("signal_date") or r.get("buy_date"), r["sell_date"],
                        r["return_pct"] / 100))
            n_sh += 1
    return out, n_sh


def main():
    etf = pd.read_json(ETF_JSON)
    etf["date"] = pd.to_datetime(etf["date"])
    etf = etf[etf["date"] >= START].reset_index(drop=True)
    dates = etf["date"].to_numpy()
    close = etf["close"].to_numpy().astype(float)
    di = {pd.Timestamp(d).strftime("%Y-%m-%d"): i for i, d in enumerate(dates)}
    n_days = len(dates)
    mmf = (1 + MMF_APY) ** (1 / 252) - 1
    mmf_arr = np.full(n_days, mmf)
    mmf_arr[0] = 0.0

    # ---- R3 日收益 ----
    trades, n_sh = load_r3_trades()
    r3_ret = mmf_arr.copy()
    used = 0
    for bd, xd, r in trades:
        i0, i1 = di.get(bd), di.get(xd)
        if i0 is None or i1 is None or i1 <= i0:
            continue
        n = i1 - i0 + 1
        dr = (1 + r) ** (1 / n) - 1
        r3_ret[i0:i1 + 1] = dr
        used += 1

    # ---- N12 日收益 ----
    tgt, frac = vote_target(close)
    n12_ret = np.empty(n_days)
    pos = 0
    for i in range(n_days):
        r = close[i] / close[i - 1] - 1 if (i > 0 and pos) else 0.0
        t = int(tgt[i])
        cost = SLIP if t != pos else 0.0
        n12_ret[i] = r - cost + (mmf if pos == 0 else 0.0)
        pos = t

    # ---- 统计 ----
    yrs = (dates[-1] - dates[0]) / np.timedelta64(1, "D") / 365.25

    def line(tag, eq):
        total = eq[-1] / eq[0] - 1
        ann = (1 + total) ** (1 / yrs) - 1
        mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
        vol = np.std(np.diff(eq) / eq[:-1]) * np.sqrt(252)
        shp = (np.mean(np.diff(eq) / eq[:-1]) * 252) / vol if vol > 0 else 0
        print("  %-22s 累计 %+8.1f%%  年化 %+7.2f%%  MDD %7.2f%%  波动 %5.1f%%  夏普 %5.2f"
              % (tag, total * 100, ann * 100, mdd * 100, vol * 100, shp))
        return dict(tag=tag, total=float(total), ann=float(ann), mdd=float(mdd),
                    vol=float(vol), sharpe=float(shp), eq=eq)

    def combo(ws, rebal_q=True):
        q = pd.DatetimeIndex(dates).quarter.to_numpy()
        mask = np.zeros(n_days, bool)
        mask[:-1] = q[:-1] != q[1:]
        navs = np.array([1.0, 1.0, 1.0]) * np.array(ws)
        eq = np.empty(n_days)
        rets = np.vstack([r3_ret, n12_ret, mmf_arr])
        for t in range(n_days):
            navs *= (1 + rets[:, t])
            eq[t] = navs.sum()
            if rebal_q and mask[t]:
                navs = eq[t] * np.array(ws)
        return eq

    print("=" * 100)
    print("R3 + 科创N12 合并回测   %s ~ %s  (%.2f 年)" %
          (pd.Timestamp(dates[0]).date(), pd.Timestamp(dates[-1]).date(), yrs))
    print("R3 交易 %d 笔(聚宽 %d + SHADOW %d)   N12 持仓占比 %.0f%%" %
          (used, used - n_sh, n_sh, np.mean(tgt) * 100))
    print("=" * 100)

    # ---- 互斥切换: R3持仓期全仓R3, R3空仓时全仓N12 ----
    hold_r3 = np.zeros(n_days, bool)
    for bd, xd, r in trades:
        i0, i1 = di.get(bd), di.get(xd)
        if i0 is not None and i1 is not None:
            hold_r3[i0:i1 + 1] = True
    sw_ret = np.empty(n_days)
    n12_pos = 0
    for i in range(n_days):
        if hold_r3[i]:
            r = r3_ret[i]
            if n12_pos:          # 卖出 N12 换 R3
                r -= SLIP
                n12_pos = 0
            sw_ret[i] = r
        else:
            if n12_pos:
                base = close[i] / close[i - 1] - 1 if i > 0 else 0.0
                if int(tgt[i]) == 0:
                    sw_ret[i] = base - SLIP
                    n12_pos = 0
                else:
                    sw_ret[i] = base
            else:
                if int(tgt[i]) == 1:  # 买入 N12
                    sw_ret[i] = mmf - SLIP
                    n12_pos = 1
                else:
                    sw_ret[i] = mmf
    sw_eq = np.cumprod(1 + sw_ret)

    # ---- 反向: N12持仓时全仓N12, N12空仓(防御中)时走R3 ----
    # 关键: 用【昨日收盘】的N12信号决定今日持仓(无前视)
    sw2_ret = np.empty(n_days)
    n12_pos2 = 0
    r3_pos2 = 0
    for i in range(n_days):
        want_n12 = i > 0 and int(tgt[i - 1]) == 1
        if want_n12:                  # 今日按N12持仓 → 优先 N12
            r = 0.0
            if r3_pos2:
                r -= SLIP             # 卖出 R3
                r3_pos2 = 0
            if n12_pos2:
                r += close[i] / close[i - 1] - 1
            else:
                r += mmf - SLIP       # 昨日信号, 今日买入N12
                n12_pos2 = 1
            sw2_ret[i] = r
        else:                          # N12 空仓/防御 → 走 R3
            if n12_pos2:
                sw2_ret[i] = (close[i] / close[i - 1] - 1) - SLIP
                n12_pos2 = 0
                continue
            if hold_r3[i]:
                r3_pos2 = 1
                sw2_ret[i] = r3_ret[i]
            else:
                r3_pos2 = 0
                sw2_ret[i] = mmf
    sw2_eq = np.cumprod(1 + sw2_ret)

    lines = []
    lines.append(line("R3 单独(100%)", np.cumprod(1 + r3_ret)))
    lines.append(line("科创N12 单独(100%)", np.cumprod(1 + n12_ret)))
    lines.append(line("★互斥切换 R3→N12", sw_eq))
    lines.append(line("★反向 N12防御时走R3", sw2_eq))
    lines.append(line("50/50", combo((0.5, 0.5, 0.0))))
    lines.append(line("R3 20% + N12 40% + 现金40%", combo((0.2, 0.4, 0.4))))
    lines.append(line("R3 40% + N12 40% + 现金20%", combo((0.4, 0.4, 0.2))))
    print("  切换统计: R3持仓 %d 天(%.0f%%)  N12持仓 %d 天(%.0f%%)"
          % (hold_r3.sum(), hold_r3.mean() * 100,
             (np.array([1 if (not hold_r3[i] and int(tgt[i]) == 1) else 0
                        for i in range(n_days)])).sum(), 
             np.mean([1 if (not hold_r3[i] and int(tgt[i]) == 1) else 0
                      for i in range(n_days)]) * 100))

    # 相关性
    m = (np.abs(r3_ret - mmf) > 1e-9) | (np.abs(n12_ret - mmf) > 1e-9)
    print("\n相关性: R3 vs N12 日收益 corr = %.2f  (仅交易日 %d 天)"
          % (np.corrcoef(r3_ret[m], n12_ret[m])[0, 1], m.sum()))

    # 逐年
    print("\n逐年: %-6s %10s %10s %10s %10s" % ("年份", "R3", "N12", "50/50", "20/40/40"))
    q = pd.DatetimeIndex(dates).quarter.to_numpy()
    mask = np.zeros(n_days, bool)
    mask[:-1] = q[:-1] != q[1:]
    c20 = combo((0.2, 0.4, 0.4))
    ymap = {}
    for name, eq in (("r3", np.cumprod(1 + r3_ret)), ("n12", np.cumprod(1 + n12_ret)),
                     ("c50", combo((0.5, 0.5, 0.0))), ("c20", c20)):
        de = pd.Series(eq, index=pd.DatetimeIndex(dates)).resample("YE").last()
        prev = eq[0]
        for d_, v in zip(de.index, de):
            ymap.setdefault(d_.year, {})[name] = v / prev - 1
            prev = v
    for y in sorted(ymap):
        r = ymap[y]
        print("  %-6d %9.1f%% %9.1f%% %9.1f%% %9.1f%%"
              % (y, r["r3"] * 100, r["n12"] * 100, r["c50"] * 100, r["c20"] * 100))

    print("\n" + "-" * 100)
    print("注: R3 为聚宽 canonical(含默认滑点)+SHADOW实测 拼接; N12 为 588000 日线簇择时(空仓货基1.8%)。")
    print("    两者相关性低 → 合并后波动/回撤应显著低于单策略, 这是合并的主要意义。")


if __name__ == "__main__":
    main()

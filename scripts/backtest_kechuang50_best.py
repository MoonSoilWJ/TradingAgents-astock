"""科创50(588000) 单标的择时策略全历史对照。
核心思路: 满仓/空仓切换(不参与网格分批), 目标=熊市空仓+牛市满仓。
口径: 当日收盘算信号、当日收盘成交(与网格回测一致); ETF佣金万1, 免印花税。
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime

import akshare as ak
import pandas as pd

FEE_PCT = 0.01
INIT_CASH = 100_000.0


def fetch_daily(code: str) -> pd.DataFrame:
    df = ak.fund_etf_hist_sina(symbol=("sh" if code[0] in "56" else "sz") + code)
    df = df[["date", "open", "high", "low", "close"]].copy()
    df["date"] = df["date"].astype(str).str[:10]
    return df.sort_values("date").reset_index(drop=True)


def run_timing(closes, dates, pos) -> dict:
    """pos: 与 closes 等长的 0/1 列表(当日是否满仓)。当日收盘调仓。"""
    n = len(closes)
    cash, shares = INIT_CASH, 0.0
    ec, acts = [], []
    for i in range(n):
        price = closes[i]
        tgt = pos[i]
        if tgt == 1 and shares == 0:
            invest = cash
            fee = invest * FEE_PCT / 100
            shares = invest / price
            cash -= invest + fee
            acts.append({"day": dates[i], "act": "BUY", "price": round(price, 4),
                         "cash": round(cash, 2)})
        elif tgt == 0 and shares > 0:
            proceeds = shares * price
            fee = proceeds * FEE_PCT / 100
            cash += proceeds - fee
            shares = 0
            acts.append({"day": dates[i], "act": "SELL", "price": round(price, 4),
                         "cash": round(cash, 2)})
        ec.append(cash + shares * price)
    final = ec[-1]
    # 年化(按自然年数)
    yrs = (pd.to_datetime(dates[-1]) - pd.to_datetime(dates[0])).days / 365.25
    cagr = (final / INIT_CASH) ** (1 / yrs) - 1 if yrs > 0 else 0
    peak = ec[0]; mdd = 0.0
    for v in ec:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"final_pct": (final / INIT_CASH - 1) * 100, "cagr": cagr * 100,
            "mdd": mdd * 100, "trades": len(acts), "equity": ec,
            "n_buy": sum(1 for a in acts if a["act"] == "BUY")}


def max_drawdown(ec):
    peak = ec[0]; mdd = 0.0
    for v in ec:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    return mdd * 100


def buy_hold(closes):
    return (closes[-1] / closes[0] - 1) * 100


def ma_pos(closes, n):
    """close > MA(n) 持多, 否则空仓(滚动均值, 无前视)。"""
    s = pd.Series(closes)
    ma = s.rolling(n).mean()
    return [1 if (not pd.isna(ma[i]) and closes[i] > ma[i]) else 0 for i in range(len(closes))]


def dual_pos(closes, sh, lg):
    s = pd.Series(closes)
    ma_s = s.rolling(sh).mean(); ma_l = s.rolling(lg).mean()
    out = []
    for i in range(len(closes)):
        if pd.isna(ma_s[i]) or pd.isna(ma_l[i]):
            out.append(0)
        else:
            out.append(1 if ma_s[i] > ma_l[i] else 0)
    return out


def mom_pos(closes, look):
    """过去 look 日收益 > 0 持多。"""
    return [1 if (i >= look and closes[i] > closes[i - look]) else 0
            for i in range(len(closes))]


def dual_filter_pos(closes, look=120, ma=200):
    """动量(look日)为正 且 close>MA(ma) 才满仓, 否则空仓。
    双过滤: 动量捕捉趋势方向, MA200 过滤'动量刚转正但仍在长均线下'的假突破。
    """
    s = pd.Series(closes)
    ma_s = s.rolling(ma).mean()
    out = []
    for i in range(len(closes)):
        if i < max(look, ma - 1):
            out.append(0)
        else:
            mom_up = closes[i] > closes[i - look]
            above_ma = (not pd.isna(ma_s[i])) and closes[i] > ma_s[i]
            out.append(1 if (mom_up and above_ma) else 0)
    return out


def weekly_mom_pos(closes, dates):
    """周线级别: 本周收盘 > 上周收盘 持多(用每周最后交易日判定)。"""
    wk = pd.Series(dates).str[:7]  # 年月
    out = [0] * len(closes)
    last_close_in_month = {}
    for i, (d, c) in enumerate(zip(dates, closes)):
        ym = d[:7]
        last_close_in_month[ym] = (i, c)
    prev = None
    for ym, (i, c) in last_close_in_month.items():
        if prev is not None and c > prev:
            out[i] = 1
        prev = c
    # 持仓延续: 一旦周线看多, 持续持有直到下个判定日翻空
    # 简化: 在每个判定日设置目标, 之间沿用上一判定
    filled = []
    cur = 0
    for i in range(len(closes)):
        if out[i] == 1:
            cur = 1
        elif i in {v[0] for v in last_close_in_month.values()} and out[i] == 0:
            cur = 0
        filled.append(cur)
    return filled


def main():
    ap = argparse.ArgumentParser(description="科创50 单标的择时最佳策略探索")
    ap.add_argument("--code", default="588000")
    args = ap.parse_args()

    df = fetch_daily(args.code)
    closes = df["close"].tolist()
    dates = df["date"].tolist()
    bh = buy_hold(closes)
    print("=" * 72)
    print(f"  标的 {args.code} | 全历史 {dates[0]} ~ {dates[-1]} ({len(closes)}交易日)")
    print("=" * 72)
    print(f"\n  买入持有基准: {bh:+.2f}%  年化≈{( (closes[-1]/closes[0])**(365.25/((pd.to_datetime(dates[-1])-pd.to_datetime(dates[0])).days) )-1)*100:+.2f}%")

    strategies = []
    strategies.append(("MA60", ma_pos(closes, 60)))
    strategies.append(("MA120", ma_pos(closes, 120)))
    strategies.append(("MA200", ma_pos(closes, 200)))
    strategies.append(("双均20/60", dual_pos(closes, 20, 60)))
    strategies.append(("双均20/120", dual_pos(closes, 20, 120)))
    strategies.append(("动量20日", mom_pos(closes, 20)))
    strategies.append(("动量60日", mom_pos(closes, 60)))
    strategies.append(("动量120日", mom_pos(closes, 120)))
    strategies.append(("动量120+MA200", dual_filter_pos(closes, 120, 200)))
    strategies.append(("月线动量", weekly_mom_pos(closes, dates)))

    print(f"\n  {'策略':>10} {'收益%':>9} {'年化%':>8} {'回撤%':>8} {'交易次':>6} {'vsBH':>8}")
    print("  " + "-" * 54)
    results = []
    for name, pos in strategies:
        r = run_timing(closes, dates, pos)
        results.append((name, r))
        print(f"  {name:>10} {r['final_pct']:>+8.2f} {r['cagr']:>+7.2f} "
              f"{r['mdd']:>+7.2f} {r['trades']:>6} {r['final_pct']-bh:>+7.2f}")

    # 分年度(对最优的几个策略)
    print(f"\n  分年度(每年独立10万):")
    show = ["MA120", "双均20/120", "动量120日", "动量120+MA200", "月线动量"]
    print(f"    {'年':>5} {'BH%':>8} " + " ".join(f"{s:>9}" for s in show))
    by_year = {}
    for y in sorted(set(d[:4] for d in dates)):
        idx = [i for i, d in enumerate(dates) if d[:4] == y]
        if len(idx) < 2:
            continue
        sub_c = [closes[i] for i in idx]
        sub_d = [dates[i] for i in idx]
        bh_y = buy_hold(sub_c)
        row = [f"{y:>5} {bh_y:>+7.2f}"]
        for s in show:
            pos_full = strategies[[x[0] for x in strategies].index(s)][1]
            pos_sub = [pos_full[i] for i in idx]
            ry = run_timing(sub_c, sub_d, pos_sub)["final_pct"]
            row.append(f"{ry:>+9.2f}")
        print("    " + " ".join(row))

    best = max(results, key=lambda x: x[1]["final_pct"])
    print(f"\n  ★ 绝对收益最佳: {best[0]} {best[1]['final_pct']:+.2f}% (BH {bh:+.2f}%, "
          f"回撤 {best[1]['mdd']:+.2f}%, 交易 {best[1]['trades']}次)")

    out = (Path.home() / ".tradingagents" / "rotation" /
           f"kc50_best_{datetime.now():%Y%m%d_%H%M}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "code": args.code, "buy_hold_pct": bh,
        "strategies": {n: {"final_pct": r["final_pct"], "cagr": r["cagr"],
                           "mdd": r["mdd"], "trades": r["trades"]}
                       for n, r in results},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  结果已保存: {out}")


if __name__ == "__main__":
    main()

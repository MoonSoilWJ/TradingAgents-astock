#!/usr/bin/env python3
"""科创50(588000) 阶梯网格回测 — 跌买涨卖(加仓/减半)。

策略逻辑(日线收盘, T+1 现实可行):
  设 anchor = 参考价(首日为窗口首日收盘, 每次操作后重置为成交价)。
  每天收盘比较 今收盘 vs anchor:
    - 收盘 <= anchor*(1-跌阈%) 且仍有现金 -> 买入一个单位(=初始资金 unit_pct)
        anchor = 今收盘 (下一买需再跌跌阈%)
    - 收盘 >= anchor*(1+涨阈%) 且持筹    -> 卖出当前持仓的 sell_frac(默认一半)
        anchor = 今收盘 (下一卖需再涨涨阈%)
  "买一倍"=每次买固定单位(unit_pct 比例); "卖一半"=卖当前持仓50%。
  现金用尽即停止买入(封顶, 不实杠杆/martingale)。

用法:
    python scripts/backtest_kechuang50_reversal.py                 # 默认 588000 近2月 跌10/涨10
    python scripts/backtest_kechuang50_reversal.py --drop 10 --rise 10 --unit-pct 0.1
    python scripts/backtest_kechuang50_reversal.py --code 588080 --months 6
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd

FEE_PCT = 0.01  # 单边佣金(万1), ETF 免印花税
INIT_CASH = 100_000.0


def fetch_daily(code: str, months: float = 2.0, all_history: bool = False) -> pd.DataFrame:
    df = ak.fund_etf_hist_sina(symbol=("sh" if code[0] in "56" else "sz") + code)
    df = df[["date", "open", "high", "low", "close"]].copy()
    df["date"] = df["date"].astype(str).str[:10]
    df = df.sort_values("date").reset_index(drop=True)
    if all_history:
        return df
    keep = int(months * 21 * 1.6)
    return df.tail(max(keep, 30)).reset_index(drop=True)


def run_grid(df: pd.DataFrame, drop_pct: float, rise_pct: float,
             unit_pct: float, sell_frac: float, fee_pct: float,
             enter: bool = True, mode: str = "reversal",
             stop_pct: float = 10.0) -> dict:
    """mode='reversal' : 跌阈值买一倍 / 涨阈值卖一半 (逆向/网格)
       mode='momentum': 涨阈值买一倍 / 跌阈值卖一半 (顺势/动量金字塔)
       mode='trend'    : 涨阈值加仓一倍 / 持仓从峰值回落stop%则清仓 (小仓趋势)
       mode='trend_full': 满仓持有 / 回落stop%清仓 / 再涨rise%满仓重入 (满仓趋势跟踪)
    """
    closes = df["close"].tolist()
    dates = df["date"].tolist()
    n = len(closes)
    cash = INIT_CASH
    lots: list[tuple[float, float]] = []   # (shares, buy_price)
    anchor = closes[0]
    peak_hold = 0.0    # 持仓市值峰值(移动止损用)
    actions: list[dict] = []
    equity_curve: list[float] = []
    full = (mode == "trend_full")

    # 建底仓: trend_full 满仓, 其余每倍=unit_pct; --no-enter 则等首次触发
    if enter:
        invest = cash if full else INIT_CASH * unit_pct
        fee = invest * fee_pct / 100
        shares = invest / closes[0]
        cash -= invest + fee
        lots.append((shares, closes[0]))
        anchor = closes[0]
        peak_hold = shares * closes[0]
        actions.append({"day": dates[0], "act": "BUY", "price": round(closes[0], 4),
                        "shares": round(shares, 2), "invest": round(invest, 2),
                        "cash": round(cash, 2), "note": "满仓" if full else "底仓"})
        equity_curve.append(cash + sum(sh for sh, _p in lots) * closes[0])

    for i in range(1, n):
        price = closes[i]
        day = dates[i]
        hold_val = sum(s for s, _ in lots) * price
        if lots:
            peak_hold = max(peak_hold, hold_val)

        if mode == "reversal":
            buy_trig = price <= anchor * (1 - drop_pct / 100)
            sell_trig = price >= anchor * (1 + rise_pct / 100)
        elif mode == "momentum":
            buy_trig = price >= anchor * (1 + rise_pct / 100)
            sell_trig = price <= anchor * (1 - drop_pct / 100)
        elif mode == "trend_full":
            buy_trig = (not lots) and price >= anchor * (1 + rise_pct / 100)
            sell_trig = False
        else:  # trend: 涨加仓, 回落清仓
            buy_trig = price >= anchor * (1 + rise_pct / 100)
            sell_trig = False

        if buy_trig and cash > 1.0:
            invest = cash if full else min(INIT_CASH * unit_pct, cash)
            fee = invest * fee_pct / 100
            shares = invest / price
            cash -= invest + fee
            lots.append((shares, price))
            anchor = price
            actions.append({"day": day, "act": "BUY", "price": round(price, 4),
                            "shares": round(shares, 2), "invest": round(invest, 2),
                            "cash": round(cash, 2),
                            "note": "满仓重入" if full else ""})
        elif lots and (
            (mode in ("reversal", "momentum") and sell_trig)
            or (mode in ("trend", "trend_full") and hold_val <= peak_hold * (1 - stop_pct / 100))
        ):
            if mode in ("trend", "trend_full"):
                # 移动止损: 清仓全部
                total_shares = sum(s for s, _ in lots)
                proceeds = total_shares * price
                fee = proceeds * fee_pct / 100
                cash += proceeds - fee
                lots = []
                peak_hold = 0.0
                anchor = price
                actions.append({"day": day, "act": "SELL", "price": round(price, 4),
                                "shares": round(total_shares, 2), "note": "移动止损清仓",
                                "proceeds": round(proceeds, 2), "cash": round(cash, 2)})
            else:
                # 卖一半
                total_shares = sum(s for s, _ in lots)
                sell_shares = total_shares * sell_frac
                proceeds = sell_shares * price
                fee = proceeds * fee_pct / 100
                cash += proceeds - fee
                remaining = sell_shares
                new_lots: list[tuple[float, float]] = []
                for s, p in lots:
                    if remaining <= 1e-9:
                        new_lots.append((s, p))
                    elif s <= remaining:
                        remaining -= s
                    else:
                        new_lots.append((s - remaining, p))
                        remaining = 0
                lots = new_lots
                anchor = price
                actions.append({"day": day, "act": "SELL", "price": round(price, 4),
                                "shares": round(sell_shares, 2),
                                "proceeds": round(proceeds, 2), "cash": round(cash, 2)})

        shares_val = sum(s for s, _ in lots) * price   # 按当日收盘市价估值
        equity_curve.append(cash + shares_val)

    final_shares = sum(s for s, _ in lots)
    final_equity = cash + final_shares * closes[-1]
    return {
        "actions": actions,
        "final_equity": final_equity,
        "final_equity_pct": (final_equity / INIT_CASH - 1) * 100,
        "final_shares": final_shares,
        "final_cash": cash,
        "equity_curve": equity_curve,
        "n_lots": len(lots),
    }


def buy_hold(df: pd.DataFrame) -> float:
    c = df["close"].tolist()
    if len(c) < 2:
        return 0.0
    return (c[-1] / c[0] - 1) * 100


def max_drawdown(equity_curve: list[float]) -> float:
    peak = equity_curve[0]
    mdd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd * 100


def main():
    ap = argparse.ArgumentParser(description="科创50 阶梯网格/动量 回测")
    ap.add_argument("--code", default="588000")
    ap.add_argument("--months", type=float, default=2.0)
    ap.add_argument("--all", action="store_true", help="全历史(上市第一天起)")
    ap.add_argument("--mode", default="reversal",
                    choices=["reversal", "momentum", "trend", "trend_full"],
                    help="reversal=跌买涨卖 / momentum=涨买跌卖 / trend=小仓涨加仓+回落清仓 / trend_full=满仓持有+回落清仓重入")
    ap.add_argument("--drop", type=float, default=10.0, help="下跌阈值%")
    ap.add_argument("--rise", type=float, default=10.0, help="上涨阈值%(加仓/重入步长)")
    ap.add_argument("--unit-pct", type=float, default=0.1,
                    help="每倍买入占初始资金比例(满仓模式忽略)")
    ap.add_argument("--sell-frac", type=float, default=0.5,
                    help="reversal/momentum 触发时卖出持仓比例")
    ap.add_argument("--stop", type=float, default=10.0,
                    help="trend/trend_full 移动止损: 持仓从峰值回落%清仓")
    ap.add_argument("--no-enter", action="store_true",
                    help="不在首日建底仓(等首次触发才建仓)")
    ap.add_argument("--fee", type=float, default=FEE_PCT)
    args = ap.parse_args()

    df = fetch_daily(args.code, args.months, all_history=args.all)
    if args.all:
        dfw = df.reset_index(drop=True)
    else:
        window = int(round(args.months * 21))
        dfw = df.tail(window).reset_index(drop=True)
    start, end = dfw["date"].iloc[0], dfw["date"].iloc[-1]
    mode_cn = {"reversal": "逆向网格(跌买涨卖)",
               "momentum": "顺势动量(涨买跌卖)",
               "trend": f"小仓趋势(涨加仓+回落{args.stop:.0f}%清仓)",
               "trend_full": f"满仓趋势(持有+回落{args.stop:.0f}%清仓重入)"}[args.mode]

    print("=" * 68)
    print(f"  标的: {args.code} | 窗口: {start} ~ {end} ({len(dfw)}交易日)")
    print(f"  模式: {mode_cn} | 步长 跌{args.drop}%/涨{args.rise}% | "
          f"每倍={args.unit_pct*100:.0f}%资金 卖{args.sell_frac*100:.0f}% | 佣金{args.fee}‰")
    print("=" * 68)

    res = run_grid(dfw, args.drop, args.rise, args.unit_pct,
                   args.sell_frac, args.fee, enter=not args.no_enter,
                   mode=args.mode, stop_pct=args.stop)
    bh = buy_hold(dfw)

    print(f"\n  [策略] 累计收益: {res['final_equity_pct']:+.2f}%")
    print(f"  期末权益: {res['final_equity']:,.0f} (现金{res['final_cash']:,.0f} + "
          f"持仓{res['final_shares']:.1f}股)")
    print(f"  最大回撤: {max_drawdown(res['equity_curve']):+.2f}%")
    print(f"  买卖次数: {len(res['actions'])} (买{sum(1 for a in res['actions'] if a['act']=='BUY')} / "
          f"卖{sum(1 for a in res['actions'] if a['act']=='SELL')})")
    print(f"\n  [买入持有] 累计收益: {bh:+.2f}%")
    print(f"  策略超额: {res['final_equity_pct'] - bh:+.2f}pp")

    print(f"\n  操作记录(前20条):")
    for a in res["actions"][:20]:
        extra = f" 余现{a['cash']:>10,.0f}"
        if a["act"] == "BUY":
            print(f"    {a['day']} 买 {a['price']:>7}  股{a['shares']:>9.1f}  "
                  f"投入{a['invest']:>10,.0f}{extra}")
        else:
            tag = f"({a.get('note','')})" if a.get("note") else ""
            print(f"    {a['day']} 卖 {a['price']:>7}  股{a['shares']:>9.1f}  "
                  f"回收{a['proceeds']:>10,.0f}{extra} {tag}")
    if len(res["actions"]) > 20:
        print(f"    ... 共 {len(res['actions'])} 条")

    # 分年度拆解
    if args.all:
        print(f"\n  分年度(每年独立10万本金, 同参数):")
        print(f"    {'年份':>6} {'交易日':>6} {'策略%':>9} {'BH%':>9} {'超额':>9} {'回撤':>8}")
        print("    " + "-" * 50)
        for y, g in dfw.groupby(dfw["date"].str[:4]):
            gw = g.reset_index(drop=True)
            if len(gw) < 2:
                continue
            ry = run_grid(gw, args.drop, args.rise, args.unit_pct,
                          args.sell_frac, args.fee, enter=not args.no_enter,
                          mode=args.mode, stop_pct=args.stop)
            by = buy_hold(gw)
            print(f"    {y:>6} {len(gw):>6} {ry['final_equity_pct']:>+8.2f} "
                  f"{by:>+8.2f} {ry['final_equity_pct']-by:>+8.2f} "
                  f"{max_drawdown(ry['equity_curve']):>+7.2f}")

    out = (Path.home() / ".tradingagents" / "rotation" /
           f"kc50_{args.mode}_{datetime.now():%Y%m%d_%H%M}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "code": args.code, "mode": args.mode, "start": start, "end": end,
        "drop": args.drop, "rise": args.rise, "unit_pct": args.unit_pct,
        "sell_frac": args.sell_frac, "stop_pct": args.stop,
        "final_equity_pct": res["final_equity_pct"], "buy_hold_pct": bh,
        "max_drawdown_pct": max_drawdown(res["equity_curve"]),
        "actions": res["actions"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  结果已保存: {out}")


if __name__ == "__main__":
    main()

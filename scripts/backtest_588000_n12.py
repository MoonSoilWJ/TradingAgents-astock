# 计算 588000(科创50ETF) 日线 N12 结果簇 投票策略 的回测 + 当日信号,
# 输出 JSON 供 export_to_web.py 上站, 同时刷新 results/vote_ratio_n12_588000.csv。
#
# 与 scripts/compare_hybrid.py 保持同一套 sim 逻辑 (信号当日收盘同价成交, 滑点 0.05%),
# 所以网页展示的累计收益与之前验证的 N12簇 +271.4% 口径一致。
import json, os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams

SLIP = 0.0005
COMB_N12 = [(10, 9), (10, 12), (12, 9), (12, 12), (14, 9), (14, 12)]  # N12 结果簇

OUT_JSON = Path.home() / ".tradingagents" / "rotation" / "star50_n12_ensemble.json"
VOTE_CSV = Path(__file__).resolve().parent.parent / "results" / "vote_ratio_n12_588000.csv"


def trix_series(c, N, M):
    s = pd.Series(c, dtype=float)
    e1 = s.ewm(span=N, adjust=False).mean()
    e2 = e1.ewm(span=N, adjust=False).mean()
    e3 = e2.ewm(span=N, adjust=False).mean()
    tr = e3.pct_change() * 100
    sig = tr.rolling(M).mean()
    return tr.values, sig.values


def vote_from(c, combos, thr=0.5):
    trs, sigs = [], []
    for n, m in combos:
        tr, sig = trix_series(c, n, m)
        trs.append(tr)
        sigs.append(sig)
    pos = (np.array(trs) > np.array(sigs)).astype(int)
    frac = pos.mean(0)  # 每日看多占比
    return (frac > thr).astype(int), frac


def sim(target, price):
    """复刻 compare_hybrid.sim: 信号当日收盘同价成交, 滑点 SLIP。返回 (收益序列, 末益, 最大回撤, 切换数)。"""
    cash, units, pos, eq, sw, prev = 1.0, 0.0, 0, [], 0, 0
    for i in range(len(price)):
        t = int(target[i]); nd = price[i]
        if t != prev:
            sw += 1
            if t == 1 and pos == 0:
                fee = cash * SLIP; units = (cash - fee) / nd; cash = 0.0; pos = 1
            elif t == 0 and pos == 1:
                amt = units * nd; fee = amt * SLIP; cash = amt - fee; units = 0.0; pos = 0
        prev = t; eq.append(cash + units * nd)
    eq = np.array(eq)
    total = eq[-1] / eq[0] - 1
    mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    return eq, total, mdd, sw


def fetch_day(start_date="2021-01-01"):
    api = TdxHq_API()
    api.connect("180.153.18.170", 7709, time_out=5)
    frames = []
    for pg in range(20):
        k = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, TDXParams.MARKET_SH, b"588000", pg * 700, 700)
        if k is None:
            break
        d = api.to_df(k)
        if d is None or len(d) == 0:
            break
        frames.append(d)
        if len(d) < 700:
            break
    api.disconnect()
    f = pd.concat(frames, ignore_index=True)
    f["date"] = pd.to_datetime(f["datetime"]).dt.normalize()
    f = f[f["date"] >= pd.Timestamp(start_date)]
    return f.sort_values("date").reset_index(drop=True)


def main():
    f = fetch_day("2021-01-01")
    close = f["close"].values.astype(float)
    dates = pd.to_datetime(f["date"].values)
    n = len(close)

    pos, frac = vote_from(close, COMB_N12, thr=0.5)
    eq, total, mdd, sw = sim(pos, close)

    # 净值曲线 [[ms, 累计%]]
    curve = []
    for i in range(n):
        try:
            ts = int(dates[i].timestamp() * 1000)
        except Exception:
            continue
        curve.append([ts, (eq[i] - 1) * 100])

    # 逐笔交易 (0->1 开, 1->0 平), 净收益含滑点
    trades = []
    entry = None
    for i in range(n):
        if pos[i] == 1 and entry is None:
            entry = i
        elif pos[i] == 0 and entry is not None:
            pe = close[entry] * (1 + SLIP)
            px = close[i] * (1 - SLIP)
            ret = px / pe - 1
            trades.append({
                "status": "closed",
                "signalDate": dates[entry].strftime("%Y-%m-%d"),
                "buyDate": dates[entry].strftime("%Y-%m-%d"),
                "sellDate": dates[i].strftime("%Y-%m-%d"),
                "etf": "588000",
                "name": "科创50ETF",
                "buyPrice": round(float(close[entry]), 4),
                "sellPrice": round(float(close[i]), 4),
                "returnPct": round(ret * 100, 2),
                "sellReason": "TRIX死叉(簇多数翻空)",
                "note": "回测",
            })
            entry = None
    if entry is not None:  # 持仓中
        trades.append({
            "status": "open",
            "signalDate": dates[entry].strftime("%Y-%m-%d"),
            "buyDate": dates[entry].strftime("%Y-%m-%d"),
            "sellDate": None,
            "etf": "588000",
            "name": "科创50ETF",
            "buyPrice": round(float(close[entry]), 4),
            "sellPrice": None,
            "returnPct": None,
            "sellReason": None,
            "note": "持仓中",
        })
    closed = [t for t in trades if t["status"] == "closed"]
    wins = sum(1 for t in closed if (t["returnPct"] or 0) > 0)
    win_rate = (wins / len(closed) * 100) if closed else 0.0

    days = n
    years = days / 365.0
    annual = ((1 + total) ** (1 / max(years, 1e-9)) - 1) * 100 if total > -1 else -100.0

    # 当日信号 / 投票率
    live_ratio = float(frac[-1])
    live_pos = int(pos[-1])
    last_ret = closed[0]["returnPct"] if closed else 0.0  # 最近平仓笔收益

    payload = {
        "id": "star50_n12_ensemble",
        "window": f"{dates[0].strftime('%Y-%m-%d')}~{dates[-1].strftime('%Y-%m-%d')}",
        "startDate": dates[0].strftime("%Y-%m-%d"),
        "endDate": dates[-1].strftime("%Y-%m-%d"),
        "trading_days": days,
        "combos": COMB_N12,
        "stats": {
            "equity_pct": round(total * 100, 2),
            "annualReturn": round(annual, 2),
            "max_drawdown": round(abs(mdd) * 100, 2),
            "win_rate": round(win_rate, 1),
            "trades": len(closed),
            "switches": int(sw),
        },
        "equity_curve": curve,
        "trades": trades,
        "live": {
            "date": dates[-1].strftime("%Y-%m-%d"),
            "long_ratio": round(live_ratio, 4),
            "position": live_pos,   # 1=持仓, 0=空仓
            "lastReturn": last_ret,
        },
    }

    os.makedirs(OUT_JSON.parent, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 刷新投票率 CSV
    vote_df = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "long_ratio": frac,
    })
    os.makedirs(VOTE_CSV.parent, exist_ok=True)
    vote_df.to_csv(VOTE_CSV, index=False)

    print(f"已写入: {OUT_JSON}")
    print(f"已刷新: {VOTE_CSV}")
    print(f"区间: {payload['window']}  交易日: {days}")
    print(f"累计: {total*100:.1f}%  年化: {annual:.1f}%  最大回撤: {abs(mdd)*100:.1f}%")
    print(f"交易笔数: {len(closed)}  胜率: {win_rate:.1f}%  切换: {sw}")
    print(f"今日({dates[-1].date()}) 看多占比: {live_ratio:.3f}  持仓: {live_pos}")


if __name__ == "__main__":
    main()

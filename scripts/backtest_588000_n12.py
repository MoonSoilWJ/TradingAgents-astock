# 计算 588000(科创50ETF) 日线 N12 结果簇 投票策略 + 防御组轮动叠加层,
# 输出 JSON 供 export_to_web.py 上站, 同时刷新 results/vote_ratio_n12_588000.csv。
#
# 2026-08 重构: 主策略由「纯 N12 持币」升级为「N12 + 防御组轮动」:
#   核心 588000 仍用 N12 投票(不变); 仅当 N12 空仓 且 588000(=科创50)TRIX 死叉时,
#   在【低相关防御组】(国债/黄金/红利)中挑「仍金叉 且 20 日动量最强」的一只单标的持有,
#   全死叉或 588000 金叉则保守持国债。目的: 把 N12 空仓期 ~57% 的闲置资金利用起来,
#   且不与成长 beta 同跌, 同时每次只持 1 只便于手动跟车。
#   原纯 N12 结果保留在 payload["n12_baseline"] 供对比, 不再作为主策略。
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

# 防御组: 与科创50(588000)低相关/负相关的资产 (国债/黄金/红利)
BOND = "511260"
DEF = ["511260", "518880", "510880", "515080", "512890"]
NAMES = {"588000": "科创50ETF", "511260": "国债ETF", "518880": "黄金ETF",
         "510880": "红利ETF", "515080": "中证红利ETF", "512890": "红利低波ETF"}


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


def trix_cross(c, N=14, M=9):
    tr, sig = trix_series(np.asarray(c, float), N, M)
    return (tr > sig).astype(int)


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


def sim_mixed(hold_spec, closes, slip):
    """多资产轮换回测: hold_spec[i] = {code: weight}。返回 (eq, total, mdd, idle_days, switches)。"""
    n = len(hold_spec)
    eq = 1.0; eqs = []; prev = None; idle = 0; sw = 0
    for i in range(n):
        w = hold_spec[i]
        if prev is None:
            cost = sum(slip * v for v in w.values()) if w else 0.0
            eq *= (1 - cost)
        else:
            turn = 0.0
            for c in set(w) | set(prev):
                turn += abs(w.get(c, 0.0) - prev.get(c, 0.0))
            ret = sum(v * (closes[c][i] / closes[c][i - 1] - 1) for c, v in w.items())
            eq *= (1 + ret) * (1 - slip * turn)
            if w != prev:
                sw += 1
        prev = w
        idle += (0 if w else 1)
        eqs.append(eq)
    eq = np.array(eqs)
    total = eq[-1] / eq[0] - 1
    mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    return eq, total, mdd, idle, sw


def _market_of(code):
    c = code.strip()
    return TDXParams.MARKET_SH if (c and c[0] in "569") else TDXParams.MARKET_SZ


def fetch_day_code(code, start_date="2020-11-16", pages=20):
    market = _market_of(code)
    api = TdxHq_API()
    api.connect("180.153.18.170", 7709, time_out=5)
    frames = []
    for pg in range(pages):
        k = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, market, code.encode(), pg * 700, 700)
        if k is None:
            break
        d = api.to_df(k)
        if d is None or len(d) == 0:
            break
        frames.append(d)
        if len(d) < 700:
            break
    api.disconnect()
    if not frames:
        return None
    f = pd.concat(frames, ignore_index=True)
    f["date"] = pd.to_datetime(f["datetime"]).dt.normalize()
    f = f[f["date"] >= pd.Timestamp(start_date)]
    f = f.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    return f.set_index("date")["close"].astype(float)


def fetch_day(start_date="2020-11-16"):
    s = fetch_day_code("588000", start_date)
    if s is None:
        raise RuntimeError("588000 行情拉取失败")
    return s.reset_index()


def _years(dates_index):
    """回测年数: 用【自然日跨度】计算。

    ⚠️ 2026-09 修复: 原实现写 d_n/365.0, 其中 d_n 是【交易日数】(如 1407),
    除以 365 得到 3.85 年, 而实际跨度是 5.79 年 → 年化被系统性放大约 1.6 倍
    (实测: 累计 655.8% 时, 错算年化 69.0%, 正确值约 42.0%)。
    """
    span = (dates_index[-1] - dates_index[0]).days
    return max(span / 365.25, 1e-9)


def _momentum(closes, win=20):
    """滚动 window 日收益率, 用于防御日挑选'最强动量'单标的。前 win 天为 NaN。"""
    n = len(closes)
    m = np.full(n, np.nan)
    if n > win:
        m[win:] = closes[win:] / closes[:-win] - 1
    return m


def build_defensive_rotation(df_close):
    """给定对齐后的收盘价 DataFrame(含 588000 与防御组), 返回防御组轮动策略的持仓序列与诊断。

    防御腿规则(V3 单标的动量最强):
      N12 持仓 588000; 闲置期且 588000(=科创50)死叉时, 在【国债/黄金/红利】中挑
      '仍金叉 且 20 日动量最强' 的一只单标的持有(非等权); 全死叉 或 588000 金叉则保守持国债。
    """
    kc = df_close["588000"].values.astype(float)
    core, frac = vote_from(kc, COMB_N12, thr=0.5)          # N12 核心(不变)
    kc_cross = trix_cross(kc)                              # 科创50(=588000) TRIX 金叉状态
    def_cross = {c: trix_cross(df_close[c].values.astype(float)) for c in DEF}
    mom = {c: _momentum(df_close[c].values.astype(float), 20) for c in DEF}

    hold_spec = []
    for i in range(len(kc)):
        if core[i] == 1:                                  # 核心开仓: 持 588000
            hold_spec.append({"588000": 1.0})
        else:                                              # N12 空仓(闲置期)
            if kc_cross[i] == 0:                          # 588000 死叉 -> 挑单只动量最强金叉防御标的
                g = [c for c in DEF if def_cross[c][i] == 1]
                if g:
                    best = max(g, key=lambda c: mom[c][i] if not np.isnan(mom[c][i]) else -1e9)
                    hold_spec.append({best: 1.0})
                else:                                      # 防御组全死叉 -> 国债
                    hold_spec.append({BOND: 1.0})
            else:                                          # 588000 金叉但 N12 未喊多 -> 保守持国债
                hold_spec.append({BOND: 1.0})
    return core, kc_cross, def_cross, hold_spec, frac


def build_rotation_trades(hold_spec, closes, dates_def):
    """把逐日持仓序列展开成逐资产交易台账(web 可直接渲染):
    资产权重 0->正 视为开仓, 正->0 视为平仓, 权重变化视为先平后开(再平衡)。
    末尾仍持有的资产记为 status='open'(供实盘页显示持仓中)。
    """
    open_pos = {}  # code -> {"entry_idx", "entry_price", "weight"}
    trades = []
    n = len(hold_spec)
    for i in range(n):
        w = hold_spec[i]
        for code, rec in list(open_pos.items()):
            new_w = w.get(code, 0.0)
            if new_w == 0.0 or new_w != rec["weight"]:
                px = closes[code][i]; pe = rec["entry_price"]
                ret = (px * (1 - SLIP)) / (pe * (1 + SLIP)) - 1
                if code == "588000":
                    reason = "TRIX死叉(簇多数翻空)"
                else:
                    reason = "防御组轮动切换" if new_w > 0 else "轮动清仓"
                trades.append({
                    "status": "closed",
                    "signalDate": dates_def[rec["entry_idx"]].strftime("%Y-%m-%d"),
                    "buyDate": dates_def[rec["entry_idx"]].strftime("%Y-%m-%d"),
                    "sellDate": dates_def[i].strftime("%Y-%m-%d"),
                    "etf": code, "name": NAMES.get(code, ""),
                    "buyPrice": round(float(pe), 4),
                    "sellPrice": round(float(px), 4),
                    "returnPct": round(ret * 100, 2),
                    "sellReason": reason,
                    "note": ("权重%.0f%%→%.0f%%" % (rec["weight"] * 100, new_w * 100)) if new_w > 0 else "清仓",
                })
                del open_pos[code]
        for code, new_w in w.items():
            if new_w > 0:
                if code not in open_pos:
                    open_pos[code] = {"entry_idx": i, "entry_price": closes[code][i], "weight": new_w}
                else:
                    open_pos[code]["weight"] = new_w
    for code, rec in open_pos.items():
        trades.append({
            "status": "open",
            "signalDate": dates_def[rec["entry_idx"]].strftime("%Y-%m-%d"),
            "buyDate": dates_def[rec["entry_idx"]].strftime("%Y-%m-%d"),
            "sellDate": None,
            "etf": code, "name": NAMES.get(code, ""),
            "buyPrice": round(float(rec["entry_price"]), 4),
            "sellPrice": None, "returnPct": None, "sellReason": None,
            "note": "持仓中 权重%.0f%%" % (rec["weight"] * 100),
        })
    return trades


def main():
    f = fetch_day("2020-11-16")
    close = f["close"].values.astype(float)
    dates = pd.to_datetime(f["date"].values)
    n = len(close)

    # ===== 纯 N12 基线(保留为参考) =====
    pos, frac = vote_from(close, COMB_N12, thr=0.5)
    eq, total, mdd, sw = sim(pos, close)
    curve = []
    for i in range(n):
        try:
            ts = int(dates[i].timestamp() * 1000)
        except Exception:
            continue
        curve.append([ts, (eq[i] - 1) * 100])
    n12_trades = []
    entry = None
    for i in range(n):
        if pos[i] == 1 and entry is None:
            entry = i
        elif pos[i] == 0 and entry is not None:
            pe = close[entry] * (1 + SLIP); px = close[i] * (1 - SLIP)
            ret = px / pe - 1
            n12_trades.append({
                "status": "closed", "signalDate": dates[entry].strftime("%Y-%m-%d"),
                "buyDate": dates[entry].strftime("%Y-%m-%d"), "sellDate": dates[i].strftime("%Y-%m-%d"),
                "etf": "588000", "name": "科创50ETF", "buyPrice": round(float(close[entry]), 4),
                "sellPrice": round(float(close[i]), 4), "returnPct": round(ret * 100, 2),
                "sellReason": "TRIX死叉(簇多数翻空)", "note": "回测",
            })
            entry = None
    if entry is not None:
        n12_trades.append({
            "status": "open", "signalDate": dates[entry].strftime("%Y-%m-%d"),
            "buyDate": dates[entry].strftime("%Y-%m-%d"), "sellDate": None,
            "etf": "588000", "name": "科创50ETF", "buyPrice": round(float(close[entry]), 4),
            "sellPrice": None, "returnPct": None, "sellReason": None, "note": "持仓中",
        })
    n12_closed = [t for t in n12_trades if t["status"] == "closed"]
    n12_wins = sum(1 for t in n12_closed if (t["returnPct"] or 0) > 0)
    n12_win_rate = (n12_wins / len(n12_closed) * 100) if n12_closed else 0.0
    n12_years = _years(dates)
    n12_annual = ((1 + total) ** (1 / max(n12_years, 1e-9)) - 1) * 100 if total > -1 else -100.0

    # ===== 防御组轮动(主策略) =====
    print("拉取防御组行情 ...")
    df = pd.DataFrame({"588000": f.set_index("date")["close"].astype(float)})
    for c in DEF:
        s = fetch_day_code(c, "2020-11-16")
        if s is not None:
            df[c] = s
    df = df.dropna()
    d_dates = df.index
    d_n = len(df)
    core, kc_cross, def_cross, hold_spec, _ = build_defensive_rotation(df)
    closes_def = {c: df[c].values.astype(float) for c in ["588000"] + DEF}
    eq_d, total_d, mdd_d, idle_d, sw_d = sim_mixed(hold_spec, closes_def, SLIP)

    d_curve = []
    for i in range(d_n):
        try:
            ts = int(pd.Timestamp(d_dates[i]).timestamp() * 1000)
        except Exception:
            continue
        d_curve.append([ts, (eq_d[i] - 1) * 100])

    rot_trades = build_rotation_trades(hold_spec, closes_def, d_dates)
    rot_closed = [t for t in rot_trades if t["status"] == "closed"]
    rot_wins = sum(1 for t in rot_closed if (t["returnPct"] or 0) > 0)
    rot_win_rate = (rot_wins / len(rot_closed) * 100) if rot_closed else 0.0

    d_years = _years(d_dates)
    annual_d = ((1 + total_d) ** (1 / max(d_years, 1e-9)) - 1) * 100 if total_d > -1 else -100.0

    w_now = hold_spec[-1]
    live = {
        "date": d_dates[-1].strftime("%Y-%m-%d"),
        "long_ratio": round(float(w_now.get("588000", 0.0)), 4),
        "position": int(w_now.get("588000", 0.0) > 0),
        "lastReturn": (rot_closed[0]["returnPct"] if rot_closed else 0.0),
    }

    # ===== 组装 payload: 顶层 = 防御组轮动(覆盖原纯 N12) =====
    payload = {
        "id": "star50_n12_ensemble",
        "window": f"{d_dates[0].strftime('%Y-%m-%d')}~{d_dates[-1].strftime('%Y-%m-%d')}",
        "startDate": d_dates[0].strftime("%Y-%m-%d"),
        "endDate": d_dates[-1].strftime("%Y-%m-%d"),
        "trading_days": int(d_n),
        "combos": COMB_N12,
        "stats": {
            "equity_pct": round(total_d * 100, 2),
            "annualReturn": round(annual_d, 2),
            "max_drawdown": round(abs(mdd_d) * 100, 2),
            "win_rate": round(rot_win_rate, 1),
            "trades": len(rot_closed),
            "switches": int(sw_d),
            "idle_rate": round(idle_d / d_n * 100, 2),
        },
        "equity_curve": d_curve,
        "trades": rot_trades,
        "live": live,
        # 原纯 N12 结果, 仅作对比参考 (不再是主策略)
        "n12_baseline": {
            "window": f"{dates[0].strftime('%Y-%m-%d')}~{dates[-1].strftime('%Y-%m-%d')}",
            "stats": {
                "equity_pct": round(total * 100, 2),
                "annualReturn": round(n12_annual, 2),
                "max_drawdown": round(abs(mdd) * 100, 2),
                "win_rate": round(n12_win_rate, 1),
                "trades": len(n12_closed),
                "switches": int(sw),
                "idle_rate": round((n - sum(pos)) / n * 100, 2),
            },
            "equity_curve": curve,
            "trades": n12_trades,
            "live": {
                "date": dates[-1].strftime("%Y-%m-%d"),
                "long_ratio": round(float(frac[-1]), 4),
                "position": int(pos[-1]),
                "lastReturn": (n12_closed[0]["returnPct"] if n12_closed else 0.0),
            },
        },
    }

    os.makedirs(OUT_JSON.parent, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 刷新投票率 CSV (仍为 588000 N12 投票率, 供其他模块)
    vote_df = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "long_ratio": frac})
    os.makedirs(VOTE_CSV.parent, exist_ok=True)
    vote_df.to_csv(VOTE_CSV, index=False)

    print(f"已写入: {OUT_JSON}")
    print(f"已刷新: {VOTE_CSV}")
    print(f"[主策略] 防御组轮动  区间 {payload['window']}  交易日 {d_n}")
    print(f"  累计 {total_d*100:.1f}%  年化 {annual_d:.1f}%  最大回撤 {abs(mdd_d)*100:.1f}%  "
          f"胜率 {rot_win_rate:.1f}%  交易 {len(rot_closed)}  切换 {sw_d}  空仓率 {idle_d/d_n*100:.1f}%")
    print(f"[参考] 纯 N12         累计 {total*100:.1f}%  年化 {n12_annual:.1f}%  最大回撤 {abs(mdd)*100:.1f}%")
    print(f"今日({d_dates[-1].date()}) 持仓: { {k: round(v,2) for k,v in w_now.items()} }")


if __name__ == "__main__":
    main()

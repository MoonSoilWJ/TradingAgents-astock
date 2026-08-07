"""非趋势进攻·另外两个"不同维度"信号 (pre2024 决定性窗口)

① 波动率突破 (range-breakout): 买当日振幅相对 20日历史异常放大的 ETF
   -> 捕捉"有事发生/隐性催化剂", 而非单纯涨幅. 隔夜持有.
② 截面反转 (cross-sectional reversal, long-only): 买昨日跌幅榜 Top-N, 次日卖
   -> 区别于已死的"时序隔夜反转"(买当日盘中最超卖). 这是截面相对反转.

数据: sys.argv[1] = tdx_5min_pre2024.json / tdx_5min_2y.json
"""
from __future__ import annotations
import json, math, statistics, sys
from pathlib import Path

CACHE = Path.home() / ".tradingagents/cache/t0_5min"
FIVE = CACHE / (sys.argv[1] if len(sys.argv) > 1 else "tdx_5min_2y.json")
FEE = 0.0003
SIG = "14:50"

print(f"载入 {FIVE.name} ...", flush=True)
etf_5min = json.loads(FIVE.read_text(encoding="utf-8"))["etf_5min"]
ALL_DATES = sorted({d for days in etf_5min.values() for d in days})
DATE_IDX = {d: i for i, d in enumerate(ALL_DATES)}

# 每只 ETF 的日特征
def price_at(bars, t):
    best = None
    for b in bars:
        if b["time"] <= t:
            best = b["close"]
    return best

feat = {}  # code -> list of dict(date, high, low, prev_close, sig, prior_ret)
for code, days in etf_5min.items():
    ds = sorted(days)
    daily_close = {d: days[d][-1]["close"] for d in ds if days[d]}
    recs = []
    for i, d in enumerate(ds):
        bars = days[d]
        if not bars:
            continue
        hi = max(b["high"] for b in bars)
        lo = min(b["low"] for b in bars)
        sp = price_at(bars, SIG)
        prev_c = daily_close.get(ds[i - 1]) if i > 0 else None
        prior_ret = (daily_close[d] / daily_close[ds[i - 1]] - 1) if (i > 0 and daily_close.get(ds[i - 1])) else None
        recs.append({
            "date": d, "high": hi, "low": lo,
            "prev_close": prev_c, "sig": sp, "prior_ret": prior_ret,
        })
    feat[code] = recs

def summarize(name, trades):
    if not trades:
        print(f"  [{name}] 0 笔"); return
    eq = 1.0
    for r in trades:
        eq *= (1 + r)
    n = len(trades)
    win = sum(1 for r in trades if r > 0) / n
    print(f"  [{name}] 笔数={n}  累计={(eq-1)*100:.1f}%  胜率={win*100:.1f}%  "
          f"均值/笔={statistics.mean(trades)*100:.3f}%  中位={statistics.median(trades)*100:.3f}%")

print("\n=== ① 波动率突破 (range-breakout) ===")
# 每只 ETF: 当日 range = (hi-lo)/prev_close; 历史20日均值; expansion = range/hist_mean
# 每天选 expansion 最大且 >= EXPAND_THR 的, 14:50买, 次日14:50卖
def run_breakout(EXPAND_THR, MIN_DAYS=240):
    cand = {c: r for c, r in feat.items() if len(r) >= MIN_DAYS}
    trades = []
    for d in ALL_DATES[21:]:
        di = DATE_IDX[d]
        if di < 21:
            continue
        best_code, best_exp = None, EXPAND_THR
        best_sig = None
        for code, recs in cand.items():
            idx = next((k for k, r in enumerate(recs) if r["date"] == d), None)
            if idx is None or idx < 20:
                continue
            r = recs[idx]
            if not r["prev_close"] or not r["sig"]:
                continue
            hist = [ (recs[k]["high"] - recs[k]["low"]) / recs[k]["prev_close"]
                     for k in range(idx - 20, idx) if recs[k]["prev_close"] ]
            if len(hist) < 15 or statistics.mean(hist) <= 0:
                continue
            exp = ((r["high"] - r["low"]) / r["prev_close"]) / statistics.mean(hist)
            if exp > best_exp:
                best_exp = exp; best_code = code; best_sig = r["sig"]
        if best_code is None:
            continue
        # 次日 14:50 卖
        nxt = next((r["sig"] for c2, recs in cand.items() if c2 == best_code
                    for r in recs if r["date"] > d), None)
        # 简单: 找 best_code 次日 sig
        recs = cand[best_code]
        nxt_rec = next((r for r in recs if r["date"] > d), None)
        if nxt_rec and nxt_rec["sig"]:
            trades.append(nxt_rec["sig"] / best_sig - 1 - 2 * FEE)
    return trades

for thr in (1.5, 2.0, 3.0):
    summarize(f"突破 expansion>={thr} 隔夜", run_breakout(thr))

print("\n=== ② 截面反转 (买昨日跌幅榜 Top-N, 次日14:50卖) ===")
def run_xsec(N, MIN_DAYS=240):
    cand = {c: r for c, r in feat.items() if len(r) >= MIN_DAYS}
    trades = []
    for d in ALL_DATES[1:]:
        # 昨日 prior_ret 排名
        scored = []
        for code, recs in cand.items():
            r = next((x for x in recs if x["date"] == d), None)
            if r and r["prior_ret"] is not None and r["sig"]:
                scored.append((r["prior_ret"], code, r["sig"]))
        if len(scored) < N:
            continue
        scored.sort()  # 最小 prior_ret 在前 = 昨日跌最多
        losers = scored[:N]
        for _, code, sig in losers:
            recs = cand[code]
            nxt = next((x for x in recs if x["date"] > d), None)
            if nxt and nxt["sig"]:
                trades.append(nxt["sig"] / sig - 1 - 2 * FEE)
    return trades

for n in (1, 3, 5):
    summarize(f"截面反转 N={n}", run_xsec(n))

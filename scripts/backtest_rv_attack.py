"""相对价值配对收敛攻击 v2 —— 固定时点 + 大幅瞬时偏离过滤

关键修正(针对 v1 的"薄且右偏/噪声交易"):
- 只在固定信号时点(14:55)判定, 每天每对最多 1 次信号 -> 杜绝 48 时点高频噪声
- 入场要求 |z|>=ENTRY_K 且 |偏离|>=MIN_ABS(真正的瞬时错位, 非溢价慢漂移)
- 隔夜持有, 比值回中(|z|<=EXIT_K)或 MAX_DAYS 平仓
- 按家族拆分, 看黄金/恒生科技/纳指/标普/日经/HSCEI/原油 哪个真能回归

数据：tdx_5min_2y.json (2024-07-03~2026-07-31)
"""
from __future__ import annotations
import json, math, statistics
from pathlib import Path

CACHE = Path.home() / ".tradingagents/cache/t0_5min"
import sys
FIVE = CACHE / (sys.argv[1] if len(sys.argv) > 1 else "tdx_5min_2y.json")
FEE = 0.0003
ST = "14:55"

FAMILIES = {
    "GOLD":    ["518880", "159934", "518600", "518660", "518800", "517520", "159812"],
    "HSTECH":  ["513180", "513130", "513010", "513260", "159740", "159632", "159808", "513110"],
    "NASDAQ":  ["513100", "159941", "513300", "513400", "513850"],
    "SP500":   ["513500", "513550", "513650", "159685", "159697"],
    "NIKKEI":  ["513520", "513880", "513580"],
    "HSCEI":   ["510900", "513600", "513630", "513730", "513900", "513750"],
    "OIL":     ["162411", "501018", "161129", "162719"],
    "HKINTERNET": ["513330", "513050", "513770", "513190", "513700", "513980"],
}

print("载入 5min ...", flush=True)
etf_5min = json.loads(FIVE.read_text(encoding="utf-8"))["etf_5min"]
codes = set(etf_5min.keys())
ALL_DATES = sorted({d for days in etf_5min.values() for d in days})
DATE_IDX = {d: i for i, d in enumerate(ALL_DATES)}

def price_at(bars, target):
    best = None
    for b in bars:
        if b["time"] <= target:
            best = b["close"]
    return best

# 日收益相关性(验证双胞胎)
daily_ret = {}
for code in codes:
    closes = {d: bars[-1]["close"] for d, bars in etf_5min[code].items() if bars}
    ds = sorted(closes)
    daily_ret[code] = [(ds[i], closes[ds[i]] / closes[ds[i - 1]] - 1)
                       for i in range(1, len(ds)) if closes[ds[i - 1]] > 0]

def corr(c1, c2):
    s1 = dict(daily_ret[c1]); s2 = dict(daily_ret[c2])
    common = [d for d in s1 if d in s2]
    if len(common) < 150:
        return None
    xs = [s1[d] for d in common]; ys = [s2[d] for d in common]
    n = len(xs); mx = sum(xs) / n; my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs) ** 0.5
    vy = sum((y - my) ** 2 for y in ys) ** 0.5
    return cov / (vx * vy) if vx > 1e-9 and vy > 1e-9 else None

twin_pairs = []  # (A,B,corr,fam)
for fam, members in FAMILIES.items():
    avail = [m for m in members if m in codes]
    for i in range(len(avail)):
        for j in range(i + 1, len(avail)):
            c = corr(avail[i], avail[j])
            if c is not None and c >= 0.90:
                twin_pairs.append((avail[i], avail[j], round(c, 3), fam))
print(f"已验证双胞胎对: {len(twin_pairs)}")

L = 30
MINL = 15

def run_daily(pairs, ENTRY_K, EXIT_K, MIN_ABS, MAX_DAYS):
    trades = []          # (ret, fam)
    by_fam = {}
    for (A, B, _, fam) in pairs:
        series = []
        for d in ALL_DATES:
            ba = etf_5min[A].get(d); bb = etf_5min[B].get(d)
            if not ba or not bb:
                continue
            ca = price_at(ba, ST); cb = price_at(bb, ST)
            if ca and cb and ca > 0 and cb > 0:
                series.append((d, math.log(ca / cb), ca, cb))
        evs = []
        for i in range(len(series)):
            past = [series[j][1] for j in range(max(0, i - L), i)]
            if len(past) >= MINL:
                mu = statistics.mean(past); sd = statistics.pstdev(past)
                if sd > 1e-9:
                    d, s, ca, cb = series[i]
                    if abs(s - mu) >= MIN_ABS:
                        evs.append((d, (s - mu) / sd, ca, cb))
        pos = None
        for (d, z, ca, cb) in evs:
            if pos is None:
                if z <= -ENTRY_K:
                    pos = {"day": d, "leg": "A", "px": ca}
                elif z >= ENTRY_K:
                    pos = {"day": d, "leg": "B", "px": cb}
            else:
                held = DATE_IDX[d] - DATE_IDX[pos["day"]]
                cur = ca if pos["leg"] == "A" else cb
                cond = (z >= EXIT_K) if pos["leg"] == "A" else (z <= -EXIT_K)
                if cond or held >= MAX_DAYS:
                    ret = (cur / pos["px"] - 1) - 2 * FEE
                    trades.append((ret, fam))
                    by_fam.setdefault(fam, []).append(ret)
                    pos = None
    return trades, by_fam

def summ(name, trades):
    if not trades:
        print(f"  {name}: 0 笔"); return
    eq = 1.0
    for r, _ in trades:
        eq *= (1 + r)
    n = len(trades)
    win = sum(1 for r, _ in trades if r > 0) / n
    print(f"  {name}: 笔数={n}  累计={(eq-1)*100:.1f}%  胜率={win*100:.1f}%  "
          f"均值/笔={statistics.mean([r for r,_ in trades])*100:.3f}%  "
          f"中位={statistics.median([r for r,_ in trades])*100:.3f}%")

print("\n=== 网格: ENTRY_K / MIN_ABS (EXIT_K=0.3, MAX_DAYS=3, ST=14:55) ===")
for EK in (2.0, 2.5, 3.0):
    for MA in (0.003, 0.005):
        tr, _ = run_daily(twin_pairs, EK, 0.3, MA, 3)
        print(f"\n[ENTRY_K={EK} MIN_ABS={MA}]")
        summ("  总体", tr)
print("\n=== 按家族 (ENTRY_K=2.5, MIN_ABS=0.005) ===")
tr, by_fam = run_daily(twin_pairs, 2.5, 0.3, 0.005, 3)
for fam in FAMILIES:
    if fam in by_fam:
        eq = 1.0
        for r in by_fam[fam]:
            eq *= (1 + r)
        n = len(by_fam[fam])
        win = sum(1 for r in by_fam[fam] if r > 0) / n
        print(f"  {fam}: 笔数={n}  累计={(eq-1)*100:.1f}%  胜率={win*100:.1f}%  "
              f"均值/笔={statistics.mean(by_fam[fam])*100:.3f}%")

# 按年拆分(验证非趋势年 2023 是否仍有效)
print("\n=== 按年拆分 (ENTRY_K=2.5, MIN_ABS=0.005, 总体) ===")
by_year = {}
for (ret, fam), d in zip(tr, [e[0] for e in []]):
    pass
# 重新跑一遍带年份
def run_daily_yearly(pairs, ENTRY_K, EXIT_K, MIN_ABS, MAX_DAYS):
    by_year = {}
    by_fam_year = {}
    for (A, B, _, fam) in pairs:
        series = []
        for d in ALL_DATES:
            ba = etf_5min[A].get(d); bb = etf_5min[B].get(d)
            if not ba or not bb:
                continue
            ca = price_at(ba, ST); cb = price_at(bb, ST)
            if ca and cb and ca > 0 and cb > 0:
                series.append((d, math.log(ca / cb), ca, cb))
        evs = []
        for i in range(len(series)):
            past = [series[j][1] for j in range(max(0, i - L), i)]
            if len(past) >= MINL:
                mu = statistics.mean(past); sd = statistics.pstdev(past)
                if sd > 1e-9:
                    d, s, ca, cb = series[i]
                    if abs(s - mu) >= MIN_ABS:
                        evs.append((d, (s - mu) / sd, ca, cb))
        pos = None
        for (d, z, ca, cb) in evs:
            if pos is None:
                if z <= -ENTRY_K:
                    pos = {"day": d, "leg": "A", "px": ca}
                elif z >= ENTRY_K:
                    pos = {"day": d, "leg": "B", "px": cb}
            else:
                held = DATE_IDX[d] - DATE_IDX[pos["day"]]
                cur = ca if pos["leg"] == "A" else cb
                cond = (z >= EXIT_K) if pos["leg"] == "A" else (z <= -EXIT_K)
                if cond or held >= MAX_DAYS:
                    ret = (cur / pos["px"] - 1) - 2 * FEE
                    y = d[:4]
                    by_year.setdefault(y, []).append(ret)
                    by_fam_year.setdefault(fam, {}).setdefault(y, []).append(ret)
                    pos = None
    return by_year, by_fam_year

by_year, by_fam_year = run_daily_yearly(twin_pairs, 2.5, 0.3, 0.005, 3)
for y in sorted(by_year):
    rs = by_year[y]
    eq = 1.0
    for r in rs:
        eq *= (1 + r)
    win = sum(1 for r in rs if r > 0) / len(rs)
    print(f"  {y}: 笔数={len(rs)}  累计={(eq-1)*100:.1f}%  胜率={win*100:.1f}%  "
          f"均值/笔={statistics.mean(rs)*100:.3f}%")
print("\n  黄金家族按年:")
if "GOLD" in by_fam_year:
    for y in sorted(by_fam_year["GOLD"]):
        rs = by_fam_year["GOLD"][y]
        eq = 1.0
        for r in rs:
            eq *= (1 + r)
        print(f"    {y}: 笔数={len(rs)}  累计={(eq-1)*100:.1f}%  均值/笔={statistics.mean(rs)*100:.3f}%")

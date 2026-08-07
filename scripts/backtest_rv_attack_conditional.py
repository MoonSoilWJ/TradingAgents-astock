"""条件化配对攻击 —— 相对价值收敛作为「主动量攻击熄火的补充进攻」

设计(用户选定方向):
- 主动量攻击 = 买当日涨幅 Top1 ≥3% 的 T0 ETF, 次日卖(现有 B 策略)。
- 主动量「熄火」 = 当日 14:55 全宇宙无任何 ETF 日内涨幅 ≥3%  -> 今天没动量行情。
- 条件化配对攻击 = 仅在「动量熄火」且「非趋势市(regime=震荡/中性)」日,
  部署干净子宇宙(GOLD/NASDAQ/HSCEI, 剔除不回归的 HSTECH/HKINTERNET)的
  配对收敛(固定 14:55 判定, 隔夜持有至比值回中或 ≤MAX_DAYS 平)。
- regime gate 修掉 pre2024 上 2022 趋势性下跌年的脆弱性(趋势市里双胞胎
  溢价结构不同步, z 信号误触发亏钱)。

对照实验(看 gate 的边际贡献, 尤其 2022):
  A 裸配对(无条件) | B 仅动量熄火 | C 仅 regime非趋势 | D 双gate(最终方案)
"""
from __future__ import annotations
import json, math, statistics, sys
from pathlib import Path

CACHE = Path.home() / ".tradingagents/cache/t0_5min"
# 支持逗号分隔多个文件(合并加载, 重叠0天)
FILES = (sys.argv[1] if len(sys.argv) > 1 else "tdx_5min_2y.json").split(",")
FEE = 0.0003
ST = "14:55"
MIN_GAIN_MOM = 0.03          # 主动量熄火阈值(日内涨幅 <3% 视为无动量行情)
REGIME_PROXY = "501018"      # t0_regime 同款代理

# ── 干净子宇宙(剔除不回归的 HSTECH/HKINTERNET/SP500/NIKKEI/OIL) ──
FAMILIES = {
    "GOLD":    ["518880", "159934", "518600", "518660", "518800", "517520", "159812"],
    "NASDAQ":  ["513100", "159941", "513300", "513400", "513850"],
    "HSCEI":   ["510900", "513600", "513630", "513730", "513900", "513750"],
}

print("载入 5min ...", flush=True)
etf_5min: dict = {}
for fn in FILES:
    p = CACHE / fn.strip()
    data = json.loads(p.read_text(encoding="utf-8"))
    sub = data.get("etf_5min", data)
    for code, days in sub.items():
        etf_5min.setdefault(code, {}).update(days)
print(f"  文件={FILES}  标的数={len(etf_5min)}", flush=True)

codes = set(etf_5min.keys())
ALL_DATES = sorted({d for days in etf_5min.values() for d in days})
DATE_IDX = {d: i for i, d in enumerate(ALL_DATES)}
N_DAYS = len(ALL_DATES)

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
print(f"  干净子宇宙双胞胎对: {len(twin_pairs)} -> {[f'{a}/{b}' for a,b,_,_ in twin_pairs]}")

# ── regime 代理日K 构造 ──
proxy = REGIME_PROXY if REGIME_PROXY in codes else (FAMILIES["GOLD"][0] if FAMILIES["GOLD"][0] in codes else None)
proxy_daily = []
if proxy:
    for d in ALL_DATES:
        bars = etf_5min[proxy].get(d)
        if bars:
            proxy_daily.append({
                "day": d,
                "open": bars[0]["open"],
                "high": max(b["high"] for b in bars),
                "low": min(b["low"] for b in bars),
                "close": bars[-1]["close"],
            })
print(f"  regime 代理={proxy}  日K={len(proxy_daily)}根")

# 内联 detect_regime(避免依赖, 与 t0_regime.py 同逻辑)
def detect_regime(daily, as_of):
    idx_map = {k["day"]: i for i, k in enumerate(daily)}
    idx = idx_map.get(as_of, len(daily) - 1)
    if idx < 29:
        return {"mode": "中性", "as_of": as_of}
    closes = [daily[j]["close"] for j in range(idx - 29, idx + 1)]
    ma20 = sum(closes[-20:]) / 20
    close = closes[-1]
    dist = abs(close - ma20) / ma20 * 100 if ma20 else 0
    # MA20 穿越(近10日)
    crosses = 0; prev = None
    for i in range(len(closes) - 10, len(closes)):
        above = closes[i] > sum(closes[i - 20 + 1:i + 1]) / 20
        if prev is not None and above != prev:
            crosses += 1
        prev = above
    # ADX(14) 简化
    def adx(cls, p=14):
        if len(cls) < p + 2: return 0.0
        trs = []; pdm = []; mdm = []
        for i in range(1, len(cls)):
            up = cls[i] - cls[i - 1]; dn = cls[i - 1] - cls[i]
            pdm.append(up if up > 0 else 0); mdm.append(dn if dn > 0 else 0)
            trs.append(abs(cls[i] - cls[i - 1]))
        def w(v):
            s = sum(v[:p]); out = [None] * p; out.append(s)
            for i in range(p, len(v) - 1):
                s = s - s / p + v[i + 1]; out.append(s)
            return out
        atr = w(trs); pdi = w(pdm); mdi = w(mdm)
        dx = []
        for i in range(p, len(trs)):
            if atr[i]:
                p1 = 100 * pdi[i] / atr[i]; m1 = 100 * mdi[i] / atr[i]
                dx.append(100 * abs(p1 - m1) / (p1 + m1) if (p1 + m1) else 0)
        return sum(dx) / len(dx) if dx else 0.0
    a = adx(closes)
    if crosses >= 2:
        mode = "震荡"
    elif dist > 8.0 and a > 30.0:
        mode = "趋势"
    else:
        mode = "中性"
    return {"mode": mode, "as_of": as_of}

# ── 预先算每交易日的 gate 标志 ──
L = 30; MINL = 15; ENTRY_K = 2.5; EXIT_K = 0.3; MIN_ABS = 0.005; MAX_DAYS = 3

def mom_fired(d):
    """当日 14:55 是否有任一 ETF 日内涨幅 ≥ MIN_GAIN_MOM (动量未熄火)."""
    best = -9
    di = DATE_IDX[d]
    if di < 1:
        return False
    prev_d = ALL_DATES[di - 1]
    for code in codes:
        ba = etf_5min[code].get(d); bb = etf_5min[code].get(prev_d)
        if not ba or not bb:
            continue
        ca = price_at(ba, ST); cb = price_at(bb, ST) or (bb[-1]["close"] if bb else None)
        if ca and cb and cb > 0:
            g = ca / cb - 1
            if g > best:
                best = g
    return best >= MIN_GAIN_MOM

print("预计算 gate (动量熄火 / regime) ...", flush=True)
mom_off = {}      # d -> bool (True=熄火=无动量)
regime_mode = {}  # d -> str
for d in ALL_DATES:
    mom_off[d] = not mom_fired(d)
    regime_mode[d] = detect_regime(proxy_daily, d)["mode"] if proxy_daily else "中性"

n_mom_off = sum(1 for d in ALL_DATES if mom_off[d])
n_choppy = sum(1 for d in ALL_DATES if regime_mode[d] != "趋势")
print(f"  总交易日={N_DAYS}  动量熄火日={n_mom_off}({n_mom_off/N_DAYS*100:.1f}%)  "
      f"非趋势(regime≠趋势)日={n_choppy}({n_choppy/N_DAYS*100:.1f}%)")

# ── 配对信号核心(可加 gate) ──
def run_conditional(gate_name, allow):
    trades = []; by_fam = {}; by_year = {}
    for (A, B, _, fam) in twin_pairs:
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
            if not allow(d):
                continue
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
                    by_year.setdefault(d[:4], []).append(ret)
                    pos = None
    return trades, by_fam, by_year

def summ(name, trades):
    if not trades:
        print(f"  [{name}] 0 笔"); return
    eq = 1.0
    for r, _ in trades: eq *= (1 + r)
    n = len(trades)
    win = sum(1 for r, _ in trades if r > 0) / n
    print(f"  [{name}] 笔数={n}  累计={(eq-1)*100:.1f}%  胜率={win*100:.1f}%  "
          f"均值/笔={statistics.mean([r for r,_ in trades])*100:.3f}%  "
          f"中位={statistics.median([r for r,_ in trades])*100:.3f}%")

def yearly(name, by_year):
    print(f"  └─ 按年 [{name}]:")
    for y in sorted(by_year):
        rs = by_year[y]; eq = 1.0
        for r in rs: eq *= (1 + r)
        win = sum(1 for r in rs if r > 0) / len(rs)
        print(f"      {y}: 笔={len(rs)}  累计={(eq-1)*100:.1f}%  胜={win*100:.1f}%  "
              f"均值/笔={statistics.mean(rs)*100:.3f}%")

print("\n========== 对照实验 ==========")
# A 裸配对
trA, faA, yaA = run_conditional("A裸配对", lambda d: True)
summ("A 裸配对(无条件)", trA); yearly("A", yaA)
# B 仅动量熄火
trB, faB, yaB = run_conditional("B仅动量熄火", lambda d: mom_off[d])
summ("B 仅动量熄火", trB); yearly("B", yaB)
# C 仅 regime 非趋势
trC, faC, yaC = run_conditional("C仅regime非趋势", lambda d: regime_mode[d] != "趋势")
summ("C 仅regime≠趋势", trC); yearly("C", yaC)
# D 双 gate(最终)
trD, faD, yaD = run_conditional("D双gate", lambda d: mom_off[d] and regime_mode[d] != "趋势")
summ("D 双gate(最终方案)", trD); yearly("D", yaD)

print("\n=== D 方案按家族 ===")
for fam in FAMILIES:
    if fam in faD:
        eq = 1.0
        for r in faD[fam]: eq *= (1 + r)
        n = len(faD[fam]); win = sum(1 for r in faD[fam] if r > 0) / n
        print(f"  {fam}: 笔={n}  累计={(eq-1)*100:.1f}%  胜={win*100:.1f}%  "
              f"均值/笔={statistics.mean(faD[fam])*100:.3f}%")

# 补充进攻增量 vs 现金
print("\n=== 补充进攻频率 ===")
d_days = sum(1 for d in ALL_DATES if mom_off[d] and regime_mode[d] != "趋势")
print(f"  双gate触发日(配对可开仓窗口): {d_days} 天 / {N_DAYS} 总交易日 "
      f"({d_days/N_DAYS*100:.1f}%)")
print(f"  这些天主动量熄火(无B候选), 配对是这些天的纯补充进攻。")

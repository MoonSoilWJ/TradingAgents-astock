"""
模拟：把策略从「进攻主导 Overlay」翻转为「防守常驻底仓 X% + 进攻卫星(1-X)%」。
防守腿月度 = 真实日线等权(可用资产)；进攻腿月度 = 从用户给的 Overlay 月度表反推
  (Overlay ≈ (1-f)*防守 + f*进攻, f≈0.38 进攻持仓占比)。
新组合月度 = X*防守 + (1-X)*进攻。
输出各 X 的逐年收益 / 总收益 / MDD，对比原 Overlay。
用法：python3 scripts/sim_defense_base.py
"""
import json
from pathlib import Path
import akshare as ak
import pandas as pd
import numpy as np

CACHE = Path.home() / '.tradingagents/cache/t0_5min'
DEFENSE = {'518880': '黄金', '511090': '30Y', '511260': '10Y', '510880': '红利'}
F = 0.38  # 进攻腿持仓占比（占交易日）

def to_sina(c):
    return ('sh' if c[0] in '56' else 'sz') + c

# ---- 用户给的 Overlay(False) 月度收益（来自聚宽 2018-2026）----
OVERLAY = {
 '2018-06':-0.0201,'2018-07':0.0143,'2018-08':-0.0164,'2018-09':0.0112,'2018-10':-0.0060,'2018-11':0.0012,'2018-12':0.0179,
 '2019-01':0.0444,'2019-02':0.0319,'2019-03':0.0116,'2019-04':-0.0121,'2019-05':-0.0037,'2019-06':0.0327,'2019-07':0.0103,'2019-08':0.0954,'2019-09':-0.0216,'2019-10':-0.0102,'2019-11':-0.0095,'2019-12':0.0316,
 '2020-01':-0.0032,'2020-02':0.0189,'2020-03':-0.1090,'2020-04':0.0389,'2020-05':-0.0186,'2020-06':-0.0134,'2020-07':0.1450,'2020-08':0.0123,'2020-09':-0.0159,'2020-10':-0.0332,'2020-11':-0.0042,'2020-12':-0.0121,
 '2021-01':-0.0209,'2021-02':0.0542,'2021-03':0.0035,'2021-04':0.0151,'2021-05':0.0391,'2021-06':-0.0164,'2021-07':-0.0176,'2021-08':0.0349,'2021-09':-0.0343,'2021-10':0.0012,'2021-11':-0.0034,'2021-12':0.0255,
 '2022-01':-0.0186,'2022-02':-0.0166,'2022-03':0.0654,'2022-04':-0.0260,'2022-05':-0.0272,'2022-06':-0.0163,'2022-07':-0.0446,'2022-08':0.0381,'2022-09':0.0001,'2022-10':-0.0769,'2022-11':0.0504,'2022-12':-0.0460,
 '2023-01':0.0249,'2023-02':-0.0218,'2023-03':0.0130,'2023-04':0.0168,'2023-05':0.0785,'2023-06':0.1076,'2023-07':0.0246,'2023-08':-0.0521,'2023-09':-0.0171,'2023-10':0.0224,'2023-11':0.0194,'2023-12':-0.0613,
 '2024-01':0.1609,'2024-02':0.0086,'2024-03':0.0286,'2024-04':0.4925,'2024-05':-0.0719,'2024-06':-0.0181,'2024-07':0.0893,'2024-08':0.0199,'2024-09':0.0914,'2024-10':0.0363,'2024-11':0.0510,'2024-12':0.0332,
 '2025-01':0.6271,'2025-02':-0.2204,'2025-03':0.0978,'2025-04':0.3360,'2025-05':0.0051,'2025-06':0.0224,'2025-07':0.0481,'2025-08':-0.0084,'2025-09':-0.0038,'2025-10':0.0579,'2025-11':-0.0667,'2025-12':0.0429,
 '2026-01':0.2080,'2026-02':-0.0489,'2026-03':-0.0284,'2026-04':0.1434,'2026-05':0.0013,'2026-06':-0.1361,'2026-07':0.0473,'2026-08':0.0090,
}

def fetch_defense_monthly():
    prices = {}
    for code in DEFENSE:
        df = ak.fund_etf_hist_sina(symbol=to_sina(code))
        df = df[['date', 'close']].copy()
        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        df = df[df['date'] >= '2018-01-01']
        prices[code] = df.set_index('date')['close'].astype(float)
    all_dates = pd.Index(sorted(set().union(*[set(s.index) for s in prices.values()])))
    df = pd.DataFrame({c: s.reindex(all_dates) for c, s in prices.items()}).ffill()
    codes = list(DEFENSE)
    n = len(codes)
    rets = df.pct_change().fillna(0.0)
    rets.index = pd.to_datetime(rets.index)
    nav = 1.0
    nav_hist = []
    cur_month = None
    w = None
    for date, row in rets.iterrows():
        m = date.strftime('%Y-%m')
        avail = [i for i, c in enumerate(codes) if not np.isnan(row.values[i])]
        if m != cur_month or w is None:
            k = len(avail)
            w = np.array([(1.0/k if i in avail else 0.0) for i in range(n)])
            cur_month = m
        day_ret = float(sum(w[i]*(row.values[i] if not np.isnan(row.values[i]) else 0.0) for i in range(n)))
        nav *= (1.0 + day_ret)
        nav_hist.append((date, nav))
    ns = pd.Series({d: v for d, v in nav_hist})
    # 月度收益
    monthly = {}
    for m in pd.unique(ns.index.strftime('%Y-%m')):
        md = ns[ns.index.strftime('%Y-%m') == m]
        if len(md) >= 2:
            monthly[m] = md.iloc[-1]/md.iloc[0] - 1
    return monthly

def yearly_of(monthly):
    eq = 1.0
    by_year = {}
    cur = None
    for m in sorted(monthly):
        y = m[:4]
        if y != cur:
            if cur is not None and cur in by_year and by_year[cur] is None:
                pass
            cur = y
            by_year[y] = None
        eq *= (1 + monthly[m])
    # 用月收益按年复利
    yret = {}
    years = sorted(set(m[:4] for m in monthly))
    for y in years:
        ms = [monthly[m] for m in sorted(monthly) if m[:4] == y]
        yret[y] = float(np.prod([1+r for r in ms]) - 1)
    return yret

def stats(monthly):
    eq = 1.0
    curve = []
    for m in sorted(monthly):
        eq *= (1 + monthly[m])
        curve.append(eq)
    total = curve[-1] - 1
    mdd = 0.0
    peak = 1.0
    # 用月度曲线近似回撤
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v/peak - 1)
    return total, mdd

def main():
    def_m = fetch_defense_monthly()
    # 公共月份
    months = sorted(set(OVERLAY) & set(def_m))
    # 反推进攻月度
    atk_m = {}
    for m in months:
        atk_m[m] = (OVERLAY[m] - (1-F)*def_m[m]) / F
    print(f'共同月份 {months[0]}~{months[-1]} 共 {len(months)} 月')
    print('\n===== 各「防守常驻底仓 X」方案的逐年收益 =====')
    header = f'{"X(防守%)":>10} | ' + ' '.join(f'{y:>7}' for y in sorted(set(m[:4] for m in months))) + ' |   总     MDD'
    print(header)
    for X in [1.0, 0.85, 0.7, 0.5]:
        new_m = {m: X*def_m[m] + (1-X)*atk_m[m] for m in months}
        yret = yearly_of(new_m)
        total, mdd = stats(new_m)
        line = f'{int(X*100):>9}% | ' + ' '.join(f'{yret.get(y,0)*100:>+6.1f}%' for y in sorted(yret)) + f' | {total*100:>+5.0f}% {mdd*100:>+5.1f}%'
        print(line)
    # 原 Overlay 参照
    ov_y = yearly_of({m: OVERLAY[m] for m in months})
    ov_t, ov_d = stats({m: OVERLAY[m] for m in months})
    print(f'{"Overlay":>10} | ' + ' '.join(f'{ov_y.get(y,0)*100:>+6.1f}%' for y in sorted(ov_y)) + f' | {ov_t*100:>+5.0f}% {ov_d*100:>+5.1f}%')
    # 纯防守参照
    df_y = yearly_of({m: def_m[m] for m in months})
    df_t, df_d = stats({m: def_m[m] for m in months})
    print(f'{"纯防守":>10} | ' + ' '.join(f'{df_y.get(y,0)*100:>+6.1f}%' for y in sorted(df_y)) + f' | {df_t*100:>+5.0f}% {df_d*100:>+5.1f}%')

if __name__ == '__main__':
    main()

"""
分析：聚宽防守腿（518880黄金 + 511090 30Y国债 + 511260 10Y国债 + 510880红利）
等权月度再平衡，2018-2026 逐年收益。
目的：验证「防守腿自己拿住是否每年都正」——这是把策略从
      「进攻主导彩票」改成「防守常驻底仓 + 进攻卫星」能否稳定盈利的关键证据。
用法：python3 scripts/analyze_defense_leg.py
"""
import json
from pathlib import Path
import akshare as ak
import pandas as pd
import numpy as np

CACHE = Path.home() / '.tradingagents/cache/t0_5min'
OUT = CACHE / 'defense_leg_2018_2026.json'

DEFENSE = {
    '518880': '黄金ETF',
    '511090': '30Y国债ETF',
    '511260': '10Y国债ETF',
    '510880': '红利ETF',
}

def to_sina(code):
    return ('sh' if code[0] in '56' else 'sz') + code

def fetch(code):
    df = ak.fund_etf_hist_sina(symbol=to_sina(code))
    df = df[['date', 'close']].copy()
    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
    df = df[df['date'] >= '2018-01-01']
    return df

def main():
    prices = {}
    for code, name in DEFENSE.items():
        print(f'拉取 {code} {name} ...')
        df = fetch(code)
        print(f'   {df.iloc[0]["date"]} ~ {df.iloc[-1]["date"]} 共 {len(df)} 行')
        prices[code] = df.set_index('date')['close'].astype(float)
    all_dates = pd.Index(sorted(set().union(*[set(s.index) for s in prices.values()])))
    print(f'对齐交易日 {all_dates[0]} ~ {all_dates[-1]} 共 {len(all_dates)} 天')
    df = pd.DataFrame({c: s.reindex(all_dates) for c, s in prices.items()})
    df = df.ffill()   # 仅填充上市后的交易日缺口；上市前保持 NaN（不参与等权）

    codes = list(DEFENSE)
    n = len(codes)
    rets = df.pct_change().fillna(0.0)
    rets.index = pd.to_datetime(rets.index)

    # 等权月度再平衡：每月首个交易日权重重置为 1/k（k=当年可交易资产数），
    # 月内持有。缺失上市前数据的资产当年不参与（如 511090 2023-06 才上市）。
    nav = 1.0
    nav_hist = []
    cur_month = None
    w = None
    for date, row in rets.iterrows():
        m = date.strftime('%Y-%m')
        avail = [i for i, c in enumerate(codes) if not np.isnan(row.values[i])]
        if m != cur_month or w is None:
            k = len(avail)
            w = np.array([(1.0 / k if i in avail else 0.0) for i in range(n)])
            cur_month = m
        day_ret = float(sum(w[i] * (row.values[i] if not np.isnan(row.values[i]) else 0.0)
                             for i in range(n)))
        nav *= (1.0 + day_ret)
        nav_hist.append((date, nav))

    nav_series = pd.Series({d: v for d, v in nav_hist})
    yearly = {}
    for y in range(2018, 2027):
        yd = nav_series[nav_series.index.year == y]
        if len(yd) < 5:
            continue
        yearly[y] = yd.iloc[-1] / yd.iloc[0] - 1

    # 单资产逐年（买入持有，参考）
    single = {}
    for c in codes:
        s = prices[c].copy()
        s.index = pd.to_datetime(s.index)
        yr = {}
        for y in range(2018, 2027):
            sd = s[s.index.year == y]
            if len(sd) > 1:
                yr[y] = sd.iloc[-1] / sd.iloc[0] - 1
        single[c] = yr

    print('\n===== 防守腿等权月度再平衡 · 逐年收益 =====')
    hdr = f'{"年":>6} | {"组合":>9} | ' + ' '.join(f'{DEFENSE[c][:4]:>8}' for c in codes)
    print(hdr)
    for y in sorted(yearly):
        line = f'{y:>6} | {yearly[y]*100:>+8.2f}% | ' + ' '.join(
            f'{single[c].get(y, float("nan"))*100:>+7.1f}%' for c in codes)
        print(line)
    total = nav_series.iloc[-1] / nav_series.iloc[0] - 1
    yrs = (nav_series.index[-1] - nav_series.index[0]).days / 365.25
    cagr = (nav_series.iloc[-1] / nav_series.iloc[0]) ** (1 / yrs) - 1
    roll_max = nav_series.cummax()
    mdd = (nav_series / roll_max - 1).min()
    print(f'\n全周期 2018~2026: 总 {total*100:.1f}%  CAGR {cagr*100:.1f}%  MDD {mdd*100:.1f}%')

    json.dump({
        'yearly': {str(k): v for k, v in yearly.items()},
        'single': {c: {str(y): single[c][y] for y in single[c]} for c in codes},
        'total': total, 'cagr': cagr, 'mdd': mdd,
    }, open(OUT, 'w'), ensure_ascii=False, indent=2)
    print('落盘:', OUT)

if __name__ == '__main__':
    main()

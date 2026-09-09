#!/usr/bin/env python3
"""A 股个股趋势检验 — 个股层面横截面动量 IC + 技术趋势选股回测。

问题: "如果选个股, 怎么选" 的前提是个股存在可抓的趋势。
检验:
  [1] 截面动量 IC: 每日 spearman(过去20日收益, 未来20日收益) 跨个股
      → 正 = 强者恒强(趋势选股有原料); 负 = 反转市(选趋势股=反向收割)
  [2] 技术趋势选股回测: N12簇≥2/3 + 多头排列 → 等权持最强 Top5
数据: 东财前复权(个股分红除权频繁, 必须复权)
⚠ 池=当前知名股(幸存者偏差, 方向参考); 涨停未模拟(实际更差)。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

START = "2019-06-01"
SLIP = 0.001  # 个股滑点高于ETF

STOCKS = [
    ("600519", "贵州茅台"), ("601318", "中国平安"), ("600036", "招商银行"),
    ("000858", "五粮液"), ("600900", "长江电力"), ("601899", "紫金矿业"),
    ("300750", "宁德时代"), ("002594", "比亚迪"), ("601012", "隆基绿能"),
    ("600030", "中信证券"), ("000333", "美的集团"), ("600276", "恒瑞医药"),
    ("600887", "伊利股份"), ("603259", "药明康德"), ("000651", "格力电器"),
    ("002475", "立讯精密"), ("600028", "中国石化"), ("601398", "工商银行"),
    ("002415", "海康威视"), ("600809", "山西汾酒"), ("601888", "中国中免"),
    ("600031", "三一重工"), ("601668", "中国建筑"), ("600585", "海螺水泥"),
    ("000725", "京东方A"), ("002230", "科大讯飞"), ("600690", "海尔智家"),
    ("000063", "中兴通讯"), ("002241", "歌尔股份"), ("600745", "闻泰科技"),
    ("601021", "春秋航空"), ("600009", "上海机场"), ("601111", "中国国航"),
    ("600104", "上汽集团"), ("601633", "长城汽车"), ("000625", "长安汽车"),
    ("600346", "恒力石化"), ("601225", "陕西煤业"), ("601088", "中国神华"),
    ("600256", "广汇能源"), ("000568", "泸州老窖"), ("600809", "山西汾酒2"),
    ("000596", "古井贡酒"), ("600703", "三安光电"), ("002050", "三花智控"),
    ("600438", "通威股份"), ("601865", "福莱特"), ("300274", "阳光电源"),
    ("002129", "TCL中环"), ("600089", "特变电工"), ("601728", "中国电信"),
    ("600941", "中国移动"), ("601857", "中国石油"), ("600938", "中国海油"),
    ("601988", "中国银行"), ("601288", "农业银行"), ("601328", "交通银行"),
    ("600016", "民生银行"), ("600000", "浦发银行"), ("002007", "华兰生物"),
]


def fetch_qfq(code: str) -> pd.Series | None:
    """pytdx 不复权 + 送转/折算断点自动接续修复(fix_splits, >25%跳空)。
    分红的小跳空(1~3%)不修复——对 20 日动量/趋势形态判定影响小。
    (东财 akshare 接口当前被网络环境阻断, pytdx TCP 直连可用)"""
    from pytdx.hq import TdxHq_API
    from pytdx.params import TDXParams
    from backtest_8wide_ma20_rotation import fix_splits

    code = code[:6]
    market = TDXParams.MARKET_SH if code[0] in "569" else TDXParams.MARKET_SZ
    api = TdxHq_API()
    try:
        if not api.connect("180.153.18.170", 7709, time_out=5):
            return None
        frames = []
        for pg in range(3):
            bars = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, market,
                                         code.encode(), pg * 700, 700)
            if not bars:
                break
            d = api.to_df(bars)
            frames.append(d)
            if len(d) < 700:
                break
        if not frames:
            return None
        f = pd.concat(frames, ignore_index=True)
        f["date"] = pd.to_datetime(f["datetime"]).dt.normalize()
        f = (f[f["date"] >= pd.Timestamp(START)].sort_values("date")
             .drop_duplicates("date"))
        rows = [[str(d.date()), float(v)] for d, v in
                zip(f["date"], f["close"].astype(float))]
        rows, _ = fix_splits(rows)
        return pd.Series({pd.Timestamp(d): v for d, v in rows}).sort_index()
    except Exception:
        return None
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def n12_frac(c: pd.Series) -> pd.Series:
    parts = []
    for n, m in [(10, 9), (10, 12), (12, 9), (12, 12), (14, 9), (14, 12)]:
        e1 = c.ewm(span=n, adjust=False).mean()
        e2 = e1.ewm(span=n, adjust=False).mean()
        e3 = e2.ewm(span=n, adjust=False).mean()
        tr = e3.pct_change() * 100
        sig = tr.rolling(m).mean()
        parts.append(tr > sig)
    return pd.concat(parts, axis=1).mean(axis=1)


def main() -> None:
    print(f"拉取 {len(STOCKS)} 只个股前复权日K ...", flush=True)
    cols, names = {}, {}
    for code, name in STOCKS:
        s = fetch_qfq(code)
        if s is None or len(s) < 300:
            print(f"  {code} {name} 失败/不足, 跳过")
            continue
        cols[code] = s
        names[code] = name
    df = pd.DataFrame(cols).sort_index().dropna()
    print(f"对齐 {len(df)} 天 {df.index[0].date()}~{df.index[-1].date()} | {len(df.columns)} 只\n")

    # ── [1] 截面动量 IC ──
    print("=" * 84)
    print("  [1] 个股截面动量 IC — spearman(过去20日收益, 未来20日收益), 每日一测")
    print("      >0 = 强者恒强(趋势选股有原料) | <0 = 反转市(选趋势股=反向收割)")
    print("=" * 84)
    lb = 20
    rp = df.pct_change(lb)
    rf = df.shift(-lb) / df - 1
    rk_p, rk_f = rp.rank(axis=1), rf.rank(axis=1)
    arr_p, arr_f = rk_p.values, rk_f.values
    idxs, vals = [], []
    for i in range(len(df)):
        a, b = arr_p[i], arr_f[i]
        msk = ~(np.isnan(a) | np.isnan(b))
        if msk.sum() >= 30:
            v = np.corrcoef(a[msk], b[msk])[0, 1]
            if not np.isnan(v):
                idxs.append(df.index[i])
                vals.append(v)
    ic = pd.Series(vals, index=pd.DatetimeIndex(idxs))
    sm = ic.rolling(60).mean().dropna()
    print(f"  有效截面 {len(ic)} 天 | 全期均值 IC {ic.mean():+.4f} | 中位 {ic.median():+.4f}")
    print(f"  为正占比 {(ic > 0).mean() * 100:.1f}% | 滚动60日平滑最新 {sm.iloc[-1]:+.4f}")
    print("\n  分年均值 IC:")
    for y, v in ic.groupby(ic.index.year).mean().items():
        seg = ic[ic.index.year == y]
        print(f"    {y}: {v:+.4f}  (为正占比 {(seg > 0).mean() * 100:.0f}%)")
    verdict = ("正 → 个股存在截面动量, 趋势选股有原料"
               if ic.mean() > 0.02 else
               "≈0 → 个股无可靠动量, 技术趋势选股缺乏原料" if abs(ic.mean()) <= 0.02 else
               "负 → 个股反转市! 选趋势上涨的股 = 系统性买在顶部")
    print(f"\n  ★ 判定: 全期均值 {ic.mean():+.4f} → {verdict}")

    # ── [2] 技术趋势选股回测 ──
    print("\n" + "=" * 84)
    print("  [2] 技术趋势选股回测 — N12簇≥2/3 + 多头排列 → 等权持最强 Top5")
    print("      (每日再平衡近似, 摩擦按换手×0.1%扣; 涨停未模拟=乐观偏差)")
    print("=" * 84)
    frac = pd.DataFrame({c: n12_frac(df[c]) for c in df.columns})
    ma20 = df.rolling(20).mean()
    ma60 = df.rolling(60).mean()
    mom = df / df.shift(20) - 1
    rets = df.pct_change()

    cur, prev_w = 1.0, pd.Series(dtype=float)
    eq = [1.0]
    turns = []
    day_list = list(df.index[61:])
    for j, d in enumerate(day_list):
        f = frac.loc[d]
        px, m20, m60, mm = df.loc[d], ma20.loc[d], ma60.loc[d], mom.loc[d]
        cands = [c for c in df.columns
                 if f[c] >= 2 / 3 and px[c] > m20[c] > m60[c] and not np.isnan(mm[c])]
        if cands:
            top5 = sorted(cands, key=lambda c: -mm[c])[:5]
            w = pd.Series(1 / len(top5), index=top5)
        else:
            w = pd.Series(dtype=float)
        # ★ 收益归属: d 日收盘信号 → 计 d 的【次日】收益(d收盘→d+1收盘)。
        #   错误写法 rets.loc[d] 会把「选股前已发生的当日涨幅」计入 = 前视。
        if j + 1 < len(day_list) and len(w):
            nd = day_list[j + 1]
            r = rets.loc[nd, w.index].mean()
            turn = sum(abs(w.get(c, 0.0) - prev_w.get(c, 0.0))
                       for c in set(w.index) | set(prev_w.index))
            cur *= (1 + r) * (1 - SLIP * turn)
            turns.append(turn)
        prev_w = w
        eq.append(cur)
    ser = pd.Series(eq, index=df.index[60:]).dropna()
    total = (ser.iloc[-1] - 1) * 100
    yrs = (ser.index[-1] - ser.index[0]).days / 365.25
    ann = ((1 + total / 100) ** (1 / yrs) - 1) * 100
    mdd = ((ser - ser.cummax()) / ser.cummax()).min() * 100

    ew = (1 + rets.loc[ser.index].mean(axis=1)).cumprod()
    ew_t = (ew.iloc[-1] - 1) * 100
    ew_mdd = ((ew - ew.cummax()) / ew.cummax()).min() * 100

    print(f"  累计 {total:+.2f}% | 年化 {ann:+.2f}% | 回撤 {mdd:.2f}% | "
          f"日均换手 {np.mean(turns) * 100:.1f}%")
    print(f"  对照: 等权持有同池 {ew_t:+.2f}% / 回撤 {ew_mdd:.2f}%  "
          f"→ 选股 {'跑赢' if total > ew_t else '跑输'} {abs(total - ew_t):.1f}pp")
    y = ser.groupby(ser.index.year).apply(lambda s: (s.iloc[-1] / s.iloc[0] - 1) * 100)
    ye = ew.groupby(ew.index.year).apply(lambda s: (s.iloc[-1] / s.iloc[0] - 1) * 100)
    print(f"\n  {'年份':<8}{'趋势Top5':>12}{'等权持有':>12}")
    for k in sorted(set(y.index) | set(ye.index)):
        print(f"  {k:<8}{y.get(k, float('nan')):>+11.2f}%{ye.get(k, float('nan')):>+11.2f}%")


if __name__ == "__main__":
    main()

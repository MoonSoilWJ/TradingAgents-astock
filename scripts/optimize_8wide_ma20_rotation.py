"""8 只宽基 ETF 动量轮动 — 策略优化 / 网格搜参 / 走查验证。

目的: 在原版规则(站上MA20 + 相对MA20动量第一 + 跌破或跌出第一即清仓)基础上,
      系统性放开若干"病灶参数", 搜索能把 +116.81% 推到多高, 并诚实回答
      "网上声称 +5215.88%(约52倍) 能否达到"。

原版已诊断出的 3 个病灶:
  1) 换仓 554 次 → 每日排名抖动导致极端来回打脸(whipsaw), 手续费+摩擦双杀
  2) 动量定义 close/MA20-1 高频均值回归, 噪音大
  3) "跌出第一即清仓"过于苛刻, 强趋势中被甩下车

可搜索的参数:
  ma_win     : 趋势过滤均线窗口 (原版 20)
  mom_win    : 动量排序窗口     (原版 20, 与均线窗口绑死)
  mom_def    : ma_dev(原版) / ret(过去N日涨幅) / sharpe(涨幅/波动率, 风险调整动量)
  hold_rank  : 持仓排名容忍度   (原版 1 = 跌到第二就走; 2/3 = 放宽)
  buffer     : 换仓缓冲, 新第一动量需超过持仓 buffer 才切换 (原版 0)
  rebal      : 决策频率, 每 N 个交易日决策一次 (原版 1 = 每日)

输出:
  A. 原版基线
  B. 全周期网格 Top 榜(此为"上界/带过拟合", 不可当实盘预期)
  C. 走查验证: 2017-2022 训练选参 → 2023-2026 样本外检验(诚实预期)
  D. 距离 52 倍的缺口归因

用法:
  python3 scripts/optimize_8wide_ma20_rotation.py            # 8 只原池
  python3 scripts/optimize_8wide_ma20_rotation.py --expand   # 扩池(高波动标的)
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from backtest_8wide_ma20_rotation import (  # noqa: E402
    CACHE_DIR,
    benchmark_hs300,
    count_jumps,
    fetch_em_qfq,
    fetch_sina_raw,
    fetch_universe,
)

EXPAND_CACHE = CACHE_DIR / "etf_daily_qfq_expand.json"

# 扩池候选(明确偏离原版8只, 单独标注): 高波动/低相关, 给动量轮动更多"强腿"
EXPAND_UNIVERSE: dict[str, list[str]] = {
    "科创50": ["588000"],
    "券商": ["512880"],
    "半导体": ["512480"],
    "军工": ["512660"],
    "医药生物": ["512010"],
    "中概互联": ["513050"],
    "恒生科技": ["513130"],
    "标普500": ["513500"],
    "日经225": ["513520"],
    "豆粕": ["159985"],
    "原油": ["162411"],
    "红利": ["510880"],
    "有色": ["512400"],
    "证券保险": ["512070"],
}


# --------------------------------------------------------------------------
# 数据结构: 预计算 MA / 动量 数组
# --------------------------------------------------------------------------
class Series:
    __slots__ = ("dates", "close", "pos", "ps", "ma", "mom", "dret")

    def __init__(self, rows: list):
        self.dates = [r[0] for r in rows]
        self.close = [float(r[1]) for r in rows]
        self.pos = {d: i for i, d in enumerate(self.dates)}
        ps = [0.0]
        for c in self.close:
            ps.append(ps[-1] + c)
        self.ps = ps
        self.ma: dict[int, list] = {}
        self.mom: dict[tuple, list] = {}
        self.dret: list[float] = [0.0] * len(self.close)
        for i in range(1, len(self.close)):
            p = self.close[i - 1]
            self.dret[i] = (self.close[i] / p - 1) if p > 0 else 0.0

    def build_ma(self, w: int):
        if w in self.ma:
            return
        n = len(self.close)
        arr: list = [None] * n
        for i in range(w - 1, n):
            arr[i] = (self.ps[i + 1] - self.ps[i + 1 - w]) / w
        self.ma[w] = arr

    def build_mom(self, w: int, mdef: str):
        key = (w, mdef)
        if key in self.mom:
            return
        n = len(self.close)
        arr: list = [None] * n
        if mdef == "ma_dev":
            self.build_ma(w)
            mw = self.ma[w]
            for i in range(n):
                m = mw[i]
                if m and m > 0:
                    arr[i] = self.close[i] / m - 1
        elif mdef == "ret":
            for i in range(w, n):
                b = self.close[i - w]
                if b > 0:
                    arr[i] = self.close[i] / b - 1
        elif mdef == "sharpe":
            # 滚动 w 日累计涨幅 / 日收益标准差 (风险调整动量)
            for i in range(w, n):
                b = self.close[i - w]
                if b <= 0:
                    continue
                r = self.close[i] / b - 1
                seg = self.dret[i - w + 1:i + 1]
                mean = sum(seg) / len(seg)
                var = sum((x - mean) ** 2 for x in seg) / len(seg)
                sd = var ** 0.5
                if sd > 1e-9:
                    arr[i] = r / sd
        elif mdef == "ret_skip":
            # 12-1 式动量: 跳过最近 1 天, 用 (i-1) 日收盘 / (i-1-w) 日收盘 - 1
            # 剔除"决策日单日暴涨"带来的伪动量, 降低噪声
            skip = 1
            for i in range(w + skip, n):
                b = self.close[i - w - skip]
                if b > 0:
                    arr[i] = self.close[i - skip] / b - 1
        self.mom[key] = arr


def build_series(data: dict) -> dict:
    out = {}
    for name, info in data.items():
        if name == "__benchmark":
            continue
        out[name] = Series(info["rows"])
    return out


# --------------------------------------------------------------------------
# 通用回测引擎
# --------------------------------------------------------------------------
def backtest(series: dict, dates: list, *, ma_win: int, mom_win: int,
             mom_def: str, hold_rank: int, buffer: float, rebal: int,
             fee_pct: float = 0.05, mom_thresh: float = -1e9) -> dict:
    """一次回测。dates 为已过滤起点的交易日轴。"""
    for s in series.values():
        s.build_ma(ma_win)
        s.build_mom(mom_win, mom_def)

    fee = fee_pct / 100.0
    mkey = (mom_win, mom_def)
    equity = 1.0
    held: str | None = None
    switches = 0
    curve: list = []
    names = list(series.keys())

    for di, d in enumerate(dates):
        # 1) 盯市: 持仓吃当日收益
        if held is not None:
            s = series[held]
            i = s.pos.get(d)
            if i is not None:
                equity *= 1 + s.dret[i]

        # 2) 是否决策日
        if di % rebal != 0:
            curve.append((d, equity))
            continue

        # 3) 计算候选: 站上均线 + 有动量
        cand: list = []
        held_above = False
        held_mom = None
        for nm in names:
            s = series[nm]
            i = s.pos.get(d)
            if i is None:
                continue
            m = s.ma[ma_win][i]
            mo = s.mom[mkey][i]
            if m is None or mo is None or m <= 0:
                continue
            if mo < mom_thresh:  # 最低动量门槛: 动量不足直接淘汰
                continue
            if s.close[i] > m:  # 有效站上均线
                cand.append((mo, nm))
                if nm == held:
                    held_above = True
                    held_mom = mo

        if cand:
            cand.sort(reverse=True)
            best_mom, best = cand[0]
        else:
            best_mom, best = None, None

        # 4) 决策
        if held is None:
            if best is not None:
                equity *= 1 - fee
                held = best
                switches += 1
        else:
            keep = False
            if held_above and held_mom is not None:
                rank = 1 + sum(1 for mo, _ in cand if mo > held_mom)
                if rank <= hold_rank:
                    keep = True
                elif best_mom is not None and (best_mom - held_mom) <= buffer:
                    keep = True  # 换仓缓冲: 差距不够大就不折腾
            if not keep:
                if best is not None and best != held:
                    equity *= (1 - fee) * (1 - fee)  # 卖 + 买
                    held = best
                    switches += 1
                elif best is None:
                    equity *= 1 - fee
                    held = None

        curve.append((d, equity))

    # 统计
    yearly_eq: dict[str, float] = {}
    for d, eq in curve:
        yearly_eq[d[:4]] = eq
    years = sorted(yearly_eq)
    year_ret: dict[str, float] = {}
    prev = None
    for y in years:
        year_ret[y] = (yearly_eq[y] - 1) if prev is None else (yearly_eq[y] / yearly_eq[prev] - 1)
        prev = y

    peak, mdd = 0.0, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            mdd = min(mdd, eq / peak - 1)

    n_years = max(len(curve) / 244.0, 0.1)
    cagr = equity ** (1 / n_years) - 1 if equity > 0 else -1

    return {
        "final_pct": round((equity - 1) * 100, 2),
        "equity": equity,
        "cagr_pct": round(cagr * 100, 2),
        "mdd_pct": round(mdd * 100, 2),
        "switches": switches,
        "yearly": {y: round(v * 100, 2) for y, v in year_ret.items()},
        "neg_years": [y for y, v in year_ret.items() if v < 0],
        "mar": round((cagr / abs(mdd)) if mdd < -1e-9 else 0, 2),
        "curve": curve,
    }


def fetch_expand(fresh: bool = False) -> dict:
    if EXPAND_CACHE.exists() and not fresh:
        return json.loads(EXPAND_CACHE.read_text(encoding="utf-8"))
    out = {}
    for name, codes in EXPAND_UNIVERSE.items():
        for code in codes:
            rows = fetch_em_qfq(code)
            src = "em-qfq"
            if rows is None:
                rows = fetch_sina_raw(code)
                src = "sina-raw"
            if not rows:
                print(f"  [扩池失败] {name} {code}")
                continue
            out[name] = {"code": code, "rows": rows, "src": src}
            jm = count_jumps(rows)
            print(f"  [OK] {name:8s} {code} [{src}] 起{rows[0][0]} K线{len(rows)}"
                  + (f"  ⚠跳变{jm}" if jm else ""))
            break
    EXPAND_CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--split", default="2023-01-01", help="走查训练/验证分界")
    ap.add_argument("--fee", type=float, default=0.05)
    ap.add_argument("--expand", action="store_true", help="扩池(加14只高波动ETF)")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--final", action="store_true",
                    help="只跑最终推荐方案(rank3 + 每周决策 + 1%%缓冲)并输出逐年表")
    args = ap.parse_args()

    print("=" * 78)
    print("8 只宽基 ETF 动量轮动 — 优化搜参 & 走查验证")
    print("=" * 78)

    data = fetch_universe(fresh=False)
    if args.expand:
        print("\n[扩池] 抓取额外高波动标的...")
        data = dict(data)
        data.update(fetch_expand())

    # 数据质量校验: 未复权的拆分/份额折算会造成假涨假跌, 直接污染回测
    bad = []
    for nm, info in data.items():
        if nm == "__benchmark":
            continue
        j = count_jumps(info["rows"])
        if j and info.get("src") != "em-qfq":
            bad.append(f"{nm}({info['code']},{info.get('src')},跳变{j})")
    if bad:
        print("\n  ⚠ 数据质量告警(疑似未复权断点): " + ", ".join(bad))

    series = build_series(data)
    all_dates = sorted({d for s in series.values() for d in s.dates if d >= args.start})
    print(f"\n[数据] 标的 {len(series)} 只  交易日 {len(all_dates)} "
          f"({all_dates[0]} ~ {all_dates[-1]})")

    bm = benchmark_hs300(data, args.start)

    # ---------------- 最终推荐方案 (--final) ----------------
    if args.final:
        FINAL = dict(ma_win=20, mom_win=20, mom_def="ma_dev",
                     hold_rank=3, buffer=0.01, rebal=5)
        r = backtest(series, all_dates, fee_pct=args.fee, **FINAL)
        b = backtest(series, all_dates, ma_win=20, mom_win=20, mom_def="ma_dev",
                     hold_rank=1, buffer=0.0, rebal=1, fee_pct=args.fee)
        print("\n" + "=" * 78)
        print("[最终推荐方案]  8只宽基 / MA20趋势过滤 / 相对MA20动量排序")
        print("  改动1: 持仓跌到第3名之外才换(原版跌出第1就清仓)")
        print("  改动2: 每5个交易日(每周)决策一次(原版每日)")
        print("  改动3: 新第1动量需超过持仓1个百分点才换仓(换仓缓冲)")
        print("=" * 78)
        print(f"  {'年份':<8}{'优化后':>12}{'原版':>12}{'沪深300':>12}")
        for y in sorted(r["yearly"]):
            print(f"  {y:<8}{r['yearly'][y]:>11.2f}%{b['yearly'].get(y, 0):>11.2f}%"
                  f"{bm.get('yearly', {}).get(y, 0):>11.2f}%")
        print("-" * 78)
        print(f"  {'累计':<8}{r['final_pct']:>11.2f}%{b['final_pct']:>11.2f}%"
              f"{bm.get('final_pct', 0):>11.2f}%")
        print(f"\n  年化 {r['cagr_pct']:.2f}% (原版 {b['cagr_pct']:.2f}%)   "
              f"最大回撤 {r['mdd_pct']:.2f}% (原版 {b['mdd_pct']:.2f}%)")
        print(f"  换仓 {r['switches']} 次 (原版 {b['switches']} 次)   "
              f"亏损年 {r['neg_years'] or '无'} (原版 {b['neg_years'] or '无'})")
        op = CACHE_DIR / "rotation_final_8wide.json"
        op.write_text(json.dumps(
            {"params": FINAL, "optimized": {k: v for k, v in r.items() if k != "curve"},
             "baseline": {k: v for k, v in b.items() if k != "curve"},
             "benchmark": bm}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[保存] {op}")
        return

    # ---------------- A. 原版基线 ----------------
    base = backtest(series, all_dates, ma_win=20, mom_win=20, mom_def="ma_dev",
                    hold_rank=1, buffer=0.0, rebal=1, fee_pct=args.fee)
    print("\n" + "-" * 78)
    print("[A] 原版基线 (ma20 / mom20 / ma_dev / 跌出第一即清仓 / 每日决策)")
    print(f"    累计 {base['final_pct']:+.2f}%   年化 {base['cagr_pct']:+.2f}%   "
          f"回撤 {base['mdd_pct']:.2f}%   换仓 {base['switches']}   "
          f"亏损年 {len(base['neg_years'])}")

    # ---------------- B. 全周期网格 ----------------
    GRID = {
        "ma_win": [10, 20, 30, 60],
        "mom_win": [5, 10, 20, 40, 60],
        "mom_def": ["ma_dev", "ret", "sharpe"],
        "hold_rank": [1, 2, 3],
        "buffer": [0.0, 0.01, 0.02, 0.04],
        "rebal": [1, 5],
    }
    keys = list(GRID)
    combos = list(itertools.product(*(GRID[k] for k in keys)))
    print(f"\n[B] 全周期网格搜索  {len(combos)} 组合 ...")
    t0 = time.time()
    results = []
    for ci, vals in enumerate(combos):
        p = dict(zip(keys, vals))
        r = backtest(series, all_dates, fee_pct=args.fee, **p)
        r["params"] = p
        results.append(r)
        if (ci + 1) % 300 == 0:
            print(f"    ...{ci+1}/{len(combos)}  ({time.time()-t0:.0f}s)")
    print(f"    完成, 耗时 {time.time()-t0:.0f}s")

    by_ret = sorted(results, key=lambda x: -x["equity"])
    print(f"\n  Top {args.top} (按累计收益, ⚠含参数过拟合, 是上界不是预期):")
    print(f"    {'累计%':>11}{'年化%':>8}{'回撤%':>8}{'MAR':>6}{'换仓':>6}"
          f"{'亏损年':>6}  参数")
    for r in by_ret[:args.top]:
        p = r["params"]
        ps = (f"ma{p['ma_win']}/mom{p['mom_win']}/{p['mom_def']}/"
              f"rank{p['hold_rank']}/buf{p['buffer']}/reb{p['rebal']}")
        print(f"    {r['final_pct']:>11.2f}{r['cagr_pct']:>8.2f}{r['mdd_pct']:>8.2f}"
              f"{r['mar']:>6.2f}{r['switches']:>6}{len(r['neg_years']):>6}  {ps}")

    by_mar = sorted(results, key=lambda x: -x["mar"])
    print(f"\n  Top 5 (按 MAR = 年化/回撤, 更稳健的选参标准):")
    for r in by_mar[:5]:
        p = r["params"]
        ps = (f"ma{p['ma_win']}/mom{p['mom_win']}/{p['mom_def']}/"
              f"rank{p['hold_rank']}/buf{p['buffer']}/reb{p['rebal']}")
        print(f"    {r['final_pct']:>11.2f}{r['cagr_pct']:>8.2f}{r['mdd_pct']:>8.2f}"
              f"{r['mar']:>6.2f}{r['switches']:>6}{len(r['neg_years']):>6}  {ps}")

    # ---------------- C. 走查验证 ----------------
    tr_dates = [d for d in all_dates if d < args.split]
    te_dates = [d for d in all_dates if d >= args.split]
    print("\n" + "-" * 78)
    print(f"[C] 走查验证  训练 {tr_dates[0]}~{tr_dates[-1]} ({len(tr_dates)}日)  "
          f"验证 {te_dates[0]}~{te_dates[-1]} ({len(te_dates)}日)")
    train = []
    for vals in combos:
        p = dict(zip(keys, vals))
        r = backtest(series, tr_dates, fee_pct=args.fee, **p)
        train.append((r, p))
    train.sort(key=lambda x: -x[0]["equity"])

    print("\n  训练段 Top10 → 各自样本外(OOS)表现:")
    print(f"    {'IS累计%':>10}{'OOS累计%':>11}{'OOS年化%':>10}{'OOS回撤%':>10}  参数")
    oos_list = []
    for r_is, p in train[:10]:
        r_oos = backtest(series, te_dates, fee_pct=args.fee, **p)
        oos_list.append(r_oos)
        ps = (f"ma{p['ma_win']}/mom{p['mom_win']}/{p['mom_def']}/"
              f"rank{p['hold_rank']}/buf{p['buffer']}/reb{p['rebal']}")
        print(f"    {r_is['final_pct']:>10.2f}{r_oos['final_pct']:>11.2f}"
              f"{r_oos['cagr_pct']:>10.2f}{r_oos['mdd_pct']:>10.2f}  {ps}")

    best_is_p = train[0][1]
    best_oos = oos_list[0]
    base_oos = backtest(series, te_dates, ma_win=20, mom_win=20, mom_def="ma_dev",
                        hold_rank=1, buffer=0.0, rebal=1, fee_pct=args.fee)
    avg_oos = sum(x["final_pct"] for x in oos_list) / len(oos_list)
    print(f"\n  训练最优参数 OOS 累计 {best_oos['final_pct']:+.2f}%  "
          f"vs 原版同段 {base_oos['final_pct']:+.2f}%  "
          f"(Top10 OOS 均值 {avg_oos:+.2f}%)")

    # 用训练最优参数跑全周期(实盘可复现口径: 参数在2023前定死)
    wf_full = backtest(series, all_dates, fee_pct=args.fee, **best_is_p)
    print(f"  该参数全周期(2017起, 参数于2023前定死): 累计 {wf_full['final_pct']:+.2f}%  "
          f"年化 {wf_full['cagr_pct']:+.2f}%  回撤 {wf_full['mdd_pct']:.2f}%")
    print("  逐年: " + "  ".join(f"{y}:{v:+.1f}%" for y, v in sorted(wf_full["yearly"].items())))

    # ---------------- E. 单因素消融 ----------------
    print("\n" + "-" * 78)
    print("[E] 单因素消融 — 每个改动各自贡献多少 (其余参数保持原版)")
    ABL = [
        ("原版(基线)", dict(ma_win=20, mom_win=20, mom_def="ma_dev",
                            hold_rank=1, buffer=0.0, rebal=1)),
        ("① 只改: 每周决策(reb5)", dict(ma_win=20, mom_win=20, mom_def="ma_dev",
                                        hold_rank=1, buffer=0.0, rebal=5)),
        ("② 只改: 跌到第3名才走(rank3)", dict(ma_win=20, mom_win=20, mom_def="ma_dev",
                                              hold_rank=3, buffer=0.0, rebal=1)),
        ("③ 只改: 换仓缓冲2%(buf)", dict(ma_win=20, mom_win=20, mom_def="ma_dev",
                                        hold_rank=1, buffer=0.02, rebal=1)),
        ("④ 只改: 趋势用MA60", dict(ma_win=60, mom_win=20, mom_def="ma_dev",
                                    hold_rank=1, buffer=0.0, rebal=1)),
        ("⑤ 只改: 风险调整动量(sharpe)", dict(ma_win=20, mom_win=20, mom_def="sharpe",
                                              hold_rank=1, buffer=0.0, rebal=1)),
        ("★ ①+② 组合(走查最优)", dict(ma_win=20, mom_win=20, mom_def="ma_dev",
                                       hold_rank=3, buffer=0.0, rebal=5)),
    ]
    print(f"    {'方案':<30}{'累计%':>10}{'年化%':>8}{'回撤%':>8}{'换仓':>6}{'亏损年':>7}")
    abl_out = []
    for label, p in ABL:
        r = backtest(series, all_dates, fee_pct=args.fee, **p)
        abl_out.append({"label": label, "params": p, "final_pct": r["final_pct"],
                        "cagr_pct": r["cagr_pct"], "mdd_pct": r["mdd_pct"],
                        "switches": r["switches"], "neg_years": r["neg_years"],
                        "yearly": r["yearly"]})
        print(f"    {label:<30}{r['final_pct']:>10.2f}{r['cagr_pct']:>8.2f}"
              f"{r['mdd_pct']:>8.2f}{r['switches']:>6}{len(r['neg_years']):>7}")

    # ---------------- D. 缺口归因 ----------------
    CLAIM = 5215.88
    n_years = len(all_dates) / 244.0
    need_cagr = ((1 + CLAIM / 100) ** (1 / n_years) - 1) * 100
    top_eq = by_ret[0]
    print("\n" + "=" * 78)
    print("[D] 距离网上声称 +5215.88%(约52倍) 的缺口")
    print("=" * 78)
    print(f"  声称需年化: {need_cagr:.2f}%   (期间 {n_years:.1f} 年)")
    print(f"  原版实测年化: {base['cagr_pct']:.2f}%  (累计 {base['final_pct']:+.2f}%)")
    print(f"  全周期最优(过拟合上界)年化: {top_eq['cagr_pct']:.2f}%  "
          f"(累计 {top_eq['final_pct']:+.2f}%)")
    print(f"  走查诚实预期年化: {wf_full['cagr_pct']:.2f}%  "
          f"(累计 {wf_full['final_pct']:+.2f}%)")
    gap = (1 + CLAIM / 100) / (1 + top_eq["final_pct"] / 100)
    print(f"\n  ⇒ 即便把所有参数在全周期上'开天眼'调到最优, 仍差 {gap:.1f} 倍。")
    print(f"  ⇒ 沪深300 同期 {bm.get('final_pct', 0):+.2f}%")

    out = {
        "baseline": {k: v for k, v in base.items()},
        "grid_top": [{"params": r["params"], "final_pct": r["final_pct"],
                      "cagr_pct": r["cagr_pct"], "mdd_pct": r["mdd_pct"],
                      "mar": r["mar"], "switches": r["switches"],
                      "neg_years": r["neg_years"], "yearly": r["yearly"]}
                     for r in by_ret[:30]],
        "grid_top_mar": [{"params": r["params"], "final_pct": r["final_pct"],
                          "cagr_pct": r["cagr_pct"], "mdd_pct": r["mdd_pct"],
                          "mar": r["mar"]} for r in by_mar[:10]],
        "walkforward": {
            "split": args.split, "best_is_params": best_is_p,
            "oos_best": best_oos, "oos_baseline": base_oos,
            "oos_top10_avg_pct": round(avg_oos, 2),
            "full_with_is_params": wf_full,
        },
        "ablation": abl_out,
        "benchmark": bm,
        "claim": {"final_pct": CLAIM, "need_cagr_pct": round(need_cagr, 2)},
        "universe": sorted(series.keys()),
        "config": {"start": args.start, "fee": args.fee, "expand": args.expand},
    }
    tag = "expand" if args.expand else "8wide"
    op = CACHE_DIR / f"rotation_optimize_{tag}.json"
    op.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[保存] {op}")


if __name__ == "__main__":
    main()

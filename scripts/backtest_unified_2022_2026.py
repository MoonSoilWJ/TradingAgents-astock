#!/usr/bin/env python3
"""大一统策略 2022-2026 回测 + 自动优化 + 权益曲线图。

进攻腿 = A选股(hybrid-A, regime+滚动优质池) + 14:40确认 + TRIX卖  ← 与聚宽对齐
防守腿 = 黄金/国债等权月度再平衡(Overlay剩余资金模式)
优化目标: 弱市(2022/2023)也能盈利, 强市(2024/2025/2026)更强。

窗口: 2022-06-15 ~ 2026-07-31 (A选股无偏数据可达起点; 2022上半年无 unbiased 5min 故不含)
防守资产本地: 518880/511090/511260 (510880红利本地缺, 聚宽可补)

用法: python3 scripts/backtest_unified_2022_2026.py
"""
from __future__ import annotations
import argparse
import json
import statistics as st
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from backtest_t0_hybrid_sell import run_strategy  # noqa: E402
from backtest_recent100_live_vs_b_idle import apply_confirm  # noqa: E402
from quality_pool import build_picks_hybrid, regime_on_date, BLACKLIST_CODES  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402
from backtest_t0_today1 import rank_by_today_gain, passes_gain_filter  # noqa: E402
from dynamic_pool import month_pools_for_range  # noqa: E402

def _prev_month(ym):
    """使用月 -> 上月(上月末 pool_as_of 用于当月, 消除未来函数)。"""
    y, m = map(int, ym.split("-"))
    m -= 1
    if m == 0:
        m = 12
        y -= 1
    return f"{y}-{m:02d}"

CACHE = Path.home() / ".tradingagents/cache/t0_5min"
ALIGNED = CACHE / "aligned_live_4y.json"
DENSE = [CACHE / "tdx_5min_pre2024.json",
         CACHE / "tdx_5min_2y.json",
         CACHE / "tdx_5min_auto.json"]
FULL_DAILY = CACHE / "full_daily_2015_2026.json"
DENSE_START = "2022-06-15"
START = "2022-06-15"
END = "2026-07-31"
FEE = 0.0003
DEFENSE = ["518880", "511090", "511260"]   # 本地可用防守资产(红利510880缺)

WEAK = ["2022", "2023"]
STRONG = ["2024", "2025", "2026"]


# --------------------------------------------------------------------------
def load():
    cache = json.loads(ALIGNED.read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    proxy = cache["proxy_klines"]
    etf_daily = cache["etf_daily"]
    # 合并 full_daily: 含 auto 层 59只宽基日K(ALIGNED 未覆盖, 否则对齐聚宽池选不出标的)
    full = json.loads(FULL_DAILY.read_text(encoding="utf-8"))
    for c, rec in full.items():
        if c not in etf_daily:
            etf_daily[c] = rec
    etf_5min: dict = {}
    for p in DENSE:
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))["etf_5min"]
        for c, days in d.items():
            etf_5min.setdefault(c, {}).update(days)
    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]
    return all_dates, proxy, etf_daily, etf_5min, etf_list


def build_attack_A(all_dates, proxy, etf_daily, etf_5min, etf_list, rank_time="14:40",
                   pool_codes=None, scheme="jq", pool_fn=None):
    """进攻腿选股 + 双时点确认 + TRIX 卖。

    pool_codes: 候选池白名单(去后缀 code 集合)。若给定, 则 etf_list 仅保留这些 code
    → 用于与聚宽实盘 AUTO_ETFS(59只宽基)对齐。
    scheme:
      "jq"     = faithful 复刻聚宽 scan_at_1440(无品类过滤 + 趋势门禁 + 动量优质池)
      "hybrid" = 原 scheme A(品类过滤 + skip_choppy + 训练门槛优质池)
    """
    if pool_codes is not None and pool_fn is None:
        etf_list = [e for e in etf_list if e["code"] in pool_codes]
    post = [d for d in all_dates if d >= DENSE_START]
    if scheme == "jq":
        picks = build_picks_jq(post, etf_list, etf_daily, etf_5min, all_dates, proxy,
                               signal_time=rank_time, pool_fn=pool_fn)
        tag = "聚宽等价(无品类过滤+趋势门禁)"
    else:
        picks = build_picks_hybrid(
            post, etf_list, etf_daily, etf_5min, all_dates, proxy,
            lookback=30, warmup=30, signal_times=[rank_time],
        )
        tag = "hybrid-A(品类过滤+skip)"
    picks, rej = apply_confirm(picks, etf_daily, etf_5min, "14:40")
    res = run_strategy("trix", post, all_dates, picks, etf_5min, FEE,
                       signal_time=rank_time)
    trades = res["trades"] if res else []
    print(f"[进攻] {tag} + {rank_time}排名 + 14:40确认 + TRIX | "
          f"候选池 {len(etf_list)}只 | 成交 {len(trades)} 笔, "
          f"确认否决 {rej} 天", flush=True)
    return trades


def build_picks_jq(eval_dates, pool, etf_daily, etf_5min, all_dates, proxy,
                   lookback=30, topn=25, signal_time="14:40", gate_ma=20,
                   pool_fn=None):
    """Faithful 复刻聚宽 scan_at_1440 选股(对齐实盘必需)。

    与本地 scheme A 的关键差异(正是这些差异导致此前笔数/收益对不上聚宽):
    - 无 ALLOWED/EXCLUDE 品类过滤(聚宽直接选涨幅 Top1)
    - 无 skip_choppy(趋势 regime 也交易; 本地 scheme A 趋势天反被跳过)
    - 优质池用简单动量(过去 lookback 天累计涨幅 Top topn), 非训练门槛
    - 趋势门禁(当日收盘>前 gate_ma 天 MA)对所有 regime 生效, 同聚宽 passes_gate
    """
    def close_above_ma(code, day):
        info = etf_daily.get(code)
        if not info:
            return True
        rets = info["returns"]
        idm = {r["date"]: i for i, r in enumerate(rets)}
        if day not in idm:
            return True
        idx = idm[day]
        if idx < gate_ma:
            return True
        window = rets[idx - gate_ma: idx + 1]   # 含当日共 gate_ma+1 根
        ma = sum(r["close"] for r in window[:-1]) / gate_ma
        today = window[-1]["close"]
        if ma <= 0 or today <= 0:
            return True
        return today > ma

    def momentum_top(day):
        idx = all_dates.index(day)
        start = max(0, idx - lookback + 1)
        wset = set(all_dates[start: idx + 1])
        scored = []
        base = pool_fn(day) if pool_fn else pool
        for e in base:
            code = e["code"]
            info = etf_daily.get(code)
            if not info:
                continue
            rets = info["returns"]
            closes = [r["close"] for r in rets if r["date"] in wset]
            if len(closes) < 2:
                continue
            mom = (closes[-1] - closes[0]) / closes[0] * 100
            scored.append((mom, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:topn]]

    picks = {}
    for day in eval_dates:
        reg = regime_on_date(proxy, day)
        mode = (reg or {}).get("mode", "中性")
        qpool = momentum_top(day) if mode in ("趋势", "震荡") else (pool_fn(day) if pool_fn else pool)
        scores = rank_by_today_gain(qpool, etf_daily, etf_5min, day, signal_time)
        chosen = None
        for g, e in scores:
            if not passes_gain_filter(g):
                continue
            if e["code"] in BLACKLIST_CODES:
                continue
            if not close_above_ma(e["code"], day):
                continue
            chosen = (e["code"], g, e.get("name") or e["code"])
            break
        picks[(signal_time, day)] = chosen
    return picks


def load_jq_pool():
    """聚宽实盘等价进攻池(59只宽基, 不含原油/商品/主题跨境)。"""
    import json as _json
    p = HERE / "jq_attack_pool.json"
    if not p.exists():
        return None
    return set(_json.loads(p.read_text(encoding="utf-8"))["codes"])


def run_defense(full_daily, codes, all_dates, start, end):
    d = json.loads(full_daily.read_text(encoding="utf-8"))
    closes = {}
    for c in codes:
        recs = d[c]["returns"]
        closes[c] = {r["date"]: r["close"] for r in recs}
    n = len(codes)
    w = {c: 1.0 / n for c in codes}
    idx_map = {dd: i for i, dd in enumerate(all_dates)}
    daily = {}
    last_month = None
    for day in all_dates:
        if day < start or day > end:
            continue
        md = day[:7]
        r = {}
        ok = True
        for c in codes:
            s = closes[c]
            if day not in s:
                ok = False
                break
            i = idx_map[day] - 1
            while i >= 0:
                if all_dates[i] in s:
                    r[c] = s[day] / s[all_dates[i]] - 1
                    break
                i -= 1
            else:
                ok = False
                break
        if not ok:
            daily[day] = 0.0
            continue
        if last_month is None or md != last_month:
            w = {c: 1.0 / n for c in codes}
            last_month = md
        dr = sum(w[c] * r[c] for c in codes)
        if abs(dr) > 0.10:
            dr = 0.0
        daily[day] = dr
        for c in codes:
            w[c] = w[c] * (1 + r[c])
        tot = sum(w.values())
        w = {c: w[c] / tot for c in codes}
    return daily


def simulate(trades, def_daily, all_dates, start, end, attack_split,
             mode="fixed", dd_thr=None, regime_on=None):
    by_sell, by_sig = {}, {}
    for t in trades:
        by_sell.setdefault(t["sell_date"], []).append(t)
        by_sig.setdefault(t["signal_date"], []).append(t)
    open_pos = []
    eq = 1.0
    peak = 1.0
    curve = []
    for day in all_dates:
        if day < start or day > end:
            continue
        dr = def_daily.get(day, 0.0)
        # 平仓(实现收益)
        for t in by_sell.get(day, []):
            if t["etf"] in open_pos:
                k = len(open_pos)
                open_pos.remove(t["etf"])
                ret = t["return_pct"] / 100.0
                eq *= (1 + (attack_split / k) * ret)
        k = len(open_pos)
        # 有效攻击仓位(dd模式: 深回撤时暂停开新仓)
        dd = (eq / peak - 1) * 100
        if mode == "fixed":
            eff = attack_split
            can_open = True
        elif mode == "dd":
            eff = attack_split
            can_open = (dd > dd_thr)   # 深回撤(dd很负)时不新开 → 留防守
        elif mode == "regime":
            reg = regime_on(day) if regime_on else "中性"
            eff = attack_split if reg == "趋势" else min(attack_split, 0.4)
            can_open = True
        else:
            eff = attack_split
            can_open = True
        if k == 0:
            eq *= (1 + dr)
        else:
            eq = eq * (1 - eff) * (1 + dr) + eq * eff
        peak = max(peak, eq)
        # 新开仓
        for t in by_sig.get(day, []):
            if t["etf"] not in open_pos and can_open:
                open_pos.append(t["etf"])
        curve.append((day, eq))
    return curve


def metrics(curve):
    if not curve:
        return 0, {}, 0, 0
    total = (curve[-1][1] / curve[0][1] - 1) * 100
    eqmap = {d: e for d, e in curve}
    years = sorted({d[:4] for d, _ in curve})
    yr = {}
    for y in years:
        days = [d for d, _ in curve if d[:4] == y]
        if days:
            yr[y] = (eqmap[days[-1]] / eqmap[days[0]] - 1) * 100
    peak = curve[0][1]
    mdd = 0.0
    for _, e in curve:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    mdd *= 100
    rets = [curve[i + 1][1] / curve[i][1] - 1 for i in range(len(curve) - 1)]
    mu, sd = (st.mean(rets), st.pstdev(rets)) if len(rets) > 1 else (0, 0)
    sharpe = (mu / sd * 244 ** 0.5) if sd > 0 else 0.0
    return total, yr, mdd, sharpe


def sweep_pool(all_dates, proxy, etf_daily, etf_5min, etf_list, def_daily, args):
    """扫描月度轮动规则关键点, 在 22-26 无偏段找最大收益的固定轮动规则。

    只 load 一次 5min; 对内层每个规则配置重算选股(build_attack_A) + 纯进攻
    模拟(fixed split=1.0, 用户目标=最大收益) + metrics, 按 total 排序。
    最优规则写回 CACHE/sweep_pool_result.json 供聚宽 14-21 验证照搬。
    """
    from t0_etf_list import get_all_market_etf_lof as _gam
    mkt = {m["code"]: m for m in _gam()}
    universe = set(etf_daily.keys())   # 确保候选均有日K, pool_as_of 自然收窄

    # 规则网格: 主题拦截 × 流动性门槛 × 祖父保留(上市天数固定120)
    grid = []
    for ds in (True, False):
        for mat in (10_000_000, 30_000_000, 100_000_000):
            for us in (True, False):
                grid.append((ds, mat, us))
    print(f"[sweep] 规则网格 {len(grid)} 组合 | universe={len(universe)}只(确保有日K) "
          f"| scheme={args.scheme}", flush=True)

    # 预生成各配置月度池(一次性, 避免重复扫 universe)
    mp_grid = {}
    for cfg in grid:
        ds, mat, us = cfg
        mp_grid[cfg] = month_pools_for_range(
            _prev_month(START[:7]), END[:7], universe=universe,
            drop_sector=ds, min_avg_turnover=mat, use_seed=us)
    print(f"[sweep] 月度池预生成完毕 ({len(grid)} 份)", flush=True)

    results = []
    for cfg in grid:
        ds, mat, us = cfg
        mp = mp_grid[cfg]
        pool_fn = lambda day, mp=mp: [mkt[c] for c in mp.get(_prev_month(day[:7]), set()) if c in mkt]
        trades = build_attack_A(all_dates, proxy, etf_daily, etf_5min, etf_list,
                                rank_time=args.rank_time, pool_codes=None,
                                scheme=args.scheme, pool_fn=pool_fn)
        curve = simulate(trades, def_daily, all_dates, START, END, 1.0, "fixed")
        total, yr, mdd, sharpe = metrics(curve)
        results.append({"cfg": cfg, "total": total, "yr": yr, "mdd": mdd,
                        "sharpe": sharpe, "n": len(trades), "curve": curve})
        print(f"[sweep] drop_sector={str(ds):>5} turn={mat/1e6:>3.0f}M "
              f"seed={str(us):>5} | 总 {total:>8.1f}% MDD {mdd:>6.1f}% "
              f"笔 {len(trades):>4} 夏普 {sharpe:.2f}", flush=True)

    results.sort(key=lambda r: r["total"], reverse=True)
    years = sorted({y for r in results for y in r["yr"]})
    print("\n=== 月度轮动规则扫描 (按 22-26 总收益降序) ===")
    hdr = (f"{'drop':>5}{'turn(M)':>8}{'seed':>6}"
           + "".join(f"{y:>9}" for y in years) + f"{'总%':>10}{'MDD%':>8}{'笔':>6}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        ds, mat, us = r["cfg"]
        row = (f"{str(ds):>5}{mat/1e6:>8.0f}{str(us):>6}")
        for y in years:
            row += f"{r['yr'].get(y, 0.0):>+9.1f}"
        row += f"{r['total']:>10.1f}{r['mdd']:>8.1f}{r['n']:>6}"
        print(row)

    best = results[0]
    bds, bmat, bus = best["cfg"]
    print(f"\n★ 22-26 最大收益固定规则: drop_sector={bds} "
          f"min_avg_turnover={bmat/1e6:.0f}M use_seed={bus} "
          f"(上市天数=120 固定)")
    print(f"   总 {best['total']:.1f}% | MDD {best['mdd']:.1f}% | "
          f"夏普 {best['sharpe']:.2f} | 笔 {best['n']}")
    print("   逐年:", "  ".join(f"{y}:{best['yr'].get(y,0):+.1f}%"
          for y in years))

    # 写回最优规则(供聚宽 14-21 验证照搬)
    out = CACHE / "sweep_pool_result.json"
    payload = {
        "best_total_2022_2026": best["total"],
        "best_mdd": best["mdd"],
        "best_sharpe": best["sharpe"],
        "best_trades": best["n"],
        "best_yearly": best["yr"],
        "rule": {
            "drop_sector": bds,
            "min_avg_turnover": bmat,
            "use_seed": bus,
            "min_listing_days": 120,
        },
        "all_results": [
            {"drop_sector": c[0], "min_avg_turnover": c[1], "use_seed": c[2],
             "total": r["total"], "mdd": r["mdd"], "sharpe": r["sharpe"],
             "n": r["n"], "yr": r["yr"]}
            for r, c in zip(results, [x["cfg"] for x in results])
        ],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[sweep] 最优规则已保存: {out}  ← 搬到聚宽跑 14-21 验证")

    if args.plot:
        out_png = HERE.parent / "unified_2022_2026_equity.png"
        def_only = simulate([], def_daily, all_dates, START, END, 0.0, "fixed")
        plot(all_dates, {"mode": "sweep-best", "sp": 1.0, "thr": None,
                         "total": best["total"], "yr": best["yr"], "mdd": best["mdd"],
                         "sharpe": best["sharpe"], "curve": best["curve"]},
             best["curve"], def_only, out_png)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plot", action="store_true", help="生成权益曲线 PNG")
    ap.add_argument("--rank-time", default="14:40",
                    help="进攻排名时点(对齐聚宽=14:40)")
    ap.add_argument("--no-jq-pool", action="store_true",
                    help="不用聚宽等价池, 退回全量 get_all_t0_etfs(含原油/商品)")
    ap.add_argument("--scheme", default="jq", choices=["jq", "hybrid"],
                    help="选股口径: jq=聚宽等价(默认,对齐实盘); hybrid=原scheme A")
    ap.add_argument("--dynamic-pool", action="store_true",
                    help="用 refresh_t0_pool 规则按月份回溯动态池(替代写死59白名单)")
    ap.add_argument("--sweep-pool", action="store_true",
                    help="扫描月度轮动规则关键点(drop_sector/流动性门槛/seed祖父), "
                         "在22-26无偏段找最大收益的固定轮动规则")
    args = ap.parse_args()

    # 默认对齐聚宽实盘: 候选池=59只宽基(jq_attack_pool.json), 不含原油/商品
    jq_pool = None if args.no_jq_pool else load_jq_pool()
    if jq_pool is None and not args.dynamic_pool:
        print("[池子] 用全量 get_all_t0_etfs()(含原油/商品, 非聚宽对齐口径)",
              flush=True)
    elif jq_pool is not None and not args.dynamic_pool:
        print(f"[池子] 对齐聚宽实盘: 候选池白名单 {len(jq_pool)} 只宽基",
              flush=True)

    all_dates, proxy, etf_daily, etf_5min, etf_list = load()

    # 动态规则池: 按月份回溯 refresh_t0_pool 规则(真T+0 + 宽基 + 上市≥120天 + 成交≥3000万)
    pool_fn = None
    if args.dynamic_pool:
        # ★全市场扫描: 用全市场 ETF/LOF(5941) 作 universe, 由 dynamic_pool 的
        #   _FULL(数据覆盖≈405) + _attack_filter(剔除511xxx/DEFENSE_POOL) + 规则门槛
        #   自然收窄到可交易真T+0宽基, 对齐聚宽 compute_attack_universe 的
        #   get_all_securities 全市场扫描(之前只用 etf_list=162 收窄, 漏掉全市场票)。
        from t0_etf_list import get_all_market_etf_lof as _gam
        mkt = {m["code"]: m for m in _gam()}
        universe = set(mkt.keys())
        mp = month_pools_for_range(_prev_month(START[:7]), END[:7], universe=universe)
        by_code = mkt  # 覆盖全市场(含名称), 不再限于 etf_list 的 162
        pool_fn = lambda day: [by_code[c] for c in mp.get(_prev_month(day[:7]), set()) if c in by_code]
        jq_pool = None
        outp = CACHE / "dynamic_pool_backtest.json"
        outp.write_text(json.dumps({ym: sorted(s) for ym, s in mp.items()},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[池子] ★动态规则池(全市场扫描, 规则维护, 非写死): "
              f"{START[:7]}={len(mp[START[:7]])}只 → "
              f"{END[:7]}={len(mp[END[:7]])}只 | 明细 {outp}", flush=True)
        for ym, s in mp.items():
            print(f"    {ym}: {len(s)}", flush=True)

    # 防守腿日收益(Overlay 用, 需在 sweep 之前算好)
    def_daily = run_defense(FULL_DAILY, DEFENSE, all_dates, START, END)

    # ★扫描月度轮动规则关键点: 在 22-26 无偏段找最大收益的固定规则(替代单次build)
    if args.sweep_pool:
        sweep_pool(all_dates, proxy, etf_daily, etf_5min, etf_list,
                   def_daily, args)
        return

    trades = build_attack_A(all_dates, proxy, etf_daily, etf_5min, etf_list,
                            rank_time=args.rank_time, pool_codes=jq_pool,
                            scheme=args.scheme, pool_fn=pool_fn)
    print(f"[防守] 等权 {DEFENSE} 月度再平衡\n", flush=True)

    # ---- 网格搜索 ----
    configs = []
    for sp in [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
        configs.append(("fixed", sp, None))
    for thr in [-8, -12, -15]:
        configs.append(("dd", 1.0, thr))
    configs.append(("regime", 1.0, None))

    results = []
    for mode, sp, thr in configs:
        reg_fn = (lambda day: regime_on_date(proxy, day)) if mode == "regime" else None
        curve = simulate(trades, def_daily, all_dates, START, END, sp,
                         mode=mode, dd_thr=thr, regime_on=reg_fn)
        total, yr, mdd, sharpe = metrics(curve)
        weak_min = min(yr.get(w, 0.0) for w in WEAK)
        strong_mean = sum(yr.get(s, 0.0) for s in STRONG) / len(STRONG)
        # 优化目标: 抬升弱市(权重高) + 适度奖励强市 - 回撤惩罚
        score = weak_min + 0.25 * strong_mean - max(0, -mdd) * 0.02
        results.append({
            "mode": mode, "sp": sp, "thr": thr, "total": total, "yr": yr,
            "mdd": mdd, "sharpe": sharpe, "weak_min": weak_min,
            "strong_mean": strong_mean, "score": score, "curve": curve,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    print("配置网格 (按优化得分排序):")
    print(f"{'模式':<8}{'split':>6}{'thr':>6}{'总%':>10}{'弱市min':>9}"
          f"{'强市均':>9}{'MDD%':>8}{'夏普':>7}")
    print("-" * 64)
    for r in results:
        print(f"{r['mode']:<8}{r['sp']:>6.2f}{str(r['thr']):>6}{r['total']:>10.1f}"
              f"{r['weak_min']:>9.1f}{r['strong_mean']:>9.1f}"
              f"{r['mdd']:>8.1f}{r['sharpe']:>7.2f}")
    # ★每块配置逐年分解: 直接看"每年都正 + MDD 可接受"(对齐用户目标: 每年稳定盈利)
    print("\n逐年收益矩阵 (%):  * = 弱市年(WEAK={})".format(WEAK))
    years = sorted({y for r in results for y in r["yr"]})
    def _ycol(y):
        return f"{y}{'*' if y in WEAK else ' '}"
    hdr = (f"{'模式':<8}{'split':>6}{'thr':>6}"
           + "".join(f"{_ycol(y):>9}" for y in years) + f"{'MDD%':>9}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        row = (f"{r['mode']:<8}{r['sp']:>6.2f}{str(r['thr']):>6}")
        for y in years:
            row += f"{r['yr'].get(y, 0.0):>+9.1f}"
        row += f"{r['mdd']:>9.1f}"
        print(row)
    # 筛"每年都正 且 MDD≤-20%"的稳健候选(弱市年也含下半年, 不允许亏)
    stable = [r for r in results
              if all(r["yr"].get(y, 0.0) > 0 for y in years)
              and r["mdd"] >= -20]
    print("\n★ 每年都正 且 MDD≤-20% 的稳健候选:")
    if stable:
        for r in stable:
            print(f"   {r['mode']:<7} split={r['sp']:.2f} thr={r['thr']} | "
                  f"总 {r['total']:.1f}% MDD {r['mdd']:.1f}% 夏普 {r['sharpe']:.2f}")
    else:
        print("   (无: 没有配置同时满足 每年正 且 MDD≤-20%, "
              "需放宽阈值 / 调参 / 引入更强弱市降险)")

    print()
    best = results[0]
    print(f"★ 最优配置: mode={best['mode']} split={best['sp']} "
          f"thr={best['thr']}")
    print(f"  总收益 {best['total']:.1f}% | 弱市min {best['weak_min']:.1f}% "
          f"| 强市均 {best['strong_mean']:.1f}% | MDD {best['mdd']:.1f}% "
          f"| 夏普 {best['sharpe']:.2f}")
    print("  逐年:", "  ".join(f"{y}:{best['yr'].get(y,0):+.1f}%" for y in
          sorted(best['yr'])))

    # 基准对照: 纯进攻 / 纯防守
    atk_only = simulate(trades, def_daily, all_dates, START, END, 1.0, "fixed")
    def_only = simulate(trades, def_daily, all_dates, START, END, 0.0, "fixed")
    ta, _, ma, sa = metrics(atk_only)
    td, _, md, sd = metrics(def_only)
    print(f"\n[对照] 纯进攻(split=1): 总 {ta:.1f}% MDD {ma:.1f}% 夏普 {sa:.2f}")
    print(f"[对照] 纯防守(split=0): 总 {td:.1f}% MDD {md:.1f}% 夏普 {sd:.2f}")

    if args.plot:
        out = HERE.parent / "unified_2022_2026_equity.png"
        plot(all_dates, best, atk_only, def_only, out)


def plot(all_dates, best, atk_only, def_only, out):
    def norm(curve):
        return [(d, e / curve[0][1]) for d, e in curve]
    b = norm(best["curve"])
    a = norm(atk_only)
    d = norm(def_only)
    fig, ax = plt.subplots(2, 1, figsize=(13, 9),
                           gridspec_kw={"height_ratios": [3, 1]})
    ax[0].plot([x for x, _ in b], [y for _, y in b], label="Unified (best)", lw=2, color="black")
    ax[0].plot([x for x, _ in a], [y for _, y in a], label="Pure Attack (A)", lw=1.2, color="red", alpha=0.7)
    ax[0].plot([x for x, _ in d], [y for _, y in d], label="Pure Defense", lw=1.2, color="green", alpha=0.7)
    ax[0].set_title(f"Unified Strategy Equity Curve 2022-06~2026-07  "
                    f"(best: {best['mode']} split={best['sp']} | "
                    f"tot {best['total']:.0f}% MDD {best['mdd']:.0f}% Sharpe {best['sharpe']:.2f})")
    ax[0].set_ylabel("Net value (start=1)")
    ax[0].legend(loc="upper left")
    ax[0].grid(alpha=0.3)

    yrs = sorted(best["yr"].keys())
    vals = [best["yr"][y] for y in yrs]
    colors = ["#2ca02c" if v >= 0 else "#d62728" for v in vals]
    ax[1].bar(yrs, vals, color=colors)
    ax[1].axhline(0, color="black", lw=0.8)
    ax[1].set_title("Yearly Return % (green=profit red=loss)")
    ax[1].set_ylabel("Year Return %")
    ax[1].grid(alpha=0.3, axis="y")
    for i, v in enumerate(vals):
        ax[1].text(i, v + (3 if v >= 0 else -6), f"{v:+.0f}", ha="center", fontsize=8)

    fig.tight_layout()
    fig.savefig(out, dpi=130)
    print(f"\n曲线图已保存: {out}")


if __name__ == "__main__":
    main()

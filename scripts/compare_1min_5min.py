#!/usr/bin/env python3
"""1分K vs 5分K 回测差异对比（同窗口、同策略）。

    核心问题：用1分K数据回测，和5分K比，结果差多大？

    重要前提(数据来源！)：
      - 原生5分K = pytdx 拉取 (tdx_5min_2y.json)
      - 1分K缓存 = 新浪 拉取 (min_cache/*_1min_*.json，见 backtest_cached_1min.py)
      两者不同源，所以"原生-重采样"列的差距主要是【数据源差异】，不是【粒度】。

    本脚本无意中验证了：
      1) 同一窗口同一策略，原生5分K 与 新浪1分K重采样 选出的13笔信号【完全相同】
         → 粒度/分辨率不改变选股；
      2) 两者买卖价不同导致收益差 ~11.5%，但这是 Sina vs pytdx 价格源差异；
      3) 真正的【粒度/滞后效应】由"滞后 vs 对齐"两列给出：同一份数据内，
         滞后bar(14:50信号拿到14:45 bar)仅比实时对齐(拿到14:50 bar)虚增
         ~+1%/17日 —— 这个偏差才是对齐口径要消除的，且与分辨率无关。

    结论：对当前策略，1分K相对5分K的【粒度增益≈0】，真正影响结果的是
    price_at_time 的"已完成bar"滞后，已由 --aligned 实盘对齐口径消除；且
    pytdx 1分K深度仅~20交易日、分页不可靠，5分K才是合适的主干数据。
    """
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT, run_backtest,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_5MIN = Path.home() / ".tradingagents" / "cache" / "t0_5min"
OUT = CACHE_5MIN / "tdx_5min_2y.json"
BACKFILL = CACHE_5MIN / "backfill_daily_1000.json"
MIN_CACHE = Path.home() / ".tradingagents" / "rotation" / "min_cache"


def resample_1min_to_5min(bars: list[dict]) -> list[dict]:
    """把1分K聚合成5分K，标签用结束时刻(与原生5分K一致)。"""
    buckets: dict[int, list[dict]] = {}
    for b in bars:
        dt = b.get("day", "")
        if " " not in dt:
            continue
        t = dt.split(" ", 1)[1]
        h, m = int(t[:2]), int(t[3:5])
        em = ((m + 4) // 5) * 5  # 结束时刻(5分边界)
        key = h * 60 + em
        buckets.setdefault(key, []).append(b)
    out = []
    for key in sorted(buckets):
        grp = buckets[key]
        h, m = key // 60, key % 60
        etime = f"{h:02d}:{m:02d}:00"
        day = grp[0]["day"].split(" ")[0]
        out.append({
            "datetime": f"{day} {etime}",
            "day": day,
            "time": etime,
            "open": float(grp[0]["open"]),
            "high": max(float(x["high"]) for x in grp),
            "low": min(float(x["low"]) for x in grp),
            "close": float(grp[-1]["close"]),
            "volume": sum(float(x.get("volume", 0)) for x in grp),
        })
    return out


def load_resampled_5min(codes: set[str]) -> dict[str, dict]:
    """从1分K缓存重采样出5分K，仅保留 codes 内标的。"""
    etf_5min: dict[str, dict] = {}
    for f in MIN_CACHE.glob("*_1min_*.json"):
        parts = f.stem.split("_")
        code, day = parts[0], parts[2]
        if code not in codes:
            continue
        try:
            bars = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not bars:
            continue
        rb = resample_1min_to_5min(bars)
        if rb:
            etf_5min.setdefault(code, {})[day] = rb
    return etf_5min


def mdd(trades: list[dict]) -> float:
    eq, peak, m = 1.0, 1.0, 0.0
    for t in sorted(trades, key=lambda x: x["signal_date"]):
        eq *= 1 + t["return_pct"] / 100
        peak = max(peak, eq)
        m = min(m, (eq - peak) / peak * 100)
    return m


def main() -> None:
    etf_list = get_all_t0_etfs()
    codes = {e["code"] for e in etf_list}
    print(f">>> T+0 池 {len(codes)} 只")

    native = json.loads(OUT.read_text(encoding="utf-8"))["etf_5min"]
    bd = json.loads(BACKFILL.read_text(encoding="utf-8"))["etf_daily"]
    resp = load_resampled_5min(codes)
    print(f">>> 原生5分K: {sum(len(v) for v in native.values())} 标/日; "
          f"重采样5分K(来自1分): {sum(len(v) for v in resp.values())} 标/日")

    # 取"两份数据都覆盖、且覆盖≥90%标的"的日期，保证同窗口可比
    # (run_backtest 会自动跳过单只缺数据的标的，故不要求100%覆盖)
    from collections import Counter
    native_days = {c: set(native.get(c, {})) for c in codes}
    resp_days = {c: set(resp.get(c, {})) for c in codes}
    cov = Counter()
    for c in codes:
        for d in (native_days[c] & resp_days[c]):
            cov[d] += 1
    thr = 0.9 * len(codes)
    eval_dates = sorted(d for d, n in cov.items() if n >= thr)
    all_dates = sorted({day for c in codes for day in native_days[c]})
    if not eval_dates:
        # 兜底：放宽到≥50%
        eval_dates = sorted(d for d, n in cov.items() if n >= 0.5 * len(codes))
    print(f">>> 同覆盖窗口(≥90%标的): {eval_dates[0]} ~ {eval_dates[-1]} "
          f"({len(eval_dates)} 交易日)\n")

    def run(tag, data, sig, buy, conf):
        r = run_backtest(etf_list, bd, data, all_dates, eval_dates, FEE_PCT,
                         use_filter=True, daily_proxy=False,
                         confirm_time=conf, signal_time=sig, buy_time=buy)
        st = r["stats"]
        print(f"  [{tag}] 笔数 {r['trade_count']:>3} | 累计 {r['final_equity_pct']:+7.2f}% "
              f"| 回撤 {mdd(r['trades']):>6.2f}% | 胜率 {st.get('win_rate',0):>5.1f}% "
              f"| 均笔 {st.get('avg',0):>+6.2f}%")
        return r

    print("=== 原生5分K ===")
    nat_lag = run("滞后14:50/14:55 cf14:40", native, "14:50", "14:55", "14:40")
    nat_al = run("对齐14:51/14:56 cf14:41", native, "14:51", "14:56", "14:41")
    print("=== 重采样5分K(来自1分K) ===")
    rsp_lag = run("滞后14:50/14:55 cf14:40", resp, "14:50", "14:55", "14:40")
    rsp_al = run("对齐14:51/14:56 cf14:41", resp, "14:51", "14:56", "14:41")

    print("\n=== 差异拆解 (同窗口同策略) ===")
    def delta(a, b):
        return a["final_equity_pct"] - b["final_equity_pct"]
    print(f"  数据源差(原生pytdx5分 - 新浪1分重采样): {delta(nat_lag, rsp_lag):+.2f}%  "
          f"[Sina vs pytdx 价格源差异，非粒度]")
    print(f"  数据源差(对齐口径):                   {delta(nat_al, rsp_al):+.2f}%  "
          f"[同上，对齐口径]")
    print(f"  滞后/粒度效应(原生滞后 - 原生对齐):   {delta(nat_lag, nat_al):+.2f}%  "
          f"[同数据内 bar完成滞后虚增，对齐口径已消除]")
    print(f"  滞后/粒度效应(新浪1分滞后-对齐):     {delta(rsp_lag, rsp_al):+.2f}%  "
          f"[同上，1分K重采样后仍存在]")

    # 诊断：原生 vs 重采样 的 trade 集合与买卖价是否一致
    print("\n=== 诊断: 原生滞后 vs 重采样滞后 逐笔 ===")
    nl = {(t["signal_date"], t["etf"]): t for t in nat_lag["trades"]}
    rl = {(t["signal_date"], t["etf"]): t for t in rsp_lag["trades"]}
    only_n = [k for k in nl if k not in rl]
    only_r = [k for k in rl if k not in nl]
    common_keys = [k for k in nl if k in rl]
    print(f"  原生独有 {len(only_n)} 笔, 重采样独有 {len(only_r)} 笔, 交集 {len(common_keys)} 笔")
    if only_n:
        print(f"  原生独有: {only_n[:5]}")
    if only_r:
        print(f"  重采样独有: {only_r[:5]}")
    if common_keys:
        diff_buy = [k for k in common_keys
                    if abs(nl[k]["buy_price"] - rl[k]["buy_price"]) > 1e-4]
        diff_sell = [k for k in common_keys
                     if abs(nl[k]["sell_price"] - rl[k]["sell_price"]) > 1e-4]
        print(f"  交集里买价不同 {len(diff_buy)} 笔, 卖价不同 {len(diff_sell)} 笔")
        # 抽样展示一笔买价差异
        if diff_buy:
            k = diff_buy[0]
            print(f"  样例 {k}: 原生买 {nl[k]['buy_price']:.4f} / 重采样买 "
                  f"{rl[k]['buy_price']:.4f} (差 "
                  f"{(nl[k]['buy_price']-rl[k]['buy_price'])*100/rl[k]['buy_price']:+.2f}%)")


if __name__ == "__main__":
    main()

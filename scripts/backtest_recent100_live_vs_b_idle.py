#!/usr/bin/env python3
"""最近 N 个交易日: 当前 t0 实盘(hybrid-A 选股 + TRIX 卖) vs 候选 B+idle SHADOW 同口径对比。

对照组合:
  LIVE   = build_picks_hybrid(scheme A) + run_strategy("trix")        ← 当前实盘等价
  B核心  = 全市场Top1(≥3%, 不regime过滤) + run_strategy("hybrid")     ← SHADOW 核心腿
  idle腿 = 核心未触发日 14:50 买当日最强≥1.0% → 次日 14:50 固定卖      ← SHADOW 闲置腿
  SHADOW = B核心 + idle腿 (同一笔资金串行, 每天最多一笔)

用法:
    python scripts/backtest_recent100_live_vs_b_idle.py            # 最近100天
    python scripts/backtest_recent100_live_vs_b_idle.py --recent 60
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import backtest_t0_hybrid_sell as BH  # noqa: E402
BH.MIN_TRADES = 1  # 短窗口不做最小笔数门槛(默认10会返回None)

from backtest_t0_hybrid_sell import run_strategy, SIGNAL_TIME  # noqa: E402
from backtest_t0_etf import price_at_time  # noqa: E402
from backtest_t0_idle_window import sell_time_mode  # noqa: E402
from backtest_t0_today1 import FEE_PCT, MIN_GAIN, gain_at_time, next_trading_day  # noqa: E402
from backtest_b_idle_merge import (  # noqa: E402
    build_picks_B, build_prev_close, pick_momentum, stats_of, merge_equity,
    MOM_BUY, MOM_THR, MOM_SELL,
)
from quality_pool import build_picks_hybrid  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

CACHE_FILE = Path.home() / ".tradingagents/cache/t0_5min/aligned_live_4y.json"
DENSE_5MIN = Path.home() / ".tradingagents/cache/t0_5min/tdx_5min_2y.json"
OUT = Path.home() / ".tradingagents/cache/t0_5min/recent100_live_vs_b_idle.json"


def apply_confirm(picks: dict, etf_daily: dict, etf_5min: dict,
                  confirm_time: str, min_gain: float = MIN_GAIN) -> tuple[dict, int]:
    """双时点确认: confirm_time 时刻涨幅也须 ≥ min_gain (对齐实盘 t0_monitor CONFIRM_TIME)。

    注意口径: price_at_time 只取「已完成 bar」, 故 SIGNAL_TIME="14:45" 实际用 14:40 收盘价、
    confirm_time="14:40" 实际用 14:35 收盘价 —— 两者相对间隔 5 分钟, 与实盘
    (14:45 实时价 / 14:40 实时价) 的相对关系一致。数据缺失时放行(与实盘一致)。
    """
    out: dict = {}
    rejected = 0
    for key, val in picks.items():
        if not val:
            out[key] = val
            continue
        code = val[0]
        g = gain_at_time(etf_daily, etf_5min, code, key[1], confirm_time)
        if g is not None and g < min_gain:
            out[key] = None
            rejected += 1
        else:
            out[key] = val
    return out, rejected


def month_table(trades: list[dict]) -> dict:
    by: dict[str, list[float]] = {}
    for t in trades:
        by.setdefault(t["signal_date"][:7], []).append(t["return_pct"])
    out = {}
    for m in sorted(by):
        eq = 1.0
        for x in by[m]:
            eq *= 1 + x / 100
        out[m] = {"trades": len(by[m]), "ret": round((eq - 1) * 100, 2)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="最近N天 实盘 vs B+idle SHADOW")
    ap.add_argument("--recent", type=int, default=100)
    ap.add_argument("--lookback", type=int, default=30, help="hybrid-A 滚动优质池训练窗")
    ap.add_argument("--cache", type=str, default=str(CACHE_FILE))
    ap.add_argument("--five-min", type=str, default=str(DENSE_5MIN),
                    help="密集5min缓存(逗号分隔可多份合并, 默认tdx_5min_2y); 传 none 用主缓存")
    ap.add_argument("--fee", type=float, default=FEE_PCT)
    ap.add_argument("--confirm-time", type=str, default="14:40",
                    help="双时点确认时刻(对齐实盘 t0_monitor CONFIRM_TIME); none 关闭")
    args = ap.parse_args()
    cf_time = None if args.confirm_time.lower() == "none" else args.confirm_time

    print("载入缓存 ...", flush=True)
    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    all_dates = cache["all_dates"]
    etf_daily = cache["etf_daily"]
    etf_5min = cache["etf_5min"]
    proxy = cache["proxy_klines"]
    if args.five_min and args.five_min.lower() != "none":
        etf_5min = {}
        for part in args.five_min.split(","):
            part = part.strip()
            if not part:
                continue
            print(f"载入密集5min: {Path(part).name} ...", flush=True)
            dense = json.loads(Path(part).read_text(encoding="utf-8"))["etf_5min"]
            for c, days in dense.items():
                etf_5min.setdefault(c, {}).update(days)
        five_dates = sorted({d for days in etf_5min.values() for d in days})
        all_dates = sorted(set(all_dates) | set(five_dates))
        print(f"    合并密集池 {len(etf_5min)} 只, {five_dates[0]}~{five_dates[-1]}, "
              f"覆盖 {len(five_dates)} 交易日 (原稀疏缓存按需抓取, 每日仅20~40只有分钟线)")
    codes5 = set(etf_5min.keys())
    etf_list = [e for e in get_all_t0_etfs() if e["code"] in codes5]

    lb = args.lookback
    N = args.recent
    eval_dates = all_dates[-(N + 2 * lb):]
    test_dates = eval_dates[-N:]
    warmup = len(eval_dates) - len(test_dates) - lb
    test_set = set(test_dates)

    print(f"\n=== 最近 {N} 个交易日: {test_dates[0]} ~ {test_dates[-1]} ===")
    print(f"    候选池 {len(etf_list)} 只 | 费率 {args.fee}% | hybrid-A lookback={lb}\n")

    # ① LIVE: hybrid-A 选股 + TRIX 卖
    print(">>> 计算 LIVE(hybrid-A) 选股 ... (滚动优质池, 约1~2分钟)", flush=True)
    picks_a_full = build_picks_hybrid(
        eval_dates, etf_list, etf_daily, etf_5min, all_dates, proxy,
        lookback=lb, warmup=warmup,
    )
    picks_a = {k: v for k, v in picks_a_full.items() if k[1] in test_set}
    if cf_time:
        picks_a_cf, n_rej_a = apply_confirm(picks_a, etf_daily, etf_5min, cf_time)
        print(f"    双时点确认 {cf_time} (≥{MIN_GAIN:.0f}%): A 选股否决 {n_rej_a} 天")
    else:
        picks_a_cf, n_rej_a = picks_a, 0
    # 实盘等价: A选股 + confirm + TRIX 卖
    live = run_strategy("trix", test_dates, all_dates, picks_a_cf, etf_5min, args.fee)
    live_trades = live["trades"] if live else []
    # 对照: 无 confirm(旧口径)
    live_nc = run_strategy("trix", test_dates, all_dates, picks_a, etf_5min, args.fee)
    live_nc_trades = live_nc["trades"] if live_nc else []
    # 对照: confirm + hybrid 卖 (卖点升级的真实增益)
    a_hyb = run_strategy("hybrid", test_dates, all_dates, picks_a_cf, etf_5min, args.fee)
    a_hyb_trades = a_hyb["trades"] if a_hyb else []

    # ② B 核心腿: 全市场 Top1 + hybrid 卖
    print(">>> 计算 B(全市场Top1) 选股 ...", flush=True)
    picks_b = build_picks_B(test_dates, etf_list, etf_daily, etf_5min, 0)
    bcore = run_strategy("hybrid", test_dates, all_dates, picks_b, etf_5min, args.fee)
    b_trades = bcore["trades"] if bcore else []
    # 对照: B 选股 + TRIX 卖(拆分卖点贡献)
    b_trix = run_strategy("trix", test_dates, all_dates, picks_b, etf_5min, args.fee)
    b_trix_trades = b_trix["trades"] if b_trix else []
    # 对照: B 选股若也加 confirm (SHADOW 现无 confirm, 评估是否该加)
    if cf_time:
        picks_b_cf, n_rej_b = apply_confirm(picks_b, etf_daily, etf_5min, cf_time)
        b_cf = run_strategy("hybrid", test_dates, all_dates, picks_b_cf, etf_5min, args.fee)
        b_cf_trades = b_cf["trades"] if b_cf else []
        b_cf_trix = run_strategy("trix", test_dates, all_dates, picks_b_cf, etf_5min, args.fee)
        b_cf_trix_trades = b_cf_trix["trades"] if b_cf_trix else []
        print(f"    双时点确认 {cf_time}: B 选股否决 {n_rej_b} 天")
    else:
        b_cf_trades = []
        b_cf_trix_trades = []

    # ③ idle 腿
    prev_close = build_prev_close(etf_list, etf_daily)
    idle_days = [d for d in test_dates if not picks_b.get((SIGNAL_TIME, d))]
    idle_trades = []
    no_cand = 0
    drops = {"no_buy_px": 0, "no_next_day": 0, "no_next_bars": 0}
    for day in idle_days:
        pk = pick_momentum(etf_list, etf_5min, prev_close, day, MOM_THR)
        if not pk:
            no_cand += 1
            continue
        code, gain = pk
        bp = price_at_time(etf_5min.get(code, {}).get(day, []), MOM_BUY)
        if not bp or bp <= 0:
            drops["no_buy_px"] += 1
            continue
        nday = next_trading_day(all_dates, day)
        if not nday:
            drops["no_next_day"] += 1
            continue
        out = sell_time_mode(
            etf_5min.get(code, {}).get(nday, []), MOM_BUY, MOM_SELL, bp, args.fee)
        if not out:
            drops["no_next_bars"] += 1
            print(f"    [idle drop] {day} {code} 次日{nday}无5min数据")
            continue
        ret, reason = out
        idle_trades.append({
            "signal_date": day, "sell_date": nday, "etf": code,
            "today_gain": round(gain, 2), "return_pct": round(ret, 4),
            "sell_reason": "momentum_" + reason,
        })

    shadow_trades = b_trades + idle_trades

    s_live = stats_of(live_trades)
    s_b = stats_of(b_trades)
    s_btrix = stats_of(b_trix_trades)
    s_idle = stats_of(idle_trades)
    s_shadow = stats_of(shadow_trades)

    def row(label, s):
        print(f"  {label:<28}{s['equity_pct']:>+10.2f}%{s['trades']:>7}笔"
              f"{s['win_rate']:>8.0f}%{s['max_drawdown']:>9.1f}%")

    print("\n" + "=" * 70)
    print(f"  {'策略':<26}{'累计收益':>11}{'笔数':>8}{'胜率':>9}{'最大回撤':>10}")
    print("=" * 70)
    cf_tag = f"+确认{cf_time}" if cf_time else ""
    row(f"LIVE 实盘(A{cf_tag}+TRIX卖)", s_live)
    row("  └对照 A无确认+TRIX卖", stats_of(live_nc_trades))
    row(f"  └对照 A{cf_tag}+hybrid卖", stats_of(a_hyb_trades))
    row("SHADOW B核心(B+hybrid卖)", s_b)
    row("  └对照 B选股+TRIX卖", s_btrix)
    if b_cf_trades:
        row(f"  └对照 B{cf_tag}+hybrid卖", stats_of(b_cf_trades))
        row(f"  └对照 B{cf_tag}+TRIX卖", stats_of(b_cf_trix_trades))
    row("SHADOW idle腿(隔夜动量)", s_idle)
    row("★ SHADOW 合并(B+idle)", s_shadow)
    print("=" * 70)
    diff = s_shadow["equity_pct"] - s_live["equity_pct"]
    print(f"  SHADOW - LIVE = {diff:+.2f} 个百分点  "
          f"(核心腿差 {s_b['equity_pct']-s_live['equity_pct']:+.2f}, "
          f"idle腿贡献 {s_shadow['equity_pct']-s_b['equity_pct']:+.2f})")
    print(f"  资金利用: LIVE {s_live['trades']}/{N} 天, "
          f"SHADOW {s_shadow['trades']}/{N} 天 | idle日{len(idle_days)}天中"
          f"{no_cand}天无≥{MOM_THR:.1f}%标的, 丢弃 {drops}")

    # 逐月
    m_live, m_b, m_idle, m_sh = (month_table(x) for x in
                                 (live_trades, b_trades, idle_trades, shadow_trades))
    print(f"\n  逐月对比:")
    print(f"  {'月份':<9}{'LIVE':>16}{'B核心':>16}{'idle腿':>16}{'SHADOW':>16}")
    print("  " + "-" * 73)
    for m in sorted(set(m_live) | set(m_sh)):
        def f(tbl):
            v = tbl.get(m)
            return f"{v['ret']:+7.2f}%({v['trades']:>2})" if v else "      -     "
        print(f"  {m:<9}{f(m_live):>16}{f(m_b):>16}{f(m_idle):>16}{f(m_sh):>16}")

    # 明细: SHADOW 相对 LIVE 的差异日
    live_by_day = {t["signal_date"]: t for t in live_trades}
    b_by_day = {t["signal_date"]: t for t in b_trades}
    idle_by_day = {t["signal_date"]: t for t in idle_trades}
    diff_days = []
    for d in test_dates:
        l, b, i = live_by_day.get(d), b_by_day.get(d), idle_by_day.get(d)
        if not l and not b and not i:
            continue
        lc = f"{l['etf']} {l['return_pct']:+.2f}%" if l else "—(空仓)"
        s = b or i
        sc = (f"{s['etf']} {s['return_pct']:+.2f}%"
              f"{'[idle]' if i and not b else ''}") if s else "—(空仓)"
        if (l["etf"] if l else None) != (s["etf"] if s else None):
            diff_days.append((d, lc, sc))
    print(f"\n  选股不同的交易日 {len(diff_days)} 天(展示最近20天):")
    print(f"  {'日期':<12}{'LIVE':<26}{'SHADOW':<26}")
    print("  " + "-" * 62)
    for d, lc, sc in diff_days[-20:]:
        print(f"  {d:<12}{lc:<26}{sc:<26}")

    result = {
        "window": f"{test_dates[0]}~{test_dates[-1]}",
        "trading_days": N,
        "five_min_source": ([Path(p.strip()).name for p in args.five_min.split(",") if p.strip()]
                             if (args.five_min and args.five_min.lower() != "none")
                             else "aligned_live_4y(主缓存)"),
        "confirm_time": cf_time,
        "live_hybridA_confirm_trix": s_live,
        "live_hybridA_noconfirm_trix": stats_of(live_nc_trades),
        "a_confirm_hybrid": stats_of(a_hyb_trades),
        "b_confirm_hybrid": stats_of(b_cf_trades) if b_cf_trades else None,
        "b_confirm_trix": stats_of(b_cf_trix_trades) if b_cf_trix_trades else None,
        "b_core_hybrid": s_b,
        "b_core_trix": s_btrix,
        "idle_leg": s_idle,
        "shadow_merged": s_shadow,
        "shadow_minus_live_pct": round(diff, 2),
        "monthly": {"live": m_live, "b_core": m_b, "idle": m_idle, "shadow": m_sh},
        "live_trades": live_trades,
        "b_trades": b_trades,
        "b_confirm_trix_trades": b_cf_trix_trades,
        "idle_trades": idle_trades,
    }
    out_path = OUT.with_name(f"recent{N}_live_vs_b_idle.json")
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已落盘: {out_path}")


if __name__ == "__main__":
    main()

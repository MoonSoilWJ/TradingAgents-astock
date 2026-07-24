#!/usr/bin/env python3
"""100 日 5 分 K 回测 — 原T+0池 / T+0交割池 / 全市场 ETF/LOF，分 3 段对比。

策略（与实盘一致）: 14:45 信号 / 14:50 买入 / 次日 5分 TRIX(5,3)≥09:40≤11:05

用法:
    python scripts/backtest_t0_pool_100d.py --days 100
    python scripts/backtest_t0_pool_100d.py --days 100 --no-cache
    python scripts/backtest_t0_pool_100d.py --days 100 --fetch-limit 300
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import _calc_stats, fetch_sina_kline  # noqa: E402
from backtest_t0_etf import compute_daily_data, fetch_5min_kline, normalize_5min_bars  # noqa: E402
from backtest_t0_hybrid_sell import (  # noqa: E402
    BUY_TIME,
    MIN_TRADES,
    SELL_CUTOFF,
    SIGNAL_TIME,
    TRIX_MIN_SELL,
    TRIX_PERIOD,
    run_strategy,
)
from backtest_t0_today1 import FEE_PCT, load_market_data, resolve_eval_dates  # noqa: E402
from search_t0_time_combo import precompute_picks, segment_stats  # noqa: E402
from quality_pool import build_picks_hybrid  # noqa: E402
from t0_etf_list import (  # noqa: E402
    get_all_market_etf_lof,
    get_all_t0_etfs,
    get_t0_only_etfs,
    pool_stats,
)
from t0_regime import REGIME_PROXY  # noqa: E402
from tradingagents.dataflows.instrument import settlement_rule  # noqa: E402

SINA_INTERVAL = 0.25
CACHE_DIR = Path.home() / ".tradingagents" / "cache" / "t0_5min"


def load_market_5min_parallel(
    etf_list: list[dict],
    lookback: int,
    workers: int = 16,
) -> tuple[dict, dict, list[str], list[dict]]:
    """并行拉全市场 日K + 5分K。"""
    bar_len = lookback + 15
    datalen_5m = min(lookback * 50 + 200, 5500)
    etf_daily: dict = {}
    etf_5min: dict = {}

    def _one(info: dict) -> tuple[str, dict | None, dict | None]:
        code = info["code"]
        sym = info["sina_symbol"]
        daily_dict = None
        bars_5m = None
        daily = fetch_sina_kline(sym, datalen=bar_len)
        if daily and len(daily) > 3:
            daily_dict = {"returns": compute_daily_data(daily)}
        m5 = fetch_5min_kline(sym, datalen=datalen_5m)
        if m5:
            bars_5m = normalize_5min_bars(m5)
        return code, daily_dict, bars_5m

    w = min(workers, max(4, len(etf_list) // 200))
    print(f">>> 并行拉取 {len(etf_list)} 只 日K+5分K (workers={w}, datalen_5m={datalen_5m})...")
    done = 0
    with ThreadPoolExecutor(max_workers=w) as pool:
        futs = [pool.submit(_one, info) for info in etf_list]
        for fut in as_completed(futs):
            code, daily_dict, bars_5m = fut.result()
            if daily_dict:
                etf_daily[code] = daily_dict
            if bars_5m:
                etf_5min[code] = bars_5m
            done += 1
            if done % 500 == 0 or done == len(etf_list):
                print(f"    进度 {done}/{len(etf_list)} | 日K={len(etf_daily)} 5分K={len(etf_5min)}")

    proxy_sym = f"sh{REGIME_PROXY}"
    proxy_klines = fetch_sina_kline(proxy_sym, datalen=bar_len)
    all_dates = sorted({
        r["date"] for info in etf_daily.values() for r in info["returns"]
    })
    m5_dates = sorted({d for bars in etf_5min.values() for d in bars})
    if m5_dates:
        all_dates = sorted(set(all_dates) | set(m5_dates))
    print(f"    完成: 5分K {len(etf_5min)}/{len(etf_list)} | 日期 {all_dates[0] if all_dates else '?'} ~ {all_dates[-1] if all_dates else '?'}")
    return etf_daily, etf_5min, all_dates, proxy_klines


def load_or_fetch(
    etf_list: list[dict],
    lookback: int,
    *,
    use_cache: bool,
    write_cache: bool,
    fetch_limit: int | None,
) -> tuple[dict, dict, list[str], list[dict], str]:
    if fetch_limit:
        etf_list = etf_list[:fetch_limit]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d")
    cache_file = CACHE_DIR / f"pool_{tag}_days{lookback}_allmarket.json"

    if use_cache and cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        n = len(cached.get("etf_5min", {}))
        if n >= 100:
            print(f">>> 使用 5分K 缓存: {cache_file.name} ({n} 只)")
            return (
                cached["etf_daily"],
                cached["etf_5min"],
                cached["all_dates"],
                cached.get("proxy_klines", []),
                f"cached({n})",
            )

    etf_daily, etf_5min, all_dates, proxy_klines = load_market_5min_parallel(
        etf_list, lookback,
    )
    ds = f"sina_5m({len(etf_5min)})"
    if write_cache and len(etf_5min) >= 100:
        cache_file.write_text(json.dumps({
            "etf_daily": etf_daily,
            "etf_5min": etf_5min,
            "all_dates": all_dates,
            "proxy_klines": proxy_klines,
            "data_source": ds,
        }, ensure_ascii=False), encoding="utf-8")
        print(f"    已缓存: {cache_file}")
    return etf_daily, etf_5min, all_dates, proxy_klines, ds


def pool_list(pool: str, etf_5min: dict) -> tuple[list[dict], str]:
    codes = set(etf_5min.keys())
    if pool == "current":
        lst = [e for e in get_all_t0_etfs() if e["code"] in codes]
        return lst, f"原T+0池 ({len(lst)}只有5分K)"
    if pool == "t0_only":
        lst = [e for e in get_t0_only_etfs() if e["code"] in codes]
        return lst, f"T+0交割池 ({len(lst)}只有5分K)"
    lst = [e for e in get_all_market_etf_lof() if e["code"] in codes]
    return lst, f"全市场ETF/LOF ({len(lst)}只有5分K)"


def audit_picks(picks: dict, eval_dates: list[str]) -> list[dict]:
    rows = []
    for day in eval_dates:
        p = picks.get((SIGNAL_TIME, day))
        if not p:
            continue
        code, gain, name = p
        rows.append({
            "signal_date": day,
            "code": code,
            "name": name,
            "gain": gain,
            "settlement": settlement_rule(code, name),
        })
    return rows


def print_pool_result(label: str, result: dict, eval_dates: list[str], audit: list[dict]):
    st = result["stats"]
    t1_n = sum(1 for a in audit if a["settlement"] != "T0")
    print(f"\n  【{label}】")
    print(f"  选股 {len(audit)} 次 | T+1误选 {t1_n} | 成交 {result['trade_count']} 笔")
    print(
        f"  累计 {result['final_equity_pct']:+.2f}% | 胜率 {st.get('win_rate', 0):.1f}% | "
        f"均笔 {st.get('avg', 0):+.2f}% | 回撤 {st.get('max_drawdown', 0):+.2f}% | "
        f"夏普 {st.get('sharpe', 0):.2f}"
    )
    print(f"  卖出: {result.get('sell_reasons', {})}")


def print_segments(results: dict[str, dict], eval_dates: list[str]):
    seg_size = len(eval_dates) // 3
    segs = [
        ("前期", eval_dates[:seg_size]),
        ("中期", eval_dates[seg_size: 2 * seg_size]),
        ("后期", eval_dates[2 * seg_size:]),
    ]
    print("\n" + "=" * 90)
    print("  分 3 段对比（各段独立复利起算）")
    print("=" * 90)
    hdr = f"  {'阶段':<8} {'区间':<23}"
    for key in results:
        hdr += f" {results[key]['label'][:12]:>14}"
    print(hdr)
    print("  " + "─" * 86)
    for name, ds in segs:
        if not ds:
            continue
        row = f"  {name:<8} {ds[0]}~{ds[-1]:<12}"
        for key in results:
            s = segment_stats(results[key]["result"]["trades"], ds)
            row += f" {s['total']:+13.2f}%"
        print(row)
        detail = f"           ({len(ds)}日)"
        for key in results:
            s = segment_stats(results[key]["result"]["trades"], ds)
            detail += f" {s['count']:>3}笔/{s['win_rate']:4.0f}%".rjust(14)
        print(detail)


def main() -> None:
    parser = argparse.ArgumentParser(description="100日5分K T+0池 vs 全市场池")
    parser.add_argument("--days", type=int, default=100)
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="")
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-write-cache", action="store_true")
    parser.add_argument("--fetch-limit", type=int, default=None, help="调试：限制拉取只数")
    args = parser.parse_args()

    lookback = args.days if not (args.start_date or args.end_date) else max(args.days, 280)

    print("=== 100 日 5 分 K 池对比回测 ===")
    print(f"策略: {SIGNAL_TIME} 信号 / {BUY_TIME} 买入 / 5分 TRIX({TRIX_PERIOD},3) {TRIX_MIN_SELL}~{SELL_CUTOFF}")
    print(f"过滤: 涨幅≥3% | 震荡跳过 | 手续费万3\n")

    market = get_all_market_etf_lof()
    print(f"全市场名单: {len(market)} 只 ETF/LOF")

    etf_daily, etf_5min, all_dates, proxy_klines, data_source = load_or_fetch(
        market, lookback,
        use_cache=not args.no_cache,
        write_cache=not args.no_write_cache,
        fetch_limit=args.fetch_limit,
    )
    if len(etf_5min) < 50:
        print("ERROR: 5 分 K 数据不足")
        sys.exit(1)

    eval_dates = resolve_eval_dates(all_dates, args.days, args.start_date, args.end_date)
    if len(eval_dates) < MIN_TRADES:
        print(f"ERROR: 有效信号日不足 ({len(eval_dates)})")
        sys.exit(1)
    print(f"\n数据: {data_source}")
    print(f"回测区间: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 信号日)\n")

    results: dict[str, dict] = {}
    pool_orig = [e for e in get_all_t0_etfs() if e["code"] in etf_5min]
    hybrid_lb = min(30, max(len(eval_dates) // 3, 10))

    for pool_key in ("current", "t0_only", "all_market", "hybrid"):
        if pool_key == "hybrid":
            plabel = f"★ hybrid-A (趋势震荡→优质/中性→原池, lb={hybrid_lb})"
            ps = pool_stats(pool_orig)
            print(f">>> {plabel} | 原池 T+0={ps['t0']} T+1={ps['t1']}")
            picks = build_picks_hybrid(
                eval_dates, pool_orig, etf_daily, etf_5min, all_dates, proxy_klines,
                lookback=hybrid_lb, warmup=hybrid_lb,
            )
        else:
            plst, plabel = pool_list(pool_key, etf_5min)
            if len(plst) < 5:
                print(f"WARN: {plabel} 有效标的不足")
                continue
            ps = pool_stats(plst)
            print(f">>> {plabel} | 交割 T+0={ps['t0']} T+1={ps['t1']}")
            picks = precompute_picks(
                plst, etf_daily, etf_5min, eval_dates, [SIGNAL_TIME],
                proxy_klines, use_filter=True, skip_choppy=True,
            )

        audit = audit_picks(picks, eval_dates)
        result = run_strategy(
            "trix", eval_dates, all_dates, picks, etf_5min, args.fee,
        )
        if not result:
            print(f"  ERROR: {plabel} 有效交易不足")
            continue
        results[pool_key] = {
            "label": plabel,
            "result": result,
            "audit": audit,
            "pool_stats": ps,
        }
        print_pool_result(plabel, result, eval_dates, audit)

    if len(results) >= 2:
        print("\n  全期累计对比:")
        for key in ("current", "t0_only", "all_market", "hybrid"):
            if key not in results:
                continue
            r = results[key]["result"]
            print(f"    {results[key]['label']}: {r['final_equity_pct']:+.2f}%")

    if results:
        print_segments(results, eval_dates)

    out_dir = Path.home() / ".tradingagents" / "rotation"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = out_dir / f"backtest_t0_pool_100d_{tag}.json"
    out_path.write_text(json.dumps({
        "config": {
            "days": args.days,
            "eval_dates": eval_dates,
            "data_source": data_source,
            "strategy": f"{SIGNAL_TIME}/{BUY_TIME}/5m_trix0940_cut/{SELL_CUTOFF}",
        },
        "pools": {
            k: {
                "label": v["label"],
                "pool_stats": v["pool_stats"],
                "picks_audit": v["audit"],
                "summary": {kk: vv for kk, vv in v["result"].items() if kk != "trades"},
                "trades": v["result"].get("trades"),
            }
            for k, v in results.items()
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()

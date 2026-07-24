#!/usr/bin/env python3
"""T+0 ETF 1 分钟 K 精确回测 — 东财 trends2 或新浪 1 分 K。

对比「网格搜索最优组合」与「当前实盘基线」(14:50/14:55/TRIX≥09:40)，
或 `--compare` 对比实盘基线与候选策略 (14:45/14:50/TRIX≥09:40≤11:05)。

更贴近实盘的成交价（1 分 K 买卖 + 原生 5 分 TRIX 信号）请用:
    python scripts/backtest_t0_hybrid_1min.py --realistic --trades --ndays 9 --source sina

用法:
    python scripts/backtest_t0_1min.py
    python scripts/backtest_t0_1min.py --ndays 5 --top 15
    python scripts/backtest_t0_1min.py --baseline-only
    python scripts/backtest_t0_1min.py --compare --source sina
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(ROOT))

from backtest_top1 import _calc_stats, fetch_sina_kline  # noqa: E402
from backtest_t0_etf import compute_daily_data, normalize_5min_bars  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    load_market_data,
    resolve_eval_dates,
)
from search_t0_time_combo import (  # noqa: E402
    BASELINE,
    DEFAULT_BUY_TIMES,
    DEFAULT_SELL_CUTOFFS,
    DEFAULT_SIGNAL_TIMES,
    iter_combos,
    precompute_picks,
    print_top_results,
    run_combo,
    segment_stats,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

try:
    from tradingagents.dataflows.a_stock import _em_get
except ImportError:
    _em_get = None  # type: ignore[assignment]

EM_INTERVAL = float(__import__("os").environ.get("EM_MIN_INTERVAL", "2.0"))
SINA_INTERVAL = 0.3
MIN_TRADES_1MIN = 2  # 短窗口样本少，放宽门槛
MIN_ETF_COVERAGE = 0.75  # 低于此覆盖率不写入缓存

CANDIDATE = {
    "signal": "14:45",
    "buy": "14:50",
    "sell_mode": "trix0940_cut",
    "sell_cutoff": "11:05",
    "label": "候选(14:45/14:50/TRIX≥09:40≤11:05)",
}
CANDIDATE_TIME = {
    "signal": "14:45",
    "buy": "14:50",
    "sell_mode": "time",
    "sell_cutoff": "10:05",
    "label": "候选(14:45/14:50/定时10:05)",
}


def etf_secid(code: str) -> str:
    return f"1.{code}" if code.startswith("5") else f"0.{code}"


def _parse_trends_payload(data: dict) -> dict[str, list[dict]]:
    bars_by_date: dict[str, list[dict]] = {}
    for t in data.get("data", {}).get("trends", []):
        parts = t.split(",")
        if len(parts) < 7:
            continue
        dt_str = parts[0]
        day = dt_str[:10]
        time_part = dt_str[11:16]
        bars_by_date.setdefault(day, []).append({
            "datetime": dt_str,
            "day": day,
            "time": time_part,
            "open": float(parts[1] or 0),
            "close": float(parts[2] or 0),
            "high": float(parts[3] or 0),
            "low": float(parts[4] or 0),
            "volume": float(parts[5] or 0),
        })
    return bars_by_date


def fetch_1min_kline_em(code: str, ndays: int = 5, retries: int = 3) -> dict[str, list[dict]]:
    """东财 trends2 拉 1 分钟 K，按交易日分组。优先 curl（绕过代理），失败再 _em_get。"""
    secid = etf_secid(code)
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/trends2/get?"
        "fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13&"
        "fields2=f51,f52,f53,f54,f55,f56,f57,f58&"
        f"ut=7eea3edcaed734bea9cbfc24409ed989&ndays={ndays}&iscr=0&secid={secid}"
    )
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            cmd = [
                "curl", "-s", "--connect-timeout", "15", "--noproxy", "*",
                "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "-H", "Referer: https://quote.eastmoney.com/",
                url,
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=25)
            text = proc.stdout.decode("utf-8", errors="replace")
            if not text.strip():
                raise ValueError("empty curl response")
            data = json.loads(text)
            bars = _parse_trends_payload(data)
            if bars:
                return bars
            raise ValueError("no trends in response")
        except Exception as e:
            last_err = e
            if _em_get is not None:
                try:
                    resp = _em_get(url, timeout=20)
                    if resp and resp.text:
                        bars = _parse_trends_payload(json.loads(resp.text))
                        if bars:
                            return bars
                except Exception as em_err:
                    last_err = em_err
            time.sleep(EM_INTERVAL * (attempt + 1))
    print(f"  [warn] 1分K失败 {code}: {last_err}")
    return {}


def fetch_1min_kline_sina(sina_symbol: str, datalen: int = 1970) -> dict[str, list[dict]]:
    """新浪 jsonp 1 分 K，按交易日分组（约 9 个交易日）。"""
    import requests

    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData"
    params = {"symbol": sina_symbol, "scale": "1", "ma": "no", "datalen": str(datalen)}
    try:
        r = requests.get(url, params=params, timeout=15)
        text = r.text
        payload = text.split("=(")[1].split(");")[0]
        klines = json.loads(payload)
        if not klines:
            return {}
        return normalize_5min_bars(klines)
    except Exception as e:
        print(f"  [warn] 新浪1分K失败 {sina_symbol}: {e}")
        return {}


def load_1min_data(
    etf_list: list[dict],
    ndays: int = 5,
    source: str = "auto",
    *,
    use_cache: bool = True,
    write_cache: bool = True,
    fetch_limit: int | None = None,
    cache_suffix: str = "",
    min_write_count: int = 50,
) -> tuple[dict, dict, list[str], list[dict], str]:
    """拉 ETF/LOF 1 分 K + 日 K（昨收/震荡识别）。

    use_cache=False 跳过读缓存；write_cache=True 拉取成功后仍写入（供下次加速）。
    cache_suffix 如 '_allmarket' 区分全市场池文件。
    """
    if fetch_limit is not None and fetch_limit > 0:
        etf_list = etf_list[:fetch_limit]

    cache_dir = Path.home() / ".tradingagents" / "cache" / "t0_1min"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_tag = datetime.now().strftime("%Y%m%d")
    cache_file = cache_dir / f"pool_{cache_tag}_src{source}_ndays{ndays}{cache_suffix}.json"

    if use_cache and cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        cached_n = len(cached.get("etf_1min", {}))
        cov = cached_n / max(len(etf_list), 1)
        min_need = min_write_count if len(etf_list) > 200 else max(min_write_count, int(len(etf_list) * MIN_ETF_COVERAGE))
        if cached_n >= min_need or cov >= MIN_ETF_COVERAGE:
            ds = cached.get("data_source", "cached")
            print(f">>> 使用缓存 1 分 K: {cache_file.name} ({cached_n} ETF, {ds})")
            return (
                cached["etf_daily"],
                cached["etf_1min"],
                cached["all_dates"],
                cached.get("proxy_klines", []),
                ds,
            )
        print(f">>> 忽略不完整缓存 ({cached_n}/{len(etf_list)} ETF)，重新拉取...")

    etf_1min: dict[str, dict[str, list[dict]]] = {}
    etf_daily: dict = {}
    em_ok = sina_ok = 0

    def _fetch_one(info: dict) -> tuple[str, dict | None, dict | None, str]:
        code = info["code"]
        sym = info["sina_symbol"]
        bars: dict[str, list[dict]] = {}
        hit = ""
        if source in ("auto", "em"):
            bars = fetch_1min_kline_em(code, ndays)
            if bars:
                hit = "em"
        if not bars and source in ("auto", "sina"):
            bars = fetch_1min_kline_sina(sym)
            if bars:
                hit = "sina"
        daily_dict = None
        daily = fetch_sina_kline(sym, datalen=max(ndays + 40, 60))
        if daily and len(daily) > 3:
            daily_dict = {"returns": compute_daily_data(daily)}
        return code, bars or None, daily_dict, hit

    use_parallel = len(etf_list) > 80 and source == "sina" and not use_cache
    if use_parallel:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        workers = min(16, max(4, len(etf_list) // 150))
        print(f">>> 并行拉取 {len(etf_list)} 只 ETF/LOF 1 分 K (workers={workers}, source=sina)...")
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_fetch_one, info) for info in etf_list]
            for fut in as_completed(futs):
                code, bars, daily_dict, hit = fut.result()
                if bars:
                    etf_1min[code] = bars
                    if hit == "em":
                        em_ok += 1
                    elif hit == "sina":
                        sina_ok += 1
                if daily_dict:
                    etf_daily[code] = daily_dict
                done += 1
                if done % 200 == 0 or done == len(etf_list):
                    print(f"    进度 {done}/{len(etf_list)} | 1分K={len(etf_1min)} 日K={len(etf_daily)}")
    else:
        print(f">>> 拉取 {len(etf_list)} 只 ETF/LOF 1 分 K (source={source}, ndays={ndays})...")
        for i, info in enumerate(etf_list):
            code, bars, daily_dict, hit = _fetch_one(info)
            if bars:
                etf_1min[code] = bars
                if hit == "em":
                    em_ok += 1
                elif hit == "sina":
                    sina_ok += 1
            if daily_dict:
                etf_daily[code] = daily_dict
            if (i + 1) % 20 == 0:
                print(f"    进度 {i + 1}/{len(etf_list)} | 1分K={len(etf_1min)} 日K={len(etf_daily)}")
            time.sleep(SINA_INTERVAL if source == "sina" or (source == "auto" and not bars) else EM_INTERVAL)

    all_dates = sorted({d for bars in etf_1min.values() for d in bars})
    m1_dates = set(all_dates)
    daily_dates = {r["date"] for info in etf_daily.values() for r in info["returns"]}
    all_dates = sorted(m1_dates | daily_dates)

    proxy = next((e for e in etf_list if e["code"] == "501018"), etf_list[0])
    proxy_klines = fetch_sina_kline(proxy["sina_symbol"], datalen=80)

    m1_only = sorted({d for bars in etf_1min.values() for d in bars})
    if em_ok and sina_ok:
        data_source = f"em({em_ok})+sina({sina_ok})"
    elif em_ok:
        data_source = f"eastmoney({em_ok})"
    elif sina_ok:
        data_source = f"sina({sina_ok})"
    else:
        data_source = "none"
    print(
        f"    完成: 1分K {len(etf_1min)}/{len(etf_list)} ETF ({data_source}) | "
        f"覆盖 {m1_only[0] if m1_only else '?'} ~ {m1_only[-1] if m1_only else '?'}"
    )
    cov = len(etf_1min) / max(len(etf_list), 1)
    if write_cache and len(etf_1min) >= min_write_count:
        payload = {
            "etf_daily": etf_daily,
            "etf_1min": etf_1min,
            "all_dates": all_dates,
            "proxy_klines": proxy_klines,
            "data_source": data_source,
            "universe_count": len(etf_list),
            "cache_suffix": cache_suffix,
        }
        cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"    已缓存: {cache_file} ({len(etf_1min)} 只)")
    elif not write_cache:
        print("    未写缓存 (--no-write-cache)")
    else:
        print(f"    有效 {len(etf_1min)} 只 < min_write_count={min_write_count}，跳过缓存")
    return etf_daily, etf_1min, all_dates, proxy_klines, data_source


def run_combo_1min(*args, **kwargs):
    """run_combo 的 1 分 K 版（降低 MIN_TRADES）。"""
    import search_t0_time_combo as stc

    old_min = stc.MIN_TRADES
    stc.MIN_TRADES = MIN_TRADES_1MIN
    try:
        return run_combo(*args, **kwargs)
    finally:
        stc.MIN_TRADES = old_min


def print_trade_detail(result: dict, title: str):
    print()
    print("=" * 90)
    print(f"  {title}")
    print("=" * 90)
    st = result.get("stats") or {}
    print(
        f"  {result.get('signal')} 信号 | {result.get('buy')} 买入 | {result.get('label')} | "
        f"{result['trade_count']} 笔 | 累计 {result['final_equity_pct']:+.2f}%"
    )
    if st:
        print(
            f"  胜率 {st.get('win_rate', 0):.1f}% | 均笔 {st.get('avg', 0):+.2f}% | "
            f"回撤 {st.get('max_drawdown', 0):+.2f}% | 夏普 {st.get('sharpe', 0):.2f}"
        )
    trades = result.get("trades") or []
    if trades:
        print(
            f"\n  {'信号日':>12} {'信号':>5} {'买入':>5} {'卖出日':>12} {'卖出':>5} "
            f"{'ETF':>8} {'涨幅':>6} {'买价':>7} {'卖价':>7} {'原因':>14} {'收益':>7} {'累计':>7}"
        )
        print("  " + "-" * 108)
        eq = 1.0
        for t in trades:
            eq *= 1 + t["return_pct"] / 100
            print(
                f"  {t['signal_date']:>12} {t.get('signal_time', result.get('signal', '')):>5} "
                f"{t.get('buy_time', result.get('buy', '')):>5} "
                f"{t.get('sell_date', ''):>12} {t.get('sell_time', ''):>5} "
                f"{t['etf']:>8} {t['today_gain']:+5.1f}% "
                f"{t['buy_price']:7.4f} {t['sell_price']:7.4f} {t['sell_reason']:>14} "
                f"{t['return_pct']:+6.2f}% {(eq - 1) * 100:+6.2f}%"
            )
    print("=" * 90)


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 ETF 1 分钟 K 精确回测（东财最近 ndays 天）")
    parser.add_argument("--ndays", type=int, default=5, help="东财 1 分 K 天数（默认 5）")
    parser.add_argument("--top", type=int, default=15, help="显示前 N 个最优组合")
    parser.add_argument("--fee", type=float, default=FEE_PCT, help="单边手续费(万3=0.03)")
    parser.add_argument("--baseline-only", action="store_true", help="仅跑实盘基线")
    parser.add_argument("--compare", action="store_true", help="对比实盘基线与候选策略")
    parser.add_argument("--candidate-only", action="store_true",
                        help="仅跑候选策略(14:45/14:50/TRIX≤11:05)")
    parser.add_argument("--trix-period", type=int, default=5, help="TRIX EMA 周期(默认5)")
    parser.add_argument("--trix-signal", type=int, default=None,
                        help="TRIX signal 周期(默认 period//2 至少3)")
    parser.add_argument("--source", choices=["auto", "em", "sina"], default="auto",
                        help="1分K数据源: auto=东财优先/新浪兜底, sina=仅新浪")
    parser.add_argument("--no-skip-choppy", dest="skip_choppy", action="store_false", default=True)
    parser.add_argument("--no-filter", action="store_true", help="关闭涨幅≥3%%过滤")
    parser.add_argument("--no-same-day", action="store_true", help="不搜索当日 T+0 卖出")
    args = parser.parse_args()

    use_filter = not args.no_filter
    skip_choppy = args.skip_choppy
    trix_signal = args.trix_signal if args.trix_signal is not None else max(args.trix_period // 2, 3)

    print("=== T+0 ETF 1 分钟 K 精确回测 ===")
    print(f"数据源: {args.source} (ndays={args.ndays}) | 手续费万{args.fee * 100:.0f}")
    print(f"过滤: 涨幅≥3%={'是' if use_filter else '否'} | 震荡跳过={'是' if skip_choppy else '否'}")
    print(f"TRIX: ({args.trix_period},{trix_signal}) 基于 1 分 K")
    print(f"实盘基线: {BASELINE['label']}")
    if args.compare or args.candidate_only:
        print(f"候选策略: {CANDIDATE['label']}")
    if args.compare:
        print(f"备选定时: {CANDIDATE_TIME['label']}")
    print()

    etf_list = get_all_t0_etfs()
    etf_daily, etf_1min, all_dates, proxy_klines, data_source = load_1min_data(
        etf_list, args.ndays, source=args.source,
    )
    if len(etf_1min) < 5:
        print("ERROR: 1 分 K 数据不足（检查网络/东财限流）")
        sys.exit(1)

    # 仅使用有 1 分 K 的日期；隔夜策略需留出一日作卖出
    m1_dates = sorted({d for bars in etf_1min.values() for d in bars})
    eval_dates = m1_dates[:-1] if len(m1_dates) > 1 else m1_dates
    if len(eval_dates) < 2:
        print("ERROR: 有效信号日不足 2 天")
        sys.exit(1)

    print(f"回测信号日: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 日)\n")

    signal_times = DEFAULT_SIGNAL_TIMES
    picks = precompute_picks(
        etf_list, etf_daily, etf_1min, eval_dates, signal_times,
        proxy_klines, use_filter, skip_choppy,
    )

    import search_t0_time_combo as stc

    old_min = stc.MIN_TRADES
    stc.MIN_TRADES = MIN_TRADES_1MIN

    bl = run_combo(
        BASELINE["signal"], BASELINE["buy"], BASELINE["sell_mode"], BASELINE["sell_cutoff"],
        eval_dates, all_dates, picks, etf_1min, args.fee,
        trix_period=args.trix_period, trix_signal_period=trix_signal,
    )

    if args.candidate_only:
        import search_t0_time_combo as stc

        cand = run_combo(
            CANDIDATE["signal"], CANDIDATE["buy"], CANDIDATE["sell_mode"], CANDIDATE["sell_cutoff"],
            eval_dates, all_dates, picks, etf_1min, args.fee,
            trix_period=args.trix_period, trix_signal_period=trix_signal,
        )
        stc.MIN_TRADES = old_min
        label = f"{CANDIDATE['label']} TRIX({args.trix_period},{trix_signal})"
        if cand:
            print_trade_detail({**cand, **CANDIDATE, "label": label}, label)
        else:
            print("ERROR: 候选策略无有效交易")
        out_dir = Path.home() / ".tradingagents" / "rotation"
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d_%H%M")
        out_path = out_dir / f"backtest_t0_1min_candidate_{tag}.json"
        out_path.write_text(json.dumps({
            "config": {"trix_period": args.trix_period, "trix_signal": trix_signal,
                       "data_source": data_source, "eval_dates": eval_dates},
            "candidate": cand,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已保存: {out_path}")
        sys.exit(0 if cand else 1)

    if args.compare:
        import search_t0_time_combo as stc

        cand = run_combo(
            CANDIDATE["signal"], CANDIDATE["buy"], CANDIDATE["sell_mode"], CANDIDATE["sell_cutoff"],
            eval_dates, all_dates, picks, etf_1min, args.fee,
            trix_period=args.trix_period, trix_signal_period=trix_signal,
        )
        cand_time = run_combo(
            CANDIDATE_TIME["signal"], CANDIDATE_TIME["buy"],
            CANDIDATE_TIME["sell_mode"], CANDIDATE_TIME["sell_cutoff"],
            eval_dates, all_dates, picks, etf_1min, args.fee,
            trix_period=args.trix_period, trix_signal_period=trix_signal,
        )
        stc.MIN_TRADES = old_min

        print("\n" + "=" * 90)
        print("  策略对比（1 分钟 K）")
        print("=" * 90)
        print(f"  {'策略':<42} {'笔数':>4} {'累计':>9} {'胜率':>6} {'均笔':>7} {'回撤':>8}")
        print("  " + "-" * 82)
        for spec, r in [
            (BASELINE, bl), (CANDIDATE, cand), (CANDIDATE_TIME, cand_time),
        ]:
            if not r:
                print(f"  {spec['label']:<42} {'—':>4} {'—':>9} {'—':>6} {'—':>7} {'—':>8}")
                continue
            st = r.get("stats") or {}
            print(
                f"  {spec['label']:<42} {r['trade_count']:>4} "
                f"{r['final_equity_pct']:+8.2f}% {st.get('win_rate', 0):>5.1f}% "
                f"{st.get('avg', 0):>+6.2f}% {st.get('max_drawdown', 0):>+7.2f}%"
            )
        print("=" * 90)

        for spec, r in [(BASELINE, bl), (CANDIDATE, cand), (CANDIDATE_TIME, cand_time)]:
            if r:
                print_trade_detail({**r, **spec}, spec["label"])

        if bl and cand:
            diff = cand["final_equity_pct"] - bl["final_equity_pct"]
            print(f"\n  候选 TRIX≤11:05 vs 实盘: {diff:+.2f} pp（{len(eval_dates)} 信号日）")
        if bl and cand_time:
            diff2 = cand_time["final_equity_pct"] - bl["final_equity_pct"]
            print(f"  候选 定时10:05 vs 实盘: {diff2:+.2f} pp")

        out_dir = Path.home() / ".tradingagents" / "rotation"
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d_%H%M")
        payload = {
            "config": {
                "ndays": args.ndays,
                "data_source": data_source,
                "eval_dates": eval_dates,
                "use_filter": use_filter,
                "skip_choppy": skip_choppy,
            },
            "baseline": bl,
            "candidate": cand,
            "candidate_time": cand_time,
        }
        out_path = out_dir / f"backtest_t0_1min_compare_{tag}.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已保存: {out_path}")
        sys.exit(0 if (bl or cand) else 1)

    if args.baseline_only:
        if bl:
            print_trade_detail({**bl, **BASELINE}, BASELINE["label"])
        else:
            print("ERROR: 基线无有效交易")
        stc.MIN_TRADES = old_min
        sys.exit(0 if bl else 1)

    combos = iter_combos(
        DEFAULT_SIGNAL_TIMES, DEFAULT_BUY_TIMES, DEFAULT_SELL_CUTOFFS,
        include_same_day=not args.no_same_day,
    )
    print(f">>> 网格搜索 {len(combos)} 种组合（1 分 K，MIN_TRADES={MIN_TRADES_1MIN}）...")
    results: list[dict] = []
    for sig, buy, mode, cutoff in combos:
        r = run_combo(sig, buy, mode, cutoff, eval_dates, all_dates, picks, etf_1min, args.fee)
        if r:
            results.append({k: v for k, v in r.items() if k != "trades"})

    results.sort(key=lambda x: x["final_equity_pct"], reverse=True)
    stc.MIN_TRADES = old_min

    print(f"    有效组合: {len(results)}\n")
    if results:
        print_top_results(results, args.top, eval_dates, picks, etf_1min, all_dates, args.fee)

    if bl:
        rank = 1 + next(
            (i for i, r in enumerate(results) if r["final_equity_pct"] <= bl["final_equity_pct"]),
            len(results),
        )
        print(f"\n  实盘基线: {bl['final_equity_pct']:+.2f}% | {bl['trade_count']} 笔 | 排名 #{rank}/{len(results)}")
        print_trade_detail(
            {**bl, **BASELINE},
            f"实盘基线 — {BASELINE['label']}",
        )

    if results:
        best = results[0]
        best_detail = run_combo(
            best["signal"], best["buy"], best["sell_mode"], best["sell_cutoff"],
            eval_dates, all_dates, picks, etf_1min, args.fee,
        )
        print(f"\n  ★ 5 日 1 分 K 累计最优: {best['signal']} 买{best['buy']} | {best['label']}")
        print(f"    累计 {best['final_equity_pct']:+.2f}% | {best['trade_count']} 笔")
        if best_detail:
            print_trade_detail(best_detail, f"最优组合 — {best['signal']}/{best['buy']} {best['label']}")

        if bl and best_detail:
            diff = best["final_equity_pct"] - bl["final_equity_pct"]
            print(f"\n  最优 vs 实盘: {diff:+.2f} pp（样本仅 {len(eval_dates)} 信号日，仅供参考）")

    out_dir = Path.home() / ".tradingagents" / "rotation"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    payload = {
        "config": {
            "ndays": args.ndays,
            "m1_dates": m1_dates,
            "eval_dates": eval_dates,
            "use_filter": use_filter,
            "skip_choppy": skip_choppy,
            "fee": args.fee,
            "data_source": data_source,
        },
        "baseline": bl,
        "best": results[0] if results else None,
        "top": results[: args.top],
    }
    out_path = out_dir / f"backtest_t0_1min_{tag}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()

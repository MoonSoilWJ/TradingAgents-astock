#!/usr/bin/env python3
"""T+0 ETF 量能/相对量网格搜索 — 用相对量能替代固定买卖时间。

两阶段：
1. --analyze  归因：在已知时间基线交易的买/卖点记录相对量能，对比盈亏分布
2. 默认搜索   网格：lookback × 买入量比 × 卖出量比/模式，样本回测验证

相对量能（1 分钟，因果、无未来函数）：
- vol_ratio:     当前 bar 量 / 前 lookback 根均量（日内滚动）
- vol_day_ratio: 累计量 / (前日全天量 × 时段进度) — 相对前日同期放量程度
- cmf:           滚动 Chaikin Money Flow（价量方向代理）

买卖规则（完全替代时钟）：
- 选股：当日涨幅 TOP1（与 time_combo 一致，涨幅 ≥MIN_GAIN）
- 买入：09:31~14:54 扫描，TOP1 首次 vol_ratio ≥ buy_vol 时买入
- 卖出：次日开盘起扫描，按 sell_mode 触发；无信号则次日最后一根 bar 收盘卖

用法:
    python scripts/search_t0_vol_combo.py --ndays 5 --top 20
    python scripts/search_t0_vol_combo.py --ndays 9 --analyze
    python scripts/search_t0_vol_combo.py --ndays 9 --compare-baseline
    python scripts/search_t0_vol_combo.py --combo 10,2.0,below,0.8
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import _calc_stats  # noqa: E402
from backtest_t0_1min import (  # noqa: E402
    MIN_TRADES_1MIN,
    load_1min_data,
    run_combo_1min,
)
from backtest_t0_etf import apply_net_return, bar_time_min  # noqa: E402
from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    MIN_GAIN,
    regime_on_date,
    resolve_eval_dates,
    select_etf,
)
from search_t0_time_combo import BASELINE  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

MIN_TRADES = MIN_TRADES_1MIN
SESSION_START = 9 * 60 + 31
SESSION_END = 14 * 60 + 54
LUNCH_START = 11 * 60 + 30
LUNCH_END = 13 * 60

DEFAULT_LOOKBACKS = [5, 10, 15]
DEFAULT_BUY_VOLS = [1.5, 2.0, 2.5, 3.0, 4.0]
DEFAULT_SELL_BELOW = [0.6, 0.8, 1.0, 1.2]
DEFAULT_SELL_SPIKE = [2.5, 3.0, 4.0]
DEFAULT_EXHAUST_PCT = [30, 50]


def time_to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def in_buy_session(bar_min: int) -> bool:
    if bar_min < SESSION_START or bar_min > SESSION_END:
        return False
    if LUNCH_START <= bar_min < LUNCH_END:
        return False
    return True


def prev_close(etf_daily: dict, code: str, day: str) -> float | None:
    info = etf_daily.get(code)
    if not info:
        return None
    returns = info["returns"]
    idx_map = {r["date"]: i for i, r in enumerate(returns)}
    if day not in idx_map or idx_map[day] == 0:
        return None
    pc = returns[idx_map[day] - 1]["close"]
    return pc if pc and pc > 0 else None


def prev_day_volume(
    etf_1min: dict,
    code: str,
    day: str,
    all_dates: list[str],
) -> float:
    if day not in all_dates:
        return 0.0
    idx = all_dates.index(day)
    if idx == 0:
        return 0.0
    prev_day = all_dates[idx - 1]
    bars = etf_1min.get(code, {}).get(prev_day, [])
    return sum(float(b.get("volume", 0)) for b in bars)


def _bar_mfm(bar: dict) -> float:
    high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
    vol = float(bar.get("volume", 0))
    if high <= low or vol <= 0:
        return 0.0
    return ((2 * close - high - low) / (high - low)) * vol


def enrich_bars(bars: list[dict], prev_day_vol: float, lookback: int) -> list[dict]:
    """为每根 bar 附加因果相对量能指标。"""
    out: list[dict] = []
    n = len(bars)
    for i, bar in enumerate(bars):
        vol = float(bar.get("volume", 0))
        hist = [float(bars[j].get("volume", 0)) for j in range(max(0, i - lookback), i)]
        avg_vol = sum(hist) / len(hist) if hist and sum(hist) > 0 else vol
        vol_ratio = vol / avg_vol if avg_vol > 0 else 1.0

        cum_vol = sum(float(bars[j].get("volume", 0)) for j in range(i + 1))
        progress = (i + 1) / n if n else 1.0
        expected = prev_day_vol * progress if prev_day_vol > 0 else cum_vol
        vol_day_ratio = cum_vol / expected if expected > 0 else 1.0

        mfm_slice = [_bar_mfm(bars[j]) for j in range(max(0, i - lookback + 1), i + 1)]
        vol_slice = [float(bars[j].get("volume", 0)) for j in range(max(0, i - lookback + 1), i + 1)]
        vol_sum = sum(vol_slice)
        cmf = sum(mfm_slice) / vol_sum if vol_sum > 0 else 0.0

        out.append({
            **bar,
            "vol_ratio": vol_ratio,
            "vol_day_ratio": vol_day_ratio,
            "cmf": cmf,
            "bar_idx": i,
        })
    return out


def rank_top1_at_bar(
    etf_list: list[dict],
    etf_daily: dict,
    enriched_by_code: dict[str, list[dict]],
    day: str,
    bar_idx: int,
) -> tuple[float, dict] | None:
    scores: list[tuple[float, dict]] = []
    for etf_info in etf_list:
        code = etf_info["code"]
        pc = prev_close(etf_daily, code, day)
        eb = enriched_by_code.get(code)
        if not pc or not eb or bar_idx >= len(eb):
            continue
        close = float(eb[bar_idx]["close"])
        if close <= 0:
            continue
        gain = (close - pc) / pc * 100
        scores.append((gain, etf_info))
    if len(scores) < 2:
        return None
    scores.sort(key=lambda x: x[0], reverse=True)
    return scores[0]


def find_vol_buy(
    etf_list: list[dict],
    etf_daily: dict,
    enriched_by_code: dict[str, list[dict]],
    day: str,
    buy_vol: float,
    use_filter: bool,
) -> tuple[str, dict, float, int, str, dict] | None:
    """返回 (code, etf_info, gain, bar_idx, buy_time, metrics)。"""
    max_len = max((len(v) for v in enriched_by_code.values()), default=0)
    for i in range(max_len):
        sample = next((v[i] for v in enriched_by_code.values() if i < len(v)), None)
        if not sample:
            continue
        t = sample.get("time", "00:00")
        if len(t) >= 5:
            t = t[:5]
        bm = bar_time_min({"time": t + ":00" if len(t) == 5 else t})
        if not in_buy_session(bm):
            continue

        picked = rank_top1_at_bar(etf_list, etf_daily, enriched_by_code, day, i)
        if not picked:
            continue
        gain, etf_info = picked
        if use_filter and gain < MIN_GAIN:
            continue
        code = etf_info["code"]
        bar = enriched_by_code.get(code, [])[i]
        if bar["vol_ratio"] < buy_vol:
            continue
        return code, etf_info, gain, i, t, {
            "vol_ratio": round(bar["vol_ratio"], 3),
            "vol_day_ratio": round(bar["vol_day_ratio"], 3),
            "cmf": round(bar["cmf"], 4),
        }
    return None


def find_vol_sell(
    enriched: list[dict],
    sell_mode: str,
    sell_vol: float,
    exhaust_pct: float = 50.0,
    buy_price: float | None = None,
    stop_loss_pct: float | None = None,
) -> tuple[int, str, dict, float]:
    """返回 (bar_idx, reason, metrics, sell_price)。"""
    peak = 0.0
    sl_price = None
    if buy_price and stop_loss_pct is not None:
        sl_price = buy_price * (1 + stop_loss_pct / 100)

    for i, bar in enumerate(enriched):
        vr = bar["vol_ratio"]
        metrics = {
            "vol_ratio": round(vr, 3),
            "vol_day_ratio": round(bar["vol_day_ratio"], 3),
            "cmf": round(bar["cmf"], 4),
        }
        if sl_price is not None and float(bar["low"]) <= sl_price:
            return i, "stop_loss", metrics, sl_price
        if sell_mode == "below" and vr < sell_vol:
            return i, "vol_below", metrics, float(bar["close"])
        if sell_mode == "spike" and vr >= sell_vol:
            return i, "vol_spike", metrics, float(bar["close"])
        if sell_mode == "exhaust":
            peak = max(peak, vr)
            if peak >= 1.5 and peak > 0:
                drop = (peak - vr) / peak * 100
                if drop >= exhaust_pct:
                    return i, f"vol_exhaust_{int(exhaust_pct)}", metrics, float(bar["close"])

    last = len(enriched) - 1
    bar = enriched[last]
    metrics = {
        "vol_ratio": round(bar["vol_ratio"], 3),
        "vol_day_ratio": round(bar["vol_day_ratio"], 3),
        "cmf": round(bar["cmf"], 4),
    }
    return last, "close", metrics, float(bar["close"])


def combo_label(
    lookback: int,
    buy_vol: float,
    sell_mode: str,
    sell_vol: float,
    exhaust_pct: float | None = None,
    stop_loss_pct: float | None = None,
) -> str:
    mode_labels = {
        "below": f"量缩<{sell_vol}",
        "spike": f"量峰≥{sell_vol}",
        "exhaust": f"量峰回落≥{int(exhaust_pct or 50)}%",
    }
    base = f"LB{lookback} 买≥{buy_vol} {mode_labels.get(sell_mode, sell_mode)}"
    if stop_loss_pct is not None:
        base += f" SL{stop_loss_pct:+.0f}%"
    return base


def vol_combo_key(
    lookback: int,
    buy_vol: float,
    sell_mode: str,
    sell_vol: float,
    exhaust_pct: float | None = None,
    stop_loss_pct: float | None = None,
) -> str:
    ep = "" if exhaust_pct is None else f",{exhaust_pct}"
    sl = "" if stop_loss_pct is None else f",sl{stop_loss_pct}"
    return f"{lookback},{buy_vol},{sell_mode},{sell_vol}{ep}{sl}"


def vol_spec_from_key(key: str) -> dict:
    parts = key.split(",")
    spec: dict = {
        "lookback": int(parts[0]),
        "buy_vol": float(parts[1]),
        "sell_mode": parts[2],
        "sell_vol": float(parts[3]),
        "exhaust_pct": None,
        "stop_loss_pct": None,
    }
    for p in parts[4:]:
        if p.startswith("sl"):
            spec["stop_loss_pct"] = float(p[2:])
        else:
            spec["exhaust_pct"] = float(p)
    ep = spec["exhaust_pct"]
    sl = spec["stop_loss_pct"]
    spec["label"] = combo_label(
        spec["lookback"], spec["buy_vol"], spec["sell_mode"],
        spec["sell_vol"], ep, sl,
    )
    spec["combo_key"] = key
    return spec


def iter_narrow_vol_combos() -> list[tuple[int, float, str, float, float | None]]:
    """归因收窄网格（约 24 组）。"""
    return iter_vol_combos(
        lookbacks=[5, 15],
        buy_vols=[2.5, 3.0, 4.0],
        sell_below=[0.8],
        sell_spike=[3.0, 4.0],
        exhaust_pcts=[50],
    )


def iter_vol_combos(
    lookbacks: list[int] | None = None,
    buy_vols: list[float] | None = None,
    sell_below: list[float] | None = None,
    sell_spike: list[float] | None = None,
    exhaust_pcts: list[float] | None = None,
) -> list[tuple[int, float, str, float, float | None]]:
    lookbacks = lookbacks or DEFAULT_LOOKBACKS
    buy_vols = buy_vols or DEFAULT_BUY_VOLS
    sell_below = sell_below or DEFAULT_SELL_BELOW
    sell_spike = sell_spike or DEFAULT_SELL_SPIKE
    exhaust_pcts = exhaust_pcts or DEFAULT_EXHAUST_PCT
    combos: list[tuple[int, float, str, float, float | None]] = []
    for lb in lookbacks:
        for bv in buy_vols:
            for sv in sell_below:
                combos.append((lb, bv, "below", sv, None))
            for sv in sell_spike:
                combos.append((lb, bv, "spike", sv, None))
            for ep in exhaust_pcts:
                combos.append((lb, bv, "exhaust", 0.0, ep))
    return combos


def precompute_enriched(
    etf_list: list[dict],
    etf_1min: dict,
    etf_daily: dict,
    eval_dates: list[str],
    all_dates: list[str],
    lookback: int,
) -> dict[tuple[str, str], list[dict]]:
    cache: dict[tuple[str, str], list[dict]] = {}
    for etf_info in etf_list:
        code = etf_info["code"]
        for day in eval_dates:
            bars = etf_1min.get(code, {}).get(day, [])
            if not bars:
                continue
            pdv = prev_day_volume(etf_1min, code, day, all_dates)
            cache[(code, day)] = enrich_bars(bars, pdv, lookback)
    return cache


def run_vol_combo(
    lookback: int,
    buy_vol: float,
    sell_mode: str,
    sell_vol: float,
    exhaust_pct: float | None,
    etf_list: list[dict],
    etf_daily: dict,
    etf_bars: dict,
    eval_dates: list[str],
    all_dates: list[str],
    enriched_cache: dict[tuple[str, str], list[dict]] | None,
    fee_pct: float,
    use_filter: bool,
    skip_choppy: bool,
    proxy_klines: list[dict],
    min_trades: int | None = None,
    stop_loss_pct: float | None = None,
) -> dict | None:
    if enriched_cache is None:
        enriched_cache = precompute_enriched(
            etf_list, etf_bars, etf_daily, eval_dates, all_dates, lookback,
        )

    rets: list[float] = []
    trades: list[dict] = []
    for day in eval_dates:
        if skip_choppy:
            regime = regime_on_date(proxy_klines, day)
            if regime and regime.get("skip_choppy"):
                continue

        day_enriched = {
            e["code"]: enriched_cache.get((e["code"], day), [])
            for e in etf_list
            if enriched_cache.get((e["code"], day))
        }
        if len(day_enriched) < 2:
            continue

        buy_hit = find_vol_buy(etf_list, etf_daily, day_enriched, day, buy_vol, use_filter)
        if not buy_hit:
            continue
        code, etf_info, gain, buy_idx, buy_time, buy_metrics = buy_hit

        if day not in all_dates:
            continue
        idx = all_dates.index(day)
        if idx + 1 >= len(all_dates):
            continue
        next_day = all_dates[idx + 1]
        next_bars = etf_bars.get(code, {}).get(next_day, [])
        if not next_bars:
            continue

        pdv = prev_day_volume(etf_bars, code, next_day, all_dates)
        next_enriched = enrich_bars(next_bars, pdv, lookback)
        buy_price = float(day_enriched[code][buy_idx]["close"])
        sell_idx, sell_reason, sell_metrics, sell_price = find_vol_sell(
            next_enriched, sell_mode, sell_vol, exhaust_pct or 50.0,
            buy_price=buy_price, stop_loss_pct=stop_loss_pct,
        )
        if buy_price <= 0 or sell_price <= 0:
            continue

        ret = apply_net_return(buy_price, sell_price, fee_pct)
        rets.append(ret)
        trades.append({
            "signal_date": day,
            "etf": code,
            "name": etf_info.get("name", ""),
            "today_gain": round(gain, 2),
            "buy_time": buy_time,
            "sell_date": next_day,
            "sell_time": next_enriched[sell_idx].get("time", "")[:5],
            "buy_price": round(buy_price, 4),
            "sell_price": round(sell_price, 4),
            "sell_reason": sell_reason,
            "return_pct": ret,
            "buy_metrics": buy_metrics,
            "sell_metrics": sell_metrics,
        })

    need = min_trades if min_trades is not None else MIN_TRADES
    if len(rets) < need:
        return None

    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    stats = _calc_stats(rets)
    ep = exhaust_pct if sell_mode == "exhaust" else None
    label = combo_label(lookback, buy_vol, sell_mode, sell_vol, ep, stop_loss_pct)
    return {
        "lookback": lookback,
        "buy_vol": buy_vol,
        "sell_mode": sell_mode,
        "sell_vol": sell_vol,
        "exhaust_pct": ep,
        "stop_loss_pct": stop_loss_pct,
        "label": label,
        "combo_key": vol_combo_key(lookback, buy_vol, sell_mode, sell_vol, ep, stop_loss_pct),
        "trade_count": len(rets),
        "final_equity_pct": (eq - 1) * 100,
        "stats": stats,
        "trades": trades,
    }


def metrics_at_time(
    enriched: list[dict],
    target_time: str,
) -> dict | None:
    target_min = time_to_min(target_time)
    best = None
    best_diff = 9999
    for bar in enriched:
        t = bar.get("time", "00:00")[:5]
        bm = bar_time_min({"time": t + ":00"})
        if bm <= target_min:
            diff = target_min - bm
            if diff < best_diff:
                best_diff = diff
                best = bar
    if not best:
        return None
    return {
        "vol_ratio": round(best["vol_ratio"], 3),
        "vol_day_ratio": round(best["vol_day_ratio"], 3),
        "cmf": round(best["cmf"], 4),
    }


def analyze_baseline(
    etf_list: list[dict],
    etf_daily: dict,
    etf_1min: dict,
    eval_dates: list[str],
    all_dates: list[str],
    proxy_klines: list[dict],
    fee_pct: float,
    use_filter: bool,
    skip_choppy: bool,
    lookback: int = 10,
) -> dict:
    """在时间基线交易的买/卖点做量能归因。"""
    import search_t0_time_combo as stc

    old_min = stc.MIN_TRADES
    stc.MIN_TRADES = MIN_TRADES
    picks = stc.precompute_picks(
        etf_list, etf_daily, etf_1min, eval_dates,
        [BASELINE["signal"]], proxy_klines, use_filter, skip_choppy,
    )
    baseline = run_combo_1min(
        BASELINE["signal"], BASELINE["buy"],
        BASELINE["sell_mode"], BASELINE["sell_cutoff"],
        eval_dates, all_dates, picks, etf_1min, fee_pct,
    )
    stc.MIN_TRADES = old_min

    if not baseline or not baseline.get("trades"):
        return {"baseline": baseline, "samples": [], "summary": {}}

    samples: list[dict] = []
    for t in baseline["trades"]:
        code = t["etf"]
        day = t["signal_date"]
        pdv = prev_day_volume(etf_1min, code, day, all_dates)
        day_e = enrich_bars(etf_1min.get(code, {}).get(day, []), pdv, lookback)
        buy_m = metrics_at_time(day_e, BASELINE["buy"])

        sell_day = None
        if day in all_dates:
            idx = all_dates.index(day)
            if idx + 1 < len(all_dates):
                sell_day = all_dates[idx + 1]
        sell_m = None
        if sell_day:
            pdv2 = prev_day_volume(etf_1min, code, sell_day, all_dates)
            sell_e = enrich_bars(etf_1min.get(code, {}).get(sell_day, []), pdv2, lookback)
            sell_m = metrics_at_time(sell_e, BASELINE.get("sell_cutoff", "11:05"))

        samples.append({
            **t,
            "buy_metrics": buy_m,
            "sell_metrics": sell_m,
            "win": t["return_pct"] > 0,
        })

    def _summarize(group: list[dict], key: str) -> dict:
        vals = [
            s[key]["vol_ratio"]
            for s in group
            if s.get(key) and s[key].get("vol_ratio") is not None
        ]
        if not vals:
            return {}
        vals.sort()
        n = len(vals)
        return {
            "count": n,
            "mean": round(statistics.mean(vals), 3),
            "p25": round(vals[max(0, n // 4 - 1)], 3),
            "p50": round(statistics.median(vals), 3),
            "p75": round(vals[min(n - 1, 3 * n // 4)], 3),
        }

    wins = [s for s in samples if s["win"]]
    losses = [s for s in samples if not s["win"]]
    summary = {
        "baseline_equity_pct": baseline["final_equity_pct"],
        "trade_count": len(samples),
        "win_rate": round(100 * len(wins) / len(samples), 1) if samples else 0,
        "buy_vol_ratio_win": _summarize(wins, "buy_metrics"),
        "buy_vol_ratio_loss": _summarize(losses, "buy_metrics"),
        "sell_vol_ratio_win": _summarize(wins, "sell_metrics"),
        "sell_vol_ratio_loss": _summarize(losses, "sell_metrics"),
        "suggested_buy_vol": _summarize(wins, "buy_metrics").get("p25"),
        "suggested_sell_below": _summarize(wins, "sell_metrics").get("p75"),
    }
    return {"baseline": baseline, "samples": samples, "summary": summary}


def print_analyze_report(report: dict, lookback: int) -> None:
    bl = report.get("baseline")
    summary = report.get("summary") or {}
    print("=" * 90)
    print(f"  量能归因（时间基线 {BASELINE['label']}，lookback={lookback}）")
    print("=" * 90)
    if not bl:
        print("  无有效基线交易")
        return
    print(f"  基线累计: {bl['final_equity_pct']:+.2f}% | {bl['trade_count']} 笔 | 胜率 {summary.get('win_rate', 0):.1f}%")
    print()
    print(f"  {'分组':<10} {'买 vol_ratio':>36} {'卖 vol_ratio':>36}")
    print("  " + "-" * 86)

    def fmt(s: dict) -> str:
        if not s:
            return "—"
        return f"μ={s.get('mean', 0):.2f} p25={s.get('p25', 0):.2f} p50={s.get('p50', 0):.2f} p75={s.get('p75', 0):.2f}"

    print(f"  {'盈利':<10} {fmt(summary.get('buy_vol_ratio_win', {})):>36} {fmt(summary.get('sell_vol_ratio_win', {})):>36}")
    print(f"  {'亏损':<10} {fmt(summary.get('buy_vol_ratio_loss', {})):>36} {fmt(summary.get('sell_vol_ratio_loss', {})):>36}")
    print()
    sb = summary.get("suggested_buy_vol")
    ss = summary.get("suggested_sell_below")
    if sb is not None:
        print(f"  建议网格起点: buy_vol ≥ {sb:.2f}（盈利笔买量 p25）", end="")
        if ss is not None:
            print(f" | sell_below < {ss:.2f}（盈利笔卖量 p75）")
        else:
            print()
    print("=" * 90)


def print_top_results(results: list[dict], top: int) -> None:
    print("=" * 115)
    print(f"  T+0 量能组合 TOP {top}（按累计收益）")
    print("=" * 115)
    print(f"  {'#':>3} {'组合':<32} {'笔数':>4} {'累计':>9} {'胜率':>6} {'均笔':>7} {'回撤':>8}")
    print("  " + "─" * 95)
    for i, r in enumerate(results[:top], 1):
        st = r["stats"]
        print(
            f"  {i:>3} {r['label']:<32} {r['trade_count']:>4} "
            f"{r['final_equity_pct']:+8.2f}% {st.get('win_rate', 0):>5.1f}% "
            f"{st.get('avg', 0):>+6.2f}% {st.get('max_drawdown', 0):>+7.2f}%"
        )
    print("=" * 115)


def print_trade_detail(result: dict, title: str) -> None:
    print()
    print("=" * 100)
    print(f"  {title}")
    print("=" * 100)
    st = result.get("stats") or {}
    print(
        f"  {result.get('label')} | {result['trade_count']} 笔 | "
        f"累计 {result['final_equity_pct']:+.2f}% | 胜率 {st.get('win_rate', 0):.1f}%"
    )
    trades = result.get("trades") or []
    if trades:
        print(f"\n  {'信号日':>12} {'买':>5} {'卖日':>12} {'卖':>5} {'ETF':>8} {'买VR':>6} {'卖VR':>6} {'收益':>8}")
        print("  " + "-" * 78)
        for t in trades:
            bvr = t.get("buy_metrics", {}).get("vol_ratio", 0)
            svr = t.get("sell_metrics", {}).get("vol_ratio", 0)
            print(
                f"  {t['signal_date']:>12} {t.get('buy_time', ''):>5} {t.get('sell_date', ''):>12} "
                f"{t.get('sell_time', ''):>5} {t['etf']:>8} {bvr:>6.2f} {svr:>6.2f} {t['return_pct']:+7.2f}%"
            )
    print("=" * 100)


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 ETF 相对量能网格搜索（替代固定时间）")
    parser.add_argument("--ndays", type=int, default=5, help="1 分 K 天数（东财 ndays）")
    parser.add_argument("--source", choices=["auto", "em", "sina"], default="auto")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--no-filter", action="store_true")
    parser.add_argument("--skip-choppy", action="store_true")
    parser.add_argument("--analyze", action="store_true", help="时间基线量能归因")
    parser.add_argument("--compare-baseline", action="store_true", help="量能最优 vs 时间基线")
    parser.add_argument("--lookback", type=int, default=10, help="归因分析用 lookback")
    parser.add_argument("--min-trades", type=int, default=5, help="最少成交笔数（默认 5）")
    parser.add_argument("--stop-loss", type=float, default=None,
                        help="次日止损 %%（如 -3 表示 -3%%）")
    parser.add_argument(
        "--combo", type=str, default="",
        help="单组合: lookback,buy_vol,sell_mode,sell_vol[,exhaust_pct][,sl-3]",
    )
    args = parser.parse_args()

    use_filter = not args.no_filter
    skip_choppy = args.skip_choppy
    min_trades = args.min_trades
    stop_loss = args.stop_loss
    etf_list = get_all_t0_etfs()

    etf_daily, etf_1min, all_dates, proxy_klines, data_source = load_1min_data(
        etf_list, ndays=args.ndays, source=args.source,
    )
    eval_dates = resolve_eval_dates(all_dates, args.ndays, "", "")
    m1_dates = sorted({d for bars in etf_1min.values() for d in bars})
    eval_dates = [d for d in eval_dates if d in m1_dates]
    if len(eval_dates) < 2:
        print(f"ERROR: 1 分 K 有效交易日不足 ({len(eval_dates)} 天, source={data_source})")
        sys.exit(1)

    print("=== T+0 ETF 相对量能网格搜索 ===")
    print(f"  数据: {data_source} | 评估 {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)} 天)")
    print(f"  规则: 涨幅TOP1 + vol_ratio 触发买卖（无固定时钟）")
    if use_filter:
        print(f"  过滤: 当日涨幅 ≥{MIN_GAIN}%")
    if stop_loss is not None:
        print(f"  止损: 次日 {stop_loss:+.1f}%")
    print(f"  MIN_TRADES: {min_trades}")

    if args.analyze:
        report = analyze_baseline(
            etf_list, etf_daily, etf_1min, eval_dates, all_dates, proxy_klines,
            args.fee, use_filter, skip_choppy, lookback=args.lookback,
        )
        print_analyze_report(report, args.lookback)
        out_dir = Path.home() / ".tradingagents" / "rotation"
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d_%H%M")
        out_path = out_dir / f"t0_vol_analyze_{tag}.json"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n归因结果已保存: {out_path}")
        return

    if args.combo:
        parts = args.combo.split(",")
        if len(parts) < 4:
            print("ERROR: --combo 格式: lookback,buy_vol,sell_mode,sell_vol[,exhaust_pct]")
            sys.exit(1)
        lb = int(parts[0])
        bv = float(parts[1])
        mode = parts[2]
        sv = float(parts[3])
        ep = float(parts[4]) if len(parts) > 4 and not parts[4].startswith("sl") else None
        sl = stop_loss
        for p in parts[4:]:
            if p.startswith("sl"):
                sl = float(p[2:])
        cache = precompute_enriched(etf_list, etf_1min, etf_daily, eval_dates, all_dates, lb)
        r = run_vol_combo(
            lb, bv, mode, sv, ep,
            etf_list, etf_daily, etf_1min, eval_dates, all_dates, cache,
            args.fee, use_filter, skip_choppy, proxy_klines,
            min_trades=min_trades, stop_loss_pct=sl,
        )
        if not r:
            print("ERROR: 该组合无有效交易")
            sys.exit(1)
        print_trade_detail(r, f"单组合 — {r['label']}")
        return

    combos = iter_vol_combos()
    print(f">>> 网格搜索 {len(combos)} 种量能组合（MIN_TRADES={min_trades}）...")

    caches: dict[int, dict] = {}
    results: list[dict] = []
    for lb, bv, mode, sv, ep in combos:
        if lb not in caches:
            caches[lb] = precompute_enriched(
                etf_list, etf_1min, etf_daily, eval_dates, all_dates, lb,
            )
        r = run_vol_combo(
            lb, bv, mode, sv, ep,
            etf_list, etf_daily, etf_1min, eval_dates, all_dates, caches[lb],
            args.fee, use_filter, skip_choppy, proxy_klines,
            min_trades=min_trades, stop_loss_pct=stop_loss,
        )
        if r:
            results.append({k: v for k, v in r.items() if k != "trades"})

    results.sort(key=lambda x: x["final_equity_pct"], reverse=True)
    print(f"    有效组合: {len(results)}\n")
    if results:
        print_top_results(results, args.top)

    baseline = None
    if args.compare_baseline or results:
        import search_t0_time_combo as stc

        old_min = stc.MIN_TRADES
        stc.MIN_TRADES = min(min_trades, 2)  # 1分K短窗基线对比放宽
        picks = stc.precompute_picks(
            etf_list, etf_daily, etf_1min, eval_dates,
            [BASELINE["signal"]], proxy_klines, use_filter, skip_choppy,
        )
        baseline = run_combo_1min(
            BASELINE["signal"], BASELINE["buy"],
            BASELINE["sell_mode"], BASELINE["sell_cutoff"],
            eval_dates, all_dates, picks, etf_1min, args.fee,
        )
        stc.MIN_TRADES = old_min

    if baseline and results:
        print()
        print("=" * 90)
        print("  量能最优 vs 时间基线")
        print("=" * 90)
        best = results[0]
        bl_st = baseline.get("stats") or {}
        bt_st = best.get("stats") or {}
        print(f"  {'策略':<40} {'笔数':>4} {'累计':>9} {'胜率':>6} {'均笔':>7}")
        print("  " + "-" * 72)
        print(
            f"  {best['label']:<40} {best['trade_count']:>4} "
            f"{best['final_equity_pct']:+8.2f}% {bt_st.get('win_rate', 0):>5.1f}% "
            f"{bt_st.get('avg', 0):>+6.2f}%"
        )
        print(
            f"  {BASELINE['label']:<40} {baseline['trade_count']:>4} "
            f"{baseline['final_equity_pct']:+8.2f}% {bl_st.get('win_rate', 0):>5.1f}% "
            f"{bl_st.get('avg', 0):>+6.2f}%"
        )
        diff = best["final_equity_pct"] - baseline["final_equity_pct"]
        print(f"\n  量能最优 vs 基线: {diff:+.2f} pp")
        print("=" * 90)

        best_detail = run_vol_combo(
            best["lookback"], best["buy_vol"], best["sell_mode"],
            best["sell_vol"], best.get("exhaust_pct"),
            etf_list, etf_daily, etf_1min, eval_dates, all_dates, caches.get(best["lookback"]),
            args.fee, use_filter, skip_choppy, proxy_klines,
            min_trades=min_trades, stop_loss_pct=stop_loss,
        )
        if best_detail:
            print_trade_detail(best_detail, f"量能 TOP1 — {best_detail['label']}")

    out_dir = Path.home() / ".tradingagents" / "rotation"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = out_dir / f"t0_vol_search_{tag}.json"
    out_path.write_text(
        json.dumps({
            "config": {
                "ndays": args.ndays,
                "data_source": data_source,
                "eval_dates": eval_dates,
                "use_filter": use_filter,
            },
            "top_results": results[: args.top],
            "baseline": {k: v for k, v in (baseline or {}).items() if k != "trades"} if baseline else None,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()

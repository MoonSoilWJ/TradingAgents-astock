#!/usr/bin/env python3
"""T+0 量能策略 Walk-Forward — 训练窗网格 + 样本外验证 vs 时间基线。

使用 5 分 K（约 80+ 交易日历史）做 WF；量能逻辑与 search_t0_vol_combo 相同。
1 分 K 历史仅 ~9 天，不足以做 60/20 窗，故 WF 默认 5 分 K。

用法:
    python scripts/t0_vol_walk_forward.py
    python scripts/t0_vol_walk_forward.py --train 60 --validate 20 --scope narrow
    python scripts/t0_vol_walk_forward.py --train 40 --validate 15 --stop-loss -3
    python scripts/t0_vol_walk_forward.py --scope full --min-edge 5
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_today1 import FEE_PCT, load_market_data, resolve_eval_dates  # noqa: E402
from search_t0_time_combo import (  # noqa: E402
    BASELINE,
    precompute_picks,
    run_combo,
    segment_stats,
)
from search_t0_vol_combo import (  # noqa: E402
    iter_narrow_vol_combos,
    iter_vol_combos,
    precompute_enriched,
    run_vol_combo,
    vol_combo_key,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

STATE_DIR = Path.home() / ".tradingagents" / "rotation"
STATE_FILE = STATE_DIR / "t0_vol_walk_forward_state.json"


def train_stability_ok(trades: list[dict], train_dates: list[str], min_positive: int) -> tuple[bool, list[float]]:
    if len(train_dates) < 9:
        return True, []
    seg_size = len(train_dates) // 3
    segs = [
        train_dates[:seg_size],
        train_dates[seg_size: 2 * seg_size],
        train_dates[2 * seg_size:],
    ]
    totals = [segment_stats(trades, ds)["total"] for ds in segs]
    positive = sum(1 for t in totals if t > 0)
    return positive >= min_positive, totals


def search_vol_on_window(
    combos: list[tuple[int, float, str, float, float | None]],
    eval_dates: list[str],
    all_dates: list[str],
    etf_list: list[dict],
    etf_daily: dict,
    etf_bars: dict,
    proxy_klines: list[dict],
    fee_pct: float,
    min_trades: int,
    require_stable: bool,
    min_positive_segments: int,
    stop_loss_pct: float | None,
    enriched_by_lb: dict[int, dict],
) -> list[dict]:
    results: list[dict] = []
    for lb, bv, mode, sv, ep in combos:
        cache = enriched_by_lb.get(lb)
        if cache is None:
            cache = precompute_enriched(
                etf_list, etf_bars, etf_daily, eval_dates, all_dates, lb,
            )
            enriched_by_lb[lb] = cache
        r = run_vol_combo(
            lb, bv, mode, sv, ep,
            etf_list, etf_daily, etf_bars, eval_dates, all_dates, cache,
            fee_pct, use_filter=True, skip_choppy=True, proxy_klines=proxy_klines,
            min_trades=min_trades, stop_loss_pct=stop_loss_pct,
        )
        if not r:
            continue
        stable, seg_totals = train_stability_ok(r["trades"], eval_dates, min_positive_segments)
        if require_stable and not stable:
            continue
        r["train_seg_totals"] = seg_totals
        results.append(r)
    results.sort(key=lambda x: x["final_equity_pct"], reverse=True)
    return results


def eval_vol_spec(
    spec: dict,
    eval_dates: list[str],
    all_dates: list[str],
    etf_list: list[dict],
    etf_daily: dict,
    etf_bars: dict,
    proxy_klines: list[dict],
    fee_pct: float,
    min_trades: int,
    stop_loss_pct: float | None,
    enriched_cache: dict | None,
) -> dict | None:
    return run_vol_combo(
        spec["lookback"], spec["buy_vol"], spec["sell_mode"], spec["sell_vol"],
        spec.get("exhaust_pct"),
        etf_list, etf_daily, etf_bars, eval_dates, all_dates, enriched_cache,
        fee_pct, use_filter=True, skip_choppy=True, proxy_klines=proxy_klines,
        min_trades=min_trades, stop_loss_pct=stop_loss_pct,
    )


def eval_time_baseline(
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_bars: dict,
    fee_pct: float,
    min_trades: int,
) -> dict | None:
    import search_t0_time_combo as stc

    old_min = stc.MIN_TRADES
    stc.MIN_TRADES = min_trades
    try:
        return run_combo(
            BASELINE["signal"], BASELINE["buy"],
            BASELINE["sell_mode"], BASELINE["sell_cutoff"],
            eval_dates, all_dates, picks, etf_bars, fee_pct,
        )
    finally:
        stc.MIN_TRADES = old_min


def decide_recommendation(
    baseline_val: dict | None,
    candidate_val: dict | None,
    candidate_train: dict | None,
    min_edge_pp: float,
    min_validate_trades: int,
) -> tuple[str, str, bool]:
    if not candidate_train:
        return "保持基线", "训练窗无满足稳定性/笔数要求的量能组合", False
    if not candidate_val:
        return "保持基线", "量能候选在样本外无足够交易", False
    if candidate_val["trade_count"] < min_validate_trades:
        return (
            "保持基线",
            f"样本外仅 {candidate_val['trade_count']} 笔 (< {min_validate_trades})",
            False,
        )
    bl_ret = baseline_val["final_equity_pct"] if baseline_val else 0.0
    cand_ret = candidate_val["final_equity_pct"]
    edge = cand_ret - bl_ret
    if edge < min_edge_pp:
        if edge >= 0:
            reason = (
                f"样本外量能 {cand_ret:+.2f}% vs 时间基线 {bl_ret:+.2f}%"
                f"（仅领先 {edge:+.2f}pp < {min_edge_pp}pp）"
            )
        else:
            reason = f"样本外量能 {cand_ret:+.2f}% vs 时间基线 {bl_ret:+.2f}%（落后 {-edge:.2f}pp）"
        return "保持基线", reason, False
    return (
        "可考虑切换",
        f"样本外量能 {cand_ret:+.2f}% vs 时间基线 {bl_ret:+.2f}%（领先 {edge:+.2f}pp）",
        True,
    )


def print_report(
    train_dates: list[str],
    validate_dates: list[str],
    baseline_train: dict | None,
    baseline_val: dict | None,
    candidate_train: dict | None,
    candidate_val: dict | None,
    top_train: list[dict],
    label: str,
    detail: str,
    switch: bool,
    combos_searched: int,
    stop_loss_pct: float | None,
):
    print()
    print("=" * 95)
    print("  T+0 量能策略 Walk-Forward 复核（5 分 K）")
    print("=" * 95)
    print(f"  训练窗: {train_dates[0]} ~ {train_dates[-1]} ({len(train_dates)} 日)")
    print(f"  验证窗: {validate_dates[0]} ~ {validate_dates[-1]} ({len(validate_dates)} 日)")
    print(f"  搜索组合: {combos_searched} | 训练有效: {len(top_train)}")
    if stop_loss_pct is not None:
        print(f"  次日止损: {stop_loss_pct:+.1f}%")
    print(f"  时间基线: {BASELINE['label']}")
    print()

    def row(name: str, r: dict | None, is_vol: bool = False):
        if not r:
            print(f"  {name:<14} —")
            return
        st = r.get("stats") or {}
        tag = r.get("label", "") if is_vol else r.get("label", BASELINE["label"])
        print(
            f"  {name:<14} {tag:<34} {r['trade_count']:>3}笔 "
            f"{r['final_equity_pct']:+8.2f}% 胜率{st.get('win_rate', 0):>5.1f}% "
            f"回撤{st.get('max_drawdown', 0):+7.2f}%"
        )

    print("  【训练窗】")
    row("时间基线", baseline_train, False)
    row("量能网格最优", candidate_train, True)
    print()
    print("  【样本外验证】")
    row("时间基线", baseline_val, False)
    row("量能网格最优", candidate_val, True)
    print()

    if top_train[:5]:
        print("  训练窗 TOP5（量能）:")
        print(f"  {'#':>2} {'组合':<36} {'笔':>3} {'累计':>8} {'分段(+/-/+)':>16}")
        print("  " + "-" * 72)
        for i, r in enumerate(top_train[:5], 1):
            segs = r.get("train_seg_totals") or []
            seg_s = "/".join(f"{s:+.1f}" for s in segs) if segs else "—"
            print(
                f"  {i:>2} {r['label']:<36} {r['trade_count']:>3} "
                f"{r['final_equity_pct']:+7.2f}% {seg_s:>16}"
            )
        print()

    icon = "✅" if switch else "⛔"
    print(f"  {icon} 结论: {label}")
    print(f"     {detail}")
    if switch and candidate_train:
        print()
        print(f"  若切换，建议量能参数: {candidate_train['combo_key']}")
        print(f"  （请先小仓位试跑 1～2 周，勿立即全仓）")
    print("=" * 95)


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 量能 Walk-Forward 训练/验证")
    parser.add_argument("--train", type=int, default=60, help="训练窗交易日（默认 60）")
    parser.add_argument("--validate", type=int, default=20, help="验证窗（默认 20）")
    parser.add_argument("--scope", choices=["narrow", "full"], default="narrow",
                        help="narrow≈24组; full≈135组")
    parser.add_argument("--min-edge", type=float, default=3.0,
                        help="样本外领先时间基线至少 N pp 才建议切换")
    parser.add_argument("--min-train-trades", type=int, default=8)
    parser.add_argument("--min-validate-trades", type=int, default=3)
    parser.add_argument("--min-positive-segments", type=int, default=2)
    parser.add_argument("--no-stability", action="store_true")
    parser.add_argument("--stop-loss", type=float, default=-3.0,
                        help="次日止损 %%（默认 -3；0 关闭）")
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    args = parser.parse_args()

    stop_loss = None if args.stop_loss == 0 else args.stop_loss
    total_days = args.train + args.validate

    print("=== T+0 量能 Walk-Forward（5 分 K）===")
    print(f"训练 {args.train} 日 + 验证 {args.validate} 日 | 搜索: {args.scope}")
    print(f"换参门槛: 样本外领先 ≥{args.min_edge}pp | 止损: {stop_loss or '关'}")
    print()

    etf_list = get_all_t0_etfs()
    etf_daily, etf_5min, all_dates, proxy_klines = load_market_data(
        etf_list, lookback=total_days + 40,
    )
    if len(etf_5min) < 5:
        print("ERROR: 5 分 K 数据不足")
        sys.exit(1)

    eval_all = resolve_eval_dates(all_dates, total_days, "", "")
    if len(eval_all) < total_days:
        print(f"ERROR: 需要至少 {total_days} 个交易日，当前仅 {len(eval_all)}")
        sys.exit(1)
    eval_all = eval_all[-total_days:]
    train_dates = eval_all[: args.train]
    validate_dates = eval_all[args.train:]

    picks = precompute_picks(
        etf_list, etf_daily, etf_5min, eval_all,
        [BASELINE["signal"]], proxy_klines, use_filter=True, skip_choppy=True,
    )

    combos = iter_vol_combos() if args.scope == "full" else iter_narrow_vol_combos()
    enriched_by_lb: dict[int, dict] = {}
    # 训练+验证窗一并预计算，避免验证时 enriched 缓存缺验证日
    for lb in sorted({c[0] for c in combos}):
        enriched_by_lb[lb] = precompute_enriched(
            etf_list, etf_5min, etf_daily, eval_all, all_dates, lb,
        )

    print(f">>> 训练窗量能网格 ({len(combos)} 组)...")
    top_train = search_vol_on_window(
        combos, train_dates, all_dates, etf_list, etf_daily, etf_5min,
        proxy_klines, args.fee, args.min_train_trades,
        require_stable=not args.no_stability,
        min_positive_segments=args.min_positive_segments,
        stop_loss_pct=stop_loss,
        enriched_by_lb=enriched_by_lb,
    )
    candidate_train = top_train[0] if top_train else None

    baseline_train = eval_time_baseline(
        train_dates, all_dates, picks, etf_5min, args.fee, args.min_train_trades,
    )
    baseline_val = eval_time_baseline(
        validate_dates, all_dates, picks, etf_5min, args.fee, args.min_validate_trades,
    )

    candidate_val = None
    if candidate_train:
        lb = candidate_train["lookback"]
        candidate_val = eval_vol_spec(
            candidate_train, validate_dates, all_dates,
            etf_list, etf_daily, etf_5min, proxy_klines, args.fee,
            args.min_validate_trades, stop_loss,
            enriched_by_lb.get(lb),
        )

    label, detail, switch = decide_recommendation(
        baseline_val, candidate_val, candidate_train,
        args.min_edge, args.min_validate_trades,
    )

    print_report(
        train_dates, validate_dates,
        baseline_train, baseline_val,
        candidate_train, candidate_val,
        top_train, label, detail, switch, len(combos), stop_loss,
    )

    payload = {
        "run_at": datetime.now().isoformat(),
        "bar_scale": "5min",
        "config": {
            "train_days": args.train,
            "validate_days": args.validate,
            "scope": args.scope,
            "min_edge_pp": args.min_edge,
            "stop_loss_pct": stop_loss,
            "combos_searched": len(combos),
        },
        "windows": {
            "train": {"start": train_dates[0], "end": train_dates[-1], "days": len(train_dates)},
            "validate": {"start": validate_dates[0], "end": validate_dates[-1], "days": len(validate_dates)},
        },
        "baseline_time": {
            "train": {k: v for k, v in (baseline_train or {}).items() if k != "trades"},
            "validate": {k: v for k, v in (baseline_val or {}).items() if k != "trades"},
        },
        "candidate_vol": {
            "train": {k: v for k, v in (candidate_train or {}).items() if k != "trades"},
            "validate": {k: v for k, v in (candidate_val or {}).items() if k != "trades"},
        },
        "top_train": [{k: v for k, v in r.items() if k != "trades"} for r in top_train[:10]],
        "recommendation": {"label": label, "detail": detail, "switch": switch},
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = STATE_DIR / f"t0_vol_walk_forward_{tag}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}")
    print(f"最新状态: {STATE_FILE}")


if __name__ == "__main__":
    main()

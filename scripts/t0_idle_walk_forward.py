#!/usr/bin/env python3
"""闲置双段 Walk-Forward — 训练窗小网格 + 样本外验证，决定是否 Shadow/升级。

固定（不在 WF 里搜）:
  池: 全市场 T+0
  选股: v6 TOP1 + 信号时刻涨幅 ≥2%

搜索（刻意缩小，~6 组/段）:
  段1: 信号 {11:15,11:25} × 13:05 买 × 13:30 定时卖
  段2: 11:05 选 × 14:05 买 × 卖 {14:15,14:30} × 卖法 {TRIX,time}

决策: 样本外「基线+闲置段」须领先「仅基线」≥ min_edge pp，且段本身 OOS 累计 > 0。

用法:
    python scripts/t0_idle_walk_forward.py --use-cache
    python scripts/t0_idle_walk_forward.py --train 60 --validate 20 --min-edge 3
    python scripts/t0_idle_walk_forward.py --no-push

建议调度: 每月首个交易日（可与 t0_walk_forward.py 同日跑）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from itertools import product
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_top1 import _calc_stats  # noqa: E402
from backtest_t0_idle_dual import run_leg  # noqa: E402
from backtest_t0_idle_grid import MIN_GAIN_V6, build_v6_picks, load_data  # noqa: E402
from backtest_t0_idle_pool_search import _pool_list  # noqa: E402
from backtest_t0_idle_window import (  # noqa: E402
    LIVE_BUY,
    LIVE_SIGNAL,
    idle_eligible_days,
    run_baseline_overnight_legs,
    valid_combo,
)
from backtest_t0_today1 import FEE_PCT, resolve_eval_dates  # noqa: E402
from rotation_monitor import send_dingtalk  # noqa: E402
from search_t0_time_combo import precompute_picks  # noqa: E402
from t0_etf_list import get_all_t0_etfs  # noqa: E402

STATE_DIR = Path.home() / ".tradingagents" / "rotation"
STATE_FILE = STATE_DIR / "t0_idle_walk_forward_state.json"

# 当前 Shadow 定版（对照用，非训练结论）
SHADOW_LEG1 = {
    "signal": "11:25", "buy": "13:05", "sell": "13:30",
    "buy_mode": "fixed", "sell_mode": "time", "id": "leg1",
}
SHADOW_LEG2 = {
    "signal": "11:05", "buy": "14:05", "sell": "14:15",
    "buy_mode": "fixed", "sell_mode": "trix", "id": "leg2",
}

LEG1_SIGNALS = ["11:15", "11:25"]
LEG1_BUYS = ["13:05"]
LEG1_SELLS = ["13:30"]
LEG1_SELL_MODES = ["time"]

LEG2_SIGNALS = ["11:05"]
LEG2_BUYS = ["14:05"]
LEG2_SELLS = ["14:15", "14:30"]
LEG2_SELL_MODES = ["trix", "time"]


def iter_leg1_combos() -> list[dict]:
    out: list[dict] = []
    for sig, buy, sell, sm in product(LEG1_SIGNALS, LEG1_BUYS, LEG1_SELLS, LEG1_SELL_MODES):
        if not valid_combo(sig, buy, sell):
            continue
        out.append({
            "signal": sig, "buy": buy, "sell": sell,
            "buy_mode": "fixed", "sell_mode": sm, "id": "leg1",
        })
    return out


def iter_leg2_combos() -> list[dict]:
    out: list[dict] = []
    for sig, buy, sell, sm in product(LEG2_SIGNALS, LEG2_BUYS, LEG2_SELLS, LEG2_SELL_MODES):
        if not valid_combo(sig, buy, sell):
            continue
        out.append({
            "signal": sig, "buy": buy, "sell": sell,
            "buy_mode": "fixed", "sell_mode": sm, "id": "leg2",
        })
    return out


def leg_label(leg: dict) -> str:
    return f"{leg['signal']}/{leg['buy']}→{leg['sell']} [{leg['sell_mode']}]"


def leg_key(leg: dict) -> str:
    return f"{leg['signal']},{leg['buy']},{leg['sell']},{leg['sell_mode']}"


def train_stability_ok(
    trades: list[dict],
    window_days: list[str],
    min_positive: int,
) -> tuple[bool, list[float]]:
    if len(window_days) < 9:
        return True, []
    seg_size = len(window_days) // 3
    segs = [
        window_days[:seg_size],
        window_days[seg_size: 2 * seg_size],
        window_days[2 * seg_size:],
    ]
    day_set = [set(s) for s in segs]
    totals: list[float] = []
    for ds in day_set:
        rets = [t["return_pct"] for t in trades if t["day"] in ds]
        eq = 1.0
        for r in rets:
            eq *= 1 + r / 100
        totals.append((eq - 1) * 100)
    positive = sum(1 for t in totals if t > 0)
    return positive >= min_positive, totals


def run_leg_window(
    idle_days: list[str],
    v6_picks: dict,
    etf_bars: dict,
    leg: dict,
    fee: float,
    min_trades: int,
) -> dict | None:
    trades: list[dict] = []
    for day in idle_days:
        t = run_leg(day, v6_picks, etf_bars, leg, fee)
        if t:
            trades.append(t)
    if len(trades) < min_trades:
        return None
    rets = [t["return_pct"] for t in trades]
    eq = 1.0
    for r in rets:
        eq *= 1 + r / 100
    return {
        "leg_key": leg_key(leg),
        "label": leg_label(leg),
        "spec": leg,
        "trade_count": len(trades),
        "coverage_pct": len(trades) / len(idle_days) * 100 if idle_days else 0,
        "final_equity_pct": (eq - 1) * 100,
        "stats": _calc_stats(rets),
        "trades": trades,
    }


def search_leg_on_window(
    combos: list[dict],
    idle_days: list[str],
    v6_picks: dict,
    etf_bars: dict,
    fee: float,
    min_trades: int,
    require_stable: bool,
    min_positive_segments: int,
) -> list[dict]:
    results: list[dict] = []
    for leg in combos:
        r = run_leg_window(idle_days, v6_picks, etf_bars, leg, fee, min_trades)
        if not r:
            continue
        stable, seg_totals = train_stability_ok(r["trades"], idle_days, min_positive_segments)
        if require_stable and not stable:
            continue
        r["train_seg_totals"] = seg_totals
        results.append(r)
    results.sort(key=lambda x: x["final_equity_pct"], reverse=True)
    return results


def combine_baseline_plus_legs(
    baseline: dict,
    *idle_results: dict | None,
) -> dict:
    maps = []
    for res in idle_results:
        if res:
            maps.append({t["day"]: t for t in res["trades"]})
        else:
            maps.append({})
    eq = 1.0
    legs = 0
    for bt in baseline["trades"]:
        eq *= 1 + bt["return_pct"] / 100
        legs += 1
        day = bt["sell_date"]
        for m in maps:
            t = m.get(day)
            if t:
                eq *= 1 + t["return_pct"] / 100
                legs += 1
    return {"legs": legs, "final_equity_pct": (eq - 1) * 100}


def decide_leg_recommendation(
    baseline_val: dict,
    leg_val: dict | None,
    leg_train: dict | None,
    combined_val_pct: float,
    min_edge_pp: float,
    min_validate_trades: int,
    leg_name: str,
) -> tuple[str, str, bool]:
    if not leg_train:
        return f"不 Shadow {leg_name}", "训练窗无满足稳定性/笔数要求的候选", False
    if not leg_val:
        return f"不 Shadow {leg_name}", f"{leg_name} 样本外无足够交易", False
    if leg_val["trade_count"] < min_validate_trades:
        return (
            f"不 Shadow {leg_name}",
            f"样本外仅 {leg_val['trade_count']} 笔 (< {min_validate_trades})",
            False,
        )
    if leg_val["final_equity_pct"] <= 0:
        return (
            f"不 Shadow {leg_name}",
            f"样本外段本身 {leg_val['final_equity_pct']:+.2f}% ≤ 0",
            False,
        )
    bl_ret = baseline_val["final_equity_pct"]
    edge = combined_val_pct - bl_ret
    if edge < min_edge_pp:
        if edge >= 0:
            reason = (
                f"基线+{leg_name} {combined_val_pct:+.2f}% vs 仅基线 {bl_ret:+.2f}%"
                f"（仅领先 {edge:+.2f}pp < {min_edge_pp}pp）"
            )
        else:
            reason = (
                f"基线+{leg_name} {combined_val_pct:+.2f}% vs 仅基线 {bl_ret:+.2f}%"
                f"（落后 {-edge:.2f}pp）"
            )
        return f"不 Shadow {leg_name}", reason, False
    return (
        f"可 Shadow {leg_name}",
        f"基线+{leg_name} {combined_val_pct:+.2f}% vs 仅基线 {bl_ret:+.2f}%（领先 {edge:+.2f}pp）",
        True,
    )


def decide_dual_recommendation(
    baseline_val: dict,
    leg1_val: dict | None,
    leg2_val: dict | None,
    combined_val_pct: float,
    min_edge_pp: float,
    leg1_ok: bool,
    leg2_ok: bool,
) -> tuple[str, str, bool]:
    if not (leg1_ok and leg2_ok):
        return "不 Shadow 双段", "段1/段2 须分别通过样本外门槛", False
    if not leg1_val or not leg2_val:
        return "不 Shadow 双段", "段1或段2 样本外无足够交易", False
    bl_ret = baseline_val["final_equity_pct"]
    edge = combined_val_pct - bl_ret
    if edge < min_edge_pp:
        return (
            "不 Shadow 双段",
            f"基线+双段 {combined_val_pct:+.2f}% vs 仅基线 {bl_ret:+.2f}%（领先 {edge:+.2f}pp < {min_edge_pp}pp）",
            False,
        )
    return (
        "可 Shadow 双段",
        f"基线+双段 {combined_val_pct:+.2f}% vs 仅基线 {bl_ret:+.2f}%（领先 {edge:+.2f}pp）",
        True,
    )


def print_report(
    train_dates: list[str],
    validate_dates: list[str],
    train_idle: list[str],
    val_idle: list[str],
    baseline_train: dict,
    baseline_val: dict,
    leg1_train: dict | None,
    leg1_val: dict | None,
    leg2_train: dict | None,
    leg2_val: dict | None,
    shadow_leg1_val: dict | None,
    shadow_leg2_val: dict | None,
    top_leg1: list[dict],
    top_leg2: list[dict],
    recs: dict,
    combos_leg1: int,
    combos_leg2: int,
) -> None:
    print()
    print("=" * 92)
    print("  闲置双段 Walk-Forward 复核")
    print("=" * 92)
    print(f"  训练窗: {train_dates[0]} ~ {train_dates[-1]} ({len(train_dates)} 日, idle {len(train_idle)})")
    print(f"  验证窗: {validate_dates[0]} ~ {validate_dates[-1]} ({len(validate_dates)} 日, idle {len(val_idle)})")
    print(f"  搜索: 段1 {combos_leg1} 组 | 段2 {combos_leg2} 组 | 池 v6≥{MIN_GAIN_V6}%")
    print(f"  基线: {LIVE_SIGNAL}/{LIVE_BUY} 隔夜")
    print()

    def row(name: str, r: dict | None, combined: float | None = None):
        if not r:
            print(f"  {name:<18} —")
            return
        st = r.get("stats") or {}
        extra = f" | +基线 {combined:+.2f}%" if combined is not None else ""
        print(
            f"  {name:<18} {r['label']:<32} {r['trade_count']:>3}笔 "
            f"{r['final_equity_pct']:+8.2f}% 胜率{st.get('win_rate', 0):>5.1f}%{extra}"
        )

    bl_tr = baseline_train["final_equity_pct"]
    bl_va = baseline_val["final_equity_pct"]
    c1 = combine_baseline_plus_legs(baseline_val, leg1_val)
    c2 = combine_baseline_plus_legs(baseline_val, leg2_val)
    cd = combine_baseline_plus_legs(baseline_val, leg1_val, leg2_val)
    cs1 = combine_baseline_plus_legs(baseline_val, shadow_leg1_val) if shadow_leg1_val else None
    cs2 = combine_baseline_plus_legs(baseline_val, shadow_leg2_val) if shadow_leg2_val else None

    print("  【训练窗】")
    print(f"  {'仅基线':<18} {'':32} {baseline_train['trade_count']:>3}笔 {bl_tr:+8.2f}%")
    row("段1 最优", leg1_train)
    row("段2 最优", leg2_train)
    print()
    print("  【样本外验证】")
    print(f"  {'仅基线':<18} {'':32} {baseline_val['trade_count']:>3}笔 {bl_va:+8.2f}%")
    row("段1 冻结", leg1_val, c1["final_equity_pct"])
    row("段2 冻结", leg2_val, c2["final_equity_pct"])
    row("双段 冻结", None)
    print(
        f"  {'基线+双段':<18} {'':32} {cd['legs']:>3}腿 "
        f"{cd['final_equity_pct']:+8.2f}%"
    )
    print()
    print("  【当前 Shadow 定版 — 样本外参考】")
    row("Shadow 段1", shadow_leg1_val, cs1["final_equity_pct"] if cs1 else None)
    row("Shadow 段2", shadow_leg2_val, cs2["final_equity_pct"] if cs2 else None)

    for title, top in (("段1", top_leg1), ("段2", top_leg2)):
        if not top:
            continue
        print()
        print(f"  训练窗 {title} TOP3:")
        print(f"  {'#':>2} {'组合':<36} {'笔':>3} {'累计':>8} {'分段(+/-/+)':>16}")
        print("  " + "-" * 72)
        for i, r in enumerate(top[:3], 1):
            segs = r.get("train_seg_totals") or []
            seg_s = "/".join(f"{s:+.1f}" for s in segs) if segs else "—"
            print(
                f"  {i:>2} {r['label']:<36} {r['trade_count']:>3} "
                f"{r['final_equity_pct']:+7.2f}% {seg_s:>16}"
            )

    print()
    for key in ("leg1", "leg2", "dual"):
        r = recs[key]
        icon = "✅" if r["shadow"] else "⛔"
        print(f"  {icon} {r['label']}: {r['detail']}")
        if r["shadow"] and r.get("frozen_spec"):
            print(f"     冻结参数: {leg_key(r['frozen_spec'])}")
    print("=" * 92)


def main() -> None:
    parser = argparse.ArgumentParser(description="闲置双段 Walk-Forward 训练/验证")
    parser.add_argument("--train", type=int, default=60, help="训练窗交易日数")
    parser.add_argument("--validate", type=int, default=20, help="样本外验证窗")
    parser.add_argument("--min-edge", type=float, default=3.0, help="样本外领先基线至少 N pp")
    parser.add_argument("--min-train-trades", type=int, default=8, help="训练窗最少成交笔数")
    parser.add_argument("--min-validate-trades", type=int, default=3, help="验证窗最少成交笔数")
    parser.add_argument("--min-positive-segments", type=int, default=2, help="训练窗 3 段中至少几段为正")
    parser.add_argument("--no-stability", action="store_true", help="不要求训练窗分段稳定")
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--save-state", action="store_true", default=True)
    parser.add_argument("--no-push", action="store_true", help="即使建议 Shadow 也不推送钉钉")
    args = parser.parse_args()

    total_days = args.train + args.validate
    leg1_combos = iter_leg1_combos()
    leg2_combos = iter_leg2_combos()

    print("=== 闲置双段 Walk-Forward ===")
    print(f"训练 {args.train} 日 + 验证 {args.validate} 日")
    print(f"段1 搜索 {len(leg1_combos)} 组 | 段2 搜索 {len(leg2_combos)} 组")
    print(f"门槛: 样本外 +基线 领先 ≥{args.min_edge}pp | 稳定性: "
          f"{'关' if args.no_stability else f'3段至少{args.min_positive_segments}段为正'}")
    print()

    etf_daily, etf_bars, all_dates, proxy_klines, src = load_data(total_days + 10, args.use_cache)
    if len(etf_bars) < 50:
        print("ERROR: K 线数据不足")
        sys.exit(1)
    print(f">>> 数据源: {src}")

    eval_all = resolve_eval_dates(all_dates, total_days, "", "")
    if len(eval_all) < total_days:
        print(f"ERROR: 需要至少 {total_days} 个交易日，当前仅 {len(eval_all)}")
        sys.exit(1)
    eval_all = eval_all[-total_days:]
    train_dates = eval_all[: args.train]
    validate_dates = eval_all[args.train:]

    baseline_picks = precompute_picks(
        get_all_t0_etfs(), etf_daily, etf_bars, eval_all, [LIVE_SIGNAL],
        proxy_klines, use_filter=True, skip_choppy=True,
    )
    idle_all = idle_eligible_days(eval_all, all_dates, baseline_picks)
    train_idle = [d for d in idle_all if d in set(train_dates)]
    val_idle = [d for d in idle_all if d in set(validate_dates)]

    all_signals = sorted({c["signal"] for c in leg1_combos + leg2_combos + [SHADOW_LEG1, SHADOW_LEG2]})
    pool, pool_label = _pool_list("all_t0", etf_bars)
    v6_picks = build_v6_picks(pool, idle_all, all_signals, etf_daily, etf_bars)

    print(f">>> 池: {pool_label} | idle eligible 训练/验证: {len(train_idle)}/{len(val_idle)}")
    print(">>> 训练窗段1搜索...")
    top_leg1 = search_leg_on_window(
        leg1_combos, train_idle, v6_picks, etf_bars, args.fee,
        args.min_train_trades, not args.no_stability, args.min_positive_segments,
    )
    print(">>> 训练窗段2搜索...")
    top_leg2 = search_leg_on_window(
        leg2_combos, train_idle, v6_picks, etf_bars, args.fee,
        args.min_train_trades, not args.no_stability, args.min_positive_segments,
    )

    leg1_train = top_leg1[0] if top_leg1 else None
    leg2_train = top_leg2[0] if top_leg2 else None

    baseline_train = run_baseline_overnight_legs(
        train_dates, all_dates, baseline_picks, etf_bars, args.fee,
    )
    baseline_val = run_baseline_overnight_legs(
        validate_dates, all_dates, baseline_picks, etf_bars, args.fee,
    )

    leg1_val = None
    leg2_val = None
    if leg1_train:
        leg1_val = run_leg_window(
            val_idle, v6_picks, etf_bars, leg1_train["spec"], args.fee, args.min_validate_trades,
        )
    if leg2_train:
        leg2_val = run_leg_window(
            val_idle, v6_picks, etf_bars, leg2_train["spec"], args.fee, args.min_validate_trades,
        )

    shadow_leg1_val = run_leg_window(
        val_idle, v6_picks, etf_bars, SHADOW_LEG1, args.fee, 1,
    )
    shadow_leg2_val = run_leg_window(
        val_idle, v6_picks, etf_bars, SHADOW_LEG2, args.fee, 1,
    )

    c1 = combine_baseline_plus_legs(baseline_val, leg1_val)
    c2 = combine_baseline_plus_legs(baseline_val, leg2_val)
    cd = combine_baseline_plus_legs(baseline_val, leg1_val, leg2_val)

    l1_label, l1_detail, l1_ok = decide_leg_recommendation(
        baseline_val, leg1_val, leg1_train, c1["final_equity_pct"],
        args.min_edge, args.min_validate_trades, "段1",
    )
    l2_label, l2_detail, l2_ok = decide_leg_recommendation(
        baseline_val, leg2_val, leg2_train, c2["final_equity_pct"],
        args.min_edge, args.min_validate_trades, "段2",
    )
    d_label, d_detail, d_ok = decide_dual_recommendation(
        baseline_val, leg1_val, leg2_val, cd["final_equity_pct"],
        args.min_edge, l1_ok, l2_ok,
    )

    recs = {
        "leg1": {
            "label": l1_label, "detail": l1_detail, "shadow": l1_ok,
            "frozen_spec": leg1_train["spec"] if leg1_train else None,
        },
        "leg2": {
            "label": l2_label, "detail": l2_detail, "shadow": l2_ok,
            "frozen_spec": leg2_train["spec"] if leg2_train else None,
        },
        "dual": {
            "label": d_label, "detail": d_detail, "shadow": d_ok,
            "frozen_spec": None,
        },
    }

    print_report(
        train_dates, validate_dates, train_idle, val_idle,
        baseline_train, baseline_val,
        leg1_train, leg1_val, leg2_train, leg2_val,
        shadow_leg1_val, shadow_leg2_val,
        top_leg1, top_leg2, recs, len(leg1_combos), len(leg2_combos),
    )

    def slim(r: dict | None) -> dict | None:
        if not r:
            return None
        return {k: v for k, v in r.items() if k != "trades"}

    payload = {
        "run_at": datetime.now().isoformat(),
        "config": {
            "train_days": args.train,
            "validate_days": args.validate,
            "min_edge_pp": args.min_edge,
            "combos_leg1": len(leg1_combos),
            "combos_leg2": len(leg2_combos),
            "pool": pool_label,
            "min_gain_v6": MIN_GAIN_V6,
        },
        "windows": {
            "train": {"start": train_dates[0], "end": train_dates[-1], "idle_days": len(train_idle)},
            "validate": {"start": validate_dates[0], "end": validate_dates[-1], "idle_days": len(val_idle)},
        },
        "baseline": {
            "train": slim(baseline_train),
            "validate": slim(baseline_val),
        },
        "leg1": {
            "train_best": slim(leg1_train),
            "validate_frozen": slim(leg1_val),
            "validate_combined_pct": c1["final_equity_pct"],
            "shadow_validate": slim(shadow_leg1_val),
        },
        "leg2": {
            "train_best": slim(leg2_train),
            "validate_frozen": slim(leg2_val),
            "validate_combined_pct": c2["final_equity_pct"],
            "shadow_validate": slim(shadow_leg2_val),
        },
        "dual": {
            "validate_combined_pct": cd["final_equity_pct"],
        },
        "top_train": {
            "leg1": [slim(r) for r in top_leg1[:5]],
            "leg2": [slim(r) for r in top_leg2[:5]],
        },
        "shadow_defaults": {
            "leg1": SHADOW_LEG1,
            "leg2": SHADOW_LEG2,
        },
        "recommendations": recs,
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    out_tag = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = STATE_DIR / f"t0_idle_walk_forward_{out_tag}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.save_state:
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}")
    if args.save_state:
        print(f"最新状态: {STATE_FILE}")

    any_shadow = l1_ok or l2_ok or d_ok
    if any_shadow and not args.no_push:
        webhook = (os.getenv("DINGTALK_ROTATION_WEBHOOK") or os.getenv("DINGTALK_WEBHOOK") or "").strip()
        if webhook:
            lines = [
                f"### 闲置双段 Walk-Forward | {datetime.now():%Y-%m-%d %H:%M}",
                "",
                f"验证窗: {validate_dates[0]} ~ {validate_dates[-1]}",
                f"仅基线 OOS: {baseline_val['final_equity_pct']:+.2f}%",
                "",
            ]
            for key, name in (("leg1", "段1"), ("leg2", "段2"), ("dual", "双段")):
                r = recs[key]
                icon = "✅" if r["shadow"] else "⛔"
                lines.append(f"- {icon} **{name}**: {r['label']} — {r['detail']}")
            lines.append("")
            lines.append("> Shadow 仍非实盘；须再跑 20+ 交易日 jsonl 复核。")
            ok = send_dingtalk("闲置双段 Walk-Forward", "\n".join(lines))
            print("✅ 钉钉已推送" if ok else "❌ 钉钉推送失败")


if __name__ == "__main__":
    main()

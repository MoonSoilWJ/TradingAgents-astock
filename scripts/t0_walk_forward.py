#!/usr/bin/env python3
"""T+0 策略 Walk-Forward 复核 — 训练窗网格搜索 + 样本外验证，输出是否建议换参。

避免「每日全网格」过拟合：默认 60 日训练 / 20 日验证，仅在样本外明显优于
当前实盘基线时才建议切换参数。

用法:
    python scripts/t0_walk_forward.py
    python scripts/t0_walk_forward.py --train 60 --validate 20
    python scripts/t0_walk_forward.py --scope narrow --min-edge 5
    python scripts/t0_walk_forward.py --scope full --top 5

    python scripts/t0_walk_forward.py --test-push   # 测试钉钉
    python scripts/t0_walk_forward.py --no-push     # 不推送（即使建议切换）

建议调度: 每月第一个交易日 9:00（install_crontab.sh --install-walk-forward）。
「可考虑切换」时自动钉钉推送（需 DINGTALK_ROTATION_WEBHOOK）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from backtest_t0_today1 import (  # noqa: E402
    FEE_PCT,
    load_market_data,
    resolve_eval_dates,
)
from rotation_monitor import send_dingtalk  # noqa: E402
from search_t0_time_combo import (  # noqa: E402
    BASELINE,
    DEFAULT_BUY_TIMES,
    DEFAULT_SELL_CUTOFFS,
    DEFAULT_SIGNAL_TIMES,
    iter_combos,
    precompute_picks,
    run_combo,
    segment_stats,
)
from t0_etf_list import get_all_t0_etfs  # noqa: E402

STATE_DIR = Path.home() / ".tradingagents" / "rotation"
STATE_FILE = STATE_DIR / "t0_walk_forward_state.json"

# 窄搜索：固定下午尾盘 + 仅调 TRIX/定时卖点（约 40 组，降低过拟合）
NARROW_SIGNALS = ["14:30", "14:45", "14:50"]
NARROW_BUY = ["14:35", "14:50", "14:55"]
NARROW_CUTOFFS = ["10:05", "10:35", "11:05", "11:20"]
NARROW_MODES = ("trix0940_cut", "time")


def iter_narrow_combos() -> list[tuple[str, str, str, str | None]]:
    combos: list[tuple[str, str, str, str | None]] = []
    for sig in NARROW_SIGNALS:
        for buy in NARROW_BUY:
            from search_t0_time_combo import same_session, time_to_min

            if time_to_min(buy) <= time_to_min(sig):
                continue
            if not same_session(sig, buy):
                continue
            for cutoff in NARROW_CUTOFFS:
                if time_to_min(cutoff) >= time_to_min(buy):
                    continue
                for mode in NARROW_MODES:
                    combos.append((sig, buy, mode, cutoff))
    return combos


def combo_key(r: dict) -> str:
    c = r.get("sell_cutoff") or ""
    return f"{r['signal']},{r['buy']},{r['sell_mode']},{c}"


def is_baseline(r: dict) -> bool:
    return (
        r["signal"] == BASELINE["signal"]
        and r["buy"] == BASELINE["buy"]
        and r["sell_mode"] == BASELINE["sell_mode"]
        and r.get("sell_cutoff") == BASELINE["sell_cutoff"]
    )


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


def search_on_window(
    combos: list[tuple[str, str, str, str | None]],
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
    min_trades: int,
    require_stable: bool,
    min_positive_segments: int,
) -> list[dict]:
    import search_t0_time_combo as stc

    old_min = stc.MIN_TRADES
    stc.MIN_TRADES = min_trades
    results: list[dict] = []
    try:
        for sig, buy, mode, cutoff in combos:
            r = run_combo(sig, buy, mode, cutoff, eval_dates, all_dates, picks, etf_5min, fee_pct)
            if not r:
                continue
            stable, seg_totals = train_stability_ok(r["trades"], eval_dates, min_positive_segments)
            if require_stable and not stable:
                continue
            r["train_seg_totals"] = seg_totals
            r["combo_key"] = combo_key(r)
            results.append(r)
    finally:
        stc.MIN_TRADES = old_min
    results.sort(key=lambda x: x["final_equity_pct"], reverse=True)
    return results


def eval_combo_on_dates(
    spec: dict,
    eval_dates: list[str],
    all_dates: list[str],
    picks: dict,
    etf_5min: dict,
    fee_pct: float,
    min_trades: int,
) -> dict | None:
    import search_t0_time_combo as stc

    old_min = stc.MIN_TRADES
    stc.MIN_TRADES = min_trades
    try:
        return run_combo(
            spec["signal"], spec["buy"], spec["sell_mode"], spec.get("sell_cutoff"),
            eval_dates, all_dates, picks, etf_5min, fee_pct,
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
    """返回 (结论标签, 说明, 是否建议切换)。"""
    if not candidate_train:
        return "保持基线", "训练窗无满足稳定性/笔数要求的候选组合", False
    if not candidate_val:
        return "保持基线", "候选在样本外无足够交易", False
    if candidate_val["trade_count"] < min_validate_trades:
        return (
            "保持基线",
            f"样本外仅 {candidate_val['trade_count']} 笔 (< {min_validate_trades})",
            False,
        )
    bl_ret = baseline_val["final_equity_pct"] if baseline_val else 0.0
    cand_ret = candidate_val["final_equity_pct"]
    edge = cand_ret - bl_ret
    if is_baseline(candidate_train):
        return "保持基线", "训练窗最优即为当前实盘参数", False
    if edge < min_edge_pp:
        if edge >= 0:
            reason = f"样本外候选 {cand_ret:+.2f}% vs 基线 {bl_ret:+.2f}%（仅领先 {edge:+.2f}pp < {min_edge_pp}pp）"
        else:
            reason = f"样本外候选 {cand_ret:+.2f}% vs 基线 {bl_ret:+.2f}%（落后 {-edge:.2f}pp）"
        return "保持基线", reason, False
    return (
        "可考虑切换",
        f"样本外候选 {cand_ret:+.2f}% vs 基线 {bl_ret:+.2f}%（领先 {edge:+.2f}pp）",
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
):
    print()
    print("=" * 90)
    print("  T+0 Walk-Forward 参数复核")
    print("=" * 90)
    print(f"  训练窗: {train_dates[0]} ~ {train_dates[-1]} ({len(train_dates)} 日)")
    print(f"  验证窗: {validate_dates[0]} ~ {validate_dates[-1]} ({len(validate_dates)} 日)")
    print(f"  搜索组合: {combos_searched} | 训练有效: {len(top_train)}")
    print(f"  当前基线: {BASELINE['label']}")
    print()

    def row(name: str, r: dict | None):
        if not r:
            print(f"  {name:<12} —")
            return
        st = r.get("stats") or {}
        print(
            f"  {name:<12} {r['signal']}/{r['buy']} {r['label']:<22} "
            f"{r['trade_count']:>3}笔 {r['final_equity_pct']:+8.2f}% "
            f"胜率{st.get('win_rate', 0):>5.1f}% 回撤{st.get('max_drawdown', 0):+7.2f}%"
        )

    print("  【训练窗】")
    row("基线", baseline_train)
    row("网格最优", candidate_train)
    print()
    print("  【样本外验证】")
    row("基线", baseline_val)
    row("网格最优", candidate_val)
    print()

    if top_train[:5]:
        print("  训练窗 TOP5:")
        print(f"  {'#':>2} {'信号':>6} {'买入':>6} {'卖出':<22} {'笔':>3} {'累计':>8} {'分段(+/-/+)':>16}")
        print("  " + "-" * 78)
        for i, r in enumerate(top_train[:5], 1):
            segs = r.get("train_seg_totals") or []
            seg_s = "/".join(f"{s:+.1f}" for s in segs) if segs else "—"
            mark = " ◀基线" if is_baseline(r) else ""
            print(
                f"  {i:>2} {r['signal']:>6} {r['buy']:>6} {r['label']:<22} "
                f"{r['trade_count']:>3} {r['final_equity_pct']:+7.2f}% {seg_s:>16}{mark}"
            )
        print()

    icon = "✅" if switch else "⛔"
    print(f"  {icon} 结论: {label}")
    print(f"     {detail}")
    if switch and candidate_train:
        print()
        print(f"  若切换，建议参数: {candidate_train['combo_key']}")
        print(f"  （请先小仓位试跑 1～2 周，勿立即全仓）")
    print("=" * 90)


def format_dingtalk_switch_message(
    train_dates: list[str],
    validate_dates: list[str],
    baseline_val: dict | None,
    candidate_train: dict,
    candidate_val: dict | None,
    detail: str,
    scope: str,
) -> tuple[str, str]:
    """构建钉钉标题与 Markdown 正文。"""
    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = "T0轮动 Walk-Forward 可考虑换参"
    bl_ret = (baseline_val or {}).get("final_equity_pct", 0.0)
    cand_ret = (candidate_val or {}).get("final_equity_pct", 0.0)
    edge = cand_ret - bl_ret
    ct = candidate_train
    st_tr = ct.get("stats") or {}
    st_val = (candidate_val or {}).get("stats") or {}

    lines = [
        f"### T+0 Walk-Forward | {run_ts}",
        "",
        "**结论**: ✅ **可考虑切换参数**",
        f"- {detail}",
        "",
        "**建议新参数**",
        f"- 组合: `{ct['combo_key']}`",
        f"- 信号 {ct['signal']} → 买入 {ct['buy']} → {ct['label']}",
        "",
        "**样本外验证**（决策依据）",
        f"- 候选: {cand_ret:+.2f}% ({(candidate_val or {}).get('trade_count', 0)} 笔)",
        f"- 基线: {bl_ret:+.2f}% ({(baseline_val or {}).get('trade_count', 0)} 笔)",
        f"- 领先: **{edge:+.2f} pp**",
        "",
        "**训练窗表现**（参考）",
        f"- {train_dates[0]} ~ {train_dates[-1]} | 累计 {ct['final_equity_pct']:+.2f}%",
        f"- {ct['trade_count']} 笔 | 胜率 {st_tr.get('win_rate', 0):.1f}% | 回撤 {st_tr.get('max_drawdown', 0):+.2f}%",
        "",
        f"**当前基线**: {BASELINE['label']}",
        f"**搜索范围**: {scope}",
        "",
        "> ⚠️ 请先小仓位试跑 1～2 周，勿立即全仓；脚本不自动改实盘。",
    ]
    if candidate_val:
        lines.extend([
            "",
            "**验证窗明细**",
            f"- {validate_dates[0]} ~ {validate_dates[-1]}",
            f"- 胜率 {st_val.get('win_rate', 0):.1f}% | 回撤 {st_val.get('max_drawdown', 0):+.2f}%",
        ])
    return title, "\n".join(lines)


def push_switch_alert(
    train_dates: list[str],
    validate_dates: list[str],
    baseline_val: dict | None,
    candidate_train: dict | None,
    candidate_val: dict | None,
    detail: str,
    scope: str,
) -> bool:
    webhook = (os.getenv("DINGTALK_ROTATION_WEBHOOK") or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    if not webhook:
        print("⚠️  未配置 DINGTALK_ROTATION_WEBHOOK / DINGTALK_WEBHOOK，跳过钉钉推送")
        return False
    if not candidate_train:
        return False
    title, text = format_dingtalk_switch_message(
        train_dates, validate_dates, baseline_val,
        candidate_train, candidate_val, detail, scope,
    )
    ok = send_dingtalk(title, text)
    print("✅ 钉钉已推送换参建议" if ok else "❌ 钉钉推送失败")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 Walk-Forward 训练/验证 + 换参建议")
    parser.add_argument("--train", type=int, default=60, help="训练窗交易日数（默认 60）")
    parser.add_argument("--validate", type=int, default=20, help="样本外验证窗（默认 20）")
    parser.add_argument("--scope", choices=["narrow", "full"], default="narrow",
                        help="narrow=尾盘+卖点约40组; full=全网格3700+组")
    parser.add_argument("--min-edge", type=float, default=3.0,
                        help="样本外领先基线至少 N 个百分点才建议切换（默认 3）")
    parser.add_argument("--min-train-trades", type=int, default=8, help="训练窗最少笔数")
    parser.add_argument("--min-validate-trades", type=int, default=3, help="验证窗最少笔数")
    parser.add_argument("--min-positive-segments", type=int, default=2,
                        help="训练窗 3 段中至少几段为正（默认 2）")
    parser.add_argument("--no-stability", action="store_true", help="不要求训练窗分段稳定")
    parser.add_argument("--fee", type=float, default=FEE_PCT)
    parser.add_argument("--save-state", action="store_true", default=True,
                        help="写入 ~/.tradingagents/rotation/t0_walk_forward_state.json")
    parser.add_argument("--no-push", action="store_true", help="即使建议切换也不推送钉钉")
    parser.add_argument("--test-push", action="store_true", help="发送测试推送并退出")
    args = parser.parse_args()

    if args.test_push:
        webhook = (os.getenv("DINGTALK_ROTATION_WEBHOOK") or os.getenv("DINGTALK_WEBHOOK") or "").strip()
        if not webhook:
            print("ERROR: 未配置 DINGTALK_ROTATION_WEBHOOK")
            sys.exit(1)
        ok = send_dingtalk(
            "T0轮动 Walk-Forward 测试",
            "### T+0 Walk-Forward 测试\n\n钉钉推送配置正常。",
        )
        print("✅ 测试推送成功" if ok else "❌ 测试推送失败")
        sys.exit(0 if ok else 1)

    total_days = args.train + args.validate
    print("=== T+0 Walk-Forward 参数复核 ===")
    print(f"训练 {args.train} 日 + 验证 {args.validate} 日 | 搜索范围: {args.scope}")
    print(f"换参门槛: 样本外领先 ≥{args.min_edge}pp | 稳定性: "
          f"{'关' if args.no_stability else f'3段至少{args.min_positive_segments}段为正'}")
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

    signal_times = DEFAULT_SIGNAL_TIMES if args.scope == "full" else sorted(
        set(NARROW_SIGNALS + [BASELINE["signal"]])
    )
    picks = precompute_picks(
        etf_list, etf_daily, etf_5min, eval_all, signal_times,
        proxy_klines, use_filter=True, skip_choppy=True,
    )

    if args.scope == "full":
        combos = iter_combos(DEFAULT_SIGNAL_TIMES, DEFAULT_BUY_TIMES, DEFAULT_SELL_CUTOFFS)
    else:
        combos = iter_narrow_combos()

    print(f">>> 训练窗网格搜索 ({len(combos)} 组)...")
    top_train = search_on_window(
        combos, train_dates, all_dates, picks, etf_5min, args.fee,
        min_trades=args.min_train_trades,
        require_stable=not args.no_stability,
        min_positive_segments=args.min_positive_segments,
    )
    candidate_train = top_train[0] if top_train else None

    baseline_train = eval_combo_on_dates(
        BASELINE, train_dates, all_dates, picks, etf_5min, args.fee, args.min_train_trades,
    )
    baseline_val = eval_combo_on_dates(
        BASELINE, validate_dates, all_dates, picks, etf_5min, args.fee, args.min_validate_trades,
    )
    candidate_val = None
    if candidate_train:
        candidate_val = eval_combo_on_dates(
            candidate_train, validate_dates, all_dates, picks, etf_5min, args.fee,
            args.min_validate_trades,
        )

    label, detail, switch = decide_recommendation(
        baseline_val, candidate_val, candidate_train,
        args.min_edge, args.min_validate_trades,
    )

    print_report(
        train_dates, validate_dates,
        baseline_train, baseline_val,
        candidate_train, candidate_val,
        top_train, label, detail, switch, len(combos),
    )

    payload = {
        "run_at": datetime.now().isoformat(),
        "config": {
            "train_days": args.train,
            "validate_days": args.validate,
            "scope": args.scope,
            "min_edge_pp": args.min_edge,
            "combos_searched": len(combos),
        },
        "windows": {
            "train": {"start": train_dates[0], "end": train_dates[-1], "days": len(train_dates)},
            "validate": {"start": validate_dates[0], "end": validate_dates[-1], "days": len(validate_dates)},
        },
        "baseline": {
            "train": {k: v for k, v in (baseline_train or {}).items() if k != "trades"},
            "validate": {k: v for k, v in (baseline_val or {}).items() if k != "trades"},
        },
        "candidate": {
            "train": {k: v for k, v in (candidate_train or {}).items() if k != "trades"},
            "validate": {k: v for k, v in (candidate_val or {}).items() if k != "trades"},
        },
        "top_train": [{k: v for k, v in r.items() if k != "trades"} for r in top_train[:10]],
        "recommendation": {"label": label, "detail": detail, "switch": switch},
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    out_tag = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = STATE_DIR / f"t0_walk_forward_{out_tag}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.save_state:
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_path}")
    if args.save_state:
        print(f"最新状态: {STATE_FILE}")

    if switch and not args.no_push:
        pushed = push_switch_alert(
            train_dates, validate_dates,
            baseline_val, candidate_train, candidate_val,
            detail, args.scope,
        )
        payload["recommendation"]["dingtalk_pushed"] = pushed
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.save_state:
            STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""每日量比 vs 时间基线对比 — cache_min_data 之后运行。

结果写入 strategies/data/（git 可迁移）：
- artifacts/t0_vol_daily/daily_vol_compare_YYYYMMDD.json
- logs/daily_compare.log
- runs.jsonl

用法:
    python scripts/daily_vol_compare.py
    python scripts/daily_vol_compare.py --ndays 9 --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPT_DIR))

from strategies.runtime.paths import ARTIFACTS_DIR, LOGS_DIR, ensure_data_dirs  # noqa: E402
from strategies.runtime.run_emit import emit_run  # noqa: E402

# Vol combos to track daily (not full grid)
VOL_COMBOS = [
    {"lookback": 5, "buy_vol": 2.0, "sell_mode": "below", "sell_vol": 0.8, "label": "LB5"},
    {"lookback": 15, "buy_vol": 2.0, "sell_mode": "below", "sell_vol": 0.8, "label": "LB15"},
]


def _log(msg: str) -> None:
    ensure_data_dirs()
    log_path = LOGS_DIR / "daily_compare.log"
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n"
    print(msg)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line)


def run_compare(ndays: int = 9, dry_run: bool = False) -> dict:
    import search_t0_time_combo as stc  # noqa: E402
    from search_t0_time_combo import BASELINE, precompute_picks  # noqa: E402
    from search_t0_vol_combo import (  # noqa: E402
        load_1min_data,
        precompute_enriched,
        run_vol_combo,
    )
    from backtest_t0_1min import run_combo_1min  # noqa: E402
    from backtest_t0_today1 import FEE_PCT, resolve_eval_dates  # noqa: E402
    from t0_etf_list import get_all_t0_etfs  # noqa: E402

    etf_list = get_all_t0_etfs()
    etf_daily, etf_1min, all_dates, proxy_klines, data_source = load_1min_data(
        etf_list, ndays=ndays, source="auto",
    )
    eval_dates = resolve_eval_dates(all_dates, ndays, "", "")
    m1_dates = sorted({d for bars in etf_1min.values() for d in bars})
    eval_dates = [d for d in eval_dates if d in m1_dates]
    if len(eval_dates) < 2:
        raise RuntimeError(f"1分K有效交易日不足 ({len(eval_dates)} 天)")

    use_filter = True
    skip_choppy = False
    old_min = stc.MIN_TRADES
    stc.MIN_TRADES = 2
    picks = precompute_picks(
        etf_list, etf_daily, etf_1min, eval_dates,
        [BASELINE["signal"]], proxy_klines, use_filter, skip_choppy,
    )
    baseline = run_combo_1min(
        BASELINE["signal"], BASELINE["buy"],
        BASELINE["sell_mode"], BASELINE["sell_cutoff"],
        eval_dates, all_dates, picks, etf_1min, FEE_PCT,
    )
    stc.MIN_TRADES = old_min

    vol_results = []
    caches = {}
    for cfg in VOL_COMBOS:
        lb = cfg["lookback"]
        if lb not in caches:
            caches[lb] = precompute_enriched(
                etf_list, etf_1min, etf_daily, eval_dates, all_dates, lb,
            )
        r = run_vol_combo(
            lb, cfg["buy_vol"], cfg["sell_mode"], cfg["sell_vol"], None,
            etf_list, etf_daily, etf_1min, eval_dates, all_dates, caches[lb],
            FEE_PCT, use_filter, skip_choppy, proxy_klines,
            min_trades=2,
        )
        if r:
            vol_results.append({
                "label": cfg["label"],
                "final_equity_pct": r.get("final_equity_pct"),
                "trade_count": r.get("trade_count"),
                "combo": cfg,
            })

    bl_ret = baseline.get("final_equity_pct") if baseline else None
    payload = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "ndays": ndays,
        "data_source": data_source,
        "eval_dates": eval_dates,
        "baseline": {
            "label": BASELINE.get("label"),
            "final_equity_pct": bl_ret,
            "trade_count": baseline.get("trade_count") if baseline else 0,
        },
        "vol_combos": vol_results,
        "vs_baseline_pp": {
            v["label"]: round(v["final_equity_pct"] - bl_ret, 2)
            for v in vol_results
            if v.get("final_equity_pct") is not None and bl_ret is not None
        },
    }

    _log(
        f"基线 {bl_ret:+.2f}% | "
        + " | ".join(f"{v['label']} {v['final_equity_pct']:+.2f}%" for v in vol_results)
        if vol_results and bl_ret is not None
        else "对比完成（数据不足）"
    )

    if not dry_run:
        out_dir = ARTIFACTS_DIR / "t0_vol_daily"
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d")
        out_path = out_dir / f"daily_vol_compare_{tag}.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        emit_run(
            "t0_vol_daily",
            "cron",
            "ok",
            metrics={
                "baseline_ret": bl_ret,
                "eval_days": len(eval_dates),
                "vs_baseline_pp": payload["vs_baseline_pp"],
            },
            artifacts=[str(out_path.relative_to(PROJECT_ROOT))],
            message=_summary(payload),
        )
        _log(f"已保存: {out_path}")

    return payload


def _summary(payload: dict) -> str:
    bl = payload.get("baseline", {}).get("final_equity_pct")
    parts = []
    for v in payload.get("vol_combos", []):
        diff = payload.get("vs_baseline_pp", {}).get(v["label"])
        parts.append(f"{v['label']} {v.get('final_equity_pct'):+.2f}% ({diff:+.2f}pp)")
    return f"基线 {bl:+.2f}% vs " + ", ".join(parts) if parts else "无有效对比"


def main() -> None:
    parser = argparse.ArgumentParser(description="每日量比 vs 基线对比")
    parser.add_argument("--ndays", type=int, default=9)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        run_compare(ndays=args.ndays, dry_run=args.dry_run)
    except Exception as e:
        _log(f"ERROR: {e}")
        if not args.dry_run:
            emit_run("t0_vol_daily", "cron", "error", message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()

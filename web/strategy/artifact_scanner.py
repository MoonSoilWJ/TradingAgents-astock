"""Scan ~/.tradingagents/rotation for backtest artifacts (excludes min_cache bulk)."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from web.strategy.paths import MIN_CACHE_DIR, ROTATION_DIR
from web.strategy.registry_loader import load_registry


@dataclass
class Artifact:
    path: Path
    name: str
    mtime: datetime
    kind: str
    strategy_id: str | None = None
    summary: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


def _parse_json_summary(path: Path, data: dict) -> tuple[str, dict[str, Any]]:
    name = path.name
    metrics: dict[str, Any] = {}

    if name.startswith("t0_walk_forward_"):
        rec = data.get("recommendation", {})
        bl = data.get("baseline", {}).get("validate", {})
        cand = data.get("candidate", {}).get("validate", {})
        bl_ret = bl.get("final_equity_pct")
        cand_ret = cand.get("final_equity_pct")
        metrics = {
            "baseline_val_pct": bl_ret,
            "candidate_val_pct": cand_ret,
            "decision": rec.get("label"),
        }
        summary = rec.get("label") or rec.get("detail", "")[:80]
        return summary, metrics

    if name.startswith("t0_vol_search_") or name.startswith("t0_vol_analyze_"):
        top = (data.get("top_results") or [{}])[0]
        baseline = data.get("baseline") or {}
        metrics = {
            "top_ret_pct": top.get("final_equity_pct"),
            "baseline_ret_pct": baseline.get("final_equity_pct"),
            "top_label": top.get("label"),
        }
        top_ret = top.get("final_equity_pct")
        bl_ret = baseline.get("final_equity_pct")
        if top_ret is not None and bl_ret is not None:
            summary = f"最优 {top_ret:+.2f}% vs 基线 {bl_ret:+.2f}%"
        else:
            summary = top.get("label", name)
        return summary, metrics

    if name.startswith("t0_vol_walk_forward_"):
        rec = data.get("recommendation", {})
        metrics = {"decision": rec.get("label")}
        return rec.get("label", name), metrics

    for key in ("final_equity_pct", "total_return_pct", "cumulative_return"):
        if key in data:
            val = data[key]
            metrics["return_pct"] = val
            return f"累计 {val:+.2f}%", metrics

    if "results" in data and isinstance(data["results"], list) and data["results"]:
        first = data["results"][0]
        if isinstance(first, dict) and "final_equity_pct" in first:
            val = first["final_equity_pct"]
            metrics["return_pct"] = val
            return f"TOP {val:+.2f}%", metrics

    return name, metrics


def _match_strategy(name: str) -> str | None:
    for strat in load_registry().get("strategies", []):
        for pattern in strat.get("artifact_patterns") or []:
            if fnmatch.fnmatch(name, pattern):
                return strat["id"]
    if name.startswith("backtest_"):
        if "t0" in name:
            return "t0_baseline_trix"
        if "6sig" in name or "rotation" in name:
            return "rotation_8way"
    if name.startswith("t0_walk_forward_"):
        return "t0_wf_time"
    if name.startswith("t0_vol_"):
        return "t0_vol_ratio"
    return None


def _kind_from_name(name: str) -> str:
    if "walk_forward" in name:
        return "walk_forward"
    if name.startswith("t0_vol_search") or "search" in name:
        return "grid_search"
    if name.startswith("validation_"):
        return "validation"
    if name.endswith("_state.json"):
        return "state"
    return "backtest"


def scan_artifacts(
    *,
    limit: int = 100,
    strategy_id: str | None = None,
    kind: str | None = None,
) -> list[Artifact]:
    if not ROTATION_DIR.exists():
        return []

    artifacts: list[Artifact] = []
    for path in ROTATION_DIR.iterdir():
        if not path.is_file() or path.suffix != ".json":
            continue
        if path.name.endswith("_state.json"):
            continue

        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        name = path.name
        sid = _match_strategy(name)
        k = _kind_from_name(name)

        summary = name
        metrics: dict[str, Any] = {}
        try:
            raw = path.read_text(encoding="utf-8")
            if len(raw) < 5_000_000:
                data = json.loads(raw)
                if isinstance(data, dict):
                    summary, metrics = _parse_json_summary(path, data)
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass

        art = Artifact(
            path=path,
            name=name,
            mtime=mtime,
            kind=k,
            strategy_id=sid,
            summary=summary,
            metrics=metrics,
        )
        if strategy_id and art.strategy_id != strategy_id:
            continue
        if kind and art.kind != kind:
            continue
        artifacts.append(art)

    artifacts.sort(key=lambda a: a.mtime, reverse=True)
    return artifacts[:limit]


def min_cache_stats() -> dict[str, Any]:
    if not MIN_CACHE_DIR.exists():
        return {"file_count": 0, "latest_date": None, "latest_mtime": None}

    latest_mtime: float | None = None
    latest_date: str | None = None
    count = 0
    for path in MIN_CACHE_DIR.glob("*.json"):
        count += 1
        mtime = path.stat().st_mtime
        if latest_mtime is None or mtime > latest_mtime:
            latest_mtime = mtime
            parts = path.stem.split("_")
            if len(parts) >= 3:
                latest_date = parts[-1]

    return {
        "file_count": count,
        "latest_date": latest_date,
        "latest_mtime": datetime.fromtimestamp(latest_mtime) if latest_mtime else None,
    }


def log_files() -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    if not ROTATION_DIR.exists():
        return logs
    for path in sorted(ROTATION_DIR.glob("*.log")):
        st = path.stat()
        logs.append({
            "name": path.name,
            "path": path,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime),
        })
    return logs

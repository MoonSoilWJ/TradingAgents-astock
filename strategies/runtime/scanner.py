"""Read runs.jsonl and build artifact index from legacy + repo dirs."""

from __future__ import annotations

import fnmatch
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from strategies.runtime.paths import (
    ARTIFACTS_DIR,
    DATA_DIR,
    INDEX_FILE,
    LEGACY_ROTATION_DIR,
    LOGS_DIR,
    RUNS_FILE,
    STATE_DIR,
    ensure_data_dirs,
)
from strategies.runtime.registry_loader import load_registry


def load_runs(limit: int = 200) -> list[dict[str, Any]]:
    if not RUNS_FILE.exists():
        return []
    lines = RUNS_FILE.read_text(encoding="utf-8").strip().splitlines()
    runs = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(runs))


def _mtime_iso(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def _parse_json_metrics(path: Path) -> dict[str, Any]:
    """Best-effort extract headline metrics from backtest/WF JSON."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    out: dict[str, Any] = {"file": path.name}
    name = path.name

    if name.startswith("t0_walk_forward"):
        rec = data.get("recommendation") or {}
        bl = (data.get("baseline") or {}).get("validate") or {}
        cand = (data.get("candidate") or {}).get("validate") or {}
        out.update({
            "kind": "walk_forward",
            "decision": rec.get("label"),
            "baseline_val_ret": bl.get("final_equity_pct"),
            "candidate_val_ret": cand.get("final_equity_pct"),
        })
        return out

    if name.startswith("t0_vol_search"):
        top = (data.get("top_results") or [{}])[0]
        bl = data.get("baseline") or {}
        out.update({
            "kind": "grid_search",
            "best_label": top.get("label"),
            "best_ret": top.get("final_equity_pct"),
            "baseline_ret": bl.get("final_equity_pct"),
        })
        return out

    if name.startswith("daily_vol_compare"):
        out.update({"kind": "daily_compare", **{k: v for k, v in data.items() if k != "trades"}})
        return out

    if "final_equity_pct" in data:
        out["total_ret"] = data.get("final_equity_pct")
        out["trade_count"] = data.get("trade_count")
        return out

    if isinstance(data, dict) and "stats" in data:
        st = data["stats"]
        if isinstance(st, dict):
            out["total_ret"] = st.get("total") or st.get("cum_ret")
        return out

    return out


def _scan_json_dir(base: Path, max_files: int = 500) -> list[dict[str, Any]]:
    if not base.exists():
        return []
    files = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    artifacts = []
    for f in files[:max_files]:
        if f.parent.name == "min_cache":
            continue
        artifacts.append({
            "path": str(f),
            "name": f.name,
            "mtime": _mtime_iso(f),
            "size_kb": round(f.stat().st_size / 1024, 1),
            "metrics": _parse_json_metrics(f),
        })
    return artifacts


def _match_strategy(filename: str, patterns: list[str]) -> str | None:
    for pat in patterns:
        if fnmatch.fnmatch(filename, pat):
            return pat
    return None


def _scan_logs() -> dict[str, dict[str, Any]]:
    logs: dict[str, dict[str, Any]] = {}
    for log_dir in (LOGS_DIR, LEGACY_ROTATION_DIR):
        if not log_dir.exists():
            continue
        for f in log_dir.glob("*.log"):
            key = f.name
            mtime = _mtime_iso(f)
            if key not in logs or (mtime and mtime > (logs[key].get("mtime") or "")):
                tail = ""
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    tail = "\n".join(text.splitlines()[-80:])
                except Exception:
                    pass
                logs[key] = {
                    "path": str(f),
                    "mtime": mtime,
                    "size_kb": round(f.stat().st_size / 1024, 1),
                    "tail": tail,
                }
    return logs


def _scan_state_snapshots() -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    reg = load_registry()
    state_names = set()
    for s in reg.get("strategies", []):
        for sf in s.get("state_files", []):
            state_names.add(sf)

    for name in state_names:
        for base in (LEGACY_ROTATION_DIR, STATE_DIR):
            f = base / name
            if not f.exists():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            states[name] = {
                "path": str(f),
                "mtime": _mtime_iso(f),
                "summary": _summarize_state(name, data),
            }
            break
    return states


def _summarize_state(name: str, data: dict) -> str:
    if name == "t0_monitor_state.json":
        pos = data.get("position") or data.get("holding")
        if pos:
            return f"持仓 {pos.get('etf', pos.get('code', '?'))}"
        last = data.get("last_signal") or data.get("last_run")
        return f"最近信号: {last}" if last else "无持仓"
    if name == "monitor_state.json":
        top = data.get("top_etfs") or data.get("rankings") or []
        if top:
            t0 = top[0] if isinstance(top, list) else top
            if isinstance(t0, dict):
                return f"TOP1: {t0.get('name', t0.get('code', '?'))}"
        return data.get("last_run", "—")
    if "walk_forward" in name:
        rec = data.get("recommendation") or {}
        return rec.get("label", rec.get("detail", "—"))[:80]
    return "—"


def _min_cache_stats() -> dict[str, Any]:
    cache = LEGACY_ROTATION_DIR / "min_cache"
    if not cache.exists():
        return {"file_count": 0, "dates": []}
    # Sample recent 1min files for date range (avoid full scan)
    dates: set[str] = set()
    count = 0
    for f in cache.glob("*_1min_*.json"):
        count += 1
        m = re.search(r"_1min_(\d{4}-\d{2}-\d{2})\.json$", f.name)
        if m:
            dates.add(m.group(1))
        if count > 5000:
            break
    return {
        "file_count": count,
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "path": str(cache),
    }


def build_index(*, write: bool = True) -> dict[str, Any]:
    ensure_data_dirs()
    legacy_arts = _scan_json_dir(LEGACY_ROTATION_DIR)
    repo_arts = _scan_json_dir(ARTIFACTS_DIR)
    all_arts = legacy_arts + repo_arts
    all_arts.sort(key=lambda x: x.get("mtime") or "", reverse=True)

    # Link artifacts to strategies
    reg = load_registry()
    by_strategy: dict[str, list[dict]] = {s["id"]: [] for s in reg.get("strategies", [])}
    for art in all_arts:
        for s in reg.get("strategies", []):
            patterns = s.get("artifact_patterns") or []
            if _match_strategy(art["name"], patterns):
                by_strategy[s["id"]].append(art)
                break

    index: dict[str, Any] = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "runs_count": len(load_runs(limit=9999)),
        "artifacts_recent": all_arts[:100],
        "artifacts_by_strategy": {k: v[:20] for k, v in by_strategy.items() if v},
        "logs": _scan_logs(),
        "states": _scan_state_snapshots(),
        "min_cache": _min_cache_stats(),
        "legacy_dir": str(LEGACY_ROTATION_DIR),
        "data_dir": str(DATA_DIR),
    }

    if write:
        INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index


def load_index(*, rebuild_if_missing: bool = True) -> dict[str, Any]:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    if rebuild_if_missing:
        return build_index()
    return {}

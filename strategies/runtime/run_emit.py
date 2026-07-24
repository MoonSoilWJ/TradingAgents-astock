"""Append structured run records to strategies/data/runs.jsonl (git-tracked)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from strategies.runtime.paths import RUNS_FILE, ensure_data_dirs


def emit_run(
    strategy_id: str,
    kind: str,
    status: str = "ok",
    *,
    metrics: dict[str, Any] | None = None,
    artifacts: list[str] | None = None,
    message: str = "",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Append one run line to runs.jsonl. Returns the record."""
    ensure_data_dirs()
    record: dict[str, Any] = {
        "run_id": run_id or f"{strategy_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "strategy_id": strategy_id,
        "kind": kind,
        "status": status,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": metrics or {},
        "artifacts": artifacts or [],
        "message": message,
    }
    with RUNS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def copy_artifact_to_repo(src: Path, strategy_id: str) -> Path | None:
    """Copy a small JSON artifact into strategies/data/artifacts/ for git portability."""
    from strategies.runtime.paths import ARTIFACTS_DIR, ensure_data_dirs

    if not src.exists() or src.stat().st_size > 2_000_000:
        return None
    ensure_data_dirs()
    dest = ARTIFACTS_DIR / strategy_id / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    return dest.relative_to(ARTIFACTS_DIR.parent.parent)

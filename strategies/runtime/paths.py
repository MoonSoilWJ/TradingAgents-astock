"""Paths for strategy registry, runs index, and legacy rotation artifacts."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STRATEGIES_DIR = PROJECT_ROOT / "strategies"
REGISTRY_FILE = STRATEGIES_DIR / "registry.json"
DATA_DIR = STRATEGIES_DIR / "data"
RUNS_FILE = DATA_DIR / "runs.jsonl"
INDEX_FILE = DATA_DIR / "index.json"
LOGS_DIR = DATA_DIR / "logs"
STATE_DIR = DATA_DIR / "state"
ARTIFACTS_DIR = DATA_DIR / "artifacts"

# Legacy runtime dir (large min_cache stays here)
LEGACY_ROTATION_DIR = Path(
    os.environ.get(
        "TRADINGAGENTS_ROTATION_DIR",
        str(Path.home() / ".tradingagents" / "rotation"),
    )
)


def ensure_data_dirs() -> None:
    for d in (DATA_DIR, LOGS_DIR, STATE_DIR, ARTIFACTS_DIR):
        d.mkdir(parents=True, exist_ok=True)

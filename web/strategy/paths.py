"""Strategy dashboard — paths and constants."""

from __future__ import annotations

import os
from pathlib import Path


def _find_project_root() -> Path:
    env = os.environ.get("TRADINGAGENTS_ROOT", "").strip()
    if env:
        root = Path(env).expanduser().resolve()
        if (root / "strategies" / "registry.json").exists():
            return root
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "strategies" / "registry.json").exists():
            return parent
    return here.parents[2]


PROJECT_ROOT = _find_project_root()
REGISTRY_PATH = PROJECT_ROOT / "strategies" / "registry.json"
CRON_MANIFEST_PATH = PROJECT_ROOT / "strategies" / "cron_manifest.json"
ROTATION_DIR = Path.home() / ".tradingagents" / "rotation"
MIN_CACHE_DIR = ROTATION_DIR / "min_cache"

STATUS_ORDER = ("live", "shadow", "candidate", "research", "rejected", "deprecated")
STATUS_LABELS = {
    "live": "实盘 LIVE",
    "shadow": "旁路 SHADOW",
    "candidate": "候选 CANDIDATE",
    "research": "研究 RESEARCH",
    "rejected": "已否决 REJECTED",
    "deprecated": "已废弃 DEPRECATED",
}
STATUS_COLORS = {
    "live": "#22c55e",
    "shadow": "#3b82f6",
    "candidate": "#f59e0b",
    "research": "#9ca3af",
    "rejected": "#ef4444",
    "deprecated": "#6b7280",
}
CATEGORY_LABELS = {
    "rotation": "板块轮动",
    "t0": "T+0 ETF",
    "data": "数据",
}

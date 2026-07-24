"""Load strategy registry from strategies/registry.json."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from strategies.runtime.paths import REGISTRY_FILE


@lru_cache(maxsize=1)
def load_registry() -> dict[str, Any]:
    if not REGISTRY_FILE.exists():
        return {"strategies": [], "cron_jobs": []}
    return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))


def get_strategies(*, status: str | None = None, category: str | None = None) -> list[dict]:
    items = load_registry().get("strategies", [])
    if status:
        items = [s for s in items if s.get("status") == status]
    if category:
        items = [s for s in items if s.get("category") == category]
    return items


def get_strategy(strategy_id: str) -> dict | None:
    for s in load_registry().get("strategies", []):
        if s.get("id") == strategy_id:
            return s
    return None


def get_cron_jobs() -> list[dict]:
    return load_registry().get("cron_jobs", [])


def count_by_status() -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in load_registry().get("strategies", []):
        st = s.get("status", "unknown")
        counts[st] = counts.get(st, 0) + 1
    return counts

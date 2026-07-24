"""Load strategy registry and cron manifest."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from web.strategy.paths import CRON_MANIFEST_PATH, REGISTRY_PATH


@lru_cache(maxsize=1)
def load_registry() -> dict[str, Any]:
    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in data.get("strategies", [])}
    data["_by_id"] = by_id
    return data


def get_strategies(*, status: str | None = None, category: str | None = None) -> list[dict]:
    strategies = load_registry().get("strategies", [])
    out = strategies
    if status:
        out = [s for s in out if s.get("status") == status]
    if category:
        out = [s for s in out if s.get("category") == category]
    return out


def get_strategy(strategy_id: str) -> dict | None:
    return load_registry().get("_by_id", {}).get(strategy_id)


@lru_cache(maxsize=1)
def load_cron_manifest() -> dict[str, Any]:
    if not CRON_MANIFEST_PATH.exists():
        return {"jobs": []}
    return json.loads(CRON_MANIFEST_PATH.read_text(encoding="utf-8"))


def count_by_status() -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in load_registry().get("strategies", []):
        st = s.get("status", "research")
        counts[st] = counts.get(st, 0) + 1
    return counts

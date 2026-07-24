"""Tests for strategy registry and artifact scanner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def registry_dir(tmp_path: Path, monkeypatch):
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "registry.json").write_text(
        json.dumps({
            "strategies": [{
                "id": "demo",
                "name": "Demo",
                "status": "research",
                "artifact_patterns": ["demo_*.json"],
            }],
        }),
        encoding="utf-8",
    )
    (strategies / "cron_manifest.json").write_text('{"jobs": []}', encoding="utf-8")
    monkeypatch.setenv("TRADINGAGENTS_ROOT", str(tmp_path))
    from web.strategy import paths, registry_loader

    paths.PROJECT_ROOT = tmp_path
    paths.REGISTRY_PATH = strategies / "registry.json"
    paths.CRON_MANIFEST_PATH = strategies / "cron_manifest.json"
    registry_loader.load_registry.cache_clear()
    registry_loader.load_cron_manifest.cache_clear()
    return tmp_path


def test_load_registry(registry_dir):
    from web.strategy.registry_loader import get_strategy, load_registry

    data = load_registry()
    assert len(data["strategies"]) == 1
    assert get_strategy("demo")["name"] == "Demo"


def test_scan_artifacts(tmp_path: Path, registry_dir, monkeypatch):
    from web.strategy import artifact_scanner

    rot = tmp_path / "rotation"
    rot.mkdir()
    monkeypatch.setattr(artifact_scanner, "ROTATION_DIR", rot)

    payload = {
        "recommendation": {"label": "保持基线", "detail": "ok"},
        "baseline": {"validate": {"final_equity_pct": 41.0}},
        "candidate": {"validate": {"final_equity_pct": 32.0}},
    }
    (rot / "t0_walk_forward_20260720.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )

    arts = artifact_scanner.scan_artifacts(limit=10)
    assert len(arts) == 1
    assert arts[0].strategy_id == "t0_wf_time"
    assert arts[0].metrics["baseline_val_pct"] == 41.0


def test_min_cache_stats(tmp_path: Path, monkeypatch):
    from web.strategy import artifact_scanner

    cache = tmp_path / "min_cache"
    cache.mkdir()
    (cache / "159915_1min_20260720.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(artifact_scanner, "MIN_CACHE_DIR", cache)

    stats = artifact_scanner.min_cache_stats()
    assert stats["file_count"] == 1
    assert stats["latest_date"] == "20260720"

#!/usr/bin/env python3
"""Rebuild strategies/data/index.json from legacy artifacts + registry."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from strategies.runtime.scanner import build_index  # noqa: E402


def main() -> None:
    idx = build_index()
    print(f"Index rebuilt: {idx['built_at']}")
    print(f"  Runs: {idx['runs_count']}")
    print(f"  Recent artifacts: {len(idx['artifacts_recent'])}")
    print(f"  Logs: {list(idx['logs'].keys())}")
    print(f"  States: {list(idx['states'].keys())}")


if __name__ == "__main__":
    main()

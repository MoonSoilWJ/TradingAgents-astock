"""Read live monitor state files."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from web.strategy.paths import ROTATION_DIR


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _mtime(path: Path) -> datetime | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime)


def rotation_state() -> dict[str, Any]:
    path = ROTATION_DIR / "monitor_state.json"
    data = _read_json(path)
    return {
        "path": path,
        "mtime": _mtime(path),
        "data": data,
        "ok": data is not None,
    }


def t0_state() -> dict[str, Any]:
    path = ROTATION_DIR / "t0_monitor_state.json"
    data = _read_json(path)
    return {
        "path": path,
        "mtime": _mtime(path),
        "data": data,
        "ok": data is not None,
    }


def walk_forward_state() -> dict[str, Any]:
    path = ROTATION_DIR / "t0_walk_forward_state.json"
    data = _read_json(path)
    rec = (data or {}).get("recommendation", {})
    return {
        "path": path,
        "mtime": _mtime(path),
        "data": data,
        "ok": data is not None,
        "decision": rec.get("label"),
        "run_at": (data or {}).get("run_at"),
    }


def state_file_info(relative_name: str) -> dict[str, Any]:
    path = ROTATION_DIR / relative_name
    data = _read_json(path)
    return {
        "path": path,
        "mtime": _mtime(path),
        "exists": path.exists(),
        "data": data,
    }


def tail_log(relative_name: str, *, lines: int = 80) -> str:
    path = ROTATION_DIR / relative_name
    if not path.exists():
        return f"（文件不存在: {path}）"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"读取失败: {exc}"
    rows = content.splitlines()
    if len(rows) <= lines:
        return content
    return "\n".join(rows[-lines:])

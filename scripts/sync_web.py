#!/usr/bin/env python3
"""触发策略数据同步到 Web (sync_to_web.sh).

在策略触发买卖信号、已写入 journal/state 之后调用, 把最新数据导出
(strategies.json) 并 scp 到 ECS 站点。best-effort: 失败不抛出,
尽量不干扰主策略流程。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_SYNC_SH = _SCRIPT_DIR / "sync_to_web.sh"


def sync_to_web(*, timeout: int = 180, verbose: bool = True) -> bool:
    """运行 sync_to_web.sh, 把策略数据同步到 Web。返回是否成功 (True/False)。"""
    if not _SYNC_SH.exists():
        if verbose:
            print("[sync_to_web] 未找到 sync_to_web.sh, 跳过同步")
        return False
    try:
        res = subprocess.run(
            ["/bin/bash", str(_SYNC_SH)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if verbose:
            if res.stdout:
                print(res.stdout)
            if res.returncode != 0 and res.stderr:
                print(res.stderr)
            print(f"[sync_to_web] 同步{'成功' if res.returncode == 0 else '失败'} (rc={res.returncode})")
        return res.returncode == 0
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"[sync_to_web] 同步异常: {e}")
        return False

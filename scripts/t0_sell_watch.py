#!/usr/bin/env python3
"""T+0 卖出监控循环 — 09:40~11:05 每 50 秒跑一次 --sell-check。

由 crontab 在 09:40 触发一次，本脚本在窗口内循环直至 11:05 后做一次收尾检查。

每次 --sell-check 会并行写入 1 分 K 追踪 shadow 日志（不改实盘卖点）：
  ~/.tradingagents/rotation/t0_trail_shadow.jsonl
查看: python scripts/t0_monitor.py --trail-log
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT = _SCRIPT_DIR.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from backtest_t0_today1 import time_to_min  # noqa: E402

try:
    from tradingagents.intraday.calendar import is_trading_day
except ImportError:
    from datetime import date

    def is_trading_day(day=None) -> bool:  # type: ignore[misc]
        d = day or date.today()
        return d.weekday() < 5

INTERVAL_SEC = 50
SELL_START = "09:40"
SELL_END = "11:05"


def in_sell_window(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    hm = now.hour * 60 + now.minute
    return time_to_min(SELL_START) <= hm <= time_to_min(SELL_END)


def run_sell_check() -> None:
    subprocess.run(
        [sys.executable, str(_SCRIPT_DIR / "t0_monitor.py"), "--sell-check"],
        cwd=_PROJECT,
        check=False,
    )


def main() -> None:
    if not is_trading_day():
        print("非交易日，跳过")
        return

    now = datetime.now()
    if time_to_min(now.strftime("%H:%M")) > time_to_min(SELL_END):
        print(f"已过 {SELL_END}，跳过")
        return

    print(f"=== T+0 卖出监控循环 | {now.strftime('%Y-%m-%d %H:%M:%S')} | 每 {INTERVAL_SEC}s ===")

    while in_sell_window():
        run_sell_check()
        if not in_sell_window():
            break
        time.sleep(INTERVAL_SEC)

    # 11:05 截止：再做一次检查（触发定时卖出）
    run_sell_check()
    print("=== 卖出监控循环结束 ===")


if __name__ == "__main__":
    main()

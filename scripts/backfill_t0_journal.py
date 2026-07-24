#!/usr/bin/env python3
"""把 shadow 日志里的 live_sell 补写到 t0_trade_journal.jsonl。"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR.parent))

from web.strategy.t0_journal import backfill_journal_from_shadow  # noqa: E402


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="补写 T+0 交易流水")
    parser.add_argument("--dry-run", action="store_true", help="仅统计，不写文件")
    args = parser.parse_args()
    n = backfill_journal_from_shadow(dry_run=args.dry_run)
    action = "将补写" if args.dry_run else "已补写"
    print(f"{action} {n} 条 → ~/.tradingagents/rotation/t0_trade_journal.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Console entry point for ``tradingagents-intraday``."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

from tradingagents.intraday.daemon import run_daemon


def main() -> None:
    run_daemon()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Intraday monitoring daemon — polls session and runs scheduled slots."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from tradingagents.intraday.daemon import run_daemon

if __name__ == "__main__":
    run_daemon()

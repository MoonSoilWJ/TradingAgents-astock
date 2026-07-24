"""Launch the TradingAgents web UI via `tradingagents-web` command."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    """Launch Streamlit UI (投研分析 + pages/ 策略仪表盘)."""
    app_path = Path(__file__).parent / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app_path)])


if __name__ == "__main__":
    main()

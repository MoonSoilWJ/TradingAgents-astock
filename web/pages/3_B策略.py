"""B+idle 优化策略专页: 策略说明书 + 执行步骤 + 实时 SHADOW + 验证结论。"""
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from web.strategy.theme import inject_css  # noqa: E402
from web.strategy.b_idle_manual import render_b_idle_page  # noqa: E402

st.set_page_config(page_title="B+idle 策略", page_icon="🆕", layout="wide")
inject_css()
render_b_idle_page()

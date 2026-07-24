"""运行日志 — tail rotation 目录下的 log 文件."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from web.strategy.artifact_scanner import log_files
from web.strategy.state_reader import tail_log
from web.strategy.theme import fmt_dt, inject_css

st.set_page_config(page_title="运行日志", page_icon="📜", layout="wide")
inject_css()

st.title("📜 运行日志")
st.caption("~/.tradingagents/rotation/*.log")

logs = log_files()
if not logs:
    st.info("暂无日志文件")
    st.stop()

names = [lg["name"] for lg in logs]
default = "walk_forward.log" if "walk_forward.log" in names else names[0]

col1, col2 = st.columns([2, 1])
with col1:
    picked = st.selectbox("日志文件", names, index=names.index(default))
with col2:
    line_count = st.slider("显示行数", 20, 300, 80, step=20)

meta = next(lg for lg in logs if lg["name"] == picked)
st.caption(f"{meta['path']} · {fmt_dt(meta['mtime'])} · {meta['size']:,} bytes")

if st.button("🔄 刷新"):
    st.rerun()

content = tail_log(picked, lines=line_count)
st.code(content, language="text")

if picked == "min_cache.log":
    st.info("min_cache 每日 15:10 由 cache_min_data.py 追加写入")
elif picked == "walk_forward.log":
    st.info("walk_forward 每月首个工作日 09:00 由 t0_walk_forward.py 追加写入")

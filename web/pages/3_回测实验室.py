"""回测实验室 — 扫描 JSON 结果时间线."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from web.strategy.artifact_scanner import scan_artifacts
from web.strategy.registry_loader import get_strategies, load_registry
from web.strategy.theme import fmt_dt, inject_css

st.set_page_config(page_title="回测实验室", page_icon="🧪", layout="wide")
inject_css()

st.title("🧪 回测实验室")
st.caption("索引 ~/.tradingagents/rotation/ 顶层 JSON（不含 min_cache）")

if st.button("🔄 重新扫描"):
    load_registry.cache_clear()
    st.rerun()

strategies = load_registry().get("strategies", [])
id_to_name = {s["id"]: s["name"] for s in strategies}

col1, col2, col3 = st.columns(3)
with col1:
    strat_ids = ["（全部）"] + [s["id"] for s in strategies]
    picked_id = st.selectbox("策略", strat_ids)
with col2:
    kinds = ["（全部）", "backtest", "walk_forward", "grid_search", "validation"]
    picked_kind = st.selectbox("类型", kinds)
with col3:
    limit = st.number_input("条数", min_value=10, max_value=500, value=50, step=10)

sid = None if picked_id == "（全部）" else picked_id
kind = None if picked_kind == "（全部）" else picked_kind
artifacts = scan_artifacts(limit=int(limit), strategy_id=sid, kind=kind)

if not artifacts:
    st.info("没有匹配的结果文件")
else:
    rows = []
    for art in artifacts:
        rows.append({
            "时间": fmt_dt(art.mtime),
            "策略": id_to_name.get(art.strategy_id or "", art.strategy_id or "—"),
            "类型": art.kind,
            "摘要": art.summary,
            "文件": art.name,
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

st.divider()
st.subheader("查看详情")
names = [a.name for a in artifacts]
if names:
    selected = st.selectbox("选择文件", names)
    art = next(a for a in artifacts if a.name == selected)
    st.caption(str(art.path))
    if art.metrics:
        st.json(art.metrics)
    if st.checkbox("加载完整 JSON（大文件可能较慢）"):
        try:
            import json
            data = json.loads(art.path.read_text(encoding="utf-8"))
            st.json(data)
        except Exception as exc:
            st.error(f"加载失败: {exc}")

st.divider()
st.subheader("手动运行命令")
for strat in get_strategies():
    script = strat.get("script")
    if not script or strat.get("status") not in ("research", "rejected", "candidate"):
        continue
    with st.expander(strat.get("name", strat["id"])):
        st.caption(strat.get("conclusion", ""))
        st.code(f"python {script}", language="bash")

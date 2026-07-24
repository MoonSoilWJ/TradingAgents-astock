"""Shared Streamlit styling for strategy dashboard pages."""

from __future__ import annotations

import streamlit as st

from web.strategy.paths import STATUS_COLORS, STATUS_LABELS


def inject_css() -> None:
    st.markdown(
        """
        <style>
        .strategy-card {
            border: 1px solid #222;
            border-radius: 10px;
            padding: 1rem 1.2rem;
            margin-bottom: 0.8rem;
            background: #111;
        }
        .strategy-card h4 { margin: 0 0 0.4rem 0; color: #f5f1eb; }
        .strategy-meta { color: #888; font-size: 0.85rem; margin-bottom: 0.5rem; }
        .strategy-conclusion { color: #ccc; font-size: 0.9rem; line-height: 1.5; }
        .status-badge {
            display: inline-block;
            padding: 0.15rem 0.55rem;
            border-radius: 999px;
            font-size: 0.75rem;
            font-weight: 600;
            margin-right: 0.5rem;
        }
        .rule-kv { color: #aaa; font-size: 0.85rem; margin: 0.15rem 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def status_badge_html(status: str) -> str:
    color = STATUS_COLORS.get(status, "#9ca3af")
    label = STATUS_LABELS.get(status, status.upper())
    return (
        f'<span class="status-badge" style="background:{color}22;color:{color};'
        f'border:1px solid {color}55;">{label}</span>'
    )


def fmt_dt(dt) -> str:
    if dt is None:
        return "—"
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)


def render_strategy_card(strategy: dict, *, expanded: bool = False) -> None:
    status = strategy.get("status", "research")
    category = strategy.get("category", "")
    badge = status_badge_html(status)
    script = strategy.get("script", "")
    schedule = strategy.get("schedule", "")

    meta_parts = [p for p in [category, script, schedule] if p]
    meta = " · ".join(meta_parts)

    with st.expander(f"{strategy.get('name', strategy.get('id'))}", expanded=expanded):
        st.markdown(
            f'{badge}<span class="strategy-meta">{meta}</span>',
            unsafe_allow_html=True,
        )
        conclusion = strategy.get("conclusion")
        if conclusion:
            st.markdown(f'<div class="strategy-conclusion">{conclusion}</div>', unsafe_allow_html=True)

        rules = strategy.get("rules") or {}
        if rules:
            st.markdown("**规则**")
            for k, v in rules.items():
                st.markdown(f'<div class="rule-kv"><b>{k}</b>: {v}</div>', unsafe_allow_html=True)

        related = strategy.get("related") or []
        if related:
            st.caption(f"关联: {', '.join(related)}")

        cmd = f"python {script}" if script else ""
        if cmd:
            st.code(cmd, language="bash")

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

        /* ── B+idle 专属美化 ── */
        .section-title {
            font-size: 1.15rem; font-weight: 800; color: #f5f1eb;
            margin: 1.4rem 0 0.7rem; padding-left: 0.6rem;
            border-left: 4px solid #ff5a1f; line-height: 1.2;
        }
        .banner {
            border: 1px solid #2a2a3a; border-radius: 12px;
            padding: 0.9rem 1.1rem; background: linear-gradient(135deg,#171721,#101018);
            margin-bottom: 1rem;
        }
        .step-card {
            border: 1px solid #23232f; border-radius: 12px;
            padding: 0.7rem 0.95rem; background: #131319; margin-bottom: 0.6rem;
        }
        .step-time {
            display:inline-block; font-weight:800; color:#0a0a0a;
            background:#ff5a1f; border-radius:6px; padding:0.1rem 0.55rem;
            font-size:0.82rem; margin-right:0.5rem;
        }
        .step-title { font-weight:700; color:#f5f1eb; font-size:0.95rem; }
        .step-body { color:#b9bccb; font-size:0.85rem; line-height:1.5; margin-top:0.25rem; }
        .verdict {
            border-radius: 10px; padding: 0.7rem 0.95rem; margin: 0.6rem 0;
            font-size: 0.88rem; line-height: 1.55;
        }
        .verdict-ok { background:#0f2018; border:1px solid #1f6b46; color:#9be8c0; }
        .verdict-warn { background:#211a0f; border:1px solid #6b541f; color:#e8d29b; }
        .kpi-grid { display:flex; flex-wrap:wrap; gap:0.6rem; margin:0.5rem 0; }
        .kpi {
            flex:1 1 120px; border:1px solid #23232f; border-radius:10px;
            padding:0.6rem 0.8rem; background:#131319;
        }
        .kpi .k { color:#8b90a3; font-size:0.72rem; }
        .kpi .v { color:#ff5a1f; font-size:1.25rem; font-weight:800; }
        .kpi .d { color:#8b90a3; font-size:0.72rem; }
        .pill {
            display:inline-block; padding:0.1rem 0.5rem; border-radius:999px;
            font-size:0.72rem; font-weight:600; margin-left:0.4rem;
        }
        .pill-in { background:#0f2018; color:#9be8c0; border:1px solid #1f6b46; }
        .pill-out { background:#211a0f; color:#e8d29b; border:1px solid #6b541f; }
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

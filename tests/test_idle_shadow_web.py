"""Tests for idle shadow dashboard loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_load_idle_shadow_closed(tmp_path: Path, monkeypatch):
    rot = tmp_path / "rotation"
    rot.mkdir()

    log = rot / "t0_idle_shadow.jsonl"
    log.write_text(
        json.dumps({
            "event": "pick",
            "leg": "leg1",
            "ts": "2026-07-27T11:25:00",
            "code": "513050",
            "name": "中概互联",
            "v6_score": 88.5,
            "today_gain": 2.3,
            "signal_time": "11:25",
        }, ensure_ascii=False) + "\n"
        + json.dumps({
            "event": "buy",
            "leg": "leg1",
            "ts": "2026-07-27T13:05:00",
            "code": "513050",
            "buy_price": 1.234,
        }, ensure_ascii=False) + "\n"
        + json.dumps({
            "event": "sell",
            "leg": "leg1",
            "ts": "2026-07-27T13:30:00",
            "code": "513050",
            "name": "中概互联",
            "buy_price": 1.234,
            "sell_price": 1.256,
            "return_pct": 1.78,
            "sell_reason": "time_sell",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    state = rot / "t0_idle_shadow_state.json"
    state.write_text(json.dumps({
        "trade_date": "2026-07-27",
        "leg1": {"code": "513050", "closed": True},
        "leg2": None,
    }), encoding="utf-8")

    from web.strategy import idle_shadow

    monkeypatch.setattr(idle_shadow, "IDLE_SHADOW_LOG", log)
    monkeypatch.setattr(idle_shadow, "IDLE_SHADOW_STATE", state)

    data = idle_shadow.load_idle_shadow_data(days=30)
    assert data["stats"]["sell_count"] == 1
    assert data["stats"]["compound_pct"] == pytest.approx(1.78)

    rows = idle_shadow.closed_to_table_rows(data["closed"])
    assert rows[0]["代码"] == "513050"
    assert rows[0]["原因"] == "定时卖"
    assert rows[0]["v6"] == 88.5

    open_rows = idle_shadow.open_leg_rows(data["state"])
    assert open_rows == []

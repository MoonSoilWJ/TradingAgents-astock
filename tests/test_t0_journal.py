"""Tests for T+0 trade journal loader."""

from __future__ import annotations

import json
from pathlib import Path


def test_load_t0_trades_from_journal(tmp_path: Path, monkeypatch):
    rot = tmp_path / "rotation"
    rot.mkdir()

    journal = rot / "t0_trade_journal.jsonl"
    journal.write_text(
        json.dumps({
            "event": "trade_closed",
            "ts": "2026-07-21T11:05:55",
            "etf": "161129",
            "name": "原油LOF易方达",
            "type": "商品",
            "buy_date": "2026-07-20",
            "buy_time": "14:50",
            "buy_price": 1.993,
            "signal_gain_pct": 9.87,
            "sell_date": "2026-07-21",
            "sell_time": "11:05",
            "sell_reason": "time_sell",
            "sell_price": 1.893,
            "float_pct": -5.018,
            "return_pct": -5.018,
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    from web.strategy import t0_journal

    monkeypatch.setattr(t0_journal, "TRADE_JOURNAL", journal)
    monkeypatch.setattr(t0_journal, "SHADOW_LOG", rot / "missing.jsonl")

    data = t0_journal.load_t0_trades(days=30)
    assert len(data["closed"]) == 1
    row = t0_journal.trades_to_table_rows(data["closed"])[0]
    assert row["代码"] == "161129"
    assert row["卖出浮盈%"] == -5.02
    assert row["预估收益%"] == -5.02
    assert row["卖出原因"] == "11:05定时"


def test_backfill_from_shadow_log(tmp_path: Path, monkeypatch):
    rot = tmp_path / "rotation"
    rot.mkdir()
    shadow = rot / "t0_trail_shadow.jsonl"
    shadow.write_text(
        "\n".join([
            json.dumps({
                "ts": "2026-07-21T11:05:55",
                "check_time": "11:05",
                "etf": "161129",
                "buy_date": "2026-07-20",
                "buy_price": 1.993,
                "price": 1.893,
                "float_pct": -5.018,
            }, ensure_ascii=False),
            json.dumps({
                "ts": "2026-07-21T11:05:55",
                "event": "live_sell",
                "sell_reason": "time_sell",
                "etf": "161129",
                "name": "原油LOF",
                "buy_date": "2026-07-20",
                "buy_price": 1.993,
                "sell_price": 1.893,
                "return_pct": "-5.02%",
            }, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )

    from web.strategy import t0_journal

    monkeypatch.setattr(t0_journal, "TRADE_JOURNAL", rot / "missing.jsonl")
    monkeypatch.setattr(t0_journal, "SHADOW_LOG", shadow)

    data = t0_journal.load_t0_trades(days=30)
    assert len(data["closed"]) == 1
    assert data["closed"][0]["float_pct"] == -5.018
    assert data["closed"][0]["name"] == "原油LOF"


def test_backfill_trix_uses_bar_price(tmp_path: Path, monkeypatch):
    rot = tmp_path / "rotation"
    rot.mkdir()
    shadow = rot / "t0_trail_shadow.jsonl"
    shadow.write_text(
        "\n".join([
            json.dumps({
                "ts": "2026-07-17T09:40:57",
                "check_time": "09:40",
                "etf": "513770",
                "name": "港股通消费ETF",
                "buy_date": "2026-07-16",
                "buy_price": 0.379,
                "price": 0.376,
                "float_pct": -0.792,
                "trix_would_sell": True,
                "trix_sell_time": "09:40",
                "trix_sell_price": 0.375,
            }, ensure_ascii=False),
            json.dumps({
                "ts": "2026-07-17T09:40:57",
                "event": "live_sell",
                "sell_reason": "trix_death_cross",
                "etf": "513770",
                "buy_date": "2026-07-16",
                "buy_price": 0.379,
                "sell_price": 0.375,
                "return_pct": "-1.06%",
            }, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )

    from web.strategy import t0_journal

    monkeypatch.setattr(t0_journal, "TRADE_JOURNAL", rot / "missing.jsonl")
    monkeypatch.setattr(t0_journal, "SHADOW_LOG", shadow)

    trade = t0_journal.load_t0_trades(days=30)["closed"][0]
    row = t0_journal.trades_to_table_rows([trade])[0]
    assert row["标的"] == "港股通消费ETF"
    assert row["卖出浮盈%"] == -0.79
    assert row["预估收益%"] == -1.06
    assert row["卖出价"] == 0.375


def test_open_position_row():
    from web.strategy.t0_journal import open_position_row

    row = open_position_row({
        "position": {
            "etf": "161129",
            "name": "原油LOF",
            "buy_date": "2026-07-20",
            "buy_price": 1.993,
            "today_gain": 9.87,
            "sold": False,
        },
    })
    assert row is not None
    assert row["状态"] == "持仓中"
    assert open_position_row({"position": {"sold": True}}) is None

"""quality_pool.json 加载与路径."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POOL_JSON = ROOT / "strategies" / "data" / "quality_pool.json"


def test_quality_pool_json_exists():
    assert POOL_JSON.exists(), "运行 python scripts/quality_pool.py 生成池子"
    data = json.loads(POOL_JSON.read_text(encoding="utf-8"))
    assert data.get("version") == 2
    assert "selection_rules" in data


def test_load_quality_pool():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from quality_pool import load_quality_pool, trade_category

    pool = load_quality_pool(POOL_JSON)
    assert len(pool) >= 8
    assert all("code" in e and "sina_symbol" in e for e in pool)
    codes = {e["code"] for e in pool}
    assert "501018" in codes
    data = json.loads(POOL_JSON.read_text(encoding="utf-8"))
    assert "selection_rules" in data
    assert data["selection_rules"]["exclude_categories"] == []
    assert data["selection_rules"]["blacklist_codes"] == ["161626"]


def test_trade_category_hk():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from quality_pool import trade_category

    assert trade_category("恒生科技ETF", "159808") == "港股"
    assert trade_category("南方原油", "501018") == "商品能源"


def test_hybrid_regime_routing():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from quality_pool import (
        HYBRID_SCHEME_B,
        get_scan_universe,
        hybrid_should_skip_choppy,
        regime_uses_quality_pool,
    )
    from t0_etf_list import get_all_t0_etfs

    choppy = {"mode": "震荡", "skip_choppy": True}
    trend = {"mode": "趋势", "skip_choppy": False}
    neutral = {"mode": "中性", "skip_choppy": False}

    assert regime_uses_quality_pool(trend) is True
    assert regime_uses_quality_pool(choppy) is True
    assert regime_uses_quality_pool(neutral) is False
    assert hybrid_should_skip_choppy(choppy, hybrid=True) is False
    assert hybrid_should_skip_choppy(choppy, hybrid=False) is True
    assert hybrid_should_skip_choppy(trend, hybrid=True) is False

    uni = get_scan_universe(POOL_JSON)
    orig = get_all_t0_etfs()
    assert len(uni) >= len(orig)
    assert {e["code"] for e in orig}.issubset({e["code"] for e in uni})

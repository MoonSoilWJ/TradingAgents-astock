"""A-share instrument classification: listed stock vs on-exchange ETF."""

from __future__ import annotations

from typing import Literal

from tradingagents.dataflows.utils import safe_ticker_component

InstrumentType = Literal["stock", "etf"]

# Shanghai / Shenzhen on-exchange ETF code prefixes (6-digit codes).
_ETF_PREFIXES: tuple[str, ...] = (
    "510",
    "511",
    "512",
    "513",
    "515",
    "516",
    "517",
    "518",
    "560",
    "561",
    "562",
    "563",
    "588",
    "589",  # SH ETFs (incl. STAR board ETFs)
    "159",  # SZ ETFs (15xxx)
)

# Known ETF codes from alias table and common indices (belt-and-suspenders).
_KNOWN_ETF_CODES: frozenset[str] = frozenset(
    {
        "510050",
        "510300",
        "510500",
        "588000",
        "588080",
        "588800",
        "589020",
        "159915",
        "512480",
        "512760",
    }
)

ALL_ANALYSTS: tuple[str, ...] = (
    "market",
    "social",
    "news",
    "fundamentals",
    "policy",
    "hot_money",
    "lockup",
)

STOCK_ANALYSTS: tuple[str, ...] = ALL_ANALYSTS

# ETF: skip company fundamentals and lockup/insider (not applicable).
ETF_ANALYSTS: tuple[str, ...] = (
    "market",
    "social",
    "news",
    "policy",
    "hot_money",
)

ETF_SKIPPED_ANALYSTS: frozenset[str] = frozenset({"fundamentals", "lockup"})


def is_on_exchange_etf_code(code: str) -> bool:
    """Return True if ``code`` is a 6-digit on-exchange A-share ETF."""
    if len(code) != 6 or not code.isdigit():
        return False
    if code in _KNOWN_ETF_CODES:
        return True
    # SZ 159xxx (colloquial 15xxx); SH 51xxxx / 56xxxx / 588·589 STAR ETFs
    if code.startswith("159") or code.startswith("51") or code.startswith("56"):
        return True
    if code.startswith(("588", "589")):
        return True
    return code.startswith(_ETF_PREFIXES)


def is_listed_astock_code(code: str) -> bool:
    """True for A-share stocks and on-exchange ETFs resolvable in the name map."""
    if len(code) != 6 or not code.isdigit():
        return False
    if code[0] in "036":
        return True
    return is_on_exchange_etf_code(code)


def normalize_astock_code(ticker: str) -> str:
    """Return a 6-digit A-share code from ticker input."""
    return safe_ticker_component(ticker.strip())


def classify_astock_instrument(ticker: str) -> InstrumentType:
    """Classify a 6-digit A-share code as ``stock`` or ``etf``."""
    try:
        code = normalize_astock_code(ticker)
    except ValueError:
        return "stock"
    if is_on_exchange_etf_code(code):
        return "etf"
    return "stock"


def analysts_for_ticker(ticker: str) -> list[str]:
    """Return analyst pipeline keys appropriate for the instrument type."""
    if classify_astock_instrument(ticker) == "etf":
        return list(ETF_ANALYSTS)
    return list(STOCK_ANALYSTS)


def etf_skip_report(analyst_type: str) -> str:
    """Pre-filled report when an analyst is skipped in ETF mode."""
    name_map = {
        "fundamentals": "基本面分析师",
        "lockup": "解禁监控师",
    }
    label = name_map.get(analyst_type, analyst_type)
    return f"""## ETF 分析模式 — {label}已跳过

| 项目 | 说明 |
|------|------|
| **标的类型** | 场内 ETF（交易型开放式指数基金），非上市公司股票 |
| **跳过原因** | {label}的工具与框架面向个股财报、估值、限售解禁与内部人交易，不适用于 ETF |
| **替代分析** | 请结合：跟踪指数与行业景气、ETF 份额/资金申购赎回、折溢价率、K 线趋势、主题政策与成分股方向 |
| **下游提示** | 辩论与决策阶段**不得**因缺少 PE/PB/解禁数据而默认看空；应使用 ETF 专用框架 |

> 本报告为系统自动占位，表示该分析师节点未运行，数据质量门控应标记为「ETF 模式不适用」而非「报告失败」。
"""

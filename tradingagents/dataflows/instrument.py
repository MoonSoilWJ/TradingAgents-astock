"""A-share instrument classification: listed stock vs on-exchange ETF."""

from __future__ import annotations

from typing import Literal

from tradingagents.dataflows.utils import safe_ticker_component

InstrumentType = Literal["stock", "etf"]
SettlementRule = Literal["T0", "T1"]

# Bond, gold, cross-border, and commodity ETFs/LOFs are typically T+0 on A-shares.
_T0_ETF_PREFIXES: tuple[str, ...] = ("511", "518", "513")

# On-exchange LOF prefixes (Shanghai 501xxx, Shenzhen 161/162xxx).
_LOF_PREFIXES: tuple[str, ...] = ("501", "161", "162")

# Name keywords for T+0 ETFs/LOFs that share prefixes with T+1 products (e.g. 159xxx).
_T0_ETF_NAME_KEYWORDS: tuple[str, ...] = (
    "纳指",
    "纳斯达克",
    "NASDAQ",
    "Nasdaq",
    "港股通",
    "恒生",
    "H股",
    "香港",
    "中概",
    "标普",
    "S&P",
    "原油",
    "黄金ETF",
    "黄金基金",
    "上海金",
    "豆粕",
    "商品",
    "油气",
    "能源",
    "可转债",
    "白银",
    "稀土",
    "南方原油",
    "原油基金",
)

# Belt-and-suspenders for well-known T+0 codes (incl. SZ 159xxx cross-border).
_T0_ETF_CODES: frozenset[str] = frozenset(
    {
        "159001",
        "159003",
        "159920",
        "159941",
        "159792",
        "511880",
        "511990",
        "513050",
        "513100",
        "513130",
        "513180",
        "513500",
        "513600",
        "518880",
        "518800",
        # Commodity / cross-border LOFs (scripts/t0_etf_list.py)
        "501018",
        "161125",
        "161129",
        "162411",
        "162719",
        "159985",
        "159981",
        "159812",
        "159518",
        "159554",
        "562990",
    }
)

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
    """Return True if ``code`` is a 6-digit on-exchange A-share ETF or LOF."""
    if len(code) != 6 or not code.isdigit():
        return False
    if code in _KNOWN_ETF_CODES:
        return True
    if code.startswith(_LOF_PREFIXES):
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


def is_t0_etf_code(code: str, name: str | None = None) -> bool:
    """Return True when an on-exchange ETF/LOF settles T+0 (same-day sell allowed)."""
    if code in _T0_ETF_CODES:
        return True
    if not is_on_exchange_etf_code(code):
        return False
    if code.startswith(_T0_ETF_PREFIXES):
        return True
    if name:
        clean = name.replace(" ", "")
        if any(keyword in clean for keyword in _T0_ETF_NAME_KEYWORDS):
            return True
    return False


def settlement_rule(ticker: str, name: str | None = None) -> SettlementRule:
    """Return ``T0`` or ``T1`` for A-share stocks and on-exchange ETFs.

    Stocks are always T+1. ETFs/LOFs default to T+1 unless they are bond/gold/cross-border/
    commodity products (513/511/518 prefixes, 501/161/162 LOFs), a known T+0 code, or the
    name mentions 纳指 / 港股通 / 原油 / 黄金 and similar T+0 categories.
    """
    try:
        code = normalize_astock_code(ticker)
    except ValueError:
        return "T1"
    if is_t0_etf_code(code, name):
        return "T0"
    if not is_on_exchange_etf_code(code):
        return "T1"
    return "T1"


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

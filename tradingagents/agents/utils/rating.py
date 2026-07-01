"""Shared 5-tier rating vocabulary and a deterministic heuristic parser.

The same five-tier scale (Buy, Overweight, Hold, Underweight, Sell) is used by:
- The Research Manager (investment plan recommendation)
- The Portfolio Manager (final position decision)
- The signal processor (rating extracted for downstream consumers)
- The memory log (rating tag stored alongside each decision entry)

Centralising it here avoids drift between those call sites.
"""

from __future__ import annotations

import re
from typing import Tuple


# Canonical, ordered 5-tier scale (most bullish to most bearish).
RATINGS_5_TIER: Tuple[str, ...] = (
    "Buy", "Overweight", "Hold", "Underweight", "Sell",
)

_RATING_SET = {r.lower() for r in RATINGS_5_TIER}

# Matches "Rating: X" / "rating - X" / "Rating: **X**" — tolerates markdown
# bold wrappers and either a colon or hyphen separator.
_RATING_LABEL_RE = re.compile(r"rating.*?[:\-][\s*]*(\w+)", re.IGNORECASE)

# Chinese labels, e.g. 评级：卖出 / 推荐：买入 / Rating: 卖出
_RATING_CN_LABEL_RE = re.compile(
    r"(?:rating|评级|推荐|recommendation)\s*[：:\-]\s*[「""\*]*([^*」""\|\n]+)",
    re.IGNORECASE,
)

# e.g. 采用"卖出"评级 / 「卖出」评级
_CN_QUOTED_RATING_RE = re.compile(r'[「""\']([^"」\'""]+)[""」\']\s*评级')

# Map Chinese rating words to the canonical 5-tier English labels.
_CN_TO_EN: dict[str, str] = {
    "买入": "Buy",
    "卖出": "Sell",
    "持有": "Hold",
    "观望": "Hold",
    "增持": "Overweight",
    "超配": "Overweight",
    "加码": "Overweight",
    "减持": "Underweight",
    "低配": "Underweight",
    "减码": "Underweight",
    "清仓": "Sell",
}

# Fallback scan order: bearish phrases before bullish to reduce false positives.
_CN_FALLBACK_RES: list[tuple[str, re.Pattern[str]]] = [
    ("Sell", re.compile(r"卖出|清仓")),
    ("Underweight", re.compile(r"减持|低配")),
    ("Overweight", re.compile(r"增持|超配")),
    ("Buy", re.compile(r"买入")),
    ("Hold", re.compile(r"持有|观望")),
]

RATING_CN_LABELS: dict[str, str] = {
    "Buy": "买入",
    "Overweight": "增持",
    "Hold": "持有",
    "Underweight": "减持",
    "Sell": "卖出",
}


def _normalize_cn_token(token: str) -> str | None:
    cleaned = token.strip("*:.，,| ")
    if not cleaned:
        return None
    if cleaned.lower() in _RATING_SET:
        return cleaned.capitalize()
    return _CN_TO_EN.get(cleaned)


def parse_rating(text: str, default: str = "Hold") -> str:
    """Heuristically extract a 5-tier rating from prose text.

    Strategy (first match wins):
    1. Explicit English ``Rating: X`` label.
    2. Explicit Chinese ``评级/推荐: X`` label.
    3. Quoted Chinese rating, e.g. ``采用"卖出"评级``.
    4. First English 5-tier word in the text.
    5. Chinese keyword fallback (卖出/买入/持有…).

    Returns a Title-cased rating string, or ``default`` if no rating word appears.
    """
    if not text or not text.strip():
        return default

    for line in text.splitlines():
        m = _RATING_LABEL_RE.search(line)
        if m and m.group(1).lower() in _RATING_SET:
            return m.group(1).capitalize()

    for line in text.splitlines():
        m = _RATING_CN_LABEL_RE.search(line)
        if m:
            mapped = _normalize_cn_token(m.group(1))
            if mapped:
                return mapped

    m = _CN_QUOTED_RATING_RE.search(text)
    if m:
        mapped = _normalize_cn_token(m.group(1))
        if mapped:
            return mapped

    for line in text.splitlines():
        for word in line.lower().split():
            clean = word.strip("*:.,")
            if clean in _RATING_SET:
                return clean.capitalize()

    for rating, pattern in _CN_FALLBACK_RES:
        if pattern.search(text):
            return rating

    return default


def rating_display_label(rating: str) -> tuple[str, str]:
    """Return (english_label, chinese_label) for UI display."""
    normalized = parse_rating(rating, default="") if rating else ""
    if not normalized or normalized == "N/A":
        upper = (rating or "N/A").upper()
        cn = RATING_CN_LABELS.get(rating, "未知")
        return upper, cn
    return normalized.upper(), RATING_CN_LABELS.get(normalized, normalized)

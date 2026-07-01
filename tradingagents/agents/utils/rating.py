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

# e.g. 采用"卖出"评级 / 「卖出」评级
_CN_QUOTED_RATING_RE = re.compile(r'[「""\']([^"」\'""]+)[""」\']\s*评级')

# Explicit rating lines, highest priority first. Later lines win on equal priority.
_EXPLICIT_LINE_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    (100, re.compile(r"\*\*Rating\*\*\s*[：:\-]\s*\**(\w+)", re.IGNORECASE)),
    (95, re.compile(r"\bRating\s*[：:\-]\s*\**(\w+)", re.IGNORECASE)),
    (90, re.compile(r"最终(?:交易)?(?:决策)?评级\s*[：:\-]\s*\**([^*\n|]+)", re.IGNORECASE)),
    (88, re.compile(r"(?:研究|本交易提案)评级\s*[：:\-]\s*\**([^*\n|]+)", re.IGNORECASE)),
    (
        80,
        re.compile(
            r"(?:rating|评级|推荐|recommendation)\s*[：:\-]\s*[「""\*]*([^*」""\|\n]+)",
            re.IGNORECASE,
        ),
    ),
]

# Only scan these lines for keyword fallback — avoids matching prose like
# "内部人减持" or operational "执行卖出".
_RATING_CONTEXT_RE = re.compile(
    r"评级|Rating|Recommendation|交易提案|Decision|"
    r"操作指令|最终结论|裁决|建议.*(?:买入|卖出|减持|减配|持有|观望|清仓)",
    re.IGNORECASE,
)

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
    "减配": "Underweight",
    "低配": "Underweight",
    "减码": "Underweight",
    "清仓": "Sell",
}

# Fallback scan order on rating-context lines only (bearish before bullish).
_CN_FALLBACK_RES: list[tuple[str, re.Pattern[str]]] = [
    ("Sell", re.compile(r"卖出|清仓")),
    ("Underweight", re.compile(r"减持|减配|低配")),
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

_EN_WORD_IN_TOKEN_RE = re.compile(
    r"\b(buy|overweight|hold|underweight|sell)\b", re.IGNORECASE
)


def _normalize_cn_token(token: str) -> str | None:
    cleaned = token.strip("*:.，,| ")
    if not cleaned:
        return None
    if cleaned.lower() in _RATING_SET:
        return cleaned.capitalize()
    m = _EN_WORD_IN_TOKEN_RE.search(cleaned)
    if m:
        return m.group(1).capitalize()
    # Strip trailing parenthetical annotations, e.g. Underweight（减配）
    head = re.split(r"[（(]", cleaned, maxsplit=1)[0].strip()
    if head.lower() in _RATING_SET:
        return head.capitalize()
    return _CN_TO_EN.get(cleaned) or _CN_TO_EN.get(head)


def _pick_explicit_rating(text: str) -> str | None:
    """Return the best explicit rating label found in the report."""
    best_rating: str | None = None
    best_priority = -1
    best_line = -1

    for line_no, line in enumerate(text.splitlines()):
        for priority, pattern in _EXPLICIT_LINE_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            mapped = _normalize_cn_token(match.group(1))
            if not mapped:
                continue
            if priority > best_priority or (
                priority == best_priority and line_no >= best_line
            ):
                best_rating = mapped
                best_priority = priority
                best_line = line_no

    return best_rating


def _pick_contextual_fallback(text: str) -> str | None:
    """Keyword fallback limited to lines that look like rating declarations."""
    for line in reversed(text.splitlines()):
        if not _RATING_CONTEXT_RE.search(line):
            continue
        for rating, pattern in _CN_FALLBACK_RES:
            if pattern.search(line):
                return rating
        for word in line.lower().split():
            clean = word.strip("*:,.（）()")
            if clean in _RATING_SET:
                return clean.capitalize()
    return None


def parse_rating(text: str, default: str = "Hold") -> str:
    """Heuristically extract a 5-tier rating from prose text.

    Strategy:
    1. Explicit ``Rating: X`` / ``最终评级：X`` / ``研究评级：X`` lines
       (later lines win when priority is equal).
    2. Quoted Chinese rating, e.g. ``采用"卖出"评级``.
    3. Keyword fallback only on lines that mention 评级/Rating/decision context.

    Returns a Title-cased rating string, or ``default`` if no rating word appears.
    """
    if not text or not text.strip():
        return default

    explicit = _pick_explicit_rating(text)
    if explicit:
        return explicit

    quoted = _CN_QUOTED_RATING_RE.search(text)
    if quoted:
        mapped = _normalize_cn_token(quoted.group(1))
        if mapped:
            return mapped

    contextual = _pick_contextual_fallback(text)
    if contextual:
        return contextual

    return default


def rating_display_label(rating: str) -> tuple[str, str]:
    """Return (english_label, chinese_label) for UI display."""
    normalized = parse_rating(rating, default="") if rating else ""
    if not normalized or normalized == "N/A":
        upper = (rating or "N/A").upper()
        cn = RATING_CN_LABELS.get(rating, "未知")
        return upper, cn
    return normalized.upper(), RATING_CN_LABELS.get(normalized, normalized)

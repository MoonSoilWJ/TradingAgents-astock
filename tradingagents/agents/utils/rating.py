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

# Machine-readable rating embedded at the top of decision markdown by agents.
# Parsed first — immune to stop-loss / downgrade prose elsewhere in the report.
RATING_MARKER_RE = re.compile(
    r"<!--\s*TRADINGAGENTS_RATING:\s*(Buy|Overweight|Hold|Underweight|Sell)\s*-->",
    re.IGNORECASE,
)

# e.g. 采用"卖出"评级 / 「卖出」评级
_CN_QUOTED_RATING_RE = re.compile(r'[「""\']([^"」\'""]+)[""」\']\s*评级')

# Explicit rating lines, highest priority first. Later lines win on equal priority.
_EXPLICIT_LINE_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    (100, re.compile(r"\*\*Rating\*\*\s*[：:\-]\s*\**(\w+)", re.IGNORECASE)),
    (100, re.compile(r"\*\*Recommendation\*\*\s*[：:\-]\s*\**(\w+)", re.IGNORECASE)),
    (100, re.compile(r"\*\*评级\*\*\s*[：:\-]\s*\**(\w+)", re.IGNORECASE)),
    (100, re.compile(r"\*\*推荐\*\*\s*[：:\-]\s*\**(\w+)", re.IGNORECASE)),
    (98, re.compile(
        r"(?:\*\*)?(?:评级|Rating)(?:\*\*)?\s*[：:\-]\s*(?:\*\*)?\s*(\w+)",
        re.IGNORECASE,
    )),
    (95, re.compile(r"\bRating\s*[：:\-]\s*\**(\w+)", re.IGNORECASE)),
    (90, re.compile(r"最终(?:交易)?(?:决策)?评级\s*[：:\-]\s*\**([^*\n|]+)", re.IGNORECASE)),
    (88, re.compile(r"(?:研究|本交易提案)评级\s*[：:\-]\s*\**([^*\n|]+)", re.IGNORECASE)),
    (
        80,
        re.compile(
            r"(?:rating|评级|推荐|recommendation)\s*[：:\-]\s*(?:[\*「""\s]+)*([^*」""\|\n（(]+)",
            re.IGNORECASE,
        ),
    ),
]

# Anchor lines that precede the authoritative PM/RM rating in long-form reports.
_FINAL_RATING_SECTION_RE = re.compile(
    r"最终(?:交易)?(?:决策)?评级|##\s*[一二三四五六七八九十\d]+[、.]?\s*最终评级",
    re.IGNORECASE,
)
_HEADING_RATING_LINE_RE = re.compile(
    r"^#+\s*\*\*评级\s*[：:]",
    re.IGNORECASE,
)

# Header-like rating declaration (exclude stop-loss / downgrade prose).
_RATING_DECLARATION_RE = re.compile(
    r"(?:\*\*)?(?:评级|推荐|Rating|Recommendation)(?:\*\*)?\s*[：:]",
    re.IGNORECASE,
)

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


def normalize_rating_label(value: str | None) -> str | None:
    """Return a canonical 5-tier label, or None if ``value`` is not a rating."""
    if not value:
        return None
    token = str(value).strip()
    if token.capitalize() in RATINGS_5_TIER:
        return token.capitalize()
    return _normalize_cn_token(token)


def extract_rating_marker(text: str) -> str | None:
    """Read the machine-readable rating marker when present."""
    if not text:
        return None
    match = RATING_MARKER_RE.search(text)
    if not match:
        return None
    return normalize_rating_label(match.group(1))


def embed_rating_marker(rating: str, markdown: str) -> str:
    """Prepend a machine-readable rating marker unless one is already present."""
    normalized = normalize_rating_label(rating)
    if not normalized:
        return markdown
    if extract_rating_marker(markdown):
        return markdown
    marker = f"<!-- TRADINGAGENTS_RATING: {normalized} -->"
    body = markdown.strip()
    return f"{marker}\n\n{body}" if body else marker


def rating_from_structured_model(model: object) -> str | None:
    """Extract the 5-tier rating from a structured agent Pydantic model."""
    for attr in ("rating", "recommendation"):
        raw = getattr(model, attr, None)
        if raw is None:
            continue
        value = raw.value if hasattr(raw, "value") else str(raw)
        normalized = normalize_rating_label(value)
        if normalized:
            return normalized
    return None


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
        if re.search(r"研究经理|Research Manager", line, re.IGNORECASE):
            continue
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


def _pick_rating_from_decision_body(text: str) -> str | None:
    """Find the declared rating in long PM/RM reports whose preamble exceeds 40 lines."""
    lines = text.splitlines()

    for i, line in enumerate(lines[:200]):
        if _FINAL_RATING_SECTION_RE.search(line) or _HEADING_RATING_LINE_RE.search(line):
            window = "\n".join(lines[i : i + 25])
            explicit = _pick_explicit_rating(window)
            if explicit:
                return explicit

    for line in lines[:200]:
        if _HEADING_RATING_LINE_RE.search(line):
            explicit = _pick_explicit_rating(line)
            if explicit:
                return explicit

    return None


def _pick_contextual_fallback(text: str) -> str | None:
    """Keyword fallback limited to header-like rating declaration lines."""
    for line in reversed(text.splitlines()):
        if not _RATING_CONTEXT_RE.search(line):
            continue
        if not _RATING_DECLARATION_RE.search(line):
            continue
        en_match = _EN_WORD_IN_TOKEN_RE.search(line)
        if en_match:
            return en_match.group(1).capitalize()
        for rating, pattern in _CN_FALLBACK_RES:
            if pattern.search(line):
                return rating
        for word in line.lower().split():
            clean = word.strip("*:,.（）()")
            if clean in _RATING_SET:
                return clean.capitalize()
    return None


def parse_rating_from_header(text: str, max_lines: int = 80, default: str = "") -> str:
    """Parse rating from the decision header only (ignore stop-loss prose below)."""
    if not text or not text.strip():
        return default

    marker = extract_rating_marker(text)
    if marker:
        return marker

    section_rating = _pick_rating_from_decision_body(text)
    if section_rating:
        return section_rating

    head = "\n".join(text.splitlines()[:max_lines])
    explicit = _pick_explicit_rating(head)
    if explicit:
        return explicit
    quoted = _CN_QUOTED_RATING_RE.search(head)
    if quoted:
        mapped = _normalize_cn_token(quoted.group(1))
        if mapped:
            return mapped
    contextual = _pick_contextual_fallback(head)
    if contextual:
        return contextual
    return default


def canonicalize_decision_ratings(state: dict) -> dict:
    """Ensure authoritative rating fields and HTML markers are always present.

    Called before persisting a completed run and when loading saved reports so
    the UI never re-derives ratings from stop-loss / downgrade prose.
    Mutates and returns ``state`` in place.
    """
    pairs = (
        ("research_rating", "investment_plan"),
        ("portfolio_rating", "final_trade_decision"),
    )
    for rating_key, text_key in pairs:
        text = str(state.get(text_key) or "")
        marker_rating = extract_rating_marker(text) if text else None
        field_rating = normalize_rating_label(state.get(rating_key))
        rating = marker_rating or field_rating
        if not rating and text:
            rating = parse_rating_from_header(text, default="")
        if not rating:
            continue
        state[rating_key] = rating
        if text:
            state[text_key] = embed_rating_marker(rating, text)
    return state


def parse_rating(text: str, default: str = "Hold") -> str:
    """Heuristically extract a 5-tier rating from prose text.

    Strategy:
    0. Machine-readable ``<!-- TRADINGAGENTS_RATING: X -->`` marker (authoritative).
    1. Explicit ``Rating: X`` / ``最终评级：X`` / ``研究评级：X`` lines
       (later lines win when priority is equal).
    2. Quoted Chinese rating, e.g. ``采用"卖出"评级``.
    3. Keyword fallback only on header-like rating declaration lines.

    Returns a Title-cased rating string, or ``default`` if no rating word appears.
    """
    if not text or not text.strip():
        return default

    marker = extract_rating_marker(text)
    if marker:
        return marker

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
    normalized = normalize_rating_label(rating) if rating else None
    if not normalized and rating:
        normalized = parse_rating(rating, default="")
    if not normalized or normalized == "N/A":
        upper = (rating or "N/A").upper()
        cn = RATING_CN_LABELS.get(rating, "未知")
        return upper, cn
    return normalized.upper(), RATING_CN_LABELS.get(normalized, normalized)

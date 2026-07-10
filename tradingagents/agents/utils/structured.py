"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. At invocation, run the structured call and render the result back to
   markdown. If the structured call itself fails for any reason
   (malformed JSON from a weak model, transient provider issue), fall
   back to a plain ``llm.invoke`` so the pipeline never blocks.
3. Persist the authoritative rating in a machine-readable HTML comment at
   the top of the markdown so downstream UI never re-parses stop-loss prose.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar

from pydantic import BaseModel

from tradingagents.agents.utils.rating import (
    embed_rating_marker,
    extract_rating_marker,
    normalize_rating_label,
    parse_rating_from_header,
    rating_from_structured_model,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class AgentMarkdownResult:
    """Markdown body plus the authoritative 5-tier rating when known."""

    markdown: str
    rating: str | None = None


@dataclass(frozen=True)
class IntradayOrderResult:
    """Markdown plus structured intraday order fields."""

    markdown: str
    action: str
    quantity_shares: int
    reason: str


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Optional[Any]:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.
    """
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def _finalize_markdown(markdown: str, rating: str | None) -> AgentMarkdownResult:
    """Ensure markdown carries a rating marker when the rating is known."""
    if rating:
        return AgentMarkdownResult(
            embed_rating_marker(rating, markdown),
            rating,
        )
    marker = extract_rating_marker(markdown)
    if marker:
        return AgentMarkdownResult(markdown, marker)
    parsed = parse_rating_from_header(markdown, default="")
    if parsed:
        return AgentMarkdownResult(embed_rating_marker(parsed, markdown), parsed)
    return AgentMarkdownResult(markdown, None)


def _parse_intraday_fallback(text: str) -> IntradayOrderResult:
    from tradingagents.agents.schemas import IntradayAction

    lowered = text.lower()
    action = "hold"
    if "买入" in text or "buy" in lowered:
        action = "buy"
    elif "卖出" in text or "sell" in lowered:
        action = "sell"
    qty = 0
    import re

    m = re.search(r"(\d+)\s*股", text)
    if m:
        qty = int(m.group(1))
    if action == "hold":
        qty = 0
    return IntradayOrderResult(
        markdown=text,
        action=action,
        quantity_shares=qty,
        reason=text[:200],
    )


def invoke_intraday_structured_or_freetext(
    structured_llm: Optional[Any],
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> IntradayOrderResult:
    if structured_llm is not None:
        try:
            result = structured_llm.invoke(prompt)
            action = getattr(result.action, "value", str(result.action))
            rendered = render(result)
            if not rendered or not str(rendered).strip():
                raise ValueError("structured output rendered empty")
            return IntradayOrderResult(
                markdown=str(rendered),
                action=action,
                quantity_shares=int(result.quantity_shares or 0),
                reason=str(result.reason or ""),
            )
        except Exception as exc:
            msg = str(exc)
            if "Thinking mode" in msg and "tool_choice" in msg:
                logger.info(
                    "%s: thinking model skips structured output; using free text",
                    agent_name,
                )
            else:
                logger.warning(
                    "%s: structured-output invocation failed (%s); retrying once as free text",
                    agent_name,
                    exc,
                )

    response = plain_llm.invoke(prompt)
    content = response.content if hasattr(response, "content") else str(response)
    if not content or not str(content).strip():
        raise ValueError(f"{agent_name} produced empty free-text output")
    parsed = _parse_intraday_fallback(str(content))
    return IntradayOrderResult(
        markdown=parsed.markdown,
        action=parsed.action,
        quantity_shares=parsed.quantity_shares,
        reason=parsed.reason,
    )


def invoke_structured_or_freetext(
    structured_llm: Optional[Any],
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> AgentMarkdownResult:
    """Run the structured call and render to markdown; fall back to free-text on any failure.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape). The same value is forwarded to the free-text path so the
    fallback sees the same input the structured call did.
    """
    if structured_llm is not None:
        try:
            result = structured_llm.invoke(prompt)
            rating = rating_from_structured_model(result)
            rendered = render(result)
            if not rendered or not str(rendered).strip():
                raise ValueError("structured output rendered empty")
            return _finalize_markdown(str(rendered), rating)
        except Exception as exc:
            msg = str(exc)
            if "Thinking mode" in msg and "tool_choice" in msg:
                logger.info(
                    "%s: thinking model skips structured output; using free text",
                    agent_name,
                )
            else:
                logger.warning(
                    "%s: structured-output invocation failed (%s); retrying once as free text",
                    agent_name, exc,
                )

    response = plain_llm.invoke(prompt)
    content = response.content if hasattr(response, "content") else str(response)
    if not content or not str(content).strip():
        raise ValueError(f"{agent_name} produced empty free-text output")
    return _finalize_markdown(str(content), None)

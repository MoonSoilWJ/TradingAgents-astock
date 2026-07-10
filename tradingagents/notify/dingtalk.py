"""DingTalk robot webhook notifications."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from tradingagents.portfolio.store import PortfolioState

logger = logging.getLogger(__name__)

# Ensure .env is loaded when notify is imported outside scripts/intraday_daemon.py
try:
    from dotenv import load_dotenv

    _ROOT = Path(__file__).resolve().parents[2]
    load_dotenv(_ROOT / ".env")
except Exception:
    pass


def _dingtalk_keyword() -> str:
    return (os.getenv("DINGTALK_KEYWORD") or "").strip()


def _with_keyword(title: str, text: str, keyword: str | None = None) -> tuple[str, str]:
    """Ensure custom robot keyword appears in title/body (DingTalk errcode 310000)."""
    kw = (keyword or _dingtalk_keyword()).strip()
    if not kw or kw in title or kw in text:
        return title, text
    return f"{kw} {title}", f"{kw}\n\n{text}"


def _sign_webhook(webhook: str, secret: str) -> str:
    timestamp = str(round(__import__("time").time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={timestamp}&sign={sign}"


def send_markdown(
    title: str,
    text: str,
    *,
    webhook: str | None = None,
    keyword: str | None = None,
) -> bool:
    url = (webhook or os.getenv("DINGTALK_WEBHOOK") or "").strip()
    if not url:
        logger.warning("DingTalk webhook not configured; skip notify")
        return False
    title, text = _with_keyword(title, text, keyword)
    kw = (keyword or _dingtalk_keyword()).strip()
    if kw:
        if kw in text or kw in title:
            logger.info("DingTalk send: keyword '%s' present in message", kw)
        else:
            logger.warning("DingTalk send: keyword '%s' missing after prepend — check robot settings", kw)
    else:
        logger.warning(
            "DingTalk send: DINGTALK_KEYWORD not set (custom robots need it; errcode 310000)"
        )
    secret = (os.getenv("DINGTALK_SECRET") or "").strip()
    if secret:
        url = _sign_webhook(url, secret)
    payload: dict[str, Any] = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            if data.get("errcode") == 310000:
                logger.error(
                    "DingTalk error: %s — set DINGTALK_KEYWORD in .env to your "
                    "robot's required keyword (e.g. DINGTALK_KEYWORD=test)",
                    data,
                )
            else:
                logger.error("DingTalk error: %s", data)
            return False
        return True
    except Exception as exc:
        logger.error("DingTalk send failed: %s", exc)
        return False


def format_order_message(
    *,
    ticker: str,
    name: str,
    slot: str,
    action: str,
    quantity: int,
    price: float,
    reason: str,
    state: PortfolioState,
    booked: bool,
    note: str = "",
    runs_today: int = 0,
    next_slot: str | None = None,
    report_path: str | None = None,
) -> str:
    action_cn = {"buy": "买入", "sell": "卖出", "hold": "不动"}.get(action, action)
    booked_label = "已记账" if booked else "未记账"
    if not booked and note:
        booked_label = note
    pnl = ""
    if state.shares and state.avg_cost and price:
        pct = (price - state.avg_cost) / state.avg_cost * 100
        pnl = f" | 成本 {state.avg_cost:.3f} | 浮盈 {pct:+.1f}%"
    lines = [
        f"### {name}({ticker}) {slot} 盘中建议",
        "",
        f"**操作**: {action_cn}",
        f"**数量**: {quantity} 股",
        f"**参考价**: {price:.3f}",
        f"**持仓**: {state.shares} 股 | 现金 {state.cash:,.0f} 元{pnl}",
        "",
        f"**理由**: {reason}",
        "",
        f"---",
        f"**记账**: {booked_label}",
    ]
    if runs_today:
        lines.append(f"今日已跑: {runs_today} 次" + (f" | 下次: {next_slot}" if next_slot else ""))
    if report_path:
        lines.append(f"**完整分析日志**: `{report_path}`")
    return "\n".join(lines)


def format_stop_message(
    *,
    ticker: str,
    name: str,
    slot: str,
    reason: str = "已手动硬停止，本次分析中断，未记账",
) -> str:
    return "\n".join(
        [
            f"### {name}({ticker}) {slot} 已停止",
            "",
            f"**原因**: {reason}",
        ]
    )


def format_skip_message(
    *,
    ticker: str,
    name: str,
    slot: str,
    reason: str = "上一轮分析仍在进行，本次定时已跳过",
    next_slot: str | None = None,
) -> str:
    lines = [
        f"### {name}({ticker}) {slot} 跳过",
        "",
        f"**原因**: {reason}",
    ]
    if next_slot:
        lines.append(f"**下次**: {next_slot}")
    return "\n".join(lines)

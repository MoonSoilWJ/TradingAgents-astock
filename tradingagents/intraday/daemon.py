"""Intraday monitoring daemon — polls session and runs scheduled slots."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from tradingagents.dataflows.a_stock import lookup_astock_name
from tradingagents.intraday.calendar import INTRADAY_SLOTS, next_slot_after, slot_due
from tradingagents.intraday.runner import run_intraday_cycle
from tradingagents.intraday.session import (
    is_active,
    load_session,
    mark_running,
    record_skipped_slot,
    should_stop,
    write_daemon_heartbeat,
)
from tradingagents.notify.dingtalk import format_skip_message, format_stop_message, send_markdown

logger = logging.getLogger("intraday_daemon")

POLL_SECONDS = int(os.getenv("INTRADAY_POLL_SECONDS", "30"))


def _already_ran_slot(session, slot: str) -> bool:
    return session.last_slot == slot and session.trade_date == datetime.now().date().isoformat()


def run_daemon() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [intraday] %(message)s",
    )
    # Propagate stage/debate lines from runner + progress_log into this terminal.
    for name in ("intraday", "tradingagents.intraday.runner", "tradingagents.intraday.progress_log"):
        logging.getLogger(name).setLevel(logging.INFO)

    kw = (os.getenv("DINGTALK_KEYWORD") or "").strip()
    logger.info("Intraday daemon started; slots=%s", ",".join(INTRADAY_SLOTS))
    logger.info(
        "Per-run debate reports: ~/.tradingagents/intraday/runs/<code>_<date>_<slot>.md"
    )
    if kw:
        logger.info("DingTalk keyword loaded: %s", kw)
    else:
        logger.warning(
            "DINGTALK_KEYWORD not set — custom robots return errcode 310000; "
            "add e.g. DINGTALK_KEYWORD=test to .env and restart daemon"
        )
    immediate_on_start = False
    last_active = False

    while True:
        write_daemon_heartbeat()
        session = load_session()
        active = is_active()

        if active and not last_active:
            immediate_on_start = True
            logger.info("Session started for %s — immediate run", session.ticker)

        if not active:
            last_active = False
            time.sleep(POLL_SECONDS)
            continue

        if should_stop():
            if session.running:
                logger.info("Hard stop requested while run in progress")
            mark_running(False)
            last_active = False
            immediate_on_start = False
            time.sleep(POLL_SECONDS)
            continue

        now = datetime.now()
        slot: str | None = None
        force_full = False

        if immediate_on_start:
            slot = now.strftime("%H:%M")
            force_full = session.full_run_done_date != now.date().isoformat()
            immediate_on_start = False
        else:
            slot = slot_due(now, session.last_slot)
            if slot and _already_ran_slot(session, slot):
                slot = None

        if slot and session.running:
            name = lookup_astock_name(session.ticker) or session.ticker
            msg = format_skip_message(
                ticker=session.ticker,
                name=name,
                slot=slot,
                next_slot=next_slot_after(now),
            )
            send_markdown(
                f"{session.ticker} skip",
                msg,
                webhook=session.dingtalk_webhook or None,
            )
            record_skipped_slot(slot)
            last_active = active
            time.sleep(POLL_SECONDS)
            continue

        if slot:
            try:
                logger.info("Running slot %s for %s", slot, session.ticker)
                run_intraday_cycle(slot=slot, force_full=force_full)
            except Exception as exc:
                from tradingagents.intraday.runner import HardStopRequested

                if isinstance(exc, HardStopRequested) or should_stop():
                    logger.info("Hard stop acknowledged for slot %s", slot)
                    name = lookup_astock_name(session.ticker) or session.ticker
                    send_markdown(
                        f"{session.ticker} stopped",
                        format_stop_message(
                            ticker=session.ticker,
                            name=name,
                            slot=slot,
                        ),
                        webhook=session.dingtalk_webhook or None,
                    )
                else:
                    logger.exception("Run failed: %s", exc)

        last_active = active
        time.sleep(POLL_SECONDS)

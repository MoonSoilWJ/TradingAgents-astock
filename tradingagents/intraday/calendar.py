"""Trading calendar helpers for intraday scheduling."""

from __future__ import annotations

from datetime import date, datetime, time

# Hourly slots (Beijing). 12:25 lunch break omitted.
INTRADAY_SLOTS: tuple[str, ...] = (
    "09:25",
    "10:25",
    "11:25",
    "13:25",
    "14:25",
    "15:25",
    "16:00",
)

_TRADING_SESSIONS: tuple[tuple[time, time], ...] = (
    (time(9, 30), time(11, 30)),
    (time(13, 0), time(15, 0)),
)

# Static 2026 holidays (extend as needed).
_HOLIDAYS_2026: frozenset[str] = frozenset(
    {
        "2026-01-01",
        "2026-01-02",
        "2026-02-17",
        "2026-02-18",
        "2026-02-19",
        "2026-02-20",
        "2026-02-23",
        "2026-04-06",
        "2026-05-01",
        "2026-05-04",
        "2026-05-05",
        "2026-06-19",
        "2026-10-01",
        "2026-10-02",
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
    }
)


def is_trading_day(day: date | None = None) -> bool:
    day = day or date.today()
    if day.weekday() >= 5:
        return False
    return day.isoformat() not in _HOLIDAYS_2026


def is_trading_session(now: datetime | None = None) -> bool:
    """True during continuous auction sessions 09:30-11:30 and 13:00-15:00."""
    now = now or datetime.now()
    t = now.time()
    return any(start <= t <= end for start, end in _TRADING_SESSIONS)


def is_bookable_session(now: datetime | None = None) -> bool:
    """Auto-ledger only during regular trading sessions."""
    return is_trading_session(now)


def slot_due(now: datetime, last_slot: str | None, min_gap_minutes: int = 20) -> str | None:
    """Return the schedule slot HH:MM if it should fire now."""
    current = now.strftime("%H:%M")
    for slot in INTRADAY_SLOTS:
        if current < slot:
            break
        if last_slot and slot <= last_slot:
            continue
        if current == slot or (current > slot and _within_minutes_after(current, slot, 2)):
            if last_slot:
                last_dt = datetime.combine(now.date(), _parse_hm(last_slot))
                cur_dt = datetime.combine(now.date(), _parse_hm(slot))
                if (cur_dt - last_dt).total_seconds() < min_gap_minutes * 60:
                    continue
            return slot
    return None


def next_slot_after(now: datetime | None = None) -> str | None:
    now = now or datetime.now()
    current = now.strftime("%H:%M")
    for slot in INTRADAY_SLOTS:
        if slot > current:
            return slot
    return None


def _parse_hm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _within_minutes_after(current_hm: str, slot_hm: str, minutes: int) -> bool:
    base = datetime.combine(date.today(), _parse_hm(slot_hm))
    cur = datetime.combine(date.today(), _parse_hm(current_hm))
    delta = (cur - base).total_seconds() / 60
    return 0 <= delta <= minutes

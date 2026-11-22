"""GTFS clock strings. Hours may pass 24:00 and belong to the next day."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


def parse_gtfs_time(service_day: date, hhmmss: str, tz: ZoneInfo = BERLIN) -> datetime | None:
    text = (hhmmss or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return None
    if not (0 <= minutes <= 59 and 0 <= seconds <= 60):
        return None
    extra_days, hours = divmod(hours, 24)
    if extra_days > 7:
        return None
    return datetime.combine(
        service_day + timedelta(days=extra_days),
        time(hours, minutes, seconds if seconds < 60 else 59),
        tzinfo=tz,
    )


def parse_service_day(yyyymmdd: str) -> date | None:
    if len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return None
    try:
        return datetime.strptime(yyyymmdd, "%Y%m%d").date()
    except ValueError:
        return None

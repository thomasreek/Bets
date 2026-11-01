"""Show game times in the user's time zone (default Houston, set BETFINDER_TZ to change)."""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "America/Chicago"  # Houston


def local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("BETFINDER_TZ", DEFAULT_TZ))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def fmt_time(iso: str) -> str:
    """'2026-11-01T18:00:00Z' -> 'Sun 1 Nov 12:00 PM CST'."""
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(local_tz())
    except ValueError:
        return iso
    hour = t.hour % 12 or 12
    return f"{t:%a} {t.day} {t:%b} {hour}:{t:%M} {'AM' if t.hour < 12 else 'PM'} {t:%Z}"

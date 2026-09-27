"""System (wall-clock) timestamps, kept alongside the stopwatch.

Durations are measured with the monotonic clock (immune to NTP steps and
clock changes). These helpers stamp the same moments with the system's UTC
clock so a turn can be lined up against other logs — `docker logs -t` on the
llama.cpp container prints the same system clock — and so each request
cross-checks the two clocks (a disagreement is logged as an event).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    """2026-09-27T23:02:25.391Z (millisecond precision)."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") \
        + f"{dt.microsecond // 1000:03d}Z"


def hms(dt: Optional[datetime]) -> str:
    """23:02:25.391 UTC — for status lines."""
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d} UTC"


def ms_between(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() * 1000.0, 1)


class Stamp:
    """One moment on both clocks."""
    __slots__ = ("wall", "mono")

    def __init__(self):
        self.wall = now()
        self.mono = time.monotonic()

    def elapsed_both(self) -> tuple[float, float]:
        """(stopwatch ms, system-clock ms) since this stamp."""
        return ((time.monotonic() - self.mono) * 1000.0,
                (now() - self.wall).total_seconds() * 1000.0)

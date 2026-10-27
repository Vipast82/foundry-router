"""Heartbeat cover for long engine steps.

The coordinator writes its heartbeat after every tick, and the Foundry Client
restarts a coordinator whose heartbeat goes quiet. Some steps legitimately
hold one tick for many minutes: a verify that runs a project's test suites
(the AU Lune suite runs twice, up to 10 minutes each). Without cover the
client restarts the busy coordinator mid-verify, the verify starts over, and
the loop never ends (2026-10-27, GF-GATE1).

A step wraps itself in `busy(label, max_s)`. While one is open, the process
files' keep-alive thread refreshes the heartbeat and says what is running.
Each step has a ceiling: past `max_s` the cover lapses and the heartbeat goes
quiet again, so a step that hangs is still found and restarted. A tick that
hangs outside any declared step gets no cover at all.
"""

from __future__ import annotations

import contextlib
import itertools
import threading
import time
from typing import Iterator, Optional

_lock = threading.Lock()
_ids = itertools.count(1)
_open: dict[int, tuple[str, float, float]] = {}      # id -> (label, started, deadline)


@contextlib.contextmanager
def busy(label: str, max_s: float, clock=time.time) -> Iterator[None]:
    """Cover the heartbeat for at most `max_s` seconds while the block runs."""
    now = clock()
    with _lock:
        i = next(_ids)
        _open[i] = (str(label)[:120], now, now + max(0.0, float(max_s)))
    try:
        yield
    finally:
        with _lock:
            _open.pop(i, None)


def current(clock=time.time) -> Optional[dict]:
    """The newest step still inside its ceiling, or None: {label, elapsed_s}."""
    now = clock()
    with _lock:
        live = [(s, lbl) for lbl, s, dl in _open.values() if now <= dl]
    if not live:
        return None
    started, label = max(live)
    return {"label": label, "elapsed_s": int(now - started)}


def reset() -> None:
    """Tests: forget every open step."""
    with _lock:
        _open.clear()

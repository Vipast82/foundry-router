"""Stall watchdog (v0.181.124): what was the router doing when it froze?

Everything in the router runs on one asyncio event loop. Any code that works for
seconds without yielding (a long synchronous query, parsing a multi-megabyte
request, a tight loop) freezes every stream, every health check and the admin UI
for that long. On 2026-11-11 the live router went silent for 10 to 30 seconds
three times and nothing in the events said why.

Two cooperating pieces:

* a task on the loop stamps the time every 250 ms;
* a daemon THREAD looks at that stamp every 500 ms. When the stamp is older than
  `THRESHOLD_S` the loop is stuck, and the thread, which is not stuck, copies the
  loop thread's current stack (`sys._current_frames`). That stack is the answer.

When the loop runs again, the task writes one warning event:
"router stalled for 12.4 s in graph (routes_view.py:88)" with the stack in its
detail, and keeps the last stalls for `/health` and the admin API.
Cost when nothing is wrong: one timestamp four times a second.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from collections import deque
from typing import Callable, Optional

log = logging.getLogger(__name__)

THRESHOLD_S = 2.0          # a pause this long is a stall worth recording
TICK_S = 0.25
CHECK_S = 0.5
KEEP = 20
OUR_FILES = ("foundry_router", "foundry_coordinator", "foundry_automation", "foundry_training")


def _ours(filename: str) -> bool:
    path = filename.replace("\\", "/")
    return any("/" + p + "/" in path for p in OUR_FILES) and "/loopwatch.py" not in path


def _where(stack: list) -> str:
    """The innermost frame in OUR code, as "function (file.py:line)"; a stall inside a
    library call is named by the Foundry function that made it."""
    for f in reversed(stack):
        path = f.filename.replace("\\", "/")
        if any("/" + p + "/" in path for p in OUR_FILES) and "/loopwatch.py" not in path:
            return f"{f.name} ({path.rsplit('/', 1)[-1]}:{f.lineno})"
    if stack:
        f = stack[-1]
        return f"{f.name} ({f.filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{f.lineno})"
    return "unknown"


def _stack_text(stack: list) -> str:
    """The last 14 frames; when none of them is Foundry code (a stall deep inside a
    library, such as response encoding) the innermost Foundry frames come first."""
    tail = stack[-14:]
    head = [] if any(_ours(f.filename) for f in tail) else [f for f in stack[:-14] if _ours(f.filename)][-4:]
    return "".join(traceback.format_list(head)) + ("  ...\n" if head else "") \
        + "".join(traceback.format_list(tail))


def _request(frame) -> str:
    """"GET /admin/api/work" for the HTTP request the loop was serving, read from the
    ASGI `scope` in one of the stalled frames; "" when it was not serving one."""
    try:
        seen = 0
        while frame is not None and seen < 200:
            scope = frame.f_locals.get("scope")
            if isinstance(scope, dict) and scope.get("type") == "http" and scope.get("path"):
                return f"{scope.get('method', '')} {scope['path']}"[:200]
            frame, seen = frame.f_back, seen + 1
    except Exception:                                             # noqa: BLE001
        pass
    return ""


def _others(frames: dict, loop_thread: Optional[int]) -> list:
    """Other threads that were inside Foundry code at that moment, as
    "thread-name: function (file.py:line)". When the loop is waiting for a lock, the
    thread that holds it is in this list (v0.181.125)."""
    names = {t.ident: t.name for t in threading.enumerate()}
    me, out = threading.get_ident(), []
    for ident, frame in frames.items():
        if ident in (loop_thread, me):
            continue
        stack = traceback.extract_stack(frame)
        if not any(_ours(f.filename) for f in stack):
            continue
        out.append(f"{names.get(ident, ident)}: {_where(stack)}")
    return out[:6]


class LoopWatch:
    def __init__(self, on_stall: Optional[Callable[[dict], None]] = None,
                 threshold: float = THRESHOLD_S):
        self.on_stall = on_stall
        self.threshold = float(threshold)
        self.stalls: deque = deque(maxlen=KEEP)
        self._last_tick = time.monotonic()
        self._pending: Optional[dict] = None       # written by the thread, read by the task
        self._loop_thread: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._task: Optional[asyncio.Task] = None

    # -- the thread: notices the stall and takes the stack ------------------------------
    def _watch(self) -> None:
        while not self._stop.wait(CHECK_S):
            behind = time.monotonic() - self._last_tick
            if behind < self.threshold or self._pending is not None or self._loop_thread is None:
                continue
            frames = sys._current_frames()
            frame = frames.get(self._loop_thread)
            stack = traceback.extract_stack(frame) if frame is not None else []
            self._pending = {"since": self._last_tick, "wall": time.time() - behind,
                             "where": _where(stack), "stack": _stack_text(stack),
                             "request": _request(frame),
                             "others": _others(frames, self._loop_thread)}

    # -- the task: stamps the time, reports once the loop moves again ----------------------
    async def _tick(self) -> None:
        self._loop_thread = threading.get_ident()
        while True:
            self._last_tick = time.monotonic()
            pending, self._pending = self._pending, None
            if pending is not None:
                self._report(pending, self._last_tick - pending["since"])
            await asyncio.sleep(TICK_S)

    def _report(self, pending: dict, seconds: float) -> None:
        row = {"at": pending["wall"], "seconds": round(seconds, 1), "where": pending["where"],
               "stack": pending["stack"], "others": pending.get("others") or [],
               "request": pending.get("request") or ""}
        self.stalls.append(row)
        log.warning("event loop stalled for %.1f s in %s", seconds, pending["where"])
        if self.on_stall is not None:
            try:
                self.on_stall(row)
            except Exception:                                     # noqa: BLE001
                log.exception("stall report failed")

    # -- lifecycle ------------------------------------------------------------------------------
    def start(self) -> None:
        if self._task is not None:
            return
        self._last_tick = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._tick())
        self._thread = threading.Thread(target=self._watch, name="foundry-loopwatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def summary(self) -> dict:
        """For /health: how many stalls are remembered and the latest one (no stack)."""
        last = self.stalls[-1] if self.stalls else None
        return {"count": len(self.stalls),
                "last": ({k: last[k] for k in ("at", "seconds", "where")} if last else None)}
        # (the request, the other threads and the stack are in /admin/api/health/stalls)

"""One keep-alive policy for every streaming path.

Two different jobs used to share one mechanism:

  * keeping the CONNECTION alive — reverse proxies (Cloudflare tunnel ~100s,
    NPM) and some clients drop a stream that sends no bytes for a while. That
    needs bytes on a short interval, but they don't have to be visible;
  * telling the USER the request is alive — which needs text, but not every
    few seconds: each visible beat is a NEW line in the client's thinking
    panel (Cline, Open WebUI), so a 5s heartbeat during a 5-minute prefill
    printed 60 "still working" lines.

So every heartbeat tick now sends an invisible keep-alive chunk (an empty
message — bytes on the wire, nothing rendered), and only a sparse set of
ticks carries a visible status line: at ~30s, ~60s, then every
`heartbeat_visible_seconds` (default 60). All paths use the same wording.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Optional


def fmt_elapsed(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def status_line(who: str, elapsed: float, detail: str = "") -> str:
    """The one visible 'still working' wording, used everywhere."""
    return (f"⏳ {who} — still working · {fmt_elapsed(elapsed)}"
            + (f" ({detail})" if detail else "") + "\n")


def visible_every(cfg) -> float:
    """agent_brain.heartbeat_visible_seconds (0 = never show text, keep-alive
    bytes only)."""
    try:
        return float(getattr(cfg, "heartbeat_visible_seconds", 60) or 0)
    except (TypeError, ValueError):
        return 60.0


class Pacer:
    """Decides which heartbeat ticks show a visible status line.

    Milestones: min(30, every), then doubling up to `every`, then every
    `every` seconds — 30s, 60s, 120s, 180s … for the default 60."""

    def __init__(self, every: float = 60.0):
        self.every = float(every or 0)
        self._next = min(30.0, self.every) if self.every > 0 else math.inf

    def due(self, elapsed: float) -> bool:
        if elapsed < self._next:
            return False
        if self._next < self.every:
            self._next = min(self._next * 2, self.every)
        else:
            self._next += self.every
        while self._next <= elapsed:          # a long gap between ticks
            self._next += self.every if self.every > 0 else math.inf
        return True

    def line(self, who: str, elapsed: float, detail: str = "") -> Optional[str]:
        """status_line(...) when this tick is a visible milestone, else None
        (the caller sends an invisible keep-alive instead)."""
        return status_line(who, elapsed, detail) if self.due(elapsed) else None


class StreamStalled(Exception):
    """A backend produced no output for the stall window."""
    def __init__(self, seconds: int):
        super().__init__(f"no output for {seconds}s")
        self.seconds = seconds


def _is_output(chunk) -> bool:
    return isinstance(chunk, dict) and bool(
        chunk.get("content") or chunk.get("thinking") or chunk.get("tool_calls")
        or chunk.get("done"))


def _is_progress_only(chunk) -> bool:
    """A backend reporting generation it doesn't render yet (tool-call
    arguments being streamed): proof of life, nothing for the client."""
    return isinstance(chunk, dict) and bool(chunk.get("progress")) and not _is_output(chunk)


def progress_detail(progress: Optional[dict]) -> str:
    """Status-line detail for what the model is producing unseen."""
    if not progress:
        return ""
    n = progress.get("tool_chars")
    if n:
        size = f"{n / 1000:.1f}k" if n >= 1000 else str(n)
        tool = progress.get("tool") or "a tool call"
        return f"writing {tool} · {size} chars so far"
    if progress.get("tokens"):
        return "generating a tool call"
    return ""


async def stream_with_heartbeat(agen, hb: float, start: float, stall: float = 0,
                                progress: Optional[dict] = None):
    """Wrap an async chunk stream: yield ("chunk", c) for each real upstream
    chunk, and ("beat", elapsed_s) whenever nothing has been passed on for `hb`
    seconds — so the caller can emit a keep-alive during a silent prompt-eval /
    tool-call gap. hb <= 0 disables the beats. The pending read is shielded, so
    a beat doesn't drop the chunk that's still coming. `elapsed_s` is real wall
    time since `start`.

    Progress-only chunks (a tool call being generated — see the protocol
    adapters) are NOT passed on: they count as output for the stall watchdog,
    are recorded into the caller's `progress` dict (for the status line), and
    don't reset the beat timer — so the client still gets keep-alives while a
    long tool call streams.

    stall > 0: raise StreamStalled once no output (content / thinking / tool
    call / progress / done) has arrived for that many seconds.

    Always closes the upstream on exit — client disconnect, stall, or error —
    by cancelling the pending read and aclose()-ing the source, so an abandoned
    request stops occupying the llama.cpp slot / Claude session instead of
    running on unseen (and making the backend look busy or dead)."""
    it = agen.__aiter__()
    fut = None
    last_out = last_pass = time.monotonic()
    try:
        while True:
            fut = asyncio.ensure_future(it.__anext__())
            while True:
                now = time.monotonic()
                waits = []
                if hb and hb > 0:
                    waits.append(max(0.0, hb - (now - last_pass)))
                if stall and stall > 0:
                    remaining = stall - (now - last_out)
                    if remaining <= 0:
                        raise StreamStalled(int(now - last_out))
                    waits.append(remaining)
                wait = min(waits) if waits else None
                try:
                    if wait is not None:
                        chunk = await asyncio.wait_for(asyncio.shield(fut), wait)
                    else:
                        chunk = await fut
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if stall and stall > 0 and now - last_out >= stall:
                        raise StreamStalled(int(now - last_out))
                    if hb and hb > 0 and now - last_pass >= hb:
                        last_pass = now
                        yield "beat", int(now - start)
                    continue
                except StopAsyncIteration:
                    fut = None
                    return
                fut = None
                if _is_progress_only(chunk):
                    last_out = time.monotonic()
                    if progress is not None:
                        progress.update(chunk["progress"])
                    break                         # read on; nothing to pass
                if _is_output(chunk):
                    last_out = time.monotonic()
                    if progress is not None:
                        progress.clear()
                last_pass = time.monotonic()
                yield "chunk", chunk
                break
    finally:
        if fut is not None and not fut.done():
            fut.cancel()
            try:
                await fut
            except BaseException:                                 # noqa: BLE001
                pass
        aclose = getattr(it, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except BaseException:                                 # noqa: BLE001
                pass

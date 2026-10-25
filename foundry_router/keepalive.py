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

    Milestones: min(first, every), then doubling up to `every`, then every
    `every` seconds — 30s, 60s, 120s, 180s … for the default 60 (first=30);
    10s, 20s, 40s, 60s, 120s … with first=10 (the wait before a first token)."""

    def __init__(self, every: float = 60.0, first: float = 30.0):
        self.every = float(every or 0)
        first = float(first or 30.0)
        self._next = min(first, self.every) if self.every > 0 else math.inf

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


def first_visible(cfg) -> float:
    """agent_brain.heartbeat_first_visible_seconds: when the first visible
    'still working' line shows while a model call has produced nothing yet
    (default 10; the later milestones are unchanged)."""
    try:
        return float(getattr(cfg, "heartbeat_first_visible_seconds", 10) or 30)
    except (TypeError, ValueError):
        return 10.0


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def prefill_line(processed: int, total: int, cache: int, elapsed: float) -> str:
    """The visible 'reading the prompt' line. Starts with ⏳ so
    prompts.scrub_router_lines strips it from history a client echoes back."""
    new_total = max(1, total - cache)
    done_new = max(0, processed - cache)
    pct = min(99, 100 * done_new // new_total)
    return (f"⏳ reading prompt… {pct}% ({_k(done_new)} / {_k(new_total)} new tokens"
            + (f", {_k(cache)} cached" if cache else "")
            + f") · {fmt_elapsed(elapsed)}\n")


class PrefillReporter:
    """Visible prompt-reading progress before a model call's first token.

    The backend (llama.cpp / Strata `prompt_progress`) reports every batch —
    every ~8k tokens on Strata — so a line per frame would fill the client's
    thinking panel on a long Cline cache miss. Shown instead: the first frame,
    then only when the read crosses 25 / 50 / 75 %, at most `max_lines` per
    call — so a 30-second read and a 5-minute one both show a handful of
    lines. Prompts with fewer than `min_new_tokens` to read show nothing."""

    STEPS = (25, 50, 75)

    def __init__(self, enabled: bool = True, min_new_tokens: int = 4096, max_lines: int = 4):
        self.enabled = bool(enabled)
        self.min_new = int(min_new_tokens)
        self.max_lines = int(max_lines)
        self.reset()

    def reset(self) -> None:
        self._bucket = -1
        self._lines = 0

    def line(self, progress: Optional[dict], elapsed: float) -> Optional[str]:
        if not self.enabled or not progress or progress.get("tool_chars"):
            return None
        pf = progress.get("prefill")
        if not isinstance(pf, dict) or not pf.get("total"):
            return None
        total, cache = int(pf["total"]), int(pf.get("cache") or 0)
        processed = int(pf.get("processed") or 0)
        new_total = total - cache
        if new_total < self.min_new or processed >= total:
            return None
        pct = 100 * max(0, processed - cache) // max(1, new_total)
        bucket = sum(1 for s in self.STEPS if pct >= s)
        if bucket <= self._bucket or self._lines >= self.max_lines:
            return None
        self._bucket = bucket
        self._lines += 1
        return prefill_line(processed, total, cache, elapsed)


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
    pf = progress.get("prefill")
    if isinstance(pf, dict) and pf.get("total") and not progress.get("tool_chars"):
        total, done_, cache = pf["total"], pf.get("processed") or 0, pf.get("cache") or 0
        new_total = max(1, total - cache)
        done_new = max(0, done_ - cache)
        return (f"reading the prompt · {done_new:,} / {new_total:,} new tokens "
                f"({100 * done_new // new_total}%)"
                + (f", {cache:,} from cache" if cache else ""))
    n = progress.get("tool_chars")
    if n:
        size = f"{n / 1000:.1f}k" if n >= 1000 else str(n)
        tool = progress.get("tool") or "a tool call"
        return f"writing {tool} · {size} chars so far"
    if progress.get("tokens"):
        return "generating a tool call"
    return ""


async def stream_with_heartbeat(agen, hb: float, start: float, stall: float = 0,
                                progress: Optional[dict] = None, progress_events: bool = False):
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

    progress_events: also yield ("progress", elapsed_s) right after each
    progress-only chunk is recorded (the caller decides whether to show it;
    it does not count as a pass, so keep-alive beats keep their timing).

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
                        if progress_events:
                            yield "progress", int(time.monotonic() - start)
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

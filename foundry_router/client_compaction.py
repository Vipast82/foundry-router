"""Spot a Cline conversation whose auto-compaction is stuck.

Cline never summarizes away the latest message the user typed: its cut
always stays at or before that message (`findCutIndex` in Cline's
sdk/packages/core/src/extensions/context/compaction-shared.ts). After one
compaction the history Cline sends is

    system · "Context summary: …" · <your latest message> · tool loop …

Everything Cline is allowed to fold (the part before your message) is already
in the summary, so every later attempt returns nothing and Cline shows
"Compaction skipped" — silently, with no reason line in its log — until the
user types again. A long autonomous loop after that message keeps growing
past the window; only Foundry's context guard keeps it running.

Foundry sees the same messages, so it can tell when that state is reached and
the conversation is near the window, and say what fixes it: type /compact (or
any short message) so the old loop lands before the latest message and can
be summarized.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Optional

from . import context_guard
from .agents import conversation_key

SUMMARY_PREFIX = "Context summary:"
WARN_RATIO = 0.75      # warn from 75% of the window (Cline compacts at ~81%)
_LOGGED_MAX = 500
_logged: "OrderedDict[str, float]" = OrderedDict()


def _text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(str(p.get("text") or "") for p in c if isinstance(p, dict))
    return str(c or "")


def is_summary(m: dict) -> bool:
    return m.get("role") == "user" and _text(m).lstrip().startswith(SUMMARY_PREFIX)


def is_typed(m: dict) -> bool:
    """A message the user typed. Cline moves images returned by a tool into a
    separate user message holding only the images — that one is part of the
    tool result, not typed."""
    return m.get("role") == "user" and bool(_text(m).strip()) and not is_summary(m)


def stuck(messages: list[dict]) -> Optional[dict]:
    """{summary_index, prompt_index, steps} when Cline cannot compact any
    further: the latest typed message follows the latest summary with nothing
    foldable in between. None otherwise."""
    msgs = messages or []
    s = next((i for i in range(len(msgs) - 1, -1, -1) if is_summary(msgs[i])), -1)
    if s < 0:
        return None
    t = next((i for i in range(len(msgs) - 1, s, -1) if is_typed(msgs[i])), -1)
    if t < 0:
        return None
    if any(m.get("role") in ("assistant", "tool") for m in msgs[s + 1:t]):
        return None                      # there is something left to fold
    steps = sum(1 for m in msgs[t + 1:] if m.get("role") == "assistant")
    if steps == 0:
        return None
    return {"summary_index": s, "prompt_index": t, "steps": steps}


def warning(messages: list[dict], tools: Optional[list], window: int,
            model: str = "") -> tuple[str, Optional[dict]]:
    """(note, details) when Cline's compaction is stuck and the conversation
    has reached WARN_RATIO of the window; ('', None) otherwise."""
    if not window or window <= 0:
        return "", None
    st = stuck(messages)
    if not st:
        return "", None
    est = context_guard.estimate(messages, tools, model)
    if est < window * WARN_RATIO:
        return "", None
    note = (f"Cline can't auto-compact this task (~{est / 1000:.0f}k of "
            f"{window / 1000:.0f}k): its last summary is followed directly by your "
            f"latest message, and Cline never summarizes past your latest message, "
            f"so the {st['steps']} step(s) since then can't be folded. Type /compact "
            f"(or any short message) and Cline will compact on the next turn. Until "
            f"then Foundry's context guard keeps each request inside the window.")
    return note, {**st, "estimate": est, "window": window}


def first_report(messages: list[dict], details: dict) -> bool:
    """True once per stuck episode (conversation + latest typed message), so
    the Events log gets one entry, not one per turn."""
    try:
        conv = conversation_key(messages or [])
    except Exception:                                            # noqa: BLE001
        conv = ""
    prompt = _text((messages or [])[details["prompt_index"]])
    key = conv + ":" + hashlib.sha1(prompt.encode("utf-8", "replace")).hexdigest()[:12]
    if key in _logged:
        return False
    _logged[key] = time.monotonic()
    while len(_logged) > _LOGGED_MAX:
        _logged.popitem(last=False)
    return True

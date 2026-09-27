"""Time spent on the CLIENT side between two turns of one conversation.

A coding agent (Cline) works in a loop: Foundry streams a turn that ends in a
tool call, the client runs the tool (reads a file, runs a PowerShell command,
waits for the user to approve), then sends the next request with the result.
From the backend's point of view that client time is idle time — llama.cpp
logs `slot release` … nothing … `get_available slot`.

Per conversation Foundry remembers when it finished the previous reply and
which tool(s) that reply asked for. When the next request of the same
conversation arrives it records:

  client_gap_ms  previous reply finished -> this request arrived at Foundry
                 (tool execution + approval + client overhead);
  client_tool    the tool(s) the client was running in that gap.

Together with router_ms (arrival -> sent to the backend) and the backend's
own timings, every second of a turn is accounted for.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Optional

from . import walltime
from .agents import conversation_key

_MAX = 500
_STALE_S = 3600.0
_last: "OrderedDict[str, tuple[float, str, object]]" = OrderedDict()


def arrived(messages: list[dict]) -> dict:
    """Call when a request arrives: {client_gap_ms, client_tool} if this
    conversation had a reply from Foundry recently, else {}."""
    try:
        key = conversation_key(messages or [])
    except Exception:                                            # noqa: BLE001
        return {}
    hit = _last.get(key)
    if not hit:
        return {}
    t, tools, wall = hit
    gap = time.monotonic() - t
    if gap < 0 or gap > _STALE_S:
        return {}
    return {"client_gap_ms": round(gap * 1000.0, 1), "client_tool": tools,
            "prev_reply_at": wall,
            "client_gap_wall_ms": walltime.ms_between(wall, walltime.now())}


def finished(messages: list[dict], tool_names: Optional[list] = None) -> None:
    """Call when Foundry has sent the final chunk of a reply."""
    try:
        key = conversation_key(messages or [])
    except Exception:                                            # noqa: BLE001
        return
    names = ",".join(n for n in (tool_names or []) if n)[:120]
    _last[key] = (time.monotonic(), names, walltime.now())
    _last.move_to_end(key)
    while len(_last) > _MAX:
        _last.popitem(last=False)

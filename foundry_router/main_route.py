"""Main-model mode (v0.181.140): the order in which a persona's models take a request.

A persona in `execution_mode = "main"` has no router model in front of it. The
first model on its route (the main model) reads the request, runs the tools and
answers. This module decides which model that is for ONE request:

* **Instant overflow.** A local model with no free stream or slot right now is
  not waited for: the next model on the route that is free takes the request
  (another local model first if the route lists one, then the paid ones in route
  order). The busy local models go last, so a request only queues when nothing
  else on the route may be used.
* **A conversation stays where it landed.** The model that answered a
  conversation's last turn is its home, so the prompt cache stays warm and the
  model does not change turn by turn.
* **Back to the main model.** A conversation that overflowed returns to the
  main model only when that model is free, has been idle for
  `main_return_idle_seconds` (5), and the conversation is at most
  `main_return_max_tokens` (8,000) long: the main model has not seen it, so it
  reads all of it before the first word, and a long read is a long wait.
* **Context fit.** A model whose context window is smaller than the
  conversation is left out. A model can also carry the router's own limit
  (`route_max_tokens`, Model Registry): the largest conversation it is given,
  whatever its real window is. A small local model reads a deep prompt slowly
  (the brain host: about 35 s for 20K tokens), so it can be kept to short chats.

Nothing here calls a model.
"""

from __future__ import annotations

import time
from typing import Optional

from . import lanes

_HOME_PREFIX = "main:"
FIT_RESERVE_TOKENS = 2048          # room for the answer when checking a context window


def conv_key(persona: Optional[dict], messages: list) -> str:
    return _HOME_PREFIX + lanes.facts_for(persona, messages, False).conv_key


def home_get(key: str) -> Optional[str]:
    return lanes.pin_get(key)


def home_set(key: str, model_id: str) -> None:
    lanes.pin_set(key, model_id)


def idle_seconds(svc, model_id: str, now: Optional[float] = None) -> float:
    """Seconds since the model's backend finished its last call; 0 while a call
    is in flight; a large number when it has served nothing since the router
    started."""
    info = svc.pool.backend_info(model_id) or {}
    st = (getattr(svc.pool, "backends", None) or {}).get(info.get("name") or "")
    if st is None:
        return 0.0
    if getattr(st, "busy", 0):
        return 0.0
    last = getattr(st, "last_done", None)
    if last is None:
        return 1e9
    return max(0.0, (time.monotonic() if now is None else now) - float(last))


def fit(registry, ids: list[str], est_tokens: int) -> tuple[list[str], list[str]]:
    """Models whose context window holds the conversation. Never empties the list."""
    keep, notes = [], []
    for m in ids:
        meta = registry.get(m) or {}
        try:
            window = int(meta.get("context_length") or 0)
        except (TypeError, ValueError):
            window = 0
        try:
            cap = int(meta.get("route_max_tokens") or 0)
        except (TypeError, ValueError):
            cap = 0
        if cap and est_tokens > cap:
            notes.append(f"{m} is left out: the conversation is about {est_tokens:,} tokens "
                         f"and it is given at most {cap:,}")
            continue
        if window and est_tokens + FIT_RESERVE_TOKENS > window:
            notes.append(f"{m} is left out: the conversation is about {est_tokens:,} tokens "
                         f"and its window is {window:,}")
            continue
        keep.append(m)
    return (keep, notes) if keep else (list(ids), [])


def order(svc, ids: list[str], *, est_tokens: int, home: Optional[str],
          return_idle_seconds: float = 5.0, return_max_tokens: int = 8000,
          now: Optional[float] = None) -> tuple[list[str], list[str]]:
    """`ids` in route order (first = the main model) -> the order to try them in
    for this request, with the reasons for any change."""
    if not ids:
        return [], []
    notes: list[str] = []
    out = list(ids)
    main = out[0]

    def free(m: str) -> bool:
        return (not lanes.is_local(svc, m)) or lanes.model_free(svc, m)

    if home and home in out and home != main:
        why = ""
        if not free(main):
            why = f"{main} is busy"
        elif lanes.is_local(svc, main) and idle_seconds(svc, main, now) < return_idle_seconds:
            why = f"{main} has been idle for less than {return_idle_seconds:g} s"
        elif est_tokens > return_max_tokens:
            why = (f"the conversation is about {est_tokens:,} tokens, more than {main} "
                   f"should re-read ({return_max_tokens:,})")
        if why:
            out.remove(home)
            out.insert(0, home)
            notes.append(f"this conversation stays on {home}: {why}")
        else:
            notes.append(f"{main} is free again → this conversation moves back to it from {home}")
    busy = [m for m in out if not free(m)]
    if busy and len(busy) < len(out):
        out = [m for m in out if m not in busy] + busy
        notes.append(f"{', '.join(busy)} busy with another session → {out[0]} takes this "
                     f"request now (no waiting)")
    return out, notes

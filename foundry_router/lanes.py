"""Lanes (v0.181.118): a side backend for short jobs, try-then-divert, and
conversation pinning.

Strata serves one stream. A sprint holds it for hours (each call ends in a tool
call, so the session keeps its stream), and anything else waits. A LANE is a
second, smaller local backend (the brain host's llama.cpp, a 4060 Ti) that takes
the short, stateless jobs so they don't wait behind the sprint and the sprint's
prompt cache isn't evicted by them.

A backend becomes a lane with `lane: true` (Backends -> Pool). Then:

* Its models are left out of automatic routing (`pool.available_models()`): no
  persona drifts onto a 32k-context lane model by ranking. A persona reaches a
  lane only by naming the lane's model in its allowlist, or through divert.
* A request must fit the lane's limits to go there: at most
  `lane_max_prompt_tokens` (3,000) of prompt, no tools unless `lane_allow_tools`,
  and no earlier assistant turn unless the conversation is already pinned to the
  lane. On the lane, output is capped at `lane_max_output_tokens` (400) and
  thinking is off unless `lane_thinking`.

Try, then divert (persona `divert_when_busy`): when the local model a request
would use has no free stream or slot right now, and another local model on the
persona's route (or any lane, for a flexible persona) is free and takes the
request, it goes there instead of waiting. Nothing free: it waits in the queue
as before (queue_class.py).

Pinning: a conversation served by a lane stays on that lane while it still fits,
so its second turn doesn't bounce to Strata. With persona `pin_conversation`,
every direct-mode conversation stays on the model that answered its first turn
while that model is still on the route and allowed (prompt cache; no
mid-conversation model switch when a usage window reopens).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

PIN_TTL_S = 6 * 3600
PIN_MAX = 4000
_PINS: "OrderedDict[str, tuple[str, float]]" = OrderedDict()


@dataclass
class Facts:
    """What decides whether a request may use a lane."""
    prompt_tokens: int = 0
    has_tools: bool = False
    assistant_turns: int = 0
    conv_key: str = ""


def facts_for(persona: Optional[dict], messages: list, has_tools: bool) -> Facts:
    from . import client_auth
    from .agents import conversation_key
    from .budget import estimate_tokens
    try:
        est = int(estimate_tokens(messages))
    except Exception:                                             # noqa: BLE001
        est = 0
    session = client_auth.current().get("session_id") or ""
    key = ((persona or {}).get("virtual_name") or "") + ":" + conversation_key(messages, session)
    return Facts(prompt_tokens=est, has_tools=bool(has_tools),
                 assistant_turns=sum(1 for m in messages if m.get("role") == "assistant"),
                 conv_key=key)


# -- the pool, lanes included ------------------------------------------------------------

def all_models(pool) -> dict:
    """available_models() with lane models (other pool modes have no lanes)."""
    try:
        return pool.available_models(lanes=True)
    except TypeError:
        return pool.available_models()


def _backend_cfg(svc, model_id: str):
    info = svc.pool.backend_info(model_id) or {}
    st = (getattr(svc.pool, "backends", None) or {}).get(info.get("name") or "")
    return getattr(st, "config", None)


def lane_cfg(svc, model_id: str):
    """The model's backend config when that backend is a lane, else None."""
    c = _backend_cfg(svc, model_id)
    return c if c is not None and getattr(c, "lane", False) else None


def is_local(svc, model_id: str) -> bool:
    from .facade.ollama_api import _is_paid_backend
    info = svc.pool.backend_info(model_id)
    return bool(info) and not _is_paid_backend(info)


def eligible(cfg, facts: Facts, pinned: bool = False) -> tuple[bool, str]:
    """May this request use the lane backend `cfg`? (ok, why not)."""
    limit = int(getattr(cfg, "lane_max_prompt_tokens", 3000) or 0)
    if limit and facts.prompt_tokens > limit:
        return False, f"the prompt is about {facts.prompt_tokens:,} tokens (lane limit {limit:,})"
    if facts.has_tools and not getattr(cfg, "lane_allow_tools", False):
        return False, "the request carries tools"
    if facts.assistant_turns and not pinned:
        return False, "the conversation already has replies from another model"
    return True, ""


def fits(svc, model_id: str, facts: Facts) -> bool:
    """True unless the model sits on a lane this request doesn't fit."""
    cfg = lane_cfg(svc, model_id)
    return cfg is None or eligible(cfg, facts, pinned=(pin_get(facts.conv_key) == model_id))[0]


def backend_free(svc, name: str, model_id: str = "") -> bool:
    """True when a new session could start on the backend right now: a slot is
    open, a stream is open (or this session already holds one), nobody waits."""
    pool = svc.pool
    st = (getattr(pool, "backends", None) or {}).get(name)
    if st is None:
        return False
    cap = getattr(st.config, "max_concurrent", None)
    g = (getattr(pool, "gates", None) or {}).get(name)
    if cap and (st.busy >= int(cap) or (g is not None and (g.active >= g.capacity
                                                           or g.waiting_count()))):
        return False
    led = getattr(pool, "ledger", None)
    n = getattr(st.config, "helpers_allowed", None)
    if led is not None and n is not None:
        from .budget import context_for
        mine = context_for(model_id, st.config).key("session")
        held = led.sessions(name)
        if mine not in held:
            if len(held) >= int(n) + 1 or led._stream_live(name):
                return False
    return True


def model_free(svc, model_id: str) -> bool:
    info = svc.pool.backend_info(model_id) or {}
    return bool(info) and backend_free(svc, info.get("name") or "", model_id)


# -- pins ------------------------------------------------------------------------------------

def pin_get(key: str) -> Optional[str]:
    hit = _PINS.get(key) if key else None
    if not hit:
        return None
    if time.time() - hit[1] > PIN_TTL_S:
        _PINS.pop(key, None)
        return None
    _PINS.move_to_end(key)
    return hit[0]


def pin_set(key: str, model_id: str) -> None:
    if not key or not model_id:
        return
    _PINS[key] = (model_id, time.time())
    _PINS.move_to_end(key)
    while len(_PINS) > PIN_MAX:
        _PINS.popitem(last=False)


def remember(svc, persona: Optional[dict], facts: Facts, model_id: str) -> None:
    """After the model is chosen: pin the conversation when it is served by a
    lane, or when the persona pins every conversation."""
    if not facts.conv_key or not model_id:
        return
    if lane_cfg(svc, model_id) is not None or (persona or {}).get("pin_conversation"):
        pin_set(facts.conv_key, model_id)
    elif pin_get(facts.conv_key):
        _PINS.pop(facts.conv_key, None)       # moved off its lane: the pin is over


def _pin_applies(svc, persona, pin: Optional[str]) -> bool:
    return bool(pin) and (bool((persona or {}).get("pin_conversation"))
                          or lane_cfg(svc, pin) is not None)


# -- routing -----------------------------------------------------------------------------------

def order_chain(svc, persona: Optional[dict], cands: list[str],
                facts: Facts) -> tuple[list[str], list[str]]:
    """A strict persona's candidates, re-ordered for lanes: lane models the request
    doesn't fit are dropped; a pinned conversation's model goes first; with
    divert_when_busy a free local model goes ahead of a busy one. Paid models keep
    their places. Never returns an empty list for a non-empty chain."""
    notes: list[str] = []
    pin = pin_get(facts.conv_key)
    out: list[str] = []
    for m in cands:
        cfg = lane_cfg(svc, m)
        if cfg is not None:
            ok, why = eligible(cfg, facts, pinned=(pin == m))
            if not ok:
                notes.append(f"{m} is on the {cfg.name} lane (short jobs only) — skipped: {why}")
                continue
        out.append(m)
    if not out:
        return list(cands), []
    if pin in out and _pin_applies(svc, persona, pin):
        if out[0] != pin:
            out.remove(pin)
            out.insert(0, pin)
            notes.append(f"this conversation stays on {pin} (pinned to its first model)")
        return out, notes
    if (persona or {}).get("divert_when_busy"):
        local = [m for m in out if is_local(svc, m)]
        if len(local) > 1 and not model_free(svc, local[0]):
            alt = next((m for m in local[1:] if model_free(svc, m)), None)
            if alt:
                out.remove(alt)
                out.insert(out.index(local[0]), alt)
                notes.append(f"{local[0]} is busy → {alt} is free and takes this request "
                             f"(divert when busy)")
    return out, notes


def divert(svc, persona: Optional[dict], model_id: str, facts: Facts) -> tuple[str, str]:
    """A flexible persona's pick, re-checked: (model, note). The pinned model when
    the conversation has one; else, with divert_when_busy and a busy local pick, a
    free local model on the persona's allowlist or a free lane."""
    models = all_models(svc.pool)
    pin = pin_get(facts.conv_key)
    if pin and pin != model_id and pin in models and _pin_applies(svc, persona, pin):
        cfg = lane_cfg(svc, pin)
        if cfg is None or eligible(cfg, facts, pinned=True)[0]:
            return pin, f"this conversation stays on {pin} (pinned to its first model)"
    if not (persona or {}).get("divert_when_busy"):
        return model_id, ""
    if not is_local(svc, model_id) or model_free(svc, model_id):
        return model_id, ""
    from .facade.ollama_api import _jl_list
    allow = [str(a) for a in _jl_list((persona or {}).get("model_allowlist"))]
    listed = [m for m in models if m in allow and m != model_id and is_local(svc, m)]
    lanes_ = [m for m in models if m not in listed and m != model_id
              and lane_cfg(svc, m) is not None]
    for m in listed + lanes_:
        meta = svc.registry.get(m) or {}
        if meta.get("embedding"):
            continue
        cfg = lane_cfg(svc, m)
        if cfg is not None and not eligible(cfg, facts, pinned=(pin == m))[0]:
            continue
        if model_free(svc, m):
            return m, (f"local {model_id} is busy → {m} is free and takes this request "
                       f"(divert when busy)")
    return model_id, ""


# -- limits on the lane ---------------------------------------------------------------------------

def output_cap(svc, model_id: str, cap: int) -> int:
    cfg = lane_cfg(svc, model_id)
    limit = int(getattr(cfg, "lane_max_output_tokens", 0) or 0) if cfg is not None else 0
    return min(cap, limit) if limit and cap else (limit or cap)


def thinking_off(svc, model_id: str) -> bool:
    cfg = lane_cfg(svc, model_id)
    return cfg is not None and not getattr(cfg, "lane_thinking", False)


def describe(svc) -> list[dict]:
    """Lane backends and whether each is free right now (UI)."""
    out = []
    for name, st in (getattr(svc.pool, "backends", None) or {}).items():
        c = st.config
        if getattr(c, "lane", False):
            out.append({"backend": name, "models": list(st.models), "healthy": bool(st.healthy),
                        "free": backend_free(svc, name),
                        "max_prompt_tokens": getattr(c, "lane_max_prompt_tokens", 3000),
                        "max_output_tokens": getattr(c, "lane_max_output_tokens", 400),
                        "allow_tools": bool(getattr(c, "lane_allow_tools", False)),
                        "thinking": bool(getattr(c, "lane_thinking", False))})
    return out

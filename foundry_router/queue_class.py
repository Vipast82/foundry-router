"""Queue classes (v0.181.118): who goes first when a backend is full.

Strata serves one request at a time, so chats, sprints, helper lanes and
background jobs all wait for the same slot. Until now the wait was first come,
first served (FairGate took turns between clients; the stream-session wait in
budget.py was an unordered poll). A class puts the waiters in order:

  A  interactive       a person is waiting (chat front ends, Cline, operator chat)
  B  required          coordinator work a sprint can't continue without
  C  optional          coordinator helper lanes, exploration, research, pre-work
  D  background        scheduled pulses, indexing summaries, anything unattended

A waiter's class comes from, in order:

1. the coordinator purpose bound to its session (orchestration.py):
   a required purpose is B, an optional one is C;
2. the persona's `queue_class`;
3. the client's `queue_class` (Access -> Clients and keys);
4. `queue.default_class` (A: unknown traffic is treated as a person).

A request may also send `x-foundry-priority: A|B|C|D` (or interactive /
required / optional / background). It can only LOWER its own class, so a
script can mark itself background but nothing can jump the line.

Aging: a waiter moves up one class every `promote_after_seconds` of its class
(C: 10 minutes, D: 15 minutes), never above B, so background work is delayed
but not starved and a person still goes first.

Wait limit: the backend's `queue_timeout_seconds` times the class's
`wait_factor` (A 1, B 2, C 3, D 6: 300 / 600 / 900 / 1800 s on a 300 s backend).
"""

from __future__ import annotations

from typing import Callable, Optional

CLASSES = ("A", "B", "C", "D")
RANK = {c: i for i, c in enumerate(CLASSES)}
LABELS = {"A": "interactive", "B": "required", "C": "optional", "D": "background"}
ALIASES = {"interactive": "A", "human": "A", "chat": "A",
           "required": "B", "coordinator": "B",
           "optional": "C", "helper": "C",
           "background": "D", "batch": "D", "low": "D"}
PRIORITY_HEADER = "x-foundry-priority"
# aging never lifts a waiter above this class: a person (A) always goes first
PROMOTE_CEILING = "B"

DEFAULTS = {"A": {"wait_factor": 1.0, "promote_after_seconds": 0.0},
            "B": {"wait_factor": 2.0, "promote_after_seconds": 0.0},
            "C": {"wait_factor": 3.0, "promote_after_seconds": 600.0},
            "D": {"wait_factor": 6.0, "promote_after_seconds": 900.0}}

_config_fn: Optional[Callable] = None


def configure(fn: Optional[Callable]) -> None:
    """Services sets the live `queue` config getter; None = the defaults."""
    global _config_fn
    _config_fn = fn


def _cfg():
    try:
        return _config_fn() if _config_fn else None
    except Exception:                                             # noqa: BLE001
        return None


def enabled(cfg=None) -> bool:
    cfg = cfg if cfg is not None else _cfg()
    return bool(getattr(cfg, "enabled", True))


def normalize(v) -> Optional[str]:
    """'a', 'A', 'interactive' -> 'A'; anything unknown -> None."""
    s = str(v or "").strip()
    if not s:
        return None
    if s.upper() in RANK:
        return s.upper()
    return ALIASES.get(s.lower())


def _class_setting(cls: str, key: str, cfg=None) -> float:
    cfg = cfg if cfg is not None else _cfg()
    row = (getattr(cfg, "classes", None) or {}).get(cls)
    v = getattr(row, key, None) if row is not None else None
    if v is None and isinstance(row, dict):
        v = row.get(key)
    return float(DEFAULTS[cls][key] if v is None else v)


def classify(client: Optional[dict] = None, persona: Optional[dict] = None, cfg=None) -> str:
    """The queue class of a request (see the module docstring for the order)."""
    cfg = cfg if cfg is not None else _cfg()
    client = client or {}
    cls = None
    purpose = str(client.get("purpose") or "")
    if purpose and purpose != "unverified":
        from .orchestration import OPTIONAL_PURPOSES, REQUIRED_PURPOSES
        if purpose in REQUIRED_PURPOSES:
            cls = "B"
        elif purpose in OPTIONAL_PURPOSES:
            cls = "C"
    cls = (cls or normalize((persona or {}).get("queue_class"))
           or normalize(client.get("queue_class"))
           or normalize(getattr(cfg, "default_class", None)) or "A")
    asked = normalize(client.get("priority"))
    if asked and RANK[asked] > RANK[cls]:          # a request may only lower itself
        cls = asked
    return cls


def current() -> str:
    """This request's class: what dispatch recorded (persona known), else from
    the client alone (pool calls made outside a chat dispatch)."""
    from . import client_auth, request_context
    noted = normalize(request_context.route().get("queue_class"))
    return noted or classify(client_auth.current())


def effective(cls: str, waited_s: float, cfg=None) -> str:
    """The class a waiter counts as after `waited_s` in the queue (aging)."""
    cls = normalize(cls) or "A"
    cfg = cfg if cfg is not None else _cfg()
    ceiling = RANK[PROMOTE_CEILING]
    rank, left = RANK[cls], max(0.0, float(waited_s or 0))
    while rank > ceiling:
        step = _class_setting(CLASSES[rank], "promote_after_seconds", cfg)
        if step <= 0 or left < step:
            break
        left -= step
        rank -= 1
    return CLASSES[rank]


def rank(cls: str, waited_s: float = 0.0, cfg=None) -> int:
    return RANK[effective(cls, waited_s, cfg)]


def wait_seconds(cls: str, backend_timeout: float, cfg=None) -> float:
    """How long a waiter of this class waits for a slot on a backend."""
    base = float(backend_timeout or 0)
    if not enabled(cfg):
        return base
    return base * max(0.1, _class_setting(normalize(cls) or "A", "wait_factor", cfg))


def recovery_hold(cls: str, cfg=None) -> float:
    """Seconds a strict request of this class is held for a backend that just went
    down (ollama_api._await_recovery). A person gets the shorter interactive time."""
    cfg = cfg if cfg is not None else _cfg()
    full = float(getattr(cfg, "recovery_hold_seconds", 90.0) or 0)
    if (normalize(cls) or "A") != "A":
        return full
    return min(full, float(getattr(cfg, "recovery_hold_interactive_seconds", 20.0) or 0))


def describe(cfg=None) -> dict:
    """Settings as the UI shows them."""
    cfg = cfg if cfg is not None else _cfg()
    return {"enabled": enabled(cfg),
            "default_class": normalize(getattr(cfg, "default_class", None)) or "A",
            "recovery_hold_seconds": recovery_hold("B", cfg),
            "recovery_hold_interactive_seconds": recovery_hold("A", cfg),
            "classes": [{"class": c, "label": LABELS[c],
                         "wait_factor": _class_setting(c, "wait_factor", cfg),
                         "promote_after_seconds": _class_setting(c, "promote_after_seconds", cfg)}
                        for c in CLASSES]}

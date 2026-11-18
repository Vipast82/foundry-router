"""The two prompt additions that come out of work this router has already done:
the rules an operator approved from repeated mistakes (lessons.py) and the
earlier work retrieved for this request (recall.py).

Both were written inside the brain's worker tool loop, which is the one place
they could not reach the requests this router actually serves. A strict persona
never involves the brain, and in passthrough routing mode nothing does, so every
real Kilo request is answered by the facade's direct dispatch - which assembles
its own prompt and never ran that code. The feature could be switched on, rules
could be approved, a persona's toggle could be set, and the prompt never changed.
Nothing failed, because there was nothing to fail.

So the code lives here instead of in either caller, and both call it: the agent
for the brain-mediated paths, the facade for direct dispatch. A prompt addition
belongs to the prompt, not to one of the ways of building one (v0.181.184).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

__all__ = ["inject", "insert_system"]


def insert_system(messages: list, body: str) -> None:
    """Put `body` in as a system turn after the last system turn there is, so it
    sits with the persona's own instructions rather than ahead of them, and
    ahead of the conversation rather than buried in it."""
    at = 0
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            at = i + 1
    messages.insert(at, {"role": "system", "content": body})


def _asked(messages: list) -> str:
    """The newest thing the user actually said, which is what precedent is
    searched for. An empty or whitespace turn is not a question."""
    return next((m.get("content") or "" for m in reversed(messages)
                 if m.get("role") == "user" and (m.get("content") or "").strip()), "")


async def inject(svc: Any, persona: Optional[dict], messages: list,
                 cfg_for: Callable[[str], Any],
                 logger: Any = None) -> list[str]:
    """Add the approved rules and this request's precedent to `messages`, which
    is edited in place. `cfg_for` returns one section of the router's config by
    name, because the agent reaches its config through the guardrail engine and
    the facade holds it directly.

    Returns one plain-language note per block added, for whoever shows route
    notes; nothing is added for a persona that has not opted in, and a lookup
    only happens when recall is on for it, so a persona with both off pays two
    attribute reads.
    """
    notes: list[str] = []
    if svc is None or not persona:
        return notes
    from . import lessons, recall

    lcfg = cfg_for("lessons")
    if lessons.on_for(lcfg, persona):
        rules, dropped = lessons.rulebook(
            svc.db, int(getattr(lcfg, "max_chars", 1500) or 1500))
        if rules:
            insert_system(messages, rules)
            # the block numbers its rules, so count those rather than its lines
            n = len(re.findall(r"(?m)^\s*\d+\.", rules)) or rules.count("\n")
            note = f"lessons: {n} approved rule(s) in the prompt"
            if dropped:
                note += (f", {len(dropped)} did not fit (raise lessons.max_chars): "
                         + ", ".join(str(i) for i in dropped[:8]))
            notes.append(note)

    rcfg = cfg_for("recall")
    if recall.on_for(rcfg, persona):
        asked = _asked(messages)
        hits = await recall.search(svc, asked) if asked else []
        if hits:
            insert_system(messages, recall.note(
                hits, int(getattr(rcfg, "max_chars", 2000) or 2000)))
            notes.append("recall: " + ", ".join(
                f"{h['case_id']} ({h['similarity']:.0%})" for h in hits))

    if logger is not None:
        for n in notes:
            try:
                logger.record_guardrail(n)
            except Exception:                                     # noqa: BLE001
                pass                      # a note is never worth failing a request
    return notes

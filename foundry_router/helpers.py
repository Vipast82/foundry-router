"""Helpers (v0.181.143): models a main-model persona may ask for help.

A persona in main-model mode has one model doing the whole request. `helpers` is
the list of other models it may call on, kept INSIDE the persona (no extra
persona appears in a chat program's model list). Each row:

    role          the name the main model uses ("fast", "deep", "vision", ...)
    what          one line the main model reads: what this helper is good for
    models        model ids tried in order (any backend, local or paid)
    instruction   optional system prompt for the helper
    tools         true = the helper may use the persona's own tools (its own
                  tool loop: "research this and report back")
    paid          false = never use a paid model for this row (default true)
    max_calls     calls per request (default 3)
    digest_over   characters; a tool result longer than this is condensed by
                  this helper before the main model reads it (0 / unset = off)

The main model gets ONE extra tool, `ask_helper(helper, task, context)`. The
helper does not see the conversation: the task text is all it gets. Roles, not
model names, are what the main model sees, so a model can be swapped without
touching a prompt. Every paid helper call passes the same guardrail as any paid
call. A helper on the backend the main model is running on is skipped when that
backend serves one request at a time: it would take the main model's slot and
its prompt cache.
"""

from __future__ import annotations

import json
import re
from typing import Optional

SERVER = "foundry-helpers"          # pseudo MCP server, never synced
TOOL = "ask_helper"
DEFAULT_MAX_CALLS = 3
MAX_ROWS = 12
_ROLE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def clean(value) -> tuple[list, list]:
    """(rows, errors) from what the admin page or the API sent."""
    if value in (None, "", {}):
        return [], []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [], ["helpers must be a list"]
    if not isinstance(value, list):
        return [], ["helpers must be a list"]
    rows, errors, seen = [], [], set()
    for i, r in enumerate(value[:MAX_ROWS]):
        if not isinstance(r, dict):
            errors.append(f"helpers[{i}] must be an object")
            continue
        role = str(r.get("role") or "").strip().lower()
        if not _ROLE.match(role):
            errors.append(f"helpers[{i}]: role must be a short name (letters, digits, - or _), "
                          f"got {r.get('role')!r}")
            continue
        if role in seen:
            errors.append(f"helpers: the role {role!r} is listed twice")
            continue
        seen.add(role)
        models = r.get("models") or []
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",")]
        models = [str(m).strip() for m in models if str(m).strip()]
        if not models:
            errors.append(f"helpers[{role}]: pick at least one model")
            continue
        row = {"role": role, "models": list(dict.fromkeys(models)),
               "what": str(r.get("what") or "").strip()[:300]}
        if str(r.get("instruction") or "").strip():
            row["instruction"] = str(r["instruction"]).strip()[:8000]
        if r.get("tools"):
            row["tools"] = True
        if r.get("paid") is False:
            row["paid"] = False
        for k, lo, hi in (("max_calls", 1, 50), ("digest_over", 0, 5_000_000)):
            v = r.get(k)
            if v in (None, ""):
                continue
            try:
                n = int(v)
            except (TypeError, ValueError):
                errors.append(f"helpers[{role}]: {k} must be a whole number")
                continue
            if not lo <= n <= hi:
                errors.append(f"helpers[{role}]: {k} must be between {lo} and {hi:,}")
                continue
            if n:
                row[k] = n
        rows.append(row)
    return rows, errors


def parse(persona: Optional[dict]) -> list:
    """The persona's helper rows (never raises; a bad value is no helpers)."""
    rows, _ = clean((persona or {}).get("helpers"))
    return rows


def find(rows: list, role: str) -> Optional[dict]:
    role = str(role or "").strip().lower()
    return next((r for r in rows if r["role"] == role), None)


def digest_row(rows: list) -> Optional[dict]:
    """The helper that condenses long tool results, if one is set up for it."""
    return next((r for r in rows if r.get("digest_over")), None)


def max_calls(row: dict) -> int:
    return int(row.get("max_calls") or DEFAULT_MAX_CALLS)


def describe(rows: list) -> str:
    lines = ["Ask another model for help with part of the task and get its answer back. "
             "Use it when a part of the task suits a helper better than you, or when the user "
             "asks for a particular model. The helper does NOT see this conversation: put "
             "everything it needs into `task` (and long material into `context`). Helpers:"]
    for r in rows:
        what = r.get("what") or "general help"
        lines.append(f"- {r['role']}: {what} (models: {', '.join(r['models'])}"
                     + ("; can use your tools" if r.get("tools") else "") + ")")
    return "\n".join(lines)


def tooldef(persona: Optional[dict]):
    """The one tool the main model is given, or None without helpers."""
    rows = [r for r in parse(persona)]
    if not rows:
        return None
    from .tools.sync import ToolDef
    return ToolDef(
        name=TOOL, kind="mcp", description=describe(rows), server=SERVER, mcp_tool=TOOL,
        parameters={"type": "object", "properties": {
            "helper": {"type": "string", "enum": [r["role"] for r in rows],
                       "description": "Which helper to ask."},
            "task": {"type": "string",
                     "description": "What you want from it, complete and self-contained."},
            "context": {"type": "string",
                        "description": "Optional material it needs (text, data, code)."}},
            "required": ["helper", "task"]})


HELPER_SYSTEM = ("You are helping another AI model with one part of a larger task. Do exactly "
                 "what is asked and reply with the result only: no greeting, no questions back. "
                 "If you cannot do it, say so in one line and why.")

DIGEST_SYSTEM = ("You condense a long tool result for another AI model that is answering a "
                 "user. Keep everything relevant to the user's request: exact figures, names, "
                 "dates, versions, quotes and links, with enough surrounding text to use them. "
                 "Drop navigation, boilerplate and anything unrelated. Do not answer the "
                 "request yourself and do not add anything that is not in the text.")


def digest_prompt(question: str, tool: str, text: str) -> str:
    return (f"The user's request:\n{question[:4000]}\n\nResult of the tool `{tool}` "
            f"({len(text):,} characters) to condense:\n\n{text}")

"""Reasoning-effort ("thinking") control — the one place that knows the effort
levels, which models support which of them, and how each backend expresses the
request.

Two backends want two different shapes:
  * Ollama takes a top-level `think` field — a bool, or a level string
    ("low"/"medium"/"high"/…) on runners that implement reasoning *effort* that
    way (the gpt-oss lineage). Most reasoning locals (Qwen3, DeepSeek-R1) treat
    `think` as on/off only.
  * Anthropic/Claude takes a `thinking: {type:"enabled", budget_tokens:N}`
    block, and the budget counts toward — and must stay below — max_tokens.

A single operator/persona/client-chosen effort is mapped onto whichever the
target backend wants. No Ollama or Anthropic endpoint *enumerates* the valid
level set for a model, so `supported_levels` is a curated family lookup — the
honest ceiling on "only show what's available".
"""

from __future__ import annotations

import json
import re
from typing import Optional

# Canonical effort levels, weakest -> strongest. "off"/"on" are the boolean
# poles; the graded levels match Ollama's accepted `think` strings
# (low/medium/high/max) — NOT "xhigh", which Ollama rejects. "xhigh" from older
# configs is aliased to "max" in normalize().
LEVELS = ["off", "low", "medium", "high", "max"]

_ON = {"on", "true", "yes", "1", "enabled"}
_OFF = {"off", "false", "none", "no", "0", "disabled"}

# Conservative Claude extended-thinking budgets (tokens). Anthropic requires
# budget_tokens >= 1024 AND < max_tokens; the caller's output room is added on
# top of the budget so both hold.
CLAUDE_BUDGETS = {"low": 2048, "medium": 8192, "high": 16384, "max": 32768}

# Curated per-family level menus. NOTHING in the Ollama/Anthropic APIs reports
# the valid set, so this is a maintained table keyed by model-name pattern;
# first match wins. Update it as new reasoning families land. (Qwen3/DeepSeek-R1
# builds served by current Ollama accept the graded low/medium/high/max, not
# just on/off.)
_FAMILY_MENUS: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"gpt-?oss", re.I), ["off", "low", "medium", "high"]),
    (re.compile(r"qwen3|qwq|deepseek-?r1|magistral", re.I),
     ["off", "low", "medium", "high", "max"]),
    (re.compile(r"claude|sonnet|opus|haiku", re.I),
     ["off", "low", "medium", "high", "max"]),
]


def _as_list(caps) -> list:
    if isinstance(caps, list):
        return caps
    if isinstance(caps, str):
        try:
            v = json.loads(caps)
            return v if isinstance(v, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def normalize(effort) -> Optional[object]:
    """Canonicalize a raw effort value from config/persona/client. Returns:
      None  -> leave the model's default (don't send `think` at all)
      False -> thinking OFF
      True  -> thinking ON, no specific level
      str   -> a level ("low"/"medium"/"high"/"max")."""
    if effort is None:
        return None
    if isinstance(effort, bool):
        return effort
    s = str(effort).strip().lower()
    if not s:
        return None
    if s in _OFF:
        return False
    if s in _ON:
        return True
    if s == "xhigh":          # old label; Ollama's top graded level is "max"
        return "max"
    return s


def supported_levels(model_id: str, caps=None, backend_type: str = "") -> list[str]:
    """Curated menu of effort levels meaningful for this model. Empty = the
    model doesn't support thinking at all.

    Anthropic-compatible models (Claude via Meridian) always get the full
    budgeted menu — we express thinking for them ourselves. Ollama models are
    gated on the discovered `thinking` capability, then shaped by the family
    lookup (a thinking-capable but unrecognized family gets on/off only)."""
    name = model_id or ""
    if backend_type == "anthropic-compatible":
        return ["off", "low", "medium", "high", "max"]
    if "thinking" not in _as_list(caps):
        return []
    for pat, menu in _FAMILY_MENUS:
        if pat.search(name):
            return menu
    return ["off", "on"]


def supports_thinking(model_id: str, caps=None, backend_type: str = "") -> bool:
    return bool(supported_levels(model_id, caps, backend_type))


def think_value(effort, model_id: str, caps=None, backend_type: str = ""):
    """The value to hand a pool/protocol `think=` argument: a normalized effort
    (bool or level string) if the model supports thinking, else None. Callers
    pass whatever they resolved (client > persona > global) as `effort`."""
    norm = normalize(effort)
    if norm is None:
        return None
    if not supports_thinking(model_id, caps, backend_type):
        return None
    return norm


def claude_thinking(think, max_tokens: int):
    """Translate a normalized `think` into an Anthropic thinking block plus the
    max_tokens that must accompany it (budget < max_tokens, and thinking tokens
    count toward max_tokens). Returns (block, max_tokens) or None when thinking
    should not be sent (off / unset / a level with no budget mapping).

    A bare True (thinking on, no level) maps to the conservative 'medium'."""
    if think is None or think is False:
        return None
    level = "medium" if think is True else str(think).strip().lower()
    budget = CLAUDE_BUDGETS.get(level)
    if budget is None:
        return None
    max_tokens = max(int(max_tokens or 0), 1024) + budget   # keep output room on top
    return {"type": "enabled", "budget_tokens": budget}, max_tokens

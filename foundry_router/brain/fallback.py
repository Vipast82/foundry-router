"""Brain-unreachable static fallback (design doc §4.2).

Every request depends on the brain, so this is the one place where falling
back to something dumber beats failing: a minimal static rule — keyword/length
heuristics, zero model calls — picks a conservative default backend directly
and forwards the conversation. Exercised whenever BrainUnreachable is raised;
tested by literally stopping the brain's Ollama instance (build step 3).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..registry.models_db import ModelRegistry

log = logging.getLogger(__name__)

_CODE_HINTS = re.compile(
    r"```|\bdef \w+|\bclass \w+|\bfunction\b|\bimport \w+|Traceback|"
    r"\berror\b.*\bline \d+|\.py\b|\.js\b|\.ts\b|\brefactor\b|\bcompile\b",
    re.IGNORECASE)


def guess_category(text: str) -> str:
    """Keyword/length heuristic — deliberately dumb, deliberately model-free."""
    if _CODE_HINTS.search(text or ""):
        return "coding"
    if len(text or "") > 4000:
        return "reasoning"   # long pasted material tends to want analysis
    return "general_chat"


def pick_fallback_model(pool, registry: ModelRegistry,
                        persona: Optional[dict], user_text: str,
                        allow_paid_first: bool = False) -> Optional[str]:
    """Policy-aware single-model pick (used by direct-dispatch for coding clients
    like Cline, and by the blind brain-down fallback).

    Honors the persona's `model_allowlist` (hard candidate restriction) and — when
    `allow_paid_first` is set AND the persona's bias is `prefer_paid` (e.g. a Cline
    PLAN persona) — starts in the PAID tier. Otherwise local-first (free, offline,
    §2 no-cloud-dependency). `allow_paid_first` is passed ONLY by callers that run
    the usage guardrail afterward (direct-dispatch), so the blind brain-down path
    can't bypass conservation by reaching for paid."""
    import json as _json

    def _jl(v) -> list:
        try:
            out = _json.loads(v or "[]")
            return out if isinstance(out, list) else []
        except (_json.JSONDecodeError, TypeError):
            return []

    category = (persona or {}).get("benchmark_category") or guess_category(user_text)
    available = pool.available_models()
    if not available:
        return None

    # model_allowlist: hard-restrict candidates (base-name tolerant). Empty =
    # all. If nothing in the allowlist is reachable, fall through to the full set
    # (degrade with options, same philosophy as the ranking allowlist filter).
    allow = _jl((persona or {}).get("model_allowlist"))
    if allow:
        allowset = set(allow) | {str(a).split(":")[0] for a in allow}
        picked = [m for m in available
                  if m in allowset or str(m).split(":")[0] in allowset]
        if picked:
            available = picked

    # Local pins are honored here too (LOCAL only — a paid pin would dodge the
    # guardrails on the blind path).
    for p in _jl((persona or {}).get("pinned_models")):
        if p in available and (pool.backend_info(p) or {}).get("type") == "ollama":
            return p

    local, remote = [], []
    for model_id in available:
        # Embedding-only models can't serve /api/chat — skip them even in the
        # blind fallback, so the last-resort group[0] never picks one.
        meta = registry.get(model_id)
        if meta and meta.get("embedding"):
            continue
        info = pool.backend_info(model_id) or {}
        (local if info.get("type") == "ollama" else remote).append(model_id)

    paid_first = (allow_paid_first
                  and (persona or {}).get("local_bias_strength") == "prefer_paid")
    for group in ((remote, local) if paid_first else (local, remote)):
        if not group:
            continue
        ranked = registry.ranked_for_category(category, group, limit=1)
        if ranked:
            return ranked[0]["id"]
        return group[0]
    return None

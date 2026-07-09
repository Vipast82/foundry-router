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
                        persona: Optional[dict], user_text: str) -> Optional[str]:
    """Conservative default = best-ranked *local* model for the task category
    (free and always available offline, honoring §2's no-cloud-dependency);
    a paid backend is used only if literally nothing local is reachable."""
    category = (persona or {}).get("benchmark_category") or guess_category(user_text)
    available = pool.available_models()
    if not available:
        return None

    local, remote = [], []
    for model_id in available:
        info = pool.backend_info(model_id) or {}
        (local if info.get("type") == "ollama" else remote).append(model_id)

    for group in (local, remote):
        if not group:
            continue
        ranked = registry.ranked_for_category(category, group, limit=1)
        if ranked:
            return ranked[0]["id"]
        return group[0]
    return None

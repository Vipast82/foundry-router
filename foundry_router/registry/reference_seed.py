"""Applies the reference research seed (reference_data.py) to the registry.

Never-clobber semantics, in order of authority:
  manual_override  >  research_agent  >  reference seed  >  bare discovery/poll

- rows whose source is manual_override or research_agent are left alone
  (real data beats knowledge-based estimates);
- benchmark rows are only written for (model, category) pairs that have NO
  existing row of any kind;
- idempotent: re-applying is a no-op for already-seeded models.

Runs automatically at startup and after each OpenRouter poll cycle, so newly
ingested models get seeded on arrival; also triggerable from the UI.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .models_db import ModelRegistry
from .reference_data import REFERENCE_SEED

log = logging.getLogger(__name__)

SEED_SOURCE_URL = "reference-seed:fable-5-2026-07"


def best_match(model_id: str) -> Optional[dict]:
    """Longest matching substring across all entries wins — so a specific
    entry ('qwen3-coder') beats its family entry ('qwen')."""
    mid = model_id.lower()
    best, best_len = None, 0
    for entry in REFERENCE_SEED:
        for key in entry["match"]:
            if key in mid and len(key) > best_len:
                best, best_len = entry, len(key)
    return best


def apply_seed_to_model(registry: ModelRegistry, model_id: str) -> int:
    """Apply the reference-seed benchmark defaults to ONE model, bypassing the
    whole-registry source guard (used by the explicit operator 'reset scores'
    action after clearing corrupted rows). Only fills categories with no
    existing row, so a manual_override is still respected. Returns rows written."""
    entry = best_match(model_id)
    if entry is None:
        return 0
    existing = {b["category"] for b in registry.benchmarks(model_id)}
    wrote = 0
    for category, score in (entry.get("scores") or {}).items():
        if category in existing:
            continue
        registry.upsert_benchmark(
            model_id, category, float(score),
            score_type="estimated", source_type="community_report",
            source_url=SEED_SOURCE_URL, confidence=float(entry.get("confidence", 0.5)))
        wrote += 1
    return wrote


def apply_reference_seed(registry: ModelRegistry) -> int:
    """Seed every matching registry row that real data hasn't already covered.
    Returns the number of models touched."""
    applied = 0
    for row in registry.list_models():
        if row.get("source") in ("manual_override", "research_agent"):
            continue  # real data wins over estimates
        if row.get("source") == "reference_seed" and row.get("good_for"):
            continue  # already seeded (idempotence)
        entry = best_match(row["id"])
        if entry is None:
            continue

        registry.upsert_auto(
            row["id"], source="reference_seed",
            tags=json.dumps(entry["tags"]) if entry.get("tags") else None,
            good_for=entry.get("good_for"),
            reasoning_style=entry.get("reasoning_style"),
            # Only entries that specify a tier carry one (Claude tiers today);
            # None is filtered by upsert_auto, so a discovery-set tier (e.g.
            # ollama "free", OpenRouter pricing tier) is preserved for the rest.
            relative_cost_tier=entry.get("relative_cost_tier"),
            benefits_from_explicit_prompting=1 if entry.get("refine") else 0,
        )
        existing_categories = {b["category"] for b in registry.benchmarks(row["id"])}
        for category, score in (entry.get("scores") or {}).items():
            if category in existing_categories:
                continue  # any real (or prior) benchmark row wins
            registry.upsert_benchmark(
                row["id"], category, float(score),
                score_type="estimated", source_type="community_report",
                source_url=SEED_SOURCE_URL,
                confidence=float(entry.get("confidence", 0.5)))
        applied += 1
    if applied:
        log.info("reference seed applied to %d models", applied)
    return applied

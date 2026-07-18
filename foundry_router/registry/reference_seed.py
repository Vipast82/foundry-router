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


def _seed_benchmarks(registry: ModelRegistry, model_id: str, entry: dict) -> None:
    existing = {b["category"] for b in registry.benchmarks(model_id)}
    for category, score in (entry.get("scores") or {}).items():
        if category in existing:
            continue  # any real (or prior) benchmark row wins
        registry.upsert_benchmark(
            model_id, category, float(score), score_type="estimated",
            source_type="community_report", source_url=SEED_SOURCE_URL,
            confidence=float(entry.get("confidence", 0.5)))


def apply_reference_seed(registry: ModelRegistry) -> int:
    """Seed every matching registry row that real data hasn't already covered.
    Returns the number of models touched."""
    applied = 0
    for row in registry.list_models():
        src = row.get("source")
        if src == "manual_override":
            continue  # hand-set data wins entirely
        entry = best_match(row["id"])
        if entry is None:
            continue

        if src == "research_agent":
            # Research owns this row, but for an obscure or alias model name
            # (e.g. "claude-fable-5", "ornith:35b") a web-research pass commonly
            # finds nothing and leaves good_for/reasoning_style blank. SUPPLEMENT
            # those gaps from the curated seed — fill NULL fields only, never
            # overwrite a value research actually produced. (This is why the real
            # Claude tiers show a good_for even though researching their alias
            # turns up nothing.) source stays research_agent (upsert is monotonic).
            supp: dict = {}
            if not row.get("good_for") and entry.get("good_for"):
                supp["good_for"] = entry["good_for"]
            if not row.get("reasoning_style") and entry.get("reasoning_style"):
                supp["reasoning_style"] = entry["reasoning_style"]
            if not row.get("tags") and entry.get("tags"):
                supp["tags"] = json.dumps(entry["tags"])
            if supp:
                registry.upsert_auto(row["id"], source="reference_seed", **supp)
                applied += 1
            _seed_benchmarks(registry, row["id"], entry)
            continue

        if src == "reference_seed" and row.get("good_for"):
            _seed_benchmarks(registry, row["id"], entry)  # top up any new categories
            continue

        # discovery / not-yet-seeded row: full apply.
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
        _seed_benchmarks(registry, row["id"], entry)
        applied += 1
    if applied:
        log.info("reference seed applied to %d models", applied)
    return applied

"""Structured registry population from OpenRouter's public models endpoint
(design doc §4.4, population source 1). Pricing, context length, provider
metadata for hundreds of models in one JSON call — no key required for the
listing endpoint. Polled on a schedule (daily by default) and upserted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from ..db import Database, utcnow
from .models_db import ModelRegistry

log = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
KV_LAST_POLL = "openrouter_last_poll"


def cost_tier(cost_per_1k_output: Optional[float]) -> Optional[str]:
    """Rough dollar-cost banding used for relative_cost_tier when nothing
    better is known. Thresholds are per-1k-output-token USD; manually
    overridable per model via the web UI. "free" is the tier local models get
    at discovery — cost is a fact about the backend, not a benchmark guess."""
    if cost_per_1k_output is None:
        return None
    if cost_per_1k_output == 0:
        return "free"
    if cost_per_1k_output <= 0.001:
        return "low"
    if cost_per_1k_output <= 0.01:
        return "medium"
    if cost_per_1k_output <= 0.05:
        return "high"
    return "very_high"


async def poll_openrouter(db: Database, registry: ModelRegistry,
                          client: httpx.AsyncClient, force: bool = False,
                          poll_hours: int = 24) -> int:
    """Fetch + upsert. Returns number of models ingested (0 if skipped or
    unreachable — no cloud dependency for core function, §2: failure here is
    logged and life goes on with whatever the registry already holds)."""
    last = db.kv_get(KV_LAST_POLL)
    if last and not force:
        try:
            last_dt = datetime.fromisoformat(last)
            if datetime.now(timezone.utc) - last_dt < timedelta(hours=poll_hours):
                return 0
        except ValueError:
            pass
    try:
        r = await client.get(OPENROUTER_MODELS_URL, timeout=30)
        r.raise_for_status()
        items = r.json().get("data", [])
    except Exception as e:
        db.log_event("warning", "registry",
                     "OpenRouter metadata poll failed (continuing with cached registry)",
                     str(e))
        return 0

    count = 0
    for m in items:
        try:
            model_id = m.get("id")
            if not model_id:
                continue
            pricing = m.get("pricing") or {}

            def per_1k(key: str) -> Optional[float]:
                v = pricing.get(key)
                try:
                    return float(v) * 1000 if v is not None else None
                except (TypeError, ValueError):
                    return None

            cin, cout = per_1k("prompt"), per_1k("completion")
            registry.upsert_auto(
                model_id, source="openrouter_api",
                display_name=m.get("name"),
                context_length=m.get("context_length"),
                cost_per_1k_input=cin,
                cost_per_1k_output=cout,
                relative_cost_tier=cost_tier(cout),
            )
            count += 1
        except Exception:
            log.exception("failed to ingest OpenRouter model row")
    db.kv_set(KV_LAST_POLL, utcnow())
    db.log_event("info", "registry", f"OpenRouter poll ingested {count} models")
    return count

"""Usage & cost awareness (design doc §4.7). Two distinct signals, kept
distinct: (1) the Claude Pro/Max usage window via Meridian's /telemetry, and
(2) dollar cost for metered backends via the registry's cost fields.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

from .config import MeridianConfig
from .db import Database, utcnow

log = logging.getLogger(__name__)

# Telemetry keys that plausibly express "how much window is left/used".
# DESIGN DECISION: the community Meridian bridge's /telemetry shape is not
# standardized, so parsing is deliberately lenient — we walk the JSON for
# recognizable keys and treat anything unparseable as "window available" with
# a logged warning (fail-open: an opaque telemetry format should not disable
# Claude routing entirely).
_REMAINING_KEYS = ("remaining_fraction", "remaining_pct", "remaining_percent",
                   "remaining", "left_pct")
_USED_KEYS = ("utilization", "used_fraction", "used_pct", "used_percent", "usage_pct")


def _walk(obj: Any):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                yield from _walk(v)
            else:
                yield k.lower(), v
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _as_fraction(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= f <= 1.0:
        return f
    if 1.0 < f <= 100.0:
        return f / 100.0
    return None


class MeridianUsage:
    """Cached window check — the cache keeps one slow telemetry endpoint from
    adding latency to every escalation decision."""

    def __init__(self, cfg: MeridianConfig, client: httpx.AsyncClient, db: Database):
        self.cfg = cfg
        self.client = client
        self.db = db
        self._cache: dict[str, tuple[float, bool, str]] = {}  # url -> (ts, ok, note)

    async def window_available(self, base_url: str) -> tuple[bool, str]:
        cached = self._cache.get(base_url)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1], cached[2]
        ok, note = await self._check(base_url)
        self._cache[base_url] = (time.monotonic(), ok, note)
        return ok, note

    async def _check(self, base_url: str) -> tuple[bool, str]:
        url = base_url.rstrip("/") + self.cfg.telemetry_path
        try:
            r = await self.client.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.info("Meridian telemetry unreachable (%s) — assuming window available", e)
            return True, "telemetry unreachable; assuming window available"

        remaining: Optional[float] = None
        for key, value in _walk(data):
            frac = _as_fraction(value)
            if frac is None:
                continue
            if any(k in key for k in _REMAINING_KEYS):
                remaining = frac if remaining is None else min(remaining, frac)
            elif any(k in key for k in _USED_KEYS):
                r2 = 1.0 - frac
                remaining = r2 if remaining is None else min(remaining, r2)

        if remaining is None:
            self.db.log_event("warning", "usage",
                              "Meridian telemetry shape not recognized — assuming available",
                              json.dumps(data)[:500])
            return True, "telemetry shape unrecognized; assuming window available"
        if remaining < self.cfg.min_window_fraction:
            return False, f"usage window nearly exhausted ({remaining:.0%} remaining)"
        return True, f"{remaining:.0%} of usage window remaining"


def estimate_cost_usd(meta: Optional[dict], prompt_tokens: int,
                      completion_tokens: int) -> float:
    """Metered cost from registry fields; 0 for subscription (Meridian) and
    local models, whose cost fields are NULL."""
    if not meta:
        return 0.0
    cin = meta.get("cost_per_1k_input") or 0.0
    cout = meta.get("cost_per_1k_output") or 0.0
    return (prompt_tokens / 1000.0) * cin + (completion_tokens / 1000.0) * cout


class RequestLogger:
    """Accumulates one request's routing trail and writes a single
    request_log row at the end (§4.9 item 6)."""

    def __init__(self, db: Database, persona: str, client_model: str,
                 mode: str, user_message: str):
        self.db = db
        self.persona = persona
        self.client_model = client_model
        self.mode = mode
        self.summary = (user_message or "")[:200]
        self.models_used: list[dict] = []
        self.guardrail_events: list[str] = []
        self.steps = 0
        self.est_cost = 0.0
        self._t0 = time.monotonic()

    def record_model_call(self, model: str, backend: str, prompt_tokens: int,
                          completion_tokens: int, est_cost_usd: float) -> None:
        self.models_used.append({
            "model": model, "backend": backend,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "est_cost_usd": round(est_cost_usd, 6)})
        self.est_cost += est_cost_usd

    def record_guardrail(self, event: str) -> None:
        self.guardrail_events.append(event)

    def finish(self, status: str, error: str = "") -> None:
        try:
            self.db.execute(
                """INSERT INTO request_log
                   (ts, persona, client_model, mode, summary, models_used, steps,
                    duration_ms, guardrail_events, est_cost_usd, status, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (utcnow(), self.persona, self.client_model, self.mode, self.summary,
                 json.dumps(self.models_used), self.steps,
                 int((time.monotonic() - self._t0) * 1000),
                 json.dumps(self.guardrail_events), round(self.est_cost, 6),
                 status, error[:1000]))
        except Exception:
            log.exception("failed to write request_log row")


def spend_since(db: Database, since_sql_modifier: str) -> float:
    row = db.query_one(
        "SELECT COALESCE(SUM(est_cost_usd),0) AS total FROM request_log "
        "WHERE ts > datetime('now', ?)", (since_sql_modifier,))
    return float(row["total"]) if row else 0.0

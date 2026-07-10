"""Usage & cost awareness (design doc §4.7). Two distinct signals, kept
distinct: (1) the Claude Pro/Max usage window via Meridian's quota endpoint,
and (2) dollar cost for metered backends via the registry's cost fields.

The quota client hits GET {meridian}/v1/usage/quota with the same bearer auth
every other Meridian call uses (the original /telemetry default pointed at an
HTML dashboard with no auth — hence a whole night of "telemetry unreachable").
Response shape: {"buckets": [{"type": "five_hour"|"seven_day"|...,
"utilization": <0-1 | 0-100 | null>, "resetsAt": <sec|ms|iso>}]}. Utilization
null means "no signal yet", not an error; resetsAt units are detected by
magnitude because the OAuth and SDK sources disagree.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .config import MeridianConfig
from .db import Database, utcnow

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Claude tier classification (for adaptive conservation)                      #
# --------------------------------------------------------------------------- #

def claude_premium_level(model_id: str) -> int:
    """Claude tier ladder, per the model-choosing guidance and the plan's own
    limit structure: 4 = Fable/Mythos (heaviest; has its OWN weekly bucket,
    ~50% of total plan usage — rarely needed), 3 = Opus (complex debugging/
    architecture), 2 = Sonnet (everyday strong work), 1 = Haiku (fast/cheap),
    0 = not a recognizable Claude tier. Fable is deliberately split from Opus:
    Opus covers most hard coding work without touching Fable's tighter budget."""
    mid = model_id.lower()
    if any(k in mid for k in ("fable", "mythos")):
        return 4
    if "opus" in mid:
        return 3
    if "sonnet" in mid:
        return 2
    if "haiku" in mid:
        return 1
    return 0


def _is_fable_bucket(bucket_type: str) -> bool:
    t = bucket_type.lower()
    return "fable" in t or "mythos" in t


# --------------------------------------------------------------------------- #
# Quota parsing                                                               #
# --------------------------------------------------------------------------- #

_BUCKET_LABELS = {"five_hour": "5-hour", "session": "5-hour session",
                  "seven_day": "weekly", "weekly": "weekly",
                  "seven_days": "weekly", "daily": "daily"}


def _bucket_label(btype: str) -> str:
    if _is_fable_bucket(btype):
        return "Fable weekly"
    return _BUCKET_LABELS.get(btype, btype)


def _normalize_reset(value: Any) -> Optional[datetime]:
    """resetsAt arrives as epoch seconds, epoch milliseconds, or an ISO
    string depending on the source. Magnitude disambiguates the numerics:
    anything under ~1e10 is seconds (1e10 s ≈ year 2286; ms crossed 1e10 back
    in 1970)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1e10:
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _normalize_utilization(value: Any) -> Optional[float]:
    """null => no signal yet (NOT an error). Accepts 0-1 fractions or 0-100
    percentages."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f > 1.0:
        f /= 100.0
    return max(0.0, min(1.0, f))


def parse_quota(data: Any) -> Optional[list[dict]]:
    """[{type, label, used (0-1 or None), resets_at (iso or None),
    resets_hhmm}] — or None if the payload has no recognizable buckets."""
    if not isinstance(data, dict) or not isinstance(data.get("buckets"), list):
        return None
    out = []
    for b in data["buckets"]:
        if not isinstance(b, dict):
            continue
        btype = str(b.get("type") or "window")
        reset_dt = _normalize_reset(b.get("resetsAt") or b.get("resets_at"))
        out.append({
            "type": btype,
            "label": _bucket_label(btype),
            "fable_scoped": _is_fable_bucket(btype),
            "used": _normalize_utilization(b.get("utilization")),
            "resets_at": reset_dt.isoformat() if reset_dt else None,
            "resets_hhmm": reset_dt.strftime("%H:%M UTC") if reset_dt else None,
        })
    return out


class MeridianUsage:
    """Cached quota snapshots — one slow endpoint must not add latency to
    every escalation decision. The snapshot feeds three consumers: the
    guardrail hard-stop + adaptive tier conservation, the brain's system
    prompt, and the web UI's usage indicator."""

    def __init__(self, cfg: MeridianConfig, client: httpx.AsyncClient, db: Database):
        self.cfg = cfg
        self.client = client
        self.db = db
        self._cache: dict[str, tuple[float, dict]] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    async def snapshot(self, base_url: str, api_key: Optional[str] = None) -> dict:
        cached = self._cache.get(base_url)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        snap = await self._fetch(base_url, api_key)
        self._cache[base_url] = (time.monotonic(), snap)
        return snap

    async def window_available(self, base_url: str,
                               api_key: Optional[str] = None) -> tuple[bool, str]:
        snap = await self.snapshot(base_url, api_key)
        return snap["available"], snap["note"]

    async def _fetch(self, base_url: str, api_key: Optional[str]) -> dict:
        url = base_url.rstrip("/") + self.cfg.quota_path
        headers = {}
        if api_key:
            # Same auth every other Meridian call already sends.
            headers["Authorization"] = f"Bearer {api_key}"
            headers["x-api-key"] = api_key
        try:
            r = await self.client.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.info("Meridian quota unreachable (%s) — assuming window available", e)
            return {"available": True, "buckets": [], "worst_used": None,
                    "note": "quota endpoint unreachable; assuming window available"}

        buckets = parse_quota(data)
        if buckets is None:
            self.db.log_event("warning", "usage",
                              "quota payload shape not recognized — assuming available",
                              json.dumps(data)[:500])
            return {"available": True, "buckets": [], "worst_used": None,
                    "note": "quota shape unrecognized; assuming window available"}

        signaled = [b for b in buckets if b["used"] is not None]
        # Fable-scoped buckets are kept SEPARATE from the general window:
        # an exhausted Fable bucket must only gate Fable-class calls, and
        # conversely Fable's utilization must not throttle Sonnet/Haiku.
        general = [b for b in signaled if not b["fable_scoped"]]
        fable = [b for b in signaled if b["fable_scoped"]]
        worst_used = max((b["used"] for b in general), default=None)
        fable_used = max((b["used"] for b in fable), default=None)
        available = worst_used is None or (1.0 - worst_used) >= self.cfg.min_window_fraction
        if signaled:
            note = "; ".join(
                f"{b['label']} window {b['used']:.0%} used"
                + (f", resets {b['resets_hhmm']}" if b["resets_hhmm"] else "")
                for b in signaled)
        else:
            note = "no usage signal yet (fresh window)"
        return {"available": available, "buckets": buckets,
                "worst_used": worst_used, "fable_used": fable_used, "note": note}


# --------------------------------------------------------------------------- #
# Metered cost + per-request logging (unchanged behavior)                     #
# --------------------------------------------------------------------------- #

def estimate_cost_usd(meta: Optional[dict], prompt_tokens: int,
                      completion_tokens: int) -> float:
    """Metered cost from registry fields; 0 for subscription (Meridian) and
    local models, whose cost fields are NULL — subscription consumption is
    tracked in tokens via log_subscription_usage instead."""
    if not meta:
        return 0.0
    cin = meta.get("cost_per_1k_input") or 0.0
    cout = meta.get("cost_per_1k_output") or 0.0
    return (prompt_tokens / 1000.0) * cin + (completion_tokens / 1000.0) * cout


def log_subscription_usage(db: Database, model: str, backend: str,
                           prompt_tokens: int, completion_tokens: int) -> None:
    """Our own historical record of window consumption per Claude tier —
    dollars are the wrong unit for subscription models; tokens against the
    5-hour/weekly window are what actually deplete."""
    try:
        db.execute(
            "INSERT INTO claude_usage_log (ts, model, backend, prompt_tokens, "
            "completion_tokens) VALUES (?,?,?,?,?)",
            (utcnow(), model, backend, prompt_tokens, completion_tokens))
    except Exception:
        log.exception("failed to write claude_usage_log row")


def observed_subscription_usage(db: Database) -> dict:
    """Observed consumption over the two window shapes, for the UI."""
    def _window(modifier: str) -> dict:
        row = db.query_one(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS ptok, "
            "COALESCE(SUM(completion_tokens),0) AS ctok FROM claude_usage_log "
            "WHERE ts > datetime('now', ?)", (modifier,))
        return {"calls": row["calls"], "prompt_tokens": row["ptok"],
                "completion_tokens": row["ctok"]} if row else {}
    return {"last_5h": _window("-5 hours"), "last_7d": _window("-7 days")}


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

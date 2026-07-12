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
from .errors import describe_exception

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


# Confirmed live: Anthropic returns the SAME "authentication expired" /
# "authentication_error" text for USAGE EXHAUSTION as for real credential
# failure (session independently verified at 100% used while this error
# fired). When Meridian's quota sources are blind (oauth null, sdk
# entryCount 0 — both confirmed non-functional for this profile), a failure
# of this shape on a subscription backend is the only real signal we get.
EXHAUSTION_ERROR_MARKERS = ("authentication expired", "authentication_error",
                            "rate_limit", "429", "overloaded")


def looks_like_window_exhaustion(error_text: str) -> bool:
    t = (error_text or "").lower()
    return any(m in t for m in EXHAUSTION_ERROR_MARKERS)


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


def parse_sources(data: Any) -> Optional[bool]:
    """Is Meridian's oauth quota source alive? Found live TWICE in one
    session: `sources.oauth` silently goes null (credential staleness), the
    five_hour bucket vanishes from the response, and seven_day serves a stale
    cached read — usage figures drift (30% shown vs 43% real) with no error
    anywhere. The fix is manual (`meridian profile login <profile>
    --headless`) but the DETECTION must be automatic.

    Returns True (oauth present), False (explicitly null/absent — stale),
    or None (payload carries no `sources` at all; older builds — no signal)."""
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return None
    return data["sources"].get("oauth") is not None


def parse_extra_usage(data: Any) -> Optional[float]:
    """extraUsage.usedCredits arrives in cents (confirmed live: 4343 ==
    the Claude app's "$43.43 spent"). Returns dollars, or None if absent."""
    extra = data.get("extraUsage") if isinstance(data, dict) else None
    if not isinstance(extra, dict):
        return None
    try:
        return float(extra.get("usedCredits")) / 100.0
    except (TypeError, ValueError):
        return None


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
        self._observed_exhausted: dict[str, float] = {}  # base_url -> monotonic deadline
        self._oauth_alert: dict[str, str] = {}           # base_url -> iso since

    def clear_cache(self) -> None:
        self._cache.clear()

    # -- oauth staleness alert (edge-triggered) -------------------------------

    def _note_oauth_state(self, base_url: str, oauth_ok: Optional[bool]) -> None:
        """One Events entry when oauth flips to null, one when it recovers —
        not one per poll. None (no `sources` in the payload, or endpoint
        unreachable) neither raises nor clears the alert: absence of signal
        is not evidence of recovery."""
        if oauth_ok is False and base_url not in self._oauth_alert:
            self._oauth_alert[base_url] = utcnow()
            self.db.log_event(
                "error", "usage",
                "Meridian oauth quota source went NULL — usage figures are now "
                "stale/partial (five_hour bucket vanishes, seven_day serves a "
                "cached read). Fix on the host: meridian profile login <profile> "
                "--headless", base_url)
        elif oauth_ok is True and self._oauth_alert.pop(base_url, None):
            self.db.log_event("info", "usage",
                              "Meridian oauth quota source restored — usage "
                              "figures live again", base_url)

    # -- observed-exhaustion fallback (quota endpoint blind) -----------------

    def note_observed_exhaustion(self, base_url: str, ttl_seconds: int = 1800) -> None:
        """A live subscription call just failed with the exhaustion-shaped
        error. Treat the window as exhausted for a bounded TTL (or until a
        call succeeds) so the guardrail isn't blind when the quota endpoint
        reports nothing. Trade-off, stated plainly: a REAL credential failure
        matches the same error text — but those calls would fail anyway, so a
        30-minute backoff is the right behavior for both causes; the Events
        log entry is how you tell them apart."""
        self._observed_exhausted[base_url] = time.monotonic() + ttl_seconds
        self.db.log_event(
            "warning", "usage",
            "window exhaustion OBSERVED from a live call failure — quota endpoint "
            "reports no signal; treating window as exhausted (auto-clears on the "
            "next successful Claude call or after the backoff)", base_url)

    def note_successful_call(self, base_url: str) -> None:
        if self._observed_exhausted.pop(base_url, None) is not None:
            self._cache.pop(base_url, None)
            self.db.log_event("info", "usage",
                              "Claude call succeeded — clearing observed-exhaustion "
                              "backoff", base_url)

    def _apply_observed(self, base_url: str, snap: dict) -> dict:
        deadline = self._observed_exhausted.get(base_url)
        if deadline is None:
            return snap
        if time.monotonic() >= deadline:
            self._observed_exhausted.pop(base_url, None)
            return snap
        if snap.get("worst_used") is not None:
            return snap  # real quota data wins over the inference
        minutes = int((deadline - time.monotonic()) / 60) + 1
        return {**snap, "available": False,
                "note": f"window exhaustion OBSERVED from a live call failure "
                        f"(quota endpoint reports no data); backing off ~{minutes} "
                        f"min or until a Claude call succeeds"}

    async def snapshot(self, base_url: str, api_key: Optional[str] = None) -> dict:
        cached = self._cache.get(base_url)
        if cached and time.monotonic() - cached[0] < 30:
            return self._decorate(base_url, cached[1])
        snap = await self._fetch(base_url, api_key)
        self._cache[base_url] = (time.monotonic(), snap)
        return self._decorate(base_url, snap)

    def _decorate(self, base_url: str, snap: dict) -> dict:
        snap = self._apply_observed(base_url, snap)
        alert_since = self._oauth_alert.get(base_url)
        return {**snap, "oauth_alert_since": alert_since} if alert_since else snap

    async def auth_health(self, base_url: str, api_key: Optional[str] = None) -> dict:
        """Fresh (uncached) quota probe doubling as the auth-validity check —
        /v1/usage/quota is read-only and free, so it's the cheapest way to
        answer 'is the Claude subscription login still valid' without waiting
        for a real generation to fail. Shares all plumbing with the passive
        poll loop: the same fetch drives both."""
        self._cache.pop(base_url, None)
        snap = await self.snapshot(base_url, api_key)
        valid = (snap.get("fetch_error") is None
                 and snap.get("oauth_ok") is not False)
        return {"valid": valid, "last_checked": utcnow(),
                "oauth_ok": snap.get("oauth_ok"),
                "note": snap.get("note", ""),
                "error": snap.get("fetch_error")}

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
                    "oauth_ok": None, "fetch_error": describe_exception(e),
                    "note": "quota endpoint unreachable; assuming window available"}

        oauth_ok = parse_sources(data)
        self._note_oauth_state(base_url, oauth_ok)
        credits_used = parse_extra_usage(data)
        buckets = parse_quota(data)
        if buckets is None:
            self.db.log_event("warning", "usage",
                              "quota payload shape not recognized — assuming available",
                              json.dumps(data)[:500])
            return {"available": True, "buckets": [], "worst_used": None,
                    "oauth_ok": oauth_ok, "credits_used_usd": credits_used,
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
        if oauth_ok is False:
            note += ("; WARNING: oauth quota source is NULL — these figures "
                     "are stale/partial")
        return {"available": available, "buckets": buckets,
                "worst_used": worst_used, "fable_used": fable_used,
                "oauth_ok": oauth_ok, "credits_used_usd": credits_used,
                "note": note}


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
        self.tool_calls: list[dict] = []
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

    def record_tool_call(self, tool: str, server: str, duration_ms: int,
                         ok: bool, error: str = "") -> None:
        """One MCP/tool invocation this request made (visibility spec item 5):
        without this there is no way to see whether a request actually used
        searxng/crawl4ai/etc., how long the call took, or what it died of —
        MCP-server failures were indistinguishable from backend failures."""
        self.tool_calls.append({
            "tool": tool, "server": server, "duration_ms": duration_ms,
            "ok": ok, **({"error": error[:300]} if error else {})})

    def record_guardrail(self, event: str) -> None:
        self.guardrail_events.append(event)

    def finish(self, status: str, error: str = "") -> None:
        try:
            self.db.execute(
                """INSERT INTO request_log
                   (ts, persona, client_model, mode, summary, models_used,
                    tool_calls, steps, duration_ms, guardrail_events,
                    est_cost_usd, status, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (utcnow(), self.persona, self.client_model, self.mode, self.summary,
                 json.dumps(self.models_used), json.dumps(self.tool_calls),
                 self.steps, int((time.monotonic() - self._t0) * 1000),
                 json.dumps(self.guardrail_events), round(self.est_cost, 6),
                 status, error[:1000]))
        except Exception:
            log.exception("failed to write request_log row")


def spend_since(db: Database, since_sql_modifier: str) -> float:
    row = db.query_one(
        "SELECT COALESCE(SUM(est_cost_usd),0) AS total FROM request_log "
        "WHERE ts > datetime('now', ?)", (since_sql_modifier,))
    return float(row["total"]) if row else 0.0

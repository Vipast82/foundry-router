"""Insight digest (quality-tracking spec Phase 1): a human-readable, purely
STATISTICAL summary of routing/quality patterns per persona — feedback trends,
recurring failure types, tool-call reliability by caller model, review-pass
outcomes.

Deliberately not an automated prompt-mutation system and not LLM-written: the
numbers come straight from SQL, the operator reads them and decides what to
act on. Small sample sizes are labeled rather than smoothed over — three
thumbs-down is a hint, not a trend, and the report says so.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .db import Database, utcnow

# Below this many events, a percentage is noise — the report flags it.
LOW_SAMPLE = 10


# --------------------------------------------------------------------------- #
# Response feedback ingest (thumbs up/down)                                   #
# --------------------------------------------------------------------------- #

def normalize_rating(value: Any) -> Optional[int]:
    """Accept the rating shapes different clients send (+1/-1, 'up'/'down',
    'thumbsUp', booleans) and normalize to +1/-1 — None if unrecognizable."""
    if isinstance(value, bool):
        return 1 if value else -1
    if isinstance(value, (int, float)):
        return 1 if value > 0 else (-1 if value < 0 else None)
    s = str(value or "").strip().lower().replace("-", "_")
    if s in ("up", "thumbsup", "thumbs_up", "+1", "1", "good", "positive", "like"):
        return 1
    if s in ("down", "thumbsdown", "thumbs_down", "_1", "bad", "negative", "dislike"):
        return -1
    return None


def record_feedback(db: Database, rating: int, persona: Optional[str] = None,
                    comment: str = "", message: Optional[str] = None,
                    request_log_id: Optional[int] = None,
                    source: str = "api") -> dict:
    """Store one thumbs up/down, best-effort linked to a request_log row.

    Link resolution, most-specific first: an explicit request_log_id (the
    router GUI has real ids); else a `message` match against the stored
    summary (clients echo the user prompt); else the newest request for the
    persona. Unlinked feedback is still stored — a data point without full
    provenance beats a dropped one."""
    row = None
    if request_log_id:
        row = db.query_one("SELECT * FROM request_log WHERE id=?",
                           (request_log_id,))
    elif message:
        head = (message or "")[:200]
        if persona:
            row = db.query_one(
                "SELECT * FROM request_log WHERE summary=? AND persona=? "
                "ORDER BY id DESC LIMIT 1", (head, persona))
        else:
            row = db.query_one(
                "SELECT * FROM request_log WHERE summary=? "
                "ORDER BY id DESC LIMIT 1", (head,))
    elif persona:
        row = db.query_one(
            "SELECT * FROM request_log WHERE persona=? ORDER BY id DESC LIMIT 1",
            (persona,))

    model = None
    if row:
        persona = persona or row.get("persona")
        try:
            used = json.loads(row.get("models_used") or "[]")
            # the answering model is the last call in the trail
            model = used[-1]["model"] if used else None
        except (json.JSONDecodeError, TypeError, KeyError, IndexError):
            model = None

    fid = db.execute(
        "INSERT INTO response_feedback (ts, request_log_id, persona, model, "
        "rating, comment, source) VALUES (?,?,?,?,?,?,?)",
        (utcnow(), row["id"] if row else None, persona, model,
         int(rating), (comment or "")[:1000], source))
    return {"id": fid, "request_log_id": row["id"] if row else None,
            "persona": persona, "model": model, "rating": int(rating)}


def _window(days: int) -> str:
    return f"-{max(1, int(days))} days"


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "n/a"


def generate_digest(db: Database, days: int = 7) -> dict[str, Any]:
    """Structured digest over the last `days` of accumulated tracking data.
    Every consumer (the API endpoint, the rendered report) works off this one
    dict so numbers can never disagree between views."""
    since = _window(days)

    # -- per-persona request outcomes + feedback ------------------------------
    personas: dict[str, dict] = {}
    for r in db.query(
            "SELECT persona, COUNT(*) AS n, "
            "SUM(status='error') AS errors, SUM(status='asked_user') AS asked, "
            "AVG(duration_ms) AS avg_ms, SUM(est_cost_usd) AS cost "
            "FROM request_log WHERE ts > datetime('now', ?) "
            "GROUP BY persona ORDER BY n DESC", (since,)):
        personas[r["persona"] or "(none)"] = {
            "requests": r["n"], "errors": r["errors"] or 0,
            "asked_user": r["asked"] or 0,
            "avg_duration_ms": int(r["avg_ms"] or 0),
            "est_cost_usd": round(r["cost"] or 0.0, 4),
            "feedback_up": 0, "feedback_down": 0,
            "guardrail_top": [], "reviews": 0, "reviews_corrected": 0,
        }
    for r in db.query(
            "SELECT persona, SUM(rating > 0) AS up, SUM(rating < 0) AS down "
            "FROM response_feedback WHERE ts > datetime('now', ?) "
            "GROUP BY persona", (since,)):
        p = personas.setdefault(r["persona"] or "(none)", {
            "requests": 0, "errors": 0, "asked_user": 0, "avg_duration_ms": 0,
            "est_cost_usd": 0.0, "feedback_up": 0, "feedback_down": 0,
            "guardrail_top": [], "reviews": 0, "reviews_corrected": 0})
        p["feedback_up"] = r["up"] or 0
        p["feedback_down"] = r["down"] or 0

    # -- recurring guardrail firings per persona (top phrases) ----------------
    guard_counts: dict[str, dict[str, int]] = {}
    for r in db.query(
            "SELECT persona, guardrail_events FROM request_log "
            "WHERE ts > datetime('now', ?) AND guardrail_events IS NOT NULL "
            "AND guardrail_events != '[]'", (since,)):
        try:
            events = json.loads(r["guardrail_events"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        bucket = guard_counts.setdefault(r["persona"] or "(none)", {})
        for e in events:
            # Collapse per-model variance ("denied X: reason") into the reason
            # so recurring TYPES surface, not one line per model.
            key = str(e).split(":", 1)[-1].strip()[:120]
            bucket[key] = bucket.get(key, 0) + 1
    for persona, bucket in guard_counts.items():
        if persona in personas:
            personas[persona]["guardrail_top"] = sorted(
                bucket.items(), key=lambda kv: -kv[1])[:5]

    # -- review-pass outcomes (Phase 2 writes these) --------------------------
    for r in db.query(
            "SELECT persona, COUNT(*) AS n, SUM(corrected) AS corrected "
            "FROM review_log WHERE ts > datetime('now', ?) GROUP BY persona",
            (since,)):
        if (r["persona"] or "(none)") in personas:
            p = personas[r["persona"] or "(none)"]
            p["reviews"] = r["n"]
            p["reviews_corrected"] = r["corrected"] or 0

    # -- tool-call reliability by caller model x tool -------------------------
    tools = db.query(
        "SELECT caller, server, tool, COUNT(*) AS n, SUM(ok) AS ok, "
        "AVG(duration_ms) AS avg_ms, "
        "MAX(CASE WHEN ok=0 THEN error END) AS last_error "
        "FROM tool_call_log WHERE ts > datetime('now', ?) "
        "GROUP BY caller, server, tool ORDER BY n DESC LIMIT 40", (since,))
    tool_rows = [{
        "caller": t["caller"], "server": t["server"], "tool": t["tool"],
        "calls": t["n"], "ok": t["ok"] or 0,
        "ok_rate": _pct(t["ok"] or 0, t["n"]),
        "avg_ms": int(t["avg_ms"] or 0),
        "low_sample": t["n"] < LOW_SAMPLE,
        "last_error": (t["last_error"] or "")[:160],
    } for t in tools]

    # -- recurring error types from the event log -----------------------------
    errors = db.query(
        "SELECT source, message, COUNT(*) AS n FROM event_log "
        "WHERE ts > datetime('now', ?) AND level='error' "
        "GROUP BY source, message ORDER BY n DESC LIMIT 10", (since,))

    # -- model tool-calling reliability (lifetime counters on the registry) ---
    flaky = db.query(
        "SELECT id, tool_calls_ok, tool_calls_failed FROM models "
        "WHERE tool_calls_failed > 0 "
        "AND (tool_calls_ok + tool_calls_failed) >= 3 "
        "ORDER BY CAST(tool_calls_failed AS REAL) / "
        "(tool_calls_ok + tool_calls_failed) DESC LIMIT 10")

    return {
        "days": days,
        "personas": personas,
        "tool_calls": tool_rows,
        "recurring_errors": [dict(e) for e in errors],
        "flaky_tool_callers": [dict(f) for f in flaky],
    }


def render_report(digest: dict[str, Any]) -> str:
    """The digest as a plain-text report for the GUI/on-demand read. Structure
    over prose: the operator scans headings and numbers, no editorializing."""
    days = digest["days"]
    out = [f"INSIGHT DIGEST — last {days} day(s)", "=" * 40]

    out.append("\nPER PERSONA")
    if not digest["personas"]:
        out.append("  (no requests in window)")
    for name, p in digest["personas"].items():
        n = p["requests"]
        fb = ""
        if p["feedback_up"] or p["feedback_down"]:
            total_fb = p["feedback_up"] + p["feedback_down"]
            fb = (f" | feedback {p['feedback_up']}↑ {p['feedback_down']}↓"
                  + (" (low sample)" if total_fb < LOW_SAMPLE else ""))
        rev = ""
        if p["reviews"]:
            rev = f" | reviews {p['reviews']} ({p['reviews_corrected']} corrected)"
        out.append(f"  {name}: {n} request(s), {p['errors']} error(s) "
                   f"({_pct(p['errors'], n)}), {p['asked_user']} asked-user, "
                   f"avg {p['avg_duration_ms'] / 1000:.1f}s, "
                   f"${p['est_cost_usd']:.4f} metered{fb}{rev}")
        for reason, count in p["guardrail_top"]:
            out.append(f"      guardrail ×{count}: {reason}")

    out.append("\nTOOL-CALL RELIABILITY (caller × tool)")
    if not digest["tool_calls"]:
        out.append("  (no MCP tool calls in window)")
    for t in digest["tool_calls"]:
        flag = " [low sample]" if t["low_sample"] else ""
        err = f" | last error: {t['last_error']}" if t["last_error"] else ""
        out.append(f"  {t['caller']} → {t['server']}/{t['tool']}: "
                   f"{t['ok']}/{t['calls']} ok ({t['ok_rate']}), "
                   f"avg {t['avg_ms']}ms{flag}{err}")

    if digest["flaky_tool_callers"]:
        out.append("\nMODELS WITH TOOL-CALL FAILURES (lifetime)")
        for f in digest["flaky_tool_callers"]:
            total = f["tool_calls_ok"] + f["tool_calls_failed"]
            out.append(f"  {f['id']}: {f['tool_calls_ok']}/{total} ok"
                       + (" [low sample]" if total < LOW_SAMPLE else ""))

    if digest["recurring_errors"]:
        out.append("\nRECURRING ERRORS (event log)")
        for e in digest["recurring_errors"]:
            out.append(f"  ×{e['n']} [{e['source']}] {e['message']}")

    return "\n".join(out)

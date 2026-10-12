"""Intervention log + scoreboard for turn assist (B1 / B2).

Every action the router takes on a coding conversation — a supervisor note, a
blocked or rewritten command, an audit verdict, advice from a frontier model —
is one `interventions` row. Each assisted request also writes one
`assist_turns` row (new tool calls / errors this turn + context size), which is
what the scoreboard aggregates. Writes never raise: logging must not take down
a request.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import Database, utcnow

log = logging.getLogger(__name__)

_SPRINT_RE = re.compile(r"\b([A-Z]{1,4}-?[A-Z0-9]{0,6}\d[A-Za-z0-9]*)\b")
ZONES = [(0, 100_000, "<100K"), (100_000, 140_000, "100–140K"),
         (140_000, 180_000, "140–180K"), (180_000, 10**9, "180K+")]


def sprint_of(messages: list) -> str:
    """Best-effort sprint id from the first user message (e.g. 'GF-FX3b', 'S51')."""
    for m in messages:
        if m.get("role") == "user":
            text = m.get("content") if isinstance(m.get("content"), str) else ""
            head = re.sub(r"<[^>]+>", " ", text[:300])
            mm = _SPRINT_RE.search(head)
            return mm.group(1) if mm else ""
    return ""


def record(db: Database, *, conversation_key: str, persona: str, kind: str,
           detail: str = "", intervention: str = "", excerpt: str = "",
           model: str = "", mode: str = "", request_id: str = "",
           frontier_model: str = "", prompt_tokens: int = 0,
           completion_tokens: int = 0) -> None:
    try:
        db.execute(
            "INSERT INTO interventions (ts, conversation_key, persona, model, request_id, "
            "kind, mode, detail, intervention, excerpt, frontier_model, prompt_tokens, "
            "completion_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (utcnow(), conversation_key, persona, model, request_id, kind, mode,
             (detail or "")[:2000], (intervention or "")[:8000], (excerpt or "")[:8000],
             frontier_model, int(prompt_tokens or 0), int(completion_tokens or 0)))
    except Exception:                                             # noqa: BLE001
        log.exception("failed to write intervention row")


def record_turn(db: Database, *, conversation_key: str, persona: str, model: str,
                sprint: str, context_tokens: int, new_calls: int, new_errors: int,
                total_calls: int, think: str = "", think_why: str = "") -> Optional[int]:
    try:
        return db.execute(
            "INSERT INTO assist_turns (ts, conversation_key, persona, model, sprint, "
            "context_tokens, new_calls, new_errors, total_calls, think, think_why) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (utcnow(), conversation_key, persona, model, sprint, int(context_tokens or 0),
             int(new_calls), int(new_errors), int(total_calls), think or "", think_why or ""))
    except Exception:                                             # noqa: BLE001
        log.exception("failed to write assist_turns row")
        return None


def describe_action(content: str, tool_calls: list) -> str:
    """Compact JSON of one model reply: its text head and its tool calls."""
    calls = []
    for tc in tool_calls or []:
        fn = tc.get("function") or tc
        args = fn.get("arguments")
        calls.append({"name": fn.get("name"),
                      "arguments": (json.dumps(args, ensure_ascii=False)
                                    if not isinstance(args, str) else args)[:1500]})
    return json.dumps({"text": (content or "")[:2000], "tool_calls": calls},
                      ensure_ascii=False)


def fill_next(db: Database, conversation_key: str, content: str, tool_calls: list) -> int:
    """Attach the model's reply to every intervention in this conversation
    still waiting for one — the reply the model produced right after it."""
    if not conversation_key:
        return 0
    try:
        return db.execute_count(
            "UPDATE interventions SET next_action=?, next_ts=? "
            "WHERE conversation_key=? AND next_action IS NULL",
            (describe_action(content, tool_calls), utcnow(), conversation_key))
    except Exception:                                             # noqa: BLE001
        return 0


MISS_MIN_PROMPT = 20_000      # smaller prompts re-read in seconds: not worth tracking
MISS_BELOW = 0.5              # cached < 50% of the prompt = a miss


def new_call_stats() -> dict:
    return {"prompt": 0, "cached": 0, "prefill_ms": 0.0, "miss": False, "calls": 0}


def note_call(stats: dict, res) -> None:
    """Fold one model call's cache numbers into the request's stats. A request
    can make several calls (re-asks after a block / audit); any call that
    re-read most of a big prompt makes the request a miss."""
    try:
        prompt = int(getattr(res, "prompt_tokens", 0) or 0)
        cached = int(getattr(res, "cached_tokens", 0) or 0)
        stats["calls"] += 1
        stats["prompt"], stats["cached"] = prompt, cached
        stats["prefill_ms"] += float(getattr(res, "prompt_eval_duration_ns", 0) or 0) / 1e6
        if prompt >= MISS_MIN_PROMPT and cached < MISS_BELOW * prompt:
            stats["miss"] = True
    except Exception:                                             # noqa: BLE001
        pass


def classify_miss(db: Database, row_id: int, conversation_key: str, think: str,
                  prompt: int, guard_advanced: bool) -> str:
    """Why the server had to re-read this prompt — from what changed since the
    previous request of the same conversation."""
    prev = db.query_one(
        "SELECT think, prompt_tokens FROM assist_turns WHERE conversation_key=? AND id<? "
        "ORDER BY id DESC LIMIT 1", (conversation_key, row_id))
    if prev is None:
        return "conversation_start"
    if guard_advanced:
        return "guard_trim"
    if (prev.get("think") or "") != (think or ""):
        return "think_change"
    if prev.get("prompt_tokens") and prompt < 0.6 * prev["prompt_tokens"]:
        return "compaction"
    return "unknown"


def finish_turn(db: Database, row_id: Optional[int], output_tokens: int,
                stats: Optional[dict] = None, conversation_key: str = "",
                think: str = "", guard_advanced: bool = False) -> None:
    if not row_id:
        return
    try:
        if not stats or not stats.get("calls"):
            db.execute("UPDATE assist_turns SET output_tokens=? WHERE id=?",
                       (int(output_tokens or 0), row_id))
            return
        cause = (classify_miss(db, row_id, conversation_key, think, stats["prompt"],
                               guard_advanced) if stats["miss"] else "")
        db.execute(
            "UPDATE assist_turns SET output_tokens=?, prompt_tokens=?, cached_tokens=?, "
            "prefill_ms=?, cache_miss=?, cache_cause=? WHERE id=?",
            (int(output_tokens or 0), stats["prompt"], stats["cached"],
             round(stats["prefill_ms"], 1), int(bool(stats["miss"])), cause, row_id))
    except Exception:                                             # noqa: BLE001
        pass


def frontier_calls(db: Database, conversation_key: str) -> int:
    """Frontier (paid) assist calls already made for this conversation."""
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM interventions WHERE conversation_key=? "
        "AND frontier_model IS NOT NULL AND frontier_model != ''", (conversation_key,))
    return int(row["n"]) if row else 0


def _since(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def scoreboard(db: Database, hours: float = 168, persona: str = "") -> dict:
    """Per-conversation and per-zone numbers for the window."""
    since = _since(hours)
    pf, args = ("AND persona=? ", (since, persona)) if persona else ("", (since,))
    convs = db.query(
        "SELECT conversation_key, persona, MAX(sprint) AS sprint, MIN(ts) AS first_ts, "
        "MAX(ts) AS last_ts, COUNT(*) AS turns, SUM(new_calls) AS calls, "
        "SUM(new_errors) AS errors, MAX(context_tokens) AS max_ctx, "
        "SUM(CASE WHEN output_tokens >= 30000 THEN 1 ELSE 0 END) AS runaway, "
        "SUM(COALESCE(cache_miss, 0)) AS cache_misses, "
        "SUM(CASE WHEN cache_miss = 1 THEN prefill_ms ELSE 0 END) AS miss_ms, "
        "SUM(COALESCE(prefill_ms, 0)) AS prefill_ms "
        f"FROM assist_turns WHERE ts >= ? {pf}GROUP BY conversation_key "
        "ORDER BY last_ts DESC LIMIT 200", args)
    kinds = db.query(
        "SELECT conversation_key, kind, COUNT(*) AS n, "
        "SUM(CASE WHEN frontier_model != '' THEN 1 ELSE 0 END) AS paid, "
        "SUM(prompt_tokens + completion_tokens) AS tokens "
        f"FROM interventions WHERE ts >= ? {pf}GROUP BY conversation_key, kind", args)
    by_conv: dict = {}
    for r in kinds:
        d = by_conv.setdefault(r["conversation_key"], {"kinds": {}, "paid_calls": 0,
                                                       "paid_tokens": 0})
        d["kinds"][r["kind"]] = r["n"]
        d["paid_calls"] += r["paid"] or 0
        d["paid_tokens"] += r["tokens"] or 0
    for c in convs:
        extra = by_conv.get(c["conversation_key"], {"kinds": {}, "paid_calls": 0,
                                                     "paid_tokens": 0})
        c.update(extra)
        calls = c["calls"] or 0
        c["errors_per_100"] = round(100.0 * (c["errors"] or 0) / calls, 1) if calls else None
    zones = []
    for lo, hi, label in ZONES:
        r = db.query_one(
            "SELECT SUM(new_calls) AS calls, SUM(new_errors) AS errors, COUNT(*) AS turns "
            f"FROM assist_turns WHERE ts >= ? {pf}AND context_tokens >= ? AND context_tokens < ?",
            (*args, lo, hi))
        calls = (r or {}).get("calls") or 0
        zones.append({"zone": label, "turns": (r or {}).get("turns") or 0, "calls": calls,
                      "errors": (r or {}).get("errors") or 0,
                      "errors_per_100": round(100.0 * ((r or {}).get("errors") or 0) / calls, 1)
                      if calls else None})
    cache_rows = db.query(
        "SELECT COALESCE(NULLIF(cache_cause, ''), 'unknown') AS cause, COUNT(*) AS turns, "
        "SUM(prefill_ms) AS ms, SUM(prompt_tokens - cached_tokens) AS reread "
        f"FROM assist_turns WHERE ts >= ? {pf}AND cache_miss = 1 GROUP BY cause "
        "ORDER BY ms DESC", args)
    cache_tot = db.query_one(
        "SELECT COUNT(*) AS turns, SUM(COALESCE(cache_miss, 0)) AS misses, "
        "SUM(CASE WHEN cache_miss = 1 THEN prefill_ms ELSE 0 END) AS miss_ms, "
        "SUM(COALESCE(prefill_ms, 0)) AS prefill_ms "
        f"FROM assist_turns WHERE ts >= ? {pf}AND prompt_tokens IS NOT NULL", args) or {}
    cache = {"turns": cache_tot.get("turns") or 0, "misses": cache_tot.get("misses") or 0,
             "miss_minutes": round((cache_tot.get("miss_ms") or 0) / 60000, 1),
             "prefill_minutes": round((cache_tot.get("prefill_ms") or 0) / 60000, 1),
             "causes": [{"cause": r["cause"], "turns": r["turns"],
                         "minutes": round((r["ms"] or 0) / 60000, 1),
                         "tokens_reread": r["reread"] or 0} for r in cache_rows]}
    totals = db.query(
        f"SELECT kind, COUNT(*) AS n FROM interventions WHERE ts >= ? {pf}GROUP BY kind "
        "ORDER BY n DESC", args)
    return {"hours": hours, "conversations": convs, "zones": zones, "cache": cache,
            "interventions": {r["kind"]: r["n"] for r in totals}}


def export_jsonl(db: Database, hours: float = 24 * 30, kinds: Optional[list] = None) -> str:
    """Interventions as JSONL fine-tuning material: trigger excerpt → the
    router's intervention, plus the next intervention in the same conversation
    (shows whether the model recovered or needed more help)."""
    rows = db.query("SELECT * FROM interventions WHERE ts >= ? ORDER BY conversation_key, id",
                    (_since(hours),))
    out = []
    for i, r in enumerate(rows):
        if kinds and r["kind"] not in kinds:
            continue
        nxt = rows[i + 1] if i + 1 < len(rows) and \
            rows[i + 1]["conversation_key"] == r["conversation_key"] else None
        out.append(json.dumps({
            "ts": r["ts"], "conversation": r["conversation_key"], "persona": r["persona"],
            "model": r["model"], "kind": r["kind"], "mode": r["mode"],
            "trigger": r["excerpt"], "finding": r["detail"],
            "intervention": r["intervention"], "frontier_model": r["frontier_model"],
            "model_next_action": (json.loads(r["next_action"]) if r.get("next_action")
                                  else None),
            "next_intervention": ({"kind": nxt["kind"], "detail": nxt["detail"],
                                   "ts": nxt["ts"]} if nxt else None)},
            ensure_ascii=False))
    return "\n".join(out) + ("\n" if out else "")

"""Foundry inference capture -> normalized events.

Reads Foundry's capture records (already redacted by Foundry before it wrote
them), either from GET /v1/foundry/capture/records (orchestration key) or
directly from a capture.sqlite file. Ordered by record id, so the checkpoint
is simply the last id seen.

  received      -> model_request   (client dialect, messages, tools, settings)
  backend_call  -> model_call      (effective request, actual backend/model,
                                     response text / tool calls / reasoning,
                                     finish reason, usage, timing, status,
                                     stream reconstruction)

Capture status: complete for a finished call (a complete stream, or a
non-streaming answer); partial for an incomplete stream or a cancelled
call; truncated when Foundry cut an oversized body. The capture boundary
Foundry recorded travels with every event.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

from ..schema import Event

ADAPTER = "foundry"


def from_api(url: str, key: str, after_id: int = 0, project: str = "",
             page: int = 200) -> Iterator[dict]:
    while True:
        q = urllib.parse.urlencode({"after_id": after_id, "limit": page,
                                    **({"project": project} if project else {})})
        req = urllib.request.Request(url.rstrip("/") + "/v1/foundry/capture/records?" + q)
        if key:
            req.add_header("authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        rows = d.get("records") or []
        if not rows:
            return
        for row in rows:
            yield row
        after_id = rows[-1]["id"]


def outcomes_from_api(url: str, key: str, after_id: int = 0, page: int = 500) -> Iterator[dict]:
    """The coordinator's outcome labels (POST /v1/foundry/training/outcomes), oldest
    first. An older Foundry without the endpoint yields nothing."""
    while True:
        q = urllib.parse.urlencode({"after_id": after_id, "limit": page})
        req = urllib.request.Request(url.rstrip("/") + "/v1/foundry/training/outcomes?" + q)
        if key:
            req.add_header("authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (404, 405):
                return
            raise
        rows = d.get("outcomes") or []
        if not rows:
            return
        for row in rows:
            yield row
        after_id = rows[-1]["id"]


def outcome_records(rows) -> Iterator[tuple[str, str, dict, dict]]:
    for r in rows:
        yield (f"outcome:{r['id']}", r.get("project_id") or "", r,
               {"adapter": ADAPTER, "record_id": r["id"], "sanitization": "foundry-redacted"})


def normalize_outcome(r: dict, project_id: str, raw_ref: str, redaction: dict) -> list[Event]:
    return [Event(ADAPTER, f"outcome:{r['id']}", "router_outcome", project_id=project_id,
                  timestamp=r["ts"], task_id=r.get("task_id") or None,
                  request_id=r.get("request_id") or None, actor_role="coordinator",
                  raw_ref=raw_ref,
                  facts={"label": r.get("label"), "source": r.get("source"),
                         "evidence": r.get("evidence")})]


def from_db(path: str, after_id: int = 0) -> Iterator[dict]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        for r in c.execute("SELECT * FROM capture_records WHERE id > ? ORDER BY id", (after_id,)):
            d = dict(r)
            d["body"] = json.loads(d["body"] or "{}")
            d["redaction"] = json.loads(d["redaction"] or "{}")
            yield d
    finally:
        c.close()


def records(rows, project_map: dict | None = None,
            default_project: str = "") -> Iterator[tuple[str, str, dict, dict]]:
    """Unbound traffic (no project) is skipped unless explicitly mapped
    (project_map: session_id -> project) or a `default_project` (the profile's
    `[training] unbound_project`) takes it."""
    for r in rows:
        pid = r.get("project_id") or (project_map or {}).get(r.get("session_id") or "", "") \
            or default_project
        yield (f"capture:{r['record_uid']}", pid, r,
               {"adapter": ADAPTER, "record_id": r["id"], "boundary": r.get("boundary"),
                "foundry_redaction": r.get("redaction"), "sanitization": "foundry-redacted"})


def normalize(r: dict, project_id: str, raw_ref: str, redaction: dict) -> list[Event]:
    body = r.get("body") or {}
    common = dict(project_id=project_id, session_id=r.get("session_id") or None,
                  task_id=r.get("work_task_id") or None, request_id=r.get("request_id") or None,
                  parent_session_id=r.get("parent_task_id") or None,
                  requested_persona=r.get("persona") or None, raw_ref=raw_ref,
                  redaction_status=("redacted" if (r.get("redaction") or {}).get("status")
                                    == "redacted" or redaction.get("status") == "redacted"
                                    else "clean"))
    if r.get("kind") == "received":
        req = body.get("request") or {}
        return [Event(ADAPTER, f"capture:{r['record_uid']}", "model_request", timestamp=r["ts"],
                      actor_role="client",
                      capture_status="truncated" if r.get("truncated") else "complete",
                      facts={"dialect": body.get("dialect"), "model": req.get("model"),
                             "messages": req.get("messages"), "tools": req.get("tools"),
                             "settings": {k: v for k, v in req.items()
                                          if k not in ("messages", "tools", "model")},
                             "boundary": r.get("boundary"), "client_id": r.get("client_id"),
                             "run_id": r.get("run_id"), "role": r.get("role")},
                      **common)]
    resp = body.get("response") or {}
    status = r.get("status") or ""
    purpose = r.get("purpose") or "agent_turn"      # rows before v0.153.0 carry none
    if r.get("kind") in ("agent_run", "media_job"):
        return [Event(ADAPTER, f"capture:{r['record_uid']}", r["kind"], timestamp=r["ts"],
                      actor_role="assistant", actor_model=r.get("model") or None,
                      actual_backend=r.get("backend") or None,
                      actual_model=r.get("model") or None,
                      capture_status="truncated" if r.get("truncated") else "complete",
                      facts={"purpose": purpose, "status": status,
                             "request": body.get("request") or {}, "response": resp,
                             "error": body.get("error"), "duration_ms": body.get("duration_ms"),
                             "client_id": r.get("client_id"), "boundary": r.get("boundary")},
                      **common)]
    cap = ("truncated" if r.get("truncated") else
           "complete" if status == "ok" and r.get("reconstruction") in ("complete", "n/a", "", None)
           else "partial")
    req = body.get("request") or {}
    return [Event(ADAPTER, f"capture:{r['record_uid']}", "model_call", timestamp=r["ts"],
                  actor_role="assistant", actor_model=r.get("model"),
                  actual_backend=r.get("backend"), actual_model=r.get("model"),
                  capture_status=cap,
                  facts={"attempt": r.get("attempt"), "status": status, "purpose": purpose,
                         "stream": bool(r.get("stream")), "reconstruction": r.get("reconstruction"),
                         "messages": req.get("messages"), "tools": req.get("tools"),
                         "settings": req.get("settings"), "content": resp.get("content"),
                         "tool_calls": resp.get("tool_calls") or [],
                         "reasoning": resp.get("reasoning") or "",
                         "finish_reason": resp.get("finish_reason"), "usage": resp.get("usage"),
                         "duration_ms": body.get("duration_ms"), "error": body.get("error"),
                         # the router's own record of this call (request_log outcome,
                         # feedback, perf, coordinator labels), joined at read time
                         "quality": r.get("quality"),
                         "boundary": r.get("boundary"), "client_id": r.get("client_id"),
                         "run_id": r.get("run_id"), "role": r.get("role")},
                  **common)]

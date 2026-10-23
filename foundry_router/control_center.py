"""Coordinator control center: observation history, alerts and operator actions.

What it stores and why (contract `foundry.cc.v1`, docs/CONTROL_CENTER.md):

  coordinator facts  task state, phase, attempt, active session, transitions,
                     pending approvals bound to a revision and artifact hashes
  Kilo facts         sessions (tree via parent ids), messages, parts (text,
                     published reasoning, tools), todo, workspace MCP and
                     capability snapshots, pending questions / permissions
  Foundry facts      read straight from Foundry's own tables at query time
                     (request_log, perf_samples, usage_events, media_jobs), so
                     a model call is never counted twice
  operator actions   typed, idempotent, actor-attributed, expiring; Foundry only
                     stores them. The coordinator process that owns the engine
                     claims them, re-validates and applies them, then reports
                     delivered / applied / rejected. Foundry never edits the
                     coordinator's state and never talks to Kilo.
  alerts             derived from observations and from the watchdog below,
                     deduplicated by a stable key, acknowledged separately from
                     the thing they point at (acknowledging answers nothing)

Every observation is redacted here (foundry_training.redact, plus every secret
Foundry knows) before it is stored, sent to a browser or searched; the bridge
redacts first as well. The browser authenticates with a short-lived HttpOnly
session made from an operator-scoped key, never the key itself.

The watchdog runs inside Foundry, separately from the coordinator: when a
coordinator or bridge stops heart-beating, the alert comes from here. A Foundry
that is down cannot alert about itself; the page shows "Foundry unreachable"
when its own stream fails.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from typing import Any, Callable, Iterable, Optional

CONTRACT = "foundry.cc.v1"

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS cc_sources (
      source_id TEXT PRIMARY KEY, kind TEXT, client_id TEXT, version TEXT, host TEXT,
      capabilities TEXT, detail TEXT, first_seen REAL, last_seen REAL, last_source_ts REAL,
      seq INTEGER)""",
    """CREATE TABLE IF NOT EXISTS cc_tasks (
      project_id TEXT, task_id TEXT, state TEXT, data TEXT, source_ts REAL, received_ts REAL,
      seq INTEGER, PRIMARY KEY (project_id, task_id))""",
    """CREATE TABLE IF NOT EXISTS cc_transitions (
      project_id TEXT, task_id TEXT, src_id INTEGER, ts REAL, from_state TEXT, to_state TEXT,
      actor TEXT, session_id TEXT, data TEXT, PRIMARY KEY (project_id, task_id, src_id))""",
    """CREATE TABLE IF NOT EXISTS cc_sessions (
      session_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, run_id TEXT, role TEXT,
      phase TEXT, attempt INTEGER, parent_id TEXT, title TEXT, agent TEXT, model TEXT,
      provider TEXT, status TEXT, status_source TEXT, status_ts REAL, created_ts REAL,
      ended_ts REAL, outcome TEXT, last_activity_ts REAL, updated_ts REAL, data TEXT,
      seq INTEGER)""",
    "CREATE INDEX IF NOT EXISTS idx_cc_sessions_task ON cc_sessions(project_id, task_id)",
    "CREATE INDEX IF NOT EXISTS idx_cc_sessions_parent ON cc_sessions(parent_id)",
    """CREATE TABLE IF NOT EXISTS cc_messages (
      message_id TEXT PRIMARY KEY, session_id TEXT, project_id TEXT, role TEXT,
      created_ts REAL, completed_ts REAL, data TEXT, seq INTEGER)""",
    "CREATE INDEX IF NOT EXISTS idx_cc_messages_session ON cc_messages(session_id, created_ts)",
    """CREATE TABLE IF NOT EXISTS cc_parts (
      part_id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, project_id TEXT, kind TEXT,
      ts REAL, text TEXT, data TEXT, seq INTEGER, received_ts REAL)""",
    "CREATE INDEX IF NOT EXISTS idx_cc_parts_session ON cc_parts(session_id, ts)",
    """CREATE TABLE IF NOT EXISTS cc_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, project_id TEXT, session_id TEXT,
      scope TEXT, hash TEXT, data TEXT, source_ts REAL, received_ts REAL, confirmed_ts REAL)""",
    "CREATE INDEX IF NOT EXISTS idx_cc_snap ON cc_snapshots(kind, project_id, session_id, id)",
    """CREATE TABLE IF NOT EXISTS cc_pending (
      request_id TEXT PRIMARY KEY, kind TEXT, project_id TEXT, task_id TEXT, session_id TEXT,
      status TEXT, data TEXT, created_ts REAL, resolved_ts REAL, seq INTEGER)""",
    """CREATE TABLE IF NOT EXISTS cc_feed (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, project_id TEXT,
      session_id TEXT, entity_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS cc_alerts (
      alert_id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE, kind TEXT, severity TEXT,
      project_id TEXT, task_id TEXT, session_id TEXT, request_id TEXT, title TEXT,
      detail TEXT, status TEXT, created_ts REAL, updated_ts REAL, ack_by TEXT, ack_ts REAL,
      resolved_ts REAL, resolution TEXT, seq INTEGER)""",
    "CREATE INDEX IF NOT EXISTS idx_cc_alerts_status ON cc_alerts(status, updated_ts)",
    """CREATE TABLE IF NOT EXISTS cc_alert_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id TEXT, ts REAL, event TEXT, actor TEXT,
      detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS cc_actions (
      action_id TEXT PRIMARY KEY, idem_key TEXT UNIQUE, actor TEXT, kind TEXT,
      project_id TEXT, task_id TEXT, run_id TEXT, session_id TEXT, request_id TEXT,
      payload TEXT, summary TEXT, expected TEXT, status TEXT, status_detail TEXT,
      created_ts REAL, expires_ts REAL, claimed_by TEXT, claimed_ts REAL, updated_ts REAL,
      result TEXT, seq INTEGER)""",
    "CREATE INDEX IF NOT EXISTS idx_cc_actions_status ON cc_actions(status, created_ts)",
    "CREATE INDEX IF NOT EXISTS idx_cc_actions_task ON cc_actions(project_id, task_id, kind, "
    "created_ts)",
    # one live action per pending request: two tabs can't both answer it
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cc_actions_live_request ON cc_actions(request_id) "
    "WHERE request_id != '' AND status IN ('accepted','claimed','queued','delivered','applied')",
    """CREATE TABLE IF NOT EXISTS cc_action_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT, ts REAL, status TEXT, actor TEXT,
      detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS cc_op_sessions (
      token_hash TEXT PRIMARY KEY, actor TEXT, client_id TEXT, scopes TEXT, projects TEXT,
      csrf TEXT, created_ts REAL, expires_ts REAL, last_seen REAL)""",
    """CREATE TABLE IF NOT EXISTS cc_drafts (
      actor TEXT, target TEXT, text TEXT, updated_ts REAL, PRIMARY KEY (actor, target))""",
    "CREATE TABLE IF NOT EXISTS cc_settings (key TEXT PRIMARY KEY, value TEXT)",
    # proactive helper lanes (coordinator v0.118.0): units with their result
    # envelope, immutable artifact metadata (+ a small thumbnail when the bridge
    # sent one), and each project's lane kill-switch state
    """CREATE TABLE IF NOT EXISTS cc_lane_units (
      unit_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, lane TEXT, status TEXT,
      session_id TEXT, parent_session TEXT, parent_unit TEXT, artifact_id TEXT, data TEXT,
      updated_ts REAL, received_ts REAL, seq INTEGER)""",
    "CREATE INDEX IF NOT EXISTS idx_cc_lane_task ON cc_lane_units(project_id, task_id)",
    """CREATE TABLE IF NOT EXISTS cc_artifacts (
      artifact_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, rel_path TEXT, generation INTEGER,
      sha256 TEXT, status TEXT, data TEXT, thumb TEXT, mime TEXT, received_ts REAL, seq INTEGER)""",
    """CREATE TABLE IF NOT EXISTS cc_lane_state (
      project_id TEXT PRIMARY KEY, paused INTEGER, data TEXT, received_ts REAL)""",
    # batch ids a source already delivered (v0.122): a retried batch the server
    # stored before the reply was lost is answered from here, not re-applied
    # project-wide pause status the coordinator reports (v0.123): heartbeat
    # `projects: {pid: status}` or a `project` observation; one row per project
    """CREATE TABLE IF NOT EXISTS cc_project_status (
      project_id TEXT PRIMARY KEY, data TEXT, source_id TEXT, source_ts REAL, received_ts REAL,
      seq INTEGER)""",
    """CREATE TABLE IF NOT EXISTS cc_batches (
      source_id TEXT, batch_id TEXT, boot_id TEXT, ts REAL, result TEXT,
      PRIMARY KEY (source_id, batch_id))""",
]
LANE_STATUS_EDGE = {"pending": "queued", "deferred": "deferred", "running": "waiting",
                    "done": "completed", "delivered": "completed", "failed": "failed",
                    "timed_out": "failed", "cancelled": "stale", "skipped": "stale"}
MAX_THUMB_B64 = 600_000

# Backend collection / retention settings (operator-editable, authenticated).
# Display preferences live in the browser and never reach these.
DEFAULTS: dict[str, Any] = {
    "retention_days": 14,            # observations, parts, snapshots, feed
    "action_retention_days": 90,     # actions, alerts and their audit logs
    "collect_reasoning": True,       # store reasoning text Kilo publishes
    "collect_tool_output": True,     # store (redacted, capped) tool results
    "max_part_chars": 60000,         # per text / reasoning part
    "max_tool_chars": 20000,         # per tool input / output
    "coordinator_expiry_s": 120,     # watchdog: no coordinator heartbeat for this long
    "bridge_expiry_s": 90,           # watchdog: no bridge heartbeat for this long
    "action_ttl_s": 3600,            # an unclaimed action expires after this
    "max_instruction_chars": 20000,
    "rate_per_minute": 30,           # operator mutations per actor
    "session_hours": 12,             # operator browser session lifetime
    "min_refresh_s": 2,              # server-enforced floor for polling clients
}
SETTING_LIMITS = {
    "retention_days": (1, 365), "action_retention_days": (7, 3650),
    "max_part_chars": (2000, 200000), "max_tool_chars": (1000, 100000),
    "coordinator_expiry_s": (30, 3600), "bridge_expiry_s": (30, 3600),
    "action_ttl_s": (60, 86400), "max_instruction_chars": (1000, 100000),
    "rate_per_minute": (1, 600), "session_hours": (1, 168), "min_refresh_s": (1, 60),
}

ACTION_KINDS = ("answer_question", "reply_permission", "approve_review", "reject_review",
                "pause", "resume", "cancel", "unblock", "instruction", "withdraw_instruction",
                "lanes", "decision_answer", "project_pause", "project_resume",
                "lead_case_pause", "lead_case_takeover", "lead_case_instruct", "lead_case_resume",
                "assistant_plan", "assistant_run", "client_restart", "client_reload_kilo_config",
                "client_service_enable",
                "memory_add", "memory_edit")
# project memory (Team Lead piece 4, 2026-10-22): the chat / Memory tab append to or
# edit one docs/memory file; the coordinator validates, commits and re-indexes.
# Payload {"file", "heading", "text", "task_id"?}; project-scoped, operator sessions
# only, handed only to a coordinator that declares the `project_memory` feature.
MEMORY_ACTIONS = ("memory_add", "memory_edit")
MEMORY_FILE = re.compile(r"^(?:(?i:decisions|conventions|assets|known_issues)\.md|"
                         r"(?:tracks|sprints)/[A-Za-z0-9][A-Za-z0-9._-]{0,59}(?:\.md)?)$")
MAX_MEMORY_TEXT = 4000
MAX_MEMORY_HEADING = 120
# Foundry Client stack actions (W3, 2026-10-22): claimed by the workstation
# client that heart-beats with source kind `client` (foundry_client/relay.py),
# never by the coordinator. Payloads:
#   client_restart            {"component": kilo|coordinator|rojo|supervisor, "force": bool}
#   client_reload_kilo_config {}   (Kilo re-reads kilo.jsonc; a safe restart when it can't)
#   client_service_enable     {"component": rojo|blender|acestep-music|stable-audio|kokoro-tts|
#                              chatterbox-tts, "enabled": bool}   (Team Lead 5: switch a
#                              service component on or off in the client's settings)
# Workstation-scoped, so project_id is "" and only an operator session that is
# not restricted to projects may create one; one live action per component.
CLIENT_ACTIONS = ("client_restart", "client_reload_kilo_config", "client_service_enable")
SERVICE_COMPONENTS = ("blender", "acestep-music", "stable-audio", "kokoro-tts", "chatterbox-tts")
CLIENT_TOGGLE = ("rojo",) + SERVICE_COMPONENTS
CLIENT_COMPONENTS = ("kilo", "coordinator", "rojo", "supervisor") + SERVICE_COMPONENTS
CLIENT_FEATURE = "stack_control"
SOURCE_KINDS = ("bridge", "coordinator", "client")
# launch assistant (2026-10-21): a plain-language request becomes a checked plan
# (assistant_plan {text}); the operator confirms and the coordinator runs the
# picked steps (assistant_run {plan_id, steps}). Operator sessions only.
ASSISTANT_ACTIONS = ("assistant_plan", "assistant_run")
MAX_ASSISTANT_TEXT = 2000
# lead recovery case controls (v0.123): an operator pauses, takes over or adds an
# instruction to the task's current recovery case (the bridge's `lead` field).
# Payload {"case_id": str, "note": str (<= 2000; required for lead_case_instruct)};
# expected {"case_id", "case_state"}. Operator sessions only; only handed to a
# source that declares the `lead_manager` feature.
LEAD_ACTIONS = ("lead_case_pause", "lead_case_takeover", "lead_case_instruct", "lead_case_resume")
MAX_LEAD_NOTE = 2000
LEAD_STATES = ("DIAGNOSING", "REPAIRING", "UPGRADING", "RESUMING", "NEEDS_VICTOR", "DEFERRED",
               "RESOLVED", "CANCELLED")
LEAD_TERMINAL = ("RESOLVED", "CANCELLED")
# `project_pause` / `project_resume` (v0.123): an operator stops a whole
# project for an upgrade and starts it again. Project-scoped: task_id may be
# empty and there is no task-state stale check. Payloads:
#   project_pause  {"mode": "safe" | "now", "note": str (required, <= 500),
#                   "resume_after_upgrade": true (optional, only sent when true:
#                   the coordinator resumes the project by itself when it next
#                   starts on a different build than the one that paused)}
#   project_resume {"note": str (optional, <= 500)}
# "safe" = finish the current step, start nothing new; "now" = abort running
# sessions and helpers (state kept). Only handed to a source that declares the
# `project_pause` feature. The coordinator reports the result as the project
# status below (PROJECT_STATUS_FIELDS).
PROJECT_ACTIONS = ("project_pause", "project_resume")
PAUSE_MODES = ("safe", "now")
MAX_PAUSE_NOTE = 500
# the per-project status a coordinator reports, under heartbeat `projects`
# ({pid: status}) or as a `project` observation (project_id + these fields, at
# top level or under `status`)
PROJECT_STATUS_FIELDS = ("paused", "mode", "note", "actor", "since", "busy_sessions",
                         "running_helpers", "running_reviews", "safe_to_upgrade",
                         "resume_after_upgrade", "paused_version", "upgrade_resume")
# `decision_answer` (v0.122): an operator's answer to an open coordinator
# decision (obligations.decisions). Delivered through the existing claim route;
# the coordinator's control channel applies it with Obligations.answer(). Payload:
#   {"decision_id": str, "decision_kind": str, "answer": str (<= max_instruction_chars),
#    "option": str ("" = free-text answer only), "note": str (<= 2000),
#    "authority": "operator", "actor": <the signed-in operator, from the session>,
#    "question_hash": hash of the question the operator saw, "revision": str}
# expected: {"decision_id", "decision_status": "open", "question_hash"}. The
# authority is always "operator" (a request claiming any other authority is
# refused); a delegated policy or a model never answers through this route.
DECISION_AUTHORITY = "operator"
# version-compatibility gate on heartbeats (ok | warn | refuse)
SUPPORTED_CONTRACTS = (CONTRACT,)
MIN_SOURCE_VERSION = "0.121.0"
# the coordinator version a source declares (`coordinator_version`) is what the
# gate compares; foundry_router's own version says nothing about the engine
MIN_COORDINATOR_VERSION = "0.121.0"
COORDINATOR_MAJOR = 0
# action kinds a source must declare in its features/capabilities to be handed them
FEATURE_GATED_ACTIONS = {"decision_answer": "decision_answer", "project_pause": "project_pause",
                         "project_resume": "project_pause",
                         **{k: "lead_manager" for k in LEAD_ACTIONS},
                         **{k: "project_memory" for k in MEMORY_ACTIONS}}
# observation types that only make sense inside a project: an empty project_id
# would bypass the per-key project allowlist, so they are refused
PROJECT_SCOPED_TYPES = ("task", "lane_unit", "artifact", "mcp", "config", "capabilities", "todo",
                        "lane_state", "transition", "pending", "project", "project_pause", "memory")
BATCH_MEMORY = 200                                # batch ids kept per source
# readiness classes that are not a product defect (the AU adapter's: scope,
# tooling, environment, runtime_sync, capability, credentials, model, tests,
# ownership, deps; "tools" kept for older adapters)
ENV_BLOCKER_CLASSES = ("environment", "runtime_sync", "ownership", "tools", "tooling",
                       "capability", "scope", "credentials", "model", "tests", "deps")
LIVE_ACTION = ("accepted", "claimed", "queued", "delivered")
FINAL_ACTION = ("applied", "rejected", "failed", "expired", "cancelled")
ACTION_STATUSES = LIVE_ACTION + FINAL_ACTION
PART_KINDS = ("text", "reasoning", "tool", "step-start", "step-finish", "file", "patch",
              "retry", "compaction", "agent", "subtask", "snapshot", "error", "unknown")
SEVERITIES = ("info", "warning", "high", "critical")
# an acknowledged alert that clears and comes back within this window reopens
# still acknowledged instead of alerting again (flapping sources)
REOPEN_QUIET_SECONDS = 6 * 3600
PERMISSION_REPLIES = ("once", "reject")       # "always" creates a persistent rule: not offered


class _Dup(Exception):
    """Leaves a transaction early: the live duplicate is returned after it."""


class ControlError(ValueError):
    def __init__(self, message: str, code: str = "invalid", status: int = 400, **extra):
        super().__init__(message)
        self.code, self.status, self.extra = code, status, extra


def _j(v) -> str:
    return json.dumps(v, default=str, sort_keys=True)


def _uj(v, default=None):
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _s(v, n: int = 160) -> str:
    return str(v if v is not None else "")[:n]


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _hash(v) -> str:
    return hashlib.sha256(_j(v).encode()).hexdigest()[:24]


def _cap(text, n: int) -> tuple[str, bool]:
    s = "" if text is None else str(text)
    return (s, False) if len(s) <= n else (s[:n], True)


def _semver(v) -> Optional[tuple]:
    import re
    m = re.match(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?", str(v or ""))
    return tuple(int(x or 0) for x in m.groups()) if m else None


def _server_version() -> str:
    try:
        from foundry_router import __version__
        return __version__
    except Exception:                                            # noqa: BLE001
        return ""


def source_features(payload: dict) -> Optional[list]:
    """The features a heartbeat declares, under either name: `features` or
    `capabilities` (top level or in `source`; a list, or a dict of flags), plus
    `source.capabilities.actions`. None when the source declares none at all
    (a legacy source)."""
    src = payload.get("source") or {}
    found, any_field = set(), False
    for v in (payload.get("features"), payload.get("capabilities"), src.get("features")):
        if isinstance(v, (list, tuple)):
            any_field = True
            found |= {str(x) for x in v if isinstance(x, (str, int))}
        elif isinstance(v, dict):
            any_field = True
            found |= {str(k) for k, x in v.items() if x}
    caps = src.get("capabilities")
    if isinstance(caps, dict) and isinstance(caps.get("actions"), list):
        any_field = True
        found |= {str(x) for x in caps["actions"]}
    return sorted(found) if any_field else None


def _count(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def project_status(raw) -> Optional[dict]:
    """One reported project status, normalized (None: not a status object).
    Unknown keys are dropped; counts are non-negative ints; `safe_to_upgrade`
    is only true when the coordinator says so."""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("status"), dict):
        raw = {**raw, **raw["status"]}
    if not any(k in raw for k in PROJECT_STATUS_FIELDS):
        return None
    mode = _s(raw.get("mode"), 10)
    return {"paused": bool(raw.get("paused")),
            "mode": mode if mode in PAUSE_MODES else "",
            "note": _s(raw.get("note"), MAX_PAUSE_NOTE),
            "actor": _s(raw.get("actor"), 120),
            "since": _f(raw.get("since")),
            "busy_sessions": _count(raw.get("busy_sessions")),
            "running_helpers": _count(raw.get("running_helpers")),
            "running_reviews": _count(raw.get("running_reviews")),
            "safe_to_upgrade": raw.get("safe_to_upgrade") is True,
            "resume_after_upgrade": raw.get("resume_after_upgrade") is True,
            "paused_version": _s(raw.get("paused_version"), 40),
            "upgrade_resume": _upgrade_resume(raw.get("upgrade_resume"))}


def _upgrade_resume(raw) -> Optional[dict]:
    """The coordinator's last automatic resume after an upgrade, bounded."""
    if not isinstance(raw, dict):
        return None
    lst = lambda v, n, m: [_s(x, m) for x in (v if isinstance(v, list) else [])[:n]]  # noqa: E731
    return {"ts": _f(raw.get("ts")), "actor": _s(raw.get("actor"), 200),
            "authorized_by": _s(raw.get("authorized_by"), 120),
            "from_version": _s(raw.get("from_version"), 40),
            "to_version": _s(raw.get("to_version"), 40),
            "from_fingerprint": _s(raw.get("from_fingerprint"), 40),
            "to_fingerprint": _s(raw.get("to_fingerprint"), 40),
            "resumed": lst(raw.get("resumed"), 20, 200),
            "left_paused": lst(raw.get("left_paused"), 20, 120),
            "failed": lst(raw.get("failed"), 10, 300)}


def compat_state(payload: dict, server_version: str = "") -> tuple[str, str]:
    """ok | warn | refuse for one heartbeat. The contract is the hard gate. A
    source that declares a contract is gated on its declared
    `coordinator_version` (a different major, or one below
    MIN_COORDINATOR_VERSION, is refused). A legacy source (no contract or no
    coordinator_version) is compared on its own version and only ever warned."""
    src = payload.get("source") or {}
    contract = payload.get("contract") or src.get("contract")
    if contract and contract not in SUPPORTED_CONTRACTS:
        return "refuse", (f"contract {contract!r} is not supported (server speaks "
                          f"{', '.join(SUPPORTED_CONTRACTS)})")
    if src.get("kind") == "client":
        # a Foundry Client carries no coordinator version: gated on the contract
        # (above) and on declaring stack_control, which stack actions need
        if CLIENT_FEATURE not in (source_features(payload) or []):
            return "warn", f"the client does not declare the {CLIENT_FEATURE} feature"
        return ("ok", "") if contract else ("warn", "the client did not declare a contract")
    cv_raw = payload.get("coordinator_version") or src.get("coordinator_version")
    cv = _semver(cv_raw)
    if contract and cv_raw and not cv:
        return "refuse", f"coordinator version {str(cv_raw)[:40]!r} is not a version"
    if contract and cv:
        if cv[0] != COORDINATOR_MAJOR:
            return "refuse", (f"coordinator version {cv_raw} has major {cv[0]}; this server "
                              f"speaks coordinator {COORDINATOR_MAJOR}.x")
        if cv < _semver(MIN_COORDINATOR_VERSION):
            return "refuse", (f"coordinator version {cv_raw} is below "
                              f"{MIN_COORDINATOR_VERSION}")
        feats = source_features(payload)
        if src.get("kind") == "coordinator" and "decision_answer" not in (feats or []):
            return "warn", ("the coordinator does not declare the decision_answer feature: "
                            "decision answers are withheld from it")
        return "ok", ""
    ver = _semver(src.get("version"))
    srv = _semver(server_version)
    if ver and ver < _semver(MIN_SOURCE_VERSION):
        return "warn", f"source version {src.get('version')} is below {MIN_SOURCE_VERSION}"
    if ver and srv and ver[:2] != srv[:2]:
        return "warn", f"source version {src.get('version')} differs from server {server_version}"
    if not contract:
        return "warn", "the source did not declare a contract (pre-0.122 heartbeat)"
    return "ok", ""


# helpers.PHASES / REASON_CODES vocabulary (foundry_coordinator.helpers); the
# server keeps its own copy so it never imports the coordinator.
# "execution_timeout" is accepted as a legacy spelling of "timeout".
_TIMEOUT_CODES = ("startup_failed", "timeout", "poll_unreadable", "handoff_timeout",
                  "execution_timeout")
_WAITING_CODES = ("denied_deferred", "admission_denied", "admission_unavailable",
                  "binding_failed", "provider_retry")


def helper_phase(u: dict) -> str:
    """The UI phase for a helper unit (GAP_C vocabulary) when the coordinator
    didn't send one. Never the bare word 'failed'."""
    if u.get("phase"):
        return str(u["phase"])
    st, rc = u.get("status") or "", u.get("reason_code") or ""
    reason = str(u.get("reason") or "").lower()
    if st == "pending":
        return "queued"
    if st == "deferred":
        return "waiting_provider" if (u.get("next_eligible") or "quota" in reason
                                      or "defer" in reason or rc in _WAITING_CODES) else "queued"
    if st == "running":
        return "executing"
    if st == "timed_out" or (st == "failed" and rc in _TIMEOUT_CODES):
        return "timed_out"
    if st in ("done", "delivered"):
        return "completed"
    if st in ("skipped", "cancelled"):
        return "cancelled"
    if st == "failed":
        return "error"
    return st or "unknown"


class ControlCenter:
    def __init__(self, db, secrets_fn: Callable[[], set] = lambda: set(),
                 clock: Callable[[], float] = time.time):
        self.db = db
        self.clock = clock
        self._secrets_fn = secrets_fn
        self._secrets: tuple[float, set] = (0.0, set())
        self._rate: dict[str, list[float]] = {}
        self._rlock = threading.Lock()
        self._listeners: list[Callable[[int], None]] = []
        for sql in SCHEMA:
            db.execute(sql)

    # ------------------------------------------------------------------ plumbing

    @contextlib.contextmanager
    def _tx(self):
        """One immediate transaction on Foundry's connection."""
        with self.db._lock:
            conn = self.db._conn
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            conn.commit()

    @staticmethod
    def _rows(conn, sql, args=()) -> list[dict]:
        cur = conn.execute(sql, tuple(args))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _feed(self, conn, kind: str, project: str, session: str, entity: str) -> int:
        cur = conn.execute("INSERT INTO cc_feed (ts, kind, project_id, session_id, entity_id) "
                           "VALUES (?,?,?,?,?)", (self.clock(), kind, project or "",
                                                  session or "", entity))
        return cur.lastrowid

    def add_listener(self, fn: Callable[[int], None]) -> None:
        self._listeners.append(fn)

    def _notify(self) -> None:
        seq = self.last_seq()
        for fn in list(self._listeners):
            try:
                fn(seq)
            except Exception:                                    # noqa: BLE001
                pass

    def last_seq(self) -> int:
        r = self.db.query_one("SELECT MAX(seq) AS m FROM cc_feed")
        return int((r or {}).get("m") or 0)

    # ------------------------------------------------------------------ settings

    def settings(self) -> dict:
        out = dict(DEFAULTS)
        for r in self.db.query("SELECT key, value FROM cc_settings"):
            if r["key"] in DEFAULTS:
                v = _uj(r["value"])
                if v is not None:
                    out[r["key"]] = v
        return out

    def update_settings(self, patch: dict, actor: str) -> dict:
        cur = self.settings()
        changed = {}
        for k, v in (patch or {}).items():
            if k not in DEFAULTS:
                raise ControlError(f"unknown setting {k!r}")
            if isinstance(DEFAULTS[k], bool):
                v = bool(v)
            else:
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ControlError(f"{k} must be a number") from None
                lo, hi = SETTING_LIMITS.get(k, (0, 10 ** 9))
                if not lo <= v <= hi:
                    raise ControlError(f"{k} must be between {lo} and {hi}")
            if cur.get(k) != v:
                changed[k] = v
        for k, v in changed.items():
            self.db.execute("INSERT INTO cc_settings (key, value) VALUES (?,?) ON CONFLICT(key) "
                            "DO UPDATE SET value=excluded.value", (k, _j(v)))
        if changed:
            self.db.log_event("info", "control", f"control-center settings changed by {actor}: "
                                                 + ", ".join(f"{k}={v}" for k, v in changed.items()))
        return self.settings()

    # ------------------------------------------------------------------ redaction

    def _redactor(self):
        from foundry_training.redact import Redactor
        now = self.clock()
        if now - self._secrets[0] > 60:
            try:
                vals = set(self._secrets_fn() or ())
            except Exception:                                    # noqa: BLE001
                vals = set()
            self._secrets = (now, vals)
        return Redactor(self._secrets[1])

    def redact(self, obj):
        out, _ = self._redactor().obj(obj)
        return out

    # ------------------------------------------------------------------ ingest

    def heartbeat(self, payload: dict, client: dict) -> dict:
        src = payload.get("source") or {}
        sid = _s(src.get("id"), 120)
        kind = _s(src.get("kind"), 20)
        if not sid or kind not in SOURCE_KINDS:
            raise ControlError("heartbeat needs source.id and source.kind (bridge|coordinator|"
                               "client)")
        detail = self.redact({k: payload.get(k) for k in (
            "kilo", "coordinator_owner", "projects", "sessions_tracked", "tick", "uptime_s",
            "backoff_s", "errors", "capture_boundary", "event_stream", "monotonic_s",
            "contract", "coordinator_version", "foundry_version", "boot_id", "store_schema",
            "features", "overall", "client_version")
            if payload.get(k) is not None})
        if kind == "client":
            # the stack summary a Foundry Client reports: bounded fields, no free text
            comps = payload.get("components")
            detail["components"] = [
                {k: (_s(c.get(k), 300) if isinstance(c.get(k), str) else c.get(k))
                 for k in ("name", "label", "state", "detail", "enabled", "want", "pid",
                           "uptime_s", "restarts", "pending_restart") if k in c}
                for c in comps if isinstance(c, dict)][:12] if isinstance(comps, list) else []
            # Team Lead 5: the MCP servers Kilo can see here and the service registry
            mcp = payload.get("mcp")
            detail["mcp"] = [
                {"name": _s(m.get("name"), 80), "type": _s(m.get("type"), 12),
                 "enabled": bool(m.get("enabled")),
                 "available": m.get("available") if isinstance(m.get("available"), bool) else None,
                 "detail": _s(m.get("detail"), 160), "target": _s(m.get("target"), 120)}
                for m in mcp if isinstance(m, dict)][:40] if isinstance(mcp, list) else []
            svs = payload.get("services")
            detail["services"] = [
                {**{k: _s(x.get(k), 80) for k in ("name", "label", "kind", "check", "provider",
                                                  "component", "status", "component_state")
                    if isinstance(x.get(k), str)},
                 "detail": _s(x.get("detail"), 200), "note": _s(x.get("note"), 160),
                 "enabled": x.get("enabled") if isinstance(x.get("enabled"), bool) else None,
                 "needed_by": [_s(n, 20) for n in (x.get("needed_by") or [])][:8]}
                for x in svs if isinstance(x, dict)][:30] if isinstance(svs, list) else []
            detail["config_problems"] = [_s(x, 300) for x in (payload.get("config_problems") or [])
                                         if isinstance(x, str)][:20]
            detail["events"] = [{"ts": _f(e.get("ts")), "component": _s(e.get("component"), 40),
                                 "text": _s(e.get("text"), 300)}
                                for e in (payload.get("events") or []) if isinstance(e, dict)][-20:]
        if src.get("boot_id") and not detail.get("boot_id"):
            detail["boot_id"] = _s(src.get("boot_id"), 64)
        feats = source_features(payload)
        if feats is not None:
            # normalized from `features` or `capabilities` (either name)
            detail["features"] = [_s(f, 60) for f in feats][:64]
        # `projects` is a list of ids, or {pid: status} from a coordinator that
        # reports project pause status: the ids stay in detail, statuses get a row
        statuses: dict[str, dict] = {}
        rawp = payload.get("projects")
        if isinstance(rawp, dict):
            allowed = client.get("projects") or []
            for pid, v in list(rawp.items())[:200]:
                pid = _s(pid, 120)
                if not pid or (allowed and pid not in allowed):
                    continue
                stt = project_status(v)
                if stt is not None:
                    statuses[pid] = stt
            detail["projects"] = [_s(p, 120) for p in rawp if not allowed or p in allowed][:200]
        # the coordinator's own heartbeat reports the same status as {pid: status}
        # under `project_pause` (the bridge sends `project_pause` observations)
        rawpp = payload.get("project_pause")
        if isinstance(rawpp, dict):
            allowed = client.get("projects") or []
            for pid, v in list(rawpp.items())[:200]:
                pid = _s(pid, 120)
                if not pid or (allowed and pid not in allowed):
                    continue
                stt = project_status(v)
                if stt is not None:
                    statuses[pid] = stt
        now = self.clock()
        server_version = _server_version()
        compat, why = compat_state(payload, server_version)
        detail["compat"] = {"state": compat, "reason": why, "server_version": server_version,
                            "checked_ts": now}
        with self._tx() as c:
            c.execute("INSERT INTO cc_sources (source_id, kind, client_id, version, host, "
                      "capabilities, detail, first_seen, last_seen, last_source_ts) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                      "kind=excluded.kind, client_id=excluded.client_id, version=excluded.version, "
                      "host=excluded.host, capabilities=excluded.capabilities, "
                      "detail=excluded.detail, last_seen=excluded.last_seen, "
                      "last_source_ts=excluded.last_source_ts",
                      (sid, kind, _s(client.get("client_id"), 80), _s(src.get("version"), 40),
                       _s(src.get("host"), 80), _j(self.redact(src.get("capabilities") or {})),
                       _j(detail), now, now, _f(payload.get("sent_ts"))))
            seq = self._feed(c, "source", "", "", sid)
            c.execute("UPDATE cc_sources SET seq=? WHERE source_id=?", (seq, sid))
            for pid, stt in statuses.items():
                self._put_project_status(c, pid, self.redact(stt), sid,
                                         _f(payload.get("sent_ts")), now)
        self._resolve(f"stale:{sid}", "heartbeat received again")
        if compat == "refuse" or (compat == "warn" and "version" in why):
            sev = "high" if compat == "refuse" else "warning"
            cur = self.db.query_one("SELECT severity FROM cc_alerts WHERE dedupe_key=? AND status "
                                    "IN ('new','acknowledged')", (f"version_mismatch:{sid}",))
            if cur and cur["severity"] != sev:
                # warn <-> refuse is a different problem: re-raise at the new severity
                self._resolve(f"version_mismatch:{sid}", f"version gate now {compat}")
            self.raise_alert(f"version_mismatch:{sid}", "version_mismatch", sev,
                             f"{kind} {sid}: version gate {compat}", why, {})
        else:
            self._resolve(f"version_mismatch:{sid}", "versions compatible again")
        kilo = detail.get("kilo") or {}
        if kind == "bridge":
            if kilo and kilo.get("ok") is False:
                self.raise_alert(f"kilo_unreachable:{sid}", "source_stale", "high",
                                 "Kilo API unreachable from the workstation bridge",
                                 _s(kilo.get("error"), 300), {})
            elif kilo.get("ok"):
                self._resolve(f"kilo_unreachable:{sid}", "Kilo reachable again")
            owner = detail.get("coordinator_owner") or {}
            beat = _f(owner.get("beat_ts"))
            if owner.get("present") and beat is not None and now - beat > \
                    self.settings()["coordinator_expiry_s"]:
                self.raise_alert(f"owner_stale:{sid}", "source_stale", "high",
                                 "Coordinator loop stopped heart-beating its own store",
                                 f"observed by {sid}: last engine beat {int(now - beat)}s ago "
                                 f"(owner {owner.get('id') or '?'})", {})
            elif owner.get("present") and beat is not None:
                self._resolve(f"owner_stale:{sid}", "engine beat fresh again")
        self._notify()
        return {"ok": True, "contract": CONTRACT, "server_ts": now, "compat": compat,
                "compat_reason": why, "server_version": server_version}

    def source_compat(self, source_id: str) -> Optional[str]:
        r = self.db.query_one("SELECT detail FROM cc_sources WHERE source_id=?", (source_id,))
        if not r:
            return None
        return ((_uj(r.get("detail"), {}) or {}).get("compat") or {}).get("state")

    def source_feature_set(self, source_id: str) -> set:
        """What the source declared in its last heartbeat (empty: unknown source
        or a legacy one that declared nothing)."""
        r = self.db.query_one("SELECT detail FROM cc_sources WHERE source_id=?", (source_id,))
        d = (_uj(r.get("detail"), {}) or {}) if r else {}
        return {str(x) for x in (d.get("features") or []) if isinstance(x, str)}

    def ingest(self, batch: dict, client: dict) -> dict:
        if not isinstance(batch, dict) or batch.get("contract") != CONTRACT:
            raise ControlError(f"expected contract {CONTRACT!r}", "contract_mismatch")
        obs = batch.get("observations") or []
        if not isinstance(obs, list) or len(obs) > 2000:
            raise ControlError("observations must be a list of at most 2000 items")
        allowed = client.get("projects") or []
        src = _s((batch.get("source") or {}).get("id"), 120)
        bid = _s(batch.get("batch_id"), 120)
        if bid and src:
            seen = self.db.query_one("SELECT result FROM cc_batches WHERE source_id=? AND "
                                     "batch_id=?", (src, bid))
            if seen:
                return {**(_uj(seen["result"], {}) or {}), "duplicate": True, "batch_id": bid,
                        "server_ts": self.clock()}
        st = self.settings()
        counts: dict[str, int] = {}
        skipped: dict[str, int] = {}
        red = self._redactor()
        derived: list[tuple] = []
        now = self.clock()
        with self._tx() as c:
            for o in obs:
                if not isinstance(o, dict):
                    continue
                t = o.get("type")
                pid = _s(o.get("project_id"), 120)
                if allowed and pid and pid not in allowed:
                    skipped["project_not_allowed"] = skipped.get("project_not_allowed", 0) + 1
                    continue
                if not pid and (allowed or t in PROJECT_SCOPED_TYPES):
                    # an empty project id would slip past the allowlist
                    skipped["project_id_required"] = skipped.get("project_id_required", 0) + 1
                    continue
                fn = getattr(self, f"_in_{str(t).replace('-', '_')}", None)
                if fn is None:
                    # unknown future type: counted, never stored blindly
                    skipped[f"unknown:{_s(t, 30)}"] = skipped.get(f"unknown:{_s(t, 30)}", 0) + 1
                    continue
                o, _ = red.obj(o)
                try:
                    d = fn(c, o, st, now, src)
                except (TypeError, ValueError, KeyError) as e:
                    skipped[f"invalid:{t}"] = skipped.get(f"invalid:{t}", 0) + 1
                    _ = e
                    continue
                if d:
                    derived.extend(d)
                counts[t] = counts.get(t, 0) + 1
            if src:
                c.execute("UPDATE cc_sources SET last_seen=? WHERE source_id=?", (now, src))
            if bid and src:
                c.execute("INSERT OR IGNORE INTO cc_batches (source_id, batch_id, boot_id, ts, "
                          "result) VALUES (?,?,?,?,?)",
                          (src, bid, _s((batch.get("source") or {}).get("boot_id"), 64), now,
                           _j({"ok": True, "accepted": counts, "skipped": skipped})))
                c.execute("DELETE FROM cc_batches WHERE source_id=? AND batch_id NOT IN (SELECT "
                          "batch_id FROM cc_batches WHERE source_id=? ORDER BY ts DESC LIMIT ?)",
                          (src, src, BATCH_MEMORY))
        for args in derived:
            if args[0] == "raise":
                self.raise_alert(*args[1:])
            else:
                self._resolve(*args[1:])
        self._notify()
        out = {"ok": True, "accepted": counts, "skipped": skipped, "server_ts": now}
        if bid:
            out["batch_id"] = bid
        return out

    # each _in_<type>(conn, obs, settings, now, source) -> derived alert ops

    def _in_session(self, c, o, st, now, src):
        sid = _s(o.get("id"), 160)
        if not sid:
            raise ValueError("session id")
        old = self._rows(c, "SELECT * FROM cc_sessions WHERE session_id=?", (sid,))
        old = old[0] if old else {}
        status = o.get("status") or {}
        data = dict(_uj(old.get("data"), {}) or {})
        for k in ("archived", "kilo_status", "coordinator", "model_seen", "directory_hash",
                  "summary", "share", "revert", "time", "unavailable", "capture_boundary"):
            if o.get(k) is not None:
                data[k] = o[k]
        row = {
            "project_id": _s(o.get("project_id") or old.get("project_id"), 120),
            "task_id": _s(o.get("task_id") or old.get("task_id"), 120),
            "run_id": _s(o.get("run_id") or old.get("run_id"), 120),
            "role": _s(o.get("role") or old.get("role"), 40),
            "phase": _s(o.get("phase") or old.get("phase"), 40),
            "attempt": int(o.get("attempt") or old.get("attempt") or 0),
            "parent_id": _s(o.get("parent_id") or old.get("parent_id"), 160),
            "title": _s(o.get("title") or old.get("title"), 200),
            "agent": _s(o.get("agent") or old.get("agent"), 80),
            "model": _s((o.get("model") or {}).get("id") or old.get("model"), 120),
            "provider": _s((o.get("model") or {}).get("provider") or old.get("provider"), 80),
            "status": _s(status.get("value") or old.get("status"), 30),
            "status_source": _s(status.get("source") or old.get("status_source"), 80),
            "status_ts": _f(status.get("observed_ts")) or old.get("status_ts"),
            "created_ts": _f(o.get("created_ts")) or old.get("created_ts"),
            "ended_ts": _f(o.get("ended_ts")) or old.get("ended_ts"),
            "outcome": _s(o.get("outcome") or old.get("outcome"), 40),
            "last_activity_ts": max(_f(o.get("last_activity_ts")) or 0,
                                    old.get("last_activity_ts") or 0) or None,
            "updated_ts": now, "data": _j(data),
        }
        same = bool(old) and all(old.get(k) == v for k, v in row.items()
                                 if k not in ("updated_ts", "status_ts"))
        # an unchanged re-send (bridge restart, retry) writes no feed row: no SSE churn
        row["seq"] = old.get("seq") if same else self._feed(c, "session", row["project_id"], sid, sid)
        cols = ", ".join(row)
        c.execute(f"INSERT INTO cc_sessions (session_id, {cols}) VALUES (?,{','.join('?' * len(row))}) "
                  f"ON CONFLICT(session_id) DO UPDATE SET "
                  + ", ".join(f"{k}=excluded.{k}" for k in row), (sid, *row.values()))
        return []

    def _in_message(self, c, o, st, now, src):
        mid = _s(o.get("id"), 160)
        sid = _s(o.get("session_id"), 160)
        if not mid or not sid:
            raise ValueError("message ids")
        data = {k: o.get(k) for k in ("model_id", "provider_id", "agent", "mode", "tokens",
                                      "cost", "finish", "error", "parent_id", "summary")
                if o.get(k) is not None}
        if isinstance(data.get("error"), (dict, list, str)):
            data["error"] = _cap(_j(data["error"]) if not isinstance(data["error"], str)
                                 else data["error"], 4000)[0]
        prev = self._rows(c, "SELECT role, completed_ts, data, seq FROM cc_messages WHERE "
                             "message_id=?", (mid,))
        same = bool(prev) and prev[0]["role"] == _s(o.get("role"), 20) and \
            prev[0]["completed_ts"] == _f(o.get("completed_ts")) and prev[0]["data"] == _j(data)
        seq = prev[0]["seq"] if same else self._feed(c, "message", o.get("project_id"), sid, mid)
        c.execute("INSERT INTO cc_messages (message_id, session_id, project_id, role, created_ts, "
                  "completed_ts, data, seq) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(message_id) DO "
                  "UPDATE SET role=excluded.role, completed_ts=excluded.completed_ts, "
                  "data=excluded.data, seq=excluded.seq, created_ts=COALESCE(cc_messages."
                  "created_ts, excluded.created_ts)",
                  (mid, sid, _s(o.get("project_id"), 120), _s(o.get("role"), 20),
                   _f(o.get("created_ts")), _f(o.get("completed_ts")), _j(data), seq))
        c.execute("UPDATE cc_sessions SET last_activity_ts=MAX(COALESCE(last_activity_ts,0), ?) "
                  "WHERE session_id=?", (_f(o.get("completed_ts")) or _f(o.get("created_ts")) or 0,
                                         sid))
        return []

    def _in_part(self, c, o, st, now, src):
        pid_ = _s(o.get("id"), 160)
        sid = _s(o.get("session_id"), 160)
        if not pid_ or not sid:
            raise ValueError("part ids")
        kind = o.get("kind") if o.get("kind") in PART_KINDS else "unknown"
        data: dict = {"raw_type": _s(o.get("raw_type"), 40),
                      "time": {k: _f((o.get("time") or {}).get(k)) for k in ("start", "end")}}
        text = ""
        if kind in ("text", "reasoning"):
            if kind == "reasoning" and not st["collect_reasoning"]:
                data["withheld"] = "reasoning collection is off in control-center settings"
            else:
                text, cut = _cap(o.get("text"), int(st["max_part_chars"]))
                data["truncated"] = cut
                data["full_chars"] = len(str(o.get("text") or ""))
            if kind == "reasoning":
                data["provider_label"] = _s(o.get("provider_label"), 80)
        elif kind == "tool":
            tool = o.get("tool") or {}
            n = int(st["max_tool_chars"])
            inp, cut_in = _cap(_j(tool.get("input")) if not isinstance(tool.get("input"), str)
                               else tool.get("input"), n)
            out_raw = tool.get("output")
            if not st["collect_tool_output"]:
                out, cut_out = "", False
                data["output_withheld"] = "tool output collection is off"
            else:
                out, cut_out = _cap(out_raw if isinstance(out_raw, str) or out_raw is None
                                    else _j(out_raw), n)
            data["tool"] = {"name": _s(tool.get("name"), 120), "call_id": _s(tool.get("call_id"), 120),
                            "status": _s(tool.get("status"), 20), "title": _s(tool.get("title"), 300),
                            "input": inp, "input_truncated": cut_in, "output": out,
                            "output_truncated": cut_out, "error": _cap(tool.get("error"), 4000)[0],
                            "mcp_server": _s(tool.get("mcp_server"), 80),
                            "child_session": _s(tool.get("child_session"), 160)}
            text = f"{data['tool']['name']} {data['tool']['title']} {inp[:2000]} {out[:4000]}"
        else:
            for k in ("tokens", "cost", "reason", "attempt", "error", "files", "filename",
                      "mime", "agent", "description", "prompt", "hash", "auto"):
                if o.get(k) is not None:
                    data[k] = o[k] if not isinstance(o[k], str) else _cap(o[k], 4000)[0]
            if kind in ("retry", "error"):
                text = _s(o.get("error") or o.get("text"), 4000)
        prev = self._rows(c, "SELECT kind, text, data, seq FROM cc_parts WHERE part_id=?", (pid_,))
        same = bool(prev) and prev[0]["kind"] == kind and prev[0]["text"] == text and \
            prev[0]["data"] == _j(data)
        seq = prev[0]["seq"] if same else self._feed(c, "part", o.get("project_id"), sid, pid_)
        ts = _f(o.get("ts")) or data["time"].get("start") or now
        c.execute("INSERT INTO cc_parts (part_id, message_id, session_id, project_id, kind, ts, text, "
                  "data, seq, received_ts) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(part_id) DO "
                  "UPDATE SET kind=excluded.kind, text=excluded.text, data=excluded.data, "
                  "seq=excluded.seq, received_ts=excluded.received_ts",
                  (pid_, _s(o.get("message_id"), 160), sid, _s(o.get("project_id"), 120), kind, ts,
                   text, _j(data), seq, now))
        c.execute("UPDATE cc_sessions SET last_activity_ts=MAX(COALESCE(last_activity_ts,0), ?) "
                  "WHERE session_id=?", (data["time"].get("end") or ts, sid))
        return []

    def _snapshot(self, c, kind, o, data, now):
        pid, sid = _s(o.get("project_id"), 120), _s(o.get("session_id"), 160)
        scope = _s(o.get("scope") or ("session" if sid else "workspace"), 40)
        h = _hash(data)
        last = self._rows(c, "SELECT id, hash FROM cc_snapshots WHERE kind=? AND project_id=? AND "
                             "session_id=? AND scope=? ORDER BY id DESC LIMIT 1",
                          (kind, pid, sid, scope))
        src_ts = _f(o.get("observed_ts")) or now
        if last and last[0]["hash"] == h:
            c.execute("UPDATE cc_snapshots SET confirmed_ts=? WHERE id=?", (src_ts, last[0]["id"]))
            return None
        cur = c.execute("INSERT INTO cc_snapshots (kind, project_id, session_id, scope, hash, data, "
                        "source_ts, received_ts, confirmed_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                        (kind, pid, sid, scope, h, _j(data), src_ts, now, src_ts))
        self._feed(c, "snapshot", pid, sid, str(cur.lastrowid))
        return cur.lastrowid

    def _in_todo(self, c, o, st, now, src):
        items = []
        for i in (o.get("items") or [])[:200]:
            if isinstance(i, dict):
                items.append({"id": _s(i.get("id"), 80), "content": _s(i.get("content"), 1000),
                              "status": _s(i.get("status"), 30), "priority": _s(i.get("priority"), 20)})
        self._snapshot(c, "todo", o, {"items": items, "source": _s(o.get("source"), 80)}, now)
        return []

    def _in_mcp(self, c, o, st, now, src):
        servers = []
        for s in (o.get("servers") or [])[:200]:
            if isinstance(s, dict):
                servers.append({k: s.get(k) for k in ("name", "configured_enabled", "status",
                                                      "error", "type", "tool_count", "agents")
                                if s.get(k) is not None})
                if servers[-1].get("error"):
                    servers[-1]["error"] = _s(servers[-1]["error"], 400)
        self._snapshot(c, "mcp", o, {"servers": servers, "source": _s(o.get("source"), 80),
                                     "status_endpoint": o.get("status_endpoint")}, now)
        return []

    def _in_memory(self, c, o, st, now, src):
        """The coordinator's project memory snapshot (memory/manager.py snapshot):
        files, entries (bounded), per-session slices, recent notes."""
        ents = []
        for e in (o.get("entries") or [])[:3000]:
            if isinstance(e, dict):
                ents.append({"entry_id": _s(e.get("entry_id"), 40), "file": _s(e.get("file"), 120),
                             "heading": _s(e.get("heading"), 300), "ord": int(e.get("ord") or 0),
                             "text": _s(e.get("text"), 1200), "hash": _s(e.get("hash"), 40),
                             "tags": [_s(t, 80) for t in (e.get("tags") or [])[:24]
                                      if isinstance(t, str)]})
        sl = []
        for s_ in (o.get("slices") or [])[:40]:
            if isinstance(s_, dict):
                sl.append({"session_id": _s(s_.get("session_id"), 160),
                           "task_id": _s(s_.get("task_id"), 120), "role": _s(s_.get("role"), 30),
                           "track": _s(s_.get("track"), 80), "ts": _f(s_.get("ts")),
                           "prompts": int(s_.get("prompts") or 0), "tokens": int(s_.get("tokens") or 0),
                           "budget": int(s_.get("budget") or 0), "hash": _s(s_.get("hash"), 40),
                           "live": bool(s_.get("live")), "status": _s(s_.get("status"), 30),
                           "entries": [{"file": _s(x.get("file"), 120),
                                        "heading": _s(x.get("heading"), 300),
                                        "hash": _s(x.get("hash"), 40), "why": _s(x.get("why"), 60)}
                                       for x in (s_.get("entries") or [])[:200] if isinstance(x, dict)]})
        notes = [{k: (_s(n.get(k), 300) if isinstance(n.get(k), str) else n.get(k))
                  for k in ("task_id", "session_id", "actor", "file", "heading", "status", "detail",
                            "sha", "ts")} for n in (o.get("notes") or [])[:30] if isinstance(n, dict)]
        files = [{"file": _s(f.get("file"), 120), "entries": int(f.get("entries") or 0),
                  "hash": _s(f.get("hash"), 40), "indexed_ts": _f(f.get("indexed_ts")),
                  "size": int(f.get("size") or 0)}
                 for f in (o.get("files") or [])[:500] if isinstance(f, dict)]
        self._snapshot(c, "memory", o, {"dir": _s(o.get("dir"), 80) or "docs/memory",
                                        "enabled": bool(o.get("enabled", True)),
                                        "fingerprint": _s(o.get("fingerprint"), 40),
                                        "files": files, "entries": ents, "slices": sl,
                                        "notes": notes}, now)
        return []

    def _in_capabilities(self, c, o, st, now, src):
        self._snapshot(c, "capabilities", o, {"items": o.get("items") or {},
                                               "source": _s(o.get("source"), 80)}, now)
        return []

    def _in_config(self, c, o, st, now, src):
        # an allowlisted projection made by the bridge; stored as-is after redaction
        proj = {k: o.get(k) for k in ("agents", "models", "mcp", "subagent_depth",
                                      "enabled_providers", "kilo_version") if o.get(k) is not None}
        self._snapshot(c, "config", o, proj, now)
        return []

    def _in_pending(self, c, o, st, now, src):
        rid = _s(o.get("request_id"), 160)
        kind = o.get("kind")
        if not rid or kind not in ("question", "permission"):
            raise ValueError("pending")
        data = {k: o.get(k) for k in ("questions", "permission", "patterns", "tool", "always",
                                      "metadata", "blocking", "agent") if o.get(k) is not None}
        status = "resolved" if o.get("resolved") else "open"
        old = self._rows(c, "SELECT status, data, seq FROM cc_pending WHERE request_id=?", (rid,))
        same = bool(old) and old[0]["status"] == status and (status != "open" or
                                                             old[0]["data"] == _j(data))
        seq = old[0]["seq"] if same else self._feed(c, "pending", o.get("project_id"),
                                                    o.get("session_id"), rid)
        c.execute("INSERT INTO cc_pending (request_id, kind, project_id, task_id, session_id, status, "
                  "data, created_ts, resolved_ts, seq) VALUES (?,?,?,?,?,?,?,?,?,?) ON "
                  "CONFLICT(request_id) DO UPDATE SET status=excluded.status, data=CASE WHEN "
                  "excluded.status='open' THEN excluded.data ELSE cc_pending.data END, "
                  "resolved_ts=excluded.resolved_ts, seq=excluded.seq, "
                  "task_id=COALESCE(NULLIF(excluded.task_id,''), cc_pending.task_id)",
                  (rid, kind, _s(o.get("project_id"), 120), _s(o.get("task_id"), 120),
                   _s(o.get("session_id"), 160), status, _j(data),
                   _f(o.get("created_ts")) or now, now if status == "resolved" else None, seq))
        if status == "open":
            q = data.get("questions") or []
            first = (q[0] or {}).get("question") if q and isinstance(q[0], dict) else ""
            title = (f"Question from {o.get('task_id') or o.get('session_id')}" if kind == "question"
                     else f"Permission request: {data.get('permission') or 'tool'}")
            return [("raise", f"pending:{rid}", kind, "critical", title,
                     _s(first or ", ".join(map(str, data.get("patterns") or [])), 500),
                     {"project_id": o.get("project_id"), "task_id": o.get("task_id"),
                      "session_id": o.get("session_id"), "request_id": rid})]
        if old and old[0]["status"] == "open":
            applied = self._rows(c, "SELECT action_id FROM cc_actions WHERE request_id=? AND "
                                    "status='applied'", (rid,))
            return [("resolve", f"pending:{rid}",
                     "answered through the control center" if applied else
                     "no longer pending in Kilo (answered elsewhere, cancelled or replaced)",
                     "answered" if applied else "resolved")]
        return []

    def _in_task(self, c, o, st, now, src):
        pid, tid = _s(o.get("project_id"), 120), _s(o.get("task_id"), 120)
        if not pid or not tid:
            raise ValueError("task ids")
        state = _s(o.get("state"), 30)
        old = self._rows(c, "SELECT state, data, seq FROM cc_tasks WHERE project_id=? AND "
                            "task_id=?", (pid, tid))
        prev = old[0]["state"] if old else ""
        data = {k: v for k, v in o.items() if k not in ("type", "project_id", "task_id")}
        same = bool(old) and prev == state and old[0]["data"] == _j(data)
        seq = old[0]["seq"] if same else self._feed(c, "task", pid, o.get("session_id"), f"{pid}/{tid}")
        c.execute("INSERT INTO cc_tasks (project_id, task_id, state, data, source_ts, received_ts, seq) "
                  "VALUES (?,?,?,?,?,?,?) ON CONFLICT(project_id, task_id) DO UPDATE SET "
                  "state=excluded.state, data=excluded.data, source_ts=excluded.source_ts, "
                  "received_ts=excluded.received_ts, seq=excluded.seq",
                  (pid, tid, state, _j(data), _f(o.get("source_ts")), now, seq))
        out = []
        ref = {"project_id": pid, "task_id": tid, "session_id": o.get("session_id") or ""}
        if state == "AWAITING_HUMAN" and (o.get("approval") or {}).get("kinds"):
            ap = o["approval"]
            out.append(("raise", f"approval:{pid}:{tid}:{ap.get('revision') or ''}", "approval",
                        "critical", f"{tid}: human {', '.join(ap['kinds'])} approval needed",
                        f"revision {str(ap.get('revision') or '')[:12]} — stage "
                        f"{ap.get('stage') or '?'}", ref))
        if state == "BLOCKED":
            bc = _s(o.get("blocker_class"), 40)
            if bc in ENV_BLOCKER_CLASSES:
                # an environment fault (Rojo down, MCP failed ...) is not a product
                # defect: a typed readiness warning, same dedupe prefix
                failing = [r for r in (o.get("readiness") or []) if isinstance(r, dict)
                           and r.get("ok") is not True and r.get("blocking", True)]
                out.append(("raise", f"blocked:{pid}:{tid}:{_hash(o.get('blocker'))}", "readiness",
                            "warning", f"{tid} waits on its {bc} ({len(failing) or '?'} readiness "
                                       f"check(s) failing)",
                            _s(o.get("blocker"), 600) or ", ".join(
                                _s(r.get("name"), 80) for r in failing[:6]), ref))
            else:
                out.append(("raise", f"blocked:{pid}:{tid}:{_hash(o.get('blocker'))}", "blocker",
                            "high", f"{tid} is BLOCKED", _s(o.get("blocker"), 600), ref))
        open_dec = {}
        for dcn in (o.get("decisions") or []):
            if isinstance(dcn, dict) and dcn.get("status") == "open" and dcn.get("decision_id"):
                open_dec[_s(dcn["decision_id"], 120)] = dcn
        for did, dcn in open_dec.items():
            out.append(("raise", f"decision:{pid}:{tid}:{did}", "decision", "high",
                        f"{tid}: decision needed ({_s(dcn.get('kind'), 40) or 'decision'})",
                        _s(dcn.get("question"), 600), ref))
        if o.get("decisions") is not None:
            for a in self._rows(c, "SELECT dedupe_key FROM cc_alerts WHERE dedupe_key LIKE ? AND "
                                   "status IN ('new','acknowledged')", (f"decision:{pid}:{tid}:%",)):
                if a["dedupe_key"].split(":", 3)[3] not in open_dec:
                    out.append(("resolve", a["dedupe_key"], "decision no longer open "
                                "(answered, superseded or withdrawn)", "answered"))
        if "lead" in o:
            # a recovery case that needs Victor: one alert per case, resolved when
            # the case leaves NEEDS_VICTOR (or the task no longer has it)
            lead = o.get("lead") if isinstance(o.get("lead"), dict) else {}
            cid = _s(lead.get("case_id"), 120)
            needs = cid if lead.get("state") == "NEEDS_VICTOR" and cid else ""
            if needs:
                out.append(("raise", f"lead_needs_victor:{pid}:{tid}:{needs}", "lead_needs_victor",
                            "high", f"{tid}: lead recovery needs Victor",
                            " · ".join(x for x in (_s(lead.get("classification"), 40),
                                                   _s(lead.get("trigger"), 300),
                                                   _s(lead.get("outcome"), 300)) if x)
                            or f"case {needs}", ref))
            for a in self._rows(c, "SELECT dedupe_key FROM cc_alerts WHERE dedupe_key LIKE ? AND "
                                   "status IN ('new','acknowledged')",
                                (f"lead_needs_victor:{pid}:{tid}:%",)):
                old_case = a["dedupe_key"].split(":", 3)[3]
                if old_case != needs:
                    out.append(("resolve", a["dedupe_key"],
                                f"recovery case is now {_s(lead.get('state'), 30) or 'gone'}"
                                if old_case == cid else "the task's recovery case changed",
                                "resolved"))
        if state == "FAILED":
            out.append(("raise", f"failed:{pid}:{tid}:{o.get('attempt')}", "failure", "high",
                        f"{tid} FAILED", _s(o.get("reason") or o.get("next_action"), 600), ref))
        if state == "COMPLETE" and prev != "COMPLETE":
            out.append(("raise", f"complete:{pid}:{tid}", "completion", "info",
                        f"{tid} COMPLETE (coordinator verdict)", _s(o.get("published"), 300), ref))
        if state == "PAUSED" and o.get("pause_until"):
            out.append(("raise", f"budget_wait:{pid}:{tid}:{int(_f(o.get('pause_until')) or 0)}",
                        "budget_wait", "info", f"{tid} paused until a reset",
                        _s(o.get("pause_reason"), 400), ref))
        prefix = {"BLOCKED": "blocked", "FAILED": "failed", "AWAITING_HUMAN": "approval",
                  "PAUSED": "budget_wait"}.get(prev)
        if prev != state and prefix:
            for a in self._rows(c, "SELECT dedupe_key FROM cc_alerts WHERE dedupe_key LIKE ? "
                                   "AND status IN ('new','acknowledged')",
                                (f"{prefix}:{pid}:{tid}:%",)):
                out.append(("resolve", a["dedupe_key"], f"task moved from {prev} to {state}",
                            "resolved"))
        if o.get("queue_exhausted"):
            out.append(("raise", f"queue_exhausted:{pid}:{o.get('queue_exhausted')}",
                        "queue_exhausted", "info", f"{pid}: approved queue has nothing left to run",
                        "every approved task is complete or cancelled", {"project_id": pid}))
        return out

    def _in_transition(self, c, o, st, now, src):
        pid, tid = _s(o.get("project_id"), 120), _s(o.get("task_id"), 120)
        sid_ = int(o.get("src_id") or 0)
        if not pid or not tid or not sid_:
            raise ValueError("transition ids")
        cur = c.execute("INSERT OR IGNORE INTO cc_transitions (project_id, task_id, src_id, ts, "
                        "from_state, to_state, actor, session_id, data) VALUES (?,?,?,?,?,?,?,?,?)",
                        (pid, tid, sid_, _f(o.get("ts")), _s(o.get("from"), 30), _s(o.get("to"), 30),
                         _s(o.get("actor"), 80), _s(o.get("session_id"), 160),
                         _j(o.get("evidence") or {})))
        if cur.rowcount:
            self._feed(c, "transition", pid, o.get("session_id"), f"{pid}/{tid}/{sid_}")
        return []

    def _in_coord_event(self, c, o, st, now, src):
        if o.get("kind") == "error":
            return [("raise", f"coord_error:{_s(o.get('event_id'), 80)}", "coordinator_error",
                     "high", f"Coordinator error on {o.get('task_id') or o.get('project_id')}",
                     _s((o.get("detail") or {}).get("error"), 600),
                     {"project_id": o.get("project_id"), "task_id": o.get("task_id")})]
        return []

    def _in_lane_unit(self, c, o, st, now, src):
        uid = _s(o.get("unit_id"), 60)
        if not uid:
            raise ValueError("unit id")
        pid, tid = _s(o.get("project_id"), 120), _s(o.get("task_id"), 120)
        data = {k: v for k, v in o.items() if k not in ("type",)}
        prev = self._rows(c, "SELECT status, data, seq FROM cc_lane_units WHERE unit_id=?", (uid,))
        same = bool(prev) and prev[0]["status"] == _s(o.get("status"), 20) and \
            prev[0]["data"] == _j(data)
        seq = prev[0]["seq"] if same else self._feed(c, "lane_unit", pid, o.get("session_id") or "",
                                                     uid)
        c.execute("INSERT INTO cc_lane_units (unit_id, project_id, task_id, lane, status, session_id, "
                  "parent_session, parent_unit, artifact_id, data, updated_ts, received_ts, seq) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(unit_id) DO UPDATE SET "
                  "status=excluded.status, session_id=excluded.session_id, data=excluded.data, "
                  "parent_session=excluded.parent_session, updated_ts=excluded.updated_ts, "
                  "received_ts=excluded.received_ts, seq=excluded.seq",
                  (uid, pid, tid, _s(o.get("lane"), 40), _s(o.get("status"), 20),
                   _s(o.get("session_id"), 160), _s(o.get("parent_session"), 160),
                   _s(o.get("parent_unit"), 60), _s(o.get("artifact_id"), 80), _j(data),
                   _f(o.get("updated_ts")), now, seq))
        ref = {"project_id": pid, "task_id": tid, "session_id": o.get("session_id") or ""}
        viol = ((o.get("envelope") or {}).get("checks") or {}).get("violations")
        if viol:
            return [("raise", f"helper_violation:{uid}", "helper_violation", "high",
                     f"{tid}: helper {uid} tried a tool it may not use",
                     ", ".join(_s(v.get("tool"), 60) for v in viol[:5] if isinstance(v, dict))
                     + " — the session's permissions deny these; check the helper agent "
                       "definition", ref)]
        return []

    def _in_artifact(self, c, o, st, now, src):
        aid = _s(o.get("artifact_id"), 80)
        if not aid:
            raise ValueError("artifact id")
        thumb = o.get("thumbnail_b64")
        if not isinstance(thumb, str) or len(thumb) > MAX_THUMB_B64:
            thumb = None
        data = {k: v for k, v in o.items() if k not in ("type", "thumbnail_b64")}
        data["thumbnail"] = bool(thumb)
        prev = self._rows(c, "SELECT status, data, seq, thumb IS NOT NULL AS has FROM cc_artifacts "
                             "WHERE artifact_id=?", (aid,))
        if prev and not thumb and prev[0]["has"]:
            data["thumbnail"] = True                       # the stored thumbnail is kept
        same = bool(prev) and prev[0]["status"] == _s(o.get("status"), 20) and \
            prev[0]["data"] == _j(data) and not (thumb and not prev[0]["has"])
        seq = prev[0]["seq"] if same else self._feed(c, "artifact", o.get("project_id"), "", aid)
        c.execute("INSERT INTO cc_artifacts (artifact_id, project_id, task_id, rel_path, generation, "
                  "sha256, status, data, thumb, mime, received_ts, seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(artifact_id) DO UPDATE SET status=excluded.status, data=excluded.data, "
                  "thumb=COALESCE(excluded.thumb, cc_artifacts.thumb), received_ts=excluded.received_ts, "
                  "seq=excluded.seq",
                  (aid, _s(o.get("project_id"), 120), _s(o.get("task_id"), 120),
                   _s(o.get("rel_path"), 300), int(o.get("generation") or 0),
                   _s(o.get("sha256"), 64), _s(o.get("status"), 20), _j(data), thumb,
                   _s(o.get("mime"), 40), now, seq))
        return []

    def _in_lane_state(self, c, o, st, now, src):
        pid = _s(o.get("project_id"), 120)
        if not pid:
            raise ValueError("project")
        data = _j({k: o.get(k) for k in ("enabled", "lanes", "settings") if o.get(k) is not None})
        paused = 1 if o.get("paused") else 0
        prev = self._rows(c, "SELECT paused, data FROM cc_lane_state WHERE project_id=?", (pid,))
        c.execute("INSERT INTO cc_lane_state (project_id, paused, data, received_ts) VALUES (?,?,?,?) "
                  "ON CONFLICT(project_id) DO UPDATE SET paused=excluded.paused, data=excluded.data, "
                  "received_ts=excluded.received_ts", (pid, paused, data, now))
        if not (prev and prev[0]["paused"] == paused and prev[0]["data"] == data):
            self._feed(c, "lane_state", pid, "", pid)
        return []

    def _in_project(self, c, o, st, now, src):
        pid = _s(o.get("project_id"), 120)
        stt = project_status(o)
        if not pid or stt is None:
            raise ValueError("project status")
        self._put_project_status(c, pid, stt, src, _f(o.get("ts") or o.get("observed_ts")), now)
        return []


    # the coordinator bridge's observation type for the same status
    _in_project_pause = _in_project

    def _put_project_status(self, c, pid: str, stt: dict, src: str, source_ts, now) -> None:
        """Store one project's pause status; a feed row only when it changed."""
        data = _j(stt)
        prev = self._rows(c, "SELECT data FROM cc_project_status WHERE project_id=?", (pid,))
        changed = not (prev and prev[0]["data"] == data)
        seq = self._feed(c, "project_status", pid, "", pid) if changed else None
        c.execute("INSERT INTO cc_project_status (project_id, data, source_id, source_ts, "
                  "received_ts, seq) VALUES (?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
                  "data=excluded.data, source_id=excluded.source_id, source_ts=excluded.source_ts, "
                  "received_ts=excluded.received_ts, "
                  "seq=COALESCE(excluded.seq, cc_project_status.seq)",
                  (pid, data, _s(src, 120), source_ts, now, seq))

    @staticmethod
    def _project_status_out(r: dict) -> dict:
        return {**(_uj(r.get("data"), {}) or {}), "project_id": r["project_id"],
                "source_id": r.get("source_id") or "", "received_ts": r.get("received_ts"),
                "source_ts": r.get("source_ts")}

    def project_statuses(self, projects: list) -> dict:
        """{pid: status} for the projects this operator may see."""
        pf, pa = self._pfilter(projects)
        return {r["project_id"]: self._project_status_out(r) for r in self.db.query(
            f"SELECT * FROM cc_project_status WHERE 1=1{pf}", pa)}

    # ------------------------------------------------------------------ lanes (reads)

    def lanes(self, projects: list, project: str, task: str = "") -> dict:
        if projects and project not in projects:
            raise ControlError("unknown project", "not_found", 404)
        where, args = "project_id=?", [project]
        if task:
            where += " AND task_id=?"
            args.append(task)
        units = []
        for r in self.db.query(f"SELECT * FROM cc_lane_units WHERE {where} ORDER BY "
                               f"COALESCE(json_extract(data,'$.created_ts'),0)", args):
            d = _uj(r["data"], {})
            units.append({**d, "unit_id": r["unit_id"], "status": r["status"], "seq": r["seq"],
                          "received_ts": r["received_ts"]})
        arts = []
        for r in self.db.query(f"SELECT artifact_id, project_id, task_id, rel_path, generation, sha256, "
                               f"status, data, mime, received_ts, thumb IS NOT NULL AS has_thumb "
                               f"FROM cc_artifacts WHERE {where} ORDER BY rel_path, generation", args):
            arts.append({**_uj(r.pop("data"), {}), **r})
        state = self.db.query_one("SELECT * FROM cc_lane_state WHERE project_id=?", (project,))
        if state:
            state = {"paused": bool(state["paused"]), **_uj(state["data"], {}),
                     "received_ts": state["received_ts"]}
        return {"units": units, "artifacts": arts, "state": state, "server_ts": self.clock()}

    def artifact_thumb(self, artifact_id: str, projects: list) -> tuple:
        r = self.db.query_one("SELECT project_id, thumb, mime FROM cc_artifacts WHERE artifact_id=?",
                              (artifact_id,))
        if not r or (projects and r["project_id"] not in projects):
            raise ControlError("unknown artifact", "not_found", 404)
        if not r["thumb"]:
            raise ControlError("no thumbnail (image over the bridge's thumbnail size limit)",
                               "no_thumbnail", 404)
        import base64
        import binascii
        try:
            raw = base64.b64decode("".join(str(r["thumb"]).split()), validate=True)
        except (binascii.Error, ValueError, TypeError):
            raw = b""
        if not raw:
            raise ControlError("thumbnail unreadable", "no_thumbnail", 404)
        return raw, (r["mime"] if r["mime"] in ("image/png", "image/jpeg", "image/webp", "image/gif")
                     else "application/octet-stream")

    # ------------------------------------------------------------------ alerts

    def raise_alert(self, key: str, kind: str, severity: str, title: str, detail: str,
                    ref: dict) -> Optional[dict]:
        now = self.clock()
        reopened = False
        with self._tx() as c:
            row = self._rows(c, "SELECT * FROM cc_alerts WHERE dedupe_key=?", (key,))
            if row and row[0]["status"] in ("new", "acknowledged"):
                return row[0]                       # same event: no new alert, no repeat sound
            if row:
                # it was resolved and happened again (a new occurrence)
                aid = row[0]["alert_id"]
                seq = self._feed(c, "alert", ref.get("project_id"), ref.get("session_id"), aid)
                prev = row[0]
                rank = {s: i for i, s in enumerate(SEVERITIES)}
                quiet = (prev["status"] == "resolved" and prev.get("ack_ts")
                         and now - float(prev.get("resolved_ts") or 0) < REOPEN_QUIET_SECONDS
                         and rank.get(severity, 0) <= rank.get(prev["severity"], 0))
                if quiet:
                    # the same problem the operator already acknowledged came back
                    # soon after clearing: reopen it still acknowledged (visible,
                    # no new "new" badge or sound); a worse severity is new again
                    c.execute("UPDATE cc_alerts SET status='acknowledged', title=?, detail=?, "
                              "updated_ts=?, resolved_ts=NULL, resolution=NULL, seq=? WHERE "
                              "alert_id=?", (title[:300], detail[:2000], now, seq, aid))
                    c.execute("INSERT INTO cc_alert_log (alert_id, ts, event, actor, detail) "
                              "VALUES (?,?,?,?,?)", (aid, now, "reopened", "foundry",
                                                     f"{key} (still acknowledged by "
                                                     f"{prev.get('ack_by') or 'operator'})"))
                    reopened = True
                if not reopened:
                    c.execute("UPDATE cc_alerts SET status='new', severity=?, title=?, detail=?, "
                              "updated_ts=?, ack_by=NULL, ack_ts=NULL, resolved_ts=NULL, "
                              "resolution=NULL, seq=? WHERE alert_id=?",
                              (severity, title[:300], detail[:2000], now, seq, aid))
            else:
                aid = f"al-{uuid.uuid4().hex[:16]}"
                seq = self._feed(c, "alert", ref.get("project_id"), ref.get("session_id"), aid)
                c.execute("INSERT INTO cc_alerts (alert_id, dedupe_key, kind, severity, project_id, "
                          "task_id, session_id, request_id, title, detail, status, created_ts, "
                          "updated_ts, seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (aid, key, kind, severity if severity in SEVERITIES else "warning",
                           _s(ref.get("project_id"), 120), _s(ref.get("task_id"), 120),
                           _s(ref.get("session_id"), 160), _s(ref.get("request_id"), 160),
                           title[:300], detail[:2000], "new", now, now, seq))
            if not reopened:
                c.execute("INSERT INTO cc_alert_log (alert_id, ts, event, actor, detail) VALUES "
                          "(?,?,?,?,?)", (aid, now, "raised", "foundry", key))
        self._notify()
        return self.alert(aid)

    def _resolve(self, key: str, why: str, status: str = "resolved") -> None:
        now = self.clock()
        with self._tx() as c:
            row = self._rows(c, "SELECT alert_id, status, project_id, session_id FROM cc_alerts "
                                "WHERE dedupe_key=?", (key,))
            if not row or row[0]["status"] not in ("new", "acknowledged"):
                return
            aid = row[0]["alert_id"]
            seq = self._feed(c, "alert", row[0]["project_id"], row[0]["session_id"], aid)
            c.execute("UPDATE cc_alerts SET status=?, resolved_ts=?, resolution=?, updated_ts=?, "
                      "seq=? WHERE alert_id=?", (status, now, why[:300], now, seq, aid))
            c.execute("INSERT INTO cc_alert_log (alert_id, ts, event, actor, detail) VALUES "
                      "(?,?,?,?,?)", (aid, now, status, "foundry", why[:300]))
        self._notify()

    def alert(self, alert_id: str) -> Optional[dict]:
        return self.db.query_one("SELECT * FROM cc_alerts WHERE alert_id=?", (alert_id,))

    def alerts(self, projects: list, status: str = "", limit: int = 100,
               include_log: bool = False) -> list[dict]:
        where, args = [], []
        if status == "open":
            where.append("status IN ('new','acknowledged')")
        elif status:
            where.append("status=?")
            args.append(status)
        if projects:
            where.append(f"(project_id='' OR project_id IN ({','.join('?' * len(projects))}))")
            args += projects
        rows = self.db.query("SELECT * FROM cc_alerts" + (" WHERE " + " AND ".join(where)
                                                          if where else "")
                             + " ORDER BY updated_ts DESC LIMIT ?", (*args, max(1, min(500, limit))))
        if include_log:
            for r in rows:
                r["log"] = self.db.query("SELECT ts, event, actor, detail FROM cc_alert_log WHERE "
                                         "alert_id=? ORDER BY id DESC LIMIT 30", (r["alert_id"],))
        return rows

    def acknowledge(self, alert_id: str, actor: str, projects: list) -> dict:
        """Read/acknowledged: nothing else. It answers no question, grants no
        permission and resolves no blocker."""
        a = self.alert(alert_id)
        if not a or (projects and a["project_id"] and a["project_id"] not in projects):
            raise ControlError("no such alert", "not_found", 404)
        now = self.clock()
        with self._tx() as c:
            if a["status"] == "new":
                seq = self._feed(c, "alert", a["project_id"], a["session_id"], alert_id)
                c.execute("UPDATE cc_alerts SET status='acknowledged', ack_by=?, ack_ts=?, "
                          "updated_ts=?, seq=? WHERE alert_id=?", (actor, now, now, seq, alert_id))
            c.execute("INSERT INTO cc_alert_log (alert_id, ts, event, actor, detail) VALUES "
                      "(?,?,?,?,?)", (alert_id, now, "acknowledged", actor, ""))
        self._notify()
        return self.alert(alert_id)

    def delivery(self, alert_id: str, actor: str, channel: str, result: str, detail: str = "") -> None:
        if channel not in ("visual", "audio", "notification", "test"):
            raise ControlError("channel must be visual|audio|notification|test")
        if alert_id != "test" and not self.alert(alert_id):
            raise ControlError("no such alert", "not_found", 404)
        self.db.execute("INSERT INTO cc_alert_log (alert_id, ts, event, actor, detail) VALUES "
                        "(?,?,?,?,?)", (alert_id, self.clock(), f"{channel}:{_s(result, 30)}",
                                        actor, _s(detail, 300)))

    # ------------------------------------------------------------------ watchdog

    def watchdog(self) -> list[str]:
        """Source heartbeat expiry, checked by Foundry on its own timer. A
        coordinator that crashed cannot report its own crash; this does."""
        st = self.settings()
        now = self.clock()
        out = []
        for s in self.db.query("SELECT * FROM cc_sources"):
            limit = st["coordinator_expiry_s"] if s["kind"] == "coordinator" else st["bridge_expiry_s"]
            age = now - (s["last_seen"] or 0)
            key = f"stale:{s['source_id']}"
            if age > limit:
                what = ("coordinator heartbeat expired (the engine loop stopped, crashed, lost "
                        "the network, or the workstation is asleep)" if s["kind"] == "coordinator"
                        else "Foundry Client heartbeat expired (client closed, network down or "
                             "workstation asleep): stack actions cannot be delivered"
                        if s["kind"] == "client"
                        else "workstation telemetry bridge heartbeat expired (bridge stopped, "
                             "network down or workstation asleep): live views are stale")
                self.raise_alert(key, "source_stale", "high",
                                 f"{s['kind']} {s['source_id']} silent for {int(age)}s",
                                 what, {})
                out.append(key)
        # unclaimed actions past their expiry: never delivered, safe to expire
        for a in self.db.query("SELECT action_id FROM cc_actions WHERE (status='accepted' OR "
                               "(status='queued' AND kind IN ('answer_question','reply_permission'))"
                               ") AND expires_ts < ?", (now,)):
            self._set_action(a["action_id"], "expired", "foundry",
                             "not claimed by the coordinator before it expired; nothing was sent")
        return out

    def sources(self, projects: Optional[list] = None) -> list[dict]:
        """Every heart-beating source. A project-restricted operator sees only
        sources that declared at least one of its projects (hosts and project
        lists of other projects stay hidden)."""
        st = self.settings()
        now = self.clock()
        out = []
        for s in self.db.query("SELECT * FROM cc_sources ORDER BY kind, source_id"):
            limit = st["coordinator_expiry_s"] if s["kind"] == "coordinator" else st["bridge_expiry_s"]
            s["detail"] = _uj(s["detail"], {})
            if projects and not set(s["detail"].get("projects") or []) & set(projects):
                continue
            s["capabilities"] = _uj(s["capabilities"], {})
            s["age_s"] = round(now - (s["last_seen"] or 0), 1)
            s["fresh"] = s["age_s"] <= limit
            s["expiry_s"] = limit
            out.append(s)
        return out

    # ------------------------------------------------------------------ reads

    @staticmethod
    def _pfilter(projects: list, col: str = "project_id") -> tuple[str, tuple]:
        if not projects:
            return "", ()
        return f" AND {col} IN ({','.join('?' * len(projects))})", tuple(projects)

    def _session_out(self, r: dict) -> dict:
        r = dict(r)
        r["data"] = _uj(r.get("data"), {})
        return r

    def overview(self, projects: list, hours: float = 168) -> dict:
        since = self.clock() - hours * 3600
        pf, pa = self._pfilter(projects)
        tasks = []
        for r in self.db.query(f"SELECT * FROM cc_tasks WHERE received_ts >= ?{pf} ORDER BY "
                               "received_ts DESC", (since, *pa)):
            r["data"] = _uj(r["data"], {})
            tasks.append(self._with_views(r))
        # Work-tab snapshots fill in tasks the bridge hasn't reported yet
        known = {(t["project_id"], t["task_id"]) for t in tasks}
        for r in self.db.query(f"SELECT * FROM work_tasks WHERE updated_ts >= ?{pf}", (since, *pa)):
            if (r["project_id"], r["task_id"]) not in known:
                d = _uj(r.get("data"), {})
                tasks.append({"project_id": r["project_id"], "task_id": r["task_id"],
                              "state": r["state"], "data": {**d, "source": "work snapshot"},
                              "received_ts": r["updated_ts"], "seq": 0})
        sessions = [self._session_out(r) for r in self.db.query(
            f"SELECT * FROM cc_sessions WHERE COALESCE(updated_ts, created_ts) >= ?{pf} "
            "ORDER BY created_ts", (since, *pa))]
        known_s = {s["session_id"] for s in sessions}
        for r in self.db.query(f"SELECT * FROM work_sessions WHERE updated_ts >= ?{pf}", (since, *pa)):
            if r["session_id"] not in known_s:
                sessions.append({"session_id": r["session_id"], "project_id": r["project_id"],
                                 "task_id": r["task_id"], "run_id": r["run_id"], "role": r["role"],
                                 "parent_id": r["parent_session_id"], "model": r["persona"],
                                 "status": "", "status_source": "work binding only (no Kilo "
                                 "observation yet)", "created_ts": r["created_ts"],
                                 "ended_ts": r["ended_ts"], "outcome": r["outcome"], "data": {},
                                 "binding_only": True})
        pend = self.db.query(f"SELECT * FROM cc_pending WHERE status='open'{pf} ORDER BY created_ts",
                             pa)
        for p in pend:
            p["data"] = _uj(p["data"], {})
        pstat = self.project_statuses(projects)
        return {"contract": CONTRACT, "server_ts": self.clock(), "seq": self.last_seq(),
                "tasks": tasks, "sessions": sessions, "pending": pend,
                "sources": self.sources(projects), "project_status": pstat, "projects": sorted(
                    {t["project_id"] for t in tasks} | {s["project_id"] for s in sessions if
                                                         s.get("project_id")} | set(pstat)),
                "settings": {k: self.settings()[k] for k in ("min_refresh_s",
                                                             "collect_reasoning",
                                                             "collect_tool_output",
                                                             "retention_days")}}

    def transitions(self, project: str, task: str, limit: int = 200) -> list[dict]:
        rows = self.db.query("SELECT * FROM cc_transitions WHERE project_id=? AND task_id=? "
                             "ORDER BY src_id DESC LIMIT ?", (project, task, max(1, min(1000, limit))))
        for r in rows:
            r["data"] = _uj(r["data"], {})
        return rows

    # States in which the recorded checks/review cannot belong to the run in
    # progress: a new implementation round is under way (or about to start).
    _PRE_VERIFY = ("QUEUED", "READY", "IMPLEMENTING", "CHANGES_REQUIRED")

    def _safe_view(self, task: dict) -> dict:
        try:
            return self.verification_view(task)
        except Exception as e:                                    # noqa: BLE001
            return {"error": f"verification view unavailable: {type(e).__name__}"}

    def verification_view(self, task: dict) -> dict:
        """What the Verification panel needs to be honest, derived from the
        bridge's task data plus recorded transitions and operator actions:
        which revision the checks and the review were for and when, whether
        those are from a previous run, whether the current head differs from
        the checked revision, and whether an operator authorized recovery after
        the attempt budget ran out. Observations only; it decides nothing."""
        d = task.get("data") or {}
        pid, tid, state = task.get("project_id"), task.get("task_id"), task.get("state") or ""
        eff = (d.get("resume_state") or state) if state == "PAUSED" else state
        previous = eff in self._PRE_VERIFY
        current = d.get("workspace_head") or d.get("head_sha") or None
        current_src = "workspace HEAD" if d.get("workspace_head") else "task head" if current else ""

        def differs(sha):
            return bool(sha and current and str(sha) != str(current))

        ver = [v for v in (d.get("verification") or []) if isinstance(v, dict)]
        checks = None
        if ver:
            # the newest executed row names the revision the set was run against
            newest = max(ver, key=lambda v: _f(v.get("executed_ts")) or 0)
            sha = newest.get("revision") or next(
                (v["revision"] for v in reversed(ver) if v.get("revision")), None)
            revs = list(dict.fromkeys(str(v["revision"]) for v in ver if v.get("revision")))
            checks = {"revision": sha, "revisions": revs,
                      "executed_ts": _f(newest.get("executed_ts")),
                      # recorded checks predate a VERIFYING run in progress too
                      "previous_run": previous or eff == "VERIFYING", "stale": differs(sha),
                      "verification_id": d.get("verification_id")}
        rv = next((r for r in reversed(d.get("reviews") or []) if isinstance(r, dict)), None)
        review = None
        if rv:
            review = {"verdict": rv.get("verdict"), "valid": rv.get("valid"),
                      "persona": rv.get("persona"), "reviewed_sha": rv.get("reviewed_sha"),
                      "ts": _f(rv.get("ts")),
                      "previous_run": previous or eff in ("VERIFYING", "REVIEWING"),
                      "stale": differs(rv.get("reviewed_sha"))}
        # --- attempt budget and operator recovery
        lim = d.get("limits") or {}
        used = int(d.get("attempt") or 0)
        mx = int(lim.get("max_attempts") or 0) or None
        rg = d.get("retry_guidance") if isinstance(d.get("retry_guidance"), dict) else {}
        extra = 1 if rg.get("status") == "repair" and rg.get("extra_attempts") == 1 else 0
        stops = self.db.query("SELECT MAX(ts) AS t FROM cc_transitions WHERE project_id=? AND "
                              "task_id=? AND to_state IN ('BLOCKED','FAILED')", (pid, tid))
        last_stop = (stops[0]["t"] if stops else None) or 0
        recovery = None
        acts = self.db.query("SELECT action_id, actor, status, payload, created_ts, updated_ts "
                             "FROM cc_actions WHERE project_id=? AND task_id=? AND kind='unblock' "
                             "AND status NOT IN ('rejected','failed','expired','cancelled') "
                             "ORDER BY created_ts DESC LIMIT 1", (pid, tid))
        if acts and (acts[0]["created_ts"] or 0) >= last_stop:
            a = acts[0]
            pl = _uj(a.get("payload"), {}) or {}
            recovery = {"kind": "unblock", "reset_attempts": bool(pl.get("reset_attempts")),
                        "by": a.get("actor"), "ts": a.get("created_ts"),
                        "status": a.get("status"), "action_id": a.get("action_id"),
                        "resolution": _s(pl.get("resolution"), 300)}
        if recovery is None:
            tr = self.db.query("SELECT ts, actor, from_state, data FROM cc_transitions WHERE "
                               "project_id=? AND task_id=? AND to_state='READY' AND from_state IN "
                               "('BLOCKED','FAILED') ORDER BY src_id DESC LIMIT 1", (pid, tid))
            if tr and (tr[0]["ts"] or 0) >= last_stop:
                ev = _uj(tr[0]["data"], {}) or {}
                # v0.121.7 coordinators record it in the transition; older: unknown
                recovery = {"kind": "unblock", "reset_attempts": ev.get("reset_attempts"),
                            "by": tr[0]["actor"],
                            "ts": tr[0]["ts"], "status": "applied", "action_id": None,
                            "resolution": _s(ev.get("resolution"), 300)}
        if recovery is None and (extra or rg.get("status") == "reset"):
            # the bridge forwards retry_guidance only if it has it; never inferred
            recovery = {"kind": "retry_guidance_" + ("extra_attempt" if extra else "reset"),
                        "reset_attempts": not extra, "by": rg.get("persona"),
                        "ts": None, "status": rg.get("status"), "action_id": None,
                        "resolution": _s(rg.get("summary"), 300)}
        budget = (mx + extra) if mx else None
        exhausted = bool(budget and used >= budget)
        if not mx:
            label = f"{used} / ? attempts"
        else:
            label = f"{used}/{mx} attempts used" + (" (+1 guided extra attempt)" if extra else "")
            if recovery and recovery.get("reset_attempts"):
                label += " · operator recovery authorized (fresh budget)"
            elif recovery and recovery["kind"].startswith("retry_guidance"):
                label += " · recovery authorized (retry-guidance extra attempt)"
            elif recovery:
                label += " · operator unblocked (attempt count kept)"
            elif exhausted and state in ("BLOCKED", "FAILED"):
                label += " · budget exhausted, stopped"
            elif exhausted:
                label += " · final attempt in progress"
        return {"current_sha": current, "current_source": current_src, "previous_run": previous,
                "checks": checks, "review": review, "build": self._build_continuing(d, eff),
                "attempts": {"used": used, "max": mx, "extra": extra, "exhausted": exhausted,
                             "label": label},
                "recovery": recovery}

    @staticmethod
    def _build_continuing(d: dict, eff: str) -> Optional[dict]:
        """BUILD continuing (committed partial BUILD progress, not a failure):
        only what the bridge derived from the engine's own transition record
        (build_continuing), bounded, and only while the task is still in the
        BUILD loop. Not an attempt and not a decision."""
        b = d.get("build_continuing")
        if not isinstance(b, dict) or eff not in ("CHANGES_REQUIRED", "IMPLEMENTING",
                                                  "AWAITING_HUMAN"):
            return None
        steps = [_s(x, 240) for x in (b.get("steps") if isinstance(b.get("steps"), list)
                                      else [])[:12] if isinstance(x, str)]
        n = _count(b.get("open_steps")) or len(steps)
        tot = b.get("total_steps")
        # the plan's BUILD row count (bridge >= v0.128); None = not reported
        tot = tot if isinstance(tot, int) and not isinstance(tot, bool) and 0 < tot <= 10000 else None
        return {"open_steps": n, "steps": steps, "truncated": b.get("truncated") is True,
                "total_steps": tot,
                "head_sha": _s(b.get("head_sha"), 64), "ts": _f(b.get("ts"))}

    @staticmethod
    def _background_view(d: dict, eff: str) -> Optional[dict]:
        """Background work the bridge reported (the fast lane's studio gate or
        vision batches: not Kilo sessions), bounded and typed for the page.
        Only while VERIFYING; None otherwise. `done`/`total` are parsed from the
        gate's "84/110" progress counter when it has that shape."""
        b = d.get("background")
        if not isinstance(b, dict) or eff != "VERIFYING" or b.get("kind") not in ("gate", "vision"):
            return None
        num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None  # noqa: E731
        out = {"kind": b["kind"], "head": _s(b.get("head"), 64), "started_ts": num(b.get("started_ts")),
               "updated_ts": num(b.get("updated_ts"))}
        if b["kind"] == "gate":
            prog = _s(b.get("progress"), 40)
            m = re.match(r"^\s*(\d{1,7})\s*/\s*(\d{1,7})\s*$", prog)
            done, total = (int(m.group(1)), int(m.group(2))) if m else (None, None)
            out.update(phase=_s(b.get("phase"), 60), progress=prog, done=done,
                       total=total if total else None, limit_s=num(b.get("limit_s")),
                       playing=b.get("playing") is True, log=_s(b.get("log"), 400),
                       run_id=_s(b.get("run_id"), 120))
        else:
            out.update({k: num(b.get(k)) for k in ("batch", "batches", "frames", "total_frames",
                                                    "done", "changes", "more_findings")})
            out["findings"] = [_s(f, 300) for f in (b.get("findings") if isinstance(
                b.get("findings"), list) else [])[:8] if isinstance(f, str)]
            out["persona"] = _s(b.get("persona"), 120)
        return out

    def _safe_task_view(self, task: dict) -> dict:
        try:
            return self.task_view(task)
        except Exception as e:                                    # noqa: BLE001
            return {"error": f"task view unavailable: {type(e).__name__}"}

    # raw lifecycle lists the task_view already normalizes: the overview and the
    # live feed send task_view only (the /tasks/{p}/{t}/view route has both views)
    RAW_LIFECYCLE_KEYS = ("readiness", "obligations", "evidence", "recovered",
                          "finding_history", "budgets", "decisions", "memory", "lead")

    def _with_views(self, r: dict) -> dict:
        r["verification_view"] = self._safe_view(r)
        r["task_view"] = self._safe_task_view(r)
        if not r["task_view"].get("error"):
            r["data"] = {k: v for k, v in (r.get("data") or {}).items()
                         if k not in self.RAW_LIFECYCLE_KEYS}
        return r

    _OB_DONE = ("verified", "waived", "superseded", "done", "waived_by_human")

    def task_view(self, task: dict) -> dict:
        """The v0.122 sprint-management fields the bridge reported, normalized
        for the page: readiness, the required-work checklist, evidence per
        check, recovered results, repeated findings, budgets and decisions.
        Absent fields are reported as such (`reported`), never invented."""
        d = task.get("data") or {}
        pid, tid = task.get("project_id"), task.get("task_id")
        lst = lambda v: [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []  # noqa: E731
        reported = [k for k in ("readiness", "obligations", "evidence", "recovered",
                                "finding_history", "budgets", "decisions") if k in d]
        ready = lst(d.get("readiness"))
        items = lst(d.get("obligations"))
        open_items = [x for x in items if x.get("status") not in self._OB_DONE]
        phases: dict[str, dict] = {}
        for x in items:
            ph = phases.setdefault(str(x.get("phase") or "?"), {"total": 0, "open": 0})
            ph["total"] += 1
            ph["open"] += x.get("status") not in self._OB_DONE
        nxt = next((x for x in open_items if x.get("next_action")), None)
        ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
        fh = d.get("finding_history") if isinstance(d.get("finding_history"), dict) else {}
        decs = lst(d.get("decisions"))
        live = {}
        for a in self.db.query("SELECT action_id, actor, status, payload, created_ts FROM cc_actions "
                               "WHERE project_id=? AND task_id=? AND kind='decision_answer' AND "
                               "status IN ('accepted','claimed','queued','delivered','applied') "
                               "ORDER BY created_ts", (pid, tid)):
            did = (_uj(a.get("payload"), {}) or {}).get("decision_id")
            if did:
                live[did] = {"action_id": a["action_id"], "status": a["status"], "by": a["actor"],
                             "ts": a["created_ts"]}
        for x in decs:
            # the page sends this back with an answer (bound to what it rendered)
            x["question_hash"] = _hash(x.get("question") or "")
            if x.get("decision_id") in live:
                x["answer_action"] = live[x["decision_id"]]
        state = task.get("state") or ""
        eff = (d.get("resume_state") or state) if state == "PAUSED" else state
        return {"reported": reported, "tables": d.get("lifecycle_tables") or [],
                "blocker_class": d.get("blocker_class"),
                # committed partial BUILD progress (not an attempt, not a decision)
                "build": self._build_continuing(d, eff),
                # the fast lane's gate / vision batches (v0.130): not Kilo sessions
                "background": self._background_view(d, eff),
                "readiness": {"items": ready,
                              "failing": sum(1 for r in ready if r.get("ok") is not True
                                             and r.get("blocking")),
                              "unknown": sum(1 for r in ready if r.get("ok") is None)},
                "checklist": {"items": items, "total": len(items), "open": len(open_items),
                              "by_phase": phases,
                              "next_action": (nxt or {}).get("next_action") or d.get("next_action"),
                              "blocking": [x for x in open_items if x.get("blocking_reason")][:10]},
                "evidence": {"counts": ev.get("counts") or {}, "checks": lst(ev.get("checks")),
                             "chains": lst(ev.get("chains")), "more": ev.get("more") or 0},
                "recovered": lst(d.get("recovered")),
                "findings": {"items": lst(fh.get("findings")), "repairs": lst(fh.get("repairs")),
                             "repeated": fh.get("repeated") or 0,
                             "approach_changed": fh.get("approach_changed")},
                "budgets": d.get("budgets") if isinstance(d.get("budgets"), dict) else None,
                "decisions": {"open": [x for x in decs if x.get("status") == "open"],
                              "closed": [x for x in decs if x.get("status") != "open"][:15]},
                "memory": lst(d.get("memory")),
                # lead recovery (v0.123): None = no case for this task;
                # lead_reported false = the coordinator has no lead tables
                "lead": self._lead_view(pid, tid, d.get("lead")) if "lead" in d else None,
                "lead_reported": "lead" in d}

    def _lead_view(self, pid, tid, raw) -> Optional[dict]:
        """The bridge's `lead` field, bounded and typed for the page, with the
        operator's in-flight case actions."""
        if not isinstance(raw, dict) or not raw.get("case_id"):
            return None
        num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None  # noqa: E731
        cid = _s(raw.get("case_id"), 120)
        st = _s(raw.get("state"), 30)
        cnt = {}
        rc = raw.get("counters") if isinstance(raw.get("counters"), dict) else {}
        for k, c in list(rc.items())[:12]:
            if isinstance(c, dict):
                cnt[_s(k, 40)] = {"used": num(c.get("used")), "max": num(c.get("max"))}
        acts = []
        for a in (raw.get("actions") if isinstance(raw.get("actions"), list) else [])[-30:]:
            if not isinstance(a, dict):
                continue
            files = a.get("scope_files") if isinstance(a.get("scope_files"), list) else []
            acts.append({"seq": num(a.get("seq")), "kind": _s(a.get("kind"), 60),
                         "actor": _s(a.get("actor"), 120), "revision": _s(a.get("revision"), 64),
                         "repo": _s(a.get("repo"), 200),
                         "scope_files": [_s(f, 300) for f in files[:40]],
                         "scope_more": _count(a.get("scope_more")),
                         "result": _s(a.get("result"), 800), "status": _s(a.get("status"), 20),
                         "ts": _f(a.get("ts"))})
        ca = raw.get("current_action") if isinstance(raw.get("current_action"), dict) else None
        up = raw.get("upgrade") if isinstance(raw.get("upgrade"), dict) else None
        live = []
        for a in self.db.query("SELECT action_id, kind, actor, status, created_ts FROM cc_actions "
                               "WHERE project_id=? AND task_id=? AND kind IN ("
                               + ",".join("?" * len(LEAD_ACTIONS)) + ") AND status IN "
                               "('accepted','claimed','queued','delivered') AND "
                               "json_extract(payload, '$.case_id')=? ORDER BY created_ts",
                               (pid, tid, *LEAD_ACTIONS, cid)):
            live.append({"action_id": a["action_id"], "kind": a["kind"], "status": a["status"],
                         "by": a["actor"], "ts": a["created_ts"]})
        return {"case_id": cid, "state": st, "known_state": st in LEAD_STATES,
                "active": st not in LEAD_TERMINAL,
                "classification": _s(raw.get("classification"), 40),
                "trigger": _s(raw.get("trigger"), 600),
                "fingerprint": _s(raw.get("fingerprint"), 120),
                "source_revision": _s(raw.get("source_revision"), 64),
                "decision_id": _s(raw.get("decision_id"), 120),
                "needs_human": raw.get("needs_human") is True,
                "lead_session_id": _s(raw.get("lead_session_id"), 160),
                "lead_model": _s(raw.get("lead_model"), 120),
                "current_action": {"kind": _s(ca.get("kind"), 60), "status": _s(ca.get("status"), 20),
                                   "seq": num(ca.get("seq")), "ts": _f(ca.get("ts")),
                                   "source": _s(ca.get("source"), 20)} if ca else None,
                "counters": cnt, "actions": acts,
                "upgrade": {"upgrade_id": _s(up.get("upgrade_id"), 120),
                            "status": _s(up.get("status"), 30),
                            "candidate_sha": _s(up.get("candidate_sha"), 64),
                            "prior_sha": _s(up.get("prior_sha"), 64),
                            "updated_ts": _f(up.get("updated_ts"))} if up else None,
                "paused": raw.get("paused") is True,
                "instructions_count": _count(raw.get("instructions_count")),
                "outcome": _s(raw.get("outcome"), 800),
                "created_ts": _f(raw.get("created_ts")), "updated_ts": _f(raw.get("updated_ts")),
                "pending_actions": live}

    def task_row(self, project: str, task: str, projects: list) -> dict:
        if projects and project not in projects:
            raise ControlError("unknown task", "not_found", 404)
        r = self.db.query_one("SELECT * FROM cc_tasks WHERE project_id=? AND task_id=?",
                              (project, task))
        if not r:
            raise ControlError("unknown task", "not_found", 404)
        r["data"] = _uj(r["data"], {})
        return r

    def task_view_for(self, project: str, task: str, projects: list) -> dict:
        r = self.task_row(project, task, projects)
        return {"project_id": project, "task_id": task, "state": r["state"],
                "received_ts": r["received_ts"], "view": self.task_view(r),
                "verification_view": self._safe_view(r), "server_ts": self.clock()}

    def task_helpers(self, project: str, task: str, projects: list) -> dict:
        """Ordinary and lane helper units of one task, with a display phase,
        the reason code and the disposition. A superset of /lanes' units."""
        r = self.task_row(project, task, projects)
        units = []
        for u in (r["data"].get("helpers") or []):
            # lane units (lane set) are listed once, from the lane observations
            if isinstance(u, dict) and not u.get("lane"):
                units.append({**u, "phase": helper_phase(u),
                              "disposition": u.get("feedback") or None, "kind_family": "ordinary"})
        lanes = []
        for x in self.db.query("SELECT unit_id, status, data FROM cc_lane_units WHERE project_id=? "
                               "AND task_id=?", (project, task)):
            dd = _uj(x["data"], {}) or {}
            u = {k: dd.get(k) for k in ("unit_id", "lane", "kind", "responsibility", "persona",
                                        "backend", "session_id", "reason", "reason_code",
                                        "next_eligible", "feedback", "started_ts", "finished_ts",
                                        "tokens")}
            u["kind_family"] = "lane"
            u.update(unit_id=x["unit_id"], status=x["status"])
            u["phase"] = helper_phase({**u, "phase": dd.get("phase")})
            u["disposition"] = u.get("feedback")
            lanes.append(u)
        pending = [u["unit_id"] for u in units + lanes
                   if u.get("status") == "delivered" and u.get("disposition") in (None, "delivered")]
        return {"units": units, "lanes": lanes, "disposition_pending": pending,
                "server_ts": self.clock()}

    def session(self, session_id: str, projects: list) -> dict:
        r = self.db.query_one("SELECT * FROM cc_sessions WHERE session_id=?", (session_id,))
        if not r:
            b = self.db.query_one("SELECT * FROM work_sessions WHERE session_id=?", (session_id,))
            if not b:
                raise ControlError("unknown session", "not_found", 404)
            r = {"session_id": session_id, "project_id": b["project_id"], "task_id": b["task_id"],
                 "run_id": b["run_id"], "role": b["role"], "parent_id": b["parent_session_id"],
                 "model": b["persona"], "binding_only": True, "data": "{}"}
        if projects and r.get("project_id") not in projects:
            raise ControlError("unknown session", "not_found", 404)
        s = self._session_out(r)
        s["children"] = [self._session_out(c) for c in self.db.query(
            "SELECT * FROM cc_sessions WHERE parent_id=? ORDER BY created_ts", (session_id,))]
        s["messages"] = []
        for m in self.db.query("SELECT * FROM cc_messages WHERE session_id=? ORDER BY created_ts",
                               (session_id,)):
            m["data"] = _uj(m["data"], {})
            s["messages"].append(m)
        s["todo"] = self.snapshot("todo", s.get("project_id") or "", session_id)
        s["pending"] = self.db.query("SELECT * FROM cc_pending WHERE session_id=? AND status='open'",
                                     (session_id,))
        for p in s["pending"]:
            p["data"] = _uj(p["data"], {})
        s["foundry"] = self.foundry_facts(session_id)
        s["persona"] = self.persona_limits(s.get("model") or "")
        at = s.get("last_activity_ts") if s.get("ended_ts") else 0
        s["kilo_config"] = self.snapshot("config", s.get("project_id") or "", "", at or 0)
        return s

    def persona_limits(self, name: str) -> Optional[dict]:
        """Foundry's declaration for the persona Kilo asked for (a declaration,
        not proof of what the served model holds)."""
        if not name:
            return None
        try:
            r = self.db.query_one("SELECT virtual_name, context_window, max_output_tokens, "
                                  "reasoning_effort, pinned_models, enabled FROM personas WHERE "
                                  "virtual_name=?", (name,))
        except Exception:                                        # noqa: BLE001
            return None
        if r:
            r["pinned_models"] = _uj(r.get("pinned_models"), [])
            r["source"] = "Foundry persona declaration"
        return r

    def task_totals(self, project: str, task: str, projects: list) -> dict:
        """Task-wide totals from two separate ledgers, never added together:
        Kilo's per-message token counts and Foundry's measured calls."""
        if projects and project not in projects:
            raise ControlError("unknown task", "not_found", 404)
        k = {"messages": 0, "input": 0, "output": 0, "reasoning": 0, "cache_read": 0,
             "cache_write": 0, "cost": 0.0, "sessions": 0}
        sids = [r["session_id"] for r in self.db.query(
            "SELECT session_id FROM cc_sessions WHERE project_id=? AND task_id=?", (project, task))]
        k["sessions"] = len(sids)
        for sid in sids:
            for m in self.db.query("SELECT data FROM cc_messages WHERE session_id=? AND "
                                   "role='assistant'", (sid,)):
                d = _uj(m["data"], {}) or {}
                t = d.get("tokens") or {}
                k["messages"] += 1
                for f in ("input", "output", "reasoning", "cache_read", "cache_write"):
                    k[f] += int(t.get(f) or 0)
                k["cost"] += float(d.get("cost") or 0)
        f = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "denied": 0, "inflight": 0}
        for r in self.db.query("SELECT status, COALESCE(input_tokens,0) AS i, "
                               "COALESCE(output_tokens,0) AS o FROM usage_events WHERE "
                               "project_id=? AND work_task_id=?", (project, task)):
            if r["status"] == "denied":
                f["denied"] += 1
            elif r["status"] == "reserved":
                f["inflight"] += 1
            elif r["status"] in ("settled", "error"):
                f["calls"] += 1
                f["input_tokens"] += r["i"]
                f["output_tokens"] += r["o"]
        return {"kilo": {**k, "source": "Kilo per-message tokens (cc_messages), assistant turns"},
                "foundry": {**f, "source": "Foundry budget ledger (usage_events), measured"},
                "note": "Two views of the same inference; do not add them."}

    def parts(self, session_id: str, projects: list, after_seq: int = 0, before_ts: float = 0,
              limit: int = 200) -> dict:
        s = self.db.query_one("SELECT project_id FROM cc_sessions WHERE session_id=?", (session_id,))
        if projects and (not s or s["project_id"] not in projects):
            raise ControlError("unknown session", "not_found", 404)
        limit = max(1, min(500, int(limit or 200)))
        if before_ts:
            rows = self.db.query("SELECT * FROM cc_parts WHERE session_id=? AND ts < ? ORDER BY ts "
                                 "DESC LIMIT ?", (session_id, before_ts, limit))
            rows.reverse()
        else:
            rows = self.db.query("SELECT * FROM cc_parts WHERE session_id=? AND seq > ? ORDER BY ts "
                                 "DESC LIMIT ?", (session_id, after_seq, limit))
            rows.reverse()
        older = bool(rows) and bool(self.db.query_one(
            "SELECT 1 AS x FROM cc_parts WHERE session_id=? AND ts < ? LIMIT 1",
            (session_id, rows[0]["ts"])))
        return {"parts": [self._part_out(r) for r in rows], "has_older": older,
                "seq": self.last_seq()}

    @staticmethod
    def _part_out(r: dict) -> dict:
        d = _uj(r.get("data"), {})
        return {"part_id": r["part_id"], "message_id": r["message_id"], "session_id": r["session_id"],
                "kind": r["kind"], "ts": r["ts"], "text": r["text"] if r["kind"] in (
                    "text", "reasoning", "retry", "error") else "", "data": d, "seq": r["seq"]}

    def search(self, q: str, projects: list, project: str = "", session: str = "",
               kind: str = "", limit: int = 50, offset: int = 0) -> dict:
        q = (q or "").strip()[:200]
        if len(q) < 2:
            raise ControlError("search needs at least 2 characters")
        where, args = ["text LIKE ? ESCAPE '\\'"], [
            "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"]
        if project:
            where.append("project_id=?")
            args.append(project)
        if session:
            where.append("session_id=?")
            args.append(session)
        if kind in PART_KINDS:
            where.append("kind=?")
            args.append(kind)
        pf, pa = self._pfilter(projects)
        limit = max(1, min(100, int(limit or 50)))
        rows = self.db.query("SELECT part_id, session_id, project_id, kind, ts, text FROM cc_parts "
                             "WHERE " + " AND ".join(where) + pf + " ORDER BY ts DESC LIMIT ? OFFSET ?",
                             (*args, *pa, limit + 1, max(0, int(offset or 0))))
        out = []
        for r in rows[:limit]:
            t = r.pop("text") or ""
            i = t.lower().find(q.lower())
            r["snippet"] = ("…" if i > 80 else "") + t[max(0, i - 80): i + len(q) + 120]
            out.append(r)
        return {"results": out, "more": len(rows) > limit, "offset": offset, "limit": limit}

    def snapshot(self, kind: str, project: str, session: str = "", at: float = 0) -> Optional[dict]:
        """Latest snapshot (or the one in force at `at`): dated, with its source
        time and when it was last confirmed unchanged."""
        args = [kind, project or "", session or ""]
        sql = "SELECT * FROM cc_snapshots WHERE kind=? AND project_id=? AND session_id=?"
        if at:
            sql += " AND source_ts <= ?"
            args.append(at)
        r = self.db.query_one(sql + " ORDER BY id DESC LIMIT 1", args)
        if not r:
            return None
        r["data"] = _uj(r["data"], {})
        return r

    def snapshot_by_id(self, sid: int) -> Optional[dict]:
        r = self.db.query_one("SELECT * FROM cc_snapshots WHERE id=?", (sid,))
        if r:
            r["data"] = _uj(r["data"], {})
        return r

    def foundry_facts(self, session_id: str, limit: int = 60) -> dict:
        """Foundry's own measured records for one Kilo session (x-session-id is
        the causal link). Each figure carries its source."""
        reqs = self.db.query(
            "SELECT r.id, r.request_id, r.ts, r.persona, r.client_model, r.mode, r.served_model, "
            "r.served_backend, r.routing, r.attempts, r.retries, r.outcome, r.status, r.duration_ms, "
            "r.est_cost_usd, r.models_used, r.error, p.prompt_tokens, p.completion_tokens, "
            "p.cached_tokens, p.reasoning_tokens, p.decode_tps, p.prefill_tps, p.decode_ms, "
            "p.prefill_ms, p.ttft_ms, p.eff_tps, p.wall_ms, p.start_ms, p.router_ms, p.timing_src, "
            "p.cache_hit_pct FROM request_log r LEFT JOIN perf_samples p ON p.request_id = "
            "r.request_id AND r.request_id IS NOT NULL WHERE r.session_id=? ORDER BY r.id DESC "
            "LIMIT ?", (session_id, limit))
        for r in reqs:
            mu = _uj(r.pop("models_used"), []) or []
            r["route_attempts"] = [{k: m.get(k) for k in ("model", "backend", "error", "ok")
                                    if isinstance(m, dict)} for m in mu][:8]
            r["error"] = _s(r.get("error"), 300)
        inflight = self.db.query("SELECT request_id, ts, persona, backend, model, account, est_tokens "
                                 "FROM usage_events WHERE session_id=? AND status='reserved' "
                                 "ORDER BY ts DESC LIMIT 20", (session_id,))
        denied = self.db.query("SELECT ts, persona, backend, model, rule, detail FROM usage_events "
                               "WHERE session_id=? AND status='denied' ORDER BY ts DESC LIMIT 20",
                               (session_id,))
        media = self.db.query("SELECT id, created_ts, finished_ts, op, status, model, backend, "
                              "persona, error FROM media_jobs WHERE session_id=? ORDER BY created_ts "
                              "DESC LIMIT 20", (session_id,))
        tot = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
               "decode_tokens": 0, "decode_ms": 0.0}
        for r in reqs:
            tot["calls"] += 1
            for k in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                tot[k] += int(r.get(k) or 0)
            if r.get("decode_ms") and r.get("completion_tokens"):
                tot["decode_tokens"] += int(r["completion_tokens"])
                tot["decode_ms"] += float(r["decode_ms"])
        tot["decode_tps_weighted"] = (round(tot["decode_tokens"] / (tot["decode_ms"] / 1000), 2)
                                      if tot["decode_ms"] else None)
        tot["cache_hit_pct"] = (round(100 * tot["cached_tokens"] / tot["prompt_tokens"], 1)
                                if tot["prompt_tokens"] else None)
        return {"source": "Foundry ledger (request_log + perf_samples + usage_events + media_jobs, "
                          "joined by x-session-id / request_id)",
                "requests": reqs, "inflight": inflight, "denied": denied, "media": media,
                "totals": tot, "window": f"last {limit} requests"}

    def route_graph(self, projects: list, project: str = "", task: str = "",
                    minutes: float = 30) -> dict:
        """Nodes and edges from causal ids only. An edge is `active` only while
        Foundry holds a reserved (in-flight) ledger row or a running media job."""
        now = self.clock()
        since = now - minutes * 60
        pf, pa = self._pfilter(projects)
        where = " AND project_id=?" if project else ""
        wa = (project,) if project else ()
        if task:
            where += " AND task_id=?"
            wa += (task,)
        sess = [self._session_out(r) for r in self.db.query(
            f"SELECT * FROM cc_sessions WHERE COALESCE(last_activity_ts, created_ts, 0) >= ?"
            f"{where}{pf} ORDER BY created_ts", (since, *wa, *pa))]
        ids = [s["session_id"] for s in sess]
        nodes: dict[str, dict] = {"coordinator": {"id": "coordinator", "kind": "coordinator",
                                                  "label": "Coordinator"}}
        edges: dict[str, dict] = {}

        def edge(a, b, kind, state, ref, label=""):
            k = f"{a}->{b}"
            e = edges.setdefault(k, {"id": k, "from": a, "to": b, "kind": kind, "state": state,
                                     "refs": [], "label": label, "count": 0})
            e["count"] += 1
            rank = {"active": 5, "queued": 4, "failed": 3, "fallback": 2, "deferred": 2,
                    "waiting": 2, "completed": 1, "stale": 0}
            if rank.get(state, 0) > rank.get(e["state"], 0):
                e["state"] = state
            if len(e["refs"]) < 30:
                e["refs"].append(ref)

        for s in sess:
            sid = s["session_id"]
            nodes[f"s:{sid}"] = {"id": f"s:{sid}", "kind": "session", "label":
                                 f"{s.get('role') or 'session'} {sid[-8:]}", "ref": {"session": sid},
                                 "status": s.get("status")}
            parent = s.get("parent_id")
            if parent and parent in ids:
                edge(f"s:{parent}", f"s:{sid}", "delegation",
                     "active" if s.get("status") in ("busy", "retry") else "completed",
                     {"session": sid})
            elif not parent:
                edge("coordinator", f"s:{sid}", "lifecycle",
                     "active" if s.get("status") in ("busy", "retry") else "completed",
                     {"session": sid})
        if ids:
            qmarks = ",".join("?" * len(ids))
            for r in self.db.query(f"SELECT request_id, session_id, persona, served_backend, "
                                   f"served_model, outcome, status, attempts, models_used, ts FROM "
                                   f"request_log WHERE session_id IN ({qmarks}) ORDER BY id DESC "
                                   f"LIMIT 400", ids):
                pnode = f"p:{r['persona'] or '?'}"
                nodes.setdefault(pnode, {"id": pnode, "kind": "persona", "label": r["persona"] or "?"})
                bnode = f"b:{r['served_backend'] or '?'}"
                nodes.setdefault(bnode, {"id": bnode, "kind": "backend",
                                         "label": r["served_backend"] or "?"})
                mnode = f"m:{r['served_backend']}/{r['served_model']}"
                nodes.setdefault(mnode, {"id": mnode, "kind": "model", "label": r["served_model"] or "?"})
                ok = (r.get("status") or "ok") == "ok" and r.get("outcome") in (None, "", "ok")
                mu = _uj(r.get("models_used"), []) or []
                fell = len(mu) > 1 or int(r.get("attempts") or 1) > 1
                ref = {"request": r["request_id"], "session": r["session_id"]}
                edge(f"s:{r['session_id']}", pnode, "inference", "completed" if ok else "failed", ref)
                edge(pnode, bnode, "inference", ("fallback" if fell else "completed") if ok
                     else "failed", ref, "fallback" if fell else "")
                edge(bnode, mnode, "inference", "completed" if ok else "failed", ref)
                for m in mu[:-1] if fell else []:
                    if isinstance(m, dict) and m.get("backend"):
                        fb = f"b:{m['backend']}"
                        nodes.setdefault(fb, {"id": fb, "kind": "backend", "label": m["backend"]})
                        edge(pnode, fb, "inference", "failed", {**ref, "error": _s(m.get("error"), 200)},
                             "failed attempt")
            for r in self.db.query(f"SELECT request_id, session_id, persona, backend, model, ts FROM "
                                   f"usage_events WHERE status='reserved' AND session_id IN ({qmarks})",
                                   ids):
                pnode = f"p:{r['persona'] or '?'}"
                bnode = f"b:{r['backend'] or '?'}"
                mnode = f"m:{r['backend']}/{r['model']}"
                for n, k, lab in ((pnode, "persona", r["persona"]), (bnode, "backend", r["backend"]),
                                  (mnode, "model", r["model"])):
                    nodes.setdefault(n, {"id": n, "kind": k, "label": lab or "?"})
                ref = {"request": r["request_id"], "session": r["session_id"], "inflight": True}
                edge(f"s:{r['session_id']}", pnode, "inference", "active", ref)
                edge(pnode, bnode, "inference", "active", ref)
                edge(bnode, mnode, "inference", "active", ref)
            for r in self.db.query(f"SELECT id, session_id, status, backend, model, op FROM media_jobs "
                                   f"WHERE session_id IN ({qmarks}) AND created_ts >= ?", (*ids, since)):
                jn = f"j:{r['id']}"
                nodes[jn] = {"id": jn, "kind": "media_job", "label": f"{r['op']} {r['id'][-6:]}",
                             "ref": {"media_job": r["id"]}}
                st = {"running": "active", "queued": "queued", "succeeded": "completed",
                      "failed": "failed", "cancelled": "failed"}.get(r["status"], "completed")
                edge(f"s:{r['session_id']}", jn, "media", st, {"media_job": r["id"]})
            # MCP / tool edges: only from tool parts whose server the bridge matched
            # to a configured MCP server name
            for r in self.db.query(f"SELECT part_id, session_id, data FROM cc_parts WHERE kind='tool' "
                                   f"AND session_id IN ({qmarks}) AND ts >= ? ORDER BY ts DESC "
                                   f"LIMIT 600", (*ids, since)):
                t = (_uj(r["data"], {}) or {}).get("tool") or {}
                srv = t.get("mcp_server")
                if not srv:
                    continue
                mn = f"mcp:{srv}"
                nodes.setdefault(mn, {"id": mn, "kind": "mcp", "label": srv})
                st = {"running": "active", "pending": "queued", "completed": "completed",
                      "error": "failed"}.get(t.get("status"), "completed")
                edge(f"s:{r['session_id']}", mn, "tool", st,
                     {"part": r["part_id"], "session": r["session_id"], "tool": t.get("name")})
        self._lane_lineage(nodes, edge, ids, since, where, wa, pf, pa)
        unassoc = self.db.query("SELECT id, op, status, backend, created_ts FROM media_jobs WHERE "
                                "(session_id IS NULL OR session_id='') AND created_ts >= ? LIMIT 20",
                                (since,))
        return {"nodes": list(nodes.values()), "edges": list(edges.values()), "server_ts": now,
                "window_min": minutes, "unassociated_media": unassoc,
                "note": "Inference edges come from Foundry's request_log / usage_events joined by "
                        "x-session-id; MCP edges from tool calls whose name matched a configured "
                        "server; media edges from media_jobs.session_id. Nothing is joined by time "
                        "alone. 'active' = an in-flight ledger row or running job right now."}

    def _lane_lineage(self, nodes, edge, ids, since, where, wa, pf, pa) -> None:
        """artifact -> observer -> proposer -> primary -> recapture, from the
        coordinator's own unit/artifact ids. A unit edge is 'active' only while
        Foundry holds an in-flight ledger row for that unit's session (or Kilo
        reports the helper session busy); a running unit with neither is
        'waiting', never animated."""
        units = [dict(r, data=_uj(r["data"], {})) for r in self.db.query(
            f"SELECT * FROM cc_lane_units WHERE COALESCE(updated_ts, received_ts) >= ?{where}{pf}",
            (since, *wa, *pa))]
        if not units:
            return
        sids = [u["session_id"] for u in units if u["session_id"]]
        inflight = set()
        busy = set()
        if sids:
            q = ",".join("?" * len(sids))
            inflight = {r["session_id"] for r in self.db.query(
                f"SELECT DISTINCT session_id FROM usage_events WHERE status='reserved' AND "
                f"session_id IN ({q})", sids)}
            busy = {r["session_id"] for r in self.db.query(
                f"SELECT session_id FROM cc_sessions WHERE status IN ('busy','retry') AND "
                f"session_id IN ({q})", sids)}
        arts = {}
        for u in units:
            d = u["data"]
            un = f"u:{u['unit_id']}"
            nodes[un] = {"id": un, "kind": "lane_unit", "label": f"{u['lane']} {u['unit_id'][-6:]}",
                         "ref": {"unit": u["unit_id"], "session": u["session_id"]},
                         "status": u["status"], "feedback": d.get("feedback"),
                         "persona": d.get("persona"), "backend": d.get("backend")}
            st = LANE_STATUS_EDGE.get(u["status"], "completed")
            if u["status"] == "running" and (u["session_id"] in inflight or u["session_id"] in busy):
                st = "active"
            if d.get("feedback") in ("superseded", "expired"):
                st = "stale"
            ref = {"unit": u["unit_id"], "status": u["status"], "feedback": d.get("feedback"),
                   "reason": _s(d.get("reason"), 200)}
            if u["artifact_id"] and not u["parent_unit"]:
                an = f"a:{u['artifact_id']}"
                arts[u["artifact_id"]] = an
                nodes.setdefault(an, {"id": an, "kind": "artifact", "label":
                                      f"{_s(d.get('artifact_path') or 'capture', 40)} "
                                      f"gen {d.get('artifact_generation') or '?'}",
                                      "ref": {"artifact": u["artifact_id"]}})
                edge(an, un, "lineage", st, ref, "observe")
            if u["parent_unit"]:
                edge(f"u:{u['parent_unit']}", un, "lineage", st, ref, "propose")
            elif u["parent_session"] and u["parent_session"] in ids:
                edge(f"s:{u['parent_session']}", un, "lineage", st, ref, u["lane"])
            if u["session_id"] and u["session_id"] in ids:
                edge(un, f"s:{u['session_id']}", "lineage", st, ref, "session")
            if d.get("delivered_session"):
                tgt = f"s:{d['delivered_session']}"
                if d["delivered_session"] in ids:
                    edge(un, tgt, "delivery", "completed", ref,
                         d.get("feedback") if d.get("feedback") in ("integrated", "rejected",
                                                                    "deferred") else "delivered")
        for r in self.db.query(f"SELECT artifact_id, data FROM cc_artifacts WHERE "
                               f"received_ts >= ?{where}{pf}", (since, *wa, *pa)):
            d = _uj(r["data"], {})
            prev = d.get("prev_artifact")
            if prev and prev in arts and r["artifact_id"] in arts:
                edge(arts[prev], arts[r["artifact_id"]], "lineage", "completed",
                     {"artifact": r["artifact_id"]}, "recapture")

    def feed(self, after: int, projects: list, sessions: Optional[Iterable[str]] = None,
             limit: int = 300) -> tuple[list[dict], int]:
        """Changes after a cursor, coalesced to the latest per entity, with the
        current entity. Parts are sent only for subscribed sessions."""
        pf, pa = self._pfilter(projects)
        if projects:
            pf = f" AND (project_id='' OR project_id IN ({','.join('?' * len(projects))}))"
        rows = self.db.query(f"SELECT MAX(seq) AS seq, kind, entity_id, session_id FROM cc_feed "
                             f"WHERE seq > ?{pf} GROUP BY kind, entity_id ORDER BY seq LIMIT ?",
                             (after, *pa, max(1, min(1000, limit))))
        subs = set(sessions or ())
        out = []
        last = after
        for r in rows:
            last = max(last, r["seq"])
            k, eid = r["kind"], r["entity_id"]
            if k in ("part", "message") and r["session_id"] not in subs:
                continue
            ent = self._entity(k, eid)
            if k == "source" and projects and ent is not None and not set(
                    (ent.get("detail") or {}).get("projects") or []) & set(projects):
                continue
            if ent is not None:
                out.append({"seq": r["seq"], "kind": k, "id": eid, "entity": ent})
        return out, last

    def _entity(self, kind: str, eid: str):
        if kind == "part":
            r = self.db.query_one("SELECT * FROM cc_parts WHERE part_id=?", (eid,))
            return self._part_out(r) if r else None
        if kind == "message":
            r = self.db.query_one("SELECT * FROM cc_messages WHERE message_id=?", (eid,))
            if r:
                r["data"] = _uj(r["data"], {})
            return r
        if kind == "session":
            r = self.db.query_one("SELECT * FROM cc_sessions WHERE session_id=?", (eid,))
            return self._session_out(r) if r else None
        if kind == "task":
            p, _, t = eid.partition("/")
            r = self.db.query_one("SELECT * FROM cc_tasks WHERE project_id=? AND task_id=?", (p, t))
            if r:
                r["data"] = _uj(r["data"], {})
                self._with_views(r)
            return r
        if kind == "transition":
            p, t, n = eid.split("/", 2)
            r = self.db.query_one("SELECT * FROM cc_transitions WHERE project_id=? AND task_id=? "
                                  "AND src_id=?", (p, t, int(n)))
            if r:
                r["data"] = _uj(r["data"], {})
            return r
        if kind == "pending":
            r = self.db.query_one("SELECT * FROM cc_pending WHERE request_id=?", (eid,))
            if r:
                r["data"] = _uj(r["data"], {})
            return r
        if kind == "alert":
            return self.alert(eid)
        if kind == "action":
            return self.action_view(eid)
        if kind == "snapshot":
            return self.snapshot_by_id(int(eid))
        if kind == "lane_unit":
            r = self.db.query_one("SELECT * FROM cc_lane_units WHERE unit_id=?", (eid,))
            return ({**_uj(r["data"], {}), "unit_id": r["unit_id"], "status": r["status"]}
                    if r else None)
        if kind == "artifact":
            r = self.db.query_one("SELECT artifact_id, project_id, task_id, rel_path, generation, "
                                  "sha256, status, data, mime FROM cc_artifacts WHERE artifact_id=?",
                                  (eid,))
            return {**_uj(r.pop("data"), {}), **r} if r else None
        if kind == "lane_state":
            r = self.db.query_one("SELECT * FROM cc_lane_state WHERE project_id=?", (eid,))
            return {"project_id": eid, "paused": bool(r["paused"]), **_uj(r["data"], {})} \
                if r else None
        if kind == "project_status":
            r = self.db.query_one("SELECT * FROM cc_project_status WHERE project_id=?", (eid,))
            return self._project_status_out(r) if r else None
        if kind == "source":
            for s in self.sources():
                if s["source_id"] == eid:
                    return s
        return None

    # ------------------------------------------------------------------ operator sessions

    def login(self, key: str, client: Optional[dict]) -> dict:
        if not client or not ({"operator", "admin"} & set(client.get("scopes") or [])):
            raise ControlError("the control center needs a client key with the operator (or "
                               "admin) scope", "operator_key_required", 401)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = self.clock()
        hours = self.settings()["session_hours"]
        self.db.execute("INSERT INTO cc_op_sessions (token_hash, actor, client_id, scopes, projects, "
                        "csrf, created_ts, expires_ts, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                        (hashlib.sha256(token.encode()).hexdigest(), client["client_id"],
                         client["client_id"], _j(client.get("scopes") or []),
                         _j(client.get("projects") or []), csrf, now, now + hours * 3600, now))
        self.db.execute("DELETE FROM cc_op_sessions WHERE expires_ts < ?", (now,))
        return {"token": token, "csrf": csrf, "actor": client["client_id"],
                "expires_ts": now + hours * 3600, "projects": client.get("projects") or []}

    def op_session(self, token: str) -> Optional[dict]:
        if not token:
            return None
        r = self.db.query_one("SELECT * FROM cc_op_sessions WHERE token_hash=?",
                              (hashlib.sha256(token.encode()).hexdigest(),))
        if not r or (r["expires_ts"] or 0) < self.clock():
            return None
        r["scopes"] = _uj(r["scopes"], [])
        r["projects"] = _uj(r["projects"], [])
        return r

    def logout(self, token: str) -> None:
        self.db.execute("DELETE FROM cc_op_sessions WHERE token_hash=?",
                        (hashlib.sha256((token or "").encode()).hexdigest(),))

    def rate_ok(self, actor: str) -> bool:
        now = self.clock()
        per = self.settings()["rate_per_minute"]
        with self._rlock:
            hits = [t for t in self._rate.get(actor, []) if now - t < 60]
            if len(hits) >= per:
                self._rate[actor] = hits
                return False
            hits.append(now)
            self._rate[actor] = hits
        return True

    # drafts: kept server-side for the signed-in operator only (not in browser storage)

    def save_draft(self, actor: str, target: str, text: str) -> None:
        target = _s(target, 200)
        if not text:
            self.db.execute("DELETE FROM cc_drafts WHERE actor=? AND target=?", (actor, target))
            return
        self.db.execute("INSERT INTO cc_drafts (actor, target, text, updated_ts) VALUES (?,?,?,?) "
                        "ON CONFLICT(actor, target) DO UPDATE SET text=excluded.text, "
                        "updated_ts=excluded.updated_ts",
                        (actor, target, _cap(text, self.settings()["max_instruction_chars"])[0],
                         self.clock()))

    def drafts(self, actor: str) -> list[dict]:
        return self.db.query("SELECT target, text, updated_ts FROM cc_drafts WHERE actor=? ORDER BY "
                             "updated_ts DESC LIMIT 50", (actor,))

    # ------------------------------------------------------------------ actions

    def create_action(self, actor: str, projects: list, body: dict,
                      scopes: Optional[list] = None) -> dict:
        """Accept a typed operator action after checking it against what Foundry
        currently knows. The coordinator checks it again when it applies it."""
        kind = body.get("kind")
        if kind not in ACTION_KINDS:
            raise ControlError(f"kind must be one of {', '.join(ACTION_KINDS)}")
        idem = _s(body.get("idempotency_key"), 80)
        if len(idem) < 8:
            raise ControlError("idempotency_key (8+ chars) is required")
        existing = self.db.query_one("SELECT action_id FROM cc_actions WHERE idem_key=?", (idem,))
        if existing:
            return {**self.action_view(existing["action_id"]), "duplicate": True}
        pid, tid = _s(body.get("project_id"), 120), _s(body.get("task_id"), 120)
        if kind in PROJECT_ACTIONS:
            return self._create_project_action(actor, projects, body, scopes, kind, idem, pid, tid)
        if kind in ASSISTANT_ACTIONS:
            return self._create_assistant_action(actor, projects, body, scopes, kind, idem, pid)
        if kind in CLIENT_ACTIONS:
            return self._create_client_action(actor, projects, body, scopes, kind, idem)
        if kind in MEMORY_ACTIONS:
            return self._create_memory_action(actor, projects, body, scopes, kind, idem, pid)
        if not pid or not tid:
            raise ControlError("project_id and task_id are required")
        if projects and pid not in projects:
            raise ControlError("this operator may not act on that project", "forbidden", 403)
        task = self.db.query_one("SELECT * FROM cc_tasks WHERE project_id=? AND task_id=?", (pid, tid))
        if not task:
            raise ControlError(f"{pid}/{tid} has not been reported by the coordinator bridge",
                               "unknown_task", 404)
        tdata = _uj(task["data"], {})
        expected = dict(body.get("expected") or {})
        unbound = ("lanes", "decision_answer", *LEAD_ACTIONS)
        if kind in unbound:
            # lanes: a project-wide switch the task id only routes; decision_answer:
            # bound to the decision itself (its open status), not the task state;
            # lead case actions: bound to the recovery case (its id and state)
            expected.pop("task_state", None)
        else:
            expected.setdefault("task_state", task["state"])
        if kind not in unbound and expected["task_state"] != task["state"]:
            raise ControlError(f"{tid} is now {task['state']} (you saw {expected['task_state']}); "
                               "review the current state first", "stale_state", 409,
                               current=task["state"])
        payload: dict = {}
        sid = _s(body.get("session_id") or tdata.get("session_id"), 160)
        rid = ""
        st = self.settings()
        if kind in ("answer_question", "reply_permission"):
            rid = _s(body.get("request_id"), 160)
            p = self.db.query_one("SELECT * FROM cc_pending WHERE request_id=?", (rid,))
            want = "question" if kind == "answer_question" else "permission"
            if not p or p["kind"] != want:
                raise ControlError(f"no pending {want} {rid!r}", "unknown_request", 404)
            if p["status"] != "open":
                cur = self.db.query("SELECT request_id FROM cc_pending WHERE session_id=? AND "
                                    "status='open'", (p["session_id"],))
                raise ControlError("that request is no longer pending (answered, cancelled or "
                                   "replaced); your draft is kept", "stale_request", 409,
                                   current=[c["request_id"] for c in cur])
            if p["project_id"] != pid or (p["task_id"] and p["task_id"] != tid):
                raise ControlError("request belongs to another task", "wrong_target", 409)
            if sid and p["session_id"] != sid:
                raise ControlError("request belongs to another session", "wrong_target", 409)
            sid = p["session_id"]
            if kind == "answer_question":
                qs = (_uj(p["data"], {}) or {}).get("questions") or []
                ans = body.get("answers")
                if not isinstance(ans, list) or (qs and len(ans) != len(qs)) or not all(
                        isinstance(a, list) and a and all(isinstance(x, str) for x in a) for a in ans):
                    raise ControlError(f"answers must be one list of strings per question "
                                       f"({len(qs) or 1} expected)")
                if sum(len(x) for a in ans for x in a) > st["max_instruction_chars"]:
                    raise ControlError(f"answer is over {st['max_instruction_chars']} characters")
                payload = {"answers": ans}
            else:
                reply = body.get("reply")
                if reply not in PERMISSION_REPLIES:
                    raise ControlError("reply must be 'once' or 'reject' (persistent 'always' rules "
                                       "are not created from the control center)")
                payload = {"reply": reply, "message": _s(body.get("message"), 2000)}
            expected.update(request_id=rid, session_id=sid)
        elif kind in ("approve_review", "reject_review"):
            ap = tdata.get("approval") or {}
            ak = body.get("approval_kind")
            if task["state"] != "AWAITING_HUMAN" or ak not in (ap.get("kinds") or []):
                raise ControlError(f"{tid} is not waiting for a {ak!r} approval", "stale_state", 409)
            rev = body.get("revision")
            arts = body.get("artifacts") or {}
            # artifacts are reported per approval kind: {kind: {path: sha256}}
            if rev != ap.get("revision") or arts != ((ap.get("artifacts") or {}).get(ak) or {}):
                raise ControlError("the revision or evidence artifacts changed since this decision "
                                   "card was opened; reopen it and review again",
                                   "stale_evidence", 409)
            payload = {"approval_kind": ak, "revision": rev, "artifacts": arts,
                       "note": _s(body.get("note"), 4000)}
            if kind == "reject_review":
                fs = [_s(f, 2000) for f in (body.get("findings") or []) if str(f).strip()][:20]
                if not fs:
                    raise ControlError("say what is wrong (findings) when rejecting")
                payload.update(findings=fs, mode="request_changes" if body.get("mode") ==
                               "request_changes" else "reject")
            expected.update(revision=rev, artifacts_hash=_hash(arts))
        elif kind in ("pause", "cancel"):
            reason = _s(body.get("reason"), 500).strip()
            if not reason:
                raise ControlError("a reason is required")
            if kind == "cancel" and body.get("confirm") != f"cancel {tid}":
                raise ControlError(f"type 'cancel {tid}' to confirm cancelling for good")
            if task["state"] in ("COMPLETE", "CANCELLED"):
                raise ControlError(f"{tid} is {task['state']}", "stale_state", 409)
            payload = {"reason": reason}
        elif kind == "resume":
            if task["state"] != "PAUSED":
                raise ControlError(f"{tid} is {task['state']}, not PAUSED", "stale_state", 409)
        elif kind == "unblock":
            if task["state"] not in ("BLOCKED", "FAILED"):
                raise ControlError(f"{tid} is {task['state']}, not BLOCKED/FAILED", "stale_state", 409)
            res = _s(body.get("resolution"), 1000).strip()
            if not res:
                raise ControlError("say what was fixed (resolution)")
            payload = {"resolution": res, "reset_attempts": bool(body.get("reset_attempts"))}
        elif kind == "instruction":
            text = str(body.get("text") or "")
            if not text.strip():
                raise ControlError("instruction text is empty")
            if len(text) > st["max_instruction_chars"]:
                raise ControlError(f"instruction is over {st['max_instruction_chars']} characters")
            delivery = body.get("delivery") or "next_session"
            if delivery not in ("next_session", "pause_and_steer"):
                raise ControlError("delivery must be next_session or pause_and_steer")
            scope = body.get("scope") or "task"
            if scope not in ("task", "session"):
                raise ControlError("scope must be task or session")
            if delivery == "pause_and_steer" and task["state"] not in ("IMPLEMENTING", "CLOSING"):
                raise ControlError(f"pause-and-steer needs a running worker; {tid} is "
                                   f"{task['state']} (queue it for the next session instead)",
                                   "stale_state", 409)
            if scope == "session" and delivery != "pause_and_steer":
                raise ControlError("a session-specific instruction can only be delivered by "
                                   "pause-and-steer to that session")
            if scope == "session" and sid != tdata.get("session_id"):
                raise ControlError("that session is no longer the task's active session",
                                   "stale_session", 409)
            payload = {"text": text, "delivery": delivery, "scope": scope}
            expected.update(session_id=sid if scope == "session" or delivery == "pause_and_steer"
                            else "")
        elif kind == "lanes":
            # operational: pause/un-pause proactive helper lanes for the project
            # (never enables a lane the profile has off)
            payload = {"paused": bool(body.get("paused", True)), "note": _s(body.get("note"), 500)}
            expected.pop("task_state", None)
        elif kind == "decision_answer":
            # authority is the operator, always: the signed-in session supplies the
            # actor; a body claiming any other authority (a policy, a model) is refused
            if scopes is not None and not {"operator", "admin"} & set(scopes or []):
                raise ControlError("answering a decision needs an operator session", "forbidden", 403)
            if body.get("authority") not in (None, "", DECISION_AUTHORITY):
                raise ControlError("decisions are answered here with operator authority only",
                                   "authority_not_operator", 403)
            did = _s(body.get("decision_id"), 120)
            dec = next((x for x in (tdata.get("decisions") or []) if isinstance(x, dict)
                        and x.get("decision_id") == did), None) if did else None
            if not dec:
                raise ControlError(f"no decision {did!r} reported for {tid}", "unknown_decision", 404)
            if dec.get("status") != "open":
                raise ControlError(f"decision {did} is {dec.get('status')}, not open",
                                   "stale_decision", 409, current=dec.get("status"))
            # bound to the question the operator was shown: the page sends the
            # hash it rendered (task_view decisions carry it); none, or another
            # one, is refused, never filled in here
            qh = _hash(dec.get("question") or "")
            if not body.get("question_hash"):
                raise ControlError("question_hash is required: answer from the rendered question",
                                   "question_hash_required", 400)
            if str(body["question_hash"]) != qh:
                raise ControlError("the question changed since you opened it; read it again",
                                   "stale_decision", 409, current_hash=qh)
            option = _s(body.get("option"), 300)
            opts = [str(x) for x in (dec.get("options") or [])]
            if option and opts and option not in opts:
                raise ControlError("option is not one of the decision's options")
            answer = str(body.get("answer") or "").strip()
            if not answer and not option:
                raise ControlError("choose an option or write an answer")
            if len(answer) > st["max_instruction_chars"]:
                raise ControlError(f"answer is over {st['max_instruction_chars']} characters")
            if self.db.query_one("SELECT action_id FROM cc_actions WHERE project_id=? AND task_id=? "
                                 "AND kind='decision_answer' AND status IN ('accepted','claimed',"
                                 "'queued','delivered','applied') AND json_extract(payload, "
                                 "'$.decision_id')=?", (pid, tid, did)):
                raise ControlError("an answer to this decision is already in progress or applied",
                                   "duplicate_answer", 409)
            payload = {"decision_id": did, "decision_kind": _s(dec.get("kind"), 60),
                       "answer": answer, "option": option, "note": _s(body.get("note"), 2000),
                       "authority": DECISION_AUTHORITY, "actor": actor, "question_hash": qh,
                       "revision": _s(body.get("revision") or tdata.get("head_sha"), 64)}
            expected.update(decision_id=did, decision_status="open", question_hash=qh)
        elif kind in LEAD_ACTIONS:
            payload = self._lead_payload(kind, body, tdata, scopes, tid)
            expected = {"case_id": payload["case_id"],
                        "case_state": _s((tdata.get("lead") or {}).get("state"), 30)}
        elif kind == "withdraw_instruction":
            target = _s(body.get("target_action_id"), 80)
            t = self.db.query_one("SELECT * FROM cc_actions WHERE action_id=? AND kind='instruction'",
                                  (target,))
            if not t or t["project_id"] != pid:
                raise ControlError("no such instruction", "not_found", 404)
            if t["status"] == "accepted":
                return self.cancel_action(target, actor, projects)
            if t["status"] != "queued":
                raise ControlError(f"instruction is {t['status']}; only a queued one can be withdrawn",
                                   "stale_state", 409)
            payload = {"target_action_id": target}
        if not self.rate_ok(actor):
            raise ControlError("too many control actions; wait a minute", "rate_limited", 429)
        now = self.clock()
        aid = f"act-{uuid.uuid4().hex[:16]}"
        summary = self._summary(kind, payload, tid)
        try:
            with self._tx() as c:
                if rid and self._rows(c, "SELECT action_id FROM cc_actions WHERE request_id=? AND "
                                         "status IN ('accepted','claimed','queued','delivered',"
                                         "'applied')", (rid,)):
                    raise ControlError("another reply to this request is already in progress or "
                                       "delivered (another tab?)", "duplicate_reply", 409)
                seq = self._feed(c, "action", pid, sid, aid)
                c.execute("INSERT INTO cc_actions (action_id, idem_key, actor, kind, project_id, "
                          "task_id, run_id, session_id, request_id, payload, summary, expected, "
                          "status, status_detail, created_ts, expires_ts, updated_ts, seq) VALUES "
                          "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (aid, idem, actor, kind, pid, tid, _s(tdata.get("run_id"), 120), sid, rid,
                           _j(payload), summary, _j(expected), "accepted",
                           "stored by Foundry; waiting for the coordinator to claim it", now,
                           now + st["action_ttl_s"], now, seq))
                c.execute("INSERT INTO cc_action_log (action_id, ts, status, actor, detail) VALUES "
                          "(?,?,?,?,?)", (aid, now, "accepted", actor, summary))
                if kind == "unblock" or kind in LEAD_ACTIONS:
                    # the task's derived recovery view changes with this action
                    self._feed(c, "task", pid, "", f"{pid}/{tid}")
        except Exception as e:                                   # noqa: BLE001
            if "UNIQUE" in str(e) and "request" in str(e):
                raise ControlError("another reply to this request is already in progress",
                                   "duplicate_reply", 409) from None
            if "UNIQUE" in str(e) and "idem" in str(e):
                ex = self.db.query_one("SELECT action_id FROM cc_actions WHERE idem_key=?", (idem,))
                return {**self.action_view(ex["action_id"]), "duplicate": True}
            raise
        self._notify()
        return self.action_view(aid)

    @staticmethod
    def _lead_payload(kind: str, body: dict, tdata: dict, scopes, tid: str) -> dict:
        """Check a lead case action against the case the bridge reported for
        this task: operator sessions only, the task's own case (project
        isolation is create_action's project check), still active."""
        if scopes is not None and not {"operator", "admin"} & set(scopes or []):
            raise ControlError("lead recovery controls need an operator session", "forbidden", 403)
        cid = _s(body.get("case_id"), 120)
        if not cid:
            raise ControlError("case_id is required")
        lead = tdata.get("lead") if isinstance(tdata.get("lead"), dict) else None
        if not lead or _s(lead.get("case_id"), 120) != cid:
            raise ControlError(f"no recovery case {cid!r} reported for {tid}", "unknown_case", 404,
                               current=_s((lead or {}).get("case_id"), 120))
        st = _s(lead.get("state"), 30)
        if st in LEAD_TERMINAL:
            raise ControlError(f"case {cid} is {st}", "stale_case", 409, current=st)
        exp = (body.get("expected") or {}).get("case_state")
        if exp and exp != st:
            raise ControlError(f"case {cid} is now {st} (you saw {exp}); review it first",
                               "stale_case", 409, current=st)
        note = str(body.get("note") or "")
        if len(note) > MAX_LEAD_NOTE:
            raise ControlError(f"note is over {MAX_LEAD_NOTE} characters")
        if kind == "lead_case_instruct" and not note.strip():
            raise ControlError("write the instruction (note)")
        if kind == "lead_case_pause" and lead.get("paused") is True:
            raise ControlError(f"case {cid} is already paused", "duplicate", 409)
        if kind == "lead_case_resume" and lead.get("paused") is not True:
            raise ControlError(f"case {cid} is not paused", "not_paused", 409)
        return {"case_id": cid, "note": note}

    def _project_pause_state(self, c, pid: str) -> tuple[Optional[dict], Optional[dict]]:
        """(the latest live-or-applied project_pause/resume action, the reported
        status) for one project."""
        last = self._rows(c, "SELECT action_id, kind, status, created_ts FROM cc_actions WHERE project_id=? AND "
                             "kind IN ('project_pause','project_resume') AND status IN ('accepted',"
                             "'claimed','queued','delivered','applied') ORDER BY created_ts DESC, "
                             "rowid DESC LIMIT 1", (pid,))
        st = self._rows(c, "SELECT data FROM cc_project_status WHERE project_id=?", (pid,))
        return (last[0] if last else None), (_uj(st[0]["data"], {}) if st else None)

    def _create_assistant_action(self, actor: str, projects: list, body: dict, scopes,
                                 kind: str, idem: str, pid: str) -> dict:
        if scopes is not None and not {"operator", "admin"} & set(scopes or []):
            raise ControlError("the launch assistant needs an operator session", "forbidden", 403)
        if not pid:
            raise ControlError("project_id is required")
        if projects and pid not in projects:
            raise ControlError("this operator may not act on that project", "forbidden", 403)
        if kind == "assistant_plan":
            text = str(body.get("text") or "").strip()
            if not text:
                raise ControlError("say what you want done")
            if len(text) > MAX_ASSISTANT_TEXT:
                raise ControlError(f"request is over {MAX_ASSISTANT_TEXT} characters")
            payload = {"text": text}
            summary = f"launch assistant: plan for {pid} ({len(text)} chars)"
        else:
            plan_id = str(body.get("plan_id") or "")
            if not re.fullmatch(r"asp-[0-9a-f]{12}", plan_id):
                raise ControlError("plan_id is required (from the assistant's plan)")
            steps = body.get("steps")
            if steps is not None and (not isinstance(steps, list) or not all(
                    isinstance(i, int) and 0 <= i < 50 for i in steps)):
                raise ControlError("steps must be a list of step indexes")
            payload = {"plan_id": plan_id, "steps": steps}
            summary = f"launch assistant: run {plan_id} on {pid}"
        if not self.rate_ok(actor):
            raise ControlError("too many control actions; wait a minute", "rate_limited", 429)
        st = self.settings()
        now = self.clock()
        aid = f"act-{uuid.uuid4().hex[:16]}"
        try:
            with self._tx() as c:
                seq = self._feed(c, "action", pid, "", aid)
                c.execute("INSERT INTO cc_actions (action_id, idem_key, actor, kind, project_id, "
                          "task_id, run_id, session_id, request_id, payload, summary, expected, "
                          "status, status_detail, created_ts, expires_ts, updated_ts, seq) VALUES "
                          "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (aid, idem, actor, kind, pid, "", "", "", "", _j(payload), summary,
                           _j({}), "accepted",
                           "stored by Foundry; waiting for the coordinator to claim it", now,
                           now + st["action_ttl_s"], now, seq))
                c.execute("INSERT INTO cc_action_log (action_id, ts, status, actor, detail) "
                          "VALUES (?,?,?,?,?)", (aid, now, "accepted", actor, summary))
        except Exception as e:                                   # noqa: BLE001
            if "UNIQUE" in str(e) and "idem" in str(e):
                ex = self.db.query_one("SELECT action_id FROM cc_actions WHERE idem_key=?", (idem,))
                return {**self.action_view(ex["action_id"]), "duplicate": True}
            raise
        self._notify()
        return self.action_view(aid)

    def client_sources(self) -> list[dict]:
        """Foundry Clients that heart-beat (kind client), freshest first."""
        return sorted([s for s in self.sources([]) if s["kind"] == "client"],
                      key=lambda s: -(s.get("last_seen") or 0))

    def _create_memory_action(self, actor: str, projects: list, body: dict, scopes, kind: str,
                              idem: str, pid: str) -> dict:
        """memory_add / memory_edit: project-scoped, operator sessions only. The
        body is checked here (file name, heading, text size); the coordinator
        validates again (secrets, the file's headings) and commits."""
        if scopes is not None and not {"operator", "admin"} & set(scopes or []):
            raise ControlError("editing project memory needs an operator session", "forbidden", 403)
        if not pid:
            raise ControlError("project_id is required")
        if projects and pid not in projects:
            raise ControlError("this operator may not act on that project", "forbidden", 403)
        file = _s(body.get("file"), 160).replace("\\", "/").strip().strip("/")
        if file.lower().startswith("docs/memory/"):
            file = file[len("docs/memory/"):]
        if not MEMORY_FILE.match(file):
            raise ControlError("file must be DECISIONS.md, CONVENTIONS.md, ASSETS.md, KNOWN_ISSUES.md, "
                               "tracks/<name>.md or sprints/<ID>.md")
        heading = " ".join(str(body.get("heading") or "").split())
        if not heading:
            raise ControlError("a heading is required (the section the text goes under)")
        if len(heading) > MAX_MEMORY_HEADING or heading.startswith("#"):
            raise ControlError(f"heading must be one line of at most {MAX_MEMORY_HEADING} characters, "
                               "without '#'")
        text = str(body.get("text") or "")
        if not text.strip():
            raise ControlError("text is empty")
        if len(text) > MAX_MEMORY_TEXT:
            raise ControlError(f"text is over {MAX_MEMORY_TEXT} characters")
        payload = {"file": file, "heading": heading, "text": text}
        tid = _s(body.get("task_id"), 120)
        if tid:
            payload["task_id"] = tid
        chat = body.get("chat")
        if isinstance(chat, dict):
            payload["chat"] = {k: _s(chat.get(k), 80) for k in ("thread", "turn", "call")}
        if not self.rate_ok(actor):
            raise ControlError("too many control actions; wait a minute", "rate_limited", 429)
        st = self.settings()
        now = self.clock()
        aid = f"act-{uuid.uuid4().hex[:16]}"
        summary = self._summary(kind, payload, pid)
        try:
            with self._tx() as c:
                seq = self._feed(c, "action", pid, "", aid)
                c.execute("INSERT INTO cc_actions (action_id, idem_key, actor, kind, project_id, "
                          "task_id, run_id, session_id, request_id, payload, summary, expected, "
                          "status, status_detail, created_ts, expires_ts, updated_ts, seq) VALUES "
                          "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (aid, idem, actor, kind, pid, "", "", "", "", _j(payload), summary,
                           _j({}), "accepted",
                           "stored by Foundry; waiting for the coordinator to claim it", now,
                           now + st["action_ttl_s"], now, seq))
                c.execute("INSERT INTO cc_action_log (action_id, ts, status, actor, detail) "
                          "VALUES (?,?,?,?,?)", (aid, now, "accepted", actor, summary))
        except Exception as e:                                   # noqa: BLE001
            if "UNIQUE" in str(e) and "idem" in str(e):
                ex = self.db.query_one("SELECT action_id FROM cc_actions WHERE idem_key=?", (idem,))
                return {**self.action_view(ex["action_id"]), "duplicate": True}
            raise
        self._notify()
        return self.action_view(aid)

    def _create_client_action(self, actor: str, projects: list, body: dict, scopes, kind: str,
                              idem: str) -> dict:
        """client_restart / client_reload_kilo_config: workstation-scoped, operator
        sessions without a project restriction, one live action per component.
        Refused when no Foundry Client has heart-beated (nothing could claim it)."""
        if scopes is not None and not {"operator", "admin"} & set(scopes or []):
            raise ControlError("stack actions need an operator session", "forbidden", 403)
        if projects:
            raise ControlError("the workstation stack is not project-scoped; it needs an operator "
                               "session that is not restricted to projects", "forbidden", 403)
        if kind == "client_restart":
            comp = _s(body.get("component"), 40)
            if comp not in CLIENT_COMPONENTS:
                raise ControlError(f"component must be one of {', '.join(CLIENT_COMPONENTS)}")
            force = body.get("force", False)
            if not isinstance(force, bool):
                raise ControlError("force must be true or false")
            payload = {"component": comp, "force": force}
        elif kind == "client_service_enable":
            comp = _s(body.get("component"), 40)
            if comp not in CLIENT_TOGGLE:
                raise ControlError(f"component must be one of {', '.join(CLIENT_TOGGLE)}")
            en = body.get("enabled")
            if not isinstance(en, bool):
                raise ControlError("enabled must be true or false")
            payload = {"component": comp, "enabled": en}
        else:
            comp = "kilo"
            payload = {}
        summary = self._summary(kind, payload, comp)
        chat = body.get("chat")
        if isinstance(chat, dict):
            payload["chat"] = {k: _s(chat.get(k), 80) for k in ("thread", "turn", "call")}
        if not self.client_sources():
            raise ControlError("no Foundry Client has reported to this router; start the client on "
                               "the workstation with a router key (docs/CLIENT_MANAGER.md)",
                               "no_client", 409)
        if not self.rate_ok(actor):
            raise ControlError("too many control actions; wait a minute", "rate_limited", 429)
        st = self.settings()
        now = self.clock()
        aid = f"act-{uuid.uuid4().hex[:16]}"
        dup = None
        try:
            with self._tx() as c:
                # task_id carries the component: one live stack action per component
                # (the view is read after the transaction: the db lock is not re-entrant)
                # restarts and reloads share one slot per component; switching a service on
                # or off is its own kind (a restart in flight must not swallow it)
                same = (("client_service_enable",) if kind == "client_service_enable"
                        else tuple(k for k in CLIENT_ACTIONS if k != "client_service_enable"))
                live = self._rows(c, "SELECT action_id FROM cc_actions WHERE kind IN ("
                                     + ",".join("?" * len(same)) + ") AND "
                                     "task_id=? AND status IN ('accepted','claimed','queued',"
                                     "'delivered') ORDER BY created_ts DESC LIMIT 1",
                                  (*same, comp))
                if live:
                    dup = live[0]["action_id"]
                    raise _Dup()
                seq = self._feed(c, "action", "", "", aid)
                c.execute("INSERT INTO cc_actions (action_id, idem_key, actor, kind, project_id, "
                          "task_id, run_id, session_id, request_id, payload, summary, expected, "
                          "status, status_detail, created_ts, expires_ts, updated_ts, seq) VALUES "
                          "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (aid, idem, actor, kind, "", comp, "", "", "", _j(payload), summary,
                           _j({"component": comp}), "accepted",
                           "stored by Foundry; waiting for the Foundry Client to claim it", now,
                           now + st["action_ttl_s"], now, seq))
                c.execute("INSERT INTO cc_action_log (action_id, ts, status, actor, detail) "
                          "VALUES (?,?,?,?,?)", (aid, now, "accepted", actor, summary))
        except _Dup:
            return {**self.action_view(dup), "duplicate": True}
        except Exception as e:                                   # noqa: BLE001
            if "UNIQUE" in str(e) and "idem" in str(e):
                ex = self.db.query_one("SELECT action_id FROM cc_actions WHERE idem_key=?", (idem,))
                return {**self.action_view(ex["action_id"]), "duplicate": True}
            raise
        self._notify()
        return self.action_view(aid)

    def claim_client(self, source_id: str, limit: int = 20) -> list[dict]:
        """Hand pending stack actions to a Foundry Client. Only a source whose
        last heartbeat was kind `client` and declared stack_control gets them;
        an action this source claimed but never reported is offered again."""
        if not source_id:
            raise ControlError("source_id is required")
        src = self.db.query_one("SELECT kind FROM cc_sources WHERE source_id=?", (source_id,))
        if not src or src["kind"] != "client":
            raise ControlError(f"{source_id} has not heart-beated as a client", "unknown_source", 409)
        if CLIENT_FEATURE not in self.source_feature_set(source_id):
            raise ControlError(f"{source_id} does not declare {CLIENT_FEATURE}", "forbidden", 403)
        if self.source_compat(source_id) == "refuse":
            raise ControlError(f"{source_id} is refused by the version gate", "version_refused", 409)
        now = self.clock()
        kq = ",".join("?" * len(CLIENT_ACTIONS))
        out = []
        with self._tx() as c:
            rows = self._rows(c, f"SELECT * FROM cc_actions WHERE kind IN ({kq}) AND ((status="
                                 f"'accepted' AND expires_ts >= ?) OR (claimed_by=? AND status="
                                 f"'claimed' AND claimed_ts < ?)) ORDER BY created_ts LIMIT ?",
                              (*CLIENT_ACTIONS, now, source_id, now - 60, max(1, min(50, limit))))
            for r in rows:
                if r["status"] == "accepted":
                    c.execute("UPDATE cc_actions SET status='claimed', claimed_by=?, claimed_ts=?, "
                              "updated_ts=?, status_detail=? WHERE action_id=?",
                              (source_id, now, now, "claimed by the Foundry Client", r["action_id"]))
                    c.execute("INSERT INTO cc_action_log (action_id, ts, status, actor, detail) "
                              "VALUES (?,?,?,?,?)", (r["action_id"], now, "claimed", source_id, ""))
                    self._feed(c, "action", "", "", r["action_id"])
                out.append({"action_id": r["action_id"], "kind": r["kind"], "actor": r["actor"],
                            "component": r["task_id"], "payload": _uj(r["payload"], {}),
                            "created_ts": r["created_ts"], "expires_ts": r["expires_ts"],
                            "reoffered": r["status"] != "accepted"})
        if out:
            self._notify()
        return out

    def _create_project_action(self, actor: str, projects: list, body: dict, scopes, kind: str,
                               idem: str, pid: str, tid: str) -> dict:
        """project_pause / project_resume: project-scoped (task id optional, no
        task-state stale check), operator sessions only, one at a time. A second
        request while one is in flight returns that one (duplicate: true); a
        pause of a project that is already paused is 409 `duplicate`."""
        if scopes is not None and not {"operator", "admin"} & set(scopes or []):
            raise ControlError("pausing or resuming a project needs an operator session",
                               "forbidden", 403)
        if not pid:
            raise ControlError("project_id is required")
        if projects and pid not in projects:
            raise ControlError("this operator may not act on that project", "forbidden", 403)
        note = str(body.get("note") or "").strip()
        if len(note) > MAX_PAUSE_NOTE:
            raise ControlError(f"note is over {MAX_PAUSE_NOTE} characters")
        if kind == "project_pause":
            mode = body.get("mode") or "safe"
            if mode not in PAUSE_MODES:
                raise ControlError("mode must be 'safe' or 'now'")
            if not note:
                raise ControlError("a note is required (say why, e.g. 'upgrading to v0.122.3')")
            rau = body.get("resume_after_upgrade", False)
            if not isinstance(rau, bool):
                raise ControlError("resume_after_upgrade must be true or false")
            payload = {"mode": mode, "note": note}
            if rau:
                payload["resume_after_upgrade"] = True
        else:
            payload = {"note": note}
        if not self.rate_ok(actor):
            raise ControlError("too many control actions; wait a minute", "rate_limited", 429)
        st = self.settings()
        now = self.clock()
        aid = f"act-{uuid.uuid4().hex[:16]}"
        summary = self._summary(kind, payload, pid)
        dup = None
        try:
            with self._tx() as c:
                last, status = self._project_pause_state(c, pid)
                paused = bool((status or {}).get("paused"))
                up = (status or {}).get("upgrade_resume") or {}
                if (last and last["kind"] == "project_pause" and last["status"] == "applied"
                        and not paused and (up.get("ts") or 0) >= (last.get("created_ts") or 0)):
                    # the coordinator resumed that pause itself after an upgrade:
                    # it is no longer the project's current pause
                    last = None
                live_same = bool(last and last["kind"] == kind and last["status"] in LIVE_ACTION)
                resuming = bool(last and last["kind"] == "project_resume"
                                and last["status"] in LIVE_ACTION)
                if live_same:
                    dup = last["action_id"]                      # one in flight: that one
                elif kind == "project_pause" and (
                        (last and last["kind"] == kind) or (paused and not resuming)):
                    raise ControlError(f"{pid} is already paused; resume it first", "duplicate",
                                       409, current=last["action_id"] if last else "")
                elif kind == "project_resume" and not paused and not (
                        last and last["kind"] == "project_pause"):
                    raise ControlError(f"{pid} is not paused", "not_paused", 409)
                if dup is None:
                    expected = {"project_paused": paused if status is not None else None}
                    seq = self._feed(c, "action", pid, "", aid)
                    c.execute("INSERT INTO cc_actions (action_id, idem_key, actor, kind, project_id, "
                              "task_id, run_id, session_id, request_id, payload, summary, expected, "
                              "status, status_detail, created_ts, expires_ts, updated_ts, seq) VALUES "
                              "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (aid, idem, actor, kind, pid, tid, "", "", "", _j(payload), summary,
                               _j(expected), "accepted",
                               "stored by Foundry; waiting for the coordinator to claim it", now,
                               now + st["action_ttl_s"], now, seq))
                    c.execute("INSERT INTO cc_action_log (action_id, ts, status, actor, detail) "
                              "VALUES (?,?,?,?,?)", (aid, now, "accepted", actor, summary))
        except Exception as e:                                   # noqa: BLE001
            if "UNIQUE" in str(e) and "idem" in str(e):
                ex = self.db.query_one("SELECT action_id FROM cc_actions WHERE idem_key=?", (idem,))
                return {**self.action_view(ex["action_id"]), "duplicate": True}
            raise
        if dup:
            return {**self.action_view(dup), "duplicate": True}
        self._notify()
        return self.action_view(aid)

    @staticmethod
    def _summary(kind: str, p: dict, tid: str) -> str:
        """What the audit log shows: no answer/instruction body (it may hold
        sensitive text); length only."""
        if kind == "project_pause":
            return (f"pause project {tid} ({'at a safe point' if p.get('mode') == 'safe' else 'now'})"
                    + ("; resume automatically after an upgrade" if p.get("resume_after_upgrade")
                       else "") + f": {p.get('note') or ''}")[:300]
        if kind == "project_resume":
            return (f"resume project {tid}" + (f": {p['note']}" if p.get("note") else ""))[:300]
        if kind in LEAD_ACTIONS:
            verb = {"lead_case_pause": "pause", "lead_case_takeover": "take over",
                    "lead_case_instruct": "instruct", "lead_case_resume": "resume"}[kind]
            return (f"{verb} recovery case {p.get('case_id')} ({tid})"
                    + (f", note {len(p.get('note') or '')} chars" if p.get("note") else ""))[:300]
        if kind == "lanes":
            return f"proactive lanes {'paused' if p.get('paused') else 'un-paused'} ({tid})"
        if kind in MEMORY_ACTIONS:
            verb = "add to" if kind == "memory_add" else "edit"
            return (f"memory {verb} {p.get('file')} > {p.get('heading')} ({tid}), "
                    f"{len(p.get('text') or '')} chars")[:300]
        if kind == "client_restart":
            return f"restart {p.get('component')} ({'now' if p.get('force') else 'at a safe point'})"
        if kind == "client_reload_kilo_config":
            return "reload kilo.jsonc into the Kilo server"
        if kind == "client_service_enable":
            return f"{'enable' if p.get('enabled') else 'disable'} {p.get('component')}"
        if kind == "decision_answer":
            return (f"answer decision {p.get('decision_id')} ({p.get('decision_kind') or '?'}) as "
                    f"{p.get('authority')}" + (" · option chosen" if p.get("option") else "")
                    + f", {len(p.get('answer') or '')} chars")
        if kind == "answer_question":
            return f"answer {len(p.get('answers') or [])} question(s), " \
                   f"{sum(len(x) for a in p.get('answers') or [] for x in a)} chars"
        if kind == "reply_permission":
            return f"permission reply: {p.get('reply')}"
        if kind == "instruction":
            return f"instruction ({p.get('delivery')}, {p.get('scope')}), {len(p.get('text') or '')} chars"
        if kind in ("approve_review", "reject_review"):
            return f"{kind.split('_')[0]} {p.get('approval_kind')} @ {str(p.get('revision'))[:12]}" + (
                f" ({p.get('mode')})" if p.get("mode") else "")
        return f"{kind} {tid}" + (f": {p.get('reason') or p.get('resolution') or ''}"[:200]
                                  if p else "")

    def action_view(self, action_id: str, with_log: bool = True) -> Optional[dict]:
        a = self.db.query_one("SELECT action_id, actor, kind, project_id, task_id, run_id, session_id, "
                              "request_id, summary, expected, status, status_detail, created_ts, "
                              "expires_ts, claimed_by, claimed_ts, updated_ts, result, seq FROM "
                              "cc_actions WHERE action_id=?", (action_id,))
        if not a:
            return None
        a["expected"] = _uj(a["expected"], {})
        a["result"] = _uj(a["result"], {})
        if with_log:
            a["log"] = self.db.query("SELECT ts, status, actor, detail FROM cc_action_log WHERE "
                                     "action_id=? ORDER BY id", (action_id,))
        return a

    def actions(self, projects: list, project: str = "", task: str = "", status: str = "",
                limit: int = 100) -> list[dict]:
        where, args = [], []
        if project:
            where.append("project_id=?")
            args.append(project)
        if task:
            where.append("task_id=?")
            args.append(task)
        if status == "live":
            where.append("status IN ('accepted','claimed','queued','delivered')")
        elif status:
            where.append("status=?")
            args.append(status)
        pf, pa = self._pfilter(projects)
        rows = self.db.query("SELECT action_id FROM cc_actions WHERE 1=1" + "".join(
            " AND " + w for w in where) + pf + " ORDER BY created_ts DESC LIMIT ?",
                             (*args, *pa, max(1, min(500, limit))))
        return [self.action_view(r["action_id"], with_log=False) for r in rows]

    def _set_action(self, action_id: str, status: str, actor: str, detail: str,
                    result: Optional[dict] = None, claimed_by: str = "") -> Optional[dict]:
        now = self.clock()
        with self._tx() as c:
            a = self._rows(c, "SELECT status, project_id, session_id, kind, task_id FROM "
                              "cc_actions WHERE action_id=?", (action_id,))
            if not a:
                return None
            if a[0]["status"] in FINAL_ACTION and a[0]["status"] != status:
                return self.action_view(action_id)               # final is final
            seq = self._feed(c, "action", a[0]["project_id"], a[0]["session_id"], action_id)
            c.execute("UPDATE cc_actions SET status=?, status_detail=?, updated_ts=?, seq=?, "
                      "result=COALESCE(?, result), claimed_by=COALESCE(NULLIF(?,''), claimed_by), "
                      "claimed_ts=CASE WHEN ?='claimed' THEN ? ELSE claimed_ts END WHERE action_id=?",
                      (status, detail[:1000], now, seq, _j(result) if result is not None else None,
                       claimed_by, status, now, action_id))
            c.execute("INSERT INTO cc_action_log (action_id, ts, status, actor, detail) VALUES "
                      "(?,?,?,?,?)", (action_id, now, status, actor, detail[:1000]))
            if a[0]["kind"] == "unblock" or a[0]["kind"] in LEAD_ACTIONS:
                self._feed(c, "task", a[0]["project_id"], "",
                           f"{a[0]['project_id']}/{a[0]['task_id']}")
        self._notify()
        return self.action_view(action_id)

    def cancel_action(self, action_id: str, actor: str, projects: list) -> dict:
        a = self.db.query_one("SELECT * FROM cc_actions WHERE action_id=?", (action_id,))
        if not a or (projects and a["project_id"] not in projects):
            raise ControlError("no such action", "not_found", 404)
        if a["status"] != "accepted":
            raise ControlError(f"action is {a['status']}: the coordinator already has it"
                               + ("; withdraw the queued instruction instead"
                                  if a["status"] == "queued" else ""), "stale_state", 409)
        with self._tx() as c:
            # only if still unclaimed (a claim between the read and here wins)
            n = c.execute("UPDATE cc_actions SET status='cancelled', status_detail=?, updated_ts=? "
                          "WHERE action_id=? AND status='accepted'",
                          (f"cancelled by {actor} before delivery", self.clock(), action_id)).rowcount
            if n:
                self._feed(c, "action", a["project_id"], a["session_id"], action_id)
                c.execute("INSERT INTO cc_action_log (action_id, ts, status, actor, detail) VALUES "
                          "(?,?,?,?,?)", (action_id, self.clock(), "cancelled", actor,
                                          "cancelled before delivery"))
        if not n:
            raise ControlError("the coordinator claimed it a moment ago", "stale_state", 409)
        self._notify()
        return self.action_view(action_id)

    # coordinator side (orchestration scope)

    def claim(self, source_id: str, projects: list, allowed: list, limit: int = 20) -> list[dict]:
        """Hand pending actions to the coordinator that owns the engine. A claim
        is atomic; an action claimed but never reported back is offered again
        to the same source so it can reconcile (never re-applied blindly: the
        coordinator keys every action in its own store)."""
        if not source_id or not projects:
            raise ControlError("source_id and projects are required")
        if self.source_compat(source_id) == "refuse":
            raise ControlError(f"{source_id} is refused by the version gate (see its heartbeat "
                               "compat); no actions are handed out", "version_refused", 409)
        if allowed:
            projects = [p for p in projects if p in allowed]
        if not projects:
            return []
        now = self.clock()
        q = ",".join("?" * len(projects))
        out = []
        # an action kind the source never declared is not handed to it: it stays
        # accepted (another, capable source may claim it) with the reason shown
        feats = self.source_feature_set(source_id)
        withheld = [k for k, f in FEATURE_GATED_ACTIONS.items() if f not in feats]
        wq = (f" AND kind NOT IN ({','.join('?' * len(withheld))})") if withheld else ""
        noted = False
        with self._tx() as c:
            for k in withheld:
                why = (f"waiting: {source_id} does not declare the {FEATURE_GATED_ACTIONS[k]} "
                       f"feature (upgrade the coordinator); not handed out")
                for r in self._rows(c, f"SELECT action_id, project_id, session_id, status_detail "
                                       f"FROM cc_actions WHERE project_id IN ({q}) AND kind=? AND "
                                       f"status='accepted' AND expires_ts >= ?",
                                    (*projects, k, now)):
                    if r["status_detail"] != why:
                        c.execute("UPDATE cc_actions SET status_detail=?, updated_ts=? WHERE "
                                  "action_id=?", (why, now, r["action_id"]))
                        c.execute("INSERT INTO cc_action_log (action_id, ts, status, actor, detail) "
                                  "VALUES (?,?,?,?,?)", (r["action_id"], now, "accepted", source_id,
                                                         why))
                        self._feed(c, "action", r["project_id"], r["session_id"], r["action_id"])
                        noted = True
            # re-offered to the same source: claimed but never reported (it can
            # reconcile from its own store), and replies it is still waiting to
            # send because the engine hadn't recorded the question yet
            rows = self._rows(c, f"SELECT * FROM cc_actions WHERE project_id IN ({q}) AND ((status="
                                 f"'accepted' AND expires_ts >= ?) OR (claimed_by=? AND ((status="
                                 f"'claimed' AND claimed_ts < ?) OR (status='queued' AND kind IN "
                                 f"('answer_question','reply_permission') AND expires_ts >= ?)))){wq} "
                                 f"ORDER BY created_ts LIMIT ?",
                              (*projects, now, source_id, now - 60, now, *withheld, limit))
            for r in rows:
                if r["status"] == "accepted":
                    c.execute("UPDATE cc_actions SET status='claimed', claimed_by=?, claimed_ts=?, "
                              "updated_ts=?, status_detail=? WHERE action_id=?",
                              (source_id, now, now, "claimed by the coordinator", r["action_id"]))
                    c.execute("INSERT INTO cc_action_log (action_id, ts, status, actor, detail) "
                              "VALUES (?,?,?,?,?)", (r["action_id"], now, "claimed", source_id, ""))
                    self._feed(c, "action", r["project_id"], r["session_id"], r["action_id"])
                out.append({"action_id": r["action_id"], "kind": r["kind"], "actor": r["actor"],
                            "project_id": r["project_id"], "task_id": r["task_id"],
                            "session_id": r["session_id"], "request_id": r["request_id"],
                            "payload": _uj(r["payload"], {}), "expected": _uj(r["expected"], {}),
                            "created_ts": r["created_ts"], "expires_ts": r["expires_ts"],
                            "reoffered": r["status"] != "accepted"})
        if out or noted:
            self._notify()
        return out

    def report(self, action_id: str, source_id: str, status: str, detail: str,
               result: Optional[dict] = None) -> dict:
        if status not in ("queued", "delivered", "applied", "rejected", "failed"):
            raise ControlError("status must be queued|delivered|applied|rejected|failed")
        a = self.db.query_one("SELECT * FROM cc_actions WHERE action_id=?", (action_id,))
        if not a or a["claimed_by"] != source_id:
            raise ControlError("unknown action for this source", "not_found", 404)
        v = self._set_action(action_id, status, source_id, detail or status, result=result or {})
        if status == "applied" and a["request_id"]:
            self._resolve(f"pending:{a['request_id']}", f"answered by {a['actor']}", "answered")
        if status == "applied" and a["kind"] == "decision_answer":
            did = (_uj(a.get("payload"), {}) or {}).get("decision_id")
            if did:
                self._resolve(f"decision:{a['project_id']}:{a['task_id']}:{did}",
                              f"answered by {a['actor']} (operator)", "answered")
        if status == "applied" and a["kind"] in ("approve_review", "reject_review"):
            for al in self.db.query("SELECT dedupe_key FROM cc_alerts WHERE dedupe_key LIKE ? AND "
                                    "status IN ('new','acknowledged')",
                                    (f"approval:{a['project_id']}:{a['task_id']}:%",)):
                self._resolve(al["dedupe_key"], f"decided by {a['actor']}", "answered")
        return v

    # ------------------------------------------------------------------ retention

    def prune(self) -> int:
        st = self.settings()
        cut = self.clock() - st["retention_days"] * 86400
        acut = self.clock() - st["action_retention_days"] * 86400
        n = 0
        for sql, arg in (("DELETE FROM cc_parts WHERE received_ts < ?", cut),
                         ("DELETE FROM cc_messages WHERE COALESCE(completed_ts, created_ts, 0) < ?",
                          cut),
                         ("DELETE FROM cc_sessions WHERE updated_ts < ?", cut),
                         ("DELETE FROM cc_snapshots WHERE confirmed_ts < ? AND id NOT IN (SELECT "
                          "MAX(id) FROM cc_snapshots GROUP BY kind, project_id, session_id, scope)",
                          cut),
                         ("DELETE FROM cc_feed WHERE ts < ?", cut),
                         ("DELETE FROM cc_pending WHERE status='resolved' AND resolved_ts < ?", cut),
                         ("DELETE FROM cc_transitions WHERE ts < ?", acut),
                         ("DELETE FROM cc_alerts WHERE status NOT IN ('new','acknowledged') AND "
                          "updated_ts < ?", acut),
                         ("DELETE FROM cc_alert_log WHERE ts < ?", acut),
                         ("DELETE FROM cc_actions WHERE status IN ('applied','rejected','failed',"
                          "'expired','cancelled') AND updated_ts < ?", acut),
                         ("DELETE FROM cc_action_log WHERE ts < ?", acut),
                         ("DELETE FROM cc_op_sessions WHERE expires_ts < ?", self.clock()),
                         ("DELETE FROM cc_drafts WHERE updated_ts < ?", cut),
                         ("DELETE FROM cc_lane_units WHERE received_ts < ?", cut),
                         ("DELETE FROM cc_artifacts WHERE received_ts < ?", cut),
                         ("DELETE FROM cc_batches WHERE ts < ?", cut)):
            n += self.db.execute_count(sql, (arg,))
        return n

    def capture_boundary(self) -> dict:
        r = self.db.query_one("SELECT MIN(received_ts) AS first, COUNT(*) AS n FROM cc_parts")
        return {"first_part_received_ts": (r or {}).get("first"), "parts": (r or {}).get("n"),
                "retention_days": self.settings()["retention_days"]}

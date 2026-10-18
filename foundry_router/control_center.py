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
                "lanes")
LIVE_ACTION = ("accepted", "claimed", "queued", "delivered")
FINAL_ACTION = ("applied", "rejected", "failed", "expired", "cancelled")
ACTION_STATUSES = LIVE_ACTION + FINAL_ACTION
PART_KINDS = ("text", "reasoning", "tool", "step-start", "step-finish", "file", "patch",
              "retry", "compaction", "agent", "subtask", "snapshot", "error", "unknown")
SEVERITIES = ("info", "warning", "high", "critical")
PERMISSION_REPLIES = ("once", "reject")       # "always" creates a persistent rule: not offered


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
        if not sid or kind not in ("bridge", "coordinator"):
            raise ControlError("heartbeat needs source.id and source.kind (bridge|coordinator)")
        detail = self.redact({k: payload.get(k) for k in (
            "kilo", "coordinator_owner", "projects", "sessions_tracked", "tick", "uptime_s",
            "backoff_s", "errors", "capture_boundary", "event_stream", "monotonic_s")
            if payload.get(k) is not None})
        now = self.clock()
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
        self._resolve(f"stale:{sid}", "heartbeat received again")
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
        return {"ok": True, "contract": CONTRACT, "server_ts": now}

    def ingest(self, batch: dict, client: dict) -> dict:
        if not isinstance(batch, dict) or batch.get("contract") != CONTRACT:
            raise ControlError(f"expected contract {CONTRACT!r}", "contract_mismatch")
        obs = batch.get("observations") or []
        if not isinstance(obs, list) or len(obs) > 2000:
            raise ControlError("observations must be a list of at most 2000 items")
        allowed = client.get("projects") or []
        src = _s((batch.get("source") or {}).get("id"), 120)
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
        for args in derived:
            if args[0] == "raise":
                self.raise_alert(*args[1:])
            else:
                self._resolve(*args[1:])
        self._notify()
        return {"ok": True, "accepted": counts, "skipped": skipped, "server_ts": now}

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
        seq = self._feed(c, "session", row["project_id"], sid, sid)
        row["seq"] = seq
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
        seq = self._feed(c, "message", o.get("project_id"), sid, mid)
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
        seq = self._feed(c, "part", o.get("project_id"), sid, pid_)
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
        old = self._rows(c, "SELECT status FROM cc_pending WHERE request_id=?", (rid,))
        seq = self._feed(c, "pending", o.get("project_id"), o.get("session_id"), rid)
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
        old = self._rows(c, "SELECT state, data FROM cc_tasks WHERE project_id=? AND task_id=?",
                         (pid, tid))
        prev = old[0]["state"] if old else ""
        data = {k: v for k, v in o.items() if k not in ("type", "project_id", "task_id")}
        seq = self._feed(c, "task", pid, o.get("session_id"), f"{pid}/{tid}")
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
            out.append(("raise", f"blocked:{pid}:{tid}:{_hash(o.get('blocker'))}", "blocker",
                        "high", f"{tid} is BLOCKED", _s(o.get("blocker"), 600), ref))
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
        seq = self._feed(c, "lane_unit", pid, o.get("session_id") or "", uid)
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
        seq = self._feed(c, "artifact", o.get("project_id"), "", aid)
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
        c.execute("INSERT INTO cc_lane_state (project_id, paused, data, received_ts) VALUES (?,?,?,?) "
                  "ON CONFLICT(project_id) DO UPDATE SET paused=excluded.paused, data=excluded.data, "
                  "received_ts=excluded.received_ts",
                  (pid, 1 if o.get("paused") else 0, _j({k: o.get(k) for k in (
                      "enabled", "lanes", "settings") if o.get(k) is not None}), now))
        self._feed(c, "lane_state", pid, "", pid)
        return []

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
        with self._tx() as c:
            row = self._rows(c, "SELECT * FROM cc_alerts WHERE dedupe_key=?", (key,))
            if row and row[0]["status"] in ("new", "acknowledged"):
                return row[0]                       # same event: no new alert, no repeat sound
            if row:
                # it was resolved and happened again (a new occurrence)
                aid = row[0]["alert_id"]
                seq = self._feed(c, "alert", ref.get("project_id"), ref.get("session_id"), aid)
                c.execute("UPDATE cc_alerts SET status='new', severity=?, title=?, detail=?, "
                          "updated_ts=?, ack_by=NULL, ack_ts=NULL, resolved_ts=NULL, resolution=NULL, "
                          "seq=? WHERE alert_id=?", (severity, title[:300], detail[:2000], now, seq,
                                                     aid))
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

    def sources(self) -> list[dict]:
        st = self.settings()
        now = self.clock()
        out = []
        for s in self.db.query("SELECT * FROM cc_sources ORDER BY kind, source_id"):
            limit = st["coordinator_expiry_s"] if s["kind"] == "coordinator" else st["bridge_expiry_s"]
            s["detail"] = _uj(s["detail"], {})
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
            r["verification_view"] = self._safe_view(r)
            tasks.append(r)
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
        return {"contract": CONTRACT, "server_ts": self.clock(), "seq": self.last_seq(),
                "tasks": tasks, "sessions": sessions, "pending": pend,
                "sources": self.sources(), "projects": sorted(
                    {t["project_id"] for t in tasks} | {s["project_id"] for s in sessions if
                                                         s.get("project_id")}),
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
                "checks": checks, "review": review,
                "attempts": {"used": used, "max": mx, "extra": extra, "exhausted": exhausted,
                             "label": label},
                "recovery": recovery}

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
                r["verification_view"] = self._safe_view(r)
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

    def create_action(self, actor: str, projects: list, body: dict) -> dict:
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
        if kind == "lanes":
            # project-wide operational switch: the task id only routes the action,
            # so its state is not compared (the coordinator skips it too)
            expected.pop("task_state", None)
        else:
            expected.setdefault("task_state", task["state"])
        if kind != "lanes" and expected["task_state"] != task["state"]:
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
                if kind == "unblock":
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
    def _summary(kind: str, p: dict, tid: str) -> str:
        """What the audit log shows: no answer/instruction body (it may hold
        sensitive text); length only."""
        if kind == "lanes":
            return f"proactive lanes {'paused' if p.get('paused') else 'un-paused'} ({tid})"
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
            if a[0]["kind"] == "unblock":
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
        if allowed:
            projects = [p for p in projects if p in allowed]
        if not projects:
            return []
        now = self.clock()
        q = ",".join("?" * len(projects))
        out = []
        with self._tx() as c:
            # re-offered to the same source: claimed but never reported (it can
            # reconcile from its own store), and replies it is still waiting to
            # send because the engine hadn't recorded the question yet
            rows = self._rows(c, f"SELECT * FROM cc_actions WHERE project_id IN ({q}) AND ((status="
                                 f"'accepted' AND expires_ts >= ?) OR (claimed_by=? AND ((status="
                                 f"'claimed' AND claimed_ts < ?) OR (status='queued' AND kind IN "
                                 f"('answer_question','reply_permission') AND expires_ts >= ?)))) "
                                 f"ORDER BY created_ts LIMIT ?",
                              (*projects, now, source_id, now - 60, now, limit))
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
        if out:
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
                         ("DELETE FROM cc_artifacts WHERE received_ts < ?", cut)):
            n += self.db.execute_count(sql, (arg,))
        return n

    def capture_boundary(self) -> dict:
        r = self.db.query_one("SELECT MIN(received_ts) AS first, COUNT(*) AS n FROM cc_parts")
        return {"first_part_received_ts": (r or {}).get("first"), "parts": (r or {}).get("n"),
                "retention_days": self.settings()["retention_days"]}

"""Work bindings: which project, task and role a coding session belongs to.

A development coordinator (contrib/coordinator, or any other orchestrator)
starts coding sessions in a client such as Kilo. Kilo sends its session id on
every model call (`x-session-id`), but nothing about the project or the sprint.
The coordinator registers each session it starts here:

  POST /v1/foundry/work/sessions   {session_id, project_id, task_id, run_id,
                                    role, parent_session_id, persona}

From then on every request carrying that session id is recorded, budgeted and
traced with project_id / task_id / run_id / role, and its parent session as
parent_task_id. Explicit x-foundry-* correlation headers still win.

The coordinator also pushes its own lifecycle state (task phase, attempts,
commit, review verdict, blocker) and events so the operator sees sprints next
to the model calls they caused. Foundry only displays that state: the
coordinator's own store is the authority, and Foundry never decides whether a
task is done. Foundry cannot see local file edits or Studio operations; only
what a client reports or what passes through it as inference.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

_ID = re.compile(r"^[A-Za-z0-9._:@/+-]{1,160}$")
ROLES = ("implementer", "researcher", "reviewer", "deep_reviewer", "visual_reviewer",
         "image_generator", "coordinator", "helper")
FIELDS = ("project_id", "task_id", "run_id", "role", "parent_session_id", "persona")
# Why a session runs (helper policy, quota_gate.py). Set only by the
# coordinator's orchestration-scoped binding: a client or model can't choose
# it. Required work keeps the existing guardrails; optional work is admitted
# only on fresh, below-cutoff quota for the account that serves it.
REQUIRED_PURPOSES = ("implementer", "planning", "review", "closeout", "blocker_diagnosis")
OPTIONAL_PURPOSES = ("code_proposal", "test_design", "debug_advice", "exploration")
PURPOSES = REQUIRED_PURPOSES + OPTIONAL_PURPOSES


class WorkError(ValueError):
    pass


def _clean(v, field: str, required: bool = False) -> str:
    s = str(v or "").strip()
    if not s:
        if required:
            raise WorkError(f"{field} is required")
        return ""
    if not _ID.match(s):
        raise WorkError(f"{field} has characters Foundry doesn't accept: {s[:40]!r}")
    return s


class WorkRegistry:
    """Session bindings (cached in memory: looked up on every request),
    task snapshots and coordinator events."""

    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        for r in db.query("SELECT * FROM work_sessions WHERE ended_ts IS NULL"):
            self._cache[r["session_id"]] = dict(r)

    # -- bindings --------------------------------------------------------------

    def bind(self, data: dict, client_id: str = "") -> dict:
        sid = _clean(data.get("session_id"), "session_id", required=True)
        row = {f: _clean(data.get(f), f, required=f == "project_id") for f in FIELDS}
        if row["role"] and row["role"] not in ROLES:
            raise WorkError(f"role must be one of {', '.join(ROLES)}")
        purpose = str(data.get("purpose") or "").strip()
        if purpose and purpose not in PURPOSES:
            raise WorkError(f"purpose must be one of {', '.join(PURPOSES)}")
        worker_client = _clean(data.get("worker_client"), "worker_client")
        assignment = json.dumps(data.get("assignment") or {}, default=str)[:4000]             if data.get("assignment") else ""
        existing = self.lookup(sid) or self._row(sid)
        if existing and existing.get("project_id") and existing["project_id"] != row["project_id"]:
            # One session belongs to one project for its whole life: a session
            # id reused for another project would mix their budgets and traces.
            raise WorkError(f"session {sid} is bound to project {existing['project_id']!r}; "
                            f"start a new session for {row['project_id']!r}")
        now = time.time()
        meta = json.dumps(data.get("meta") or {}, default=str)[:4000]
        self.db.execute(
            "INSERT INTO work_sessions (session_id, project_id, task_id, run_id, role, "
            "parent_session_id, persona, client_id, meta, created_ts, updated_ts, purpose, "
            "assignment, worker_client) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
            "task_id=excluded.task_id, run_id=excluded.run_id, role=excluded.role, "
            "parent_session_id=excluded.parent_session_id, persona=excluded.persona, "
            "meta=excluded.meta, updated_ts=excluded.updated_ts, ended_ts=NULL, "
            "purpose=excluded.purpose, assignment=excluded.assignment, "
            "worker_client=excluded.worker_client",
            (sid, row["project_id"], row["task_id"], row["run_id"], row["role"],
             row["parent_session_id"], row["persona"], client_id, meta, now, now, purpose,
             assignment, worker_client))
        rec = self._row(sid)
        with self._lock:
            self._cache[sid] = rec
        return rec

    def end(self, session_id: str, outcome: str = "") -> bool:
        sid = _clean(session_id, "session_id", required=True)
        n = self.db.execute_count("UPDATE work_sessions SET ended_ts=?, outcome=? "
                                  "WHERE session_id=? AND ended_ts IS NULL",
                                  (time.time(), (outcome or "")[:60], sid))
        with self._lock:
            self._cache.pop(sid, None)
        return bool(n)

    def lookup(self, session_id: str) -> Optional[dict]:
        if not session_id:
            return None
        with self._lock:
            return self._cache.get(session_id)

    def _row(self, sid: str) -> Optional[dict]:
        rows = self.db.query("SELECT * FROM work_sessions WHERE session_id=?", (sid,))
        return dict(rows[0]) if rows else None

    def correlation(self, session_id: str, client_id: str = "") -> dict:
        """The fields a bound session adds to a request's client context.
        `purpose` comes only from the binding. When the coordinator named the
        client that runs the session (worker_client) and another client sends
        that session id, the purpose isn't trusted: it reads "unverified"
        (treated as optional, never as required)."""
        b = self.lookup(session_id)
        if not b:
            return {}
        out = {"project_id": b.get("project_id") or "", "run_id": b.get("run_id") or "",
               "role": b.get("role") or "", "work_task_id": b.get("task_id") or ""}
        if b.get("parent_session_id"):
            out["parent_task_id"] = b["parent_session_id"]
        purpose = b.get("purpose") or ""
        if purpose:
            wc = b.get("worker_client") or ""
            out["purpose"] = purpose if not wc or not client_id or client_id == wc                 else "unverified"
        return {k: v for k, v in out.items() if v}

    # -- coordinator state -----------------------------------------------------------

    def put_task(self, project_id: str, task_id: str, data: dict) -> dict:
        p = _clean(project_id, "project_id", required=True)
        t = _clean(task_id, "task_id", required=True)
        state = str(data.get("state") or "")[:40]
        self.db.execute(
            "INSERT INTO work_tasks (project_id, task_id, state, data, updated_ts) "
            "VALUES (?,?,?,?,?) ON CONFLICT(project_id, task_id) DO UPDATE SET "
            "state=excluded.state, data=excluded.data, updated_ts=excluded.updated_ts",
            (p, t, state, json.dumps(data, default=str)[:20000], time.time()))
        return {"project_id": p, "task_id": t, "state": state}

    def add_events(self, events: list) -> int:
        n = 0
        for e in (events or [])[:200]:
            if not isinstance(e, dict):
                continue
            try:
                p = _clean(e.get("project_id"), "project_id", required=True)
                t = _clean(e.get("task_id"), "task_id")
                s = _clean(e.get("session_id"), "session_id")
            except WorkError:
                continue
            key = str(e.get("event_id") or "")[:80]
            if key and self.db.query("SELECT 1 FROM work_events WHERE event_id=?", (key,)):
                continue                       # already recorded (coordinator resent it)
            self.db.execute(
                "INSERT INTO work_events (event_id, ts, project_id, task_id, session_id, "
                "kind, state, detail) VALUES (?,?,?,?,?,?,?,?)",
                (key or None, float(e.get("ts") or time.time()), p, t, s,
                 str(e.get("kind") or "")[:40], str(e.get("state") or "")[:40],
                 json.dumps(e.get("detail") or {}, default=str)[:4000]))
            n += 1
        return n

    # -- views ---------------------------------------------------------------------

    def usage(self, project_id: str = "", task_id: str = "", run_id: str = "",
              session_id: str = "") -> dict:
        """Measured usage (Foundry's ledger) for a project / task / run /
        session: calls, tokens, denials, by persona and backend. These are
        locally measured figures, not provider-reported allowance."""
        where, args = ["status IN ('settled','error','denied','reserved')"], []
        for col, v in (("project_id", project_id), ("work_task_id", task_id),
                       ("run_id", run_id), ("session_id", session_id)):
            if v:
                where.append(f"{col}=?")
                args.append(v)
        rows = self.db.query("SELECT persona, backend, model, account, paid, status, "
                             "COALESCE(input_tokens,0) AS i, COALESCE(output_tokens,0) AS o "
                             "FROM usage_events WHERE " + " AND ".join(where), tuple(args))
        out = {"source": "measured (Foundry ledger)", "calls": 0, "denied": 0, "errors": 0,
               "input_tokens": 0, "output_tokens": 0, "paid_calls": 0, "by_persona": {}}
        for r in rows:
            if r["status"] == "denied":
                out["denied"] += 1
                continue
            out["calls"] += 1
            out["errors"] += r["status"] == "error"
            out["paid_calls"] += bool(r["paid"])
            out["input_tokens"] += r["i"]
            out["output_tokens"] += r["o"]
            k = f"{r['persona'] or '-'} → {r['backend']}/{r['model']}"
            b = out["by_persona"].setdefault(k, {"calls": 0, "tokens": 0})
            b["calls"] += 1
            b["tokens"] += r["i"] + r["o"]
        return out

    def view(self, project_id: str = "", hours: float = 72) -> dict:
        since = time.time() - hours * 3600
        pw, pa = ("AND project_id=?", (project_id,)) if project_id else ("", ())
        tasks = []
        for r in self.db.query(f"SELECT * FROM work_tasks WHERE updated_ts >= ? {pw} "
                               "ORDER BY updated_ts DESC LIMIT 200", (since, *pa)):
            d = dict(r)
            try:
                d["data"] = json.loads(d.get("data") or "{}")
            except ValueError:
                d["data"] = {}
            tasks.append(d)
        sessions = [dict(r) for r in self.db.query(
            f"SELECT * FROM work_sessions WHERE updated_ts >= ? {pw} "
            "ORDER BY updated_ts DESC LIMIT 300", (since, *pa))]
        stats = {}
        for r in self.db.query(
                "SELECT session_id, COUNT(*) AS calls, SUM(CASE WHEN status='denied' THEN 1 "
                "ELSE 0 END) AS denied, SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) "
                "AS tokens, SUM(CASE WHEN status='reserved' THEN 1 ELSE 0 END) AS inflight, "
                "MAX(ts) AS last_ts, GROUP_CONCAT(DISTINCT backend || '/' || model) AS routes "
                f"FROM usage_events WHERE ts >= ? AND session_id != '' {pw} GROUP BY session_id",
                (since, *pa)):
            stats[r["session_id"]] = dict(r)
        for s in sessions:
            s["usage"] = stats.get(s["session_id"]) or {}
            try:
                s["meta"] = json.loads(s.get("meta") or "{}")
            except ValueError:
                s["meta"] = {}
        events = []
        for r in self.db.query(f"SELECT * FROM work_events WHERE ts >= ? {pw} "
                               "ORDER BY ts DESC, id DESC LIMIT 300", (since, *pa)):
            d = dict(r)
            try:
                d["detail"] = json.loads(d.get("detail") or "{}")
            except ValueError:
                d["detail"] = {}
            events.append(d)
        projects = sorted({t["project_id"] for t in tasks} | {s["project_id"] for s in sessions})
        return {"projects": projects, "tasks": tasks, "sessions": sessions, "events": events,
                "note": "Coordinator state as reported by the coordinator; model calls as "
                        "measured by Foundry. Local file edits and Studio operations are "
                        "not visible to Foundry."}

    def prune(self, days: int = 90) -> int:
        cut = time.time() - days * 86400
        n = self.db.execute_count("DELETE FROM work_events WHERE ts < ?", (cut,))
        n += self.db.execute_count("DELETE FROM work_sessions WHERE ended_ts IS NOT NULL "
                                   "AND ended_ts < ?", (cut,))
        return n


_REGISTRY: Optional[WorkRegistry] = None


def set_registry(reg: Optional[WorkRegistry]) -> None:
    global _REGISTRY
    _REGISTRY = reg


def get_registry() -> Optional[WorkRegistry]:
    return _REGISTRY

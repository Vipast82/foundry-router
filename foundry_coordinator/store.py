"""Durable coordinator state (SQLite, WAL).

Everything the coordinator knows lives here, so a restart resumes from the
recorded phase: the approved queue, task states and their transition history
(with evidence), coding sessions, idempotent operations (session starts,
reviews, pushes), verification results, reviews, human approvals, resource
locks, and an outbox of events for Foundry.

Every row that belongs to a project carries its project_id, and every lookup
of evidence filters by project, task and source revision together, so one
project's review or verification can never satisfy another's.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable, Optional

from . import states as S

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY, name TEXT, profile_path TEXT, profile_hash TEXT,
  profile_version TEXT, workspace TEXT, paused INTEGER DEFAULT 0, pause_note TEXT,
  registered_ts REAL, updated_ts REAL);
CREATE TABLE IF NOT EXISTS queue (
  project_id TEXT, task_id TEXT, position INTEGER, deps TEXT, approved_by TEXT,
  approved_ts REAL, note TEXT, PRIMARY KEY (project_id, task_id));
CREATE TABLE IF NOT EXISTS tasks (
  project_id TEXT, task_id TEXT, state TEXT, resume_state TEXT, attempt INTEGER DEFAULT 0,
  sessions INTEGER DEFAULT 0, run_id TEXT, session_id TEXT, phase TEXT, base_sha TEXT,
  head_sha TEXT, reviewed_sha TEXT, review_id TEXT, verification_id TEXT, closeout_id TEXT,
  blocker TEXT, awaiting TEXT, next_action TEXT, pause_reason TEXT, pause_until REAL,
  started_ts REAL, updated_ts REAL, data TEXT, PRIMARY KEY (project_id, task_id));
CREATE TABLE IF NOT EXISTS transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, project_id TEXT, task_id TEXT,
  from_state TEXT, to_state TEXT, evidence TEXT, actor TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, run_id TEXT, role TEXT,
  phase TEXT, attempt INTEGER, client TEXT, persona TEXT, status TEXT, created_ts REAL,
  prompt_ts REAL, ended_ts REAL, outcome TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS ops (
  key TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, kind TEXT, status TEXT,
  result TEXT, created_ts REAL, updated_ts REAL);
CREATE TABLE IF NOT EXISTS verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, set_id TEXT, project_id TEXT, task_id TEXT,
  revision TEXT, phase TEXT, check_name TEXT, result TEXT, required INTEGER,
  evidence TEXT, detail TEXT, executed_ts REAL, duration_ms INTEGER);
CREATE INDEX IF NOT EXISTS idx_ver_set ON verifications(set_id);
CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, reviewed_sha TEXT, role TEXT,
  persona TEXT, verdict TEXT, valid INTEGER, error TEXT, result TEXT, raw TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, kind TEXT, revision TEXT,
  artifacts TEXT, approver TEXT, note TEXT, ts REAL, revoked_ts REAL);
CREATE TABLE IF NOT EXISTS locks (
  resource TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, acquired_ts REAL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, ts REAL, project_id TEXT,
  task_id TEXT, session_id TEXT, kind TEXT, state TEXT, detail TEXT, pushed INTEGER DEFAULT 0);
-- Operator instructions from the control center (control.py), owned here: the
-- engine adds queued task-scoped ones to the next session prompt it sends, or a
-- pause-and-steer one to the continue prompt of that exact session.
CREATE TABLE IF NOT EXISTS operator_messages (
  msg_id TEXT PRIMARY KEY, action_id TEXT UNIQUE, project_id TEXT, task_id TEXT,
  session_id TEXT, scope TEXT, delivery TEXT, actor TEXT, text TEXT, status TEXT,
  created_ts REAL, delivered_session TEXT, delivered_ts REAL, detail TEXT,
  reported TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_opmsg_task ON operator_messages(project_id, task_id, status);
"""


def _j(v) -> str:
    return json.dumps(v, default=str, sort_keys=True)


def _uj(v, default=None):
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        if self.meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    # -- low level ---------------------------------------------------------------

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, tuple(args))
            return cur.lastrowid or cur.rowcount

    def tx(self):
        return _Tx(self)

    def meta(self, key: str) -> Optional[str]:
        r = self.q("SELECT value FROM meta WHERE key=?", (key,))
        return r[0]["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.x("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))

    # -- process ownership ----------------------------------------------------------

    def claim_owner(self, stale_s: float = 120, force: bool = False) -> str:
        """One coordinator process per store. A second one refuses unless the
        first stopped heart-beating (or --force)."""
        me = f"{socket.gethostname()}:{os.getpid()}"
        with self.tx():
            cur = _uj(self.meta("owner"), {})
            if cur and cur.get("id") != me and not force \
                    and time.time() - float(cur.get("beat") or 0) < stale_s:
                raise RuntimeError(f"another coordinator ({cur['id']}) is running on this store")
            self.set_meta("owner", _j({"id": me, "beat": time.time()}))
        return me

    def heartbeat(self, me: str) -> None:
        cur = _uj(self.meta("owner"), {})
        if cur.get("id") == me:
            self.set_meta("owner", _j({"id": me, "beat": time.time()}))

    def release_owner(self, me: str) -> None:
        cur = _uj(self.meta("owner"), {})
        if cur.get("id") == me:
            self.x("DELETE FROM meta WHERE key='owner'")

    # -- projects -----------------------------------------------------------------

    def register_project(self, project_id: str, name: str, profile_path: str,
                         profile_hash: str, version: str, workspace: str) -> dict:
        now = time.time()
        old = self.project(project_id)
        self.x("INSERT INTO projects (project_id, name, profile_path, profile_hash, "
               "profile_version, workspace, registered_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?) "
               "ON CONFLICT(project_id) DO UPDATE SET name=excluded.name, "
               "profile_path=excluded.profile_path, profile_hash=excluded.profile_hash, "
               "profile_version=excluded.profile_version, workspace=excluded.workspace, "
               "updated_ts=excluded.updated_ts",
               (project_id, name, profile_path, profile_hash, version, workspace, now, now))
        if old and old["profile_hash"] != profile_hash:
            self.event(project_id, "", "profile_changed",
                       detail={"old": old["profile_hash"], "new": profile_hash,
                               "version": version})
        return self.project(project_id)

    def project(self, project_id: str) -> Optional[dict]:
        r = self.q("SELECT * FROM projects WHERE project_id=?", (project_id,))
        return r[0] if r else None

    def projects(self) -> list[dict]:
        return self.q("SELECT * FROM projects ORDER BY project_id")

    def set_project_paused(self, project_id: str, paused: bool, note: str = "") -> None:
        self.x("UPDATE projects SET paused=?, pause_note=? WHERE project_id=?",
               (1 if paused else 0, note, project_id))
        self.event(project_id, "", "project_paused" if paused else "project_resumed",
                   detail={"note": note})

    # -- queue --------------------------------------------------------------------

    def approve(self, project_id: str, task_id: str, deps: list[str], approved_by: str,
                note: str = "") -> dict:
        """Put a task on the approved queue (and create its task row). An
        approved task keeps its place; approving again updates deps/note only
        while it hasn't started."""
        with self.tx():
            pos = (self.q("SELECT MAX(position) AS m FROM queue WHERE project_id=?",
                          (project_id,))[0]["m"] or 0) + 1
            existing = self.task(project_id, task_id)
            if existing and existing["state"] not in (S.QUEUED,):
                raise ValueError(f"{project_id}/{task_id} is already {existing['state']}")
            self.x("INSERT INTO queue (project_id, task_id, position, deps, approved_by, "
                   "approved_ts, note) VALUES (?,?,?,?,?,?,?) ON CONFLICT(project_id, task_id) "
                   "DO UPDATE SET deps=excluded.deps, approved_by=excluded.approved_by, "
                   "approved_ts=excluded.approved_ts, note=excluded.note",
                   (project_id, task_id, pos, _j(sorted(set(deps))), approved_by, time.time(),
                    note))
            if not existing:
                self.x("INSERT INTO tasks (project_id, task_id, state, updated_ts, data) "
                       "VALUES (?,?,?,?,?)", (project_id, task_id, S.QUEUED, time.time(), "{}"))
                self._record(project_id, task_id, "", S.QUEUED,
                             {"approved_by": approved_by, "deps": sorted(set(deps)),
                              "note": note}, "operator")
        return self.queue_entry(project_id, task_id)

    def queue_entry(self, project_id: str, task_id: str) -> Optional[dict]:
        r = self.q("SELECT * FROM queue WHERE project_id=? AND task_id=?", (project_id, task_id))
        if not r:
            return None
        r[0]["deps"] = _uj(r[0]["deps"], [])
        return r[0]

    def queue(self, project_id: str) -> list[dict]:
        out = self.q("SELECT * FROM queue WHERE project_id=? ORDER BY position", (project_id,))
        for r in out:
            r["deps"] = _uj(r["deps"], [])
        return out

    # -- tasks --------------------------------------------------------------------

    def task(self, project_id: str, task_id: str) -> Optional[dict]:
        r = self.q("SELECT * FROM tasks WHERE project_id=? AND task_id=?", (project_id, task_id))
        if not r:
            return None
        r[0]["data"] = _uj(r[0]["data"], {})
        return r[0]

    def tasks(self, project_id: Optional[str] = None) -> list[dict]:
        sql = ("SELECT t.* FROM tasks t LEFT JOIN queue q ON q.project_id=t.project_id AND "
               "q.task_id=t.task_id ")
        rows = self.q(sql + ("WHERE t.project_id=? " if project_id else "")
                      + "ORDER BY t.project_id, q.position", (project_id,) if project_id else ())
        for r in rows:
            r["data"] = _uj(r["data"], {})
        return rows

    def update_task(self, project_id: str, task_id: str, **fields) -> None:
        if "data" in fields and isinstance(fields["data"], dict):
            fields["data"] = _j(fields["data"])
        fields["updated_ts"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE tasks SET {cols} WHERE project_id=? AND task_id=?",
               (*fields.values(), project_id, task_id))

    def merge_data(self, project_id: str, task_id: str, **kv) -> dict:
        with self.tx():
            t = self.task(project_id, task_id)
            data = dict(t["data"] or {})
            for k, v in kv.items():
                if v is None:
                    data.pop(k, None)
                else:
                    data[k] = v
            self.update_task(project_id, task_id, data=data)
        return data

    def transition(self, project_id: str, task_id: str, to: str, evidence: dict,
                   actor: str = "engine", **fields) -> dict:
        """The only way a task changes state: checked against states.py, the
        evidence recorded with the transition, in one transaction."""
        with self.tx():
            t = self.task(project_id, task_id)
            if t is None:
                raise S.TransitionError(f"unknown task {project_id}/{task_id}")
            S.check(t["state"], to, evidence)
            upd = dict(fields)
            upd["state"] = to
            if to == S.PAUSED:
                upd["resume_state"] = evidence["resume_state"]
                upd["pause_reason"] = str(evidence.get("reason"))[:500]
            elif t["state"] == S.PAUSED:
                upd.setdefault("resume_state", None)
                upd.setdefault("pause_reason", None)
                upd.setdefault("pause_until", None)
            if to == S.BLOCKED and "blocker" not in upd:
                upd["blocker"] = str(evidence.get("blocker") or "")[:1000]
            if to == S.AWAITING_HUMAN and "awaiting" not in upd:
                upd["awaiting"] = str(evidence.get("awaiting") or "")[:1000]
            if to not in (S.BLOCKED,) and "blocker" not in upd:
                upd["blocker"] = None if to != S.PAUSED else t["blocker"]
            if to not in (S.AWAITING_HUMAN, S.PAUSED) and "awaiting" not in upd:
                upd["awaiting"] = None
            self.update_task(project_id, task_id, **upd)
            if to in (S.FAILED, S.BLOCKED, S.CANCELLED, S.COMPLETE):
                # a task that stops holds nothing (workspace, Studio, writer slot)
                self.release(project_id, task_id)
            self._record(project_id, task_id, t["state"], to, evidence, actor)
        return self.task(project_id, task_id)

    def _record(self, project_id, task_id, frm, to, evidence, actor):
        self.x("INSERT INTO transitions (ts, project_id, task_id, from_state, to_state, "
               "evidence, actor) VALUES (?,?,?,?,?,?,?)",
               (time.time(), project_id, task_id, frm, to, _j(evidence)[:20000], actor))
        self.event(project_id, task_id, "transition", state=to,
                   detail={"from": frm, "actor": actor,
                           **{k: v for k, v in evidence.items()
                              if k in ("reason", "blocker", "head_sha", "session_id", "verdict",
                                       "awaiting", "run_id", "reviewed_commit_sha", "published")}})

    def history(self, project_id: str, task_id: str) -> list[dict]:
        rows = self.q("SELECT * FROM transitions WHERE project_id=? AND task_id=? ORDER BY id",
                      (project_id, task_id))
        for r in rows:
            r["evidence"] = _uj(r["evidence"], {})
        return rows

    # -- sessions -----------------------------------------------------------------

    def add_session(self, session_id: str, project_id: str, task_id: str, run_id: str,
                    role: str, phase: str, attempt: int, client: str, persona: str,
                    data: Optional[dict] = None) -> None:
        self.x("INSERT OR IGNORE INTO sessions (session_id, project_id, task_id, run_id, role, "
               "phase, attempt, client, persona, status, created_ts, data) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (session_id, project_id, task_id, run_id, role, phase, attempt, client, persona,
                "created", time.time(), _j(data or {})))

    def session(self, session_id: str) -> Optional[dict]:
        r = self.q("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        if not r:
            return None
        r[0]["data"] = _uj(r[0]["data"], {})
        return r[0]

    def update_session(self, session_id: str, **fields) -> None:
        if "data" in fields and isinstance(fields["data"], dict):
            fields["data"] = _j(fields["data"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE sessions SET {cols} WHERE session_id=?", (*fields.values(), session_id))

    def sessions(self, project_id: str, task_id: Optional[str] = None) -> list[dict]:
        rows = self.q("SELECT * FROM sessions WHERE project_id=?"
                      + (" AND task_id=?" if task_id else "") + " ORDER BY created_ts",
                      (project_id, task_id) if task_id else (project_id,))
        for r in rows:
            r["data"] = _uj(r["data"], {})
        return rows

    # -- idempotent operations -------------------------------------------------------

    def op(self, key: str) -> Optional[dict]:
        r = self.q("SELECT * FROM ops WHERE key=?", (key,))
        if not r:
            return None
        r[0]["result"] = _uj(r[0]["result"], {})
        return r[0]

    def op_begin(self, key: str, project_id: str, task_id: str, kind: str) -> Optional[dict]:
        """Start an operation once. Returns None when it is new (go ahead), or
        the existing row (already started/done: resume it, never repeat)."""
        with self.tx():
            existing = self.op(key)
            if existing:
                return existing
            self.x("INSERT INTO ops (key, project_id, task_id, kind, status, result, created_ts, "
                   "updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                   (key, project_id, task_id, kind, "started", "{}", time.time(), time.time()))
        return None

    def op_update(self, key: str, status: str, result: Optional[dict] = None) -> None:
        self.x("UPDATE ops SET status=?, result=?, updated_ts=? WHERE key=?",
               (status, _j(result or {}), time.time(), key))

    # -- verification -------------------------------------------------------------------

    def save_verification(self, project_id: str, task_id: str, revision: str, phase: str,
                          results: list) -> str:
        set_id = f"ver-{uuid.uuid4().hex[:12]}"
        with self.tx():
            for r in results:
                self.x("INSERT INTO verifications (set_id, project_id, task_id, revision, phase, "
                       "check_name, result, required, evidence, detail, executed_ts, duration_ms) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (set_id, project_id, task_id, revision, phase, r.check, r.result,
                        1 if r.required else 0, r.evidence or "", (r.detail or "")[:4000],
                        r.executed_ts, r.duration_ms))
        return set_id

    def verification(self, set_id: str, project_id: str, task_id: str) -> list[dict]:
        """A verification set, only if it belongs to this project AND task."""
        return self.q("SELECT * FROM verifications WHERE set_id=? AND project_id=? AND task_id=? "
                      "ORDER BY id", (set_id, project_id, task_id))

    # -- reviews -------------------------------------------------------------------------

    def save_review(self, project_id: str, task_id: str, reviewed_sha: str, role: str,
                    persona: str, verdict: str, valid: bool, error: str, result: dict,
                    raw: str) -> str:
        rid = f"rev-{uuid.uuid4().hex[:12]}"
        self.x("INSERT INTO reviews (review_id, project_id, task_id, reviewed_sha, role, persona, "
               "verdict, valid, error, result, raw, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (rid, project_id, task_id, reviewed_sha, role, persona, verdict, 1 if valid else 0,
                error[:1000], _j(result), raw[:50000], time.time()))
        return rid

    def reviews(self, project_id: str, task_id: str, sha: Optional[str] = None) -> list[dict]:
        rows = self.q("SELECT * FROM reviews WHERE project_id=? AND task_id=?"
                      + (" AND reviewed_sha=?" if sha else "") + " ORDER BY ts",
                      (project_id, task_id, sha) if sha else (project_id, task_id))
        for r in rows:
            r["result"] = _uj(r["result"], {})
        return rows

    def review(self, review_id: str, project_id: str, task_id: str) -> Optional[dict]:
        r = self.q("SELECT * FROM reviews WHERE review_id=? AND project_id=? AND task_id=?",
                   (review_id, project_id, task_id))
        if not r:
            return None
        r[0]["result"] = _uj(r[0]["result"], {})
        return r[0]

    # -- approvals -----------------------------------------------------------------------

    def add_approval(self, project_id: str, task_id: str, kind: str, revision: str,
                     artifacts: dict, approver: str, note: str = "") -> str:
        aid = f"apr-{uuid.uuid4().hex[:12]}"
        self.x("INSERT INTO approvals (approval_id, project_id, task_id, kind, revision, "
               "artifacts, approver, note, ts) VALUES (?,?,?,?,?,?,?,?,?)",
               (aid, project_id, task_id, kind, revision, _j(artifacts), approver, note,
                time.time()))
        self.event(project_id, task_id, "approval", detail={"kind": kind, "revision": revision,
                                                            "approver": approver})
        return aid

    def approvals(self, project_id: str, task_id: str, kind: Optional[str] = None) -> list[dict]:
        rows = self.q("SELECT * FROM approvals WHERE project_id=? AND task_id=? AND revoked_ts "
                      "IS NULL" + (" AND kind=?" if kind else "") + " ORDER BY ts",
                      (project_id, task_id, kind) if kind else (project_id, task_id))
        for r in rows:
            r["artifacts"] = _uj(r["artifacts"], {})
        return rows

    def revoke_approvals(self, project_id: str, task_id: str, reason: str) -> int:
        n = self.x("UPDATE approvals SET revoked_ts=?, note=note || ' [revoked: ' || ? || ']' "
                   "WHERE project_id=? AND task_id=? AND revoked_ts IS NULL",
                   (time.time(), reason, project_id, task_id))
        return n

    # -- locks ---------------------------------------------------------------------------

    def acquire(self, resources: list[str], project_id: str, task_id: str) -> Optional[dict]:
        """All or nothing. Returns None on success, else the lock that is held
        by someone else."""
        with self.tx():
            for res in resources:
                r = self.q("SELECT * FROM locks WHERE resource=?", (res,))
                if r and (r[0]["project_id"], r[0]["task_id"]) != (project_id, task_id):
                    return r[0]
            for res in resources:
                self.x("INSERT OR IGNORE INTO locks (resource, project_id, task_id, acquired_ts) "
                       "VALUES (?,?,?,?)", (res, project_id, task_id, time.time()))
        return None

    def release(self, project_id: str, task_id: str, resources: Optional[list[str]] = None) -> None:
        if resources is None:
            self.x("DELETE FROM locks WHERE project_id=? AND task_id=?", (project_id, task_id))
        else:
            for res in resources:
                self.x("DELETE FROM locks WHERE resource=? AND project_id=? AND task_id=?",
                       (res, project_id, task_id))

    def locks(self) -> list[dict]:
        return self.q("SELECT * FROM locks ORDER BY resource")

    def holds(self, project_id: str, task_id: str) -> list[str]:
        return [r["resource"] for r in self.q("SELECT resource FROM locks WHERE project_id=? AND "
                                               "task_id=?", (project_id, task_id))]

    # -- events (outbox for Foundry) -------------------------------------------------------

    def event(self, project_id: str, task_id: str, kind: str, state: str = "",
              session_id: str = "", detail: Optional[dict] = None) -> None:
        self.x("INSERT INTO events (event_id, ts, project_id, task_id, session_id, kind, state, "
               "detail) VALUES (?,?,?,?,?,?,?,?)",
               (f"ev-{uuid.uuid4().hex}", time.time(), project_id, task_id, session_id, kind,
                state, _j(detail or {})[:4000]))

    def unpushed_events(self, limit: int = 100) -> list[dict]:
        rows = self.q("SELECT * FROM events WHERE pushed=0 ORDER BY id LIMIT ?", (limit,))
        for r in rows:
            r["detail"] = _uj(r["detail"], {})
        return rows

    def mark_pushed(self, ids: list[int]) -> None:
        for i in ids:
            self.x("UPDATE events SET pushed=1 WHERE id=?", (i,))

    def events(self, project_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        rows = self.q("SELECT * FROM events" + (" WHERE project_id=?" if project_id else "")
                      + " ORDER BY id DESC LIMIT ?",
                      (project_id, limit) if project_id else (limit,))
        for r in rows:
            r["detail"] = _uj(r["detail"], {})
        return rows


    # -- operator messages (control center) ------------------------------------------

    def add_operator_message(self, action_id: str, project_id: str, task_id: str,
                             session_id: str, scope: str, delivery: str, actor: str,
                             text: str) -> dict:
        """Idempotent per action id."""
        self.x("INSERT OR IGNORE INTO operator_messages (msg_id, action_id, project_id, task_id, "
               "session_id, scope, delivery, actor, text, status, created_ts) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (f"om-{uuid.uuid4().hex[:12]}", action_id, project_id, task_id, session_id, scope,
                delivery, actor, text, "queued", time.time()))
        return self.q("SELECT * FROM operator_messages WHERE action_id=?", (action_id,))[0]

    def operator_message(self, action_id: str) -> Optional[dict]:
        r = self.q("SELECT * FROM operator_messages WHERE action_id=?", (action_id,))
        return r[0] if r else None

    def operator_messages(self, status: Optional[str] = None) -> list[dict]:
        return self.q("SELECT * FROM operator_messages" + (" WHERE status=?" if status else "")
                      + " ORDER BY created_ts", (status,) if status else ())

    def pending_operator_messages(self, project_id: str, task_id: str,
                                  session_id: str = "") -> list[dict]:
        """Queued instructions deliverable now: every task-scoped one, plus
        session-scoped ones only for that exact session (never reassigned)."""
        return self.q("SELECT * FROM operator_messages WHERE project_id=? AND task_id=? AND "
                      "status='queued' AND (scope='task' OR (scope='session' AND session_id=? "
                      "AND ?!='')) ORDER BY created_ts",
                      (project_id, task_id, session_id, session_id))

    def mark_operator_messages(self, msg_ids: list[str], status: str, session_id: str = "",
                               detail: str = "") -> None:
        for m in msg_ids:
            self.x("UPDATE operator_messages SET status=?, delivered_session=COALESCE(NULLIF(?,''), "
                   "delivered_session), delivered_ts=CASE WHEN ?='delivered' THEN ? ELSE "
                   "delivered_ts END, detail=? WHERE msg_id=?",
                   (status, session_id, status, time.time(), detail[:500], m))

    def set_operator_reported(self, msg_id: str, reported: str) -> None:
        self.x("UPDATE operator_messages SET reported=? WHERE msg_id=?", (reported, msg_id))


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT (re-entrant: nested blocks join the outer one)."""

    def __init__(self, store: Store):
        self.s = store

    def __enter__(self):
        self.s._lock.acquire()
        depth = getattr(self.s, "_depth", 0)
        if depth == 0:
            self.s.conn.execute("BEGIN IMMEDIATE")
        self.s._depth = depth + 1
        return self.s

    def __exit__(self, exc_type, exc, tb):
        self.s._depth -= 1
        try:
            if self.s._depth == 0:
                self.s.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.s._lock.release()
        return False

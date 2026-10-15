"""Builders for the training-pipeline acceptance tests.

The Kilo session is a real `kilo export` (Kilo 7.8.8, captured from the
coordinator pilot; paths rewritten to /work). Everything else is built here:
a git workspace whose history matches the session, the coordinator's store
(through foundry_coordinator.store.Store, so the rows are the coordinator's
own), Foundry capture rows in the exact shape foundry_router.capture writes,
and a project profile.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from foundry_coordinator.adapters.base import VerificationResult
from foundry_coordinator.store import Store
from foundry_router.capture import BOUNDARY, CaptureStore
from foundry_training import config as TC
from foundry_training.pipeline import Pipeline
from foundry_training.store import TrainingStore

FIX = Path(__file__).parent / "fixtures"
KILO_EXPORT = FIX / "kilo-7.8.8-export-fx1.json"
KILO_SANITIZED = FIX / "kilo-7.8.8-export-fx1-sanitized.json"
SESSION = "ses_ebffc2fbdffeMEENPhJKWmDTVf"
LOCAL = "qwen3.8-flash-next-iq3_s"


def kilo_export() -> dict:
    return json.loads(KILO_EXPORT.read_text())


def git(ws, *args) -> str:
    return subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True,
                          check=True).stdout.strip()


def workspace(root: Path, name: str = "fixture-game") -> tuple[str, str, str]:
    """A repo with a base commit and the session's commit. -> (path, base, head)"""
    ws = root / name
    ws.mkdir(parents=True)
    git(ws, "init", "-q", "-b", "main")
    git(ws, "config", "user.email", "t@example.invalid")
    git(ws, "config", "user.name", "t")
    (ws / "README.md").write_text("fixture\n")
    git(ws, "add", ".")
    git(ws, "commit", "-q", "-m", "base")
    base = git(ws, "rev-parse", "HEAD")
    (ws / "src").mkdir()
    (ws / "src" / "greeting.txt").write_text("OK hello\n")
    git(ws, "add", ".")
    git(ws, "commit", "-q", "-m", "FX-1: greeting")
    return str(ws), base, git(ws, "rev-parse", "HEAD")


def profile(root: Path, pid: str, ws: str, extra: str = "", eval_fraction: float = 0.0,
            sources=("kilo", "foundry", "coordinator", "git"), enabled: bool = True) -> str:
    d = root / "profiles" / pid
    d.mkdir(parents=True, exist_ok=True)
    p = d / "project.toml"
    p.write_text(f"""schema_version = 1
id = "{pid}"
name = "{pid}"
[workspace]
path = "{ws}"
[roles]
implementer = "Foundry-Kilo-Local"
reviewer = "Foundry-Kilo-Sonnet"
[training]
enabled = {"true" if enabled else "false"}
sources = {json.dumps(list(sources))}
local_models = ["{LOCAL}"]
local_personas = ["Foundry-Kilo-Local"]
artifact_roots = ["docs/evidence"]
[training.split]
eval_fraction = {eval_fraction}
seed = "test"
{extra}
""")
    return str(p)


def pipeline(root: Path, profiles: list[str], known=()) -> Pipeline:
    return Pipeline(TrainingStore(str(root / "store")), [TC.load(p) for p in profiles],
                    known_secrets=list(known))


def coordinator(root: Path, pid: str, tid: str, session: str, base: str, head: str,
                verdict: str = "APPROVED", checks=(("fixture-check", "PASS"),),
                attempt: int = 1, findings=None, store: Store | None = None,
                review_head: str | None = None) -> Store:
    """The coordinator's own rows for one implementation attempt."""
    st = store or Store(str(root / "coordinator.sqlite"))
    if st.task(pid, tid) is None:
        st.approve(pid, tid, [], "tester")
    run = f"{tid}-a{attempt}"
    st.add_session(session, pid, tid, run, "implementer", "implement", attempt, "kilo",
                   "Foundry-Kilo-Local")
    st._record(pid, tid, "READY", "IMPLEMENTING", {"session_id": session, "base_sha": base,
                                                   "run_id": run}, "engine")
    st._record(pid, tid, "IMPLEMENTING", "VERIFYING", {"session_id": session, "head_sha": head},
               "engine")
    st.save_verification(pid, tid, head, "verify",
                         [VerificationResult(c, r, evidence=f"ev/{c}.txt") for c, r in checks])
    if verdict:
        st.save_review(pid, tid, review_head or head, "reviewer", "Foundry-Kilo-Sonnet", verdict,
                       True, "", {"verdict": verdict, "findings": findings or []}, "{}")
    nxt = {"APPROVED": "APPROVED", "CHANGES_REQUIRED": "CHANGES_REQUIRED"}.get(verdict)
    if nxt:
        ev = {"reviewed_commit_sha": head}
        if findings:
            ev["findings"] = [f["text"] for f in findings]
        st._record(pid, tid, "REVIEWING", nxt, ev, "engine")
    return st


# ------------------------------------------------------------------ Foundry capture

TOOLS = [{"type": "function", "function": {
    "name": "write", "description": "Write a file",
    "parameters": {"type": "object", "properties": {"filePath": {"type": "string"},
                                                    "content": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "bash", "description": "Run a command",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"},
                                                        "description": {"type": "string"}}}}}]


def _kilo_turns(exp: dict):
    """(user prompt, [(tool name, call id, args, output)], final text) from the export."""
    user = exp["messages"][0]["parts"][0]["text"]
    calls, final = [], ""
    for m in exp["messages"][1:]:
        for p in m["parts"]:
            if p["type"] == "tool":
                calls.append((p["tool"], p["callID"], p["state"]["input"], p["state"]["output"]))
            elif p["type"] == "text":
                final = p["text"]
    return user, calls, final


def capture_rows(exp: dict, pid: str = "fixture-game", tid: str = "FX-1",
                 session: str = SESSION, persona: str = "Foundry-Kilo-Local",
                 model: str = LOCAL, system: str = "You are Kilo, a coding agent.",
                 role: str = "implementer") -> list[dict]:
    """What foundry_router.capture records for this Kilo session: one received
    row + one backend_call per model call, growing context each time."""
    user, calls, final = _kilo_turns(exp)
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    rows = []
    t = time.time() - 600
    responses = [{"content": "", "tool_calls": [{"id": cid, "function": {"name": n,
                                                                          "arguments": a}}]}
                 for n, cid, a, _ in calls] + [{"content": final, "tool_calls": []}]
    for i, resp in enumerate(responses):
        req_id = uuid.uuid4().hex[:8]
        common = {"request_id": req_id, "client_id": "kilo", "session_id": session,
                  "parent_task_id": "", "project_id": pid, "work_task_id": tid,
                  "run_id": f"{tid}-a1", "role": role, "persona": persona,
                  "redaction": json.dumps({"status": "clean", "kinds": {}}),
                  "boundary": BOUNDARY, "truncated": 0}
        rows.append({**common, "record_uid": uuid.uuid4().hex, "ts": t + i * 10,
                     "kind": "received", "attempt": 0, "stream": 1, "status": "received",
                     "model": persona, "backend": "", "reconstruction": "",
                     "body": json.dumps({"dialect": "openai", "request": {
                         "model": persona, "messages": copy.deepcopy(msgs), "tools": TOOLS,
                         "stream": True}})})
        rows.append({**common, "record_uid": uuid.uuid4().hex, "ts": t + i * 10 + 1,
                     "kind": "backend_call", "attempt": 1, "stream": 1, "status": "ok",
                     "model": model, "backend": "local-llama", "reconstruction": "complete",
                     "body": json.dumps({"request": {"model": model,
                                                     "messages": copy.deepcopy(msgs),
                                                     "tools": TOOLS, "settings": {}},
                                         "response": {**resp, "finish_reason": "stop",
                                                      "reasoning": "",
                                                      "usage": {"prompt_tokens": 900,
                                                                "completion_tokens": 25}},
                                         "error": "", "duration_ms": 400})})
        if resp["tool_calls"]:
            n, cid, a, out = calls[i]
            msgs.append({"role": "assistant", "content": "",
                         "tool_calls": [{"id": cid, "function": {"name": n, "arguments": a}}]})
            msgs.append({"role": "tool", "tool_call_id": cid, "content": out})
    return rows


def capture_db(root: Path, rows: list[dict], name: str = "capture.sqlite") -> str:
    path = root / "foundry-data" / "training" / name
    cs = CaptureStore(path)
    for r in rows:
        r = dict(r)
        r["bytes"] = len(r["body"])
        cs.insert(r)
    cs.conn.close()
    return str(path)


# ------------------------------------------------------------------ Kilo DB (synthetic)

KILO_SCHEMA = """
CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, workspace_id text,
  parent_id text, slug text NOT NULL, directory text NOT NULL, path text, title text NOT NULL,
  version text NOT NULL, share_url text, metadata text, permission text, agent text, model text,
  time_created integer NOT NULL, time_updated integer NOT NULL);
CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL,
  time_updated integer NOT NULL, data text NOT NULL);
CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
CREATE TABLE credential (id text PRIMARY KEY, integration_id text, label text NOT NULL,
  value text NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL);
CREATE TABLE session_share (session_id text PRIMARY KEY, id text NOT NULL, secret text NOT NULL,
  url text NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL);
CREATE TABLE migration (id TEXT PRIMARY KEY, time_completed INTEGER NOT NULL);
"""


def kilo_db(root: Path, sessions: list[dict], credential: str) -> str:
    """A database with Kilo 7.8.8's table layout (columns the adapter reads,
    plus the credential/share tables it must never read)."""
    path = root / "kilo-home" / "kilo.db"
    path.parent.mkdir(parents=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(KILO_SCHEMA)
    for s in sessions:
        i = s["info"]
        c.execute("INSERT INTO session (id, project_id, parent_id, slug, directory, title, version, "
                  "metadata, time_created, time_updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (i["id"], i.get("projectID") or "p", i.get("parentID"), i.get("slug") or "s",
                   i.get("directory") or "/work", i.get("title") or "", i.get("version") or "7.8.8",
                   json.dumps(i.get("metadata") or {}), i["time"]["created"], i["time"]["updated"]))
        for m in s["messages"]:
            mi = dict(m["info"])
            mid = mi.pop("id")
            c.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                      (mid, i["id"], mi["time"]["created"], mi["time"]["created"], json.dumps(mi)))
            for k, p in enumerate(m["parts"]):
                p = dict(p)
                pid = p.pop("id")
                c.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                          (pid, mid, i["id"], mi["time"]["created"] + k, mi["time"]["created"],
                           json.dumps(p)))
        c.execute("INSERT INTO session_share VALUES (?,?,?,?,?,?)",
                  (i["id"], "sh", credential + "-share", "https://example.invalid/s", 1, 1))
    c.execute("INSERT INTO credential VALUES (?,?,?,?,?,?)",
              ("cred1", "x", "provider", credential, 1, 1))
    c.execute("INSERT INTO migration VALUES ('20260901_x', 1)")
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    return str(path)


def file_state(path: str) -> dict:
    import hashlib
    out = {}
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            out[suffix or "db"] = (hashlib.sha256(open(p, "rb").read()).hexdigest(),
                                   os.stat(p).st_mtime_ns)
    return out


def clone_session(exp: dict, new_id: str, *, project: str = "fixture-game", task: str = "FX-1",
                  attempt: int = 1, start_ms: int | None = None, parent: str | None = None,
                  role: str = "implementer", directory: str | None = None,
                  write_content: str | None = None, final_text: str | None = None) -> dict:
    """A copy of the real export as another session (ids, times and the
    coordinator metadata rewritten; structure untouched)."""
    s = json.loads(json.dumps(exp).replace(SESSION, new_id))
    tag = new_id[-6:]
    t0 = s["info"]["time"]["created"]
    shift = (start_ms - t0) if start_ms is not None else 0

    def retime(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("created", "updated", "completed", "start", "end") and \
                        isinstance(v, int) and v > 10**12:
                    o[k] = v + shift
                else:
                    retime(v)
        elif isinstance(o, list):
            for v in o:
                retime(v)
    retime(s)
    s["info"]["metadata"] = {"coordinator": {"project": project, "task": task,
                                             "run": f"{task}-a{attempt}", "attempt": attempt,
                                             "phase": "implement", "role": role}}
    if parent:
        s["info"]["parentID"] = parent
    if directory:
        s["info"]["directory"] = directory
    for m in s["messages"]:
        m["info"]["id"] = m["info"]["id"] + tag
        for p in m["parts"]:
            p["id"] = p["id"] + tag
            if p["type"] == "tool":
                p["callID"] = p["callID"] + tag
                if write_content is not None and p["tool"] == "write":
                    p["state"]["input"]["content"] = write_content
            if p["type"] == "text" and final_text is not None and m["info"]["role"] == "assistant":
                p["text"] = final_text
    return s


def write_json(path: Path, obj) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))
    return str(path)

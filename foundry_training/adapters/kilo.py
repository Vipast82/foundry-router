"""Kilo sessions -> normalized events.

Three supported inputs, one normalizer:
  export   a file from `kilo export <session>` ({info, messages[{info, parts}]})
  api      a running `kilo serve`: GET /session/{id}, /session/{id}/message,
           /session/{id}/children (same shape as the export)
  db       Kilo's SQLite store, never opened in place: its files are copied
           byte for byte to a private snapshot (SQLite would create -wal/-shm
           files next to a WAL database even with mode=ro), and the copy is
           checked and read. Schema checked (session/message/part with the
           columns below); credential, account and share tables are never
           read (they exist in the copy, which is deleted afterwards).

Verified against Kilo 7.8.8 (docs/TRAINING_DATA.md, capability report):
  present  user prompts, assistant text, reasoning parts, tool calls with
           inputs, tool results with output / error / metadata (exit code,
           `truncated`), patches (file edits), step boundaries with tokens,
           session metadata (the coordinator's project/task/run), parentID
  absent   the system prompt and the tool definitions the model saw (Foundry
           capture supplies those), child sessions (separate sessions; follow
           /children or parentID), image bytes for file references unless the
           part carries a data URL
  sanitize `kilo export --sanitize` replaces every text, tool input/output and
           path with placeholders: safe to share, not usable for training. It
           is kept only as provenance (sanitization = "kilo-sanitized").

Kilo's `reasoning` parts are stored as reasoning events (in pilots they held
Foundry's own status lines); training exports leave them out.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import urllib.parse
import urllib.request
from typing import Iterator, Optional

from ..schema import Event

ADAPTER = "kilo"
REQUIRED_DB = {"session": {"id", "project_id", "parent_id", "directory", "title", "version",
                           "time_created"},
               "message": {"id", "session_id", "time_created", "data"},
               "part": {"id", "message_id", "session_id", "time_created", "data"}}


class KiloSourceError(RuntimeError):
    pass


# ---------------------------------------------------------------------------- inputs

def from_export(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict) or "info" not in d or "messages" not in d:
        raise KiloSourceError(f"{path}: not a `kilo export` file")
    text = json.dumps(d)[:200000]
    d["_sanitized"] = "[redacted:" in text and "[redacted:text:" in text
    return d


def from_api(url: str, session_id: str, password: str = "", directory: str = "") -> dict:
    import base64

    def get(path):
        q = ("?" + urllib.parse.urlencode({"directory": directory})) if directory else ""
        req = urllib.request.Request(url.rstrip("/") + path + q)
        if password:
            req.add_header("authorization", "Basic " + base64.b64encode(
                f"kilo:{password}".encode()).decode())
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    info = get(f"/session/{session_id}")
    if not info:
        raise KiloSourceError(f"no session {session_id}")
    return {"info": info, "messages": get(f"/session/{session_id}/message") or [],
            "children": [c.get("id") for c in get(f"/session/{session_id}/children") or []],
            "_sanitized": False}


def db_snapshot(db_path: str, attempts: int = 3) -> str:
    """A private copy of Kilo's DB, made by reading its files only.

    SQLite can't open a WAL-mode database without creating or touching its
    -wal/-shm files, even with mode=ro, so Kilo's database is never opened
    in place: the main file and its -wal (when present) are copied byte for
    byte into a private 0700 directory, the copy is checked (quick_check,
    schema) and its WAL folded in there. If Kilo wrote while we copied
    (size/mtime changed), the copy is retried."""
    if not os.path.isfile(db_path):
        raise KiloSourceError(f"{db_path} not found")

    def stamp():
        return [(os.stat(p).st_size, os.stat(p).st_mtime_ns) if os.path.exists(p) else None
                for p in (db_path, db_path + "-wal")]
    d = tempfile.mkdtemp(prefix="kilo-ro-")
    os.chmod(d, 0o700)
    dst_path = os.path.join(d, "kilo-snapshot.db")
    try:
        for _ in range(max(1, attempts)):
            for f in (dst_path, dst_path + "-wal", dst_path + "-shm"):
                if os.path.exists(f):
                    os.remove(f)
            before = stamp()
            shutil.copyfile(db_path, dst_path)
            if os.path.exists(db_path + "-wal"):
                shutil.copyfile(db_path + "-wal", dst_path + "-wal")
            if stamp() == before:
                break
        else:
            raise KiloSourceError("Kilo kept writing its database while it was copied; "
                                  "try again, or use `kilo export` / the server API")
        dst = sqlite3.connect(dst_path)
        try:
            ok = dst.execute("PRAGMA quick_check").fetchone()[0]
            if ok != "ok":
                raise KiloSourceError(f"the copied Kilo database failed quick_check: {ok}")
            for table, need in REQUIRED_DB.items():
                have = {r[1] for r in dst.execute(f"PRAGMA table_info({table})")}
                missing = need - have
                if missing:
                    raise KiloSourceError(f"Kilo DB schema changed: {table} lacks "
                                          f"{sorted(missing)}; use `kilo export` files instead")
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
        os.chmod(dst_path, 0o600)
        return dst_path
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise


def from_db(snapshot_path: str, session_ids: Optional[list] = None,
            directory: str = "") -> Iterator[dict]:
    c = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        q = ("SELECT id, project_id, parent_id, slug, directory, title, version, time_created, "
             "time_updated FROM session")
        rows = c.execute(q).fetchall()
        for s in rows:
            if session_ids and s["id"] not in session_ids:
                continue
            if directory and os.path.normcase(os.path.realpath(s["directory"] or "")) != \
                    os.path.normcase(os.path.realpath(directory)):
                continue
            msgs = []
            for m in c.execute("SELECT id, data, time_created FROM message WHERE session_id=? "
                               "ORDER BY time_created, id", (s["id"],)):
                info = json.loads(m["data"])
                info.setdefault("id", m["id"])
                parts = [json.loads(p["data"]) | {"id": p["id"]} for p in c.execute(
                    "SELECT id, data FROM part WHERE message_id=? ORDER BY time_created, id",
                    (m["id"],))]
                msgs.append({"info": info, "parts": parts})
            yield {"info": {"id": s["id"], "projectID": s["project_id"], "parentID": s["parent_id"],
                            "directory": s["directory"], "title": s["title"],
                            "version": s["version"], "time": {"created": s["time_created"],
                                                              "updated": s["time_updated"]},
                            "metadata": _session_metadata(c, s["id"])},
                   "messages": msgs, "_sanitized": False, "_from_db": True}
    finally:
        c.close()


def _session_metadata(c, sid) -> dict:
    cols = {r[1] for r in c.execute("PRAGMA table_info(session)")}
    if "metadata" not in cols:
        return {}
    r = c.execute("SELECT metadata FROM session WHERE id=?", (sid,)).fetchone()
    try:
        return json.loads(r[0]) if r and r[0] else {}
    except ValueError:
        return {}


# ------------------------------------------------------------------------ normalizer

def project_of(session: dict, projects: list) -> tuple[str, str]:
    """(project id, how it was decided). The coordinator's metadata is an
    observed fact; a configured workspace path match is a deterministic
    mapping. No match -> '' (the session is skipped, not guessed)."""
    from ..config import by_workspace
    info = session.get("info") or {}
    md = ((info.get("metadata") or {}).get("coordinator") or {})
    if md.get("project"):
        return str(md["project"]), "session metadata (coordinator)"
    p = by_workspace(projects, info.get("directory") or "")
    if p:
        return p.id, "configured workspace path"
    return "", "no project"


def normalize(session: dict, project_id: str, raw_ref: str, redaction: dict,
              store=None, artifact_roots: Optional[list] = None) -> list[Event]:
    info = session.get("info") or {}
    sid = info.get("id") or "unknown"
    md = ((info.get("metadata") or {}).get("coordinator") or {})
    sanitized = bool(session.get("_sanitized"))
    common = dict(project_id=project_id, session_id=sid, task_id=md.get("task"),
                  parent_session_id=info.get("parentID"), raw_ref=raw_ref,
                  redaction_status=redaction.get("status", "clean"))
    ev: list[Event] = []
    ts0 = ((info.get("time") or {}).get("created") or 0) / 1000.0
    ev.append(Event(ADAPTER, sid, "session_start", timestamp=ts0, part="session", seq=0,
                    capture_status="partial" if sanitized else "complete",
                    facts={"title": info.get("title"), "agent": info.get("agent"),
                           "model": info.get("model"), "kilo_version": info.get("version"),
                           "directory": info.get("directory"), "coordinator": md,
                           "permission": info.get("permission"),
                           "children": session.get("children"),
                           "sanitization": "kilo-sanitized" if sanitized else "none",
                           "missing": ["system_prompt", "tool_definitions"]},
                    requested_persona=(info.get("model") or {}).get("id"), **common))
    if info.get("parentID"):
        ev.append(Event(ADAPTER, sid, "child_session", timestamp=ts0, part="parent", seq=0,
                        facts={"parent": info["parentID"], "observed": "Kilo session parentID"},
                        **common))
    seq = 0
    for m in session.get("messages") or []:
        mi = m.get("info") or {}
        role = mi.get("role")
        mts = ((mi.get("time") or {}).get("created") or 0) / 1000.0
        step = 0
        if mi.get("summary") is True or mi.get("mode") == "compaction":
            seq += 1
            ev.append(Event(ADAPTER, sid, "compaction", timestamp=mts, part=f"{mi.get('id')}:c",
                            seq=seq, facts={"message_id": mi.get("id")}, **common))
        for p in m.get("parts") or []:
            pt = p.get("type")
            pid = p.get("id") or f"{mi.get('id')}:{seq}"
            seq += 1
            base = dict(timestamp=mts, part=pid, seq=seq, **common)
            if pt == "step-start":
                step += 1
                continue
            if pt == "step-finish":
                ev.append(Event(ADAPTER, sid, "assistant_message", actor_role="assistant",
                                actor_model=mi.get("modelID"),
                                requested_persona=mi.get("modelID"),
                                facts={"step_finish": True, "step": step,
                                       "reason": p.get("reason"), "tokens": p.get("tokens"),
                                       "message_id": mi.get("id")}, **base))
                continue
            if pt == "text":
                if role == "user":
                    ev.append(Event(ADAPTER, sid, "user_message", actor_role="user",
                                    facts={"text": p.get("text") or "", "message_id": mi.get("id"),
                                           "synthetic": bool(p.get("synthetic"))}, **base))
                else:
                    ev.append(Event(ADAPTER, sid, "assistant_message", actor_role="assistant",
                                    actor_model=mi.get("modelID"),
                                    requested_persona=mi.get("modelID"),
                                    facts={"text": p.get("text") or "", "step": step,
                                           "message_id": mi.get("id"),
                                           "finish": mi.get("finish"),
                                           "error": mi.get("error")}, **base))
            elif pt == "reasoning":
                ev.append(Event(ADAPTER, sid, "reasoning", actor_role="assistant",
                                actor_model=mi.get("modelID"),
                                facts={"text": p.get("text") or "", "step": step,
                                       "message_id": mi.get("id")}, **base))
            elif pt == "tool":
                st = p.get("state") or {}
                cid = p.get("callID") or pid
                ev.append(Event(ADAPTER, sid, "tool_call", actor_role="assistant",
                                actor_model=mi.get("modelID"), tool_call_id=cid,
                                facts={"name": p.get("tool"), "arguments": st.get("input") or {},
                                       "step": step, "message_id": mi.get("id")},
                                **{**base, "part": pid + ":call"}))
                status = st.get("status")
                meta = st.get("metadata") or {}
                cap = ("missing" if status in ("pending", "running", None) else
                       "truncated" if meta.get("truncated") else "complete")
                ev.append(Event(ADAPTER, sid, "tool_result", actor_role="tool", tool_call_id=cid,
                                capture_status=cap,
                                facts={"name": p.get("tool"), "status": status,
                                       "output": st.get("output") if status == "completed" else None,
                                       "error": st.get("error") if status == "error" else None,
                                       "truncated": bool(meta.get("truncated")),
                                       "exit": meta.get("exit"), "title": st.get("title")},
                                **{**base, "part": pid + ":result",
                                   "timestamp": ((st.get("time") or {}).get("end") or 0) / 1000.0
                                   or mts}))
            elif pt == "patch":
                ev.append(Event(ADAPTER, sid, "file_edit", actor_role="assistant",
                                facts={"files": p.get("files") or [], "hash": p.get("hash")},
                                **base))
            elif pt == "file":
                url = p.get("url") or ""
                refs = []
                status = "missing"
                if store is not None:
                    from ..artifacts import resolve
                    r = resolve(store, project_id, pid, url or p.get("filename") or "",
                                info.get("directory"), artifact_roots or [],
                                {"adapter": ADAPTER, "session": sid, "part": pid,
                                 "mime": p.get("mime")})
                    status = r["status"]
                    if r.get("sha256"):
                        refs = [{"sha256": r["sha256"], "mime": r.get("mime"),
                                 "width": r.get("width"), "height": r.get("height")}]
                ev.append(Event(ADAPTER, sid, "file_ref", actor_role=role,
                                artifact_references=refs,
                                capture_status="complete" if status == "present" else "missing",
                                facts={"mime": p.get("mime"), "filename": p.get("filename"),
                                       "artifact_status": status,
                                       "is_data_url": url.startswith("data:")}, **base))
            elif pt in ("subtask", "agent"):
                ev.append(Event(ADAPTER, sid, "child_session", actor_role="assistant",
                                facts={"agent": p.get("agent"), "description": p.get("description"),
                                       "observed": f"Kilo {pt} part"}, **base))
    return ev


def session_records(sessions: list[dict], projects: list,
                    adapter_mode: str) -> Iterator[tuple[str, str, dict, dict]]:
    for s in sessions:
        info = s.get("info") or {}
        pid, how = project_of(s, projects)
        yield (info.get("id") or "?", pid, s,
               {"adapter": ADAPTER, "mode": adapter_mode, "project_by": how,
                "sanitization": "kilo-sanitized" if s.get("_sanitized") else "none",
                "kilo_version": info.get("version")})


def cleanup_snapshot(path: str) -> None:
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)

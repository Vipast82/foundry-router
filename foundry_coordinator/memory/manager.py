"""MemoryManager: the coordinator's handle on project memory (Team Lead piece 4).

  c.memory.enabled(P)                 [automation] project_memory and docs/memory exists
  c.memory.tick(P)                    re-index changed files (cheap: mtime/size)
  c.memory.slice(P, spec, track=, budget_tokens=)   the entries for a sprint (dict)
  c.memory.prompt_block(P, tid, sid, spec=, track=) the rendered slice for a prompt, recorded
                                       per session; engine._memory_delta hashes it per entry
  c.memory.apply_notes(P, t, state)   MEMORY_NOTE lines of a session's report -> validated,
                                       appended, committed, indexed
  c.memory.add / edit(P, file, heading, text, actor)   the chat's typed actions
  c.memory.snapshot(P)                what the router shows (files, entries, slices, notes)

Every commit is the coordinator's own, by explicit path, with the task prefix
(or MEMORY: when no task owns it), journaled as a coordinator_commit event. The
AU repo's commit-msg hook refuses a "<ID>:" commit whose paths are outside the
sprint's file list; its documented override AU_ALLOW_SCOPE is set to exactly the
memory paths committed (COMMIT_ENV), and the commit body says so.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Optional

from ..adapters.git import GitError
from . import layout, slices
from .index import MemoryIndex, tokens
from .notes import MAX_NOTES_PER_REPORT, find_secret, parse_notes, validate
from .notes import append_to_section, replace_section

COMMIT_ENV = {"AU_ALLOW_SCOPE": "{paths}"}
SNAPSHOT_ENTRY_CHARS = 1200
SNAPSHOT_MAX_ENTRIES = 3000
NO_TASK = "MEMORY"


def _h(*parts: str) -> str:
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


class MemoryManager:
    def __init__(self, coordinator):
        self.c = coordinator
        self.store = coordinator.store
        self.clock = getattr(coordinator, "clock", None) or time.time
        self.log = getattr(coordinator, "log", None) or (lambda m: None)
        self.index = MemoryIndex(self.store)
        self._published: dict[str, str] = {}

    # ------------------------------------------------------------------ settings

    @staticmethod
    def settings(P) -> dict:
        from ..automation import settings
        s = settings(P)
        return {"enabled": bool(s.get("project_memory", True)),
                "budget": int(s.get("memory_slice_tokens", slices.DEFAULT_BUDGET) or 0),
                "decisions": int(s.get("memory_decisions", slices.DEFAULT_DECISIONS) or 0),
                "note_max": int(s.get("memory_note_max_chars", 1500) or 1500)}

    @staticmethod
    def workspace(P, workspace: str = "") -> str:
        return workspace or P.profile.workspace

    def enabled(self, P, workspace: str = "") -> bool:
        return self.settings(P)["enabled"] and layout.exists(self.workspace(P, workspace))

    # ------------------------------------------------------------------ layout

    def init(self, P, actor: str = "operator") -> dict:
        """`memory init`: create the layout and commit the new files."""
        out = layout.init(P.profile.workspace, P.profile.name, clock=self.clock)
        if out["created"]:
            sha, err = self.commit(P, out["created"], NO_TASK, "create the docs/memory layout",
                                   actor=actor)
            out["sha"], out["error"] = sha, err
        else:
            out["sha"], out["error"] = "", ""
        self.reindex(P, force=True)
        return out

    def import_state(self, P, actor: str = "operator", force: bool = False) -> dict:
        from .importer import import_state
        return import_state(self, P, actor, force)

    # ------------------------------------------------------------------ index

    def tick(self, P) -> None:
        if not self.enabled(P):
            return
        try:
            res = self.index.reindex(P.id, P.profile.workspace)
        except Exception as e:                                    # noqa: BLE001
            self.log(f"{P.id}: memory reindex failed: {type(e).__name__}: {e}")
            return
        if res["changed"] or res["removed"]:
            self.store.event(P.id, "", "memory_indexed", detail={
                "changed": res["changed"][:40], "removed": res["removed"][:40],
                "entries": res["entries"]})

    def reindex(self, P, force: bool = False) -> dict:
        return self.index.reindex(P.id, P.profile.workspace, force=force)

    def entries(self, P, file: Optional[str] = None) -> list[dict]:
        return self.index.entries(P.id, file)

    def search(self, P, query: str, limit: int = 20) -> list[dict]:
        return self.index.search(P.id, query, limit)

    def files(self, P) -> list[dict]:
        return self.index.files(P.id)

    # ------------------------------------------------------------------ slices

    def slice(self, P, spec, track: str = "", budget_tokens: Optional[int] = None,
              role: str = "implementer", needs=()) -> dict:
        """The slice for one sprint (tl-tracks: pass the track's name; a track
        session reads the same index). {"entries", "text", "tokens", "budget",
        "hash", "dropped", "query"}; empty when memory is off or empty."""
        st = self.settings(P)
        if not st["enabled"]:
            return {"entries": [], "text": "", "tokens": 0, "budget": 0, "hash": "", "dropped": 0,
                    "query": []}
        budget = int(budget_tokens or st["budget"] or slices.DEFAULT_BUDGET)
        if role in ("reviewer", "lead"):
            budget = max(300, budget // 2)
        sel = slices.select(self.entries(P), spec, track or "", budget, st["decisions"], needs)
        title = f"{getattr(spec, 'task_id', '')}" + (f", track {track}" if track else "") \
            + (f", {role}" if role != "implementer" else "")
        text = slices.render(sel["entries"], title.strip(", "))
        return {**sel, "text": text, "hash": slices.slice_hash(sel["entries"]),
                "tokens": max(sel["tokens"], tokens(text)) if text else 0}

    def prompt_block(self, P, task_id: str, session_id: str, spec=None, track: Optional[str] = None,
                     role: str = "implementer") -> str:
        """The rendered slice for a prompt to `session_id`, recorded in
        memory_slices (what the GUI shows per session). The caller passes it
        through engine._memory_delta so only changed entries are re-sent."""
        if not self.enabled(P):
            return ""
        if spec is None:
            try:
                spec = P.adapter.describe(task_id)
            except Exception as e:                                # noqa: BLE001
                self.log(f"{P.id}/{task_id}: memory slice without a spec: {e}")
                return ""
        if track is None:
            t = self.store.task(P.id, task_id) or {}
            track = str((t.get("data") or {}).get("track") or "")
        sl = self.slice(P, spec, track, role=role)
        if session_id:
            self._record_slice(P, task_id, session_id, role, track, sl)
        return sl["text"]

    def _record_slice(self, P, task_id: str, session_id: str, role: str, track: str,
                      sl: dict) -> None:
        prev = self.store.q("SELECT prompts FROM memory_slices WHERE session_id=?", (session_id,))
        n = int(prev[0]["prompts"] or 0) + 1 if prev else 1
        self.store.x("INSERT OR REPLACE INTO memory_slices (session_id, project_id, task_id, role, "
                     "track, ts, prompts, tokens, entries, budget, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (session_id, P.id, task_id, role, track or "", self.clock(), n, sl["tokens"],
                      json.dumps([{"file": e["file"], "heading": e["heading"], "hash": e["hash"],
                                   "why": e.get("why", "")} for e in sl["entries"]]),
                      sl["budget"], sl["hash"]))

    def slices(self, P, limit: int = 50) -> list[dict]:
        rows = self.store.q("SELECT m.*, s.status, s.ended_ts, s.phase FROM memory_slices m LEFT JOIN "
                            "sessions s ON s.session_id=m.session_id WHERE m.project_id=? ORDER BY "
                            "m.ts DESC LIMIT ?", (P.id, limit))
        out = []
        for r in rows:
            try:
                ents = json.loads(r["entries"] or "[]")
            except ValueError:
                ents = []
            out.append({"session_id": r["session_id"], "task_id": r["task_id"], "role": r["role"],
                        "track": r["track"], "ts": r["ts"], "prompts": r["prompts"],
                        "tokens": r["tokens"], "budget": r["budget"], "hash": r["hash"],
                        "entries": ents, "status": r.get("status"), "phase": r.get("phase"),
                        "live": bool(r.get("status")) and not r.get("ended_ts")})
        return out

    # ------------------------------------------------------------------ notes

    def apply_notes(self, P, t: dict, state, workspace: str = "") -> list[dict]:
        """MEMORY_NOTE lines in the session's final message: validated, appended
        to their files, committed together by explicit path with the task prefix,
        indexed. Each note is applied once per session (hash). Returns the per-note
        results ({file, heading, status, detail, sha})."""
        text = getattr(state, "final_text", "") or ""
        if "MEMORY_NOTE" not in text:
            return []
        notes = parse_notes(text)
        if not notes:
            return []
        tid, sid = t["task_id"], t.get("session_id") or ""
        st = self.settings(P)
        ws = self.workspace(P, workspace)
        pending, results = [], []
        for n in notes[:MAX_NOTES_PER_REPORT]:
            h = _h(n.get("file", ""), n.get("heading", ""), n.get("text", ""))
            if self.store.q("SELECT 1 FROM memory_notes WHERE session_id=? AND hash=?", (sid, h)):
                continue
            why = "" if st["enabled"] else "project memory is off ([automation] project_memory)"
            why = why or validate(n, st["note_max"])
            if why:
                self._note_row(P, tid, sid, f"session {sid}", n, h, "rejected", why, "")
                self.store.event(P.id, tid, "memory_note_rejected", session_id=sid, detail={
                    "file": str(n.get("file") or "")[:80], "heading": str(n.get("heading") or "")[:120],
                    "reason": why[:300]})
                results.append({"file": n.get("file"), "heading": n.get("heading"),
                                "status": "rejected", "detail": why})
                continue
            pending.append((n, h))
        for n in notes[MAX_NOTES_PER_REPORT:]:
            results.append({"file": n.get("file"), "heading": n.get("heading"), "status": "rejected",
                            "detail": f"over {MAX_NOTES_PER_REPORT} notes in one report"})
        if not pending:
            return results
        items = [(layout.file_ok(n["file"]), n["heading"].strip(), n["text"].strip(), "append")
                 for n, _ in pending]
        sha, err, paths = self._apply(P, items, tid, ws, actor=f"session {sid}",
                                      text=f"memory notes from session {sid[:12]}: "
                                           + ", ".join(sorted({p for p, *_ in items})))
        status = "committed" if sha else ("failed" if err else "unchanged")
        for n, h in pending:
            self._note_row(P, tid, sid, f"session {sid}", n, h, status, err, sha)
            results.append({"file": layout.file_ok(n["file"]), "heading": n["heading"],
                            "status": status, "detail": err, "sha": sha})
        self.store.event(P.id, tid, "memory_notes_committed" if sha else "memory_notes_failed",
                         session_id=sid, detail={"sha": sha, "count": len(pending),
                                                 "paths": paths[:20], "error": err[:300]})
        return results

    def _note_row(self, P, tid, sid, actor, n, h, status, detail, sha) -> None:
        self.store.x("INSERT OR REPLACE INTO memory_notes (note_id, project_id, task_id, session_id, "
                     "actor, file, heading, text, hash, status, detail, sha, ts) VALUES "
                     "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"mn-{uuid.uuid4().hex[:12]}", P.id, tid, sid, actor,
                      str(n.get("file") or "")[:120], str(n.get("heading") or "")[:200],
                      str(n.get("text") or "")[:4000], h, status, (detail or "")[:500], sha,
                      self.clock()))

    def notes(self, P, task_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        if task_id:
            return self.store.q("SELECT * FROM memory_notes WHERE project_id=? AND task_id=? ORDER BY "
                                "ts DESC LIMIT ?", (P.id, task_id, limit))
        return self.store.q("SELECT * FROM memory_notes WHERE project_id=? ORDER BY ts DESC LIMIT ?",
                            (P.id, limit))

    # ------------------------------------------------------------------ chat edits

    def add(self, P, file: str, heading: str, text: str, actor: str, task_id: str = "",
            workspace: str = "") -> dict:
        return self._edit(P, file, heading, text, actor, task_id, "append", workspace)

    def edit(self, P, file: str, heading: str, text: str, actor: str, task_id: str = "",
             workspace: str = "") -> dict:
        return self._edit(P, file, heading, text, actor, task_id, "replace", workspace)

    def _edit(self, P, file, heading, text, actor, task_id, mode, workspace) -> dict:
        st = self.settings(P)
        if not st["enabled"]:
            return {"ok": False, "error": "project memory is off ([automation] project_memory)"}
        why = validate({"file": file, "heading": heading, "text": text},
                       max(st["note_max"], 4000))
        if why:
            return {"ok": False, "error": why}
        ws = self.workspace(P, workspace)
        rel = layout.file_ok(file)
        if mode == "replace" and not os.path.isfile(os.path.join(layout.memory_dir(ws),
                                                                 *rel.split("/"))):
            return {"ok": False, "error": f"{rel} does not exist; use memory_add to create it"}
        verb = "add to" if mode == "append" else "edit"
        sha, err, paths = self._apply(P, [(rel, heading.strip(), text.strip(), mode)],
                                      task_id or NO_TASK, ws, actor=actor,
                                      text=f"memory {verb} {rel} > {heading.strip()[:60]} ({actor})")
        if err:
            return {"ok": False, "error": err, "path": layout.rel_path(rel)}
        self._note_row(P, task_id or "", "", actor, {"file": rel, "heading": heading, "text": text},
                       _h(rel, heading, text), "committed" if sha else "unchanged", "", sha)
        self.store.event(P.id, task_id or "", "memory_edited", detail={
            "mode": mode, "file": rel, "heading": heading[:120], "sha": sha, "actor": actor[:80]})
        return {"ok": True, "sha": sha, "path": layout.rel_path(rel), "mode": mode,
                "unchanged": not sha}

    # ------------------------------------------------------------------ writing + commits

    def _apply(self, P, items: list, task_id: str, ws: str, actor: str,
               text: str) -> tuple[str, str, list]:
        """Apply (file, heading, body, mode) edits to the memory files of `ws`,
        write them, commit them together. (sha, error, repo-relative paths)."""
        root = layout.memory_dir(ws)
        # a missing layout is created here and committed with the edit (never left untracked)
        created = layout.init(ws, P.profile.name)["created"] if not layout.exists(ws) else []
        texts: dict[str, str] = {}
        for rel, heading, body, mode in items:
            path = os.path.join(root, *rel.split("/"))
            if rel not in texts:
                if os.path.isfile(path):
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        texts[rel] = fh.read()
                else:
                    texts[rel] = layout.new_file_text(rel, P.profile.name)
            if mode == "replace":
                new = replace_section(texts[rel], heading, body)
                if new is None:
                    return "", f"{rel} has no heading {heading!r}", []
                texts[rel] = new
            else:
                texts[rel] = append_to_section(texts[rel], heading, body)
        paths = []
        for rel, content in texts.items():
            path = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
            paths.append(layout.rel_path(rel))
        paths = sorted(set(paths) | set(created))
        sha, err = self.commit(P, paths, task_id, text, actor=actor, workspace=ws)
        if ws == P.profile.workspace:
            try:
                self.index.reindex(P.id, ws)
            except Exception as e:                                # noqa: BLE001
                self.log(f"{P.id}: memory reindex after commit failed: {e}")
        return sha, err, paths

    def commit(self, P, paths: list, task_id: str, text: str, actor: str = "",
               workspace: str = "") -> tuple[str, str]:
        """Commit exactly `paths` as the coordinator. (sha, "") on a commit,
        ("", "") when nothing changed, ("", error) on failure. A task's commit is
        recorded in its coord_commits like every other bookkeeping commit."""
        from .. import bookkeeping
        msg = bookkeeping.message(P, task_id, text)
        msg += ("\n\nCoordinator memory commit (docs/memory). Scope override AU_ALLOW_SCOPE set to "
                "exactly these paths by the coordinator" + (f" for {actor}" if actor else "") + ".")
        env = {k: v.replace("{paths}", ";".join(paths)) for k, v in COMMIT_ENV.items()}
        ws_obj = P.ws
        if workspace and os.path.realpath(workspace) != os.path.realpath(P.profile.workspace):
            from ..adapters.git import Workspace
            ws_obj = Workspace(workspace)
        try:
            sha = ws_obj.commit_paths(sorted(paths), msg, env=env)
        except (GitError, OSError) as e:
            self.store.event(P.id, task_id if task_id != NO_TASK else "", "coordinator_commit_failed",
                             detail={"kind": "memory", "paths": sorted(paths)[:40],
                                     "error": str(e)[:400]})
            return "", str(e)[:400]
        if not sha:
            return "", ""
        if task_id and task_id != NO_TASK and self.store.task(P.id, task_id):
            cur = self.store.task(P.id, task_id)
            kept = list((cur.get("data") or {}).get(bookkeeping.KEY) or []) + [sha]
            self.store.merge_data(P.id, task_id, **{bookkeeping.KEY: kept[-bookkeeping.MAX_KEPT:]})
        self.store.event(P.id, task_id if task_id != NO_TASK else "", "coordinator_commit", detail={
            "kind": "memory", "sha": sha, "message": msg.split("\n", 1)[0], "paths": sorted(paths)[:40],
            "count": len(paths)})
        return sha, ""

    # ------------------------------------------------------------------ the router's view

    def snapshot(self, P) -> dict:
        """What the router stores as the project's `memory` snapshot: the files,
        every entry (text capped), the per-session slices and the recent notes."""
        ents = self.entries(P)[:SNAPSHOT_MAX_ENTRIES]
        return {"type": "memory", "project_id": P.id, "scope": "workspace",
                "observed_ts": self.clock(), "dir": layout.MEMORY_DIR,
                "enabled": self.enabled(P), "fingerprint": self.index.fingerprint(P.id),
                "files": self.files(P),
                "entries": [{"entry_id": e["entry_id"], "file": e["file"], "heading": e["heading"],
                             "ord": e["ord"], "text": e["text"][:SNAPSHOT_ENTRY_CHARS],
                             "tags": e["tags"][:24], "hash": e["hash"]} for e in ents],
                "slices": self.slices(P, 40),
                "notes": [{k: r.get(k) for k in ("task_id", "session_id", "actor", "file", "heading",
                                                 "status", "detail", "sha", "ts")}
                          for r in self.notes(P, limit=30)]}

    def publish(self, foundry, source_id: str) -> int:
        """Send changed memory snapshots to the router as `memory` observations
        (dedupes on fingerprint + slices). Returns the number sent."""
        if foundry is None:
            return 0
        sent = 0
        for pid in sorted(self.c.projects):
            P = self.c.projects[pid]
            if not self.enabled(P):
                continue
            snap = self.snapshot(P)
            key = _h(snap["fingerprint"], json.dumps([(s["session_id"], s["hash"], s["prompts"])
                                                     for s in snap["slices"]]),
                     json.dumps([(n["sha"], n["status"]) for n in snap["notes"]]))
            if self._published.get(pid) == key:
                continue
            from ..telemetry import CONTRACT
            try:
                foundry.telemetry({"contract": CONTRACT, "batch_id": f"mem-{pid}-{key}",
                                   "source": {"id": source_id, "kind": "coordinator"},
                                   "sent_ts": self.clock(), "observations": [snap]})
            except Exception as e:                                # noqa: BLE001
                self.log(f"{pid}: memory snapshot not sent: {type(e).__name__}: {e}")
                continue
            self._published[pid] = key
            sent += 1
        return sent

    def status(self, P) -> dict:
        return {"enabled": self.enabled(P), "dir": layout.rel_path("").rstrip("/"),
                "files": self.files(P), "entries": len(self.entries(P)),
                "fingerprint": self.index.fingerprint(P.id), "fts": self.index.fts,
                "settings": self.settings(P)}


def secret_in(text: str) -> str:
    return find_secret(text)

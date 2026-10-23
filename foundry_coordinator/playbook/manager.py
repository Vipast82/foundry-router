"""PlaybookManager: the coordinator's handle on the cross-project playbook (A1).

  c.playbook.configured()              a playbook path is set and the layout exists
  c.playbook.enabled(P)                configured and [automation] playbook for P
  c.playbook.tick()                    re-index changed files (cheap: mtime/size)
  c.playbook.slice(P, spec, ...)       the entries for one prompt (shares the memory budget)
  c.playbook.prompt_block(P, tid, sid, used_text=...)  rendered + recorded per session
  c.playbook.apply_notes(P, t, state)  PLAYBOOK_NOTE lines -> validated, committed
  c.playbook.add / edit(P, ...)        the chat's / control center's typed actions
  c.playbook.snapshot() / publish()    one cross-project snapshot (project @playbook)

Everything reuses the memory machinery: the index tables keyed by the reserved
id @playbook, the note grammar/validator, the slice scoring and the paragraph
render that engine._memory_delta hashes per entry. Commits go to the playbook's
own repository by explicit path; a session contributes only through notes.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Iterable, Optional

from ..adapters.git import GitError, Workspace
from ..memory import slices
from ..memory.index import MemoryIndex, tokens
from ..memory.notes import (MAX_NOTES_PER_REPORT, append_to_section, parse_notes,
                            replace_section, validate)
from . import layout
from .layout import NOTE_KEYWORD, PLAYBOOK_ID

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS playbook_slices (
      session_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, role TEXT, track TEXT,
      ts REAL, prompts INTEGER, tokens INTEGER, entries TEXT, budget INTEGER, hash TEXT)""",
    """CREATE TABLE IF NOT EXISTS playbook_notes (
      note_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, session_id TEXT, actor TEXT,
      file TEXT, heading TEXT, text TEXT, hash TEXT, status TEXT, detail TEXT, sha TEXT,
      ts REAL)""",
    "CREATE INDEX IF NOT EXISTS idx_playbook_notes_ts ON playbook_notes(ts)",
    """CREATE TABLE IF NOT EXISTS playbook_candidates (
      key TEXT PRIMARY KEY, kind TEXT, count INTEGER, threshold INTEGER, projects TEXT,
      project_id TEXT, task_id TEXT, first_ts REAL, last_ts REAL, evidence TEXT,
      draft_file TEXT, draft_heading TEXT, draft_text TEXT, decision_id TEXT,
      status TEXT, updated_ts REAL)""",
]
# base weight by directory: a fixes entry only needs a weak match to be worth
# sending; templates and formats come in on a real match
BASE_WEIGHT = {"fixes": 1.0, "howto": 0.5, "templates": 0.3, "formats": 0.3, "scripts": 0.3}
SIGNATURE_BOOST = 6.0          # a fixes entry tagged with the stop's failure signature
MIN_BUDGET = 400               # the playbook's floor when memory used most of the budget
SNAPSHOT_ENTRY_CHARS = 1200
SNAPSHOT_MAX_ENTRIES = 3000


def _h(*parts: str) -> str:
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


class PlaybookManager:
    def __init__(self, coordinator, path: str = ""):
        self.c = coordinator
        self.store = coordinator.store
        self.clock = getattr(coordinator, "clock", None) or time.time
        self.log = getattr(coordinator, "log", None) or (lambda m: None)
        self.path = os.path.abspath(path or os.environ.get("FOUNDRY_PLAYBOOK") or "") \
            if (path or os.environ.get("FOUNDRY_PLAYBOOK")) else ""
        self.index = MemoryIndex(self.store)
        for sql in SCHEMA:
            self.store.conn.execute(sql)
        self._published = ""
        from .promote import Promotion
        self.promote = Promotion(self)

    # ------------------------------------------------------------------ settings

    @staticmethod
    def settings(P) -> dict:
        from ..automation import settings
        s = settings(P)
        return {"enabled": bool(s.get("playbook", True)),
                "promote": bool(s.get("playbook_promote", False)),
                "note_max": int(s.get("memory_note_max_chars", 1500) or 1500)}

    def configured(self) -> bool:
        return bool(self.path) and layout.exists(self.path)

    def enabled(self, P) -> bool:
        return self.configured() and self.settings(P)["enabled"]

    def promote_enabled(self, P) -> bool:
        return self.configured() and self.settings(P)["promote"]

    # ------------------------------------------------------------------ layout

    def init(self, actor: str = "operator") -> dict:
        """`playbook init`: create the repo (git init when needed), the layout,
        and commit the new files."""
        if not self.path:
            return {"created": [], "existing": [], "sha": "",
                    "error": "no playbook path configured (--playbook or $FOUNDRY_PLAYBOOK)"}
        out = layout.init(self.path)
        ws = Workspace(self.path)
        try:
            ws.run("rev-parse", "--git-dir")
        except GitError:
            ws.run("init", "-q")
        fresh = False
        try:
            ws.head()
        except GitError:
            fresh = True                    # no commits yet: commit_paths can't diff
        out["error"] = ""
        out["sha"] = ""
        if out["created"] or fresh:
            try:
                if fresh:
                    ws.run("add", "-A")
                    ws.run("commit", "-q", "-m", self._message(
                        "create the playbook layout", actor))
                    out["sha"] = ws.head()
                else:
                    out["sha"] = ws.commit_paths(out["created"], self._message(
                        "create the playbook layout", actor))
            except (GitError, OSError) as e:
                out["error"] = str(e)[:400]
        self.reindex(force=True)
        return out

    # ------------------------------------------------------------------ index

    def tick(self) -> None:
        if not self.configured():
            return
        try:
            res = self.reindex()
        except Exception as e:                                    # noqa: BLE001
            self.log(f"playbook reindex failed: {type(e).__name__}: {e}")
            return
        if res["changed"] or res["removed"]:
            self._event("", "", "playbook_indexed", {
                "changed": res["changed"][:40], "removed": res["removed"][:40],
                "entries": res["entries"]})

    def reindex(self, force: bool = False) -> dict:
        return self.index.reindex(PLAYBOOK_ID, self.path, force=force,
                                  files=layout.list_files(self.path), root=self.path)

    def entries(self, file: Optional[str] = None) -> list[dict]:
        return self.index.entries(PLAYBOOK_ID, file)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        return self.index.search(PLAYBOOK_ID, query, limit)

    def files(self) -> list[dict]:
        return self.index.files(PLAYBOOK_ID)

    # ------------------------------------------------------------------ slices

    @staticmethod
    def _file_base(file: str) -> float:
        return BASE_WEIGHT.get(file.partition("/")[0].lower(), 0.0)

    def task_signature(self, P, task_id: str) -> str:
        """The failure signature of the task's current/last stop, for the slice
        boost: the auto-heal signature when one is recorded, else the infra
        signature of the blocker text. Deterministic, no model."""
        t = self.store.task(P.id, task_id) or {}
        data = t.get("data") or {}
        sig = str((data.get("auto_heal") or {}).get("signature") or "")
        if sig:
            return sig
        from ..autoheal import infra_signature
        return infra_signature(str(t.get("blocker") or ""))

    def slice(self, P, spec, track: str = "", role: str = "implementer", needs: Iterable[str] = (),
              budget_tokens: Optional[int] = None, used_tokens: int = 0,
              signature: str = "") -> dict:
        """The playbook entries for one prompt. The budget is what the memory
        slice left of the shared [automation] memory_slice_tokens budget, with a
        MIN_BUDGET floor so an exact fixes/ match is never starved."""
        if not self.enabled(P):
            return {"entries": [], "text": "", "tokens": 0, "budget": 0, "hash": "", "dropped": 0,
                    "query": []}
        from ..memory.manager import MemoryManager
        mst = MemoryManager.settings(P)
        budget = int(budget_tokens or mst["budget"] or slices.DEFAULT_BUDGET)
        if role in ("reviewer", "lead"):
            budget = max(300, budget // 2)
        budget = max(MIN_BUDGET, budget - max(0, int(used_tokens)))
        q = slices.query_tags(spec, track, needs)
        if role:
            q[role.lower()] = max(q.get(role.lower(), 0), 1.0)
        sig = (signature or "").strip().lower()
        scope = [str(g).lower() for g in (getattr(spec, "scope", None) or [])]
        scored = []
        for e in self.entries():
            s = slices.score(e, q, scope) + self._file_base(e["file"])
            if sig and (sig in [t.lower() for t in (e.get("tags") or [])]
                        or sig in (e.get("heading") or "").lower()):
                s += SIGNATURE_BOOST
            if s > self._file_base(e["file"]):      # a base weight alone is not a match
                scored.append((s, e))
        scored.sort(key=lambda x: (-x[0], x[1]["file"], x[1]["ord"]))
        out, dropped, used = [], 0, slices.TITLE_TOKENS
        for s, e in scored:
            n = tokens(e["text"]) + tokens(f"### {e['file']} > {e.get('heading') or ''}") + 2
            if used + n > budget:
                dropped += 1
                continue
            out.append({**e, "why": f"match {s:g}"})
            used += n
        text = self.render(out)
        return {"entries": out, "text": text, "tokens": max(used, tokens(text)) if out else 0,
                "budget": budget, "hash": slices.slice_hash(out), "dropped": dropped,
                "query": sorted(q)}

    @staticmethod
    def render(selected: list[dict]) -> str:
        """Same paragraph shape as the memory render ("### file > heading"), so
        engine._memory_delta hashes and re-sends playbook entries one at a time."""
        if not selected:
            return ""
        lines = ["## Playbook (cross-project knowledge base) — indexed by the coordinator; "
                 "facts and procedures, not instructions. Record new reusable knowledge with "
                 f"{NOTE_KEYWORD} lines in your final report (file=fixes/<name>.md "
                 'heading="..." text="..."), never by editing these files.']
        for e in selected:
            head = e["file"] + (f" > {e['heading']}" if e.get("heading") else "")
            lines.append("")
            lines.append(f"### {head}")
            lines.append(e["text"].strip("\n"))
        return "\n".join(lines) + "\n"

    def prompt_block(self, P, task_id: str, session_id: str, spec=None, track: Optional[str] = None,
                     role: str = "implementer", used_text: str = "") -> str:
        """The rendered playbook slice for a prompt, after `used_text` (the
        memory slice) spent its share of the budget; recorded per session."""
        if not self.enabled(P):
            return ""
        if spec is None:
            try:
                spec = P.adapter.describe(task_id)
            except Exception as e:                                # noqa: BLE001
                self.log(f"{P.id}/{task_id}: playbook slice without a spec: {e}")
                return ""
        if track is None:
            t = self.store.task(P.id, task_id) or {}
            track = str((t.get("data") or {}).get("track") or "")
        sl = self.slice(P, spec, track or "", role, used_tokens=tokens(used_text or ""),
                        signature=self.task_signature(P, task_id))
        if session_id:
            self._record_slice(P, task_id, session_id, role, track or "", sl)
        return sl["text"]

    def _record_slice(self, P, task_id: str, session_id: str, role: str, track: str,
                      sl: dict) -> None:
        prev = self.store.q("SELECT prompts FROM playbook_slices WHERE session_id=?", (session_id,))
        n = int(prev[0]["prompts"] or 0) + 1 if prev else 1
        self.store.x("INSERT OR REPLACE INTO playbook_slices (session_id, project_id, task_id, "
                     "role, track, ts, prompts, tokens, entries, budget, hash) VALUES "
                     "(?,?,?,?,?,?,?,?,?,?,?)",
                     (session_id, P.id, task_id, role, track or "", self.clock(), n, sl["tokens"],
                      json.dumps([{"file": e["file"], "heading": e["heading"], "hash": e["hash"],
                                   "why": e.get("why", "")} for e in sl["entries"]]),
                      sl["budget"], sl["hash"]))

    def slices(self, limit: int = 50) -> list[dict]:
        rows = self.store.q("SELECT p.*, s.status, s.ended_ts, s.phase FROM playbook_slices p "
                            "LEFT JOIN sessions s ON s.session_id=p.session_id "
                            "ORDER BY p.ts DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            try:
                ents = json.loads(r["entries"] or "[]")
            except ValueError:
                ents = []
            out.append({"session_id": r["session_id"], "project_id": r["project_id"],
                        "task_id": r["task_id"], "role": r["role"], "track": r["track"],
                        "ts": r["ts"], "prompts": r["prompts"], "tokens": r["tokens"],
                        "budget": r["budget"], "hash": r["hash"], "entries": ents,
                        "status": r.get("status"), "phase": r.get("phase"),
                        "live": bool(r.get("status")) and not r.get("ended_ts")})
        return out

    # ------------------------------------------------------------------ notes

    def apply_notes(self, P, t: dict, state) -> list[dict]:
        """PLAYBOOK_NOTE lines in the session's final message: validated, appended
        to their playbook files, committed together by explicit path to the
        playbook repo. Each note is applied once per session (hash)."""
        text = getattr(state, "final_text", "") or ""
        if NOTE_KEYWORD not in text:
            return []
        notes = parse_notes(text, keyword=NOTE_KEYWORD)
        if not notes:
            return []
        tid, sid = t["task_id"], t.get("session_id") or ""
        st = self.settings(P)
        pending, results = [], []
        for n in notes[:MAX_NOTES_PER_REPORT]:
            h = _h(n.get("file", ""), n.get("heading", ""), n.get("text", ""))
            if self.store.q("SELECT 1 FROM playbook_notes WHERE session_id=? AND hash=?", (sid, h)):
                continue
            why = "" if self.enabled(P) else \
                ("no playbook is configured for this coordinator" if not self.configured()
                 else "the playbook is off for this project ([automation] playbook)")
            why = why or validate(n, st["note_max"], file_ok=layout.file_ok,
                                  files_hint=layout.files_hint())
            if why:
                self._note_row(P, tid, sid, f"session {sid}", n, h, "rejected", why, "")
                self._event(P.id, tid, "playbook_note_rejected", {
                    "file": str(n.get("file") or "")[:80],
                    "heading": str(n.get("heading") or "")[:120], "reason": why[:300]},
                    session_id=sid)
                results.append({"file": n.get("file"), "heading": n.get("heading"),
                                "status": "rejected", "detail": why})
                continue
            pending.append((n, h))
        for n in notes[MAX_NOTES_PER_REPORT:]:
            results.append({"file": n.get("file"), "heading": n.get("heading"),
                            "status": "rejected",
                            "detail": f"over {MAX_NOTES_PER_REPORT} notes in one report"})
        if not pending:
            return results
        items = [(layout.file_ok(n["file"]), n["heading"].strip(), n["text"].strip(), "append")
                 for n, _ in pending]
        sha, err, paths = self._apply(items, f"playbook notes from session {sid[:12]}: "
                                      + ", ".join(sorted({p for p, *_ in items})),
                                      actor=f"session {sid}", project_id=P.id, task_id=tid)
        status = "committed" if sha else ("failed" if err else "unchanged")
        for n, h in pending:
            self._note_row(P, tid, sid, f"session {sid}", n, h, status, err, sha)
            results.append({"file": layout.file_ok(n["file"]), "heading": n["heading"],
                            "status": status, "detail": err, "sha": sha})
        self._event(P.id, tid, "playbook_notes_committed" if sha else "playbook_notes_failed",
                    {"sha": sha, "count": len(pending), "paths": paths[:20], "error": err[:300]},
                    session_id=sid)
        return results

    def _note_row(self, P, tid, sid, actor, n, h, status, detail, sha) -> None:
        self.store.x("INSERT OR REPLACE INTO playbook_notes (note_id, project_id, task_id, "
                     "session_id, actor, file, heading, text, hash, status, detail, sha, ts) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"pn-{uuid.uuid4().hex[:12]}", P.id if P is not None else "", tid, sid, actor,
                      str(n.get("file") or "")[:120], str(n.get("heading") or "")[:200],
                      str(n.get("text") or "")[:4000], h, status, (detail or "")[:500], sha,
                      self.clock()))

    def notes(self, limit: int = 50) -> list[dict]:
        return self.store.q("SELECT * FROM playbook_notes ORDER BY ts DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ chat edits

    def add(self, P, file: str, heading: str, text: str, actor: str, task_id: str = "") -> dict:
        return self._edit(P, file, heading, text, actor, task_id, "append")

    def edit(self, P, file: str, heading: str, text: str, actor: str, task_id: str = "") -> dict:
        return self._edit(P, file, heading, text, actor, task_id, "replace")

    def _edit(self, P, file, heading, text, actor, task_id, mode) -> dict:
        if not self.configured():
            return {"ok": False, "error": "no playbook is configured for this coordinator "
                                          "(--playbook or $FOUNDRY_PLAYBOOK)"}
        st = self.settings(P) if P is not None else {"note_max": 1500}
        why = validate({"file": file, "heading": heading, "text": text},
                       max(st["note_max"], 4000), file_ok=layout.file_ok,
                       files_hint=layout.files_hint())
        if why:
            return {"ok": False, "error": why}
        rel = layout.file_ok(file)
        if mode == "replace" and not os.path.isfile(os.path.join(self.path, *rel.split("/"))):
            return {"ok": False, "error": f"{rel} does not exist; use kb_add to create it"}
        verb = "add to" if mode == "append" else "edit"
        sha, err, _ = self._apply([(rel, heading.strip(), text.strip(), mode)],
                                  f"{verb} {rel} > {heading.strip()[:60]} ({actor})",
                                  actor=actor, project_id=P.id if P is not None else "",
                                  task_id=task_id)
        if err:
            return {"ok": False, "error": err, "path": rel}
        self._note_row(P, task_id or "", "", actor,
                       {"file": rel, "heading": heading, "text": text},
                       _h(rel, heading, text), "committed" if sha else "unchanged", "", sha)
        self._event(P.id if P is not None else "", task_id or "", "playbook_edited", {
            "mode": mode, "file": rel, "heading": heading[:120], "sha": sha, "actor": actor[:80]})
        return {"ok": True, "sha": sha, "path": rel, "mode": mode, "unchanged": not sha}

    # ------------------------------------------------------------------ writing + commits

    def _apply(self, items: list, text: str, actor: str, project_id: str = "",
               task_id: str = "") -> tuple[str, str, list]:
        """Apply (file, heading, body, mode) edits to playbook files, write them,
        commit them together. (sha, error, playbook-relative paths)."""
        created = layout.init(self.path)["created"] if not layout.exists(self.path) else []
        texts: dict[str, str] = {}
        for rel, heading, body, mode in items:
            path = os.path.join(self.path, *rel.split("/"))
            if rel not in texts:
                if os.path.isfile(path):
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        texts[rel] = fh.read()
                else:
                    texts[rel] = layout.new_file_text(rel)
            if mode == "replace":
                new = replace_section(texts[rel], heading, body)
                if new is None:
                    return "", f"{rel} has no heading {heading!r}", []
                texts[rel] = new
            else:
                texts[rel] = append_to_section(texts[rel], heading, body)
        paths = []
        for rel, content in texts.items():
            path = os.path.join(self.path, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
            paths.append(rel)
        paths = sorted(set(paths) | set(created))
        sha, err = self.commit(paths, text, actor=actor, project_id=project_id, task_id=task_id)
        try:
            self.reindex()
        except Exception as e:                                    # noqa: BLE001
            self.log(f"playbook reindex after commit failed: {e}")
        return sha, err, paths

    @staticmethod
    def _message(text: str, actor: str = "", project_id: str = "", task_id: str = "") -> str:
        src = " / ".join(x for x in (project_id, task_id) if x)
        return (f"PLAYBOOK: {text}"[:200]
                + "\n\nCoordinator playbook commit (cross-project knowledge base)"
                + (f" for {actor}" if actor else "") + (f", from {src}" if src else "") + ".")

    def commit(self, paths: list, text: str, actor: str = "", project_id: str = "",
               task_id: str = "") -> tuple[str, str]:
        """Commit exactly `paths` to the playbook repo as the coordinator.
        (sha, "") on a commit, ("", "") when nothing changed, ("", error)."""
        msg = self._message(text, actor, project_id, task_id)
        try:
            sha = Workspace(self.path).commit_paths(sorted(paths), msg)
        except (GitError, OSError) as e:
            self._event(project_id, task_id, "coordinator_commit_failed", {
                "kind": "playbook", "paths": sorted(paths)[:40], "error": str(e)[:400]})
            return "", str(e)[:400]
        if sha:
            self._event(project_id, task_id, "coordinator_commit", {
                "kind": "playbook", "sha": sha, "message": msg.split("\n", 1)[0],
                "paths": sorted(paths)[:40], "count": len(paths)})
        return sha, ""

    def _event(self, project_id: str, task_id: str, kind: str, detail: dict,
               session_id: str = "") -> None:
        """A journal event; the playbook is cross-project, so events land on the
        contributing project when one is known, else on the first project."""
        pid = project_id or (sorted(self.c.projects)[0] if getattr(self.c, "projects", None)
                             else "")
        if not pid:
            return
        try:
            self.store.event(pid, task_id or "", kind, session_id=session_id, detail=detail)
        except Exception as e:                                    # noqa: BLE001
            self.log(f"playbook event {kind} not recorded: {e}")

    # ------------------------------------------------------------------ the router's view

    def snapshot(self) -> dict:
        """The cross-project `memory` observation (project @playbook, scope
        "playbook"): files, entries, per-session slices, notes and the promotion
        candidates."""
        ents = self.entries()[:SNAPSHOT_MAX_ENTRIES]
        return {"type": "memory", "project_id": PLAYBOOK_ID, "scope": "playbook",
                "observed_ts": self.clock(), "dir": "playbook",
                "enabled": self.configured(), "fingerprint": self.index.fingerprint(PLAYBOOK_ID),
                "files": self.files(),
                "entries": [{"entry_id": e["entry_id"], "file": e["file"], "heading": e["heading"],
                             "ord": e["ord"], "text": e["text"][:SNAPSHOT_ENTRY_CHARS],
                             "tags": e["tags"][:24], "hash": e["hash"]} for e in ents],
                "slices": self.slices(40),
                "notes": [{k: r.get(k) for k in ("task_id", "session_id", "actor", "file",
                                                 "heading", "status", "detail", "sha", "ts")}
                          for r in self.notes(limit=30)],
                "candidates": self.promote.view(limit=30)}

    def publish(self, foundry, source_id: str) -> int:
        """Send the playbook snapshot to the router when it changed. 0 or 1."""
        if foundry is None or not self.configured():
            return 0
        snap = self.snapshot()
        key = _h(snap["fingerprint"],
                 json.dumps([(s["session_id"], s["hash"], s["prompts"]) for s in snap["slices"]]),
                 json.dumps([(n["sha"], n["status"]) for n in snap["notes"]]),
                 json.dumps([(cd["key"], cd["status"], cd["count"])
                             for cd in snap["candidates"]]))
        if self._published == key:
            return 0
        from ..telemetry import CONTRACT
        try:
            foundry.telemetry({"contract": CONTRACT, "batch_id": f"pb-{key}",
                               "source": {"id": source_id, "kind": "coordinator"},
                               "sent_ts": self.clock(), "observations": [snap]})
        except Exception as e:                                    # noqa: BLE001
            self.log(f"playbook snapshot not sent: {type(e).__name__}: {e}")
            return 0
        self._published = key
        return 1

    def status(self) -> dict:
        return {"configured": self.configured(), "path": self.path,
                "files": self.files(), "entries": len(self.entries()),
                "fingerprint": self.index.fingerprint(PLAYBOOK_ID),
                "candidates": self.promote.view(limit=10)}

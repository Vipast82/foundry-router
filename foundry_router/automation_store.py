"""Automation config store: versioned coordinator settings and role rules.

One draft per project (edited by the GUI) and an append-only list of
revisions; the highest revision is the deployed one. Rollback appends a copy
of an older revision. The coordinator pulls the deployed revision
(GET /v1/foundry/control/config) and reports what it applied.

Documents are validated with the shared schema (foundry_automation), the same
code the coordinator's profile loader uses. Secrets and machine bootstrap
values (URLs, env-var names, workspace path) are never stored here.

Contract: side-session notes revamp/config_api.md.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import tomllib
from typing import Any, Optional

from foundry_automation import FORMAT, ROUTER_DOC
from foundry_automation import document as D
from foundry_automation import role_rules as RR
from foundry_automation.schema import schema_json

_PROJECT = re.compile(r"^(_router|[a-z0-9][a-z0-9._-]{0,63})$")

DDL = (
    """CREATE TABLE IF NOT EXISTS automation_drafts (
      project TEXT PRIMARY KEY, doc TEXT, etag TEXT, base_rev INTEGER,
      updated_ts REAL, actor TEXT)""",
    """CREATE TABLE IF NOT EXISTS automation_revisions (
      project TEXT, rev INTEGER, doc TEXT, hash TEXT, ts REAL, actor TEXT, note TEXT,
      source TEXT, rollback_of INTEGER, PRIMARY KEY (project, rev))""",
    """CREATE TABLE IF NOT EXISTS automation_audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, project TEXT, actor TEXT, action TEXT,
      rev INTEGER, detail TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_automation_audit ON automation_audit(project, id)",
    """CREATE TABLE IF NOT EXISTS automation_applied (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, project TEXT, source_id TEXT,
      rev INTEGER, hash TEXT, status TEXT, detail TEXT, restart_needed TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_automation_applied ON automation_applied(project, id)",
)


class StoreError(ValueError):
    def __init__(self, message: str, code: str = "invalid", status: int = 400, **extra):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _uj(v: Optional[str], default: Any = None) -> Any:
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def check_project(project: str) -> str:
    p = str(project or "").strip()
    if not _PROJECT.match(p):
        raise StoreError(f"bad project id {p[:40]!r}", "bad_project")
    return p


class AutomationStore:
    def __init__(self, db, clock=time.time):
        self.db = db
        self.clock = clock
        self._lock = threading.RLock()
        for stmt in DDL:
            db.execute(stmt)

    # -- audit ---------------------------------------------------------------------

    def _audit(self, project: str, actor: str, action: str, rev: Optional[int] = None,
               detail: Any = "") -> None:
        self.db.execute("INSERT INTO automation_audit (ts, project, actor, action, rev, detail) "
                        "VALUES (?,?,?,?,?,?)",
                        (self.clock(), project, (actor or "")[:80], action, rev,
                         detail if isinstance(detail, str) else _j(detail)[:4000]))

    def audit(self, project: str, limit: int = 100) -> list[dict]:
        rows = self.db.query("SELECT ts, actor, action, rev, detail FROM automation_audit "
                             "WHERE project=? ORDER BY id DESC LIMIT ?",
                             (check_project(project), max(1, min(int(limit), 1000))))
        for r in rows:
            r["detail"] = _uj(r["detail"], r["detail"]) if r["detail"] else ""
        return rows

    # -- revisions -----------------------------------------------------------------

    def _latest(self, project: str) -> Optional[dict]:
        r = self.db.query_one("SELECT * FROM automation_revisions WHERE project=? "
                              "ORDER BY rev DESC LIMIT 1", (project,))
        if r:
            r["doc"] = _uj(r["doc"], {})
        return r

    def deployed(self, project: str) -> Optional[dict]:
        """The deployed revision {rev, hash, ts, actor, note, source, doc} or None."""
        return self._latest(check_project(project))

    def revisions(self, project: str, limit: int = 50) -> list[dict]:
        return self.db.query("SELECT rev, hash, ts, actor, note, source, rollback_of FROM "
                             "automation_revisions WHERE project=? ORDER BY rev DESC LIMIT ?",
                             (check_project(project), max(1, min(int(limit), 500))))

    def revision(self, project: str, rev: int) -> dict:
        r = self.db.query_one("SELECT * FROM automation_revisions WHERE project=? AND rev=?",
                              (check_project(project), int(rev)))
        if not r:
            raise StoreError(f"{project} has no revision {rev}", "not_found", 404)
        r["doc"] = _uj(r["doc"], {})
        return r

    def _append(self, project: str, doc: dict, actor: str, note: str, source: str,
                rollback_of: Optional[int] = None) -> dict:
        with self._lock:
            last = self._latest(project)
            rev = (last["rev"] if last else 0) + 1
            h = D.doc_hash(doc)
            ts = self.clock()
            self.db.execute("INSERT INTO automation_revisions (project, rev, doc, hash, ts, actor, "
                            "note, source, rollback_of) VALUES (?,?,?,?,?,?,?,?,?)",
                            (project, rev, _j(doc), h, ts, (actor or "")[:80], (note or "")[:500],
                             source, rollback_of))
        return {"rev": rev, "hash": h, "ts": ts}

    # -- drafts --------------------------------------------------------------------

    def _draft_row(self, project: str) -> Optional[dict]:
        r = self.db.query_one("SELECT * FROM automation_drafts WHERE project=?", (project,))
        if r:
            r["doc"] = _uj(r["doc"], {})
        return r

    def _write_draft(self, project: str, doc: dict, base_rev: Optional[int], actor: str) -> str:
        etag = D.doc_hash({"draft": doc, "n": time.time_ns()})
        self.db.execute(
            "INSERT INTO automation_drafts (project, doc, etag, base_rev, updated_ts, actor) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET doc=excluded.doc, "
            "etag=excluded.etag, base_rev=excluded.base_rev, updated_ts=excluded.updated_ts, "
            "actor=excluded.actor",
            (project, _j(doc), etag, base_rev, self.clock(), (actor or "")[:80]))
        return etag

    def draft(self, project: str, actor: str = "") -> dict:
        project = check_project(project)
        with self._lock:
            row = self._draft_row(project)
            if row is None:
                dep = self._latest(project)
                doc = copy.deepcopy(dep["doc"]) if dep else D.empty(project)
                self._write_draft(project, doc, dep["rev"] if dep else None, actor or "system")
                row = self._draft_row(project)
        dep = self._latest(project)
        return {"project": project, "doc": row["doc"], "etag": row["etag"],
                "base_rev": row["base_rev"], "updated_ts": row["updated_ts"],
                "updated_by": row["actor"],
                "dirty": D.canonical(row["doc"]) != D.canonical(dep["doc"] if dep else None)}

    def _hard_refusals(self, project: str, doc: Any) -> list[dict]:
        """Problems that refuse a save outright (nothing stored): not a
        document, local-only keys, literal secrets."""
        if not isinstance(doc, dict) or not isinstance(doc.get("settings", {}), dict):
            return [{"path": "", "message": "the document must be an object with 'settings'",
                     "code": "invalid"}]
        v = D.validate(doc, project)
        return [e for e in v["errors"] if e["code"] in ("local_key", "secret")
                or e["path"] in ("format", "project")]

    def _normalise(self, project: str, doc: dict) -> dict:
        doc = copy.deepcopy(doc)
        doc.setdefault("format", FORMAT)
        doc["project"] = project
        doc.setdefault("settings", {})
        if project != ROUTER_DOC:
            doc.setdefault("role_rules", RR.all_defaults())
        return doc

    def save_draft(self, project: str, doc: Any, etag: str, actor: str) -> dict:
        project = check_project(project)
        if isinstance(doc, dict):
            doc = self._normalise(project, doc)
        hard = self._hard_refusals(project, doc)
        if hard:
            raise StoreError("the draft has settings the router never stores", "invalid", 422,
                             validation={"ok": False, "errors": hard, "warnings": []})
        with self._lock:
            cur = self.draft(project, actor)
            if not etag or etag != cur["etag"]:
                raise StoreError("the draft changed since you loaded it", "etag_mismatch", 409,
                                 current_etag=cur["etag"])
            new = self._write_draft(project, doc, cur["base_rev"], actor)
        val = self.validate(project, doc)
        self._audit(project, actor, "draft_saved", cur["base_rev"],
                    {"changes": len(D.diff(cur["doc"], doc)), "ok": val["ok"]})
        return {"etag": new, "validation": val}

    def reset_draft(self, project: str, rev: Optional[int], actor: str) -> dict:
        project = check_project(project)
        if rev is None:
            dep = self._latest(project)
            doc, base = (copy.deepcopy(dep["doc"]), dep["rev"]) if dep else (D.empty(project), None)
        else:
            r = self.revision(project, rev)
            doc, base = r["doc"], r["rev"]
        with self._lock:
            self._write_draft(project, doc, base, actor)
        self._audit(project, actor, "draft_reset", base)
        return self.draft(project, actor)

    # -- validate / import ---------------------------------------------------------

    def validate(self, project: str, doc: Any = None) -> dict:
        project = check_project(project)
        if doc is None:
            doc = self.draft(project)["doc"]
        v = D.validate(doc, project)
        dep = self._latest(project)
        v["restart_needed"] = D.restart_needed(dep["doc"] if dep else None, doc) \
            if isinstance(doc, dict) else []
        if project != ROUTER_DOC and isinstance(doc, dict) and isinstance(
                doc.get("role_rules"), dict) and not any(
                e["path"].startswith("role_rules") for e in v["errors"]):
            v["effective_role_rules"] = {r: RR.normalize(r, doc["role_rules"].get(r))
                                         for r in RR.RULE_ROLES}
        return v

    def import_profile(self, project: str, toml_text: str, actor: str,
                       dry_run: bool = False) -> dict:
        project = check_project(project)
        if project == ROUTER_DOC:
            raise StoreError("import a project profile into a project document", "bad_project")
        try:
            raw = tomllib.loads(toml_text)
        except tomllib.TOMLDecodeError as e:
            raise StoreError(f"not valid TOML: {e}", "bad_toml") from None
        if raw.get("id") and raw["id"] != project:
            raise StoreError(f"the profile is for {raw['id']!r}, not {project!r}",
                             "project_mismatch", 409)
        doc, dropped, notes = D.import_profile(raw, project)
        val = self.validate(project, doc)
        out = {"doc": doc, "dropped_local": dropped, "notes": notes, "validation": val}
        if dry_run:
            return out
        hard = self._hard_refusals(project, doc)
        if hard:
            raise StoreError("the profile has secrets in it", "invalid", 422,
                             validation={"ok": False, "errors": hard, "warnings": []})
        with self._lock:
            cur = self._draft_row(project)
            out["etag"] = self._write_draft(project, doc, cur["base_rev"] if cur else None, actor)
        self._audit(project, actor, "import", None, {"dropped_local": dropped, "ok": val["ok"]})
        return out

    # -- deploy / rollback ---------------------------------------------------------

    def deploy(self, project: str, etag: str, note: str, actor: str) -> dict:
        project = check_project(project)
        with self._lock:
            cur = self.draft(project, actor)
            if not etag or etag != cur["etag"]:
                raise StoreError("the draft changed since you loaded it", "etag_mismatch", 409,
                                 current_etag=cur["etag"])
            val = self.validate(project, cur["doc"])
            if not val["ok"]:
                self._audit(project, actor, "validate_failed", None,
                            {"errors": val["errors"][:20]})
                raise StoreError("the draft does not validate", "invalid", 422, validation=val)
            dep = self._latest(project)
            if dep and D.canonical(dep["doc"]) == D.canonical(cur["doc"]):
                return {"rev": dep["rev"], "hash": dep["hash"], "ts": dep["ts"],
                        "unchanged": True}
            out = self._append(project, cur["doc"], actor, note, "deploy")
            self.db.execute("UPDATE automation_drafts SET base_rev=? WHERE project=?",
                            (out["rev"], project))
        self._audit(project, actor, "deploy", out["rev"],
                    {"note": note, "changes": len(D.diff(dep["doc"] if dep else None,
                                                         cur["doc"])),
                     "restart_needed": val.get("restart_needed") or []})
        out["restart_needed"] = val.get("restart_needed") or []
        return out

    def rollback(self, project: str, rev: int, note: str, actor: str,
                 reset_draft: bool = True) -> dict:
        project = check_project(project)
        old = self.revision(project, rev)
        val = D.validate(old["doc"], project)
        if not val["ok"]:
            raise StoreError(f"revision {rev} no longer validates", "invalid", 422,
                             validation=val)
        with self._lock:
            out = self._append(project, old["doc"], actor, note or f"rollback to {rev}",
                               "rollback", rollback_of=int(rev))
            if reset_draft:
                self._write_draft(project, old["doc"], out["rev"], actor)
        self._audit(project, actor, "rollback", out["rev"], {"rollback_of": int(rev),
                                                             "note": note})
        return out

    # -- diff ----------------------------------------------------------------------

    def _resolve(self, project: str, which: str) -> Optional[dict]:
        w = str(which or "").strip()
        if w == "draft":
            return self.draft(project)["doc"]
        if w in ("deployed", ""):
            dep = self._latest(project)
            return dep["doc"] if dep else None
        try:
            return self.revision(project, int(w))["doc"]
        except ValueError:
            raise StoreError("from/to must be draft, deployed or a revision number") from None

    def diff(self, project: str, frm: str = "deployed", to: str = "draft") -> dict:
        project = check_project(project)
        return {"from": frm, "to": to,
                "changes": D.diff(self._resolve(project, frm), self._resolve(project, to))}

    # -- coordinator reports -------------------------------------------------------

    def record_applied(self, project: str, source_id: str, rev: Any, hash_: str, status: str,
                       detail: str = "", restart_needed: Optional[list] = None) -> dict:
        project = check_project(project)
        if status not in ("applied", "rejected", "pending"):
            raise StoreError("status must be applied, rejected or pending")
        try:
            rev = int(rev)
        except (TypeError, ValueError):
            raise StoreError("rev must be a revision number") from None
        rn = [str(x)[:120] for x in (restart_needed or [])][:100]
        self.db.execute("INSERT INTO automation_applied (ts, project, source_id, rev, hash, "
                        "status, detail, restart_needed) VALUES (?,?,?,?,?,?,?,?)",
                        (self.clock(), project, str(source_id or "")[:120], rev,
                         str(hash_ or "")[:32], status, str(detail or "")[:4000], _j(rn)))
        if status != "pending":
            self._audit(project, f"coordinator:{source_id}"[:80], status, rev,
                        {"detail": str(detail or "")[:1000], "restart_needed": rn})
        return {"ok": True}

    def applied(self, project: str, limit: int = 50) -> list[dict]:
        rows = self.db.query("SELECT ts, source_id, rev, hash, status, detail, restart_needed "
                             "FROM automation_applied WHERE project=? ORDER BY id DESC LIMIT ?",
                             (check_project(project), max(1, min(int(limit), 500))))
        for r in rows:
            r["restart_needed"] = _uj(r["restart_needed"], [])
        return rows

    # -- overview ------------------------------------------------------------------

    def projects(self) -> list[dict]:
        names = {r["project"] for r in self.db.query(
            "SELECT project FROM automation_drafts UNION SELECT project FROM "
            "automation_revisions")}
        names.add(ROUTER_DOC)
        out = []
        for p in sorted(names):
            dep = self._latest(p)
            row = self._draft_row(p)
            last = self.applied(p, 1)
            out.append({
                "project": p,
                "deployed_rev": dep["rev"] if dep else None,
                "deployed_ts": dep["ts"] if dep else None,
                "deployed_by": dep["actor"] if dep else None,
                "draft_etag": row["etag"] if row else None,
                "draft_dirty": bool(row) and D.canonical(row["doc"]) != D.canonical(
                    dep["doc"] if dep else None),
                "applied": last[0] if last else None,
            })
        return out

    @staticmethod
    def schema() -> dict:
        return schema_json()

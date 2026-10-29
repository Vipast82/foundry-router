"""The pipeline: ingest -> correlate -> episodes -> curate -> export, plus the
coordinator's closeout queue, status for the UI, and Foundry reporting.

All entry points are idempotent; any of them can be re-run after an
interruption.
"""

from __future__ import annotations

import glob
import json
import os
import time
import urllib.request
from collections import defaultdict
from typing import Optional

from . import correlate, curate as C, episodes as E
from .adapters import cline, coordinator, foundry, gate, git as G, kilo
from .config import TrainingProject, unbound_of
from .ingest import Ingestor
from .store import TrainingStore


class Pipeline:
    def __init__(self, store: TrainingStore, projects: list[TrainingProject],
                 known_secrets=()):
        self.store = store
        self.projects = {p.id: p for p in projects}
        for p in projects:
            if p.unbound_project and p.unbound_project not in self.projects:
                self.projects[p.unbound_project] = unbound_of(p)
        self.unbound_project = next((p.id for p in self.projects.values() if p.unbound), "")
        self.known = list(known_secrets)
        self.ing = Ingestor(store, self.known)

    def _cfg(self, pid: str) -> TrainingProject:
        if pid not in self.projects:
            raise ValueError(f"no training profile for project {pid!r}")
        return self.projects[pid]

    def _allowed(self, pid: str, source: str) -> bool:
        p = self.projects.get(pid)
        return bool(p and p.enabled and source in p.sources)

    def _filter(self, records, source):
        for rid, pid, payload, prov in records:
            if not self._allowed(pid, source):
                if pid:
                    self.store.gap(pid, "", source, "capture-disabled",
                                   f"{source} capture is not enabled for {pid}; record {rid} skipped")
                yield rid, "", payload, prov
            else:
                yield rid, pid, payload, prov

    # -- ingestion ---------------------------------------------------------------------

    def ingest_kilo(self, exports=(), api: Optional[dict] = None, db: Optional[str] = None,
                    directory: str = "") -> dict:
        sessions = [kilo.from_export(p) for p in exports]
        snap = None
        mode = "export"
        if api:
            mode = "api"
            todo = list(api["sessions"])
            seen = set()
            while todo:                          # follow children: helper sessions too
                sid = todo.pop()
                if sid in seen:
                    continue
                seen.add(sid)
                try:
                    s = kilo.from_api(api["url"], sid, api.get("password", ""),
                                      api.get("directory", ""))
                except Exception as e:                            # noqa: BLE001
                    # recorded, not fatal: the other sessions are still ingested
                    self.store.gap(api.get("project_id") or "", sid, "kilo",
                                   "kilo-session-unavailable", f"{type(e).__name__}: {e}"[:300])
                    continue
                sessions.append(s)
                todo.extend(s.get("children") or [])
        if db:
            mode = "db-snapshot"
            snap = kilo.db_snapshot(db)
            sessions += list(kilo.from_db(snap, directory=directory))
        try:
            plist = list(self.projects.values())

            def norm(payload, pid, raw_ref, redaction):
                cfg = self.projects[pid]
                return kilo.normalize(payload, pid, raw_ref, redaction, store=self.store,
                                      artifact_roots=cfg.artifact_dirs())
            counts = self.ing.run(f"kilo:{mode}", self._filter(
                kilo.session_records(sessions, plist, mode), "kilo"), norm)
            for s in sessions:
                pid, _ = kilo.project_of(s, plist)
                if pid and s.get("_sanitized"):
                    self.store.gap(pid, s["info"].get("id"), "kilo", "sanitized-export",
                                   "kilo export --sanitize: text and tool data are placeholders")
            for pid in {kilo.project_of(s, plist)[0] for s in sessions}:
                if pid and self._allowed(pid, "kilo"):
                    self.context_gaps(pid)
            return counts
        finally:
            if snap:
                kilo.cleanup_snapshot(snap)

    def ingest_foundry(self, url: str = "", key: str = "", capture_db: str = "",
                       project_map: Optional[dict] = None) -> dict:
        key_name = f"foundry:{url or capture_db}"
        after = int(self.store.checkpoint(key_name) or 0)
        rows = foundry.from_api(url, key, after) if url else foundry.from_db(capture_db, after)
        out = self.ing.run(key_name, self._filter(foundry.records(
            rows, project_map, self.unbound_project), "foundry"),
                           foundry.normalize,
                           cursor_of=lambda rid, payload: payload.get("id") or 0)
        if url:
            # the coordinator's verified outcomes (gate PASS, review APPROVED, vision
            # approved) arrive as their own records, so a label posted after the call
            # was ingested still reaches its episode
            okey = f"foundry-outcomes:{url}"
            orows = foundry.outcomes_from_api(url, key, int(self.store.checkpoint(okey) or 0))
            o = self.ing.run(okey, self._filter(foundry.outcome_records(orows), "foundry"),
                             foundry.normalize_outcome,
                             cursor_of=lambda rid, payload: payload.get("id") or 0)
            out = dict(out, outcomes=o.get("new_events", 0))
        return out

    def ingest_coordinator(self, db: str) -> dict:
        def norm(payload, pid, raw_ref, redaction):
            cfg = self.projects[pid]
            # vision-verdict frames are read only from the project's artifact roots
            return coordinator.normalize(payload, pid, raw_ref, redaction, store=self.store,
                                         workspace=cfg.workspace,
                                         artifact_roots=cfg.artifact_dirs())
        return self.ing.run(f"coordinator:{db}", self._filter(coordinator.from_db(db),
                                                              "coordinator"), norm)

    def ingest_gate(self, pid: str) -> dict:
        """studio_gate verdict JSON under <workspace>/.plan/tmp/gate (labels +
        capture artifacts). Part of the coordinator source: the runner is the
        coordinator's verification tool."""
        cfg = self._cfg(pid)
        if not os.path.isdir(cfg.workspace):
            self.store.gap(pid, "", "gate", "workspace-missing", cfg.workspace)
            return {"records": 0}

        def norm(payload, p, raw_ref, redaction):
            return gate.normalize(payload, p, raw_ref, redaction, store=self.store,
                                  workspace=cfg.workspace, artifact_roots=cfg.artifact_dirs())
        recs = ((rid, p, pay, prov) if self._allowed(p, "coordinator") else (rid, "", pay, prov)
                for rid, p, pay, prov in gate.records(cfg.workspace, pid))
        return self.ing.run(f"gate:{pid}", recs, norm)

    def ingest_cline(self, sessions_dir: str) -> dict:
        return self.ing.run(f"cline:{sessions_dir}", self._filter(
            cline.sessions(sessions_dir, list(self.projects.values())), "cline"), cline.normalize)

    def ingest_git(self, pid: str) -> dict:
        cfg = self._cfg(pid)
        shas = set()
        bases, heads = {}, {}
        for e in self.store.events(pid, "event_type IN ('task_transition','verification',"
                                        "'review','approval')"):
            evd = e["facts"].get("evidence")
            evd = evd if isinstance(evd, dict) else {}
            for k in ("head_sha", "base_sha", "reviewed_commit_sha"):
                if evd.get(k):
                    shas.add(evd[k])
            if evd.get("base_sha"):
                bases.setdefault(e["task_id"], set()).add(evd["base_sha"])
            if evd.get("head_sha"):
                heads.setdefault(e["task_id"], set()).add(evd["head_sha"])
            if e["source_revision"]:
                shas.add(e["source_revision"])
        if not os.path.isdir(cfg.workspace):
            self.store.gap(pid, "", "git", "workspace-missing", cfg.workspace)
            return {"records": 0}
        # intermediate commits of each task (helper-tagged steps are committed
        # on their own between base and head)
        for tid, bs in bases.items():
            for b in bs:
                for h in heads.get(tid, ()):
                    shas.update(G.range_shas(cfg.workspace, b, h))
        res = self.ing.run(f"git:{pid}", self._filter(G.commits(cfg.workspace, pid, sorted(shas)),
                                                      "git"), G.normalize)
        # harvested frontier pre-code models (docs/progress/*_PRECODE/)
        pre = self.ing.run(f"git-precode:{pid}", self._filter(
            G.precode_records(cfg.workspace, pid), "git"), G.normalize)
        res["precode_records"] = pre.get("records", 0)
        return res

    NO_SYSTEM_PROMPT = ("Kilo records don't include the system prompt or tool definitions, "
                        "and no Foundry capture of this session was found")

    def context_gaps(self, pid: str) -> dict:
        """`no-system-prompt` for exactly the Kilo sessions no Foundry capture
        (a `received` or `backend_call` row with the same session id) covers;
        a gap whose session is covered now is removed. Run after Kilo ingestion
        and again at every curate, since Foundry is often ingested after Kilo."""
        q = self.store.norm.q
        kilo_s = {r["session_id"] for r in q(
            "SELECT DISTINCT session_id FROM events WHERE project_id=? AND source_adapter='kilo' "
            "AND session_id IS NOT NULL AND deleted=0", (pid,))}
        covered = {r["session_id"] for r in q(
            "SELECT DISTINCT session_id FROM events WHERE project_id=? AND "
            "source_adapter='foundry' AND event_type IN ('model_call','model_request') AND "
            "session_id IS NOT NULL AND deleted=0", (pid,))}
        missing = sorted(kilo_s - covered)
        for sid in missing:
            self.store.gap(pid, sid, "kilo", "no-system-prompt", self.NO_SYSTEM_PROMPT)
        resolved = self.store.resolve_gaps(pid, "no-system-prompt", sorted(covered))
        return {"missing": len(missing), "resolved": resolved}

    # -- curation ------------------------------------------------------------------------

    def curate(self, pid: str, explicit_links: Optional[dict] = None) -> dict:
        cfg = self._cfg(pid)
        if cfg.unbound:
            # no task, no workspace: its calls feed only the calls kind
            return {"episodes": 0, "unbound": True}
        gaps = self.context_gaps(pid)
        links = correlate.run(self.store, pid, explicit_links)
        red = self.ing.redactor(cfg.allowed_email_domains)

        def diff_fn(base, head):
            if not os.path.isdir(cfg.workspace):
                return "", False
            d, cut = G.diff(cfg.workspace, base, head)
            return red.text(d), cut
        eps = E.build(self.store, pid, cfg, diff_fn=diff_fn)
        counts = C.run(self.store, pid, eps, cfg, self.known)
        return {"correlations": links, "episodes": len(eps), "context_gaps": gaps, **counts}

    # -- coordinator closeout queue -------------------------------------------------------

    def process_queue(self, spool: str, foundry_url: str = "", foundry_key: str = "",
                      kilo_api: Optional[dict] = None) -> list[dict]:
        """Coordinator closeout manifests (one JSON file per finished task):
        ingest what they point at, curate, record completeness. A manifest
        is moved to done/ only after it was processed; a failure leaves it
        (and records why), never discards it."""
        out = []
        os.makedirs(os.path.join(spool, "done"), exist_ok=True)
        done_dbs: set = set()
        done_pids: set = set()
        done_foundry = False
        settle: list = []
        for path in sorted(glob.glob(os.path.join(spool, "*.json"))):
            try:
                with open(path, encoding="utf-8") as f:
                    m = json.load(f)
                pid, task = m["project_id"], m["task_id"]
            except (OSError, ValueError, KeyError, TypeError) as e:
                # one unreadable manifest never blocks the rest of the spool; it
                # stays in place (never discarded) and is reported every pass
                out.append({"manifest": os.path.basename(path), "error":
                            f"unreadable manifest: {type(e).__name__}: {e}"[:300], "left": True})
                continue
            res = {"manifest": os.path.basename(path), "project_id": pid, "task_id": task}
            if pid not in self.projects:
                # another project's manifest in a shared spool: leave it for a run
                # that has that project's profile (never mark it done unread)
                res["left"] = "no profile for this project in this run"
                out.append(res)
                continue
            # 2026-10-29: the shared sources (the whole coordinator DB, Foundry, git, gate
            # files) and curation were re-read for EVERY manifest; 33 queued manifests meant
            # 33 full passes over a 167 MB DB, and one run held 32 GB for 30 hours. Phase 1
            # ingests each shared source once per call and each manifest's own Kilo
            # sessions; phase 2 curates each project once and settles the manifests.
            try:
                if not self._allowed(pid, "coordinator"):
                    res["skipped"] = "training capture not enabled for this project"
                    os.replace(path, os.path.join(spool, "done", os.path.basename(path)))
                    out.append(res)
                    continue
                db = m.get("coordinator_db")
                if db and db not in done_dbs:
                    self.ingest_coordinator(db)
                    done_dbs.add(db)
                roles = m.get("session_roles") or {}
                kilo_sessions = [s for s in m.get("sessions") or []
                                 if (roles.get(s) or {}).get("client", "kilo") == "kilo"]
                if kilo_api and kilo_sessions:
                    self.ingest_kilo(api={**kilo_api, "sessions": kilo_sessions,
                                          "project_id": pid,
                                          "directory": kilo_api.get("directory")
                                          or self._cfg(pid).workspace})
                if foundry_url and not done_foundry:
                    self.ingest_foundry(url=foundry_url, key=foundry_key)
                    done_foundry = True
                if pid not in done_pids:
                    self.ingest_git(pid)
                    self.ingest_gate(pid)
                    done_pids.add(pid)
                settle.append((path, m, res, kilo_sessions))
            except Exception as e:                                # noqa: BLE001
                res["error"] = f"{type(e).__name__}: {e}"[:500]
                self.store.gap(pid, "", "coordinator", "curation-failed", res["error"])
                out.append(res)
        curated: dict = {}
        for path, m, res, kilo_sessions in settle:
            pid = m["project_id"]
            try:
                if pid not in curated:
                    curated[pid] = self.curate(pid)
                res["curation"] = curated[pid]
                have = {e["session_id"] for e in self.store.events(
                    pid, "event_type IN ('user_message','assistant_message','model_call')")}
                missing = [s for s in kilo_sessions if s not in have]
                res["sessions_missing"] = missing
                for s in missing:
                    self.store.gap(pid, s, "coordinator", "session-not-captured",
                                   f"task {m['task_id']}: no conversation records for {s}")
                os.replace(path, os.path.join(spool, "done", os.path.basename(path)))
            except Exception as e:                                # noqa: BLE001
                res["error"] = f"{type(e).__name__}: {e}"[:500]
                self.store.gap(pid, "", "coordinator", "curation-failed", res["error"])
            out.append(res)
        return out

    # -- reporting -------------------------------------------------------------------------

    def status(self) -> dict:
        st = self.store
        proj = {}
        for pid in sorted(set(st.projects()) | set(self.projects)):
            cfg = self.projects.get(pid)
            ev = st.norm.q("SELECT source_adapter, event_type, COUNT(*) n FROM events WHERE "
                           "project_id=? AND deleted=0 GROUP BY source_adapter, event_type", (pid,))
            sessions = st.norm.q("SELECT source_adapter, COUNT(DISTINCT session_id) n FROM events "
                                 "WHERE project_id=? AND session_id IS NOT NULL GROUP BY "
                                 "source_adapter", (pid,))
            eps = st.cur.q("SELECT kind, status, split, COUNT(*) n FROM episodes WHERE "
                           "project_id=? GROUP BY kind, status, split", (pid,))
            reasons = defaultdict(int)
            for r in st.cur.q("SELECT reasons FROM episodes WHERE project_id=? AND status IN "
                              "('excluded','duplicate')", (pid,)):
                for x in json.loads(r["reasons"] or "[]"):
                    reasons[x] += 1
            red = st.norm.q("SELECT redaction_status, COUNT(*) n FROM events WHERE project_id=? "
                            "GROUP BY redaction_status", (pid,))
            arts = st.norm.q("SELECT status, COUNT(*) n FROM artifact_refs WHERE project_id=? "
                             "GROUP BY status", (pid,))
            proj[pid] = {"capture": {"enabled": bool(cfg and cfg.enabled),
                                     "sources": cfg.sources if cfg else []},
                         "events": ev, "sessions": sessions, "episodes": eps,
                         "exclusion_reasons": dict(reasons), "redaction": red,
                         "artifacts": arts, "gaps": len(st.gaps(pid)),
                         "duplicates": sum(r["n"] for r in eps if r["status"] == "duplicate")}
        runs = st.norm.q("SELECT source_key, status, counts, error, started_ts, finished_ts FROM "
                         "ingest_runs ORDER BY id DESC LIMIT 20")
        datasets = st.cur.q("SELECT dataset_id, kind, target, purpose, created_ts, status, "
                            "validation FROM datasets ORDER BY created_ts DESC LIMIT 30")
        for d in datasets:
            v = json.loads(d.pop("validation") or "{}")
            d["examples"] = v.get("examples")
            d["errors"] = len(v.get("errors") or [])
        return {"projects": proj, "ingest_runs": runs, "datasets": datasets,
                "gaps": st.gaps()[-50:], "interrupted_runs": len(st.interrupted_runs()),
                "store": str(st.root)}

    def episode_summaries(self, limit: int = 300) -> list[dict]:
        """Redacted, truncated cards for the Foundry UI (no raw payloads)."""
        out = []
        for r in self.store.cur.q("SELECT * FROM episodes ORDER BY updated_ts DESC LIMIT ?",
                                  (limit,)):
            ep = json.loads(r["data"] or "{}")
            turns = []
            for m in (ep.get("messages") or [])[:40]:
                t = {"role": m["role"], "author": (m.get("author") or {}).get("kind"),
                     "text": (m.get("content") or "")[:300]}
                if m.get("tool_calls"):
                    t["tool_calls"] = [tc["name"] for tc in m["tool_calls"]]
                turns.append(t)
            out.append({"episode_id": r["episode_id"], "project_id": r["project_id"],
                        "task_id": r["task_id"], "kind": r["kind"], "status": r["status"],
                        "split": r["split"], "reasons": json.loads(r["reasons"] or "[]"),
                        "outcome": {"verification": ep.get("outcome", {}).get("verification",
                                                                               {}).get("status"),
                                    "review": (ep.get("outcome", {}).get("review") or {}).get(
                                        "verdict"),
                                    "approval": bool(ep.get("outcome", {}).get("approval"))},
                        "sources": ep.get("sources"), "turns": turns,
                        "summary": {"session": ep.get("session_id"), "run": ep.get("run_id"),
                                    "attempt": ep.get("attempt"),
                                    "context_source": ep.get("context_source"),
                                    "revision": ep.get("revision"), "flags": {
                                        k: v for k, v in (ep.get("flags") or {}).items()
                                        if k != "artifacts"},
                                    "checks": (ep.get("outcome", {}).get("verification") or {})
                                    .get("checks"),
                                    "diff_stat": _diffstat((ep.get("diff") or {}).get("patch")),
                                    "eval_categories": ep.get("eval_categories"),
                                    "feedback": (ep.get("feedback") or {}).get("items")}})
        return out

    def push(self, foundry_url: str, key: str, pipeline_id: str) -> dict:
        def send(method, path, body):
            req = urllib.request.Request(foundry_url.rstrip("/") + path, method=method,
                                         data=json.dumps(body, default=str).encode())
            req.add_header("content-type", "application/json")
            if key:
                req.add_header("authorization", f"Bearer {key}")
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        send("PUT", f"/v1/foundry/training/status/{pipeline_id}", self.status())
        res = send("POST", f"/v1/foundry/training/episodes/{pipeline_id}",
                   {"episodes": self.episode_summaries()})
        return {"pushed": res.get("recorded"), "at": time.time()}


def _diffstat(patch: Optional[str]) -> dict:
    if not patch:
        return {}
    files = [l[6:] for l in patch.splitlines() if l.startswith("+++ b/")]
    add = sum(1 for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
    rem = sum(1 for l in patch.splitlines() if l.startswith("-") and not l.startswith("---"))
    return {"files": files[:20], "added": add, "removed": rem}

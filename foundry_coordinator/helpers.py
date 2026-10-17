"""Optional coding helpers dispatched by the coordinator (docs/HELPER_POLICY.md).

A helper unit is one bounded piece of optional assistance inside the approved
task: a code proposal, a test design, debugging advice or read-only
exploration, for one named responsibility. Units come from the task source
(`helpers: [...]` in a json task) or from the primary's own report
(`helper_requests`); a request from a model is ALWAYS optional, whatever it
calls itself, so it can't claim the required-work reserve.

For each unit the coordinator:
  1. asks Foundry whether optional work is admitted now for the helper
     persona of the preferred backend (quota windows of the serving account,
     helper streams free); no inference is made to ask
  2. defers it (reason + next eligible time, persisted) or starts a fresh,
     READ-ONLY Kilo session bound in Foundry with purpose, parent session,
     project/task/run and the assignment, so every call it makes is gated
  3. collects the final proposal when the session goes idle, and hands it to
     the idle primary at a safe point; the primary integrates, tests and
     commits, and the final revision goes through independent review

Helpers never edit files, run shell or Studio, delegate, commit, push or
approve anything; only the primary writes. One unit per responsibility.
At a quota cutoff a running unit finishes; nothing new starts.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Callable, Optional

from . import states as S
from .adapters.foundry import FoundryError

OPTIONAL_KINDS = ("code_proposal", "test_design", "debug_advice", "exploration")
SCHEMA = """
CREATE TABLE IF NOT EXISTS helper_units (
  unit_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, run_id TEXT, responsibility TEXT,
  kind TEXT, value TEXT, prompt TEXT, files TEXT, source TEXT, status TEXT, persona TEXT,
  backend TEXT, session_id TEXT, parent_session TEXT, attempts INTEGER DEFAULT 0, reason TEXT,
  next_check REAL, next_eligible REAL, result TEXT, delivered_session TEXT, created_ts REAL,
  updated_ts REAL, requests INTEGER DEFAULT 1, UNIQUE (project_id, task_id, responsibility));
"""
# statuses: pending -> deferred <-> running -> done -> delivered
#           pending/deferred -> skipped (primary was ready first; never blocks it)
#           running -> timed_out (handoff wait over) | failed | cancelled (task paused/cancelled)
REQUESTABLE_AGAIN = ("skipped", "timed_out", "failed", "cancelled")
_SUBAGENT_TITLE = re.compile(r"\(@([\w.-]+) subagent\)")


def child_agent(title: str) -> str:
    """Kilo names a task-tool child session '<description> (@<agent> subagent)'."""
    m = _SUBAGENT_TITLE.search(title or "")
    return m.group(1) if m else ""


class HelperManager:
    def __init__(self, coordinator, clock: Callable[[], float] = time.time):
        self.c = coordinator
        self.store = coordinator.store
        self.clock = clock
        self.store.conn.executescript(SCHEMA)
        try:                                  # v0.117.0 tables: additive column
            self.store.conn.execute("ALTER TABLE helper_units ADD COLUMN requests INTEGER "
                                    "DEFAULT 1")
        except Exception:                                        # noqa: BLE001
            pass

    # ------------------------------------------------------------------ settings

    @staticmethod
    def settings(P) -> dict:
        h = dict((getattr(P.profile, "raw", None) or {}).get("helpers") or {})
        h.setdefault("enabled", False)
        h.setdefault("personas", {})            # backend name -> helper persona
        h.setdefault("order", [])               # backend preference (else Foundry's)
        h.setdefault("selective", [])           # backends only for value=high units
        h.setdefault("agent", "au-helper")
        h.setdefault("worker_client", "")       # Foundry client that runs Kilo sessions
        h.setdefault("max_running", 3)
        h.setdefault("max_units_per_task", 8)
        h.setdefault("poll_seconds", 60)
        h.setdefault("timeout_minutes", 30)
        h.setdefault("max_result_chars", 20000)
        # an idle primary waits this long for ADMITTED, running helpers before
        # verification; then they are stopped (timed_out). Units that were never
        # admitted (deferred / unknown quota) never hold the primary.
        h.setdefault("handoff_wait_minutes", 15)
        # a skipped / timed-out unit may be requested again by a later report
        # up to this many requests in total
        h.setdefault("max_unit_requests", 2)
        h.setdefault("max_requests_per_report", 4)
        # model-spawned child sessions (Kilo task tool): agent -> purpose
        h.setdefault("child_purposes", {"au-planner": "planning", "au-review": "review",
                                        "au-deep-review": "review",
                                        "au-visual-review": "review"})
        return h

    def enabled(self, P) -> bool:
        return bool(self.settings(P)["enabled"]) and P.foundry is not None

    # ------------------------------------------------------------------ units

    def units(self, pid: str, tid: str, status: Optional[str] = None) -> list[dict]:
        rows = self.store.q("SELECT * FROM helper_units WHERE project_id=? AND task_id=?"
                            + (" AND status=?" if status else "") + " ORDER BY created_ts",
                            (pid, tid, status) if status else (pid, tid))
        for r in rows:
            r["files"] = json.loads(r["files"] or "[]")
        return rows

    def _add(self, pid, tid, run_id, u: dict, source: str, h: dict) -> Optional[str]:
        resp = re.sub(r"\s+", " ", str(u.get("responsibility") or "")).strip()[:200]
        kind = str(u.get("kind") or "code_proposal")
        if not resp or kind not in OPTIONAL_KINDS:
            return None
        now = self.clock()
        prior = self.store.q("SELECT * FROM helper_units WHERE project_id=? AND task_id=? AND "
                             "responsibility=?", (pid, tid, resp))
        if prior:
            p = prior[0]
            if p["status"] in REQUESTABLE_AGAIN and source == "report" and \
                    int(p["requests"] or 1) < int(h["max_unit_requests"]):
                self.store.x("UPDATE helper_units SET status='pending', kind=?, prompt=?, "
                             "files=?, run_id=?, requests=COALESCE(requests,1)+1, next_check=0, "
                             "reason=?, session_id=NULL, updated_ts=? WHERE unit_id=?",
                             (kind, str(u.get("prompt") or "")[:6000],
                              json.dumps([str(f) for f in (u.get("files") or [])][:30]), run_id,
                              f"requested again (was {p['status']}: {p['reason'] or ''})"[:500],
                              now, p["unit_id"]))
                self.store.event(pid, tid, "helper_unit", detail={"unit": p["unit_id"],
                                                                  "requested_again": True})
                return p["unit_id"]
            return None                       # one unit per responsibility
        if len(self.units(pid, tid)) >= int(h["max_units_per_task"]):
            return None
        uid = f"hu-{uuid.uuid4().hex[:10]}"
        n = self.store.x("INSERT OR IGNORE INTO helper_units (unit_id, project_id, task_id, run_id, "
                         "responsibility, kind, value, prompt, files, source, status, created_ts, "
                         "updated_ts, next_check) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (uid, pid, tid, run_id, resp, kind,
                          "high" if u.get("value") == "high" else "normal",
                          str(u.get("prompt") or "")[:6000],
                          json.dumps([str(f) for f in (u.get("files") or [])][:30]), source,
                          "pending", now, now, 0))
        if n:
            self.store.event(pid, tid, "helper_unit", detail={"unit": uid, "kind": kind,
                                                              "responsibility": resp,
                                                              "source": source})
        return uid

    def collect(self, P, t: dict, spec) -> None:
        """Units declared by the task source (one per responsibility)."""
        h = self.settings(P)
        for u in (spec.data or {}).get("helpers") or []:
            if isinstance(u, dict):
                self._add(P.id, t["task_id"], t["run_id"], u, "task", h)

    def collect_report(self, P, t: dict, spec, report: dict) -> list[str]:
        """Consume the helper_requests of a FRESH primary report, at the safe
        point where the primary went idle, once per report. A model request is
        always optional (never planning/review), never high-value, and only
        keeps files inside the task's scope."""
        from .profile import matches_any
        reqs = (report or {}).get("helper_requests") or []
        if not reqs or not isinstance(reqs, list):
            return []
        sig = hashlib.sha256(json.dumps(report, sort_keys=True, default=str).encode()).hexdigest()
        seen = list((t.get("data") or {}).get("helper_reports_seen") or [])
        if sig in seen:
            return []                         # already consumed (e.g. before a restart)
        h = self.settings(P)
        out = []
        for u in reqs[:int(h["max_requests_per_report"])]:
            if not isinstance(u, dict):
                continue
            files = [str(f) for f in (u.get("files") or []) if matches_any(str(f), spec.scope)]
            kind = u.get("kind") if u.get("kind") in OPTIONAL_KINDS else "code_proposal"
            uid = self._add(P.id, t["task_id"], t["run_id"], {
                "responsibility": u.get("responsibility"), "kind": kind,
                "prompt": u.get("prompt") or u.get("question") or "", "files": files,
                "value": "normal"}, "report", h)
            if uid:
                out.append(uid)
        self.store.merge_data(P.id, t["task_id"], helper_reports_seen=(seen + [sig])[-20:])
        return out

    # ------------------------------------------------------------------ the tick

    def tick(self, P, t: dict) -> None:
        """Called while the task is IMPLEMENTING (the primary owns the writer),
        and at the safe point right after the primary reported."""
        if not self.enabled(P) or t["state"] != S.IMPLEMENTING:
            return
        try:
            spec = P.adapter.describe(t["task_id"])
        except Exception:                                        # noqa: BLE001
            return
        self.collect(P, t, spec)
        h = self.settings(P)
        for u in self.units(P.id, t["task_id"], "running"):
            self._poll(P, u, h)
        running = len(self.units(P.id, t["task_id"], "running"))
        now = self.clock()
        for u in [*self.units(P.id, t["task_id"], "pending"),
                  *self.units(P.id, t["task_id"], "deferred")]:
            if running >= int(h["max_running"]):
                break
            if (u["next_check"] or 0) > now:
                continue
            if self._dispatch(P, t, spec, u, h):
                running += 1

    def _order(self, P, u: dict, h: dict) -> list[str]:
        order = list(h["order"]) or list(h["personas"])
        sel = set(h["selective"])
        return [b for b in order if b in h["personas"] and (u["value"] == "high" or b not in sel)]

    def _dispatch(self, P, t: dict, spec, u: dict, h: dict) -> bool:
        pid, tid = P.id, t["task_id"]
        reasons, nexts = [], []
        open_ended = False          # a backend that could clear at any time (UNKNOWN, error)
        for backend in self._order(P, u, h):
            persona = h["personas"][backend]
            try:
                adm = P.foundry.helper_admission(persona)
            except FoundryError as e:
                reasons.append(f"{backend}: admission check failed ({e})")
                open_ended = True
                continue
            if adm.get("routes") and any(r.get("source") == "local" for r in adm["routes"][:1]):
                reasons.append(f"{backend}: {persona} routes to the local backend, which the "
                               f"primary holds; helpers use frontier backends only")
                continue
            if not adm.get("admit"):
                reasons.append(f"{backend}: {adm.get('state', '?').upper()} — {adm.get('reason')}")
                if adm.get("next_eligible"):
                    nexts.append(float(adm["next_eligible"]))
                else:
                    open_ended = True
                continue
            return self._start(P, t, spec, u, h, backend, persona)
        if not reasons:
            reasons.append("no helper persona configured for any allowed backend "
                           "([helpers] personas)")
        now = self.clock()
        nxt = None if open_ended or not nexts else min(nexts)
        check = max(now + float(h["poll_seconds"]), nxt or 0)
        reason = "; ".join(reasons)[:900]
        if u["status"] != "deferred" or u["reason"] != reason:
            self.store.event(pid, tid, "helper_deferred", detail={"unit": u["unit_id"],
                                                                  "reason": reason,
                                                                  "next_eligible": nxt})
        self.store.x("UPDATE helper_units SET status='deferred', reason=?, next_eligible=?, "
                     "next_check=?, updated_ts=? WHERE unit_id=?",
                     (reason, nxt, check, now, u["unit_id"]))
        return False

    def _start(self, P, t: dict, spec, u: dict, h: dict, backend: str, persona: str) -> bool:
        pid, tid = P.id, t["task_id"]
        # one operation per request of the unit (a re-request is a new session)
        key = f"helper:{pid}:{tid}:{u['unit_id']}:{int(u.get('requests') or 1)}"
        prior = self.store.op_begin(key, pid, tid, "helper")
        res = dict((prior or {}).get("result") or {})
        sid = res.get("session_id")
        directory = P.profile.workspace
        if not sid:
            sid = P.client.start_session(
                directory=directory, title=f"{pid} {tid} helper {u['kind']}: "
                                          f"{u['responsibility'][:60]}",
                persona=persona, agent=h["agent"],
                metadata={"project": pid, "task": tid, "run": t["run_id"], "role": "helper",
                          "purpose": u["kind"], "unit": u["unit_id"]},
                permissions=P.adapter.helper_permissions(spec))
            res = {"session_id": sid}
            self.store.op_update(key, "started", res)
            self.store.add_session(sid, pid, tid, t["run_id"], "helper", "helper", t["attempt"],
                                   P.client.name, persona,
                                   {"parent": t["session_id"], "unit": u["unit_id"],
                                    "purpose": u["kind"]})
        if not res.get("bound"):
            # binding before inference, also when a restart came between the two
            try:
                P.foundry.bind(sid, pid, tid, t["run_id"], "helper", persona,
                               parent=t["session_id"] or "",
                               purpose=u["kind"], worker_client=h["worker_client"],
                               assignment={"unit": u["unit_id"],
                                           "responsibility": u["responsibility"],
                                           "files": u["files"], "kind": u["kind"]})
            except FoundryError as e:
                # without its binding the session's calls wouldn't be gated: don't run it
                P.client.abort(sid, directory=directory)
                self.store.update_session(sid, status="ended", outcome="unbound",
                                          ended_ts=self.clock())
                self.store.x("UPDATE helper_units SET status='deferred', reason=?, "
                             "next_check=?, updated_ts=? WHERE unit_id=?",
                             (f"binding failed: {e}"[:500], self.clock() + float(h["poll_seconds"]),
                              self.clock(), u["unit_id"]))
                self.store.x("DELETE FROM ops WHERE key=?", (key,))
                return False
            res["bound"] = True
            self.store.op_update(key, "bound", res)
        if not res.get("prompted"):
            P.client.prompt(sid, self.prompt(spec, u), directory=directory, persona=persona,
                            agent=h["agent"])
            self.store.op_update(key, "done", {**res, "prompted": True})
        now = self.clock()
        self.store.x("UPDATE helper_units SET status='running', persona=?, backend=?, "
                     "session_id=?, parent_session=?, attempts=attempts+1, reason=?, "
                     "next_eligible=NULL, updated_ts=? WHERE unit_id=?",
                     (persona, backend, sid, t["session_id"], f"admitted on {backend}", now,
                      u["unit_id"]))
        self.store.event(pid, tid, "helper_started", session_id=sid,
                         detail={"unit": u["unit_id"], "kind": u["kind"], "persona": persona,
                                 "backend": backend, "parent": t["session_id"]})
        return True

    @staticmethod
    def prompt(spec, u: dict) -> str:
        return (f"You are a read-only {u['kind'].replace('_', ' ')} helper for task "
                f"{spec.task_id} ({spec.title}). Responsibility: {u['responsibility']}.\n"
                + (f"Relevant files: {', '.join(u['files'])}\n" if u["files"] else "")
                + (f"\n{u['prompt']}\n" if u["prompt"] else "")
                + "\nRules: you may read files. You must not edit or write files, run shell "
                  "commands or Studio, delegate to other agents, commit, push, approve a patch "
                  "or declare the task done. Another session (the primary) decides what to "
                  "integrate and tests it; an independent reviewer checks the result.\n"
                  "Return ONE final message: a concise proposal (unified diff or test code "
                  "where relevant), the evidence it rests on, and how the primary should "
                  "verify it.")

    def _poll(self, P, u: dict, h: dict) -> None:
        directory = P.profile.workspace
        sid = u["session_id"]
        now = self.clock()
        # one prompt per helper session: until Kilo answers it, the last
        # message is the prompt itself, which already reads as busy
        st = P.client.state(sid, directory=directory, since_ts=0)
        if st.status == "busy":
            if now - u["updated_ts"] > float(h["timeout_minutes"]) * 60:
                P.client.abort(sid, directory=directory)
                self._finish(P, u, "failed", f"timed out after {h['timeout_minutes']} min", None)
            return
        if st.status == "attention":
            # a helper can't wait for a person; it ends without a result
            P.client.abort(sid, directory=directory)
            self._finish(P, u, "failed", "the helper asked for input; helpers can't wait", None)
            return
        if st.status in ("missing", "error"):
            self._finish(P, u, "failed", (st.error or st.status)[:300], None)
            return
        text = (st.final_text or "").strip()
        self._finish(P, u, "done" if text else "failed",
                     "proposal returned" if text else "no proposal text", text)

    def _finish(self, P, u: dict, status: str, reason: str, text: Optional[str]) -> None:
        cap = int(self.settings(P)["max_result_chars"])
        self.store.x("UPDATE helper_units SET status=?, reason=?, result=?, updated_ts=? "
                     "WHERE unit_id=?", (status, reason, (text or "")[:cap] or None, self.clock(),
                                         u["unit_id"]))
        self.store.update_session(u["session_id"], status="ended", outcome=status,
                                  ended_ts=self.clock())
        if P.foundry is not None:
            try:
                P.foundry.end(u["session_id"], status)
            except FoundryError:
                pass
        self.store.event(P.id, u["task_id"], f"helper_{status}", session_id=u["session_id"],
                         detail={"unit": u["unit_id"], "reason": reason})

    # ------------------------------------------------------------------ integration

    def abort_running(self, P, tid: str, status: str, reason: str) -> list[str]:
        """Stop every running unit of a task (no leaked sessions). A stopped
        unit has no result, so it's never delivered and never an author."""
        out = []
        for u in self.units(P.id, tid, "running"):
            try:
                P.client.abort(u["session_id"], directory=P.profile.workspace)
            except Exception:                                    # noqa: BLE001
                pass
            self._finish(P, u, status, reason, None)
            out.append(u["unit_id"])
        return out

    def skip_waiting(self, P, tid: str, reason: str) -> list[str]:
        """Units never admitted (pending / deferred) when the primary is ready:
        skipped, so optional work never holds required progress."""
        out = []
        for st in ("pending", "deferred"):
            for u in self.units(P.id, tid, st):
                why = f"{reason}" + (f" (last: {u['reason']})" if u.get("reason") else "")
                self.store.x("UPDATE helper_units SET status='skipped', reason=?, updated_ts=? "
                             "WHERE unit_id=?", (why[:900], self.clock(), u["unit_id"]))
                self.store.event(P.id, tid, "helper_skipped", detail={"unit": u["unit_id"],
                                                                      "reason": why[:300]})
                out.append(u["unit_id"])
        return out

    def request_guidance(self, P) -> str:
        """What the primary is told about asking for optional help."""
        h = self.settings(P)
        return ("\n## Optional helpers (read-only)\nIf a bounded, independent part of THIS task would "
                "benefit from a second opinion, you may ask the coordinator for up to "
                f"{h['max_requests_per_report']} read-only helpers by adding \"helper_requests\" to "
                "your coordinator-report block, then stopping your turn:\n"
                '"helper_requests": [{"kind": "code_proposal | test_design | debug_advice | '
                'exploration", "responsibility": "<one distinct part, e.g. S45 ordering test>", '
                '"prompt": "<what you need>", "files": ["<paths inside this task scope>"]}]\n'
                "Helpers can only read; they return proposals that arrive later in this session as "
                "advisory text. You still write, test and commit everything yourself. Helpers run "
                "only if the subscription quota allows optional work; a request may be deferred or "
                "skipped, so don't wait for one to make progress. Don't start au-helper through "
                "your task tool, and don't ask helpers to review or approve your work.\n")

    def proposals_for(self, pid: str, tid: str) -> list[dict]:
        return [u for u in self.units(pid, tid, "done") if not u["delivered_session"]]

    def integration_prompt(self, units: list[dict]) -> str:
        body = "\n\n".join(f"### {u['unit_id']} — {u['kind']}: {u['responsibility']} "
                           f"(by {u['persona']})\n{u['result']}" for u in units)
        return ("## Helper proposals (advisory)\nRead-only helpers returned these proposals for "
                "independent parts of this task. You decide what to integrate: apply only what "
                "fits the approved scope, run the meaningful tests, and commit as usual. Don't "
                "treat a proposal as verified. Mention which unit ids you integrated or "
                "rejected in your report.\n\n" + body + "\n")

    def mark_delivered(self, units: list[dict], session_id: str) -> None:
        for u in units:
            self.store.x("UPDATE helper_units SET status='delivered', delivered_session=?, "
                         "updated_ts=? WHERE unit_id=?", (session_id, self.clock(), u["unit_id"]))

    def authors(self, pid: str, tid: str) -> set[str]:
        """Personas whose proposals reached the primary (possible code authors)."""
        return {u["persona"] for u in self.units(pid, tid, "delivered") if u["persona"]}

    def child_purpose(self, P, title: str) -> str:
        """Purpose for a model-spawned child session (Kilo task tool), from its
        agent name. Unknown agents are optional exploration (never required)."""
        agent = child_agent(title)
        return self.settings(P)["child_purposes"].get(agent, "exploration") if agent \
            else "exploration"

    def snapshot(self, pid: str, tid: str) -> list[dict]:
        return [{k: u.get(k) for k in ("unit_id", "kind", "responsibility", "value", "source",
                                       "status", "persona", "backend", "session_id",
                                       "parent_session", "reason", "next_eligible", "attempts",
                                       "delivered_session", "updated_ts")}
                for u in self.units(pid, tid)]

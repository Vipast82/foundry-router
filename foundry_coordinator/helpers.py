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
# v0.118.0 proactive lanes (lanes.py). feedback: waiting | delivered | display_only |
# integrated | rejected | deferred | superseded | expired (what happened to the result)
LANE_COLUMNS = ("lane TEXT", "priority INTEGER", "parent_unit TEXT", "depth INTEGER DEFAULT 0",
                "artifact_id TEXT", "snapshot_id TEXT", "mode TEXT", "envelope TEXT",
                "feedback TEXT", "feedback_detail TEXT", "superseded_by TEXT", "event_key TEXT",
                "skills TEXT", "served_model TEXT", "tokens INTEGER", "started_ts REAL",
                "finished_ts REAL", "delivered_ts REAL", "coalesced INTEGER DEFAULT 0",
                "packet TEXT")
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
        # additive columns only (v0.117.0 -> requests; v0.118.0 -> proactive lanes);
        # existing rows keep their meaning (lane NULL = an ordinary helper unit)
        for col in ("requests INTEGER DEFAULT 1", *LANE_COLUMNS):
            try:
                self.store.conn.execute(f"ALTER TABLE helper_units ADD COLUMN {col}")
            except Exception:                                    # noqa: BLE001
                pass
        from .proactive import Proactive
        self.lanes = Proactive(self)

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
        h.setdefault("startup_grace_seconds", 180)  # idle with no reply -> startup failed
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
                            + (" AND status=?" if status else "") + " ORDER BY created_ts, rowid",
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
        ordinary = [x for x in self.units(pid, tid) if not x.get("lane")]
        if len(ordinary) >= int(h["max_units_per_task"]):
            self.store.event(pid, tid, "helper_limit", detail={
                "responsibility": resp, "source": source,
                "reason": f"max_units_per_task ({h['max_units_per_task']}) reached"})
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
        from . import lanes as L
        ls = L.settings(P)
        if ls["enabled"]:
            # lanes are optional: a lane error never stops ordinary helpers
            for step in ("ingest", "plan"):
                if step == "plan" and self.lanes.paused(P.id):
                    break
                try:
                    getattr(self.lanes, step)(P, t, spec, ls)
                except Exception as e:                           # noqa: BLE001
                    self.store.event(P.id, t["task_id"], "warning", detail={
                        "lanes": f"{step}: {type(e).__name__}: {e}"[:300]})
                t = self.store.task(P.id, t["task_id"]) or t
        self._adm_fail = {}          # persona -> admission transport failure, this tick
        for u in self.units(P.id, t["task_id"], "running"):
            try:
                self._poll(P, u, h)
            except Exception as e:                               # noqa: BLE001
                # one unreadable session never blocks the others; its timeout
                # still runs on the clock
                self._poll_failed(P, u, h, e)
        running = self.units(P.id, t["task_id"], "running")
        now = self.clock()
        waiting = [*self.units(P.id, t["task_id"], "pending"),
                   *self.units(P.id, t["task_id"], "deferred")]
        # dependency-aware priority queue (lanes.py); ordinary units keep
        # their arrival order at the default priority, so with lanes off the
        # order is exactly the v0.117 order
        for u in self.lanes.order(P, waiting, ls, now):
            if len(running) >= int(h["max_running"]):
                break
            if (u["next_check"] or 0) > now:
                continue
            if u.get("lane"):
                why = self.lanes.hold_reason(P, t, u, ls, running)
                if why:
                    if why.startswith("skip:"):
                        self._skip(P, u, why[5:])
                    continue
            if self._dispatch(P, t, spec, u, h):
                running = self.units(P.id, t["task_id"], "running")

    def _skip(self, P, u: dict, why: str, feedback: str = "") -> None:
        self.store.x("UPDATE helper_units SET status='skipped', reason=?, feedback=COALESCE(NULLIF(?,''),"
                     " feedback), updated_ts=? WHERE unit_id=?", (why[:900], feedback, self.clock(),
                                                                  u["unit_id"]))
        self.store.event(P.id, u["task_id"], "helper_skipped", detail={"unit": u["unit_id"],
                                                                       "reason": why[:300]})

    def _order(self, P, u: dict, h: dict) -> list[str]:
        order = list(h["order"]) or list(h["personas"])
        sel = set(h["selective"])
        return [b for b in order if b in h["personas"] and (u["value"] == "high" or b not in sel)]

    def _dispatch(self, P, t: dict, spec, u: dict, h: dict) -> bool:
        pid, tid = P.id, t["task_id"]
        reasons, nexts = [], []
        open_ended = False          # a backend that could clear at any time (UNKNOWN, error)
        cands = (self.lanes.candidates(P, u, h) if u.get("lane") else
                 [(b, h["personas"][b]) for b in self._order(P, u, h)])
        kcfg = None                 # Kilo's effective config, read once per dispatch
        for backend, persona in cands:
            # Kilo accepts prompt_async before resolving the model. A local
            # filter failure otherwise leaves only a user message, which looks
            # busy until the full helper timeout. Direct lanes bypass Kilo.
            check_model = getattr(P.client, 'model_config_problem', None)
            if u.get('mode') != 'direct' and callable(check_model):
                try:
                    read = getattr(P.client, 'effective_config', None)
                    if kcfg is None and callable(read):
                        kcfg = read(P.profile.workspace)
                    problem = (check_model(P.profile.workspace, persona, kcfg)
                               if kcfg is not None else check_model(P.profile.workspace, persona))
                except (RuntimeError, ValueError, OSError) as e:
                    problem = f"Kilo model preflight failed: {e}"
                if problem:
                    reasons.append(f"{backend}: {problem}")
                    open_ended = True
                    continue
            fail = getattr(self, "_adm_fail", {}).get(persona)
            if fail:
                reasons.append(f"{backend}: admission check failed ({fail}; not re-asked "
                               f"this tick)")
                open_ended = True
                continue
            try:
                adm = P.foundry.helper_admission(persona)
            except Exception as e:                               # noqa: BLE001
                why = str(e) if isinstance(e, FoundryError) else f"{type(e).__name__}: {e}"
                if hasattr(self, "_adm_fail"):
                    self._adm_fail[persona] = why[:200]
                reasons.append(f"{backend}: admission check failed ({why[:300]})")
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
            if u.get("lane"):
                why = self.lanes.admission_problem(P, u, adm)
                if why:
                    reasons.append(f"{backend}: {why}")
                    open_ended = True
                    continue
                if u.get("mode") == "direct":
                    return self.lanes.start_direct(P, t, spec, u, h, backend, persona, adm)
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
                                           "files": u["files"], "kind": u["kind"],
                                           **({"lane": u["lane"], "depth": u.get("depth") or 0,
                                               "parent_unit": u.get("parent_unit") or "",
                                               "snapshot": u.get("snapshot_id") or "",
                                               "artifact": u.get("artifact_id") or ""}
                                              if u.get("lane") else {})})
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
            text = self.lanes.prompt(P, spec, u) if u.get("lane") else self.prompt(spec, u)
            P.client.prompt(sid, text, directory=directory, persona=persona, agent=h["agent"])
            self.store.op_update(key, "done", {**res, "prompted": True})
        now = self.clock()
        self.store.x("UPDATE helper_units SET status='running', persona=?, backend=?, "
                     "session_id=?, parent_session=?, attempts=attempts+1, reason=?, "
                     "next_eligible=NULL, updated_ts=?, started_ts=? WHERE unit_id=?",
                     (persona, backend, sid, t["session_id"], f"admitted on {backend}", now, now,
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
        if u.get("mode") == "direct":
            return self.lanes.poll_direct(P, u, h)
        directory = P.profile.workspace
        sid = u["session_id"]
        now = self.clock()
        # one prompt per helper session: until Kilo answers it, the last
        # message is the prompt itself, which already reads as busy
        st = P.client.state(sid, directory=directory, since_ts=0)
        if st.status == "busy":
            if st.denial:
                # Foundry refused this optional call (e.g. quota cutoff) and Kilo is
                # retrying: stop now and defer, instead of holding it until timeout
                return self._defer_running(P, u, h, st.denial)
            grace = float(h.get("startup_grace_seconds") or 180)
            if getattr(st, "unanswered", False) and now - u["updated_ts"] > grace:
                self._stop(P, u, "failed",
                           f"no reply to the prompt after {int(grace)} s while Kilo reports "
                           "the session idle (startup failed: model, provider or auth); "
                           "not held until the timeout")
                return
            if now - u["updated_ts"] > float(h["timeout_minutes"]) * 60:
                self._stop(P, u, "failed", f"timed out after {h['timeout_minutes']} min")
            return
        if st.status == "attention":
            # a helper can't wait for a person; it ends without a result
            self._stop(P, u, "failed", "the helper asked for input; helpers can't wait")
            return
        if st.status in ("missing", "error"):
            self._finish(P, u, "failed", (st.error or st.status)[:300], None)
            return
        text = (st.final_text or "").strip()
        self._finish(P, u, "done" if text else "failed",
                     "proposal returned" if text else "no proposal text", text,
                     tokens=st.tokens)

    def _stop(self, P, u: dict, status: str, reason: str) -> bool:
        """Abort a running helper, then finish it. If Kilo does not confirm the
        abort, the unit stays running WITH its Foundry binding (its calls stay
        gated as optional work) and the abort is retried on the next poll."""
        ok = P.client.abort(u["session_id"], directory=P.profile.workspace)
        if ok is False:
            note = f"abort not confirmed by Kilo; retrying ({reason})"[:500]
            if u.get("reason") != note:
                self.store.event(P.id, u["task_id"], "helper_abort_unconfirmed",
                                 session_id=u["session_id"],
                                 detail={"unit": u["unit_id"], "reason": reason})
            self.store.x("UPDATE helper_units SET reason=? WHERE unit_id=?",
                         (note, u["unit_id"]))
            return False
        self._finish(P, u, status, reason, None)
        return True

    def _defer_running(self, P, u: dict, h: dict, denial: dict) -> None:
        """A running unit Foundry refused: abort it and queue it again for when
        the refusal says it may clear (a fresh session on the next dispatch)."""
        now = self.clock()
        retry = denial.get("retry_after")
        why = (f"Foundry refused the helper mid-run ({denial.get('category')}"
               f"{': ' + str(denial.get('code')) if denial.get('code') else ''})")
        if P.client.abort(u["session_id"], directory=P.profile.workspace) is False:
            self.store.x("UPDATE helper_units SET reason=? WHERE unit_id=?",
                         (f"{why}; abort not confirmed by Kilo, retrying"[:500], u["unit_id"]))
            return
        self.store.update_session(u["session_id"], status="ended", outcome="deferred",
                                  ended_ts=now)
        if P.foundry is not None:
            try:
                P.foundry.end(u["session_id"], "deferred")
            except FoundryError:
                pass
        # the next dispatch starts a new session (never the aborted one) without
        # using up one of the unit's re-requests
        self.store.x("DELETE FROM ops WHERE key=?",
                     (f"helper:{P.id}:{u['task_id']}:{u['unit_id']}:"
                      f"{int(u.get('requests') or 1)}",))
        nxt = now + float(retry) if retry else None
        self.store.x("UPDATE helper_units SET status='deferred', reason=?, next_eligible=?, "
                     "next_check=?, updated_ts=? WHERE unit_id=?",
                     (why[:500], nxt, max(now + float(h["poll_seconds"]), nxt or 0), now,
                      u["unit_id"]))
        self.store.event(P.id, u["task_id"], "helper_deferred", session_id=u["session_id"],
                         detail={"unit": u["unit_id"], "reason": why, "next_eligible": nxt})

    def _poll_failed(self, P, u: dict, h: dict, e: Exception) -> None:
        now = self.clock()
        why = f"status unreadable: {type(e).__name__}: {e}"[:300]
        if now - u["updated_ts"] > float(h["timeout_minutes"]) * 60:
            self._stop(P, u, "failed", f"timed out after {h['timeout_minutes']} min ({why})")
            return
        if u.get("reason") != why:
            self.store.event(P.id, u["task_id"], "warning", session_id=u.get("session_id"),
                             detail={"unit": u["unit_id"], "helper_poll": why})
            self.store.x("UPDATE helper_units SET reason=? WHERE unit_id=?", (why, u["unit_id"]))

    def _finish(self, P, u: dict, status: str, reason: str, text: Optional[str],
                tokens: Optional[dict] = None, meta: Optional[dict] = None) -> None:
        cap = int(self.settings(P)["max_result_chars"])
        if u.get("lane"):
            status, reason = self.lanes.on_finish(P, u, status, reason, text, tokens, meta or {})
        self.store.x("UPDATE helper_units SET status=?, reason=?, result=?, updated_ts=?, "
                     "finished_ts=? WHERE unit_id=?",
                     (status, reason, (text or "")[:cap] or None, self.clock(), self.clock(),
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
            if u.get("mode") == "direct":
                # one HTTP call already in flight can't be recalled; its result
                # (if it comes) is discarded, and the quota it used is spent
                self.lanes.forget_direct(u["unit_id"])
            else:
                try:
                    P.client.abort(u["session_id"], directory=P.profile.workspace)
                except Exception:                                # noqa: BLE001
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

    def proposals_for(self, pid: str, tid: str, session_id: str = "",
                      run_id: str = "") -> list[dict]:
        """Results the primary hasn't seen. Ordinary units: as in v0.117.
        Lane units: only actionable, current, same-run results within the
        delivery budget (lanes.deliverable); the rest stay visible in the
        control center but never interrupt the primary."""
        units = [u for u in self.units(pid, tid, "done") if not u["delivered_session"]]
        plain = [u for u in units if not u.get("lane")]
        lane = [u for u in units if u.get("lane")]
        return plain + (self.lanes.deliverable(pid, tid, lane, session_id, run_id) if lane
                        else [])

    def abort_lane_running(self, P, tid: str, reason: str) -> list[str]:
        """Stop running LANE units only (ordinary units keep their v0.117 hand-off)."""
        out = []
        for u in self.units(P.id, tid, "running"):
            if not u.get("lane"):
                continue
            if u.get("mode") == "direct":
                self.lanes.forget_direct(u["unit_id"])
            else:
                try:
                    P.client.abort(u["session_id"], directory=P.profile.workspace)
                except Exception:                                # noqa: BLE001
                    pass
            self._finish(P, u, "cancelled", reason, None)
            out.append(u["unit_id"])
        return out

    def integration_prompt(self, units: list[dict]) -> str:
        plain = [u for u in units if not u.get("lane")]
        lane = [u for u in units if u.get("lane")]
        out = ""
        if plain:
            body = "\n\n".join(f"### {u['unit_id']} — {u['kind']}: {u['responsibility']} "
                               f"(by {u['persona']})\n{u['result']}" for u in plain)
            out = ("## Helper proposals (advisory)\nRead-only helpers returned these proposals for "
                   "independent parts of this task. You decide what to integrate: apply only what "
                   "fits the approved scope, run the meaningful tests, and commit as usual. Don't "
                   "treat a proposal as verified. Mention which unit ids you integrated or "
                   "rejected in your report.\n\n" + body + "\n")
        if lane:
            out += ("\n" if out else "") + self.lanes.cards(lane)
        return out

    def mark_delivered(self, units: list[dict], session_id: str) -> None:
        for u in units:
            self.store.x("UPDATE helper_units SET status='delivered', delivered_session=?, "
                         "updated_ts=?, delivered_ts=?, feedback=CASE WHEN lane IS NULL THEN "
                         "feedback ELSE 'delivered' END WHERE unit_id=?",
                         (session_id, self.clock(), self.clock(), u["unit_id"]))
        self.lanes.note_delivery(units, session_id)

    def holding(self, pid: str, tid: str) -> list[dict]:
        """Running units an idle primary waits for (bounded by
        handoff_wait_minutes): ordinary units, and lanes whose result is
        likely to change what the primary does next. Advisory lanes (visual,
        research, preflight, handoff, evidence) never hold the primary."""
        return [u for u in self.units(pid, tid, "running")
                if not u.get("lane") or u["lane"] in ("failure_triage", "incremental_review",
                                                      "proposer", "test_design")]

    def record_feedback(self, P, t: dict, report: dict) -> list[str]:
        """The primary's own account of what it did with delivered lane results
        (report `helper_feedback`). Recorded as the primary's claim; it is not
        evidence that a fix works (the gate, review and Victor decide that)."""
        out = []
        for f in (report or {}).get("helper_feedback") or []:
            if not isinstance(f, dict):
                continue
            uid = str(f.get("unit") or "")
            st = str(f.get("status") or "").lower()
            if st not in ("integrated", "rejected", "deferred"):
                continue
            u = self.store.q("SELECT * FROM helper_units WHERE unit_id=? AND project_id=? AND "
                             "task_id=? AND lane IS NOT NULL AND status='delivered' AND "
                             "COALESCE(feedback,'') != ?", (uid, P.id, t["task_id"], st))
            if not u:
                continue
            note = str(f.get("note") or f.get("reason") or "")[:500]
            det = json.loads(u[0].get("feedback_detail") or "{}")
            det.update(note=note, claimed_by="primary", session=t["session_id"],
                       head=self._head(P))
            self.store.x("UPDATE helper_units SET feedback=?, feedback_detail=?, updated_ts=? "
                         "WHERE unit_id=?", (st, json.dumps(det), self.clock(), uid))
            self.store.event(P.id, t["task_id"], "helper_feedback", session_id=t["session_id"],
                             detail={"unit": uid, "status": st, "note": note[:200]})
            out.append(uid)
        return out

    @staticmethod
    def _head(P) -> str:
        try:
            return P.ws.head()
        except Exception:                                        # noqa: BLE001
            return ""

    def expire_lane_results(self, P, tid: str, reason: str) -> list[str]:
        """Leaving IMPLEMENTING: undelivered lane results are never delivered
        after verification or review (shown as expired)."""
        out = []
        for u in self.store.q("SELECT unit_id FROM helper_units WHERE project_id=? AND task_id=? "
                              "AND lane IS NOT NULL AND status='done' AND delivered_session IS NULL "
                              "AND COALESCE(feedback,'waiting') = 'waiting'",
                              (P.id, tid)):
            self.store.x("UPDATE helper_units SET feedback='expired', feedback_detail=?, "
                         "updated_ts=? WHERE unit_id=?",
                         (json.dumps({"note": reason}), self.clock(), u["unit_id"]))
            out.append(u["unit_id"])
        return out

    def authors(self, pid: str, tid: str) -> set[str]:
        """Personas whose proposals reached the primary (possible code authors)."""
        return {u["persona"] for u in self.units(pid, tid, "delivered") if u["persona"]}

    def child_purpose(self, P, title: str) -> str:
        """Purpose for a model-spawned child session (Kilo task tool), from its
        agent name. Unknown agents are optional exploration (never required)."""
        agent = child_agent(title)
        return self.settings(P)["child_purposes"].get(agent, "exploration") if agent \
            else "exploration"

    def supersede_artifact(self, P, tid: str, old_artifact: str, new_artifact: str) -> list[str]:
        """A newer generation of the same capture arrived: work on the old one
        never reaches the primary. Waiting units are superseded; a running one
        finishes (its quota is already spent) and its result is marked
        superseded, not delivered."""
        out = []
        for u in self.store.q("SELECT * FROM helper_units WHERE project_id=? AND task_id=? AND "
                              "artifact_id=?", (P.id, tid, old_artifact)):
            if u["status"] in ("pending", "deferred"):
                self.store.x("UPDATE helper_units SET status='skipped', feedback='superseded', "
                             "superseded_by=?, reason=?, updated_ts=? WHERE unit_id=?",
                             (new_artifact, f"superseded by newer capture {new_artifact}",
                              self.clock(), u["unit_id"]))
            elif u["status"] == "delivered":
                # lineage: the primary recaptured after this result reached it
                det = json.loads(u.get("feedback_detail") or "{}")
                det["recaptured_as"] = new_artifact
                self.store.x("UPDATE helper_units SET feedback_detail=?, updated_ts=? WHERE "
                             "unit_id=?", (json.dumps(det), self.clock(), u["unit_id"]))
                self.store.event(P.id, tid, "helper_recapture", detail={
                    "unit": u["unit_id"], "artifact": old_artifact, "recapture": new_artifact,
                    "feedback": u.get("feedback")})
                continue
            elif u["status"] in ("running", "done"):
                self.store.x("UPDATE helper_units SET feedback='superseded', superseded_by=?, "
                             "updated_ts=? WHERE unit_id=?", (new_artifact, self.clock(),
                                                              u["unit_id"]))
            else:
                continue
            self.store.event(P.id, tid, "helper_superseded", detail={
                "unit": u["unit_id"], "artifact": old_artifact, "by": new_artifact})
            out.append(u["unit_id"])
        return out

    def snapshot(self, pid: str, tid: str) -> list[dict]:
        return [{k: u.get(k) for k in ("unit_id", "kind", "responsibility", "value", "source",
                                       "status", "persona", "backend", "session_id",
                                       "parent_session", "reason", "next_eligible", "attempts",
                                       "delivered_session", "updated_ts")}
                for u in self.units(pid, tid)]

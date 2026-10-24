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
from .adapters.foundry import FoundryError, admitted_routes, pinned_meta
from .store import add_column

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
                "packet TEXT", "coalesce_key TEXT")
_SUBAGENT_TITLE = re.compile(r"\(@([\w.-]+) subagent\)")

# v0.122.0 status vocabulary (additive; `status` keeps its v0.117 meaning).
# phase: what the unit is doing now, for the bridge and the UI. reason_code: WHY
# it is in that phase, so a terminal `failed` is never ambiguous.
STATUS_COLUMNS = ("phase TEXT", "reason_code TEXT", "investigation_key TEXT")
# revamp B5: who consumes a unit's result (declared when the unit is created), and
# whether a commit of that consumer used it (measured from git, never self-report):
# git_used 1 = a commit after delivery touched a path of the proposal, 0 = the
# consumer finished without one, NULL = not measured yet
CONSUMER_COLUMNS = ("consumer TEXT", "git_used INTEGER", "git_detail TEXT")
PHASES = ("queued", "admitted", "executing", "waiting_provider", "timed_out", "completed",
          "cancelled", "error")
PHASE_LABELS = {"queued": "queued", "admitted": "admitted (starting)",
                "executing": "executing", "waiting_provider": "waiting on provider",
                "timed_out": "timed out", "completed": "completed", "cancelled": "cancelled",
                "error": "error"}
REASON_CODES = {
    # failed / timed_out (terminal)
    "startup_failed": "no reply to the prompt within the startup grace",
    "timeout": "execution timed out",
    "poll_unreadable": "session status unreadable until the timeout",
    "handoff_timeout": "the idle primary stopped waiting (handoff wait over)",
    "needs_input": "the helper asked for input; helpers can't wait",
    "session_missing": "Kilo has no such session",
    "provider_error": "the provider or Kilo returned an error",
    "empty_result": "the session ended without proposal text",
    "snapshot_unavailable": "the assignment's snapshot could not be read",
    "interrupted": "the coordinator restarted mid-call; result unknown, not repeated",
    "error": "failed (unclassified)",
    # cancelled / skipped (terminal)
    "primary_ready": "the primary was ready before this unit was admitted",
    "primary_finished": "the primary finished first",
    "task_paused": "task paused",
    "task_cancelled": "task cancelled",
    "session_ended": "the primary session ended",
    "superseded": "superseded by newer work",
    "skipped": "skipped",
    "stopped": "stopped by the coordinator",
    # deferred / waiting (not terminal)
    "denied_deferred": "Foundry refused the helper mid-run; queued again",
    "admission_denied": "Foundry did not admit optional work now (quota/window)",
    "admission_unavailable": "the admission check failed",
    "binding_failed": "Foundry binding failed",
    "provider_retry": "Kilo is retrying the provider (no Foundry denial)",
    "capacity_full": "helper capacity is full",
    "model_config": "the Kilo model preflight failed",
    "local_route": "the persona routes to the local backend",
    "lane_admission": "the lane's admission requirements are not met",
    "no_persona": "no helper persona configured",
    # running (not terminal)
    "abort_unconfirmed": "abort not confirmed by Kilo; retried on the next poll",
}
# phases each stored status may legitimately carry; anything else is stale
# (a row written by another module) and is re-derived
_PHASES_FOR = {"pending": ("queued",), "deferred": ("queued", "waiting_provider"),
               "running": ("admitted", "executing", "waiting_provider"),
               "done": ("completed",), "delivered": ("completed",),
               "failed": ("timed_out", "error"), "timed_out": ("timed_out",),
               "skipped": ("cancelled",), "cancelled": ("cancelled",)}
_TIMEOUT_CODES = ("startup_failed", "timeout", "poll_unreadable", "handoff_timeout")
_WAITING_CODES = ("denied_deferred", "admission_denied", "admission_unavailable",
                  "binding_failed", "provider_retry")
# a duplicate investigation is matched against units that are live or answered
DUPLICATE_STATUSES = ("pending", "deferred", "running", "done", "delivered")
DISPOSITIONS = ("integrated", "rejected", "superseded", "pending", "expired", "display_only")


def child_agent(title: str) -> str:
    """Kilo names a task-tool child session '<description> (@<agent> subagent)'."""
    m = _SUBAGENT_TITLE.search(title or "")
    return m.group(1) if m else ""


def infer_reason_code(status: str, reason: str) -> Optional[str]:
    """Best reason code for a row whose writer set none (older rows, other
    modules). Never returns None for a terminal failure."""
    r = (reason or "").lower()
    if status in ("failed", "timed_out"):
        if "no reply to the prompt" in r or "startup failed" in r:
            return "startup_failed"
        if "status unreadable" in r:
            return "poll_unreadable"
        if status == "timed_out":
            return "handoff_timeout"
        if "timed out" in r:
            return "timeout"
        if "asked for input" in r:
            return "needs_input"
        if "no such session" in r or r.strip() == "missing":
            return "session_missing"
        if "no proposal text" in r:
            return "empty_result"
        if "snapshot unavailable" in r:
            return "snapshot_unavailable"
        if "restarted" in r:
            return "interrupted"
        if "failed" in r or "error" in r:
            return "provider_error"
        return "error"
    if status in ("skipped", "cancelled"):
        if "supersede" in r:
            return "superseded"
        if "task paused" in r:
            return "task_paused"
        if "task cancelled" in r:
            return "task_cancelled"
        if "primary finished" in r:
            return "primary_finished"
        if "primary was ready" in r or "before this unit was" in r:
            return "primary_ready"
        if "session ended" in r:
            return "session_ended"
        return "skipped" if status == "skipped" else "stopped"
    if status == "deferred":
        if "refused" in r and "mid-run" in r:
            return "denied_deferred"
        if "binding failed" in r:
            return "binding_failed"
        if "refused" in r or "quota" in r or "denied" in r or \
                re.search(r": [A-Z_]+ — ", reason or ""):      # _dispatch: "<b>: STATE — why"
            return "admission_denied"
        if "admission check failed" in r:
            return "admission_unavailable"
        if "capacity" in r:
            return "capacity_full"
        if "preflight" in r:
            return "model_config"
        if "local backend" in r:
            return "local_route"
        if "no helper persona" in r:
            return "no_persona"
    return None


def _phase_for(status: str, reason_code: Optional[str], u: Optional[dict] = None) -> str:
    """The phase a status implies (when the row carries no consistent phase)."""
    if status == "pending":
        return "queued"
    if status == "deferred":
        if reason_code in _WAITING_CODES or (u or {}).get("next_eligible"):
            return "waiting_provider"
        return "queued"
    if status == "running":
        return "waiting_provider" if reason_code == "provider_retry" else "executing"
    if status in ("done", "delivered"):
        return "completed"
    if status == "timed_out":
        return "timed_out"
    if status == "failed":
        return "timed_out" if reason_code in _TIMEOUT_CODES else "error"
    if status in ("skipped", "cancelled"):
        return "cancelled"
    return "queued"


_RUNNING_CODES = (None, "", "provider_retry", "abort_unconfirmed", "poll_unreadable")


def _consistent(u: dict) -> bool:
    """Does the stored phase/reason_code still describe the stored status?"""
    status, phase, code = str(u.get("status") or ""), u.get("phase"), u.get("reason_code")
    if phase not in _PHASES_FOR.get(status, ()):
        return False
    if status == "running":
        return code in _RUNNING_CODES and \
            (phase != "waiting_provider" or code == "provider_retry")
    if status in ("pending", "done", "delivered"):
        return not code
    if status in ("failed", "timed_out", "skipped", "cancelled"):
        return bool(code) and code not in _RUNNING_CODES and code not in _WAITING_CODES
    if status == "deferred":
        return code not in ("abort_unconfirmed", "provider_retry", "poll_unreadable")
    return True


def disposition(u: dict) -> Optional[str]:
    """What happened to a unit's result: integrated | rejected | superseded |
    pending (answered, no claim yet) | expired | display_only. None while the
    unit has no result."""
    fb = u.get("feedback")
    if fb in ("integrated", "rejected", "superseded", "expired", "display_only"):
        return fb
    if u.get("status") in ("done", "delivered"):
        return "pending"
    return None


def unit_status(u: dict) -> dict:
    """{phase, reason_code, disposition, label} for one helper_units row. The
    stored phase is used when it agrees with the stored status; a row written
    by a module that sets only `status` is re-derived, so this never lies."""
    status = str(u.get("status") or "pending")
    if _consistent(u):
        phase, code = u["phase"], u.get("reason_code") or None
    else:
        code = infer_reason_code(status, u.get("reason") or "")
        phase = _phase_for(status, code, u)
    disp = disposition(u)
    label = PHASE_LABELS.get(phase, phase) + (f": {code}" if code else "")
    if disp:
        label += f" ({disp})"
    return {"phase": phase, "reason_code": code, "disposition": disp, "label": label}


status_phase = unit_status


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _norm_files(files) -> list[str]:
    out = set()
    for f in files or []:
        f = str(f).strip().replace("\\", "/").lower()
        while f.startswith("./"):
            f = f[2:]
        if f:
            out.add(f)
    return sorted(out)


def investigation_key(responsibility: str, files, lane: Optional[str] = None) -> str:
    """Cross-kind identity of an investigation: normalized responsibility plus
    the normalized file set. A lane's responsibility is '<lane>:<key>' (maybe
    '~<unit>' suffixed); the lane prefix and suffix are not part of it."""
    resp = str(responsibility or "")
    if lane and resp.startswith(f"{lane}:"):
        resp = resp[len(lane) + 1:]
    resp = re.sub(r"~hu-[0-9a-f]+$", "", resp)
    body = json.dumps([_norm_text(resp), _norm_files(files)])
    return hashlib.sha1(body.encode()).hexdigest()[:20]


class HelperManager:
    def __init__(self, coordinator, clock: Callable[[], float] = time.time):
        self.c = coordinator
        self.store = coordinator.store
        self.clock = clock
        self.store.conn.executescript(SCHEMA)
        # additive columns only (v0.117.0 -> requests; v0.118.0 -> proactive lanes);
        # existing rows keep their meaning (lane NULL = an ordinary helper unit)
        # v0.122.0 -> phase / reason_code / investigation_key (status unchanged)
        for col in ("requests INTEGER DEFAULT 1", *LANE_COLUMNS, *STATUS_COLUMNS,
                    *CONSUMER_COLUMNS):
            add_column(self.store.conn, "helper_units", col)
        self._backfill_status()
        from .proactive import Proactive
        self.lanes = Proactive(self)

    def _backfill_status(self, where: str = "phase IS NULL", args: tuple = ()) -> int:
        """Rows from before v0.122.0 (or written by a module that sets only
        `status`) get a derived phase/reason_code; never touches updated_ts."""
        n = 0
        for u in self.store.q(f"SELECT * FROM helper_units WHERE {where}", args):
            s = unit_status(u)
            self.store.x("UPDATE helper_units SET phase=?, reason_code=? WHERE unit_id=?",
                         (s["phase"], s["reason_code"], u["unit_id"]))
            n += 1
        return n

    def _mark(self, uid: str, phase: str, reason_code: Optional[str]) -> None:
        """Record phase + reason_code (never updated_ts: timeouts run on it)."""
        self.store.x("UPDATE helper_units SET phase=?, reason_code=? WHERE unit_id=? AND "
                     "(COALESCE(phase,'') != ? OR COALESCE(reason_code,'') != ?)",
                     (phase, reason_code, uid, phase, reason_code or ""))

    def sync_status(self, pid: str, tid: str) -> int:
        """Re-derive phase/reason_code for rows whose stored phase disagrees
        with their status (lane/visual/advisory code writes `status` only)."""
        n = 0
        for u in self.store.q("SELECT * FROM helper_units WHERE project_id=? AND task_id=?",
                              (pid, tid)):
            if _consistent(u):
                continue
            s = unit_status(u)
            self.store.x("UPDATE helper_units SET phase=?, reason_code=? WHERE unit_id=?",
                         (s["phase"], s["reason_code"], u["unit_id"]))
            n += 1
        return n

    @staticmethod
    def unit_status(u: dict) -> dict:
        return unit_status(u)

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

    def _add(self, pid, tid, run_id, u: dict, source: str, h: dict,
             head=None, consumer: str = "") -> Optional[str]:
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
                files = [str(f) for f in (u.get("files") or [])][:30]
                self.store.x("UPDATE helper_units SET status='pending', kind=?, prompt=?, "
                             "files=?, run_id=?, requests=COALESCE(requests,1)+1, next_check=0, "
                             "reason=?, session_id=NULL, updated_ts=?, phase='queued', "
                             "reason_code=NULL, investigation_key=? WHERE unit_id=?",
                             (kind, str(u.get("prompt") or "")[:6000],
                              json.dumps(files), run_id,
                              f"requested again (was {p['status']}: {p['reason'] or ''})"[:500],
                              now, investigation_key(resp, files), p["unit_id"]))
                self.store.event(pid, tid, "helper_unit", detail={"unit": p["unit_id"],
                                                                  "requested_again": True})
                return p["unit_id"]
            return None                       # one unit per responsibility
        files = [str(f) for f in (u.get("files") or [])][:30]
        ikey = investigation_key(resp, files)
        dup = self.duplicate_of(pid, tid, run_id, resp, files, head=head)
        if dup:
            self._coalesce_duplicate(pid, tid, dup, resp, kind, source, ikey)
            return None
        ordinary = [x for x in self.units(pid, tid) if not x.get("lane")]
        if len(ordinary) >= int(h["max_units_per_task"]):
            self.store.event(pid, tid, "helper_limit", detail={
                "responsibility": resp, "source": source,
                "reason": f"max_units_per_task ({h['max_units_per_task']}) reached"})
            return None
        uid = f"hu-{uuid.uuid4().hex[:10]}"
        n = self.store.x("INSERT OR IGNORE INTO helper_units (unit_id, project_id, task_id, run_id, "
                         "responsibility, kind, value, prompt, files, source, status, created_ts, "
                         "updated_ts, next_check, phase, investigation_key) VALUES "
                         "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (uid, pid, tid, run_id, resp, kind,
                          "high" if u.get("value") == "high" else "normal",
                          str(u.get("prompt") or "")[:6000],
                          json.dumps(files), source,
                          "pending", now, now, 0, "queued", ikey))
        if n:
            self.store.x("UPDATE helper_units SET consumer=? WHERE unit_id=?",
                         ((consumer or "primary (task-declared)")[:300], uid))
            self.store.event(pid, tid, "helper_unit", detail={"unit": uid, "kind": kind,
                                                              "responsibility": resp,
                                                              "source": source})
        return uid

    def duplicate_of(self, pid: str, tid: str, run_id: str, responsibility: str, files,
                     lane: Optional[str] = None, snapshot_id: str = "",
                     head=None) -> Optional[dict]:
        """A live or answered unit of this run, of ANY kind (ordinary or lane),
        that investigates the same thing: same normalized responsibility and
        the same normalized file set. Rejected/superseded/expired results
        don't count; failed/skipped/cancelled/timed-out units produced nothing
        to reuse. An ANSWERED (done/delivered) unit only counts when its result
        is still about the requester's code: a lane result proactive would
        reuse (structured, its own assignment, no base mismatch) of the SAME
        snapshot_id, or an ordinary result recorded at the SAME head (`head`:
        a str, or a callable read only when needed). When that can't be
        determined, nothing is coalesced onto an answered unit."""
        key = investigation_key(responsibility, files, lane)
        head_cache: list = []

        def cur_head() -> str:
            if not head_cache:
                try:
                    head_cache.append(str((head() if callable(head) else head) or ""))
                except Exception:                                # noqa: BLE001
                    head_cache.append("")
            return head_cache[0]

        for x in self.store.q("SELECT * FROM helper_units WHERE project_id=? AND task_id=? AND "
                              "run_id IS ? ORDER BY created_ts, rowid", (pid, tid, run_id)):
            if x["status"] not in DUPLICATE_STATUSES or \
                    x.get("feedback") in ("rejected", "superseded", "expired"):
                continue
            if x["status"] in ("done", "delivered") and not self._answer_reusable(
                    x, snapshot_id, cur_head):
                continue
            xfiles = json.loads(x.get("files") or "[]")
            if x.get("lane"):
                resp = x.get("coalesce_key") or x["responsibility"]
                xkey = investigation_key(resp, xfiles, x["lane"])
            else:
                xkey = x.get("investigation_key") or investigation_key(x["responsibility"],
                                                                       xfiles)
            if xkey == key:
                return x
        return None

    @staticmethod
    def _answer_reusable(x: dict, snapshot_id: str, cur_head: Callable[[], str]) -> bool:
        """May a new request be coalesced onto this done/delivered unit?"""
        if x.get("lane"):
            try:
                env = json.loads(x.get("envelope") or "{}")
            except (TypeError, ValueError):
                return False
            if not isinstance(env, dict):
                return False
            mism = (env.get("checks") or {}).get("base_mismatch")
            if mism and x.get("lane") == "visual":
                try:
                    from .visual import visual_identity_mismatch
                    if not visual_identity_mismatch(x, env):
                        mism = None      # pre-v0.121.7 row: base == artifact id was compliant
                except Exception:                                # noqa: BLE001
                    pass
            if not (env.get("structured") and env.get("assignment") == x["unit_id"]
                    and not mism):
                return False             # proactive's usable() would refuse it too
            return bool(snapshot_id) and x.get("snapshot_id") == snapshot_id
        try:
            det = json.loads(x.get("feedback_detail") or "{}")
        except (TypeError, ValueError):
            det = {}
        at = (det.get("head") if isinstance(det, dict) else None) or ""
        if not at:
            return False                 # the result's head is unknown: don't reuse it
        h = cur_head()
        return bool(h) and h == at

    def _coalesce_duplicate(self, pid, tid, dup: dict, resp: str, kind: str, source: str,
                            ikey: str) -> None:
        """Coalesce (never dispatch) a cross-kind duplicate, once per request:
        a task-declared duplicate is re-collected every tick and must not
        inflate the existing unit's coalesced count. The once-marker is an ops
        row (primary key lookup), not a LIKE scan over the events table."""
        mark = hashlib.sha1(f"{dup['unit_id']}|{source}|{kind}|{resp}".encode()).hexdigest()[:16]
        if self.store.op_begin(f"helper_duplicate:{pid}:{tid}:{mark}", pid, tid,
                               "helper_duplicate") is not None:
            return
        self.store.x("UPDATE helper_units SET coalesced=COALESCE(coalesced,0)+1 WHERE unit_id=?",
                     (dup["unit_id"],))
        why = (f"same investigation as {dup['unit_id']} ({dup.get('lane') or dup['kind']}, "
               f"{dup['status']}): normalized responsibility and files match")
        self.store.event(pid, tid, "helper_duplicate", detail={
            "unit": dup["unit_id"], "existing_kind": dup["kind"], "existing_lane": dup.get("lane"),
            "existing_status": dup["status"], "requested_kind": kind, "responsibility": resp,
            "source": source, "investigation_key": ikey, "reason": why, "dedupe": mark})
        self.store.op_update(f"helper_duplicate:{pid}:{tid}:{mark}", "done",
                             {"unit": dup["unit_id"]})

    def collect(self, P, t: dict, spec) -> None:
        """Units declared by the task source (one per responsibility)."""
        h = self.settings(P)
        for u in (spec.data or {}).get("helpers") or []:
            if isinstance(u, dict):
                self._add(P.id, t["task_id"], t["run_id"], u, "task", h,
                          head=lambda: self._head(P))

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
            use = str(u.get("consumer") or u.get("use") or "").strip()
            uid = self._add(P.id, t["task_id"], t["run_id"], {
                "responsibility": u.get("responsibility"), "kind": kind,
                "prompt": u.get("prompt") or u.get("question") or "", "files": files,
                "value": "normal"}, "report", h, head=lambda: self._head(P),
                consumer=f"primary:{t.get('session_id') or ''}" + (f" ({use[:200]})"
                                                                   if use else ""))
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
        # (a [fast_lane] task runs only its configured lanes: lanes.settings)
        ls = L.settings(P, t["task_id"])
        # an operator's project pause: running units are still polled (they
        # finish, or time out), but nothing new is planned or dispatched
        is_held = getattr(self.c, "_held", None)
        held = bool(is_held(P, None, "helper dispatch")) if callable(is_held) else False
        if ls["enabled"]:
            # lanes are optional: a lane error never stops ordinary helpers
            for step in ("ingest", "plan"):
                if step == "plan" and (self.lanes.paused(P.id) or held):
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
        waiting = [] if held else [*self.units(P.id, t["task_id"], "pending"),
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
        # rows written by lane/visual/advisory code carry status only
        self.sync_status(P.id, t["task_id"])

    def _skip(self, P, u: dict, why: str, feedback: str = "") -> None:
        code = "superseded" if feedback == "superseded" else             (infer_reason_code("skipped", why) or "skipped")
        self.store.x("UPDATE helper_units SET status='skipped', reason=?, feedback=COALESCE(NULLIF(?,''),"
                     " feedback), updated_ts=?, phase='cancelled', reason_code=? WHERE unit_id=?",
                     (why[:900], feedback, self.clock(), code, u["unit_id"]))
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
        codes = set()               # reason codes seen, most specific wins below
        cands = (self.lanes.candidates(P, u, h) if u.get("lane") else
                 [(b, h["personas"][b]) for b in self._order(P, u, h)])
        kcfg = None                 # Kilo's effective config, read once per dispatch
        for backend, persona in cands:
            cap = (h.get('backend_max_running') or {}).get(backend)
            if cap is not None and sum(u['backend'] == backend for u in
                    self.units(pid,tid,'running')) >= int(cap):
                reasons.append(f'{backend}: coordinator backend helper capacity is full')
                codes.add("capacity_full")
                open_ended = True
                continue
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
                    codes.add("model_config")
                    open_ended = True
                    continue
            fail = getattr(self, "_adm_fail", {}).get(persona)
            if fail:
                reasons.append(f"{backend}: admission check failed ({fail}; not re-asked "
                               f"this tick)")
                codes.add("admission_unavailable")
                open_ended = True
                continue
            try:
                need = self._admission_needs(P, u)
                adm = (P.foundry.helper_admission(persona, **need) if need
                       else P.foundry.helper_admission(persona))
            except Exception as e:                               # noqa: BLE001
                why = str(e) if isinstance(e, FoundryError) else f"{type(e).__name__}: {e}"
                if hasattr(self, "_adm_fail"):
                    self._adm_fail[persona] = why[:200]
                reasons.append(f"{backend}: admission check failed ({why[:300]})")
                codes.add("admission_unavailable")
                open_ended = True
                continue
            # the route that will serve: the pinned fallback when Foundry pinned one
            if any(r.get("source") == "local" for r in admitted_routes(adm)[:1]):
                reasons.append(f"{backend}: {persona} routes to the local backend, which the "
                               f"primary holds; helpers use frontier backends only")
                codes.add("local_route")
                continue
            if not adm.get("admit"):
                reasons.append(f"{backend}: {adm.get('state', '?').upper()} — {adm.get('reason')}")
                codes.add("admission_denied")
                if adm.get("next_eligible"):
                    nexts.append(float(adm["next_eligible"]))
                else:
                    open_ended = True
                continue
            if u.get("lane"):
                why = self.lanes.admission_problem(P, u, adm)
                if why:
                    reasons.append(f"{backend}: {why}")
                    codes.add("lane_admission")
                    open_ended = True
                    continue
                if u.get("mode") == "direct":
                    return self.lanes.start_direct(P, t, spec, u, h, backend, persona, adm)
            return self._start(P, t, spec, u, h, backend, persona, adm)
        if not reasons:
            reasons.append("no helper persona configured for any allowed backend "
                           "([helpers] personas)")
            codes.add("no_persona")
        now = self.clock()
        nxt = None if open_ended or not nexts else min(nexts)
        check = max(now + float(h["poll_seconds"]), nxt or 0)
        reason = "; ".join(reasons)[:900]
        if u["status"] != "deferred" or u["reason"] != reason:
            self.store.event(pid, tid, "helper_deferred", detail={"unit": u["unit_id"],
                                                                  "reason": reason,
                                                                  "next_eligible": nxt})
        code = next((c for c in ("admission_denied", "admission_unavailable", "model_config",
                                 "capacity_full", "local_route", "lane_admission", "no_persona")
                     if c in codes), "admission_denied")
        phase = "waiting_provider" if code in _WAITING_CODES else "queued"
        self.store.x("UPDATE helper_units SET status='deferred', reason=?, next_eligible=?, "
                     "next_check=?, updated_ts=?, phase=?, reason_code=? WHERE unit_id=?",
                     (reason, nxt, check, now, phase, code, u["unit_id"]))
        return False

    def _admission_needs(self, P, u: dict) -> dict:
        """The helper's route requirements for Foundry's admission (so a later
        chain route can be admitted and pinned when the first is not): image
        input and the approved models for the visual lane. {} = none."""
        if u.get("lane") != "visual":
            return {}
        try:
            from . import lanes as L
            approved = list(L.settings(P)["lanes"]["visual"].get("approved_models") or [])
        except Exception:                                        # noqa: BLE001
            approved = []
        return {"vision": True, "models": approved, "purpose": u.get("kind") or ""}

    def _start(self, P, t: dict, spec, u: dict, h: dict, backend: str, persona: str,
               adm: Optional[dict] = None) -> bool:
        pid, tid = P.id, t["task_id"]
        pin = pinned_meta(adm)
        # one operation per request of the unit (a re-request is a new session)
        key = f"helper:{pid}:{tid}:{u['unit_id']}:{int(u.get('requests') or 1)}"
        prior = self.store.op_begin(key, pid, tid, "helper")
        res = dict((prior or {}).get("result") or {})
        sid = res.get("session_id")
        directory = P.profile.workspace
        # every helper title carries this marker, so a create whose response was
        # lost is found again instead of leaving an orphan and creating a twin
        marker = res.get("marker") or (f"[{u['unit_id']}#{int(u.get('requests') or 1)}."
                                         f"{uuid.uuid4().hex[:4]}]")
        if not sid and res.get("creating"):
            find = getattr(P.client, "find_session", None)
            found = find(directory, marker) if callable(find) else ""
            if found:
                sid = found
                res = {"session_id": sid, "reconciled": "create", "marker": marker}
                self.store.op_update(key, "started", res)
                self.store.add_session(sid, pid, tid, t["run_id"], "helper", "helper",
                                       t["attempt"], P.client.name, persona,
                                       {"parent": t["session_id"], "unit": u["unit_id"],
                                        "purpose": u["kind"]})
                self.store.event(pid, tid, "helper_reconciled", session_id=sid,
                                 detail={"unit": u["unit_id"], "what": "session create "
                                         "response was lost; existing session adopted"})
        if not sid:
            self.store.op_update(key, "started", {**res, "creating": True, "marker": marker})
            sid = P.client.start_session(
                directory=directory, title=f"{marker} {pid} {tid} helper {u['kind']}: "
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
                               parent=t["session_id"] or "", meta=pin or None,
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
                             "next_check=?, updated_ts=?, phase='waiting_provider', "
                             "reason_code='binding_failed' WHERE unit_id=?",
                             (f"binding failed: {e}"[:500], self.clock() + float(h["poll_seconds"]),
                              self.clock(), u["unit_id"]))
                self.store.x("DELETE FROM ops WHERE key=?", (key,))
                return False
            res["bound"] = True
            self.store.op_update(key, "bound", res)
        if not res.get("prompted") and res.get("prompting"):
            # the last send raised: Kilo may still have accepted it. Ask Kilo,
            # never replay blind (no way to ask -> treat as sent)
            check = getattr(P.client, "has_prompt", None)
            if not callable(check) or check(sid, directory=directory):
                res = {**res, "prompted": True, "reconciled": "prompt"}
                self.store.op_update(key, "done", res)
                self.store.event(pid, tid, "helper_reconciled", session_id=sid,
                                 detail={"unit": u["unit_id"], "what": "prompt response was "
                                         "lost; Kilo has the prompt, not resent"})
        if not res.get("prompted"):
            text = self.lanes.prompt(P, spec, u) if u.get("lane") else self.prompt(spec, u)
            text += self._playbook_text(P, spec, u, sid)
            self.store.op_update(key, "bound", {**res, "prompting": True})
            P.client.prompt(sid, text, directory=directory, persona=persona, agent=h["agent"])
            self.store.op_update(key, "done", {**res, "prompted": True})
        now = self.clock()
        self.store.x("UPDATE helper_units SET status='running', persona=?, backend=?, "
                     "session_id=?, parent_session=?, attempts=attempts+1, reason=?, "
                     "next_eligible=NULL, updated_ts=?, started_ts=?, phase='admitted', "
                     "reason_code=NULL WHERE unit_id=?",
                     (persona, backend, sid, t["session_id"], f"admitted on {backend}" + (
                         f" (pinned to {pin['pinned_route']['backend']}/"
                         f"{pin['pinned_route']['model']}: {(adm or {}).get('reason')})"[:600]
                         if pin else ""), now, now, u["unit_id"]))
        self.store.event(pid, tid, "helper_started", session_id=sid,
                         detail={"unit": u["unit_id"], "kind": u["kind"], "persona": persona,
                                 "backend": backend, "parent": t["session_id"],
                                 **({"pinned_route": pin["pinned_route"]} if pin else {})})
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

    def _playbook_text(self, P, spec, u: dict, sid: str) -> str:
        """The playbook slice for a helper prompt (A1: all lanes get it; helpers
        read no docs/memory slice, so the full shared budget is free). Never
        raises; a helper is read-only, so its render still says notes go in the
        final report (the primary decides what to keep)."""
        pb = getattr(self.c, "playbook", None)
        if pb is None:
            return ""
        try:
            text = pb.prompt_block(P, spec.task_id, sid, spec=spec,
                                   role=str(u.get("kind") or "helper"))
        except Exception as e:                                    # noqa: BLE001
            (getattr(self.c, "log", None) or (lambda m: None))(
                f"{P.id}/{spec.task_id}: helper playbook slice failed: {e}")
            return ""
        return ("\n\n" + text.strip("\n") + "\n") if text.strip() else ""

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
            unanswered = bool(getattr(st, "unanswered", False))
            # Kilo's own provider retry (429/5xx) with no Foundry denial: the
            # session reports busy with the retry message and no compaction
            retrying = bool(getattr(st, "retry", False)) or (
                bool(st.error) and not unanswered and not getattr(st, "compaction", None))
            if u.get("reason_code") != "abort_unconfirmed":
                if retrying:
                    self._mark(u["unit_id"], "waiting_provider", "provider_retry")
                else:
                    self._mark(u["unit_id"], "admitted" if unanswered else "executing", None)
            if unanswered and now - u["updated_ts"] > grace:
                self._stop(P, u, "failed",
                           f"no reply to the prompt after {int(grace)} s while Kilo reports "
                           "the session idle (startup failed: model, provider or auth); "
                           "not held until the timeout", "startup_failed")
                return
            if now - u["updated_ts"] > float(h["timeout_minutes"]) * 60:
                self._stop(P, u, "failed", f"timed out after {h['timeout_minutes']} min",
                           "timeout")
            return
        if st.status == "attention":
            # a helper can't wait for a person; it ends without a result
            self._stop(P, u, "failed", "the helper asked for input; helpers can't wait",
                       "needs_input")
            return
        if st.status in ("missing", "error"):
            self._finish(P, u, "failed", (st.error or st.status)[:300], None,
                         reason_code="session_missing" if st.status == "missing"
                         else "provider_error")
            return
        text = (st.final_text or "").strip()
        self._finish(P, u, "done" if text else "failed",
                     "proposal returned" if text else "no proposal text", text,
                     tokens=st.tokens, reason_code=None if text else "empty_result")

    def _stop(self, P, u: dict, status: str, reason: str,
              reason_code: Optional[str] = None) -> bool:
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
            self.store.x("UPDATE helper_units SET reason=?, reason_code='abort_unconfirmed' "
                         "WHERE unit_id=?", (note, u["unit_id"]))
            return False
        self._finish(P, u, status, reason, None, reason_code=reason_code)
        return True

    def _defer_running(self, P, u: dict, h: dict, denial: dict) -> None:
        """A running unit Foundry refused: abort it and queue it again for when
        the refusal says it may clear (a fresh session on the next dispatch)."""
        now = self.clock()
        retry = denial.get("retry_after")
        why = (f"Foundry refused the helper mid-run ({denial.get('category')}"
               f"{': ' + str(denial.get('code')) if denial.get('code') else ''})")
        if P.client.abort(u["session_id"], directory=P.profile.workspace) is False:
            self.store.x("UPDATE helper_units SET reason=?, reason_code='abort_unconfirmed' "
                         "WHERE unit_id=?",
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
                     "next_check=?, updated_ts=?, phase='waiting_provider', "
                     "reason_code='denied_deferred' WHERE unit_id=?",
                     (why[:500], nxt, max(now + float(h["poll_seconds"]), nxt or 0), now,
                      u["unit_id"]))
        self.store.event(P.id, u["task_id"], "helper_deferred", session_id=u["session_id"],
                         detail={"unit": u["unit_id"], "reason": why, "next_eligible": nxt})

    def _poll_failed(self, P, u: dict, h: dict, e: Exception) -> None:
        now = self.clock()
        why = f"status unreadable: {type(e).__name__}: {e}"[:300]
        if now - u["updated_ts"] > float(h["timeout_minutes"]) * 60:
            self._stop(P, u, "failed", f"timed out after {h['timeout_minutes']} min ({why})",
                       "poll_unreadable")
            return
        if u.get("reason") != why:
            self.store.event(P.id, u["task_id"], "warning", session_id=u.get("session_id"),
                             detail={"unit": u["unit_id"], "helper_poll": why})
            self.store.x("UPDATE helper_units SET reason=? WHERE unit_id=?", (why, u["unit_id"]))
        if u.get("reason_code") != "abort_unconfirmed":
            self.store.x("UPDATE helper_units SET reason_code='poll_unreadable' WHERE unit_id=?",
                         (u["unit_id"],))

    def _finish(self, P, u: dict, status: str, reason: str, text: Optional[str],
                tokens: Optional[dict] = None, meta: Optional[dict] = None,
                reason_code: Optional[str] = None) -> None:
        cap = int(self.settings(P)["max_result_chars"])
        if u.get("lane"):
            status, reason = self.lanes.on_finish(P, u, status, reason, text, tokens, meta or {})
        # every terminal row names WHY (a bare "failed" is never the whole story)
        if status in ("done", "delivered"):
            reason_code = None
        elif not reason_code:
            reason_code = infer_reason_code(status, reason)
        phase = _phase_for(status, reason_code)
        self.store.x("UPDATE helper_units SET status=?, reason=?, result=?, updated_ts=?, "
                     "finished_ts=?, phase=?, reason_code=? WHERE unit_id=?",
                     (status, reason, (text or "")[:cap] or None, self.clock(), self.clock(),
                      phase, reason_code, u["unit_id"]))
        self.store.update_session(u["session_id"], status="ended", outcome=status,
                                  ended_ts=self.clock())
        if P.foundry is not None:
            try:
                P.foundry.end(u["session_id"], status)
            except FoundryError:
                pass
        began = u.get("started_ts") or u.get("created_ts")
        self.store.event(P.id, u["task_id"], f"helper_{status}", session_id=u["session_id"],
                         detail={"unit": u["unit_id"], "reason": reason, "kind": u.get("kind"),
                                 "lane": u.get("lane"),
                                 # numbers for the router's sprint scorecard
                                 "duration_s": (round(max(0.0, self.clock() - float(began)), 1)
                                                if began else None),
                                 **({"tokens": dict(tokens)} if isinstance(tokens, dict) else {})})

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
            code = "handoff_timeout" if status == "timed_out" else \
                (infer_reason_code(status, reason) if status in ("cancelled", "skipped",
                                                                  "failed") else None)
            self._finish(P, u, status, reason, None, reason_code=code)
            out.append(u["unit_id"])
        return out

    def skip_waiting(self, P, tid: str, reason: str) -> list[str]:
        """Units never admitted (pending / deferred) when the primary is ready:
        skipped, so optional work never holds required progress."""
        from .lanes import OWN_BUDGET_LANES
        out = []
        for st in ("pending", "deferred"):
            for u in self.units(P.id, tid, st):
                if u.get("lane") in OWN_BUDGET_LANES:
                    continue          # prework waits for the next implementing session
                why = f"{reason}" + (f" (last: {u['reason']})" if u.get("reason") else "")
                self.store.x("UPDATE helper_units SET status='skipped', reason=?, updated_ts=?, "
                             "phase='cancelled', reason_code='primary_ready' WHERE unit_id=?",
                             (why[:900], self.clock(), u["unit_id"]))
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
                '"prompt": "<what you need>", "files": ["<paths inside this task scope>"], '
                '"consumer": "<the step or commit that will use the result>"}]\n'
                "Ask only for help you will use: a helper's value is measured by whether a "
                "later commit of yours touches the files of its proposal.\n"
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
        from .lanes import OWN_BUDGET_LANES
        out = []
        for u in self.units(P.id, tid, "running"):
            if not u.get("lane") or u.get("lane") in OWN_BUDGET_LANES:
                # prework is bound to a commit, not to this session: it keeps
                # running and its note serves the next session (v0.126.4)
                continue
            if u.get("mode") == "direct":
                self.lanes.forget_direct(u["unit_id"])
            else:
                try:
                    P.client.abort(u["session_id"], directory=P.profile.workspace)
                except Exception:                                # noqa: BLE001
                    pass
            self._finish(P, u, "cancelled", reason, None, reason_code="session_ended")
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
            self._note_delivered_head(u)
            self.store.x("UPDATE helper_units SET status='delivered', delivered_session=?, "
                         "updated_ts=?, delivered_ts=?, feedback=CASE WHEN lane IS NULL THEN "
                         "COALESCE(feedback, 'delivered') ELSE 'delivered' END, "
                         "phase='completed', reason_code=NULL WHERE unit_id=?",
                         (session_id, self.clock(), self.clock(), u["unit_id"]))
        self.lanes.note_delivery(units, session_id)

    def _note_delivered_head(self, u: dict) -> None:
        """The workspace HEAD when a result reached its consumer: the git
        measurement compares what the consumer committed after it."""
        P = (getattr(self.c, "projects", None) or {}).get(u.get("project_id"))
        head = self._head(P) if P is not None else ""
        if not head:
            return
        row = self.store.q("SELECT feedback_detail FROM helper_units WHERE unit_id=?",
                           (u["unit_id"],))
        try:
            det = json.loads((row[0]["feedback_detail"] if row else None) or "{}")
        except (TypeError, ValueError):
            det = {}
        if not isinstance(det, dict):
            det = {}
        det["delivered_head"] = head
        self.store.x("UPDATE helper_units SET feedback_detail=? WHERE unit_id=?",
                     (json.dumps(det), u["unit_id"]))

    @staticmethod
    def proposal_paths(u: dict) -> list[str]:
        """Every workspace path a unit's proposal names: its files, the paths of a
        unified diff in its result, and (lane units) the envelope's affected paths
        and finding paths (":line" stripped)."""
        from .lanes import patch_paths
        out: list[str] = []
        files = u.get("files")
        if isinstance(files, str):
            try:
                files = json.loads(files or "[]")
            except ValueError:
                files = []
        out += [str(f) for f in files or []]
        out += patch_paths(str(u.get("result") or ""))
        try:
            env = json.loads(u.get("envelope") or "{}")
        except (TypeError, ValueError):
            env = {}
        if isinstance(env, dict):
            out += [str(p) for p in env.get("affected_paths") or []]
            out += patch_paths(str(env.get("patch") or ""))
            for f in env.get("findings") or []:
                if isinstance(f, dict):
                    out += [str(p) for p in f.get("paths") or []]
        clean = []
        for p in out:
            p = re.sub(r":\d+(?::\d+)?$", "", p.strip().replace("\\", "/"))
            while p.startswith("./"):
                p = p[2:]
            if p and not p.startswith(("/", "../")) and ":" not in p:
                clean.append(p)
        return sorted(dict.fromkeys(clean))

    def measure_git(self, P, tid: str, final: bool = False) -> list[str]:
        """revamp B5: a delivered result counts as used only when a commit after
        its delivery touched a path of the proposal (git, not the primary's
        report). With final=True (the consumer finished) an unmatched result is
        recorded unused. Returns the unit ids measured now."""
        from .automation import get as auto_get
        from .profile import matches_any
        if not auto_get(P, "helper_git_measure"):
            return []
        head = self._head(P)
        out = []
        for u in self.store.q("SELECT * FROM helper_units WHERE project_id=? AND task_id=? AND "
                              "status='delivered' AND git_used IS NULL", (P.id, tid)):
            try:
                det = json.loads(u.get("feedback_detail") or "{}")
            except (TypeError, ValueError):
                det = {}
            base = (det if isinstance(det, dict) else {}).get("delivered_head") or ""
            paths = self.proposal_paths(u)
            touched: list[str] = []
            if base and head and base != head and paths:
                try:
                    changed = P.ws.changed_files(base, head)
                except Exception:                                # noqa: BLE001
                    changed = []
                touched = [f for f in changed if matches_any(f, paths)]
            if touched:
                used = 1
            elif final:
                used = 0
            else:
                continue
            why = ("a commit touched " + ", ".join(touched[:6]) if used else
                   "no proposal paths to measure" if not paths else
                   "unknown delivery revision" if not base else
                   "no commit after delivery touched " + ", ".join(paths[:6]))
            self.store.x("UPDATE helper_units SET git_used=?, git_detail=? WHERE unit_id=?",
                         (used, json.dumps({"head": head, "delivered_head": base,
                                            "touched": touched[:20], "paths": paths[:20],
                                            "why": why}), u["unit_id"]))
            self.store.event(P.id, tid, "helper_git_used" if used else "helper_git_unused",
                             detail={"unit": u["unit_id"], "lane": u.get("lane"),
                                     "consumer": u.get("consumer"), "why": why[:300],
                                     "claimed": u.get("feedback")})
            out.append(u["unit_id"])
        return out

    def holding(self, pid: str, tid: str) -> list[dict]:
        """Running units an idle primary waits for (bounded by
        handoff_wait_minutes): ordinary units, and lanes whose result is
        likely to change what the primary does next. Advisory lanes (visual,
        research, preflight, handoff, evidence) never hold the primary."""
        return [u for u in self.units(pid, tid, "running")
                if not u.get("lane") or u["lane"] in ("failure_triage", "incremental_review",
                                                      "proposer", "test_design")]

    def record_feedback(self, P, t: dict, report: dict) -> list[str]:
        """The primary's own account of what it did with delivered results
        (report `helper_feedback`), for lane AND ordinary units (v0.122.0).
        Recorded as the primary's claim; it is not evidence that a fix works
        (the gate, review and Victor decide that). A rejection must say why:
        one without a reason is refused and the disposition stays pending."""
        out = []
        for f in (report or {}).get("helper_feedback") or []:
            if not isinstance(f, dict):
                continue
            uid = str(f.get("unit") or "")
            st = str(f.get("status") or "").lower()
            if st not in ("integrated", "rejected", "deferred", "superseded"):
                continue
            u = self.store.q("SELECT * FROM helper_units WHERE unit_id=? AND project_id=? AND "
                             "task_id=? AND status='delivered' AND "
                             "COALESCE(feedback,'') != ?", (uid, P.id, t["task_id"], st))
            if not u:
                continue
            note = str(f.get("note") or f.get("reason") or "").strip()[:500]
            det = json.loads(u[0].get("feedback_detail") or "{}")
            if st == "rejected" and not note:
                det.update(pending_note="the primary said rejected without a reason; a "
                                        "rejection must say why, so the disposition stays "
                                        "pending", claimed_by="primary",
                           session=t["session_id"])
                self.store.x("UPDATE helper_units SET feedback_detail=? WHERE unit_id=?",
                             (json.dumps(det), uid))
                self.store.event(P.id, t["task_id"], "helper_feedback_refused",
                                 session_id=t["session_id"],
                                 detail={"unit": uid, "status": st,
                                         "reason": "rejected without a reason"})
                continue
            det.pop("pending_note", None)
            det.update(note=note, claimed_by="primary", session=t["session_id"],
                       head=self._head(P))
            self.store.x("UPDATE helper_units SET feedback=?, feedback_detail=?, updated_ts=? "
                         "WHERE unit_id=?", (st, json.dumps(det), self.clock(), uid))
            self.store.event(P.id, t["task_id"], "helper_feedback", session_id=t["session_id"],
                             detail={"unit": uid, "status": st, "note": note[:200]})
            out.append(uid)
        return out

    def disposition_pending(self, pid: str, tid: str) -> list[dict]:
        """Delivered units (lane or ordinary) the primary has not yet said it
        integrated, rejected (with a reason) or that were superseded."""
        return [u for u in self.units(pid, tid, "delivered")
                if disposition(u) == "pending"]

    def statuses(self, pid: str, tid: str) -> list[dict]:
        """unit_status() of every unit of a task, with its id, kind and lane."""
        return [{"unit_id": u["unit_id"], "kind": u["kind"], "lane": u.get("lane"),
                 "status": u["status"], **unit_status(u)} for u in self.units(pid, tid)]

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
                              "AND COALESCE(feedback,'waiting') = 'waiting' AND lane <> 'prework'",
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
                             "superseded_by=?, reason=?, updated_ts=?, phase='cancelled', "
                             "reason_code='superseded' WHERE unit_id=?",
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

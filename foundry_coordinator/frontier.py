"""Optional frontier-primary phase (default OFF; manual request only, except the
[fast_lane] automatic hand-off after red studio gates: auto_request).

A frontier model may become the SOLE primary for a bounded, difficult coding
phase when local reasoning is the bottleneck. It is not an advisory helper and
never a second writer: it is the SAME Kilo session, the same single writer and
Studio controller, prompted only at a verified idle safe point, with the
frontier persona chosen for that prompt.

What the installed Kilo API supports (adapters/kilo.py, verified shapes for
kilo serve 7.8.8): POST /session/{id}/prompt_async takes `model: {providerID,
modelID}` on every prompt, so a session's next turn can name another model.
What is NOT verified without a live run: that Kilo actually serves that turn
with the named model. So the first frontier turn is checked from the session's
own transcript (assistant modelID == the frontier persona); otherwise the phase
aborts, ownership returns to the local persona and the option is marked
unsupported for this coordinator (further requests refused). That is the
documented limitation; nothing simulates a transfer by prompting concurrently.

State machine (persisted, restart-safe): requested -> active -> returned |
aborted | cancelled. Ownership changes only when the primary is idle with no
running tool part. Return conditions: the primary reports DONE / BLOCKED /
NEEDS_HUMAN, max_turns, max_minutes, a Foundry refusal (quota/budget/pause), a
new session (repair/closeout), or an operator return. Anti-oscillation:
max_phases_per_task and a cooldown between phases. Every frontier call is
bound with the optional purpose frontier_primary, so the quota gate (70% used,
fresh windows) and reserves apply exactly as for helpers. The frontier persona
counts as a code author: the final review must be a different persona.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS primary_phases (
  phase_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, run_id TEXT, session_id TEXT,
  state TEXT, persona TEXT, local_persona TEXT, requested_by TEXT, reason TEXT, head TEXT,
  snapshot_id TEXT, scope TEXT, conditions TEXT, turns INTEGER DEFAULT 0, verified_model TEXT,
  created_ts REAL, started_ts REAL, ended_ts REAL, end_reason TEXT);
"""
LIVE = ("requested", "active")


class FrontierPhase:
    def __init__(self, coordinator, clock=time.time):
        self.c = coordinator
        self.store = coordinator.store
        self.clock = clock
        self.store.conn.executescript(SCHEMA)

    @staticmethod
    def settings(P) -> dict:
        f = dict((getattr(P.profile, "raw", None) or {}).get("frontier_primary") or {})
        return {"enabled": bool(f.get("enabled", False)), "persona": str(f.get("persona") or ""),
                "max_turns": int(f.get("max_turns", 3)), "max_minutes": float(f.get("max_minutes", 120)),
                "max_phases_per_task": int(f.get("max_phases_per_task", 1)),
                "cooldown_minutes": float(f.get("cooldown_minutes", 60))}

    # ------------------------------------------------------------------ queries

    def phase(self, pid: str, tid: str, states=LIVE) -> Optional[dict]:
        q = ",".join("?" * len(states))
        r = self.store.q(f"SELECT * FROM primary_phases WHERE project_id=? AND task_id=? AND state IN "
                         f"({q}) ORDER BY created_ts DESC LIMIT 1", (pid, tid, *states))
        if not r:
            return None
        d = dict(r[0])
        d["conditions"] = json.loads(d["conditions"] or "{}")
        return d

    def phases(self, pid: str, tid: str) -> list[dict]:
        return self.store.q("SELECT * FROM primary_phases WHERE project_id=? AND task_id=? ORDER BY "
                            "created_ts", (pid, tid))

    def persona(self, P, t: dict) -> str:
        """The persona for the next prompt to this task's primary session."""
        ph = self.phase(P.id, t["task_id"], ("active",))
        if ph and ph["session_id"] == t.get("session_id"):
            return ph["persona"]
        return P.profile.persona("implementer", t["task_id"])

    def purpose(self, P, t: dict) -> str:
        ph = self.phase(P.id, t["task_id"], ("active",))
        return "frontier_primary" if ph and ph["session_id"] == t.get("session_id") else ""

    def authors(self, pid: str, tid: str) -> set:
        return {r["persona"] for r in self.store.q(
            "SELECT persona FROM primary_phases WHERE project_id=? AND task_id=? AND turns > 0",
            (pid, tid))}

    # ------------------------------------------------------------------ requests

    def _refusal(self, P, pid: str, tid: str, t: Optional[dict], f: dict,
                 states=("IMPLEMENTING",)) -> str:
        """Why a phase may not be requested now ('' = it may). One rule set for
        the manual request and the [fast_lane] automatic hand-off."""
        if not f["enabled"]:
            return ("the frontier-primary phase is disabled ([frontier_primary] enabled = "
                    "false, the default)")
        if not f["persona"]:
            return "[frontier_primary] persona is not set"
        if (self.store.meta("frontier_unsupported") or "") not in ("", "0"):
            return ("an earlier phase showed the installed Kilo does not serve a per-prompt "
                    "model switch: " + (self.store.meta("frontier_unsupported") or ""))
        if not t or t["state"] not in states:
            return (f"{pid}/{tid} is not IMPLEMENTING (a phase starts only inside an "
                    f"approved, running implementation)")
        if self.phase(pid, tid):
            return "a frontier phase is already requested or active for this task"
        done = self.phases(pid, tid)
        if len(done) >= f["max_phases_per_task"]:
            return f"max_phases_per_task ({f['max_phases_per_task']}) reached"
        last = max([p["ended_ts"] or p["created_ts"] for p in done] or [0])
        if last and self.clock() - last < f["cooldown_minutes"] * 60:
            return "cooldown after the last phase has not passed"
        if f["persona"] == P.profile.persona("implementer", tid):
            return "the frontier persona must differ from the local implementer persona"
        for role in ("reviewer", "deep_reviewer"):
            if role in P.profile.roles and P.profile.persona(role, tid) == f["persona"]:
                return f"{f['persona']} is also the {role}: review independence would be lost"
        return ""

    def _insert(self, P, t: dict, f: dict, actor: str, reason: str, state: str,
                head: str = "") -> str:
        pid_ = f"fp-{uuid.uuid4().hex[:10]}"
        tid = t["task_id"]
        now = self.clock()
        self.store.x("INSERT INTO primary_phases (phase_id, project_id, task_id, run_id, session_id, "
                     "state, persona, local_persona, requested_by, reason, conditions, created_ts, "
                     "started_ts, head) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (pid_, P.id, tid, t["run_id"], t["session_id"], state, f["persona"],
                      P.profile.persona("implementer", tid), actor, reason[:500],
                      json.dumps({"max_turns": f["max_turns"], "max_minutes": f["max_minutes"],
                                  "return_on": ["DONE", "BLOCKED", "NEEDS_HUMAN", "refusal",
                                                "new_session", "operator"]}), now,
                      now if state == "active" else None, head or None))
        return pid_

    def request(self, pid: str, tid: str, actor: str, reason: str) -> dict:
        """Manual request (operator / CLI). The phase starts at the next
        verified idle safe point (on_idle)."""
        P = self.c._project(pid)
        f = self.settings(P)
        t = self.store.task(pid, tid)
        why = self._refusal(P, pid, tid, t, f)
        if why:
            raise ValueError(why)
        pid_ = self._insert(P, t, f, actor, reason, "requested")
        self.store.event(pid, tid, "frontier_requested", detail={"phase": pid_, "by": actor,
                                                                 "persona": f["persona"]})
        return self.phase(pid, tid)

    def auto_request(self, P, t: dict, reason: str, head: str) -> tuple[Optional[dict], str]:
        """[fast_lane] automatic hand-off (UI-T0): after max_red_gates_before_handoff
        red studio gates on a LOCAL-primary fast-lane task, the frontier persona
        takes the same session. The only automatic trigger: every other task
        keeps the manual-only request. Same limits as a manual request
        (max_phases_per_task, cooldown, persona independence, unsupported).

        Called while the task is VERIFYING with the primary idle (a verified safe
        point: the coordinator itself is about to prompt it with the red-gate
        findings), so the phase is recorded ACTIVE at once and that findings
        prompt is the frontier's first turn (persona() / purpose() read it).
        Returns (phase, "") or (None, why not)."""
        f = self.settings(P)
        tid = t["task_id"]
        why = self._refusal(P, P.id, tid, t, f, states=("IMPLEMENTING", "VERIFYING"))
        if why:
            self.store.event(P.id, tid, "frontier_auto_refused", detail={"reason": why[:300]})
            return None, why
        pid_ = self._insert(P, t, f, "engine:fast_lane", reason, "active", head=head)
        self.store.event(P.id, tid, "frontier_requested", detail={
            "phase": pid_, "by": "engine:fast_lane", "persona": f["persona"], "auto": True})
        self.store.event(P.id, tid, "frontier_active", session_id=t["session_id"],
                         detail={"phase": pid_, "persona": f["persona"], "head": head,
                                 "auto": True})
        return self.phase(P.id, tid), ""

    def request_return(self, pid: str, tid: str, actor: str) -> Optional[dict]:
        ph = self.phase(pid, tid)
        if not ph:
            return None
        if ph["state"] == "requested":
            self._end(ph, "cancelled", f"cancelled by {actor} before it started")
        else:
            self.store.x("UPDATE primary_phases SET end_reason=? WHERE phase_id=?",
                         (f"operator:{actor}", ph["phase_id"]))
        return self.phase(pid, tid, ("requested", "active", "returned", "cancelled"))

    # ------------------------------------------------------------------ lifecycle

    def _end(self, ph: dict, state: str, why: str) -> None:
        self.store.x("UPDATE primary_phases SET state=?, ended_ts=?, end_reason=? WHERE phase_id=?",
                     (state, self.clock(), why[:500], ph["phase_id"]))
        self.store.event(ph["project_id"], ph["task_id"], f"frontier_{state}",
                         session_id=ph["session_id"], detail={"phase": ph["phase_id"],
                                                              "reason": why[:300]})

    def _tool_running(self, P, sid: str) -> bool:
        try:
            msgs = P.client.transcript(sid, directory=P.profile.workspace) or []
        except Exception:                                         # noqa: BLE001
            return True                                          # unknown: not a verified safe point
        for m in msgs:
            for p in (m.get("parts") or []) if isinstance(m, dict) else []:
                if p.get("type") == "tool" and (p.get("state") or {}).get("status") in (
                        "running", "pending"):
                    return True
        return False

    def _served_model(self, P, sid: str) -> str:
        try:
            msgs = P.client.transcript(sid, directory=P.profile.workspace) or []
        except Exception:                                         # noqa: BLE001
            return ""
        for m in reversed(msgs):
            info = (m or {}).get("info") or {}
            if info.get("role") == "assistant":
                return str(info.get("modelID") or "")
        return ""

    def on_new_session(self, P, tid: str) -> None:
        ph = self.phase(P.id, tid)
        if ph:
            self._end(ph, "returned" if ph["state"] == "active" else "cancelled",
                      "a new session started (repair/closeout): ownership is the local primary's")

    def on_refusal(self, P, tid: str, why: str) -> None:
        ph = self.phase(P.id, tid, ("active",))
        if ph:
            self._end(ph, "returned", f"Foundry refused a frontier call ({why}); the local primary "
                                      f"continues after the pause")

    def on_idle(self, P, t: dict, rep: dict) -> bool:
        """The primary is idle (a safe point). Start, verify or end a phase.
        True = a prompt was sent; the task stays IMPLEMENTING."""
        ph = self.phase(P.id, t["task_id"])
        if not ph:
            return False
        status = str((rep or {}).get("status") or "").upper()
        if ph["session_id"] != t["session_id"]:
            self._end(ph, "cancelled" if ph["state"] == "requested" else "returned",
                      "the primary session changed")
            return False
        if ph["state"] == "requested":
            if status in ("DONE", "BLOCKED", "NEEDS_HUMAN"):
                self._end(ph, "cancelled", f"the primary reported {status}; no phase needed")
                return False
            if self._tool_running(P, t["session_id"]):
                return False                                     # not a verified safe point yet
            spec = P.adapter.describe(t["task_id"])
            snap = self.c.helpers.lanes._snap(P, t, spec)
            if snap["status"] != "consistent":
                return False                                     # a writer is still active
            self.store.x("UPDATE primary_phases SET state='active', started_ts=?, head=?, "
                         "snapshot_id=?, scope=? WHERE phase_id=?",
                         (self.clock(), snap["head"], snap["snapshot_id"], json.dumps(spec.scope),
                          ph["phase_id"]))
            self.store.event(P.id, t["task_id"], "frontier_active", session_id=t["session_id"],
                             detail={"phase": ph["phase_id"], "persona": ph["persona"],
                                     "head": snap["head"], "snapshot": snap["snapshot_id"]})
            c = ph["conditions"]
            text = (P.adapter.continue_prompt(spec, "the coordinator moved this bounded phase to a "
                                                    "frontier model")
                    + f"\n\n## Frontier primary phase ({ph['phase_id']})\nThe same session continues "
                      f"on {ph['persona']} for at most {c['max_turns']} turn(s) / "
                      f"{int(c['max_minutes'])} min. Same scope, rules, gates and commit rules; you "
                      f"are still the only writer and Studio controller. Base revision "
                      f"{snap['head'][:12]} (snapshot {snap['snapshot_id']}). Reason: "
                      f"{ph['reason']}. When the hard part is done, report status NOT DONE with a "
                      f"summary; the local primary resumes. An independent reviewer checks the "
                      f"final revision.\n")
            try:
                return self.c._prompt_primary(P, self.store.task(P.id, t["task_id"]), text,
                                              f"frontier:{ph['phase_id']}:start")
            except Exception:
                # the start prompt never went out: ownership stays local (and the
                # next idle check must not read a local turn as "unsupported")
                self.store.x("UPDATE primary_phases SET state='requested', started_ts=NULL "
                             "WHERE phase_id=?", (ph["phase_id"],))
                raise
        # active: verify the turn that just ended, then decide whether to return
        served = self._served_model(P, t["session_id"])
        turns = int(ph["turns"] or 0) + 1
        self.store.x("UPDATE primary_phases SET turns=?, verified_model=COALESCE(verified_model, ?) "
                     "WHERE phase_id=?", (turns, served, ph["phase_id"]))
        if served != ph["persona"]:
            why = (f"Kilo served this turn with {served or 'an unknown model'}, not {ph['persona']}: "
                   f"a per-prompt model switch is not supported here")
            self.store.set_meta("frontier_unsupported", why[:300])
            self._end(ph, "aborted", why)
            return self._resume_local(P, t, "the frontier phase was aborted (unsupported); the "
                                            "local primary continues", f"abort:{ph['phase_id']}")
        c = ph["conditions"]
        if status in ("DONE", "BLOCKED", "NEEDS_HUMAN"):
            self._end(ph, "returned", f"the primary reported {status}")
            return False                                         # normal flow (verify / block)
        why = ""
        if (ph.get("end_reason") or "").startswith("operator:"):
            why = "an operator asked to return"
        elif turns >= int(c["max_turns"]):
            why = f"max_turns ({c['max_turns']}) reached"
        elif self.clock() - (ph["started_ts"] or self.clock()) > float(c["max_minutes"]) * 60:
            why = f"max_minutes ({int(c['max_minutes'])}) reached"
        elif not (rep or {}).get("frontier_continue"):
            why = "the frontier turn handed back"
        if why:
            self._end(ph, "returned", why)
            return self._resume_local(P, t, f"the frontier phase ended ({why}); the local primary "
                                            f"continues", f"return:{ph['phase_id']}")
        spec = P.adapter.describe(t["task_id"])
        return self.c._prompt_primary(P, self.store.task(P.id, t["task_id"]),
                                      P.adapter.continue_prompt(spec, "frontier phase: next turn"),
                                      f"frontier:{ph['phase_id']}:turn{turns}")

    def _resume_local(self, P, t, reason: str, key: str) -> bool:
        spec = P.adapter.describe(t["task_id"])
        t = self.store.task(P.id, t["task_id"])
        return self.c._prompt_primary(P, t, P.adapter.continue_prompt(spec, reason),
                                      f"frontier:{key}")

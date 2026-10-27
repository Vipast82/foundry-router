"""Decisions vs defects, recommended defaults and the timeout (revamp B11, W1.2).

Victor 2026-10-21: only product choices reach him, and each comes with a
recommended option and an editable timeout ([automation]
decision_timeout_minutes, default 30). When the timeout passes the
recommended option is applied with the authority `timeout_default`, journaled
(decision_timeout_applied) and announced (operator_notice); the operator can
still supersede it afterwards.

Victor 2026-10-22 (W1): every decision kind the coordinator opens carries
options, a recommended option and the timeout, so no sprint ever waits on a
person for a tooling limit. The default for a tooling limit (a lead bound or
hard cap, a no-progress stop, repeated findings, an implementer's BLOCKED
report) is to PARK the sprint: BLOCKED with a summary, and the queue moves on
to the next sprint (a dependent sprint waits, an independent one starts).
`no_progress` continues once first, then parks.

  class "decision"  carries a recommended answer and a deadline
  class "defect"    review_access / flaky_check / quarantine_check: the reviewer
                    can't read the packet or a check flips; never auto-answered

A decision whose recommendation matches none of its options keeps
recommended = None and waits for the operator: a timeout never invents an answer.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .automation import get as auto_get

TIMEOUT_AUTHORITY = "timeout_default"
TIMEOUT_ACTOR = "timeout_default"
DEFECT_KINDS = frozenset({"review_access", "flaky_check", "quarantine_check"})
SWEEP_SECONDS = 30.0

# The standard options of a tooling-limit decision (W1.2). PARK is recognised by
# Recovery._escalation_answer as a stop; CONTINUE_ONCE by engine._lc_before_session.
PARK = "stop the task (park it BLOCKED with a summary; the queue continues)"
CONTINUE_ONCE = "continue once (one more session), then park"
STRATEGY = "answer with a different strategy {strategy: ...}"
MORE_BUDGET = "authorize more budget"
_DEFAULT_SUFFIX = re.compile(r"\s*\((?:default|recommended)\)\s*$", re.I)


def classify(kind: str, evidence: Optional[dict]) -> str:
    return "defect" if kind in DEFECT_KINDS else "decision"


def _norm(text) -> str:
    s = str(text or "").strip().lower()
    s = _DEFAULT_SUFFIX.sub("", s)
    s = re.sub(r"\s+", " ", s).rstrip(" .;:!")
    return s


def match_option(options: list, text) -> Optional[str]:
    """The option label `text` names: exact (case/space-insensitive, a trailing
    '(default)' ignored), else one being a prefix of the other, else the same
    first three words. None when nothing matches: a default must be one of the
    offered options."""
    want = _norm(text)
    if not want or not options:
        return None
    labels = [str(o.get("label") if isinstance(o, dict) else o) for o in options]
    norms = [_norm(l) for l in labels]
    for label, n in zip(labels, norms):
        if n == want:
            return label
    for label, n in zip(labels, norms):
        if len(n) >= 4 and len(want) >= 4 and (want.startswith(n) or n.startswith(want)):
            return label
    head = " ".join(want.split()[:3])
    if len(want.split()) >= 3:
        for label, n in zip(labels, norms):
            if " ".join(n.split()[:3]) == head:
                return label
    return None


def _option(options: list, prefix: str) -> Optional[str]:
    for o in options or []:
        label = o.get("label") if isinstance(o, dict) else o
        if str(label or "").strip().lower().startswith(prefix):
            return str(label)
    return None


# an option that only a person can carry out (the owner clicks, logs in, provides,
# installs ...): a timeout must never "apply" it
_HUMAN_ACT = re.compile(
    r"\b(?:victor|the owner|the operator|a human|a person|manually|by hand|in person)\b"
    r"|\b(?:owner|operator|human|person|user|you)\s+(?:must\s+|will\s+|to\s+|should\s+)?"
    r"(?:clicks?|opens?|logs?\s+in|signs?\s+in|installs?|approves?|provides?|uploads?|"
    r"purchases?|buys?|pays?|presses?|connects?|restarts?|plugs?|reboots?|enters?|types?)\b",
    re.I)


def needs_human_act(option) -> bool:
    """Does this option need a person to do something (so a timeout can't apply it)?"""
    label = option.get("label") if isinstance(option, dict) else option
    return bool(_HUMAN_ACT.search(str(label or "")))


def meta(P, kind: str, evidence: Optional[dict], options: list,
         recommendation: str = "") -> dict:
    """{decision_class, recommended (JSON text or None), timeout_minutes} for a
    new decision of project P. The recommended option comes from
    evidence["recommended"], else from the `recommendation=` text the caller
    passed to open_decision; either must name one of the options."""
    ev = evidence if isinstance(evidence, dict) else {}
    cls = classify(kind, ev)
    rec = None
    from .obligations import OPERATOR_ONLY_KINDS
    if kind in OPERATOR_ONLY_KINDS:
        # approval / policy / expectation_change: the recommendation is shown
        # (the `recommendation` column) but never applied by a timeout
        return {"decision_class": cls, "recommended": None, "timeout_minutes": 0}
    if cls == "decision":
        if kind == "scope_request":
            rec = _option(options, str(auto_get(P, "scope_unlisted_default") or "deny"))
        else:
            for cand in (ev.get("recommended"), recommendation):
                got = match_option(options, cand) if cand else None
                if got is not None:
                    rec = got
                    break
            # 2026-10-27 one stop protocol: a timeout never applies an option that needs
            # a human act (a `user_action` blocker, or a recommended "Victor clicks
            # Connect"): nobody did it, so the next session would hit the same blocker.
            # The recommendation is still shown; the decision waits for the owner.
            if rec is not None and (str(ev.get("blocker_kind") or "") == "user_action"
                                    or needs_human_act(rec)):
                rec = None
    try:
        timeout = int(auto_get(P, "decision_timeout_minutes") or 0)
    except Exception:                                            # noqa: BLE001
        timeout = 0
    minutes = timeout if rec is not None else 0
    return {"decision_class": cls,
            "recommended": json.dumps(rec) if rec is not None else None,
            "timeout_minutes": minutes}


def due(store, now: float) -> list[dict]:
    try:
        return store.q("SELECT decision_id, project_id, task_id, kind, recommended, "
                       "timeout_minutes FROM decisions WHERE status='open' AND "
                       "recommended IS NOT NULL AND deadline_ts IS NOT NULL AND deadline_ts<=? "
                       "ORDER BY deadline_ts", (now,))
    except Exception:                                            # noqa: BLE001
        return []


# lead case states in which the lead is still working the stop (NEEDS_VICTOR: it gave up)
LEAD_WORKING = ("DIAGNOSING", "REPAIRING", "UPGRADING", "RESUMING", "DEFERRED")


def _lead_handling(c, d: dict) -> Optional[dict]:
    """The open lead case of d's task that is still working, or None."""
    try:
        t = c.store.task(d["project_id"], d["task_id"]) or {}
        cid = (t.get("data") or {}).get("recovery_case")
        if not cid:
            return None
        rows = c.store.q("SELECT case_id, state, deadline_ts FROM recovery_cases "
                         "WHERE case_id=?", (cid,))
    except Exception:                                            # noqa: BLE001
        return None
    if rows and rows[0]["state"] in LEAD_WORKING:
        return dict(rows[0])
    return None


def sweep(c, force: bool = False) -> list[str]:
    """Apply the recommended option of every open decision past its deadline.
    Cheap (one indexed query) and run at most every SWEEP_SECONDS."""
    now = c.clock()
    last = getattr(c, "_decision_sweep_ts", 0.0)
    if not force and now - last < SWEEP_SECONDS:
        return []
    c._decision_sweep_ts = now
    if not c.store.q("SELECT 1 FROM sqlite_master WHERE type='table' AND name='decisions'"):
        return []
    out = []
    for d in due(c.store, now):
        P = (getattr(c, "projects", None) or {}).get(d["project_id"])
        if P is None:
            continue
        case = _lead_handling(c, d)
        if case:
            # 0.145.2: a lead case is still working this very stop. The timeout
            # default (often "park the sprint") fired mid-repair and parked
            # GF-NAME1 and GF-GATE1 on 2026-10-27. The deadline follows the case's
            # own bound; once the lead gives up or the case closes, it applies.
            new = max(now + 300, float(case.get("deadline_ts") or 0) + 60)
            c.store.x("UPDATE decisions SET deadline_ts=? WHERE decision_id=?",
                      (new, d["decision_id"]))
            c.store.event(d["project_id"], d["task_id"], "decision_timeout_deferred", detail={
                "decision_id": d["decision_id"], "case_id": case["case_id"],
                "case_state": case["state"], "until": new})
            continue
        try:
            answer = json.loads(d["recommended"])
        except (TypeError, ValueError):
            continue
        if needs_human_act(answer):
            # (a decision opened before 2026-10-27 may still carry one): never applied by
            # a timeout; it waits for the owner
            c.store.x("UPDATE decisions SET deadline_ts=NULL WHERE decision_id=?",
                      (d["decision_id"],))
            c.store.event(d["project_id"], d["task_id"], "decision_timeout_skipped", detail={
                "decision_id": d["decision_id"], "recommended": answer,
                "why": "the recommended option needs a human act; it waits for the owner"})
            continue
        try:
            c.obligations.answer(d["decision_id"], answer, TIMEOUT_ACTOR, TIMEOUT_AUTHORITY)
        except Exception as e:                                   # noqa: BLE001
            c.store.event(d["project_id"], d["task_id"], "warning", detail={
                "decision_timeout": f"{d['decision_id']}: {type(e).__name__}: {e}"[:300]})
            # never retried every tick: the deadline moves back by its timeout
            c.store.x("UPDATE decisions SET deadline_ts=? WHERE decision_id=?",
                      (now + 60 * max(1, int(d.get("timeout_minutes") or 1)),
                       d["decision_id"]))
            continue
        detail = {"decision_id": d["decision_id"], "kind": d["kind"], "answer": answer,
                  "timeout_minutes": d.get("timeout_minutes"),
                  "note": "the recommended option was applied after the timeout; answer a "
                          "superseding decision to change it"}
        c.store.event(d["project_id"], d["task_id"], "decision_timeout_applied", detail=detail)
        c.store.event(d["project_id"], d["task_id"], "operator_notice", detail={
            "text": f"{d['task_id']}: decision {d['decision_id']} ({d['kind']}) timed out "
                    f"after {d.get('timeout_minutes')} min; applied the recommended option: "
                    f"{answer}"[:500], **detail})
        c.log(f"{d['project_id']}/{d['task_id']}: decision {d['decision_id']} timed out; "
              f"applied {answer!r}")
        out.append(d["decision_id"])
    return out


def is_park(answer) -> bool:
    """Did an answer choose the PARK option (or any plain 'stop')?"""
    a = answer
    if isinstance(a, dict):
        a = a.get("option") or a.get("choice") or a.get("strategy") or a.get("reason") or ""
    return _norm(a).startswith("stop")


def is_continue_once(answer) -> bool:
    a = answer
    if isinstance(a, dict):
        a = a.get("option") or a.get("choice") or a.get("strategy") or ""
    return _norm(a).startswith("continue once")

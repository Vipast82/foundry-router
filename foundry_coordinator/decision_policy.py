"""Decisions vs defects, recommended defaults and the timeout (revamp B11).

Victor 2026-10-21: only product choices reach him, and each comes with a
recommended option and an editable timeout ([automation]
decision_timeout_minutes, default 30). When the timeout passes the
recommended option is applied with the authority `timeout_default`, journaled
(decision_timeout_applied) and announced (operator_notice); the operator can
still supersede it afterwards.

  class "decision"  a product / scope choice the plan doesn't settle: carries a
                    recommended answer and a deadline when one is known
  class "defect"    tooling, harness or coordinator trouble (an obligation the
                    adapter can't satisfy, the reviewer can't read the packet, the
                    lead's own guard / route / bounds): never auto-answered; it
                    waits for a fix, and only that task waits

A decision with no safe default (e.g. "which strategy now?") keeps
recommended = None and waits for the operator: a timeout never invents an answer.
"""

from __future__ import annotations

import json
from typing import Optional

from .automation import get as auto_get

TIMEOUT_AUTHORITY = "timeout_default"
TIMEOUT_ACTOR = "timeout_default"
DEFECT_KINDS = frozenset({"obligation_conflict", "review_access", "flaky_check",
                          "quarantine_check"})
# lead escalations that are product questions (the rest are its own bounds,
# guard, readiness or route trouble: defects)
LEAD_DECISION_REASONS = frozenset({"lead_asks", "scope_core", "external"})
SWEEP_SECONDS = 30.0


def classify(kind: str, evidence: Optional[dict]) -> str:
    ev = evidence if isinstance(evidence, dict) else {}
    if kind in DEFECT_KINDS:
        return "defect"
    if kind == "lead_escalation":
        return "decision" if str(ev.get("reason") or "") in LEAD_DECISION_REASONS else "defect"
    return "decision"


def _option(options: list, prefix: str) -> Optional[str]:
    for o in options or []:
        label = o.get("label") if isinstance(o, dict) else o
        if str(label or "").strip().lower().startswith(prefix):
            return str(label)
    return None


def meta(P, kind: str, evidence: Optional[dict], options: list) -> dict:
    """{decision_class, recommended (JSON text or None), timeout_minutes,
    deadline_ts offset} for a new decision of project P."""
    ev = evidence if isinstance(evidence, dict) else {}
    cls = classify(kind, ev)
    rec = None
    if cls == "decision":
        if kind == "scope_request":
            rec = _option(options, str(auto_get(P, "scope_unlisted_default") or "deny"))
        elif ev.get("recommended"):
            rec = str(ev["recommended"])
            if options and _option(options, rec.strip().lower()[:12]) is None and \
                    rec not in [str(o) for o in options]:
                rec = None           # a default must be one of the offered options
    minutes = int(auto_get(P, "decision_timeout_minutes") or 0) if rec is not None else 0
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
        try:
            answer = json.loads(d["recommended"])
        except (TypeError, ValueError):
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

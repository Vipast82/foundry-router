"""What a task is actually doing, and who has to act, in words a person reads.

The engine's states are its own bookkeeping: AWAITING_HUMAN also covers a model
visual review, BLOCKED also covers a Lead repair in progress, PAUSED keeps the
note of the session it paused. Victor 2026-11-08: "the GUI is read and managed by
a human ... if its awaiting human, then that means a HUMAN must intervene, if its
waiting for Visual Inspection then it should reflect that". `situation()` is the
one place that reads the state together with its approval policy, open decisions,
Lead case, background work and wait notes, and says:

    {"code": ..., "label": ..., "who": ..., "detail": ...}

`who` is "human" only when a person must act; otherwise "model" (a model
reviews or judges), "lead" (the Lead is repairing), "agent" (a coding session),
or "coordinator" (the coordinator itself: checks, queues, waits, defaults).
The Control Center shows the label and raises "needs you" alerts only for
who == "human".
"""
from __future__ import annotations

import time
from typing import Optional

HUMAN, MODEL, LEAD, AGENT, COORD = "human", "model", "lead", "agent", "coordinator"
LEAD_TERMINAL = ("RESOLVED", "CANCELLED")


def _clock(ts) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return ""


def _s(x, n: int = 240) -> str:
    return str(x or "").strip()[:n]


def model_approves(raw: dict, kind: str) -> bool:
    """A kind of approval a model gives ([approval] visual_model for "visual")."""
    appr = (raw or {}).get("approval") or {}
    return kind == "visual" and appr.get("visual_model") is True


def _sit(code: str, label: str, who: str, detail: str = "") -> dict:
    return {"code": code, "label": label, "who": who, "detail": _s(detail, 400)}


def situation(t: dict, raw: Optional[dict] = None, decisions: Optional[list] = None,
              lead: Optional[dict] = None, background: Optional[dict] = None) -> dict:
    """`t`: a task row (state, data, blocker, awaiting, next_action, pause_reason,
    pause_until); `raw`: the profile's raw config; `decisions`: decisions_view rows;
    `lead`: lead_view; `background`: background_work(t)."""
    state = str(t.get("state") or "")
    d = t.get("data") or {}
    nxt = _s(t.get("next_action"), 300)
    lead = lead if isinstance(lead, dict) else {}
    lstate = str(lead.get("state") or "")
    opened = [x for x in (decisions or []) if isinstance(x, dict) and x.get("status") == "open"]

    if state in ("COMPLETE", "CANCELLED"):
        return _sit(state, "Finished" if state == "COMPLETE" else "Cancelled", COORD)
    if state == "FAILED":
        return _sit("FAILED", "Failed: decide whether to retry or cancel", HUMAN,
                    t.get("blocker") or nxt)

    # a person is asked: the Lead's escalation, a decision with no default, a question
    if lstate == "NEEDS_VICTOR":
        return _sit("NEEDS_YOU", "Needs you: the Lead's recovery question", HUMAN,
                    lead.get("trigger") or t.get("blocker"))
    for x in opened:
        if not x.get("deadline_ts") and not x.get("timeout_minutes"):
            return _sit("NEEDS_YOU", "Needs you: a decision", HUMAN, x.get("question"))
    if d.get("question"):
        return _sit("NEEDS_YOU", "Needs you: answer the session's question", HUMAN,
                    (d.get("question") or {}).get("text") if isinstance(d.get("question"), dict)
                    else d.get("question"))

    if state == "AWAITING_HUMAN":
        kinds = [str(k) for k in (d.get("awaiting_kinds") or [])]
        if kinds and all(model_approves(raw or {}, k) for k in kinds):
            rnd = d.get("visual_changes_rounds")
            return _sit("VISUAL_REVIEW", "Visual inspection by the model judge"
                        + (f" (after fix round {rnd})" if rnd else ""), MODEL,
                        t.get("awaiting") or "")
        what = ", ".join(kinds) or _s(t.get("awaiting"), 80) or "approval"
        return _sit("AWAITING_HUMAN", f"Waiting for your {what} approval", HUMAN,
                    t.get("awaiting"))

    # a decision with a default: the coordinator applies it at the deadline
    for x in opened:
        at = _clock(x.get("deadline_ts"))
        return _sit("DECISION_PENDING", "Decision pending: the recommended default applies"
                    + (f" at {at}" if at else " at its timeout"), COORD, x.get("question"))

    if lstate and lstate not in LEAD_TERMINAL:
        return _sit("LEAD_RECOVERY", "The Lead is repairing it" + (
            f" ({lstate.lower()})" if lstate else ""), LEAD,
            lead.get("waiting") or lead.get("trigger") or t.get("blocker") or "")

    if state == "BLOCKED":
        blk = _s(t.get("blocker"), 300)
        if blk.startswith("lead recovery:"):
            return _sit("LEAD_RECOVERY", "The Lead is repairing it", LEAD, blk)
        if blk.startswith("waiting for operator decision"):
            return _sit("DECISION_PENDING", "Decision pending", COORD, blk)
        return _sit("BLOCKED", "Stopped: clear the blocker, then unblock", HUMAN, blk)

    if state == "PAUSED":
        hold = d.get("hold") if isinstance(d.get("hold"), dict) else {}
        owner = str(hold.get("owner") or "")
        if t.get("pause_until"):
            return _sit("PAUSED", f"Paused until {_clock(t.get('pause_until'))} (budget reset)",
                        COORD, t.get("pause_reason"))
        if owner and owner not in ("victor", "operator") and not hold.get("actor") == "victor":
            return _sit("HELD", f"Held by the coordinator ({owner})", COORD,
                        hold.get("reason") or t.get("pause_reason"))
        return _sit("PAUSED", "Paused by you", HUMAN,
                    t.get("pause_reason") or hold.get("reason"))

    bg = background if isinstance(background, dict) else {}
    if state in ("IMPLEMENTING", "VERIFYING", "CLOSING") and (
            bg.get("kind") == "gate" or nxt.lower().startswith("scripted studio gate")):
        return _sit("STUDIO_GATE", "Studio checks running", COORD, nxt)
    if bg.get("kind") == "vision":
        return _sit("VISUAL_REVIEW", "Visual inspection by the model judge", MODEL, nxt)

    if state in ("READY", "CHANGES_REQUIRED", "QUEUED") and nxt.startswith("waiting for"):
        return _sit("WAITING_TURN", "Waiting its turn", COORD, nxt)
    labels = {
        "QUEUED": ("QUEUED", "Queued behind the tasks it depends on", COORD),
        "READY": ("READY", "Ready: the next coding session starts shortly", COORD),
        "CHANGES_REQUIRED": ("FIX_ROUND", "Fix round: changes from review or the judge are next",
                             COORD),
        "IMPLEMENTING": ("CODING", "A coding session is working", AGENT),
        "VERIFYING": ("CHECKING", "Running the project's checks", COORD),
        "REVIEWING": ("CODE_REVIEW", "Code review by the reviewer model", MODEL),
        "APPROVED": ("CLOSING", "Approved: closeout is next", COORD),
        "CLOSING": ("CLOSING", "Closeout: docs and the final gate", AGENT),
    }
    code, label, who = labels.get(state, (state or "UNKNOWN", state or "No state reported", COORD))
    return _sit(code, label, who, nxt)

"""Task lifecycle: states, allowed transitions and the evidence each needs.

A transition is refused unless (from, to) is listed here AND every evidence
key it names is present and non-empty. The engine gathers evidence from
mechanical sources (git, the project's verification commands, a validated
review bound to a commit, a recorded human approval); a coding model's own
"done" report is stored for the record but is never evidence.

    QUEUED ─► READY ─► IMPLEMENTING ─► VERIFYING ─► REVIEWING ─► APPROVED ─► CLOSING ─► COMPLETE
                ▲            │  ▲            │            │            │          │
                │            │  └─ CHANGES_REQUIRED ◄─────┴────────────┼──────────┤
                │            └─► AWAITING_HUMAN (question) ◄───────────┴──────────┘ (approval)
    any active state ─► PAUSED (operator, budget, pause switch) ─► back where it was
    any active state ─► CANCELLED (operator)      BLOCKED / FAILED ─► READY (operator)
"""

from __future__ import annotations

QUEUED = "QUEUED"
READY = "READY"
IMPLEMENTING = "IMPLEMENTING"
VERIFYING = "VERIFYING"
REVIEWING = "REVIEWING"
CHANGES_REQUIRED = "CHANGES_REQUIRED"
APPROVED = "APPROVED"
CLOSING = "CLOSING"
COMPLETE = "COMPLETE"
BLOCKED = "BLOCKED"
AWAITING_HUMAN = "AWAITING_HUMAN"
PAUSED = "PAUSED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

ALL = (QUEUED, READY, IMPLEMENTING, VERIFYING, REVIEWING, CHANGES_REQUIRED, APPROVED,
       CLOSING, COMPLETE, BLOCKED, AWAITING_HUMAN, PAUSED, FAILED, CANCELLED)
TERMINAL = (COMPLETE, CANCELLED)
# States in which the engine acts on its own. BLOCKED, FAILED, AWAITING_HUMAN
# and PAUSED wait for a person (or, for a budget pause, the reset time).
ACTIVE = (QUEUED, READY, IMPLEMENTING, VERIFYING, REVIEWING, CHANGES_REQUIRED, APPROVED, CLOSING)
# A session may be writing to the workspace in these states.
WRITING = (IMPLEMENTING, CLOSING)

# Each value is the evidence a transition needs: one tuple of keys, or a list
# of alternative tuples (any one fully present is enough).
TRANSITIONS: dict[tuple[str, str], object] = {
    (QUEUED, READY): ("deps_satisfied",),
    (QUEUED, BLOCKED): ("blocker",),
    (READY, IMPLEMENTING): ("session_id", "base_sha", "run_id"),
    (READY, BLOCKED): ("blocker",),
    (READY, FAILED): ("reason",),
    (IMPLEMENTING, VERIFYING): ("head_sha", "session_idle"),
    (IMPLEMENTING, CHANGES_REQUIRED): ("findings",),
    (IMPLEMENTING, BLOCKED): ("blocker",),
    (IMPLEMENTING, AWAITING_HUMAN): ("awaiting",),
    (VERIFYING, REVIEWING): ("verification_id", "head_sha", "all_required_pass"),
    (VERIFYING, CHANGES_REQUIRED): ("verification_id", "findings"),
    (VERIFYING, BLOCKED): ("verification_id", "blocker"),
    # [lifecycle] same-session continuation: the submission gate (or an
    # evidence-incomplete review) sent ONE journaled prompt to the same session,
    # listing the exact missing evidence; no implementation attempt is charged
    (VERIFYING, IMPLEMENTING): ("verification_id", "session_id", "continuation"),
    (REVIEWING, IMPLEMENTING): ("review_id", "session_id", "continuation"),
    (REVIEWING, APPROVED): ("review_id", "reviewed_commit_sha"),
    (REVIEWING, CHANGES_REQUIRED): ("review_id", "findings"),
    (REVIEWING, BLOCKED): ("blocker",),
    (REVIEWING, VERIFYING): ("invalidated",),
    (CHANGES_REQUIRED, IMPLEMENTING): ("session_id", "base_sha", "run_id"),
    (CHANGES_REQUIRED, FAILED): ("reason",),
    (CHANGES_REQUIRED, BLOCKED): ("blocker",),
    (APPROVED, CLOSING): ("approvals_ok",),
    (APPROVED, AWAITING_HUMAN): ("awaiting",),
    (APPROVED, VERIFYING): ("invalidated",),
    (AWAITING_HUMAN, IMPLEMENTING): ("answered",),
    (AWAITING_HUMAN, CLOSING): [("approval_id",), ("answered",)],
    (AWAITING_HUMAN, COMPLETE): ("approval_id", "closeout_id"),
    (AWAITING_HUMAN, CHANGES_REQUIRED): ("findings",),
    (AWAITING_HUMAN, VERIFYING): ("invalidated",),
    (CLOSING, COMPLETE): ("closeout_id", "review_valid", "published"),
    (CLOSING, AWAITING_HUMAN): [("closeout_id", "awaiting"), ("awaiting", "session_id")],
    (CLOSING, CHANGES_REQUIRED): ("findings",),
    (CLOSING, VERIFYING): ("invalidated",),
    (CLOSING, BLOCKED): ("blocker",),
    (BLOCKED, READY): ("resolution",),
    # [lifecycle] a gate/review decision was answered: verify the same head again
    (BLOCKED, VERIFYING): ("resolution", "decision_id"),
    (FAILED, READY): ("resolution",),
}

# Operator controls, allowed from any state that isn't terminal.
PAUSE_EVIDENCE = ("reason", "resume_state")
CANCEL_EVIDENCE = ("reason",)


class TransitionError(ValueError):
    pass


def required_evidence(frm: str, to: str):
    if to == CANCELLED and frm not in TERMINAL:
        return CANCEL_EVIDENCE
    if to == PAUSED and frm not in TERMINAL and frm != PAUSED:
        return PAUSE_EVIDENCE
    if frm == PAUSED and to != PAUSED:
        return ("resumed",)
    try:
        return TRANSITIONS[(frm, to)]
    except KeyError:
        raise TransitionError(f"{frm} -> {to} is not an allowed transition") from None


def check(frm: str, to: str, evidence: dict) -> None:
    """Raise TransitionError unless the transition is allowed with this evidence."""
    if frm in TERMINAL:
        raise TransitionError(f"{frm} is final")
    need = required_evidence(frm, to)
    options = need if isinstance(need, list) else [need]
    best = None
    for keys in options:
        missing = [k for k in keys if evidence.get(k) in (None, "", [], {}, False)]
        if not missing:
            return
        best = missing if best is None or len(missing) < len(best) else best
    raise TransitionError(f"{frm} -> {to} needs evidence: {', '.join(best or [])}")

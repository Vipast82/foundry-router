"""Deterministic auto-heal before a lead case (revamp B9, B12).

Most stops that reached the lead were tooling or infrastructure faults that no
diagnosis fixes: Kilo not admitting a prompt, a 5xx, Rojo or Studio down, the
studio_gate runner erroring instead of returning a verdict. Before the engine
opens a paid lead case for such a stop, it re-runs the step itself:

  verify   the gate / required checks could not run -> BLOCKED, wait, VERIFYING
           again at the same head (no session, no attempt)
  restart  the session died on infrastructure -> BLOCKED, wait, READY with a free
           retry (no attempt is charged)

Each (head, stop class, signature) is retried at most [automation]
auto_heal_retries times with a doubling backoff ([automation]
infra_backoff_seconds, capped at 15 min); after that the stop goes to the lead
(or the operator) as before. Every retry is journaled as an event and in the
task's data. No model call is made here.
"""

from __future__ import annotations

import re
from typing import Optional

from . import states as S
from .automation import get as auto_get

# known infrastructure signatures (01_issue_audit.md F10) -> name
INFRA_SIGNATURES = (
    ("kilo_not_admitted", re.compile(r"did not admit the prompt|prompt_not_admitted", re.I)),
    ("kilo_aborted", re.compile(r"MessageAbortedError|aborted every turn", re.I)),
    ("backend_5xx", re.compile(r"\b(?:HTTP\s*)?5(?:00|02|03|04)\b|Bad Gateway|Service "
                               r"Unavailable|Gateway Time-?out", re.I)),
    ("connection", re.compile(r"ECONNREFUSED|ECONNRESET|connection (?:refused|reset|aborted)|"
                              r"timed out connecting|could not connect", re.I)),
    ("rojo", re.compile(r"\brojo\b[^.\n]{0,80}(?:exited|down|not running|not syncing|"
                        r"websocket|refused|unreachable|code -1)", re.I)),
    ("studio", re.compile(r"\bstudio\b[^.\n]{0,80}(?:not connected|disconnected|not "
                          r"running|plugin[^.\n]{0,30}(?:connect|timeout)|mcp[^.\n]{0,40}"
                          r"(?:unavailable|not connected|timed out))", re.I)),
    ("python_path", re.compile(r"No module named '?foundry_coordinator|foundry_coordinator"
                               r"[^.\n]{0,30}not importable", re.I)),
    ("context_overflow", re.compile(r"ContextOverflowError|Compaction exhausted|context "
                                    r"(?:window )?(?:exceeded|overflow)", re.I)),
    ("kilo_auth", re.compile(r"Kilo[^.\n]{0,40}\b401\b", re.I)),
)
# stop classes whose blocker is the gate / check tooling, not the code
VERIFY_CLASSES = ("fast_lane_gate_error", "checks_unknown")
# stop classes of a session that may have died on infrastructure
SESSION_CLASSES = ("session_blocked", "session_needs_human", "session_error")
MAX_BACKOFF = 900


def infra_signature(text: str) -> str:
    for name, rx in INFRA_SIGNATURES:
        if rx.search(text or ""):
            return name
    return ""


def plan(cls: str, reason: str) -> Optional[tuple[str, str]]:
    """(signature, action) for a stop the coordinator can retry itself, else None."""
    cls = str(cls or "")
    if cls in VERIFY_CLASSES:
        return (infra_signature(reason) or "gate_tooling"), "verify"
    if cls in SESSION_CLASSES:
        sig = infra_signature(reason)
        if sig:
            return sig, "restart"
    return None


def try_heal(c, P, t: dict, to: str, evidence: dict, cls: str, fields: dict) -> Optional[dict]:
    """Called by Coordinator._stop before a lead case is opened. Returns the
    BLOCKED transition of a scheduled retry, or None (the stop proceeds)."""
    if to != S.BLOCKED:
        return None
    reason = str(evidence.get("blocker") or evidence.get("reason") or "")
    got = plan(cls, reason)
    if got is None:
        return None
    sig, action = got
    retries = int(auto_get(P, "auto_heal_retries") or 0)
    if retries <= 0:
        return None
    pid, tid = P.id, t["task_id"]
    cur = c.store.task(pid, tid) or t
    if cur["state"] in S.TERMINAL or cur["state"] == S.BLOCKED:
        return None
    try:
        head = P.ws.head()
    except Exception:                                            # noqa: BLE001
        head = ""
    data = dict(fields.get("data") or cur.get("data") or {})
    counts = dict(data.get("auto_heal_counts") or {})
    key = f"{head[:12]}|{cls}|{sig}"
    n = int(counts.get(key) or 0)
    if n >= retries:
        c.store.event(pid, tid, "auto_heal_exhausted", detail={
            "key": key, "retries": n, "action": action, "reason": reason[:300]})
        return None
    delay = min(MAX_BACKOFF, int(auto_get(P, "infra_backoff_seconds") or 0) * (2 ** n))
    now = c.clock()
    counts[key] = n + 1
    blocker = (f"auto-heal {n + 1}/{retries} ({sig}): "
               + ("re-running the gate/checks" if action == "verify"
                  else "restarting the session (no attempt charged)")
               + f" in {delay} s; was: {reason}")[:1000]
    data.update(auto_heal_counts=counts, auto_heal={
        "key": key, "n": n + 1, "action": action, "signature": sig, "class": cls,
        "retry_at": now + delay, "blocker": blocker, "reason": reason[:500],
        "verification_id": evidence.get("verification_id") or fields.get("verification_id")
        or ""})
    ev = {**evidence, "blocker": blocker, "auto_heal": True}
    try:
        S.check(cur["state"], S.BLOCKED, ev)
    except S.TransitionError:
        return None
    extra = {k: fields[k] for k in ("verification_id", "head_sha") if fields.get(k)}
    c.store.release(pid, tid)
    out = c.store.transition(pid, tid, S.BLOCKED, ev, blocker=blocker, data=data,
                             next_action=blocker[:500], **extra)
    c.store.event(pid, tid, "auto_heal", detail={
        "key": key, "n": n + 1, "of": retries, "action": action, "signature": sig,
        "class": cls, "delay_s": delay, "reason": reason[:300]})
    return out


def step(c, P, t: dict) -> bool:
    """A BLOCKED task with a scheduled auto-heal: wait for its time, then retry.
    True when this handled the tick (the caller does nothing else)."""
    data = dict(t.get("data") or {})
    ah = data.get("auto_heal") or {}
    if not ah:
        return False
    pid, tid = P.id, t["task_id"]
    if t.get("blocker") != ah.get("blocker"):
        # the task was blocked again for another reason (or an operator edited it)
        data.pop("auto_heal", None)
        c.store.update_task(pid, tid, data=data)
        return False
    if c.clock() < float(ah.get("retry_at") or 0):
        return True
    held = getattr(c, "_held", None)
    if callable(held) and held(P, t, "auto-heal retry"):
        return True
    data.pop("auto_heal", None)
    res = f"auto-heal retry {ah.get('n')} ({ah.get('signature')}): {ah.get('action')}"
    if ah.get("action") == "verify":
        if c._acquire(P, t, c._workspace_locks(P)):
            return True                  # another writer holds the workspace: next tick
        c.store.transition(pid, tid, S.VERIFYING, {
            "resolution": res, "decision_id": f"auto_heal:{ah.get('key')}"},
            data=data, next_action=res[:500])
    else:
        data["free_retry"] = True
        c.store.transition(pid, tid, S.READY, {"resolution": res, "auto_heal": True},
                           data=data, next_action=res[:500])
    c.store.event(pid, tid, "auto_heal_retry", detail={"key": ah.get("key"),
                                                       "action": ah.get("action")})
    return True

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
  class "defect"    flaky_check / quarantine_check: a check flips; never
                    auto-answered

Audit D (2026-10-28), nothing waits on a person for a tooling fault:
  * review_access is a decision: the engine retries the review with a rebuilt
    packet, then the alternate reviewer; then the lead takes it, else it parks
    after the timeout (recommended PARK).
  * Only a `user_action` blocker (a person must do something) cancels the
    timeout. A recommended option that reads as a human act ("Victor clicks
    ...") under any other blocker kind is never applied: PARK is the default.
  * A recommendation that matches none of the options defaults to PARK with
    the normal timeout (never an indefinite wait). With no recommendation at
    all the decision waits for the operator.

Self-driving part 1 (Victor 2026-10-28, reports/SELF_DRIVING_PLAN.md): only
product decisions can stop a sprint. Every decision gets a category:
  product      blocker_kind product, plan / scope questions, 2D asks, approvals
               and a lead's own question (lead_asks, scope_core, external)
  user_action  only the owner can physically do it (no timeout, as before)
  automation   everything else: tooling, budget, infra, repeated findings, no
               progress, review access, and lead escalations of those
An automation decision never times out to PARK: its recommended option is
SELF_REPAIR, and at the timeout `self_repair_plan` picks a capped repair:
  budget    an audited ledger extension authorized by `policy` (obligations
            POLICY_ACTOR), capped per task per rolling day ([automation]
            self_repair_max_attempts/reviews/sessions_per_day)
  findings  one lead repair (another bounded round, with the finding text),
            then a fresh session with a "different approach" brief, then one
            session on the [frontier_primary] persona
  tooling   a retry with backoff (self_repair_retry_minutes, doubling), at most
            self_repair_max_retries_per_day
Only when that is exhausted does it park, quietly: the answer carries
self_repair.exhausted, and the notice a `digest` flag and the alert class
`self_repair_exhausted`. Product and user_action decisions keep the behaviour
above. [automation] self_repair = false restores PARK.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .automation import get as auto_get

TIMEOUT_AUTHORITY = "timeout_default"
TIMEOUT_ACTOR = "timeout_default"
DEFECT_KINDS = frozenset({"flaky_check", "quarantine_check"})
SWEEP_SECONDS = 30.0

# The standard options of a tooling-limit decision (W1.2). PARK is recognised by
# Recovery._escalation_answer as a stop; CONTINUE_ONCE by engine._lc_before_session.
PARK = "stop the task (park it BLOCKED with a summary; the queue continues)"
CONTINUE_ONCE = "continue once (one more session), then park"
STRATEGY = "answer with a different strategy {strategy: ...}"
MORE_BUDGET = "authorize more budget"
# the default of an automation decision (self-driving part 1): never a stop
SELF_REPAIR = ("self-repair (automatic and capped: a policy budget extension, a lead repair, "
               "a different approach, then the frontier implementer; parks quietly only when "
               "exhausted)")
_DEFAULT_SUFFIX = re.compile(r"\s*\((?:default|recommended)\)\s*$", re.I)

PRODUCT, USER_ACTION, AUTOMATION = "product", "user_action", "automation"
CATEGORIES = (PRODUCT, USER_ACTION, AUTOMATION)
# decisions that are the operator's by nature: approvals (2D asks are `approval`),
# scope / expectation / policy changes and plan questions (obligation conflicts)
PRODUCT_KINDS = frozenset({"approval", "scope_change", "scope_request", "expectation_change",
                           "policy", "playbook_promotion", "quarantine_check",
                           "obligation_conflict", "obligation_phase"})
# lead escalations that are the lead's own product question (lead.py _route)
LEAD_PRODUCT_REASONS = frozenset({"lead_asks", "scope_core", "external"})
_IMAGE_ASK = re.compile(r"\b(?:2d (?:image )?assets?|image assets?)\b", re.I)


def classify(kind: str, evidence: Optional[dict]) -> str:
    return "defect" if kind in DEFECT_KINDS else "decision"


def category(kind: str, evidence: Optional[dict] = None, question: str = "") -> str:
    """product | user_action | automation for a decision (see the module notes)."""
    ev = evidence if isinstance(evidence, dict) else {}
    bk = str(ev.get("blocker_kind") or "").strip().lower()
    if kind == USER_ACTION or bk == USER_ACTION or str(ev.get("user_action") or "").strip():
        return USER_ACTION
    if kind in PRODUCT_KINDS or bk == PRODUCT or _IMAGE_ASK.search(str(question or "")):
        return PRODUCT
    if kind == "implementer_blocked" and bk != "tooling":
        return PRODUCT                   # an unknown blocker_kind is product (blocker_kind())
    if kind == "lead_escalation" and str(ev.get("reason") or "") in LEAD_PRODUCT_REASONS:
        return PRODUCT
    return AUTOMATION


def decision_category(d: dict) -> str:
    """The stored category of a decision row, else the classifier (older rows)."""
    cat = str((d or {}).get("category") or "")
    if cat in CATEGORIES:
        return cat
    ev = (d or {}).get("evidence")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except ValueError:
            ev = {}
    return category(str((d or {}).get("kind") or ""), ev if isinstance(ev, dict) else {},
                    str((d or {}).get("question") or ""))


def _auto(P, key: str, default):
    try:
        v = auto_get(P, key)
    except Exception:                                             # noqa: BLE001
        return default
    return default if v is None else v


def self_repair_on(P) -> bool:
    return bool(_auto(P, "self_repair", True))


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


# decision kinds whose answer may be "stop the task" (Recovery._escalation_answer,
# engine.LC_STOP_KINDS, the lead's NEEDS_VICTOR step): PARK is a valid default for them
PARK_KINDS = frozenset({"recovery_escalation", "no_progress", "review_access",
                        "implementer_blocked", "lead_escalation"})


def _park_option(kind: str, options: list) -> Optional[str]:
    """The PARK default of a decision: its own stop option, else PARK for a kind that
    understands a stop; None for any other kind (it waits as before)."""
    own = _option(options, "stop")
    if own is not None:
        return own
    return PARK if kind in PARK_KINDS else None


def meta(P, kind: str, evidence: Optional[dict], options: list,
         recommendation: str = "") -> dict:
    """{decision_class, recommended (JSON text or None), timeout_minutes} for a
    new decision of project P. The recommended option comes from
    evidence["recommended"], else from the `recommendation=` text the caller
    passed to open_decision; either must name one of the options."""
    ev = evidence if isinstance(evidence, dict) else {}
    cls = classify(kind, ev)
    cat = category(kind, ev)
    rec = None
    from .obligations import OPERATOR_ONLY_KINDS
    if kind in OPERATOR_ONLY_KINDS:
        # approval / policy / expectation_change: the recommendation is shown
        # (the `recommendation` column) but never applied by a timeout
        return {"decision_class": cls, "recommended": None, "timeout_minutes": 0,
                "category": cat}
    if cls == "decision":
        if kind == "scope_request":
            rec = _option(options, str(auto_get(P, "scope_unlisted_default") or "deny"))
        else:
            cands = [c for c in (ev.get("recommended"), recommendation) if c]
            for cand in cands:
                got = match_option(options, cand)
                if got is not None:
                    rec = got
                    break
            park = _park_option(kind, options)
            if rec is None and cands:
                # audit D: an unmatched recommendation (often a lead's free text) parks
                # with the normal timeout instead of waiting forever
                rec = park
            elif rec is not None and needs_human_act(rec):
                # a timeout never applies a human act nobody did (the next session would
                # hit the same blocker); it parks instead. The recommendation is shown.
                rec = park
            # 2026-10-27 one stop protocol, narrowed by audit D: only a `user_action`
            # blocker (a person must act) waits for the owner with no timeout
            if str(ev.get("blocker_kind") or "") == "user_action":
                rec = None
            # self-driving part 1: an automation decision never times out to a stop;
            # its default is the capped self-repair (sweep -> self_repair_plan)
            if rec is not None and cat == AUTOMATION and is_park(rec) and self_repair_on(P):
                rec = SELF_REPAIR
    try:
        timeout = int(auto_get(P, "decision_timeout_minutes") or 0)
    except Exception:                                            # noqa: BLE001
        timeout = 0
    minutes = timeout if rec is not None else 0
    return {"decision_class": cls,
            "recommended": json.dumps(rec) if rec is not None else None,
            "timeout_minutes": minutes, "category": cat}


def due(store, now: float) -> list[dict]:
    try:
        return store.q("SELECT decision_id, project_id, task_id, kind, recommended, "
                       "timeout_minutes, category, evidence, question FROM decisions "
                       "WHERE status='open' AND "
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


def _lead_hands_back(c, P, d: dict, case: dict) -> bool:
    """2026-10-28: an automation decision past its deadline whose lead case never made
    its call (DEFERRED on readiness / quota / admission, or not started): the case is
    handed back (lead.hand_back cancels it) and the self-repair answers now, instead
    of following the case's own multi-hour bound."""
    if decision_category(d) != AUTOMATION or case.get("state") not in ("DEFERRED",
                                                                        "DIAGNOSING"):
        return False
    L = getattr(c, "lead", None)
    fn = getattr(L, "hand_back", None)
    if not callable(fn):
        return False
    try:
        full = L.case(case["case_id"])
        if not any(x["decision_id"] == d["decision_id"]
                   for x in L.handback_decisions(P, full)):
            return False
        return bool(fn(P, full, f"decision {d['decision_id']} timed out while the lead "
                                f"case was {case.get('state')} without a lead call",
                       sweep=False))
    except Exception:                                             # noqa: BLE001
        return False


# -- self-repair (self-driving part 1) -------------------------------------------------

LEAD_ROUND = "authorize another bounded round"          # lead.Lead.ESCALATION_OPTIONS[1]
REVERIFY = "self-repair: verify the head again, then review it"
WINDOW_SECONDS = 86400.0                                 # "per day" = a rolling 24 h
LOG_KEY = "self_repair_log"                              # task data: the steps taken
FRONTIER_KEY = "self_repair_frontier"                    # task data: the frontier session
# a lead case's trigger class (engine._stop cls) -> the budget counter it exhausted
BUDGET_TRIGGERS = {"attempts_exhausted": "attempt_ordinary", "sessions_exhausted": "sessions",
                   "review_budget": "review_calls"}
TOOLING_TRIGGERS = ("session_", "readiness_", "review_invalid", "review_errors",
                    "checks_unknown", "fast_lane_gate_error", "studio_calls_error", "config",
                    "transient", "visual_frames_missing", "decision_review_access",
                    "decision_implementer_blocked", "infra")
TOOLING_REASONS = ("upgrade_", "repair_unavailable", "lead_permissions", "readiness",
                   "unknown_action")
CAP_KEYS = {"attempt_ordinary": "self_repair_max_attempts_per_day",
            "sessions": "self_repair_max_sessions_per_day",
            "review_calls": "self_repair_max_reviews_per_day"}
CONTINUE_BRIEF = ("Self-repair (automatic policy, no operator): the {counter} budget was "
                  "extended by one ({ref}). Continue the sprint from the committed work and "
                  "the findings below.\n{findings}")
DIFFERENT_APPROACH = ("Self-repair (automatic policy, no operator): the same findings came back "
                      "after the earlier repairs. Take a materially different approach in this "
                      "fresh session: re-read the failing evidence from scratch, question the "
                      "earlier hypothesis and change the method instead of retrying the same "
                      "edit. The findings that keep coming back:\n{findings}")
FRONTIER_BRIEF = ("Self-repair (automatic policy, no operator), last step: this session runs on "
                  "the frontier implementer ({persona}) because a different approach on the "
                  "local model did not clear the findings. Same scope, rules, gates and commit "
                  "rules. The findings that keep coming back:\n{findings}")


def policy_cap(P, counter: str) -> int:
    """Policy extensions of `counter` a task may get per rolling day (0 = none)."""
    key = CAP_KEYS.get(counter)
    if not key or P is None or not self_repair_on(P):
        return 0
    try:
        return max(0, int(_auto(P, key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _evidence(d: dict) -> dict:
    ev = (d or {}).get("evidence")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except ValueError:
            ev = {}
    return ev if isinstance(ev, dict) else {}


def repair_trigger(c, d: dict) -> tuple[str, str]:
    """("budget", counter) | ("tooling", "") | ("findings", "") for a decision."""
    kind = str(d.get("kind") or "")
    ev = _evidence(d)
    if kind in ("review_access", "implementer_blocked"):
        return "tooling", ""
    if kind != "lead_escalation":
        return "findings", ""
    case = None
    if ev.get("case_id"):
        try:
            case = c.lead.case(str(ev["case_id"]))
        except Exception:                                         # noqa: BLE001
            case = None
    tcls = str(((case or {}).get("trigger") or {}).get("class") or "")
    if tcls in BUDGET_TRIGGERS:
        return "budget", BUDGET_TRIGGERS[tcls]
    reason = str(ev.get("reason") or "")
    if reason.startswith(TOOLING_REASONS) or tcls.startswith(TOOLING_TRIGGERS):
        return "tooling", ""
    return "findings", ""


def _refused_items(c, d: dict) -> list[str]:
    """The items of the task's latest refused submission (the engine's
    `submission_refused` event): what the verifier still refuses right now."""
    try:
        rows = c.store.q("SELECT detail FROM events WHERE project_id=? AND task_id=? AND "
                         "kind='submission_refused' ORDER BY id DESC LIMIT 1",
                         (d["project_id"], d["task_id"]))
        items = (json.loads(rows[0]["detail"]) or {}).get("items") or [] if rows else []
    except Exception:                                             # noqa: BLE001
        return []
    out = []
    for it in items:
        x = (it or {}).get("item") or {} if isinstance(it, dict) else {}
        s = " ".join(str(v) for v in (x.get("title"), x.get("reason") or x.get("detail"))
                     if v)
        s = s or str(x.get("check_id") or "")
        if s and s not in out:
            out.append(s)
    return out


def _finding_text(d: dict, t: dict, cap: int = 2400, c=None) -> str:
    """The concrete strategy material: what the verifier still refuses (the latest
    refused submission, the repeated findings of the decision), the strategy that
    did not clear them, then the latest review's findings."""
    ev = _evidence(d)
    lines = []
    refused = _refused_items(c, d) if c is not None else []
    for s in refused[:4]:
        lines.append("- still refused: " + s[:400])
    for r in (ev.get("repeats") or [])[:4]:
        s = str((r or {}).get("text") or "").strip() if isinstance(r, dict) else ""
        if s and not any(s[:120] in x for x in lines):
            lines.append(f"- repeats x{(r or {}).get('count') or 2}: " + s[:400])
    if ev.get("last_strategy"):
        lines.append("- the earlier strategy that did not clear them: "
                     + str(ev["last_strategy"]).split("\n", 1)[0][:300])
    nf = 0
    for f in ((t or {}).get("data") or {}).get("findings") or []:
        s = str(f).strip()
        if s and not s.startswith("Self-repair"):
            lines.append("- " + s[:400])
            nf += 1
        if nf >= 4:
            break
    for k in ("trigger", "hypothesis"):
        if ev.get(k):
            lines.append(f"- {k}: {str(ev[k])[:400]}")
    if not lines:
        lines.append("- " + str(d.get("question") or "")[:600])
    return "\n".join(lines)[:cap]


def repair_log(t: dict, now: float, window: float = WINDOW_SECONDS) -> list[dict]:
    """The task's self-repair steps in the last `window` seconds."""
    return [x for x in (((t or {}).get("data") or {}).get(LOG_KEY) or [])
            if isinstance(x, dict) and now - float(x.get("ts") or 0) < window]


def record_step(c, project: str, task: str, step: str, detail: dict) -> None:
    """One self-repair step: a task-data log row (the caps count it) and an event."""
    now = c.clock()
    t = c.store.task(project, task) or {"data": {}}
    log = [x for x in ((t.get("data") or {}).get(LOG_KEY) or [])
           if isinstance(x, dict) and now - float(x.get("ts") or 0) < 7 * WINDOW_SECONDS]
    row = {"step": step, "ts": now, **{k: v for k, v in detail.items()
                                      if k in ("decision_id", "kind", "counter", "head",
                                               "trigger", "persona")}}
    c.store.merge_data(project, task, **{LOG_KEY: (log + [row])[-40:]})
    c.store.event(project, task, "self_repair", detail={"step": step, **detail,
                                                       "category": AUTOMATION})


def _frontier_persona(P, tid: str) -> str:
    """The [frontier_primary] persona when it may implement one self-repair session
    (it differs from the local implementer and from every reviewer), else ''."""
    if not _auto(P, "self_repair_frontier", True):
        return ""
    try:
        from .frontier import FrontierPhase
        fp = str(FrontierPhase.settings(P).get("persona") or "")
        if not fp or fp == P.profile.persona("implementer", tid):
            return ""
        for role in ("reviewer", "deep_reviewer"):
            if role in P.profile.roles and P.profile.persona(role, tid) == fp:
                return ""
    except Exception:                                             # noqa: BLE001
        return ""
    return fp


def _answer(kind: str, options: list, how: str, text: str, sr: dict):
    """The concrete answer the decision's own consumer understands."""
    sr = {"policy": "self_repair", **sr}
    if how == "stop":
        return {"option": PARK, "stop": True, "reason": text[:500], "self_repair": sr}
    if kind == "lead_escalation":
        if how == "round":
            return {"option": LEAD_ROUND, "note": text[:2000], "self_repair": sr}
        if how == "reverify":
            return {"option": REVERIFY, "self_repair": {**sr, "action": "reverify"}}
        return {"strategy": text[:4000], "self_repair": sr}
    if kind == "review_access" and how in ("round", "retry"):
        return {"option": _option(options, "access fixed") or "access fixed: review again",
                "self_repair": sr}
    return {"strategy": text[:4000], "self_repair": sr}


def self_repair_plan(c, P, d: dict) -> dict:
    """The capped self-repair for a timed-out automation decision:
    {"step", "answer", ...}, {"defer_until": ts} (a tooling backoff), or the quiet
    park {"step": "exhausted", "answer": <stop>, "why"}."""
    from .obligations import POLICY_ACTOR, LedgerRefused
    did, kind = d["decision_id"], str(d.get("kind") or "")
    pid, tid = d["project_id"], d["task_id"]
    full = c.obligations.decision(did) or d
    options = list(full.get("options") or []) if isinstance(full.get("options"), list) else []
    t = c.store.task(pid, tid) or {"data": {}}
    now = c.clock()
    recent = repair_log(t, now)
    trig, counter = repair_trigger(c, full)
    text = _finding_text(full, t, c=c)
    sr = {"decision_id": did, "trigger": trig}

    def exhausted(why: str) -> dict:
        reason = f"self-repair exhausted ({trig}): {why}"[:500]
        return {"step": "exhausted", "why": reason,
                "answer": _answer(kind, options, "stop", reason, {**sr, "exhausted": True})}

    if trig == "budget":
        granted, why = [], ""
        extra = ["sessions"] if counter == "attempt_ordinary" else []
        for ctr in [counter] + extra:
            if ctr != counter:
                try:
                    lim = c.obligations.effective_limits(P, t)["sessions"]
                    if lim["remaining"] > 0:
                        continue
                except Exception:                                 # noqa: BLE001
                    continue
            ref = f"policy:{ctr}:{did}"
            try:
                c.obligations.authorize(pid, tid, ctr, 1, f"self-repair of {kind} {did}: "
                                        f"+1 {ctr} (policy)", POLICY_ACTOR, ref)
                granted.append(ctr)
            except LedgerRefused as e:
                why = why or str(e)
                if ctr == counter:
                    break
        if counter not in granted:
            return exhausted(why or f"no {counter} extension allowed")
        how = "reverify" if counter == "review_calls" else "strategy"
        brief = CONTINUE_BRIEF.format(counter=counter, ref=f"policy:{counter}:{did}",
                                      findings=text)
        return {"step": "budget_extension", "counters": granted, "counter": counter,
                "answer": _answer(kind, options, how, brief, {**sr, "counters": granted})}
    if trig == "tooling":
        retries = sorted(float(x.get("ts") or 0) for x in recent if x.get("step") == "retry")
        cap = int(_auto(P, "self_repair_max_retries_per_day", 3) or 0)
        if len(retries) >= cap:
            return exhausted(f"{len(retries)}/{cap} tooling retries in the last day")
        if retries:
            wait = 60.0 * float(_auto(P, "self_repair_retry_minutes", 10) or 0) * \
                (2 ** (len(retries) - 1))
            if now < retries[-1] + wait:
                return {"step": "retry", "defer_until": retries[-1] + wait}
        brief = ("Self-repair (automatic policy, no operator): retry after a tooling / "
                 "infrastructure stop.\n" + text)
        return {"step": "retry", "answer": _answer(kind, options, "round", brief,
                                                   {**sr, "retry": len(retries) + 1})}
    used = {x.get("step") for x in recent}
    if kind == "lead_escalation" and "lead_repair" not in used:
        return {"step": "lead_repair", "answer": _answer(
            kind, options, "round", "Self-repair: one more lead repair round for these "
            "findings:\n" + text, sr)}
    if "different_approach" not in used:
        return {"step": "different_approach", "answer": _answer(
            kind, options, "strategy", DIFFERENT_APPROACH.format(findings=text), sr)}
    fp = _frontier_persona(P, tid) if "frontier" not in used else ""
    if fp:
        c.store.merge_data(pid, tid, **{FRONTIER_KEY: {"persona": fp, "decision_id": did,
                                                       "ts": now}})
        return {"step": "frontier", "persona": fp, "answer": _answer(
            kind, options, "strategy", FRONTIER_BRIEF.format(persona=fp, findings=text),
            {**sr, "persona": fp})}
    return exhausted("lead repair, different approach and frontier session used in the last "
                     "day" if "frontier" in used else
                     "lead repair and different approach used; no frontier persona available")


def _apply_self_repair(c, P, d: dict, now: float):
    """The answer the sweep applies to a timed-out automation decision, or None when it
    waits (a tooling backoff moved its deadline). Journals the step."""
    plan = self_repair_plan(c, P, d)
    if plan.get("defer_until"):
        c.store.x("UPDATE decisions SET deadline_ts=? WHERE decision_id=?",
                  (float(plan["defer_until"]), d["decision_id"]))
        c.store.event(d["project_id"], d["task_id"], "self_repair_deferred", detail={
            "decision_id": d["decision_id"], "until": plan["defer_until"],
            "why": "tooling retry backoff", "category": AUTOMATION})
        return None
    answer = plan["answer"]
    from .obligations import _j
    c.store.x("UPDATE decisions SET recommended=? WHERE decision_id=?",
              (_j(answer), d["decision_id"]))
    record_step(c, d["project_id"], d["task_id"], plan["step"], {
        "decision_id": d["decision_id"], "kind": d["kind"],
        "trigger": (answer.get("self_repair") or {}).get("trigger") if isinstance(answer, dict)
        else "", "counter": plan.get("counter") or "", "persona": plan.get("persona") or "",
        "why": plan.get("why") or "", "exhausted": plan["step"] == "exhausted"})
    return answer


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
    try:
        c.obligations                    # migrates the decisions table (category column)
    except Exception:                                             # noqa: BLE001
        pass
    out = []
    for d in due(c.store, now):
        P = (getattr(c, "projects", None) or {}).get(d["project_id"])
        if P is None:
            continue
        case = _lead_handling(c, d)
        if case and _lead_hands_back(c, P, d, case):
            case = None                  # the case could not run: the self-repair answers
        if case:
            # 0.145.2: a lead case is still working this very stop. The timeout
            # default (often "park the sprint") fired mid-repair and parked
            # GF-NAME1 and GF-GATE1 on 2026-10-27. The deadline follows the case's
            # own bound; once the lead gives up or the case closes, it applies.
            # 2026-10-31 GF-RT1: re-checked every 5 min while the case works, never moved to
            # the case's own multi-hour bound (a case that closed minutes later left the
            # decision waiting until that bound: 4 h)
            new = now + 300
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
            # (a decision opened before 2026-10-27 may still carry one): a human act is
            # never applied by a timeout; audit D: it parks instead of waiting forever
            c.store.x("UPDATE decisions SET recommended=? WHERE decision_id=?",
                      (json.dumps(PARK), d["decision_id"]))
            c.store.event(d["project_id"], d["task_id"], "decision_timeout_skipped", detail={
                "decision_id": d["decision_id"], "recommended": answer,
                "why": "the recommended option needs a human act; the timeout parks instead"})
            answer = PARK
        cat = decision_category(d)
        repair = None
        if cat == AUTOMATION and self_repair_on(P) and (answer == SELF_REPAIR or is_park(answer)):
            # self-driving part 1: an automation decision never times out to a stop; the
            # capped self-repair answers it (a stop only once the caps are exhausted)
            try:
                answer = _apply_self_repair(c, P, d, now)
            except Exception as e:                               # noqa: BLE001
                c.store.event(d["project_id"], d["task_id"], "warning", detail={
                    "self_repair": f"{d['decision_id']}: {type(e).__name__}: {e}"[:300]})
                c.store.x("UPDATE decisions SET deadline_ts=? WHERE decision_id=?",
                          (now + 600, d["decision_id"]))
                continue
            if answer is None:
                continue                 # a tooling backoff: the deadline moved
            repair = (answer.get("self_repair") or {}) if isinstance(answer, dict) else {}
        elif answer == SELF_REPAIR:
            # self_repair switched off after the decision opened: the old default
            from .obligations import _j
            c.store.x("UPDATE decisions SET recommended=? WHERE decision_id=?",
                      (_j(PARK), d["decision_id"]))
            answer = PARK
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
                  "timeout_minutes": d.get("timeout_minutes"), "category": cat,
                  "note": "the recommended option was applied after the timeout; answer a "
                          "superseding decision to change it"}
        c.store.event(d["project_id"], d["task_id"], "decision_timeout_applied", detail=detail)
        if repair is not None:
            # a self-repair is not news: a digest line, never an alert (the quiet park's
            # own sprint_parked notice, engine._park_summary, carries the alert class
            # self_repair_exhausted)
            step = "parked quietly, self-repair exhausted" if repair.get("exhausted") \
                else "self-repair applied"
            c.store.event(d["project_id"], d["task_id"], "operator_notice", detail={
                "text": f"{d['task_id']}: decision {d['decision_id']} ({d['kind']}) timed "
                        f"out after {d.get('timeout_minutes')} min; {step} "
                        f"({repair.get('trigger') or 'automation'})"[:500],
                **detail, "digest": True, "self_repair": repair})
        else:
            c.store.event(d["project_id"], d["task_id"], "operator_notice", detail={
                "text": f"{d['task_id']}: decision {d['decision_id']} ({d['kind']}) timed out "
                        f"after {d.get('timeout_minutes')} min; applied the recommended "
                        f"option: {answer}"[:500], **detail})
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

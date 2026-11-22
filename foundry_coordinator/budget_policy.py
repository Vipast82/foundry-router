"""Budgets and time limits follow plan progress (COORDINATOR_AUTOMATION_NEEDS items 9, 10).

In the week of 2026-11-14 the operator asked the owner, and ran `unblock
--reset-attempts`, for FIN-2a, FIN-2c, FIN-4a (twice) and POL-7 when each ran out of
sessions ("29/32/33/59 sessions") or of the three policy extensions a day; and raised
`max_task_minutes` by hand five times for POL-7 (1440 -> 1920 -> 2400 -> 2640 -> 3360),
each with a restart. Every time the sprint was still ticking plan steps, and every time
the answer was yes.

  renewal    a sprint that runs out of sessions, attempts or review calls while plan steps
             were ticked since its last renewal gets its budget back by itself: every
             counter is brought to its base room again (what an operator reset does to
             the ledger, and nothing else: no attempt number, clock or Lead round is
             touched), [automation] budget_renewals_per_day times per rolling day
             (default 1, 0 = never). No step ticked since the last renewal, or the day's
             renewals used: the stop goes on as before (the Lead, then the owner).
  time       `max_task_minutes` stays the project's limit; a sprint over it that ticked a
             step since its last extension gets extra minutes of its own
             ([automation] task_minutes_auto_extend): what its open steps need at its own
             pace, never more than the base limit again per rolling day. The extra lives
             on the sprint (task data `task_minutes_extra`) and ends with it, so there is
             no default to put back. No step ticked: the stop goes on as before, and its
             reason carries the suggested amount. `extend_time` (CLI `extend-time`) is
             the owner's own grant, live, with no restart.

A ledger row of a renewal is `renewal:<counter>:<key>` by RENEWAL_ACTOR; the ledger itself
refuses it past the daily cap or without the plan progress (obligations._require_renewal).
Each renewal and extension is an `operator_notice` in the digest (kind budget_renewed /
time_extended). Projects without [lifecycle] obligations keep their old counters: nothing
here applies to them.
"""

from __future__ import annotations

import json
import math
from typing import Optional

from .automation import get as auto_get

DAY_S = 86400.0
RENEWAL_COUNTERS = ("sessions", "attempt_ordinary", "review_calls")
MIN_EXTENSION = 60                 # minutes
CLOSE_MARGIN = 60                  # minutes on top of the open steps: the close-out


def _steps_of(row: dict) -> list[str]:
    from .adapters.base import checklist_transitions
    raw = row.get("checklist")
    try:
        cl = json.loads(raw) if isinstance(raw, str) and raw else raw
    except ValueError:
        cl = None
    if not isinstance(cl, dict):
        return []
    try:
        return list(checklist_transitions(cl.get("before"), cl.get("after")))
    except Exception:                                            # noqa: BLE001
        return []


def steps_since(c, pid: str, tid: str, since: float) -> list[str]:
    """Plan steps this sprint's sessions ticked after `since` (the session marks' checklist
    before / after; a mark is stamped when its session first went idle)."""
    try:
        rows = c.store.q("SELECT checklist, ts FROM progress_marks WHERE project_id=? AND "
                         "task_id=? AND ts>? ORDER BY id", (pid, tid, float(since or 0)))
    except Exception:                                            # noqa: BLE001
        return []
    out: list[str] = []
    for r in rows:
        out += _steps_of(dict(r))
    return out


# ---------------------------------------------------------------------------- renewal

def renewal_keys(c, pid: str, tid: str, since: float = 0.0) -> dict:
    """{renewal key: its time} for the task's renewals after `since`."""
    from .obligations import RENEWAL_REF
    out: dict = {}
    try:
        rows = c.store.q("SELECT decision_id, ts FROM budget_ledger WHERE project_id=? AND "
                         "task_id=? AND decision_id LIKE ? AND ts>=?",
                         (pid, tid, RENEWAL_REF + "%", float(since)))
    except Exception:                                            # noqa: BLE001
        return out
    for r in rows:
        parts = str(r["decision_id"]).split(":", 2)
        if len(parts) == 3:
            out[parts[2]] = max(float(r["ts"] or 0), out.get(parts[2], 0.0))
    return out


def progress_floor(c, t: dict, skip_key: str = "") -> float:
    """From when plan steps count for the next renewal: the last renewal (not `skip_key`,
    the one being written), else the start of the task's budget."""
    from .obligations import budget_epoch
    keys = renewal_keys(c, t["project_id"], t["task_id"])
    keys.pop(skip_key, None)
    return max([float(budget_epoch(t) or 0)] + list(keys.values()))


def renewal_ref_ok(c, P, tid: str, ref: str) -> bool:
    """The ledger's question for a `renewal:<counter>:<key>` row: did the sprint tick a
    plan step since its last renewal?"""
    t = c.store.task(P.id, tid)
    key = ref.split(":", 2)[2] if ref.count(":") >= 2 else ""
    return bool(t and key and steps_since(c, P.id, tid, progress_floor(c, t, key)))


def try_renew(c, P, t: dict, counter: str) -> bool:
    """`counter` ran out. True = the budget was renewed (the caller reads the limits again
    and goes on); False = not this time (the caller stops as before)."""
    from .obligations import RENEWAL_ACTOR, RENEWAL_REF, LedgerRefused
    pid, tid = P.id, t["task_id"]
    cap = int(auto_get(P, "budget_renewals_per_day") or 0)
    if cap <= 0 or not c._lc(P, "obligations"):
        return False
    now = c.clock()
    steps = steps_since(c, pid, tid, progress_floor(c, t))
    today = renewal_keys(c, pid, tid, now - DAY_S)
    why = ""
    if not steps:
        why = "no plan step was ticked since the last renewal"
    elif len(today) >= cap:
        why = f"{len(today)}/{cap} renewals in the last day"
    if why:
        _once(c, pid, tid, "budget_renewal_skipped", f"{counter}|{why}", {
            "counter": counter, "why": why, "steps_ticked": len(steps)})
        return False
    key = str(int(now))
    got: dict = {}
    before = c.obligations.effective_limits(P, t)
    names = {v["counter"]: k for k, v in before.items() if isinstance(v, dict) and "counter" in v}
    for ctr in RENEWAL_COUNTERS:
        lim = before[names[ctr]]
        n = int(lim["used"]) - int(lim["authorized"])        # what brings the room to its base
        if n <= 0:
            continue
        try:
            c.obligations.reset(P, t, ctr, f"renewal: {len(steps)} plan step(s) ticked since "
                                           f"the last one; {counter} ran out",
                                RENEWAL_ACTOR, f"{RENEWAL_REF}{ctr}:{key}")
        except LedgerRefused as e:
            if ctr == counter:
                _once(c, pid, tid, "budget_renewal_skipped", f"{counter}|refused", {
                    "counter": counter, "why": str(e)[:300], "steps_ticked": len(steps)})
                return False
            continue
        got[ctr] = n
    if counter not in got:
        return False
    text = (f"{tid}: budget renewed by itself ({', '.join(f'+{n} {k}' for k, n in got.items())}"
            f"): {counter} ran out and {len(steps)} plan step(s) were ticked since the last "
            f"renewal ({'; '.join(steps[:3])}). Renewal {len(today) + 1} of {cap} in a day.")
    c.store.event(pid, tid, "operator_notice", detail={
        "text": text[:900], "kind": "budget_renewed", "digest": True, "counters": got,
        "ran_out": counter, "steps": steps[:12], "renewals_today": len(today) + 1,
        "per_day": cap})
    return True


def _once(c, pid: str, tid: str, kind: str, key: str, detail: dict) -> None:
    """One event per (task, key) per hour: the stop that follows is retried by other rules,
    and each retry asks again."""
    seen = c.__dict__.setdefault("_budget_policy_seen", {})
    now = c.clock()
    if now - seen.get((pid, tid, kind, key), -1e18) < 3600:
        return
    seen[(pid, tid, kind, key)] = now
    c.store.event(pid, tid, kind, detail=detail)


# ---------------------------------------------------------------------------- time

def minutes_limit(P, t: dict) -> int:
    """The task's own active-minutes limit: the project's max_task_minutes plus the
    minutes granted to this sprint (0 = no limit)."""
    base = int(P.profile.limit("max_task_minutes", 0) or 0)
    if not base:
        return 0
    return base + int(((t or {}).get("data") or {}).get("task_minutes_extra") or 0)


def suggest_minutes(c, P, t: dict, active_s: float) -> tuple:
    """(minutes, why): what the open plan steps need at the sprint's own pace (active
    minutes so far per step done) plus the close-out; without a plan checklist, a quarter
    of the base limit."""
    base = int(P.profile.limit("max_task_minutes", 0) or 0)
    fallback = max(MIN_EXTENSION * 2, base // 4)
    try:
        spec = P.adapter.describe(t["task_id"])
        head = P.ws.head()
        fn = getattr(P.adapter, "pending_build_steps", None)
        pending = fn(spec, head, P.ws) if callable(fn) else None
        total = c._build_total(P, spec, head)
    except Exception:                                            # noqa: BLE001
        pending, total = None, None
    if pending is None or not total:
        return fallback, "no plan checklist to measure: a quarter of the limit"
    left = len(pending)
    done = int(total) - left
    if left <= 0:
        return MIN_EXTENSION, "every plan step is ticked: the close-out only"
    if done <= 0:
        return fallback, f"{left} plan step(s) open, none done yet: a quarter of the limit"
    pace = (active_s / 60.0) / done
    need = int(math.ceil(left * pace)) + CLOSE_MARGIN
    return max(MIN_EXTENSION, need), (f"{left} of {total} plan steps open at "
                                      f"{int(round(pace))} min a step, plus the close-out")


def _grants(t: dict) -> list:
    g = ((t or {}).get("data") or {}).get("task_minutes_grants")
    return [x for x in g if isinstance(x, dict)] if isinstance(g, list) else []


def _grant(c, P, t: dict, minutes: int, by: str, why: str, steps_left=None) -> dict:
    cur = c.store.task(P.id, t["task_id"]) or t
    d = dict(cur.get("data") or {})
    g = {"ts": c.clock(), "minutes": int(minutes), "by": by, "why": why[:300]}
    extra = int(d.get("task_minutes_extra") or 0) + int(minutes)
    c.store.merge_data(P.id, t["task_id"], task_minutes_extra=extra,
                       task_minutes_grants=(_grants(cur) + [g])[-40:])
    return {**g, "extra": extra}


def try_extend_time(c, P, t: dict, active_s: float) -> str:
    """The sprint is over its minutes. '' = it got more by itself (the caller goes on);
    else why not (the stop's reason says so)."""
    pid, tid = P.id, t["task_id"]
    base = int(P.profile.limit("max_task_minutes", 0) or 0)
    if not base:
        return "no limit"
    if not auto_get(P, "task_minutes_auto_extend"):
        return "extending by itself is switched off (task_minutes_auto_extend)"
    now = c.clock()
    grants = _grants(t)
    auto = [g for g in grants if g.get("by") == "policy"]
    today = sum(int(g.get("minutes") or 0) for g in auto if now - float(g.get("ts") or 0) < DAY_S)
    since = max([float(g.get("ts") or 0) for g in grants] + [float(t.get("started_ts") or 0)])
    steps = steps_since(c, pid, tid, since)
    want, basis = suggest_minutes(c, P, t, active_s)
    why = ""
    if not steps:
        why = "no plan step was ticked since the last extension"
    elif base - today < MIN_EXTENSION:
        why = f"{today} of {base} extra minutes already granted in the last day"
    if why:
        _once(c, pid, tid, "time_extension_skipped", why, {
            "why": why, "suggested_minutes": want, "basis": basis})
        return why
    add = min(want, base - today)
    g = _grant(c, P, t, add, "policy", f"{len(steps)} plan step(s) ticked; {basis}")
    c.store.event(pid, tid, "operator_notice", detail={
        "text": (f"{tid}: {add} more minutes by itself (its own limit is now "
                 f"{base + g['extra']} active minutes; the project's stays {base}). "
                 f"{len(steps)} plan step(s) were ticked since the last extension; {basis}."
                 )[:900],
        "kind": "time_extended", "digest": True, "minutes": add, "limit": base + g["extra"],
        "base": base, "steps": steps[:12], "basis": basis, "granted_today": today + add})
    return ""


def stop_reason(c, P, t: dict, active_s: float, why: str = "") -> str:
    """The task_minutes stop's reason, with what to give it: the owner is asked once, with
    an amount. It starts as it always did ("over max_task_minutes (N active minutes)")."""
    limit = minutes_limit(P, t)
    want, basis = suggest_minutes(c, P, t, active_s)
    return (f"over max_task_minutes ({limit} active minutes)"
            + (f"; not extended by itself: {why}" if why else "")
            + f". If it should go on: {want} more minutes ({basis}); `extend-time {P.id} "
              f"{t['task_id']} --minutes {want}` grants them to this sprint only")

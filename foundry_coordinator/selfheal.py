"""The self-repair sweep (SELF_DRIVING_PLAN part 2, 2026-10-28).

What the chat watch did by hand, inside the coordinator: once a minute per project,
bounded by [automation] self_heal_max_actions_per_task_per_day, every action a
`self_heal` event {rule, task, action, evidence, digest: true} (the daily digest
lists them). No model call is made here.

  1 dead_session    the store records an implementer / Lead session as running but
                    Kilo reports it missing or ended with an error for
                    self_heal_dead_session_minutes: the session is ended; an
                    implementer gets a free retry (_session_failed(free=True)); a
                    Lead call is failed uncounted, so the Lead starts a new call
                    (lead ses_e7f5e8ca1 died at 23:07 while the store said `created`;
                    case rc-f24672b33aef5998 waited for hours)
  2 stuck_case      an open Lead case with no live call and no progress for
                    self_heal_stuck_case_minutes: cancelled with a note; the task then
                    follows the automation default (the engine detaches the case)
  3 fixed_release   a BLOCKED task whose blocker matches a signature a newer running
                    release fixes (FIXED_SIGNATURES), blocked before that release was
                    first seen running: re-verified once, no attempt charged
  4 stale_lock      the gate runner lock with a dead pid, a studio_gate studio.lock
                    with no live runner, coordinator locks held by a task that can
                    hold nothing (stopped, no Lead call / check / run): released
  5 hold            a PAUSED task whose hold owner is gone or whose hold expired:
                    resumed. A PAUSED task with no owner for
                    self_heal_unowned_pause_hours: one operator notice (alert class
                    pause_forgotten); an unowned operator pause is never resumed
                    (HK-CLOSE1, "short hold ... resumed right after", paused 16 h)

Rule 6 (an operator unblock with reset resets EVERY budget counter) lives in
Coordinator.unblock; rule 3 backfills it for a block made by an older release.

Holds: task data `hold = {owner, reason, expires_ts, set_ts, actor}` set by
Coordinator.hold / pause(owner=, minutes=). Owner kinds checked here: `pid:<n>`,
`session:<id>`, `task:<project>/<task>`, `case:<id>`; others can be registered
with register_hold_owner(kind, fn) (fn(c, ref) -> alive?). An owner nobody can
check only expires.
"""

from __future__ import annotations

import os
import re
import time
from typing import Callable, Optional

from . import states as S

EVERY_S = 60.0
DAY_S = 86400.0

# blocker signature -> the release that fixes it. A BLOCKED task matching one, blocked
# before that release (or a newer one) was first seen running, is re-verified once.
FIXED_SIGNATURES = {
    # v0.155.1: an unblock with reset left the review budget used up
    "review_budget_after_reset": "0.155.1",
    # v0.152.2: a task's own leftover file failed coordinator.unrelated_preserved
    "unrelated_preserved_task_leftover": "0.152.2",
    # v0.155.0: au.gate_bound_to_head failed when only docs/progress / docs/memory / .plan moved
    "gate_binding_docs_only": "0.155.0",
}
SIGNATURE_PATTERNS = {
    "review_budget_after_reset": re.compile(r"review budget used", re.I),
    "unrelated_preserved_task_leftover": re.compile(
        r"unrelated_preserved|pre-existing uncommitted changes were modified", re.I),
    "gate_binding_docs_only": re.compile(r"gate_bound_to_head", re.I),
}

_OWNER_CHECKS: dict[str, Callable] = {}


def register_hold_owner(kind: str, fn: Callable) -> None:
    """Teach the sweep a hold owner kind: fn(coordinator, ref) -> True while the
    owner `kind:ref` is alive, False once it is gone (None = can't tell)."""
    _OWNER_CHECKS[str(kind)] = fn


def _vt(v: str) -> tuple:
    out = []
    for p in str(v or "").strip().lstrip("v").split("."):
        m = re.match(r"\d+", p)
        out.append(int(m.group(0)) if m else 0)
    return tuple(out + [0] * (3 - len(out)))


def _running_version() -> str:
    from . import __version__
    return __version__


def _setting(P, key):
    from .automation import get
    return get(P, key)


# ------------------------------------------------------------------ bookkeeping

def _actions_today(c, pid: str, tid: str) -> int:
    return int(c.store.q("SELECT COUNT(*) AS n FROM events WHERE project_id=? AND task_id=? "
                         "AND kind='self_heal' AND ts>=?",
                         (pid, tid, time.time() - DAY_S))[0]["n"] or 0)


def _allowed(c, P, tid: str) -> bool:
    cap = int(_setting(P, "self_heal_max_actions_per_task_per_day") or 0)
    if _actions_today(c, P.id, tid) < cap:
        return True
    if not c.store.q("SELECT 1 FROM events WHERE project_id=? AND task_id=? AND "
                     "kind='self_heal_exhausted' AND ts>=?", (P.id, tid, time.time() - DAY_S)):
        c.store.event(P.id, tid, "self_heal_exhausted", detail={
            "task": tid, "cap": cap, "digest": True,
            "note": "self-repair cap reached for today; the task is left as it is"})
    return False


def _record(c, P, tid: str, rule: str, action: str, evidence: dict) -> dict:
    detail = {"rule": rule, "task": tid, "action": action, "evidence": evidence,
              "digest": True}
    c.store.event(P.id, tid, "self_heal", detail=detail)
    try:
        c.log(f"{P.id}/{tid}: self-heal {rule}: {action}")
    except Exception:                                            # noqa: BLE001
        pass
    return detail


# ------------------------------------------------------------------ entry point

def sweep(c, P, force: bool = False) -> list[dict]:
    """One bounded self-repair pass over project P (at most once a minute unless
    force). Never raises; returns the actions taken."""
    try:
        if not _setting(P, "self_heal"):
            return []
        last = c.__dict__.setdefault("_selfheal_at", {})
        now = c.clock()
        if not force and now - last.get(P.id, -1e18) < EVERY_S:
            return []
        last[P.id] = now
        note_version(c)
    except Exception as e:                                       # noqa: BLE001
        _log(c, f"{P.id}: self-heal sweep failed: {type(e).__name__}: {e}")
        return []
    out: list[dict] = []
    for name, fn in (("dead_session", dead_sessions), ("stuck_case", stuck_cases),
                     ("fixed_release", fixed_releases), ("stale_lock", stale_locks),
                     ("hold", holds)):
        try:
            out += fn(c, P) or []
        except Exception as e:                                   # noqa: BLE001
            _log(c, f"{P.id}: self-heal {name} failed: {type(e).__name__}: {e}")
    return out


def _log(c, msg: str) -> None:
    try:
        c.log(msg)
    except Exception:                                            # noqa: BLE001
        pass


# ------------------------------------------------------------------ 1 dead sessions

LIVE_SESSION_STATUSES = ("created", "prompted", "running", "busy")


def dead_sessions(c, P) -> list[dict]:
    from .lead import OPEN_STATES
    out = []
    now = c.clock()
    grace = float(_setting(P, "self_heal_dead_session_minutes") or 0) * 60
    seen = c.__dict__.setdefault("_selfheal_dead", {})
    q = ",".join("?" * len(LIVE_SESSION_STATUSES))
    rows = c.store.q(f"SELECT session_id FROM sessions WHERE project_id=? AND ended_ts IS NULL "
                     f"AND role IN ('implementer','lead') AND status IN ({q})",
                     (P.id, *LIVE_SESSION_STATUSES))
    L = c._lead_any() if rows else None
    for r in rows:
        sess = c.store.session(r["session_id"])
        if not sess:
            continue
        sid, tid = sess["session_id"], sess["task_id"]
        t = c.store.task(P.id, tid)
        if t is None or t["state"] in S.TERMINAL:
            continue
        case, act, directory = None, None, P.profile.workspace
        if sess["role"] == "lead":
            if L is None:
                continue
            d = sess.get("data") or {}
            case = L.case(d.get("case") or "")
            act = L._action(d.get("op") or "") if d.get("op") else None
            if not case or case["state"] not in OPEN_STATES or not act or \
                    act["status"] not in ("started", "uncertain"):
                continue
            directory = (act.get("detail") or {}).get("directory") or directory
        else:
            if t["state"] not in S.WRITING or t.get("session_id") != sid:
                continue             # not the task's live session: nothing waits on it
            directory = c.tracks.project_for(P, t).profile.workspace
        # Kilo is asked only about a session nothing touched for the grace period (the
        # engine polls a live one every tick and handles missing / error itself)
        marks = [sess.get("created_ts"), sess.get("prompt_ts")]
        if act is not None:
            ad = act.get("detail") or {}
            marks += [ad.get("prompt_ts"), ad.get("prompting_ts"), act.get("ts")]
        else:
            marks.append(t.get("updated_ts"))
        quiet = now - max(float(x or 0) for x in marks)
        if quiet < grace:
            continue
        try:
            st = P.client.state(sid, directory=directory,
                                since_ts=float(sess.get("prompt_ts") or 0))
        except Exception:                                        # noqa: BLE001
            continue                 # Kilo unreachable: an outage, not a dead session
        denied = bool(getattr(st, "denial", None))
        if st.status not in ("missing", "error") or denied:
            seen.pop(sid, None)
            continue
        # dead on two sweeps at least the grace period apart: a session the engine polls
        # in between is the engine's to handle (its own infra / retry accounting)
        first = seen.setdefault(sid, now)
        if now - first < grace:
            continue
        seen.pop(sid, None)
        if not _allowed(c, P, tid):
            continue
        why = (f"session {sid} is {st.status} in Kilo"
               + (f" ({str(st.error)[:200]})" if st.error else "")
               + f" while the store records it as {sess['status']}, quiet "
               f"{int(quiet // 60)} min")
        if sess["role"] == "lead":
            L._infra_refund(case, act["op_key"], f"self-heal: dead lead session: {why}")
            L._upd(act["op_key"], status="failed", result=f"self-heal: dead session: {why}"[:500])
            L._end_session(P, act["op_key"], "self_heal_dead")
            L._act(case, "self_heal", result=f"dead lead session ended: {why}"[:500],
                   detail={"op_key": act["op_key"], "session_id": sid}, actor="self_heal")
            out.append(_record(c, P, tid, "dead_session",
                               "ended the dead Lead session; the call is failed uncounted and "
                               "the Lead starts a new call",
                               {"session_id": sid, "case_id": case["case_id"],
                                "op_key": act["op_key"], "kilo": st.status,
                                "error": str(st.error or "")[:300],
                                "store_status": sess["status"]}))
            continue
        Pt = c.tracks.project_for(P, t)
        c._end_session(Pt, sid, "self_heal_dead")
        phase = "closeout" if t["state"] == S.CLOSING else "implement"
        c._session_failed(Pt, t, f"the session died ({why}); a fresh session continues "
                                 "from the recorded state (self-heal, no attempt charged)",
                          phase, free=True)
        out.append(_record(c, P, tid, "dead_session",
                           "ended the dead implementer session; free retry",
                           {"session_id": sid, "kilo": st.status, "phase": phase,
                            "error": str(st.error or "")[:300],
                            "store_status": sess["status"]}))
    return out


# ------------------------------------------------------------------ 2 stuck Lead cases

STUCK_CASE_STATES = ("DIAGNOSING", "REPAIRING", "DEFERRED", "RESUMING")


def stuck_cases(c, P) -> list[dict]:
    L = c._lead_any()
    if L is None:
        return []
    out = []
    now = c.clock()
    limit = float(_setting(P, "self_heal_stuck_case_minutes") or 0) * 60
    if not limit:
        return []
    proj = c.store.project(P.id)
    if proj and proj.get("paused"):
        return []                    # a paused project starts no lead call: not stuck
    watch = c.__dict__.setdefault("_selfheal_case_watch", {})
    q = ",".join("?" * len(STUCK_CASE_STATES))
    rows = c.store.q(f"SELECT case_id FROM recovery_cases WHERE project_id=? AND "
                     f"state IN ({q}) AND COALESCE(paused,0)=0", (P.id, *STUCK_CASE_STATES))
    for r in rows:
        case = L.case(r["case_id"])
        if not case:
            continue
        cid, tid = case["case_id"], case["task_id"]
        if L._open_call(cid, "lead_call") or L._open_call(cid, "repair_session") or \
                L._open_call(cid, "track_repair"):
            continue                 # a call is live (rule 1 ends it if it is dead)
        if case["state"] == "DEFERRED" and float(case.get("next_check_ts") or 0) > now:
            continue
        acts = L.actions(cid)
        last = max([float(case.get("updated_ts") or 0), float(case.get("created_ts") or 0)]
                   + [float(a.get("ts") or 0) for a in acts])
        # stuck = this sweep has watched it sit with no call and no new action for the
        # whole period (a clock jump or a just-passed DEFERRED check is not "stuck")
        marker = (case["state"], len(acts), last)
        obs = watch.get(cid)
        if obs is None or obs[1] != marker:
            watch[cid] = (now, marker)
            continue
        if now - last < limit or now - obs[0] < limit:
            continue
        held = _ws_lock_held_by_other(c, P, tid)
        if held:
            continue                 # waiting for the workspace: not stuck
        if not _allowed(c, P, tid):
            continue
        note = (f"self-heal: the Lead case had no live call and no progress for "
                f"{int((now - obs[0]) // 60)} min ({case['state']}); cancelled, the task "
                f"follows the automation default")
        watch.pop(cid, None)
        L.cancel(cid, note, actor="self_heal", P=P)
        out.append(_record(c, P, tid, "stuck_case", "cancelled the stuck Lead case",
                           {"case_id": cid, "state": case["state"],
                            "idle_minutes": int((now - last) // 60)}))
    return out


def _ws_lock_held_by_other(c, P, tid: str) -> str:
    try:
        mine = set(c._workspace_locks(P))
    except Exception:                                            # noqa: BLE001
        return ""
    for lk in c.store.locks():
        if lk["resource"] in mine and (lk["project_id"], lk["task_id"]) != (P.id, tid):
            return f"{lk['project_id']}/{lk['task_id']}"
    return ""


# ------------------------------------------------------------------ 3 fixed by a release

VERSIONS_META = "self_heal_versions"


def note_version(c) -> dict:
    """{version: first time this coordinator was seen running it} (store meta)."""
    import json
    try:
        reg = json.loads(c.store.meta(VERSIONS_META) or "{}")
    except ValueError:
        reg = {}
    v = _running_version()
    if v not in reg:
        reg[v] = time.time()
        c.store.set_meta(VERSIONS_META, json.dumps(reg, sort_keys=True))
    return reg


def fix_deploy_ts(c, fixed_in: str) -> Optional[float]:
    """When a release >= fixed_in was first seen running (None: never, or the running
    release is older)."""
    if _vt(_running_version()) < _vt(fixed_in):
        return None
    reg = note_version(c)
    ts = [float(t) for v, t in reg.items() if _vt(v) >= _vt(fixed_in)]
    return min(ts) if ts else None


def _episode(hist: list) -> tuple[float, list]:
    """(ts the task entered its current BLOCKED stretch, the transitions since)."""
    start = None
    for i in range(len(hist) - 1, -1, -1):
        h = hist[i]
        if h["to_state"] == S.BLOCKED and h["from_state"] != S.BLOCKED:
            start = i
            break
        if h["to_state"] != S.BLOCKED:
            break
    if start is None:
        return 0.0, []
    return float(hist[start]["ts"] or 0), hist[start:]


def _block_texts(c, P, t: dict, episode: list) -> str:
    d = t.get("data") or {}
    parts = [str(t.get("blocker") or ""), str(d.get("blocker_class") or ""),
             str((d.get("lead_trigger") or {}).get("reason") or "")]
    parts += [str(x) for x in (d.get("findings") or [])[:20]]
    for h in episode:
        ev = h.get("evidence") or {}
        parts += [str(ev.get("blocker") or ""), str(ev.get("reason") or ""),
                  str(ev.get("blocker_class") or "")]
    L = c._lead_any()
    if L is not None and episode:
        since = float(episode[0]["ts"] or 0) - 5
        for case in L.cases(P.id, t["task_id"]):
            if float(case.get("created_ts") or 0) >= since:
                parts.append(str((case.get("trigger") or {}).get("reason") or ""))
    return "\n".join(p for p in parts if p)


def _last_reset_unblock(hist: list, before: float) -> Optional[tuple[int, dict]]:
    for i in range(len(hist) - 1, -1, -1):
        h = hist[i]
        if float(h["ts"] or 0) > before:
            continue
        if h["to_state"] == S.READY and (h.get("evidence") or {}).get("reset_attempts"):
            return i, h
    return None


def signatures(c, P, t: dict, hist: list, episode: list) -> list[str]:
    text = _block_texts(c, P, t, episode)
    out = []
    for sig, rx in SIGNATURE_PATTERNS.items():
        if sig not in FIXED_SIGNATURES or not rx.search(text):
            continue
        if sig == "review_budget_after_reset" and episode and \
                _last_reset_unblock(hist, float(episode[0]["ts"] or 0)) is None:
            continue                 # a budget used up with no reset before it: not this bug
        out.append(sig)
    return out


def _backfill_reset(c, P, t: dict, hist: list, before: float) -> list[str]:
    """Re-apply the operator's last reset unblock to every budget counter as it stood
    at that unblock (what rule 6 / v0.155.1 do at unblock time). Returns the counters
    authorized."""
    from .obligations import AUTH_PREFIX, LIMITS, LedgerRefused
    got = _last_reset_unblock(hist, before)
    if got is None:
        return []
    idx, h = got
    tid, done = t["task_id"], []
    rows = [r for r in c.obligations.ledger(P.id, tid) if float(r["ts"] or 0) <= float(h["ts"])]
    for counter in dict.fromkeys(v[0] for v in LIMITS.values()):
        used = sum(int(r["delta"]) for r in rows if r["counter"] == counter)
        auth = sum(int(r["delta"]) for r in rows if r["counter"] == AUTH_PREFIX + counter)
        n = used - auth
        if n <= 0:
            continue
        try:
            c.obligations.authorize(P.id, tid, counter, n,
                                    f"self-heal: backfilled reset of unblock #{idx}",
                                    h.get("actor") or "operator", f"unblock:{idx}")
            done.append(counter)
        except LedgerRefused:
            pass                     # that unblock already authorized this counter
    return done


def fixed_releases(c, P) -> list[dict]:
    if not _setting(P, "self_heal_reverify_fixed"):
        return []
    out = []
    for t in c.store.tasks(P.id):
        if t["state"] != S.BLOCKED:
            continue
        d = t.get("data") or {}
        if d.get("auto_heal"):
            continue                 # a scheduled auto-heal retry owns it
        hist = c.store.history(P.id, t["task_id"])
        block_ts, episode = _episode(hist)
        if not episode:
            continue
        done = dict(d.get("self_heal_fixed") or {})
        for sig in signatures(c, P, t, hist, episode):
            if sig in done:
                continue
            dep = fix_deploy_ts(c, FIXED_SIGNATURES[sig])
            if dep is None or block_ts >= dep:
                continue
            if not _allowed(c, P, t["task_id"]):
                break
            res = _reverify(c, P, t, sig, hist, block_ts)
            if res:
                out.append(res)
            break
    return out


def _reverify(c, P, t: dict, sig: str, hist: list, block_ts: float) -> Optional[dict]:
    pid, tid = P.id, t["task_id"]
    Pt = c.tracks.project_for(P, t)
    try:
        head = Pt.ws.head()
        pending = [p for x, p in Pt.ws.status() if str(x).strip() != "??"]
    except Exception:                                            # noqa: BLE001
        head, pending = "", ["?"]
    target = S.READY
    if head and t.get("base_sha") and head != t["base_sha"] and not pending:
        if c._acquire(Pt, t, c._workspace_locks(Pt)):
            return None              # another writer holds the workspace: next sweep
        target = S.VERIFYING
    fixed_in = FIXED_SIGNATURES[sig]
    data = dict(t.get("data") or {})
    authorized = []
    if sig == "review_budget_after_reset" and c._lc(P, "obligations"):
        authorized = _backfill_reset(c, P, t, hist, block_ts)
    case_id = data.get("recovery_case")
    if case_id:
        L = c._lead_any()
        if L is not None:
            L.cancel(case_id, f"self-heal: the blocker ({sig}) is fixed in v{fixed_in}",
                     actor="self_heal", P=P)
    ids = list((data.get("decision_wait") or {}).get("ids") or [])
    stop = (data.get("lc_stop") or {}).get("decision_id")
    for did in ids:
        _withdraw(c, pid, tid, did, "open", f"self-heal: the blocker ({sig}) is fixed in "
                                            f"v{fixed_in}")
    if stop:
        _withdraw(c, pid, tid, stop, "answered", f"self-heal: the stop was over a blocker "
                                                  f"({sig}) fixed in v{fixed_in}")
    for k in ("question", "decision_wait", "readiness_block", "readiness", "blocker_class",
              getattr(c, "LC_PENDING", "lc_pending_continuation"), "lc_stop",
              "recovery_case", "lead_trigger"):
        data.pop(k, None)
    data["self_heal_fixed"] = {**(data.get("self_heal_fixed") or {}), sig: time.time()}
    data["free_retry"] = True
    res = (f"self-heal: the blocker matches {sig}, fixed in v{fixed_in} (running "
           f"v{_running_version()}); "
           + ("verifying the same head again" if target == S.VERIFYING else
              "a free retry") + " (no attempt charged)")
    ev = {"resolution": res, "self_heal": sig}
    if target == S.VERIFYING:
        ev["decision_id"] = f"self_heal:{sig}"
    c.store.transition(pid, tid, target, ev, actor="self_heal", data=data,
                       next_action=res[:500])
    return _record(c, P, tid, "fixed_release", f"re-verify ({target})",
                   {"signature": sig, "fixed_in": fixed_in, "running": _running_version(),
                    "blocked_ts": block_ts, "blocker": str(t.get("blocker") or "")[:300],
                    "authorized": authorized, "case_cancelled": case_id or ""})


def _withdraw(c, pid, tid, did: str, status: str, reason: str) -> None:
    try:
        if c.store.q("SELECT 1 FROM decisions WHERE decision_id=? AND status=?",
                     (did, status)):
            c.store.x("UPDATE decisions SET status='withdrawn' WHERE decision_id=? AND status=?",
                      (did, status))
            c.store.event(pid, tid, "decision_withdrawn", detail={
                "decision_id": did, "by": "self_heal", "reason": reason})
    except Exception:                                            # noqa: BLE001
        pass                         # no decisions table ([lifecycle] off)


# ------------------------------------------------------------------ 4 stale locks

# a task in these states holds nothing (a Lead call, a check or a run aside)
LOCKLESS_STATES = (S.COMPLETE, S.CANCELLED, S.FAILED, S.BLOCKED, S.QUEUED)


def _task_busy(c, P, t: dict) -> str:
    """Why the task may legitimately hold a lock right now ('' = it can't)."""
    tid = t["task_id"]
    try:
        if any(j["key"][:2] == (P.id, tid) for j in c.bg_checks.running()):
            return "a background check runs"
    except Exception:                                            # noqa: BLE001
        pass
    if ((t.get("data") or {}).get("fast_lane") or {}).get("run"):
        return "a fast-lane gate run"
    try:
        if c.studio_calls.active(P, tid):
            return "a scripted Studio run"
    except Exception:                                            # noqa: BLE001
        pass
    L = c._lead_any()
    if L is not None:
        case = L.active_case(P.id, tid)
        if case and (L._open_call(case["case_id"], "lead_call") or
                     L._open_call(case["case_id"], "repair_session") or
                     L._open_call(case["case_id"], "track_repair")):
            return "a Lead call"
    return ""


def _paused_idle(c, P, t: dict) -> bool:
    """A PAUSED task whose session finished its turn holds nothing: resume takes the
    locks back (engine.resume re-acquires them before it continues the session).
    2026-10-28: a paused GF-GATE1 kept the main-checkout lock for good after its session
    went idle, so the prep queue never reached a safe point."""
    if t["state"] != S.PAUSED:
        return False
    sid = (t.get("data") or {}).get("resume_session") or t.get("session_id")
    if not sid:
        return True
    try:
        st = P.client.state(sid, directory=c.tracks.project_for(P, t).profile.workspace,
                            since_ts=0)
    except Exception:                                            # noqa: BLE001
        return False                 # Kilo unreachable: unknown, keep the locks
    return st.status != "busy"


def stale_locks(c, P) -> list[dict]:
    from .autoheal import _read_json, clear_stale_runner_lock, pid_alive, runner_lock_path
    out = []
    grace = float(_setting(P, "self_heal_stale_lock_minutes") or 0) * 60
    wall = time.time()
    # a) the studio_gate runner lock with a dead pid
    p = runner_lock_path(P.profile.workspace)
    cur = _read_json(p)
    if isinstance(cur, dict) and int(cur.get("pid") or 0) > 0 and \
            not pid_alive(cur.get("pid")):
        try:
            age = wall - os.path.getmtime(p)
        except OSError:
            age = 0
        tid = str(cur.get("sprint") or "") or "-"
        if age >= grace and _allowed(c, P, tid):
            r = clear_stale_runner_lock(P.profile.workspace)
            if r.get("cleared"):
                out.append(_record(c, P, tid, "stale_lock", "released the gate runner lock",
                                   {"lock": p, "pid": r.get("pid"), "age_s": int(age),
                                    "why": "its pid is dead"}))
    # b) a studio_gate studio.lock with no live runner (other holders: _orphan_studio_lock)
    holder_fn = getattr(P.adapter, "studio_lock_holder", None)
    if callable(holder_fn):
        h = holder_fn()
        if h and str(h.get("owner") or "").startswith("studio_gate") and \
                float(h.get("age_s") or 0) >= grace:
            cur = _read_json(p)
            live = isinstance(cur, dict) and cur.get("pid") and pid_alive(cur.get("pid"))
            tid = str(h.get("sprint") or "") or "-"
            t = c.store.task(P.id, tid) if tid != "-" else None
            busy = _task_busy(c, P, t) if t else ""
            if not live and not busy and _allowed(c, P, tid) and P.adapter.release_studio_lock(h):
                out.append(_record(c, P, tid, "stale_lock",
                                   "released the studio_gate studio.lock",
                                   {"line": h.get("line"), "age_s": int(h.get("age_s") or 0),
                                    "why": "no live gate runner and no run of the task"}))
    # c) coordinator locks held by a task that can hold nothing
    by_task: dict = {}
    for lk in c.store.locks():
        if lk["project_id"] != P.id or wall - float(lk.get("acquired_ts") or wall) < grace:
            continue
        by_task.setdefault(lk["task_id"], []).append(lk["resource"])
    for tid, res in by_task.items():
        t = c.store.task(P.id, tid)
        if t is None or (t["state"] not in LOCKLESS_STATES and not _paused_idle(c, P, t)):
            continue                 # a track lease / a live state: not ours to judge
        if _task_busy(c, P, t) or not _allowed(c, P, tid):
            continue
        c.store.release(P.id, tid, res)
        try:
            c._journal_locks(P, t, [r for r in res if not r.startswith("writer:")], "release")
        except Exception:                                        # noqa: BLE001
            pass
        out.append(_record(c, P, tid, "stale_lock", "released coordinator locks",
                           {"resources": res, "state": t["state"],
                            "why": "the task has no live session, check or run"}))
    return out


# ------------------------------------------------------------------ 5 holds

def owner_alive(c, owner: str) -> Optional[bool]:
    """True / False for the owner kinds the sweep can check, None otherwise."""
    kind, _, ref = str(owner or "").partition(":")
    if not ref:
        return None
    try:
        if kind in _OWNER_CHECKS:
            got = _OWNER_CHECKS[kind](c, ref)
            return None if got is None else bool(got)
        if kind == "pid":
            from .autoheal import pid_alive
            return pid_alive(ref)
        if kind == "session":
            s = c.store.session(ref)
            return bool(s) and not s.get("ended_ts")
        if kind == "task":
            pid, _, tid = ref.partition("/")
            t = c.store.task(pid, tid)
            return bool(t) and t["state"] not in S.TERMINAL
        if kind in ("case", "lead_case"):
            from .lead import OPEN_STATES
            L = c._lead_any()
            case = L.case(ref) if L is not None else None
            return bool(case) and case["state"] in OPEN_STATES
    except Exception:                                            # noqa: BLE001
        return None
    return None


def _paused_ts(c, P, tid: str) -> float:
    r = c.store.q("SELECT MAX(ts) AS ts FROM transitions WHERE project_id=? AND task_id=? AND "
                  "to_state=?", (P.id, tid, S.PAUSED))
    return float((r[0]["ts"] if r else 0) or 0)


def holds(c, P) -> list[dict]:
    out = []
    now = c.clock()
    hours = float(_setting(P, "self_heal_unowned_pause_hours") or 0)
    for t in c.store.tasks(P.id):
        if t["state"] != S.PAUSED:
            continue
        tid = t["task_id"]
        d = t.get("data") or {}
        hold = d.get("hold")
        if isinstance(hold, dict) and hold.get("owner"):
            exp = hold.get("expires_ts")
            expired = bool(exp) and now >= float(exp)
            alive = owner_alive(c, hold["owner"])
            if not expired and alive is not False:
                continue
            if not _allowed(c, P, tid):
                continue
            why = (f"the hold expired ({hold.get('reason') or 'no reason'})" if expired else
                   f"the hold's owner {hold['owner']} is gone")
            try:
                c.resume(P.id, tid, actor="self_heal", note=f"self-heal: {why}")
            except Exception as e:                               # noqa: BLE001
                _log(c, f"{P.id}/{tid}: self-heal resume failed: {type(e).__name__}: {e}")
                continue
            out.append(_record(c, P, tid, "hold", "resumed the held task",
                               {"hold": hold, "expired": expired, "owner_alive": alive}))
            continue
        if not hours or t.get("pause_until"):
            continue                 # a budget / route pause resumes itself
        since = _paused_ts(c, P, tid)
        if not since or time.time() - since < hours * 3600:
            continue
        if float(d.get("pause_forgotten_noticed") or 0) == since:
            continue                 # once per pause
        if not _allowed(c, P, tid):
            continue
        hrs = int((time.time() - since) // 3600)
        text = (f"{tid} has been PAUSED for {hrs} h with no hold owner or expiry "
                f"({t.get('pause_reason') or 'no reason'}). Resume it, or pause it again "
                f"with --owner/--minutes.")
        c.store.event(P.id, tid, "operator_notice", detail={
            "text": text, "kind": "pause_forgotten", "alert_class": "pause_forgotten",
            "task_id": tid, "paused_hours": hrs})
        c.store.merge_data(P.id, tid, pause_forgotten_noticed=since)
        out.append(_record(c, P, tid, "hold", "operator notice: pause_forgotten",
                           {"paused_hours": hrs, "reason": str(t.get("pause_reason") or "")[:300],
                            "alert_class": "pause_forgotten"}))
    return out

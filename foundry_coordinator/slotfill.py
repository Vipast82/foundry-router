"""Keep the slots busy (COORDINATOR_AUTOMATION_NEEDS items 12, 13 and 14).

A free Studio slot with ready work on the queue used to wait until somebody typed
`tracks start ... --slot`. With `[automation] slot_auto_fill` on, once a minute:

  a slot is free   an enabled [slots] entry with no live lane bound to it, free for at least
                   `slot_auto_fill_minutes`, with an agent seat left under max_agents and
                   the project not paused.
  what starts      in queue order, the first sprint that is
                     ready     approved, never started, on no lane, every dependency
                               complete (item 12). When nothing holds the main checkout,
                               the first such sprint is the workstation's and is left to it;
                     moved     a main-line sprint that already ran sessions and now waits
                               READY, its last session ended, while another sprint holds
                               the main checkout (item 13: what `tracks start
                               --move-started` did by hand);
                     early     only when neither exists and `order_deps_early` is on: a
                               sprint whose every unmet dependency is marked `(order)` in
                               its plan ("Depends on: FIN-5 (order)") - a dependency that is
                               there for scheduling, not for files. The dependency is taken
                               off its queue entry, as `queue set-deps` did (item 14);
                   and that
                     - is not workstation-only ([automation] workstation_only_sprints);
                     - has a plan whose file list shares no file with any other open sprint
                       (append-safe ledgers aside), except sprints that wait for it anyway.
                       Read from both plans' file lists; a sprint without a readable plan
                       is never picked.
  how              a lane named after the sprint, bound to the slot, through the same
                   `create` + `start` the command uses. One start per minute. A refusal is
                   an event and that sprint is left alone for half an hour.

Every start is an event (`slot_auto_filled`, in the watch log) saying which rule picked it.
`state()` is what the status board shows for a free slot: what would start, or why nothing
does. Both switches ship OFF: nothing starts or moves by itself until they are turned on.
"""

from __future__ import annotations

from typing import Optional

from . import states as S
from .automation import get as auto_get

EVERY_S = 60.0
STATE_CACHE_S = 30.0
REFUSED_AGAIN_S = 1800.0


def _on(P, key: str) -> bool:
    try:
        return bool(auto_get(P, key))
    except Exception:                                            # noqa: BLE001
        return False


def free_slots(c, P) -> list:
    """Enabled slots no live lane is bound to, in name order."""
    from . import slots as SL
    from .tracks.manager import LIVE_STATES
    configured = SL.configured(P.profile)
    bound = {str((r.get("data") or {}).get("slot") or "")
             for r in c.store.tracks(P.id) if r["state"] in LIVE_STATES}
    return [s for s in sorted(configured) if s not in bound]


def _waits_for(queue: dict, tid: str) -> set:
    """Sprints that depend on `tid`, directly or through others (queue deps)."""
    out: set = set()
    grew = True
    while grew:
        grew = False
        for k, q in queue.items():
            if k not in out and k != tid and any(d == tid or d in out for d in q.get("deps") or []):
                out.add(k)
                grew = True
    return out


def _clash(an: dict, tid: str, others: list) -> str:
    """'' when `tid`'s plan shares no file with any of `others`; else which and with whom.
    A sprint with no readable plan clashes (its files are not known)."""
    from .tracks import analysis as AN
    sp = an.get("sprints") or {}
    safe = list(an.get("append_safe") or AN.DEFAULT_APPEND_SAFE)

    def info(i):
        d = sp.get(i)
        if not isinstance(d, dict):
            return None
        return AN.PlanInfo(**{f: v for f, v in d.items() if f in AN.PlanInfo.__dataclass_fields__})
    me = info(tid)
    if me is None or not me.files:
        return f"{tid} has no plan with a file list"
    for o in others:
        other = info(o)
        if other is None:
            continue                     # no plan: it cannot be compared, and never runs on a lane
        hit = AN.shared_files(me, other, safe) or AN.shared_files(other, me, safe)
        if hit:
            return f"shares {hit[0]} with {o}"
    return ""


def pick(c, P) -> dict:
    """What would start on a free slot now: {"sprint", "rule", "drop"} or {"why"}."""
    from .board import _workstation_only
    pid = P.id
    tm = c.tracks
    tasks = {t["task_id"]: t for t in c.store.tasks(pid)}
    rows = c.store.queue(pid)
    queue = {q["task_id"]: q for q in rows}
    open_ids = [k for k, t in tasks.items() if t["state"] not in S.TERMINAL]
    main_busy = [t["task_id"] for t in tm.main_tasks(P) if t["state"] in tm.QUIET_BLOCKERS]
    refused = c.__dict__.setdefault("_slotfill_refused", {})
    now = c.clock()
    try:
        an = tm.analyze(P)
    except Exception as e:                                       # noqa: BLE001
        return {"why": f"the plans could not be read: {type(e).__name__}: {e}"[:200]}
    order_marks = {k: set((d or {}).get("order_deps") or [])
                   for k, d in (an.get("sprints") or {}).items()}
    early_ok = _on(P, "order_deps_early")
    ready, moved, early, whys = [], [], [], []
    # The main checkout takes the first runnable main-line sprint itself: when nothing holds
    # it, that sprint is the workstation's, not a slot's.
    for_main = ""
    if not main_busy:
        for q in rows:
            t = tasks.get(q["task_id"])
            if (t and not q.get("track") and t["state"] in (S.QUEUED, S.READY) and all(
                    (tasks.get(d) or {}).get("state") == S.COMPLETE for d in q.get("deps") or [])):
                for_main = q["task_id"]
                break
    for q in rows:
        tid = q["task_id"]
        t = tasks.get(tid)
        if not t or q.get("track") or t["state"] not in (S.QUEUED, S.READY):
            continue
        if tid == for_main:
            whys.append(f"{tid} runs on the main checkout")
            continue
        if now - refused.get((pid, tid), -1e18) < REFUSED_AGAIN_S:
            whys.append(f"{tid} was refused a lane less than 30 min ago")
            continue
        if _workstation_only(P, tid):
            whys.append(f"{tid} is workstation-only")
            continue
        started = bool(int(t.get("attempt") or 0) or int(t.get("sessions") or 0)
                       or t.get("session_id"))
        unmet = [d for d in q.get("deps") or [] if (tasks.get(d) or {}).get("state") != S.COMPLETE]
        if started:
            if unmet or t["state"] != S.READY:
                continue
            sid = t.get("session_id")
            srow = c.store.session(sid) if sid else None
            if sid and srow and not srow.get("ended_ts"):
                continue
            if not [m for m in main_busy if m != tid]:
                continue                 # the main checkout is free: it runs there
            moved.append((tid, []))
        elif not unmet:
            ready.append((tid, []))
        elif early_ok and all(d in order_marks.get(tid, ()) for d in unmet):
            early.append((tid, unmet))
        else:
            whys.append(f"{tid} waits for {', '.join(unmet[:4])}")
    for rule, group in (("ready", ready), ("moved", moved), ("early", early)):
        for tid, drop in group:
            after = _waits_for(queue, tid)       # they run after it anyway
            others = [o for o in open_ids if o != tid and o not in after]
            clash = _clash(an, tid, others)
            if clash:
                whys.append(f"{tid} {clash}" if not clash.startswith(tid) else clash)
                continue
            return {"sprint": tid, "rule": rule, "drop": drop}
    return {"why": "; ".join(whys[:4]) or "the queue has no sprint that could start"}


def state(c, P) -> dict:
    """For the board: {"on", "free": [...], "next": pick() | None, "why"} without acting."""
    out = {"on": _on(P, "slot_auto_fill"), "free": [], "why": ""}
    cache = c.__dict__.setdefault("_slotfill_state", {})
    hit = cache.get(P.id)
    if hit and 0 <= c.clock() - hit[0] < STATE_CACHE_S and hit[1]["on"] == out["on"]:
        return hit[1]
    cache[P.id] = (c.clock(), out)
    try:
        out["free"] = free_slots(c, P)
        if not out["free"]:
            return out
        tm = c.tracks
        if tm.agents_running() >= tm.max_agents():
            out["why"] = f"max_agents {tm.max_agents()} is in use"
            return out
        got = pick(c, P)
        out["next"] = got if got.get("sprint") else None
        out["why"] = got.get("why") or ""
    except Exception as e:                                       # noqa: BLE001
        out["why"] = f"not read: {type(e).__name__}: {e}"[:200]
    return out


def _lane_name(c, P, tid: str) -> str:
    from .tracks import analysis as AN
    base = AN._track_name(tid)
    name, n = base, 1
    while c.store.track(P.id, name):
        n += 1
        name = f"{base}-{n}"
    return name


def step(c, P) -> Optional[dict]:
    """Start one sprint on one free slot when the rules above allow. Never raises."""
    try:
        if not _on(P, "slot_auto_fill") or c.tracks.is_track(P):
            return None
        marks = c.__dict__.setdefault("_slotfill", {})
        m = marks.setdefault(P.id, {"at": -1e18, "free_since": {}})
        now = c.clock()
        if now - m["at"] < EVERY_S:
            return None
        m["at"] = now
        if (c.store.project(P.id) or {}).get("paused"):
            return None
        free = free_slots(c, P)
        m["free_since"] = {s: m["free_since"].get(s, now) for s in free}
        try:
            dwell = max(0, int(auto_get(P, "slot_auto_fill_minutes"))) * 60.0
        except Exception:                                        # noqa: BLE001
            dwell = 300.0
        waited = [s for s in free if now - m["free_since"][s] >= dwell]
        if not waited:
            return None
        tm = c.tracks
        if tm.agents_running() >= tm.max_agents():
            return None
        got = pick(c, P)
        tid = got.get("sprint")
        if not tid:
            return None
        return _start(c, P, waited[0], tid, got["rule"], got.get("drop") or [])
    except Exception as e:                                       # noqa: BLE001
        try:
            c.log(f"{P.id}: slot fill failed: {type(e).__name__}: {e}")
        except Exception:                                        # noqa: BLE001
            pass
        return None


RULE_WORDS = {"ready": "ready and never started",
              "moved": "started, waiting READY behind the main checkout",
              "early": "its open dependencies are order-only and share no file with it"}


def _start(c, P, slot: str, tid: str, rule: str, drop: list) -> Optional[dict]:
    from .tracks.manager import TrackError
    tm = c.tracks
    pid = P.id
    name = _lane_name(c, P, tid)
    before = tm.move_started
    try:
        if drop:
            deps = [d for d in ((c.store.queue_entry(pid, tid) or {}).get("deps") or [])
                    if d not in drop]
            # not as "coordinator": a dependency removed by anyone else stays removed (the
            # readiness check would put a plan-named one straight back otherwise)
            c.set_deps(pid, tid, deps, "slotfill")
            c.store.event(pid, tid, "order_dep_skipped", detail={
                "deps": drop, "digest": True,
                "why": "marked (order) in the plan; no file shared; nothing else was ready "
                       f"for {slot}"})
        if rule == "moved":
            tm.move_started = frozenset([tid])
        tm.create(P, name, [tid], None, "slotfill", slot=slot)
        row = tm.start(P, name, "slotfill")
    except (TrackError, ValueError) as e:
        c.__dict__.setdefault("_slotfill_refused", {})[(pid, tid)] = c.clock()
        c.store.event(pid, tid, "slot_fill_refused", detail={
            "slot": slot, "rule": rule, "error": str(e)[:400], "digest": True})
        # a lane that was created but could not start would keep the slot bound
        left = c.store.track(pid, name)
        if left and left["state"] == "planned":
            try:
                tm.stop(P, name, "slotfill", "could not start")
            except Exception:                                    # noqa: BLE001
                pass
        return None
    finally:
        tm.move_started = before
    detail = {"slot": slot, "track": row["name"], "sprint": tid, "rule": rule, "digest": True,
              "text": f"{tid} started on {slot} (lane {row['name']}): {RULE_WORDS[rule]}"
                      + (f"; skipped {', '.join(drop)}" if drop else "")}
    c.store.event(pid, tid, "slot_auto_filled", detail=detail)
    c.__dict__.get("_slotfill_state", {}).pop(pid, None)
    c.__dict__.get("_slotfill", {}).get(pid, {}).get("free_since", {}).pop(slot, None)
    return detail

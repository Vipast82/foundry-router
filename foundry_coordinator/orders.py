"""Standing orders: an owner's order that runs as a rule until its end condition is met,
then reports (COORDINATOR_AUTOMATION_NEEDS item 28).

Until now an order like "keep POL-7 going until it completes" (Victor, 2026-11-18) lived in
a chat session's notes: that session unblocked the sprint, reset its budget, added time and
stopped a stale playtest, by hand, every time the sprint stopped, and remembered to stop
when it closed. Three kinds, from the orders that were actually given:

  keep_going     one sprint, until it is COMPLETE. Whenever it stops (BLOCKED / FAILED) on
                 anything but a decision that is the owner's, the order puts it back: over
                 its minutes -> more minutes (budget_policy.suggest_minutes); out of
                 sessions / attempts / review calls -> a budget reset; anything else -> an
                 unblock that tells the next session what stopped it. Never while a product
                 decision or something only a person can do is open, the project is paused,
                 the sprint is held, or it is waiting out an outage by itself. At most one
                 action every `gap_minutes` (default 20) and `per_day` a day (default 8):
                 a sprint that stops more often than that gets one alert, not a loop
                 (CR-2 once restarted a session every four minutes).
  pause_all      no new session starts in the project (the project pause) until a time,
                 or until a model backend's usage window resets (the reset time the router
                 reports when the order is given). Running sessions finish their turn.
                 Then the project is resumed, unless someone else paused it meanwhile.
  claude_agents  resources.claude_agents is held at `n` until a time or until cancelled,
                 then put back. Re-applied every tick, so a settings deploy in between
                 does not undo it.

An order's end condition (`until`): {"type": "complete"} (keep_going), {"type": "time",
"ts"}, or {"type": "cancel"}. Every order, every action it takes and its end is an event
(`order_added`, `order_action`, `order_done`), in the digest and the watch log
(watchlog.py), with who gave it and its scope.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Optional

from . import states as S

SCHEMA = """
CREATE TABLE IF NOT EXISTS standing_orders (
  order_id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, task_id TEXT, params TEXT,
  until TEXT, status TEXT, created_by TEXT, note TEXT, created_ts REAL, ended_ts REAL,
  result TEXT, state TEXT);
CREATE INDEX IF NOT EXISTS idx_orders_project ON standing_orders(project_id, status);
"""
KINDS = ("keep_going", "pause_all", "claude_agents")
ACTIVE, DONE, CANCELLED = "active", "done", "cancelled"
EVERY_S = 30.0
DAY_S = 86400.0
MAX_NOTE = 500
MAX_ACTIVE = 20
MAX_MINUTES = 14 * 24 * 60
ORDER_ID_RE = re.compile(r"^so-[0-9a-f]{10}$")
# words for the owner's order -> the router's backend names
BACKEND_WORDS = {"claude": ("meridian", "claude", "anthropic"),
                 "gpt": ("chatgpt", "codex", "openai", "gpt"),
                 "chatgpt": ("chatgpt", "codex", "openai", "gpt")}


class OrderError(ValueError):
    pass


def ensure(store) -> None:
    if getattr(store, "_orders_ready", False):
        return
    with store._lock:
        store.conn.executescript(SCHEMA)
    store._orders_ready = True


def _j(v) -> str:
    return json.dumps(v, sort_keys=True)


def _row(r: dict) -> dict:
    out = dict(r)
    for k in ("params", "until", "state"):
        try:
            out[k] = json.loads(out.get(k) or "{}")
        except ValueError:
            out[k] = {}
    return out


def get(store, order_id: str) -> Optional[dict]:
    ensure(store)
    r = store.q("SELECT * FROM standing_orders WHERE order_id=?", (order_id,))
    return _row(r[0]) if r else None


def orders(store, project_id: str = "", status: str = "") -> list[dict]:
    ensure(store)
    sql, args = "SELECT * FROM standing_orders WHERE 1=1", []
    if project_id:
        sql += " AND project_id=?"
        args.append(project_id)
    if status:
        sql += " AND status=?"
        args.append(status)
    return [_row(r) for r in store.q(sql + " ORDER BY created_ts, order_id", args)]


def _save(store, order_id: str, **fields) -> None:
    fields = {k: (_j(v) if k in ("params", "until", "state") else v) for k, v in fields.items()}
    cols = ", ".join(f"{k}=?" for k in fields)
    store.x(f"UPDATE standing_orders SET {cols} WHERE order_id=?", (*fields.values(), order_id))


# ---------------------------------------------------------------------------- words

def describe(o: dict) -> str:
    """The order in one line, as given."""
    p, u = o.get("params") or {}, o.get("until") or {}
    if o["kind"] == "keep_going":
        what = f"keep {o['task_id']} going"
    elif o["kind"] == "pause_all":
        what = "pause all sprints"
    else:
        what = f"{p.get('n')} Claude agent{'s' if p.get('n') != 1 else ''} only"
    if u.get("type") == "complete":
        end = "until it completes"
    elif u.get("type") == "time":
        end = "until " + time.strftime("%Y-%m-%d %H:%M", time.localtime(float(u.get("ts") or 0)))
        if u.get("why"):
            end += f" ({u['why']})"
    else:
        end = "until cancelled"
    return f"{what} {end}"


# ---------------------------------------------------------------------------- the end condition

def reset_time(c, P, word: str) -> tuple:
    """(ts, why) of the reset of the fullest usage window of the backend the owner named
    ("claude", "gpt", or a backend's own name), as the router reports it now."""
    word = str(word or "").strip().lower()
    if not word:
        raise OrderError("name the backend whose window should reset (claude, gpt, ...)")
    if P.foundry is None:
        raise OrderError("this project has no router to ask for a reset time; give minutes")
    try:
        persona = P.profile.persona("implementer", "")
        ans = P.foundry.helper_admission(persona)
    except Exception as e:                                       # noqa: BLE001
        raise OrderError(f"the router did not answer for a reset time ({type(e).__name__}: "
                         f"{e}); give minutes instead"[:300]) from None
    names = BACKEND_WORDS.get(word, (word,))
    best, seen = None, []
    for route in (ans or {}).get("routes") or []:
        backend = str(route.get("backend") or "")
        seen.append(backend)
        if not any(n in backend.lower() for n in names):
            continue
        for acct in route.get("accounts") or []:
            for w in acct.get("windows") or []:
                if w.get("applies") is False or w.get("resets_in_s") is None:
                    continue
                try:
                    used, left = float(w.get("used") or 0), float(w["resets_in_s"])
                except (TypeError, ValueError):
                    continue
                if left > 0 and (best is None or used > best[0]):
                    best = (used, left, backend, str(w.get("name") or w.get("window") or "window"))
    if best is None:
        raise OrderError(f"the router reports no reset time for {word!r} (backends it named: "
                         f"{', '.join(sorted(set(seen))) or 'none'}); give minutes instead")
    used, left, backend, wname = best
    return c.clock() + left, f"{backend} {wname} at {used:.0f}% resets"


def _until(c, P, kind: str, until) -> dict:
    u = until if isinstance(until, dict) else {}
    if u.get("reset"):
        ts, why = reset_time(c, P, str(u["reset"]))
        return {"type": "time", "ts": ts, "why": why}
    if u.get("minutes") is not None:
        try:
            n = float(u["minutes"])
        except (TypeError, ValueError):
            raise OrderError("minutes must be a number") from None
        if not 1 <= n <= MAX_MINUTES:
            raise OrderError("minutes: between 1 and 20160 (14 days)")
        return {"type": "time", "ts": c.clock() + n * 60}
    if u.get("ts") is not None:
        try:
            ts = float(u["ts"])
        except (TypeError, ValueError):
            raise OrderError("until.ts must be a time") from None
        if ts <= c.clock() or ts > c.clock() + MAX_MINUTES * 60:
            raise OrderError("until: a time in the next 14 days")
        return {"type": "time", "ts": ts}
    if kind == "keep_going":
        return {"type": "complete"}
    if kind == "pause_all":
        raise OrderError("pause_all needs an end: minutes, a time, or a backend whose window "
                         "should reset")
    return {"type": "cancel"}


# ---------------------------------------------------------------------------- add / cancel

def add(c, P, kind: str, *, task_id: str = "", until=None, params: Optional[dict] = None,
        by: str = "operator", note: str = "") -> dict:
    ensure(c.store)
    kind = str(kind or "").strip()
    if kind not in KINDS:
        raise OrderError(f"kind must be one of {', '.join(KINDS)}")
    params = dict(params or {})
    pid = P.id
    live = orders(c.store, pid, ACTIVE)
    if len(live) >= MAX_ACTIVE:
        raise OrderError(f"{len(live)} standing orders are active already; cancel one first")
    p: dict = {}
    if kind == "keep_going":
        task_id = str(task_id or "").strip()
        t = c.store.task(pid, task_id) if task_id else None
        if t is None:
            raise OrderError(f"keep_going needs a sprint on the queue ({task_id or 'none given'})")
        if t["state"] in S.TERMINAL:
            raise OrderError(f"{task_id} is already {t['state']}")
        if any(o["kind"] == kind and o["task_id"] == task_id for o in live):
            raise OrderError(f"{task_id} already has a keep_going order")
        for key, default, lo, hi in (("gap_minutes", 20, 5, 720), ("per_day", 8, 1, 48)):
            try:
                v = int(params.get(key, default))
            except (TypeError, ValueError):
                raise OrderError(f"{key} must be a whole number") from None
            if not lo <= v <= hi:
                raise OrderError(f"{key}: {lo} to {hi}")
            p[key] = v
    else:
        task_id = ""
        if any(o["kind"] == kind for o in live):
            raise OrderError(f"a {kind} order is active already; cancel it first")
    if kind == "claude_agents":
        try:
            n = int(params.get("n"))
        except (TypeError, ValueError):
            raise OrderError("claude_agents needs n (how many Claude agents)") from None
        if not 1 <= n <= 32:
            raise OrderError("n: 1 to 32")
        p["n"] = n
    u = _until(c, P, kind, until)
    oid = "so-" + uuid.uuid4().hex[:10]
    now = c.clock()
    c.store.x("INSERT INTO standing_orders (order_id, project_id, kind, task_id, params, until, "
              "status, created_by, note, created_ts, ended_ts, result, state) VALUES "
              "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (oid, pid, kind, task_id, _j(p), _j(u), ACTIVE, str(by or "operator")[:120],
               str(note or "")[:MAX_NOTE], now, None, "", _j({})))
    o = get(c.store, oid)
    c.store.event(pid, task_id, "order_added", detail={
        "order_id": oid, "kind": kind, "order": describe(o), "by": o["created_by"],
        "note": o["note"], "until": u, "params": p, "digest": True})
    _apply(c, P, o, first=True)
    return get(c.store, oid)


def cancel(c, order_id: str, by: str = "operator", why: str = "") -> dict:
    o = get(c.store, order_id)
    if o is None:
        raise OrderError(f"no standing order {order_id}")
    if o["status"] != ACTIVE:
        return o
    P = c.projects.get(o["project_id"])
    return _end(c, P, o, CANCELLED, f"cancelled by {by}" + (f": {why}" if why else ""))


def _end(c, P, o: dict, status: str, result: str) -> dict:
    undone = _undo(c, P, o) if P is not None else ""
    result = (result + (f"; {undone}" if undone else ""))[:500]
    _save(c.store, o["order_id"], status=status, ended_ts=c.clock(), result=result)
    done = get(c.store, o["order_id"])
    acts = len((done.get("state") or {}).get("acts") or [])
    text = (f"Standing order {'done' if status == DONE else 'ended'}: {describe(done)} (from "
            f"{done['created_by']}). {result}."
            + (f" It acted {acts} time(s)." if done["kind"] == "keep_going" else ""))
    c.store.event(done["project_id"], done["task_id"], "operator_notice", detail={
        "text": text[:900], "kind": "order_done", "digest": True, "order_id": done["order_id"],
        "order": describe(done), "status": status, "result": result})
    c.store.event(done["project_id"], done["task_id"], "order_done", detail={
        "order_id": done["order_id"], "kind": done["kind"], "order": describe(done),
        "status": status, "result": result, "by": done["created_by"], "digest": True})
    return done


# ---------------------------------------------------------------------------- the rules

def tick(c, P) -> list[dict]:
    """Run P's active orders (at most every EVERY_S). Never raises. Returns what was done."""
    try:
        last = c.__dict__.setdefault("_orders_at", {})
        now = c.clock()
        if now - last.get(P.id, -1e18) < EVERY_S:
            return []
        last[P.id] = now
        live = orders(c.store, P.id, ACTIVE)
    except Exception as e:                                       # noqa: BLE001
        _log(c, f"{P.id}: standing orders: {type(e).__name__}: {e}")
        return []
    out = []
    for o in live:
        try:
            got = _apply(c, P, o)
            if got:
                out.append(got)
        except Exception as e:                                   # noqa: BLE001
            _log(c, f"{P.id}: standing order {o['order_id']}: {type(e).__name__}: {e}")
    return out


def _log(c, msg: str) -> None:
    try:
        c.log(msg)
    except Exception:                                            # noqa: BLE001
        pass


def _apply(c, P, o: dict, first: bool = False) -> Optional[dict]:
    u = o.get("until") or {}
    now = c.clock()
    if u.get("type") == "time" and now >= float(u.get("ts") or 0) and not first:
        return _end(c, P, o, DONE, "the time is up" if not u.get("why") else f"{u['why']}: done")
    return {"keep_going": _keep_going, "pause_all": _pause_all,
            "claude_agents": _claude_agents}[o["kind"]](c, P, o, now)


def _undo(c, P, o: dict) -> str:
    """Put back what the order changed (pause_all, claude_agents). What was done, in words."""
    st = o.get("state") or {}
    if o["kind"] == "pause_all" and st.get("applied") and not st.get("paused_by_order"):
        return "the project was paused before this order and stays as it is"
    if o["kind"] == "pause_all" and st.get("paused_by_order"):
        proj = c.store.project(P.id) or {}
        if proj.get("paused") and str(proj.get("pause_note") or "") == str(st.get("pause_note")):
            c.store.set_project_paused(P.id, False, f"standing order {o['order_id']} ended")
            return "the project is resumed"
        return "the project was paused again by someone else meanwhile: left as it is" \
            if proj.get("paused") else "the project was resumed by hand before"
    if o["kind"] == "claude_agents" and "restore" in st:
        for raw in _raws(c, P):
            r = raw.setdefault("resources", {})
            if st["restore"] is None:
                r.pop("claude_agents", None)
            else:
                r["claude_agents"] = st["restore"]
        return f"claude_agents is back to {st['restore'] if st['restore'] is not None else 'its default'}"
    return ""


def _raws(c, P) -> list:
    """The settings tables the Claude cap is read from: the project's, and each live
    track's copy of it (a track keeps the table it was made with)."""
    out = []
    projs = [P]
    try:
        projs += [tp for key, tp in c.tracks._projects.items() if key[0] == P.id]
    except Exception:                                            # noqa: BLE001
        pass
    for p in projs:
        raw = getattr(p.profile, "raw", None)
        if isinstance(raw, dict) and not any(raw is x for x in out):
            out.append(raw)
    return out


def _pause_all(c, P, o: dict, now: float) -> Optional[dict]:
    st = dict(o.get("state") or {})
    if st.get("applied"):
        return None
    proj = c.store.project(P.id) or {}
    note = f"standing order {o['order_id']}: {describe(o)}"
    st.update(applied=True, paused_by_order=not proj.get("paused"), pause_note=note)
    if not proj.get("paused"):
        c.store.set_project_paused(P.id, True, note)
    _save(c.store, o["order_id"], state=st)
    return _action(c, P, o, "paused the project: no new session starts" if st["paused_by_order"]
                   else "the project was paused already; the order only ends it", "")


def _claude_agents(c, P, o: dict, now: float) -> Optional[dict]:
    st = dict(o.get("state") or {})
    n = int((o.get("params") or {}).get("n") or 0)
    raws = _raws(c, P)
    if not raws or n <= 0:
        return None
    out = None
    if "restore" not in st:
        st["restore"] = raws[0].setdefault("resources", {}).get("claude_agents")
        _save(c.store, o["order_id"], state=st)
        out = _action(c, P, o, f"claude_agents set to {n} (was "
                               f"{st['restore'] if st['restore'] is not None else 'unset'})", "")
    for raw in raws:
        r = raw.setdefault("resources", {})
        if r.get("claude_agents") != n:
            r["claude_agents"] = n       # a settings deploy put the file's value back
    return out


def _action(c, P, o: dict, what: str, why: str, **extra) -> dict:
    detail = {"order_id": o["order_id"], "kind": o["kind"], "order": describe(o),
              "by": o["created_by"], "action": what, "why": str(why or "")[:400],
              "digest": True, **extra}
    c.store.event(o["project_id"], o["task_id"], "order_action", detail=detail)
    _log(c, f"{o['project_id']}: standing order {o['order_id']} ({describe(o)}): {what}")
    return detail


def _keep_going(c, P, o: dict, now: float) -> Optional[dict]:
    from . import budget_policy, infra, selfheal
    from .decision_policy import BUDGET_TRIGGERS
    pid, tid, oid = o["project_id"], o["task_id"], o["order_id"]
    t = c.store.task(pid, tid)
    if t is None:
        return _end(c, P, o, CANCELLED, f"{tid} is no longer on the queue")
    if t["state"] == S.COMPLETE:
        return _end(c, P, o, DONE, f"{tid} is COMPLETE")
    if t["state"] == S.CANCELLED:
        return _end(c, P, o, CANCELLED, f"{tid} was cancelled")
    if t["state"] not in (S.BLOCKED, S.FAILED):
        return None
    d = t.get("data") or {}
    st = dict(o.get("state") or {})
    params = o.get("params") or {}

    def wait(why: str) -> None:
        if st.get("waiting") != why:
            st["waiting"] = why
            _save(c.store, oid, state=st)

    if d.get("auto_heal"):
        return wait("it is waiting out an infrastructure fault by itself")
    if d.get("hold"):
        return wait("it is held")
    if (c.store.project(pid) or {}).get("paused"):
        return wait("the project is paused")
    did = selfheal._owner_decision_open(c, P, tid)
    if did:
        return wait(f"a decision of yours is open ({did})")
    if d.get("recovery_case"):
        L = c._lead_any()
        case = L.case(str(d["recovery_case"])) if L is not None else None
        try:
            busy = bool(case) and any(a.get("status") in ("started", "uncertain")
                                      for a in L.actions(case["case_id"]))
        except Exception:                                        # noqa: BLE001
            busy = True
        if busy:
            return wait("the Lead has a call in flight on it")
    acts = [float(x) for x in st.get("acts") or [] if now - float(x) < DAY_S]
    gap = float(params.get("gap_minutes") or 20) * 60
    if acts and now - acts[-1] < gap:
        return None
    per_day = int(params.get("per_day") or 8)
    if len(acts) >= per_day:
        if not st.get("capped") or now - float(st["capped"]) >= DAY_S:
            st["capped"] = now
            st["waiting"] = f"{len(acts)} actions in a day: not acting again today"
            _save(c.store, oid, state=st)
            c.store.event(pid, tid, "operator_notice", detail={
                "text": (f"Standing order '{describe(o)}': {tid} stopped {len(acts)} times in "
                         f"a day and each time it was put back. It stopped again "
                         f"({str(t.get('blocker') or '')[:200]}). The order stays, but it does "
                         f"not act again until tomorrow: this one needs a look.")[:900],
                "kind": "order_capped", "alert_class": "watch", "order_id": oid,
                "title": f"a standing order put {tid} back {len(acts)} times today"})
        return None
    hist = c.store.history(pid, tid)
    _, episode = selfheal._episode(hist)
    last = (hist[-1].get("evidence") or {}) if hist else {}
    stop = selfheal.first_stop(c, P, t, episode) if episode else {
        "class": str(last.get("blocker_class") or ""),
        "reason": str(last.get("reason") or last.get("blocker") or t.get("blocker") or "")}
    cls, reason = stop["class"], stop["reason"]
    actor = f"order:{oid}"
    Pt = c.tracks.project_for(P, t)
    resolution = (f"Standing order from {o['created_by']}: {describe(o)}. Continue from the "
                  f"committed work and the plan's next open step. What stopped it: "
                  f"{reason[:700]}")
    if cls == "task_minutes" or "over max_task_minutes" in reason:
        minutes, basis = budget_policy.suggest_minutes(c, Pt, t, infra.active_seconds(c, t))
        c.extend_time(pid, tid, minutes, actor=actor, note=f"{describe(o)}; {basis}")
        what = f"gave it {minutes} more minutes ({basis})"
    elif cls in BUDGET_TRIGGERS or any(x in reason for x in (
            "sessions used", "implementation attempts used", "review budget used")):
        c.unblock(pid, tid, resolution, reset_attempts=True, actor=actor)
        what = "reset its budget and put it back"
    else:
        c.unblock(pid, tid, resolution, reset_attempts=False, actor=actor)
        what = "put it back"
    st.pop("waiting", None)
    st["acts"] = acts + [now]
    _save(c.store, oid, state=st)
    return _action(c, P, o, what, reason, task=tid, stop_class=cls,
                   acts_today=len(st["acts"]), per_day=per_day)


# ---------------------------------------------------------------------------- view

def view(c, P, ended: int = 10) -> dict:
    """The orders of one project for the heartbeat, the page and the CLI."""
    rows = orders(c.store, P.id)
    now = c.clock()

    def one(o: dict) -> dict:
        st = o.get("state") or {}
        acts = [float(x) for x in st.get("acts") or []]
        return {"order_id": o["order_id"], "kind": o["kind"], "task_id": o["task_id"],
                "order": describe(o), "status": o["status"], "by": o["created_by"],
                "note": o["note"], "created_ts": o["created_ts"], "ended_ts": o["ended_ts"],
                "result": o["result"], "until": o.get("until") or {}, "params": o.get("params") or {},
                "waiting": st.get("waiting") or "", "actions": len(acts),
                "actions_today": len([a for a in acts if now - a < DAY_S]),
                "last_action_ts": acts[-1] if acts else None}
    live = [one(o) for o in rows if o["status"] == ACTIVE]
    old = [one(o) for o in rows if o["status"] != ACTIVE][-ended:]
    return {"active": live, "ended": list(reversed(old)), "kinds": list(KINDS)}

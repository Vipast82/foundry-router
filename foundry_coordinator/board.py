"""The status board: what the operator's "status" answered each time, built by the
coordinator from its own store (COORDINATOR_AUTOMATION_NEEDS items 26 and 12).

  seats       the workstation's main line, every configured slot and every lane without a
              Studio: the sprint on it, its state and phase, how long it has been there,
              what it is doing; or, for an idle one, why it is idle and what could go on it
  blockers    every stopped sprint (BLOCKED / FAILED / PAUSED / AWAITING_HUMAN) with how
              long, why, and the queued sprints that wait on it directly or through others
              (the most held-up first): what is blocking the critical path
  decisions   the open decisions, oldest first, with whose they are and their deadline
  done        sprints completed in the last 24 hours (the CLI also says "since the last
              check", a time it keeps in the store)
  orders      the active standing orders, in a line each
  machine     free and total RAM, and Kilo's memory with a pending restart, from psutil
              and the Foundry Client's resource watch (the slot disks are added by the
              release that measures them)

`build(c, P)` is the heartbeat's `board` block (the Automation page's Status tab adds the
models served in the router's last 100 requests, which only the router knows); `text()`
is the CLI's `status --board`. Read-only, no model call, never raises.
"""

from __future__ import annotations

import fnmatch
import time
from typing import Optional

from . import states as S

STOPPED = (S.BLOCKED, S.FAILED, S.PAUSED, S.AWAITING_HUMAN)
DAY_S = 86400.0
CHECK_META = "board_last_check:{pid}"


def _s(v, n: int = 200) -> str:
    return " ".join(str(v if v is not None else "").split())[:n]


def _since(c, pid: str, tid: str, state: str) -> float:
    """When the task entered its current state (its last transition into it)."""
    try:
        for h in reversed(c.store.history(pid, tid)):
            if h["to_state"] == state and h["from_state"] != state:
                return float(h["ts"] or 0)
    except Exception:                                            # noqa: BLE001
        pass
    return 0.0


def _row(c, pid: str, t: dict, now: float) -> dict:
    since = _since(c, pid, t["task_id"], t["state"])
    why = t.get("blocker") or t.get("awaiting") or t.get("pause_reason") or t.get("next_action")
    return {"sprint": t["task_id"], "state": t["state"], "phase": t.get("phase") or "",
            "since_ts": since or None,
            "minutes": int((now - since) // 60) if since else None,
            "doing": _s(why, 220)}


def _workstation_only(P, tid: str) -> bool:
    try:
        from .automation import get as auto_get
        return any(fnmatch.fnmatchcase(tid, g) for g in auto_get(P, "workstation_only_sprints") or [])
    except Exception:                                            # noqa: BLE001
        return False


def _slot_candidates(c, P, tasks: dict, queue: list) -> tuple:
    """(sprints that could start on a free slot now, why none) from the queue: never
    started, on no lane, every dependency complete, not workstation-only."""
    ready, first_wait = [], ""
    for q in queue:
        tid = q["task_id"]
        t = tasks.get(tid)
        if not t or t["state"] not in (S.QUEUED, S.READY) or q.get("track") or t.get("started_ts"):
            continue
        if _workstation_only(P, tid):
            first_wait = first_wait or f"{tid} is workstation-only"
            continue
        unmet = [d for d in q.get("deps") or []
                 if (tasks.get(d) or {}).get("state") != S.COMPLETE]
        if unmet:
            first_wait = first_wait or f"{tid} waits for {', '.join(unmet[:4])}"
            continue
        ready.append(tid)
    return ready, first_wait or "the queue has no sprint that has not started"


def seats(c, P) -> list:
    from .tracks.manager import LIVE_STATES
    pid, now = P.id, c.clock()
    tasks = {t["task_id"]: t for t in c.store.tasks(pid)}
    queue = c.store.queue(pid)
    order = {q["task_id"]: i for i, q in enumerate(queue)}
    on_track = {q["task_id"]: q.get("track") or "" for q in queue}
    out = []
    # the workstation's main line
    main = sorted((t for t in tasks.values() if not on_track.get(t["task_id"])
                   and t["state"] not in S.TERMINAL),
                  key=lambda t: (0 if t.get("started_ts") else 1, order.get(t["task_id"], 1e9)))
    busy = next((t for t in main if t["state"] in S.ACTIVE and t["state"] not in (S.QUEUED, S.READY)),
                None)
    seat = {"seat": "workstation", "kind": "workstation", "idle": busy is None}
    if busy is not None:
        seat.update(_row(c, pid, busy, now))
    elif main:
        t = main[0]
        seat.update(_row(c, pid, t, now))
        seat["why"] = (f"{t['task_id']} is {t['state']}: {_s(seat['doing'], 160)}"
                       if seat["doing"] else f"{t['task_id']} is {t['state']}")
    else:
        seat["why"] = "the queue has no main-line sprint"
    out.append(seat)
    # the slots and the lanes
    try:
        from . import slots as SL
        configured = SL.configured(P.profile)
    except Exception:                                            # noqa: BLE001
        configured = {}
    live = [r for r in c.store.tracks(pid) if r["state"] in LIVE_STATES]
    bound = {str((r.get("data") or {}).get("slot") or ""): r for r in live}
    candidates, none_why = _slot_candidates(c, P, tasks, queue)
    auto = ""
    if any(sid not in bound for sid in configured):
        try:
            from . import slotfill
            st = slotfill.state(c, P)
            nxt = st.get("next") or {}
            if nxt.get("sprint"):
                what = f"{nxt['sprint']} ({slotfill.RULE_WORDS[nxt['rule']]})"
                auto = (f"starts {what} by itself" if st["on"] else
                        f"automatic start is off (slot_auto_fill); it would start {what}")
            elif st.get("why"):
                auto = "nothing starts by itself: " + st["why"]
        except Exception:                                        # noqa: BLE001
            auto = ""

    def lane_seat(name: str, kind: str, r: dict) -> dict:
        cur = r.get("current_sprint") or next(
            (s for s in r.get("sprints") or [] if (tasks.get(s) or {}).get("state")
             not in S.TERMINAL), "")
        t = tasks.get(cur)
        st = {"seat": name, "kind": kind, "lane": r["name"], "lane_state": r["state"],
              "idle": not (t and t["state"] in S.ACTIVE and t["state"] not in (S.QUEUED, S.READY))}
        if (r.get("data") or {}).get("host"):            # Distributed Foundry phase b
            st["host"] = str(r["data"]["host"])
        if t:
            st.update(_row(c, pid, t, now))
            if st["idle"]:
                st["why"] = f"{cur} is {t['state']}" + (f": {_s(st['doing'], 160)}"
                                                        if st["doing"] else "")
        else:
            stage = (r.get("data") or {}).get("merge_stage")
            st["why"] = (f"lane {r['name']} has no open sprint"
                         + (f" (merge: {stage})" if stage else
                            f" ({r['state']})"))
        return st
    for sid in sorted(configured):
        r = bound.get(sid)
        if r is not None:
            out.append(lane_seat(sid, "slot", r))
            continue
        out.append({"seat": sid, "kind": "slot", "idle": True,
                    "why": "no lane is bound to it; " + (
                        "ready to start on a slot: " + ", ".join(candidates[:6]) if candidates
                        else "nothing is ready: " + none_why),
                    "could_run": candidates[:6], "auto": auto})
    for r in live:
        if str((r.get("data") or {}).get("slot") or "") in configured:
            continue
        out.append(lane_seat(f"lane {r['name']}", "lane", r))
    try:
        from . import slotkeep
        kept = slotkeep.view(c, P)
        for seat in out:
            if seat.get("kind") == "slot" and kept.get(seat["seat"]):
                seat["upkeep"] = slotkeep.words(kept[seat["seat"]])
                if kept[seat["seat"]].get("behind"):
                    seat["behind"] = kept[seat["seat"]]["behind"]
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import lane_tunnel
        tun = lane_tunnel.MANAGER.view()
        for seat in out:
            tv = tun.get(seat.get("seat")) if seat.get("kind") == "slot" else None
            if tv:
                seat["lane_tunnel"] = tv["state"] + (f": {tv['error']}" if tv.get("error") else "")
    except Exception:                                            # noqa: BLE001
        pass
    return out


def blockers(c, P) -> list:
    """Stopped sprints, each with the open sprints that wait on it (directly or through
    other waiting sprints), the most held-up first."""
    pid, now = P.id, c.clock()
    tasks = {t["task_id"]: t for t in c.store.tasks(pid)}
    queue = c.store.queue(pid)
    waits_on: dict = {}
    for q in queue:
        t = tasks.get(q["task_id"])
        if not t or t["state"] in S.TERMINAL:
            continue
        for d in q.get("deps") or []:
            if (tasks.get(d) or {}).get("state") != S.COMPLETE:
                waits_on.setdefault(d, []).append(q["task_id"])
    out = []
    for tid, t in tasks.items():
        if t["state"] not in STOPPED:
            continue
        held, todo = [], list(waits_on.get(tid) or [])
        while todo:
            x = todo.pop(0)
            if x not in held and x != tid:
                held.append(x)
                todo += waits_on.get(x) or []
        row = _row(c, pid, t, now)
        d = t.get("data") or {}
        out.append({**row, "holds_up": held[:12], "holds_up_n": len(held),
                    "waits_itself": bool(d.get("auto_heal") or d.get("hold")),
                    "lead_case": str(d.get("recovery_case") or "")})
    out.sort(key=lambda r: (-r["holds_up_n"], -(r["minutes"] or 0)))
    return out


def decisions(c, P) -> list:
    try:
        rows = c.store.q("SELECT decision_id, task_id, kind, question, opened_ts, deadline_ts, "
                         "category, decision_class FROM decisions WHERE project_id=? AND "
                         "status='open' ORDER BY opened_ts", (P.id,))
    except Exception:                                            # noqa: BLE001
        return []
    now = c.clock()
    out = []
    for r in rows:
        cat = str(r.get("category") or r.get("decision_class") or "")
        dl = float(r.get("deadline_ts") or 0)
        out.append({"decision_id": r["decision_id"], "task": r["task_id"], "kind": r["kind"],
                    "whose": "yours" if cat in ("product", "user_action") else "the coordinator's",
                    "category": cat, "question": _s(r.get("question"), 200),
                    "open_minutes": int((now - float(r.get("opened_ts") or now)) // 60),
                    "answers_itself_in_minutes": int((dl - now) // 60) if dl > now else None})
    return out


def done(c, P, since: Optional[float] = None) -> list:
    since = c.clock() - DAY_S if since is None else since
    rows = c.store.q("SELECT task_id, ts FROM transitions WHERE project_id=? AND to_state=? AND "
                     "ts>=? ORDER BY ts DESC", (P.id, S.COMPLETE, float(since)))
    seen, out = set(), []
    for r in rows:
        if r["task_id"] not in seen:
            seen.add(r["task_id"])
            out.append({"sprint": r["task_id"], "ts": float(r["ts"] or 0)})
    return out


def machine() -> dict:
    """RAM from psutil where it is installed; Kilo's memory and whether its restart is
    pending from the Foundry Client's resource watch, when a client runs here."""
    out: dict = {}
    try:
        import psutil
        vm = psutil.virtual_memory()
        out.update(ram_free_gb=round(vm.available / 2 ** 30, 1),
                   ram_total_gb=round(vm.total / 2 ** 30, 1))
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import client_api
        api = client_api.discover()
        rs = (api.status().get("resources") or {}) if api is not None else {}
        kilo = rs.get("kilo") or {}
        if kilo.get("gb") is not None:
            out["kilo_gb"] = kilo["gb"]
            if kilo.get("pending"):
                out["kilo_restart"] = "pending"
        if "ram_free_gb" not in out and rs.get("free_gb") is not None:
            out.update(ram_free_gb=rs["free_gb"], ram_total_gb=rs.get("total_gb"))
    except Exception:                                            # noqa: BLE001
        pass
    return out


def build(c, P) -> dict:
    """The whole board of one project. Each part fails alone (its `error`), never the board."""
    out: dict = {"ts": c.clock(), "project": P.id}

    def part(name, fn, default):
        try:
            out[name] = fn()
        except Exception as e:                                   # noqa: BLE001
            out[name] = default
            out.setdefault("errors", {})[name] = f"{type(e).__name__}: {e}"[:200]
    part("seats", lambda: seats(c, P), [])
    part("blockers", lambda: blockers(c, P), [])
    part("decisions", lambda: decisions(c, P), [])
    part("done", lambda: done(c, P), [])
    part("machine", machine, {})

    def _orders():
        from . import orders as O
        return [{"order_id": o["order_id"], "order": o["order"], "by": o["by"],
                 "waiting": o["waiting"], "actions": o["actions"]}
                for o in O.view(c, P)["active"]]
    part("orders", _orders, [])

    def _budget():
        from . import budget_mode as BM
        d = BM.describe(P)
        return {**d, "text": BM.words(d)}
    part("budget", _budget, {})
    proj = c.store.project(P.id) or {}
    out["paused"] = bool(proj.get("paused"))
    out["pause_note"] = _s(proj.get("pause_note"), 200) if proj.get("paused") else ""
    return out


def _mins(m) -> str:
    if m is None:
        return "?"
    return f"{m} min" if m < 120 else f"{m / 60:.1f} h"


def text(b: dict, last_check: Optional[float] = None, models: Optional[dict] = None) -> str:
    """The board as the CLI prints it."""
    L = [f"{b.get('project')}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(b.get('ts') or 0))}"
         + ("  PROJECT PAUSED: " + b["pause_note"] if b.get("paused") else "")]
    L.append("\nSeats")
    for s in b.get("seats") or []:
        if s.get("idle"):
            L.append(f"  {s['seat']:13} idle: {s.get('why') or ''}")
            if s.get("auto"):
                L.append(f"  {'':13} {s['auto']}")
        if s.get("upkeep"):
            L.append(f"  {'':13} {s['upkeep']}")
        else:
            L.append(f"  {s['seat']:13} {s.get('sprint')}  {s.get('state')}"
                     + (f"/{s['phase']}" if s.get("phase") else "")
                     + f"  for {_mins(s.get('minutes'))}  {s.get('doing') or ''}")
    bl = b.get("blockers") or []
    L.append(f"\nStopped ({len(bl)})")
    for r in bl:
        L.append(f"  {r['sprint']:13} {r['state']} for {_mins(r.get('minutes'))}"
                 + (f"; holds up {r['holds_up_n']}: {', '.join(r['holds_up'][:6])}"
                    if r["holds_up_n"] else "")
                 + ("; waits by itself" if r.get("waits_itself") else "")
                 + f"\n{'':16}{r.get('doing') or ''}")
    ds = b.get("decisions") or []
    L.append(f"\nOpen decisions ({len(ds)})")
    for d in ds:
        L.append(f"  {d['task']:13} {d['whose']} ({d['kind']}), open {_mins(d['open_minutes'])}"
                 + (f", answers itself in {_mins(d['answers_itself_in_minutes'])}"
                    if d.get("answers_itself_in_minutes") is not None else "")
                 + f": {d['question']}")
    dn = b.get("done") or []
    if last_check:
        new = [r for r in dn if r["ts"] > last_check]
        L.append(f"\nDone since the last check ("
                 f"{time.strftime('%m-%d %H:%M', time.localtime(last_check))}): "
                 + (", ".join(r["sprint"] for r in new) or "none"))
    L.append(f"Done in the last 24 h ({len(dn)}): " + (", ".join(r["sprint"] for r in dn) or "none"))
    od = b.get("orders") or []
    if od:
        L.append(f"\nStanding orders ({len(od)})")
        for o in od:
            L.append(f"  {o['order']} (from {o['by']}; acted {o['actions']}x)"
                     + (f"; waiting: {o['waiting']}" if o.get("waiting") else ""))
    if (b.get("budget") or {}).get("mode", "normal") != "normal":
        L.append("\nBudget: " + b["budget"]["text"])
    m = b.get("machine") or {}
    if m:
        L.append("\nMachine: " + ", ".join(
            f"{k.replace('_gb', '').replace('_', ' ')} {v} GB" if k.endswith("_gb") else f"{k} {v}"
            for k, v in m.items()))
    if models:
        L.append("Models, last 100 requests: " + ", ".join(f"{k} {v}" for k, v in models.items()))
    if b.get("errors"):
        L.append("\nNot read: " + "; ".join(f"{k}: {v}" for k, v in b["errors"].items()))
    return "\n".join(L)


def last_check(c, pid: str, mark: bool = False) -> Optional[float]:
    """When `status --board` was last run for the project; `mark` records now."""
    key = CHECK_META.format(pid=pid)
    try:
        v = float(c.store.meta(key) or 0) or None
    except ValueError:
        v = None
    if mark:
        c.store.set_meta(key, repr(time.time()))
    return v

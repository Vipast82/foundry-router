"""Which model is serving the sprints, and what to do when the paid ones are out
(COORDINATOR_AUTOMATION_NEEDS items 7b and 7c).

2026-11-20: Claude answered 503 "overloaded" and was (wrongly, since fixed in the router)
backed off for 30 minutes; the work fell through to GPT, which had no credits, and then to
the local Qwen. Nobody was told that sprints written for Sonnet were running on a 3-bit
local model, and nothing stopped them from doing so.

  the alert   every SERVED_EVERY_S each working implementer session is looked up in the
              router's ledger (`served_models`: the routes that served it). A route that
              is not the first of the session's persona raises one alert (class `watch`)
              per session and route: which route of how many, which one was expected, and
              what the router says about the routes above it. A persona whose first route
              is local is, of course, expected to run locally.
              [automation] served_model_alert (default on).
  the pause   before a session starts: when the persona's first route is a paid one and
              EVERY paid route of it is out - each account either marked unavailable or
              with a usage window at or over that backend's own cutoff - the start waits
              until the earliest of them is back (the reset time the router reports),
              instead of running on the local model. No attempt is used; one notice says
              until when. A route the router cannot read is not "out": nothing is paused
              on a guess. [automation] pause_when_paid_models_out (default OFF: today's
              fall-through to local stays until it is switched on).

The pause judges each route against the backend's own cutoff (`primary_cutoff_pct` in the
router's admission answer), not the lower cutoff the router uses for optional helper work.
"""

from __future__ import annotations

import time
from typing import Optional

from . import states as S
from .automation import get as auto_get

SERVED_EVERY_S = 300.0
ADMISSION_CACHE_S = 120.0
PERSONA_CACHE_S = 600.0
ALERT_AGAIN_S = 6 * 3600.0


def _name(r: dict) -> str:
    return f"{r.get('backend') or '?'}/{r.get('model') or '?'}"


def persona_routes(c, P, persona: str) -> list:
    """The persona's routes in order, [{backend, model}], from the router (cached)."""
    cache = c.__dict__.setdefault("_persona_routes", {})
    now = c.clock()
    hit = cache.get((P.id, persona))
    if hit and now - hit[0] < PERSONA_CACHE_S:
        return hit[1]
    routes: list = []
    try:
        info = P.foundry.persona_info(persona) if P.foundry is not None else None
        routes = [{"backend": str(r.get("backend") or ""), "model": str(r.get("model") or "")}
                  for r in (info or {}).get("routes") or [] if isinstance(r, dict)]
    except Exception:                                            # noqa: BLE001
        routes = hit[1] if hit else []
    cache[(P.id, persona)] = (now, routes)
    return routes


def admission(c, P, persona: str) -> Optional[dict]:
    """The router's admission answer for the persona (cached ADMISSION_CACHE_S), or None."""
    cache = c.__dict__.setdefault("_admission", {})
    now = c.clock()
    hit = cache.get((P.id, persona))
    if hit and now - hit[0] < ADMISSION_CACHE_S:
        return hit[1]
    try:
        ans = P.foundry.helper_admission(persona) if P.foundry is not None else None
    except Exception:                                            # noqa: BLE001
        ans = None
    cache[(P.id, persona)] = (now, ans if isinstance(ans, dict) else None)
    return cache[(P.id, persona)][1]


# ---------------------------------------------------------------------------- 7c the pause

def route_out(route: dict, now: float) -> Optional[dict]:
    """A paid route that cannot serve now: {"until": ts | None, "why"}; None when it can, or
    when nothing readable says it cannot (never a guess)."""
    if str(route.get("source") or "") == "local":
        return None
    accounts = [a for a in route.get("accounts") or [] if isinstance(a, dict)]
    if not accounts:
        return None
    try:
        cutoff = float(route.get("primary_cutoff_pct"))
    except (TypeError, ValueError):
        return None
    back, whys = [], []
    for a in accounts:
        if a.get("unavailable"):
            back.append(None)
            whys.append(f"{a.get('label') or a.get('account')}: marked unavailable")
            continue
        over = [w for w in a.get("windows") or [] if isinstance(w, dict)
                and w.get("applies") is not False and w.get("used") is not None
                and float(w["used"]) >= cutoff]
        if not over:
            return None                  # this account can still serve: the route is not out
        resets = [float(w["resets_in_s"]) for w in over if w.get("resets_in_s") is not None]
        back.append(now + max(resets) if resets and len(resets) == len(over) else None)
        whys.append(", ".join(f"{w.get('name')} {float(w['used']):.0f}%" for w in over)
                    + f" (cutoff {cutoff:g}%)")
    known = [b for b in back if b is not None]
    return {"until": min(known) if known else None, "why": "; ".join(whys)[:300]}


def paid_out(ans: Optional[dict], now: float) -> Optional[dict]:
    """Every paid route of the persona is out and its first route is a paid one:
    {"until", "why", "local"}; else None."""
    routes = [r for r in (ans or {}).get("routes") or [] if isinstance(r, dict)]
    if not routes or str(routes[0].get("source") or "") == "local":
        return None
    paid = [r for r in routes if str(r.get("source") or "") != "local"]
    outs = []
    for r in paid:
        o = route_out(r, now)
        if o is None:
            return None
        outs.append((r, o))
    known = [o["until"] for _, o in outs if o["until"] is not None]
    local = next((_name(r) for r in routes if str(r.get("source") or "") == "local"), "")
    return {"until": min(known) if known else None, "local": local,
            "why": "; ".join(f"{_name(r)}: {o['why']}" for r, o in outs)[:600]}


def start_hold(c, P, t: dict, persona: str) -> str:
    """Why this session must not start now ('' = start): see "the pause" above."""
    try:
        if not auto_get(P, "pause_when_paid_models_out") or P.foundry is None or not persona:
            return ""
    except Exception:                                            # noqa: BLE001
        return ""
    now = c.clock()
    out = paid_out(admission(c, P, persona), now)
    marks = c.__dict__.setdefault("_paid_out", {})
    key = (P.id, persona)
    if out is None:
        if marks.pop(key, None):
            c.store.event(P.id, "", "paid_models_back", detail={"persona": persona,
                                                               "digest": True})
        return ""
    until = out["until"]
    when = time.strftime("%H:%M", time.localtime(until)) if until else "the router says so"
    if key not in marks:
        marks[key] = now
        c.store.event(P.id, t["task_id"], "operator_notice", detail={
            "text": (f"Every paid model of {persona} is at its cutoff ({out['why']}). Its "
                     f"sprints wait until {when} instead of running on "
                     f"{out['local'] or 'the local model'}; no attempt is used. Switch "
                     f"pause_when_paid_models_out off to let them run locally.")[:900],
            "kind": "paid_models_out", "digest": True, "persona": persona,
            "until": until, "why": out["why"]})
    return (f"waiting: every paid model of {persona} is at its cutoff; starts again at {when} "
            f"(no attempt is used)")


# ---------------------------------------------------------------------------- 7b the alert

def sweep(c, P) -> list:
    """Look up each working implementer session's served routes (at most every
    SERVED_EVERY_S) and alert on one that is not its persona's first. Never raises."""
    try:
        if not auto_get(P, "served_model_alert") or P.foundry is None:
            return []
        last = c.__dict__.setdefault("_modelwatch_at", {})
        now = c.clock()
        if now - last.get(P.id, -1e18) < SERVED_EVERY_S:
            return []
        last[P.id] = now
    except Exception:                                            # noqa: BLE001
        return []
    out = []
    for t in c.store.tasks(P.id):
        if t["state"] != S.IMPLEMENTING or not t.get("session_id"):
            continue
        try:
            got = check_served(c, P, t)
        except Exception as e:                                   # noqa: BLE001
            got = None
            try:
                c.log(f"{P.id}/{t['task_id']}: served-model check failed: {type(e).__name__}: {e}")
            except Exception:                                    # noqa: BLE001
                pass
        if got:
            out.append(got)
    return out


def check_served(c, P, t: dict) -> Optional[dict]:
    pid, tid, sid = P.id, t["task_id"], str(t["session_id"])
    sess = c.store.session(sid) or {}
    persona = str(sess.get("persona") or "")
    if not persona:
        return None
    routes = persona_routes(c, P, persona)
    if not routes:
        return None
    served = P.foundry.served_models(sid) or {}
    used = [r for r in served.get("routes") or [] if isinstance(r, dict) and r.get("model")]
    if not used:
        return None
    first = routes[0]
    names = [_name(r) for r in routes]
    seen = dict((t.get("data") or {}).get("served_alerts") or {})
    now = c.clock()
    lower = []
    for r in used:
        n = _name(r)
        if n == _name(first) or r.get("model") == first["model"] and not r.get("backend"):
            continue
        rank = names.index(n) + 1 if n in names else 0
        key = f"{sid}|{n}"
        if now - float(seen.get(key) or -1e18) < ALERT_AGAIN_S:
            continue
        seen[key] = now
        lower.append({"route": n, "rank": rank})
    if not lower:
        return None
    worst = max(lower, key=lambda x: (x["rank"] == 0, x["rank"]))
    above = ""
    ans = admission(c, P, persona)
    if ans:
        by = {_name(r): r for r in ans.get("routes") or [] if isinstance(r, dict)}
        stop = names.index(worst["route"]) if worst["route"] in names else len(names)
        above = "; ".join(f"{n}: {by[n].get('state')} ({str(by[n].get('reason') or '')[:120]})"
                          for n in names[:stop] if n in by)
    where = (f"route {worst['rank']} of {len(routes)} of {persona}" if worst["rank"]
             else f"a route that is not on {persona}'s list")
    text = (f"{tid} is being served by {worst['route']}, {where}; expected {names[0]}."
            + (f" The routes above it: {above}." if above else ""))
    # A saving budget mode lowers the cutoffs on purpose: a fall to the next route because
    # every route above is at its cutoff is what was asked for. It is logged, not alerted.
    asked = ""
    try:
        from . import budget_mode
        if budget_mode.mode(P) != "normal" and ans and worst["rank"] > 1:
            by = {_name(r): r for r in ans.get("routes") or [] if isinstance(r, dict)}
            if all(n in by and route_out(by[n], now) is not None
                   for n in names[:worst["rank"] - 1]):
                asked = budget_mode.LABEL[budget_mode.mode(P)]
    except Exception:                                            # noqa: BLE001
        asked = ""
    if asked:
        text += f' This is what budget mode "{asked}" asks for.'
    c.store.merge_data(pid, tid, served_alerts={k: v for k, v in seen.items()
                                                if now - float(v) < 7 * 86400})
    detail = {"text": text[:900], "kind": "lower_model",
              "task_id": tid, "session_id": sid, "persona": persona, "expected": names[0],
              "served": [x["route"] for x in lower], "rank": worst["rank"], "of": len(routes)}
    if asked:
        detail["digest"] = True
    else:
        detail.update(alert_class="watch",
                      title=f"{tid} is on a lower model than its persona's first")
    c.store.event(pid, tid, "operator_notice", session_id=sid, detail=detail)
    return detail

"""Owner Studio reservation: the workstation's Studio is the owner's for a while.

2026-11-02 (GF-RT4 live test): the operator reserved Studio by writing AU's studio.lock by hand.
The line named no process, so AU's lock tool called it stale after 30 minutes and a session broke
it; a track merge's Studio batch then ran on the owner's Studio while he was playing, went red and
reopened two finished sprints.

A reservation lives in the coordinator's own store (meta `studio_reserved:<project>`), with an end
time, so no lock tool can judge it stale. While it is active:
  * no scripted Studio run starts on the workstation (studio_calls; slots are not affected);
  * no track merge takes the Studio lease (tracks/resources.admit_studio);
  * no main-checkout session starts (engine._start_implementation);
  * main-checkout tasks that were writing when it began are held (owner `studio_reserve:<project>`)
    and the self-repair sweep resumes them when the reservation ends (released or expired).
"""

from __future__ import annotations

import json
import time
from typing import Optional

from . import states as S

KEY = "studio_reserved:{pid}"
OWNER_KIND = "studio_reserve"


def get(store, pid: str, now: Optional[float] = None) -> Optional[dict]:
    """The active reservation of `pid`, or None (none, or expired)."""
    try:
        raw = store.meta(KEY.format(pid=pid))
        r = json.loads(raw) if raw else None
    except Exception:                                            # noqa: BLE001
        return None
    if not isinstance(r, dict):
        return None
    now = time.time() if now is None else now
    until = float(r.get("until") or 0)
    if until and now >= until:
        return None
    return r


def note(r: dict) -> str:
    until = float(r.get("until") or 0)
    when = time.strftime("%H:%M", time.localtime(until)) if until else "released"
    why = str(r.get("reason") or "").strip()
    return f"Studio reserved for the owner until {when}" + (f" ({why[:120]})" if why else "")


def blocks(c, P) -> str:
    """Why Studio work on the workstation must wait ('' = free). A track bound to a Studio
    slot is never blocked: the reservation is about the owner's Studio."""
    try:
        if c.tracks.is_track(P):
            from . import slots
            if slots.slot_for(c, P) is not None:
                return ""
        main = c.tracks.main_of(P)
    except Exception:                                            # noqa: BLE001
        main = P
    r = get(c.store, main.id, c.clock())
    return note(r) if r else ""


def reserve(c, pid: str, minutes: float, reason: str = "", by: str = "operator") -> dict:
    minutes = float(minutes or 0)
    if minutes <= 0 or minutes > 24 * 60:
        raise ValueError("reserve between 1 minute and 24 hours")
    now = c.clock()
    r = {"by": by, "reason": str(reason or "")[:300], "since": now, "until": now + minutes * 60}
    c.store.set_meta(KEY.format(pid=pid), json.dumps(r))
    held = []
    for t in c.store.tasks(pid):
        if t["state"] not in S.WRITING or c.store.task_track(pid, t["task_id"]):
            continue
        c.hold(pid, t["task_id"], f"{OWNER_KIND}:{pid}", note(r), actor=by)
        held.append(t["task_id"])
    c.store.event(pid, "", "studio_reserved", detail={**r, "held": held})
    return {**r, "held": held}


def release(c, pid: str, by: str = "operator") -> bool:
    had = c.store.meta(KEY.format(pid=pid))
    c.store.x("DELETE FROM meta WHERE key=?", (KEY.format(pid=pid),))
    c.store.event(pid, "", "studio_released", detail={"by": by, "was": bool(had)})
    return bool(had)


def _owner_alive(c, ref: str):
    """selfheal hold owner `studio_reserve:<project>`: alive while the reservation is active."""
    return get(c.store, ref, c.clock()) is not None


def register() -> None:
    from . import selfheal
    selfheal.register_hold_owner(OWNER_KIND, _owner_alive)


register()

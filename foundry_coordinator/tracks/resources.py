"""Resources with capacity and the Studio scheduler (Team Lead piece 3).

Resources are rows of the generic `locks` table, one row per unit of capacity
(`studio`, `gpu`, `blender`: capacity 1 each), next to the writer slots and
the profile's exclusive names the engine already takes. A track's sprint takes
the rows its needs map to (`needs_locks`); Studio is never one of them: a
worktree cannot drive the one Rojo-served checkout.

Studio "in use" is a fact about the MAIN checkout: the studio_gate runner lock
(`.plan/tmp/gate/runner.lock` with a live pid) or the main session's last Studio
tool call, read from its transcript (`observe_transcript`), not the session's
existence: a four-hour implementer session that only edits files does not pin
Studio.

A track's merge gate is admitted to Studio only when (`admit_studio`):
  * no Studio resource is held by anyone else (the main sprint's own locks win);
  * the main sprint has not used Studio for `studio_idle_minutes` (default 10);
  * no main request is pending: no main task about to start a session or to run
    its gate, and no main session live in VERIFYING.
The lease is bounded by `studio_track_max_minutes` (default 45: one gate run)
and is never interrupted mid-run; a main request waits for the run to end
(the engine's own `_acquire` simply finds the lock held). Everything is
journaled in `resource_journal` (who held what, for what, how long); the GUI's
Studio timeline is `studio_view()`.
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from typing import Callable, Optional

from .. import states as S

CAPACITY = {"studio": 1, "gpu": 1, "blender": 1}
STUDIO_TOOL_GLOBS = ("Roblox_Studio_*", "robloxstudio-mcp_*", "robloxstudio_mcp_*")
# needs (analysis.py) -> capacity resources a track sprint holds while it runs
NEED_RESOURCES = {"blender": ("blender",), "gpu": ("gpu",), "audio": ("gpu",),
                  "images": ("gpu",)}
MAIN_STUDIO_STATES = (S.VERIFYING,)                 # a main session live in VERIFYING
MAIN_PENDING_STATES = (S.READY, S.CHANGES_REQUIRED, S.VERIFYING, S.APPROVED, S.CLOSING)
OBSERVE_EVERY_S = 120


def needs_locks(needs) -> list[str]:
    out: list[str] = []
    for n in needs or []:
        for r in NEED_RESOURCES.get(str(n), ()):
            if r not in out:
                out.append(r)
    return out


def is_studio_tool(name: str) -> bool:
    return any(fnmatch.fnmatchcase(str(name or ""), g) for g in STUDIO_TOOL_GLOBS)


def _pid_alive(pid: int) -> bool:
    try:
        from ..autoheal import pid_alive
        return bool(pid_alive(int(pid)))
    except Exception:                                             # noqa: BLE001
        return False


def runner_lock(workspace: str) -> Optional[dict]:
    """The studio_gate runner lock of a checkout when its pid is alive."""
    p = os.path.join(os.path.abspath(workspace), ".plan", "tmp", "gate", "runner.lock")
    try:
        with open(p, encoding="utf-8") as f:
            cur = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cur, dict):
        return None
    pid = int(cur.get("pid") or 0)
    if not pid or not _pid_alive(pid):
        return None
    return {"pid": pid, "sprint": str(cur.get("sprint") or ""), "path": p}


class ResourceManager:
    def __init__(self, c, clock: Callable[[], float] = time.time):
        self.c = c
        self.store = c.store
        self.clock = clock
        self._wait_noted: dict = {}
        self._wait_since: dict = {}             # (owner, resource) -> first wait ts

    # ------------------------------------------------------------------ settings

    def setting(self, P, key: str):
        from ..automation import get
        return get(P, key)

    def idle_minutes(self, P) -> float:
        return float(self.setting(P, "studio_idle_minutes") or 0)

    def max_minutes(self, P) -> float:
        return float(self.setting(P, "studio_track_max_minutes") or 45)

    # ------------------------------------------------------------------ Studio facts

    def studio_resources(self, P) -> list[str]:
        """The rows a Studio lease takes: `studio` plus the profile's own exclusive
        names (studio:<host>, rojo:<port>), so the main sprint's locks and a
        track's lease contend on the same rows."""
        return ["studio"] + [r for r in P.profile.resources
                             if r.split(":")[0] in ("studio", "rojo")]

    def note_studio_call(self, P, ts: float, session_id: str = "") -> None:
        key = f"studio_last_call:{P.id}"
        cur = float(self.store.meta(key) or 0)
        if ts > cur:
            self.store.set_meta(key, repr(float(ts)))
            self.store.set_meta(f"studio_last_session:{P.id}", session_id or "")

    def observe_transcript(self, P, msgs: list, session_id: str = "") -> int:
        """Record the newest Studio tool call of a main session's transcript (Kilo
        message shape: parts[].type == 'tool', .tool, .state.time). Returns the
        number of Studio calls seen."""
        n = 0
        newest = 0.0
        for m in msgs or []:
            if not isinstance(m, dict):
                continue
            info = m.get("info") or {}
            mts = float(((info.get("time") or {}).get("created") or 0)) / 1000
            for p in m.get("parts") or []:
                if not isinstance(p, dict) or p.get("type") != "tool":
                    continue
                if not is_studio_tool(p.get("tool") or ""):
                    continue
                st = p.get("state") or {}
                pt = (st.get("time") or {}) if isinstance(st, dict) else {}
                ts = float(pt.get("end") or pt.get("start") or 0) / 1000 or mts
                n += 1
                newest = max(newest, ts)
        if newest:
            self.note_studio_call(P, newest, session_id)
        return n

    def last_studio_call(self, P) -> Optional[float]:
        v = self.store.meta(f"studio_last_call:{P.id}")
        return float(v) if v else None

    def main_tasks(self, P) -> list[dict]:
        return [t for t in self.store.tasks(P.id)
                if not self.store.task_track(P.id, t["task_id"])]

    def studio_in_use(self, P) -> str:
        """Why Studio counts as in use by the main checkout ('' = idle)."""
        lock = runner_lock(P.profile.workspace)
        if lock:
            return f"gate runner pid {lock['pid']} ({lock['sprint'] or 'gate'})"
        last = self.last_studio_call(P)
        if last is not None:
            age = self.clock() - last
            if age < self.idle_minutes(P) * 60:
                return f"main session used Studio {int(age)}s ago"
        return ""

    def main_pending(self, P) -> str:
        """A main request for Studio that a track lease would delay ('' = none)."""
        for t in self.main_tasks(P):
            if t["state"] in MAIN_STUDIO_STATES and t.get("session_id"):
                return f"main {t['task_id']} is {t['state']} with a live session"
            if t["state"] in MAIN_PENDING_STATES:
                # 2026-10-29: a main sprint the coordinator itself is holding back (landing
                # drain, one-started-sprint rule) is not about to use Studio; counting it
                # starved the assets-local merge's Studio batch all night
                held = str(t.get("next_action") or "")
                if "(landing drain)" in held or "one started main-checkout sprint" in held:
                    continue
                # 2026-10-30: AS-P7 READY "waiting for workspace ... (held by AS-J08)" and
                # sprints waiting for gpu / a writer slot / a dependency are not about to use
                # Studio either; five finished tracks waited on them from 11:41
                if held.startswith("waiting for"):
                    continue
                return f"main {t['task_id']} is {t['state']} (about to use Studio)"
        return ""

    def holder(self, P, resource: str = "studio") -> Optional[dict]:
        rows = [l for l in self.store.locks() if l["resource"] == resource]
        return rows[0] if rows else None

    # ------------------------------------------------------------------ leases

    def owner_id(self, track: str) -> str:
        return f"track:{track}"

    def lease(self, P) -> Optional[dict]:
        v = self.store.meta(f"studio_lease:{P.id}")
        try:
            return json.loads(v) if v else None
        except ValueError:
            return None

    def admit_studio(self, P, track: str, purpose: str,
                     minutes: Optional[float] = None) -> tuple[bool, str]:
        """Take the Studio lease for a track's merge gate, or say why not. `minutes`
        bounds the lease (default studio_track_max_minutes; a merge Studio batch
        passes its own bound)."""
        owner = self.owner_id(track)
        cur = self.lease(P)
        if cur and cur.get("owner") == owner:
            return True, "already held"
        why = self.studio_in_use(P)
        if not why:
            why = self.main_pending(P)
            since = self._wait_since.get((owner, "studio"))
            prio = float(self.setting(P, "merge_studio_priority_minutes") or 20)
            if why and since is not None and self.clock() - since >= prio * 60:
                # 2026-10-30: a stream of main sprints kept a merge batch waiting for hours;
                # past the priority window the merge goes first (main sessions wait on the
                # Studio lock it takes)
                self.store.journal("studio", P.id, owner, "priority", purpose,
                                   {"waited_s": round(self.clock() - since), "over": why})
                why = ""
        if not why:
            held = self.store.acquire(self.studio_resources(P), P.id, owner)
            if held:
                why = f"{held['resource']} held by {held['project_id']}/{held['task_id']}"
        if why:
            self._wait(P, owner, purpose, why)
            return False, why
        now = self.clock()
        mins = float(minutes) if minutes else self.max_minutes(P)
        lease = {"owner": owner, "track": track, "purpose": purpose, "since": now,
                 "until": now + mins * 60, "minutes": mins,
                 "resources": self.studio_resources(P)}
        self.store.set_meta(f"studio_lease:{P.id}", json.dumps(lease))
        self._wait_noted.pop((owner, "studio"), None)
        self._wait_since.pop((owner, "studio"), None)
        self.store.journal("studio", P.id, owner, "acquire", purpose,
                           {"lease_minutes": mins,
                            "resources": lease["resources"]}, ts=now)
        self.store.event(P.id, "", "studio_lease", detail={"track": track, "purpose": purpose,
                                                           "until": lease["until"]})
        return True, ""

    def release_studio(self, P, track: str, why: str = "") -> None:
        owner = self.owner_id(track)
        lease = self.lease(P)
        if lease and lease.get("owner") == owner:
            self.store.x("DELETE FROM meta WHERE key=?", (f"studio_lease:{P.id}",))
            held = self.clock() - float(lease.get("since") or self.clock())
            self.store.journal("studio", P.id, owner, "release", lease.get("purpose") or "",
                               {"held_s": round(held), "why": why,
                                "over_lease": held > float(lease.get("minutes") or
                                                           self.max_minutes(P)) * 60})
        self.store.release(P.id, owner)

    def lease_over(self, P) -> bool:
        lease = self.lease(P)
        return bool(lease) and self.clock() > float(lease.get("until") or 0)

    def _wait(self, P, owner: str, purpose: str, why: str, resource: str = "studio") -> None:
        self._wait_since.setdefault((owner, resource), self.clock())
        if self._wait_noted.get((owner, resource)) == why:
            return
        self._wait_noted[(owner, resource)] = why
        self.store.journal(resource, P.id, owner, "wait", purpose, {"why": why})

    # ------------------------------------------------------------------ track resources

    def acquire_for_track(self, P, track: str, needs) -> Optional[dict]:
        """Capacity resources a track's sprint needs (gpu, blender); None on
        success, else the held row."""
        res = needs_locks(needs)
        if not res:
            return None
        owner = self.owner_id(track)
        held = self.store.acquire(res, P.id, owner)
        if held:
            self._wait(P, owner, "sprint", f"{held['resource']} held by "
                                          f"{held['project_id']}/{held['task_id']}",
                       resource=held["resource"])
            return held
        for r in res:
            self._wait_noted.pop((owner, r), None)
            self.store.journal(r, P.id, owner, "acquire", "sprint")
        return None

    def release_for_track(self, P, track: str) -> None:
        owner = self.owner_id(track)
        held = [r for r in self.store.holds(P.id, owner) if r in CAPACITY]
        for r in held:
            self.store.journal(r, P.id, owner, "release", "sprint")
        self.store.release(P.id, owner, held)

    # ------------------------------------------------------------------ main-sprint journal

    def on_locks(self, P, t: dict, resources: list[str], event: str) -> None:
        """The engine's own Studio locks (session start / gate / release), journaled
        under the task so the timeline shows the main sprint's holds too."""
        for r in resources:
            if r.split(":")[0] in ("studio", "rojo") or r in CAPACITY:
                self.store.journal(r, P.id, f"{P.id}/{t['task_id']}", event,
                                   str(t.get("phase") or t.get("state") or ""))

    def tick(self, P) -> None:
        """Close journal entries whose lock is gone (a terminal transition drops
        locks without passing through the engine's release)."""
        open_ = self.open_holds("studio", P.id)
        live = {(l["project_id"], l["task_id"]) for l in self.store.locks()
                if l["resource"] == "studio" or l["resource"].split(":")[0] == "studio"}
        for owner in open_:
            if (P.id, owner.split("/", 1)[1] if owner.startswith(P.id + "/") else owner) not in live:
                self.store.journal("studio", P.id, owner, "release", "", {"inferred": True})

    def open_holds(self, resource: str, project_id: str) -> list[str]:
        """Owners whose last journal entry for `resource` is an acquire."""
        last: dict[str, str] = {}
        for r in self.store.journal_rows(resource, project_id, limit=500):
            if r["event"] in ("acquire", "release"):
                last[r["owner"]] = r["event"]
        return [o for o, e in last.items() if e == "acquire"]

    # ------------------------------------------------------------------ GUI data

    def studio_view(self, P, limit: int = 60) -> dict:
        last = self.last_studio_call(P)
        now = self.clock()
        return {"in_use": self.studio_in_use(P) or "",
                "pending": self.main_pending(P) or "",
                "holder": self.holder(P, "studio"),
                "profile_holders": [self.holder(P, r) for r in self.studio_resources(P)[1:]],
                "last_main_call_age_s": None if last is None else round(now - last),
                "idle_minutes": self.idle_minutes(P), "max_minutes": self.max_minutes(P),
                "lease": self.lease(P),
                "runner_lock": runner_lock(P.profile.workspace),
                "journal": self.store.journal_rows(None, P.id, limit=limit),
                "capacity": dict(CAPACITY),
                "held": {r: self.holder(P, r) for r in CAPACITY}}

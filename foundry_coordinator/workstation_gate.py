"""One active workstation per project (docs/plans/LINUX_WORKSTATION_PLAN.md W4).

At start and every minute the coordinator asks the router (POST
/v1/foundry/control/workstation-active) whether it holds each project. The first coordinator to
ask holds it; another one is on STANDBY for that project: the engine skips the project
entirely (no new sprints, merges or Lead calls) until Access → Machines → Make active moves it.

It fails open: no router client, an old router (404), a network error or any other unexpected
answer leaves the project running as before. Only a definite "another workstation holds it"
puts a project on standby. `workspace.single_workstation = false` turns the check off.
"""
from __future__ import annotations

import socket
import time
from typing import Callable, Optional

INTERVAL = 60


def wanted(P) -> bool:
    ws = ((getattr(P.profile, "raw", None) or {}).get("workspace") or {})
    return ws.get("single_workstation", True) is not False


class WorkstationGate:
    def __init__(self, coordinator, log: Callable[[str], None] = lambda m: None,
                 clock: Callable[[], float] = time.time, name: str = ""):
        self.c = coordinator
        self.log = log
        self.clock = clock
        self.name = name or socket.gethostname()
        self._last = 0.0
        self.standby: dict[str, dict] = {}         # project -> the holder (only on standby)

    def on_standby(self, pid: str) -> Optional[dict]:
        return self.standby.get(pid)

    def poll(self) -> None:
        if self.clock() - self._last < INTERVAL:
            return
        self.check()

    def check(self) -> dict:
        self._last = self.clock()
        out = {}
        for pid in sorted(self.c.projects):
            P = self.c.projects[pid]
            if not wanted(P):
                self._set(pid, None, "the check is off (workspace.single_workstation)")
                out[pid] = "off"
                continue
            client = getattr(P, "foundry", None)
            if client is None:
                self._set(pid, None, "")
                out[pid] = "no router"
                continue
            try:
                data, _ = client._req("POST", "/v1/foundry/control/workstation-active",
                                      {"project": pid, "name": self.name}, timeout=15)
            except Exception as e:                               # noqa: BLE001 (fail open)
                self._set(pid, None, "")
                out[pid] = f"unknown ({type(e).__name__})"
                continue
            if isinstance(data, dict) and data.get("active") is False and data.get("holder"):
                self._set(pid, data["holder"], "")
                out[pid] = "standby"
            else:
                self._set(pid, None, "")
                out[pid] = "active"
        return out

    def _set(self, pid: str, holder: Optional[dict], why: str) -> None:
        was = pid in self.standby
        if holder:
            self.standby[pid] = holder
            if not was:
                who = holder.get("name") or holder.get("client") or "another workstation"
                self.log(f"{pid}: on STANDBY - {who} holds this project, so this coordinator "
                         "starts no sprints for it (Access -> Machines -> Make active moves it)")
                self._event(pid, "workstation_standby", {"holder": who})
        else:
            self.standby.pop(pid, None)
            if was:
                self.log(f"{pid}: this workstation runs the project again"
                         + (f" ({why})" if why else ""))
                self._event(pid, "workstation_active", {})

    def _event(self, pid: str, kind: str, detail: dict) -> None:
        try:
            self.c.store.event(pid, "", kind, detail={"workstation": self.name, **detail})
        except Exception:                                        # noqa: BLE001
            pass

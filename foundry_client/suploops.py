"""Extra upgrade-supervisor loops: find them and stop them.

2026-11-23: every "Restart client" left the old client's `supervisor ... --loop` running
(a restart keeps components so the new client adopts them, and nothing could find a
supervisor to adopt), so the new client started another: five loops for one coordinator
state folder. Since v0.181.255 the loop writes <state>/supervisor.loop.json and the client
adopts the loop it names; this module stops the others - loops that older clients left -
once the client's own supervisor is running.

A loop is a process whose command line runs the supervisor (`foundry_client supervisor`,
`foundry-client supervisor` or `foundry_coordinator.supervisor`) with `--loop` and the same
`--state`. A venv launcher and its python child carry the same command line and are one
loop. Never stopped: the client's own loop (launched or adopted) and a loop holding
<state>/supervisor.lock, which is in the middle of an upgrade. Needs psutil; without it
nothing is found and nothing is stopped."""

from __future__ import annotations

import os
from typing import Optional


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(p or ""))))


def is_loop(cmdline: list, state: str) -> bool:
    args = [str(a) for a in (cmdline or [])]
    low = [os.path.basename(a).lower() for a in args]
    runs = ("foundry_coordinator.supervisor" in args) or (
        "supervisor" in args and any(x.startswith(("foundry_client", "foundry-client"))
                                     for x in low))
    if not runs or "--loop" not in args or "--state" not in args:
        return False
    i = args.index("--state")
    return i + 1 < len(args) and _norm(args[i + 1]) == _norm(state)


def find(state: str, ps=None) -> list[dict]:
    """Every supervisor loop for `state`, one entry per loop: {pid (the top process), pids
    (it and its children), created, cmdline}."""
    if ps is None:
        try:
            import psutil as ps                                   # noqa: PLW0127
        except ImportError:
            return []
    procs: dict[int, dict] = {}
    for p in ps.process_iter(["pid", "ppid", "cmdline", "create_time"]):
        try:
            info = p.info
            if is_loop(info.get("cmdline") or [], state):
                procs[int(info["pid"])] = {"pid": int(info["pid"]), "ppid": int(info.get("ppid") or 0),
                                           "created": float(info.get("create_time") or 0),
                                           "cmdline": list(info.get("cmdline") or [])}
        except Exception:                                         # noqa: BLE001
            continue
    loops: dict[int, dict] = {}
    for pid, d in procs.items():
        top, seen = pid, set()
        while procs.get(top, {}).get("ppid") in procs and top not in seen:
            seen.add(top)
            top = procs[top]["ppid"]
        lp = loops.setdefault(top, {"pid": top, "pids": set(), "created": procs[top]["created"],
                                    "cmdline": procs[top]["cmdline"]})
        lp["pids"].add(pid)
    return sorted(loops.values(), key=lambda x: x["created"])


def extras(loops: list[dict], keep: set, lock_pid: Optional[int]) -> tuple[list, list]:
    """(to stop, busy): loops that are not the client's own; busy ones hold the upgrade lock."""
    stop, busy = [], []
    for lp in loops:
        if lp["pids"] & keep:
            continue
        (busy if lock_pid and lock_pid in lp["pids"] else stop).append(lp)
    return stop, busy


def sweep(state: str, keep: set, kill, ps=None) -> dict:
    """Stop every extra loop for `state`; `kill(pid)` stops a process and its children.
    {stopped: [pid], busy: [pid], kept: [pid]}."""
    from foundry_coordinator.supervisor import lock_pid
    loops = find(state, ps)
    stop, busy = extras(loops, keep, lock_pid(state))
    for lp in stop:
        kill(lp["pid"])
    return {"stopped": [lp["pid"] for lp in stop], "busy": [lp["pid"] for lp in busy],
            "kept": [lp["pid"] for lp in loops if lp["pids"] & keep]}

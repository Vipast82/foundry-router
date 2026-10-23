"""Requests the coordinator leaves for the Foundry Client (W2.4).

The coordinator never restarts Kilo or itself: the client owns the processes
and their safe-point restarts. When the coordinator needs one (a role-rules
deploy rewrote kilo.jsonc and Kilo must reload it; a deployed revision changed
a restart-scoped setting), it writes a request file:

    <state_dir>/client-requests/<kind>-<project>.json
    {"id", "kind", "project", "reason", "ts", "safe_point": true, "detail": {...}}

kinds:  reload_kilo          restart the Kilo server at a safe point
        restart_coordinator  restart the coordinator at a safe point
        start_service        start (or restart, when unhealthy) a managed service the
                             sprint needs; detail {"service", "component"}; one file per
                             service (services.py asks at most every 10 minutes)

One file per kind and project: a repeated request updates the file in place
(its `count` grows) instead of queueing duplicates. The client (st-client)
consumes a request by moving it to `client-requests/done/` (or deleting it)
once the restart happened; `pending()` lists what is still open. The
coordinator also records a `client_request` event, so the control center and
the chat engine can show it.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

DIR = "client-requests"
KINDS = ("reload_kilo", "restart_coordinator", "start_service")


def _dir(state_dir: str) -> str:
    return os.path.join(state_dir, DIR)


def _path(state_dir: str, kind: str, project: str, key: str = "") -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in project or "all")
    if key:
        safe += "-" + "".join(c if c.isalnum() or c in "._-" else "_" for c in key)
    return os.path.join(_dir(state_dir), f"{kind}-{safe}.json")


def request(state_dir: str, kind: str, project: str, reason: str, detail: Optional[dict] = None,
            store=None, clock=time.time, key: str = "") -> dict:
    """Write (or refresh) one request; returns the record written. `key` makes a second
    request of the same kind and project a file of its own (start_service: one per service)."""
    if kind not in KINDS:
        raise ValueError(f"unknown client request kind {kind!r} (known: {', '.join(KINDS)})")
    path = _path(state_dir, kind, project, key)
    prev = read(path)
    rec = {"id": (prev or {}).get("id") or uuid.uuid4().hex[:12], "kind": kind,
           "project": project, "reason": str(reason)[:500], "ts": clock(),
           "first_ts": (prev or {}).get("first_ts") or clock(),
           "count": int((prev or {}).get("count") or 0) + 1, "safe_point": True,
           "detail": dict(detail or {}), "path": path}
    os.makedirs(_dir(state_dir), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)
    os.replace(tmp, path)
    if store is not None:
        try:
            store.event(project, "", "client_request",
                        detail={k: rec[k] for k in ("id", "kind", "reason", "count", "path")})
        except Exception:                                        # noqa: BLE001
            pass
    return rec


def read(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("kind") in KINDS else None
    except (OSError, ValueError):
        return None


def pending(state_dir: str, kind: str = "") -> list[dict]:
    """Open requests, oldest first (optionally of one kind)."""
    d = _dir(state_dir)
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        rec = read(os.path.join(d, n))
        if rec and (not kind or rec["kind"] == kind):
            out.append(rec)
    return sorted(out, key=lambda r: r.get("first_ts") or 0)


def done(state_dir: str, rec_or_path, note: str = "", clock=time.time) -> str:
    """Mark a request consumed: moved to client-requests/done/ with the note.
    Returns the new path ("" when nothing was there)."""
    path = rec_or_path.get("path") if isinstance(rec_or_path, dict) else str(rec_or_path)
    if not path or not os.path.isfile(path):
        return ""
    rec = read(path) or {}
    rec.update(done_ts=clock(), done_note=str(note)[:300])
    dd = os.path.join(_dir(state_dir), "done")
    os.makedirs(dd, exist_ok=True)
    new = os.path.join(dd, f"{int(clock())}-{os.path.basename(path)}")
    with open(new, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)
    os.remove(path)
    return new

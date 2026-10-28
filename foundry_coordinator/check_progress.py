"""Which check of a background verify / closeout run is doing what (2026-10-27).

Victor: "how do I know from the control center what is happening right now? I see it's on
verifying, but I don't see any actions". Since v0.147.0 the project's checks run on worker
threads, outside any session, so the activity view showed "nothing running" while the Lune
suite ran for minutes. Each check records start, end and result here, per (project, task);
the task note and the coordinator heartbeat (the control center's activity graph) read it.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

_lock = threading.Lock()
_runs: dict[tuple, dict] = {}            # (project, task) -> {"kind", "head", "checks": {name: row}}

_ORDER = ("FAIL", "UNKNOWN", "PASS")


def reset(project: str, task: str, kind: str = "", head: str = "") -> None:
    with _lock:
        _runs[(project, task)] = {"kind": kind, "head": head, "start": time.time(),
                                   "checks": {}}


def start(project: str, task: str, name: str) -> None:
    with _lock:
        run = _runs.setdefault((project, task), {"kind": "", "head": "", "start": time.time(),
                                                 "checks": {}})
        run["checks"][name] = {"state": "running", "start": time.time(), "end": None}


def finish(project: str, task: str, name: str, result: str) -> None:
    with _lock:
        row = ((_runs.get((project, task)) or {}).get("checks") or {}).get(name)
        if row is not None:
            row.update(state=result or "done", end=time.time())


def counts(project: str, task: str, name: str, row: dict) -> None:
    """Live test counts of a running check ({done, total, pass, fail}, see
    live_tests.Counts.row), for the control center's progress bar."""
    with _lock:
        r = ((_runs.get((project, task)) or {}).get("checks") or {}).get(name)
        if r is not None:
            r["counts"] = {k: row.get(k) for k in ("done", "total", "pass", "fail", "estimated",
                                                    "cached")
                           if row.get(k) is not None}


def _row(n: str, r: dict, now: float) -> dict:
    out = {"name": n, "state": r["state"], "seconds": int((r["end"] or now) - r["start"]),
           "started_ts": r["start"]}
    out.update(r.get("counts") or {})
    return out


def _worst(rows) -> str:
    seen = {str(getattr(r, "result", "") or "") for r in rows or []
            if getattr(r, "required", True)}
    return next((s for s in _ORDER if s in seen), "done")


def track(project: str, task: str, name: str, fn: Callable[[], list]) -> list:
    """Run `fn` (a check returning VerificationResult rows) and record it."""
    start(project, task, name)
    try:
        rows = fn()
    except BaseException:
        finish(project, task, name, "error")
        raise
    finish(project, task, name, _worst(rows))
    return rows


def carried(project: str, task: str, name: str) -> None:
    """A check whose PASS was reused (same inputs): shown, never run."""
    start(project, task, name)
    finish(project, task, name, "PASS (reused)")


def snapshot(project: str, task: str) -> list[dict]:
    """[{name, state, seconds}] in start order."""
    now = time.time()
    with _lock:
        run = _runs.get((project, task)) or {}
        rows = sorted((run.get("checks") or {}).items(), key=lambda kv: kv[1]["start"])
        return [_row(n, r, now) for n, r in rows]


def _dur(s: int) -> str:
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s} s"


def _n(r: dict) -> str:
    if r.get("done") is None:
        return ""
    tot = f"/{'~' if r.get('estimated') else ''}{r['total']}" if r.get("total") else ""
    fail = f", {r['fail']} fail" if r.get("fail") else ""
    cached = f", {r['cached']} cached" if r.get("cached") else ""
    return f" {r['done']}{tot}{fail}{cached}"


def summary(project: str, task: str) -> str:
    """'done_check PASS 41 s · Lune suite running 812/~1609, 2 fail 5m02s · ...' or ''."""
    return " · ".join(f"{r['name']} {r['state']}{_n(r)} {_dur(r['seconds'])}"
                      for r in snapshot(project, task))


def running() -> list[dict]:
    """Every (project, task) with a check still running, for the heartbeat."""
    now = time.time()
    out = []
    with _lock:
        for (pid, tid), run in _runs.items():
            checks = run.get("checks") or {}
            if not any(r["state"] == "running" for r in checks.values()):
                continue
            out.append({"project": pid, "task": tid, "kind": run.get("kind") or "",
                        "head": (run.get("head") or "")[:12],
                        "seconds": int(now - run.get("start", now)),
                        "checks": [_row(n, r, now)
                                   for n, r in sorted(checks.items(),
                                                      key=lambda kv: kv[1]["start"])]})
    return out


def clear(project: str, task: str) -> Optional[dict]:
    with _lock:
        return _runs.pop((project, task), None)

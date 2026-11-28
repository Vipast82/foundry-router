"""Training-data hook at task closeout (profile `[training]`).

When a task closes (COMPLETE, closed awaiting approval, or FAILED), the
coordinator writes a capture manifest into the training spool. Since v0.153.0
it also writes one when an attempt stops BLOCKED (once the lead's recovery case,
if any, has ended) or the task is CANCELLED, so failed attempts reach the
pipeline as labelled negatives (`attempt_end`: state, blocker, run, attempt).
One manifest per task + run + state (`task.data.training_manifests`), so a task
bouncing BLOCKED -> READY -> BLOCKED in one run is not queued twice. The
manifest lists every session
it started for the task (implementation, repair, closeout, helpers), the
verified revision, verification set ids with their evidence pointers, review
ids and verdicts, approvals, and its own store path. `foundry_training
queue --spool DIR` ingests and curates from it asynchronously.

The manifest is a pointer list, not data: it holds no prompts or outputs.
Capture failure policy (`[training.capture] on_failure`):
  ignore  carry on silently
  warn    carry on, record an event (default)
  hold    keep the task in CLOSING and retry each tick (dependents wait)
"""

from __future__ import annotations
import foundry_platform as _plat

import json
import os
import time


def enabled(profile) -> bool:
    return bool((profile.raw.get("training") or {}).get("enabled"))


def policy(profile) -> str:
    return str(((profile.raw.get("training") or {}).get("capture") or {}).get("on_failure")
               or "warn")


def spool_dir(profile, state_dir: str) -> str:
    t = profile.raw.get("training") or {}
    return os.path.expanduser(t.get("spool") or os.path.join(state_dir, "training-spool"))


NEGATIVE = ("BLOCKED", "FAILED", "CANCELLED")


def manifest_key(t: dict, outcome: str) -> str:
    return f"{t.get('run_id') or ('attempt-' + str(t.get('attempt') or 0))}:{outcome}"


def finalize(store, P, t: dict, outcome: str, state_dir: str) -> dict:
    """Write the manifest (atomically). Raises on failure; returns it."""
    pid, tid = P.id, t["task_id"]
    sessions = store.sessions(pid, tid)
    ver = store.q("SELECT DISTINCT set_id, revision, phase FROM verifications WHERE project_id=? "
                  "AND task_id=?", (pid, tid))
    evidence = [r["evidence"] for r in store.q(
        "SELECT DISTINCT evidence FROM verifications WHERE project_id=? AND task_id=? AND "
        "evidence != ''", (pid, tid))]
    reviews = [{"review_id": r["review_id"], "reviewed_sha": r["reviewed_sha"],
                "verdict": r["verdict"], "valid": bool(r["valid"]), "persona": r["persona"]}
               for r in store.reviews(pid, tid)]
    approvals = [{"approval_id": a["approval_id"], "kind": a["kind"], "revision": a["revision"],
                  "artifacts": a["artifacts"]} for a in store.approvals(pid, tid)]
    gaps = []
    if not sessions:
        gaps.append("no coding sessions recorded")
    if outcome == "COMPLETE" and not reviews and P.profile.review.get("required", True):
        gaps.append("no review recorded")
    m = {"schema": 1, "project_id": pid, "task_id": tid, "outcome": outcome,
         "created": time.time(), "head_sha": t.get("head_sha"),
         "reviewed_sha": t.get("reviewed_sha"), "base_sha": (t.get("data") or {}).get("task_base"),
         "sessions": [s["session_id"] for s in sessions],
         "session_roles": {s["session_id"]: {"role": s["role"], "phase": s["phase"],
                                             "run_id": s["run_id"], "attempt": s["attempt"],
                                             "client": s["client"]}
                           for s in sessions},
         "verification_sets": ver, "evidence": evidence, "reviews": reviews,
         "approvals": approvals, "coordinator_db": store.path,
         "run_id": t.get("run_id"), "attempt": t.get("attempt"),
         "attempt_end": ({"state": outcome, "blocker": str(t.get("blocker") or "")[:500],
                          "class": str((t.get("data") or {}).get("blocker_class")
                                       or (t.get("data") or {}).get("stop_class") or ""),
                          "run_id": t.get("run_id"), "attempt": t.get("attempt")}
                         if outcome in NEGATIVE else None),
         "completeness": {"sessions": len(sessions), "verification_sets": len(ver),
                          "reviews": len(reviews), "gaps": gaps}}
    d = spool_dir(P.profile, state_dir)
    os.makedirs(d, exist_ok=True)
    name = f"{pid}__{tid}__{int(time.time() * 1000)}.json"
    # Distributed Foundry phase b (B7): a lane on a slot keeps its sessions on the slot's own
    # Kilo / OpenCode; they are read now through the lane tunnel into files beside the
    # manifest, which the pipeline ingests like `kilo export` files (nothing is dropped)
    exports, missing = export_remote_sessions(P, sessions, os.path.join(d, "remote",
                                                                         name[:-len(".json")]))
    if exports:
        m["session_exports"] = exports
    if missing:
        m["completeness"]["gaps"] = gaps = gaps + missing
    tmp = os.path.join(d, "." + name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(m, f, indent=1, default=str)
    os.replace(tmp, os.path.join(d, name))
    store.event(pid, tid, "training_queued", detail={"manifest": name, "outcome": outcome,
                                                      "gaps": gaps})
    return m


def export_remote_sessions(P, sessions: list, dest: str, endpoint=None) -> tuple:
    """({session id: export file}, [gap texts]) for the sessions of a lane whose worktree is
    on a slot (P.profile.lane_host); ({}, []) for a local one. Each session and its children
    (helpers) are read from the slot's server through the lane tunnel, in the shape
    `kilo export` writes; a session that cannot be read is a named gap, never dropped silently."""
    host = str(getattr(P.profile, "__dict__", {}).get("lane_host") or "")
    if not host or not sessions:
        return {}, []
    if endpoint is None:
        from . import lane_tunnel
        endpoint = lane_tunnel.MANAGER.endpoint
    from foundry_training.adapters import kilo as K
    from foundry_training.adapters import opencode as O
    out, gaps = {}, []
    todo = [(s["session_id"], str(s.get("client") or "kilo")) for s in sessions]
    seen = set()
    while todo:
        sid, kind = todo.pop()
        if sid in seen:
            continue
        seen.add(sid)
        kind = "opencode" if kind.startswith("opencode") else "kilo"
        ep = endpoint(host, kind)
        if not ep:
            gaps.append(f"remote session {sid} on {host}: not exported (the lane tunnel is down)")
            continue
        try:
            if kind == "opencode":
                data = O.from_api(ep["url"], sid, ep.get("password", ""), P.profile.workspace)
            else:
                data = K.from_api(ep["url"], sid, ep.get("password", ""), P.profile.workspace)
        except Exception as e:                                    # noqa: BLE001
            gaps.append(f"remote session {sid} on {host}: not exported ({type(e).__name__}: "
                        f"{e})"[:300])
            continue
        os.makedirs(dest, exist_ok=True)
        path = os.path.join(dest, f"{kind}__{sid}.json")
        data = {**data, "_lane_host": host, "_client": kind}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
        os.replace(tmp, path)
        out[sid] = path
        todo.extend((k, kind) for k in data.get("children") or [] if isinstance(k, str))
    return out, gaps


# ---------------------------------------------------------------------------- automatic run

def auto_enabled(profile) -> bool:
    # FOUNDRY_TRAINING_AUTO=0 turns automatic runs off for this process (tests,
    # or an operator who wants to run the pipeline by hand)
    if os.environ.get("FOUNDRY_TRAINING_AUTO", "1").strip().lower() in ("0", "false", "off", "no"):
        return False
    t = profile.raw.get("training") or {}
    return bool(t.get("enabled")) and bool((t.get("auto") or {}).get("enabled", True))


def _spawn(cmd: list, log_path: str, env: dict):
    """Start the pipeline detached from the engine (tests replace this)."""
    import subprocess
    import sys
    kw = {}
    if _plat.is_windows():
        kw["creationflags"] = _plat.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    with open(log_path, "a", encoding="utf-8") as log:   # the child keeps its own handle
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
        log.flush()
        return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, **kw)


def launch_auto(store, P, t: dict, state_dir: str) -> bool:
    """After a closeout manifest: parse, export, store and upload in the
    background (foundry_training auto). Never blocks or fails the task; the
    run's output goes to <spool>/auto.log and its datasets to the library."""
    import sys
    if not auto_enabled(P.profile):
        return False
    a = (P.profile.raw.get("training") or {}).get("auto") or {}
    spool = spool_dir(P.profile, state_dir)
    cmd = [sys.executable, "-m", "foundry_training", "--profiles", P.profile.path]
    if a.get("store"):
        cmd += ["--store", os.path.expanduser(str(a["store"]))]
    cmd += ["auto", "--spool", spool]
    env = dict(os.environ)
    try:
        _spawn(cmd, os.path.join(spool, "auto.log"), env)
    except Exception as e:                                        # noqa: BLE001
        store.event(P.id, t["task_id"], "training_auto_failed",
                    detail={"error": f"{type(e).__name__}: {e}"[:300]})
        return False
    store.event(P.id, t["task_id"], "training_auto_started",
                detail={"spool": spool, "log": os.path.join(spool, "auto.log")})
    return True

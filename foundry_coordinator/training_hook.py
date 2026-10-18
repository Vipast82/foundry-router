"""Training-data hook at task closeout (profile `[training]`).

When a task closes (COMPLETE, closed awaiting approval, or FAILED), the
coordinator writes a capture manifest into the training spool: every session
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
         "completeness": {"sessions": len(sessions), "verification_sets": len(ver),
                          "reviews": len(reviews), "gaps": gaps}}
    d = spool_dir(P.profile, state_dir)
    os.makedirs(d, exist_ok=True)
    name = f"{pid}__{tid}__{int(time.time() * 1000)}.json"
    tmp = os.path.join(d, "." + name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(m, f, indent=1, default=str)
    os.replace(tmp, os.path.join(d, name))
    store.event(pid, tid, "training_queued", detail={"manifest": name, "outcome": outcome,
                                                      "gaps": gaps})
    return m


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
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
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

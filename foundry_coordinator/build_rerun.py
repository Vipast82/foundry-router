"""Re-run a build-journal entry on a slot (docs/plans/BUILD_RERUN_PLAN.md, build replay R4).

"Re-run" on a Build replay row runs the same commit, the same builder script (the journal's
copy, so an uncommitted script is exact too) and the same arguments on a Blender slot. The
outputs come back into the new entry's own folder under the journal, never into a sprint's
worktree, and the result is a new journal entry (source `replay`, `replay_of` = the run).
Other files that were uncommitted at the original run are not included; the entry says so.

Off by default (`blender.journal_rerun`). One re-run at a time per coordinator, in a
background thread: the control action is answered at once.
"""

from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import threading
import time
import uuid
from typing import Callable, Optional

from . import build_journal as BJ

_LOCK = threading.Lock()
_RUNNING: dict = {}
BRANCH = "blender/replay"           # one branch on the slot mirror, overwritten per re-run
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


class RerunError(Exception):
    pass


def enabled(profile) -> bool:
    return bool(((getattr(profile, "raw", None) or {}).get("blender") or {}).get("journal_rerun"))


def entry(state_dir: str, pid: str, tid: str, run_id: str) -> dict:
    d = BJ.run_dir(state_dir, pid, tid, run_id)
    try:
        with open(os.path.join(d, "journal.json"), encoding="utf-8") as f:
            e = json.load(f)
    except (OSError, ValueError):
        raise RerunError(f"no journal entry {run_id} for {pid}/{tid}") from None
    return e if isinstance(e, dict) else {}


def pick_slot(profile, want: str = "") -> dict:
    """The slot to re-run on: `want`, else the pool's first slot, else blender.slot. Its
    settings plus `id` and `jobs`."""
    from . import slot_blender as SB, slots
    conf = slots.configured(profile)
    order = [w for w in SB.pool(profile) if w["id"] != SB.LOCAL]
    if SB.blender_slot(profile):
        order.append({"id": SB.blender_slot(profile), "jobs": 1})
    if want:
        order = [w for w in order if w["id"] == want] or [{"id": want, "jobs": 1}]
    for w in order:
        s = conf.get(w["id"])
        if s and str(s.get("host") or "").strip() and s.get("container"):
            return {**s, "id": w["id"], "jobs": int(w.get("jobs") or 1)}
    raise RerunError("no Blender slot to re-run on (blender.pool / blender.slot name no "
                     "enabled mini-PC slot)" + (f": {want}" if want else ""))


def plan(profile, state_dir: str, pid: str, tid: str, run_id: str, slot: str = "") -> dict:
    """Check that the run can be re-run; what would run (no side effects)."""
    if not enabled(profile):
        raise RerunError("re-running journal entries is off (blender.journal_rerun)")
    e = entry(state_dir, pid, tid, run_id)
    where = e.get("where") or {}
    commit = str(where.get("commit") or "")
    if not _COMMIT.match(commit):
        raise RerunError(f"{run_id} recorded no commit, so it cannot be re-run exactly")
    args = [str(a) for a in e.get("args") or []]
    if not args:
        raise RerunError(f"{run_id} recorded no Blender arguments")
    script = (e.get("script") or {})
    copy = os.path.join(BJ.run_dir(state_dir, pid, tid, run_id), "script.txt")
    return {"run": e, "commit": commit, "args": args, "slot": pick_slot(profile, slot),
            "script_path": str(script.get("path") or ""),
            "script_copy": copy if script.get("copy") and os.path.isfile(copy) else ""}


def start(profile, state_dir: str, pid: str, tid: str, run_id: str, *, slot: str = "",
          actor: str = "", store=None, runner: Optional[Callable] = None) -> dict:
    """Check, then run in a background thread. Returns {"replay_run_id", "slot"}; raises
    RerunError when it can't start (another re-run running, off, no commit, no slot)."""
    p = plan(profile, state_dir, pid, tid, run_id, slot)
    with _LOCK:
        if _RUNNING:
            cur = next(iter(_RUNNING.values()))
            raise RerunError(f"another re-run is running ({cur['replay_of']} on {cur['slot']}); "
                             "one at a time")
        new_id = f"replay-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        _RUNNING[new_id] = {"replay_of": run_id, "slot": p["slot"]["id"], "since": time.time()}
    if store is not None:
        store.event(pid, tid, "build_rerun_started", detail={
            "run": run_id, "replay_run_id": new_id, "slot": p["slot"]["id"],
            "commit": p["commit"], "by": actor})
    work = runner or run

    def go():
        try:
            out = work(profile, state_dir, pid, tid, run_id, new_id, p)
            if store is not None:
                store.event(pid, tid, "build_rerun_done", detail={
                    "run": run_id, "replay_run_id": new_id, **(out or {})})
        except Exception as e:                                    # noqa: BLE001
            if store is not None:
                store.event(pid, tid, "build_rerun_failed", detail={
                    "run": run_id, "replay_run_id": new_id,
                    "error": f"{type(e).__name__}: {e}"[:500]})
        finally:
            with _LOCK:
                _RUNNING.pop(new_id, None)

    threading.Thread(target=go, name=f"build-rerun-{new_id}", daemon=True).start()
    return {"replay_run_id": new_id, "slot": p["slot"]["id"], "commit": p["commit"]}


def run(profile, state_dir: str, pid: str, tid: str, run_id: str, new_id: str, p: dict) -> dict:
    """The re-run itself (blocking). The scratch folder is the new entry's `out/`."""
    from . import slot_blender as SB, slots
    slot, commit, args = p["slot"], p["commit"], p["args"]
    out_dir = os.path.join(BJ.run_dir(state_dir, pid, tid, new_id), "out")
    os.makedirs(out_dir, exist_ok=True)
    files = []
    if p["script_copy"] and p["script_path"] and not os.path.isabs(p["script_path"]):
        dst = os.path.join(out_dir, *p["script_path"].split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(p["script_copy"], encoding="utf-8") as f, \
                open(dst, "w", encoding="utf-8", newline="") as g:
            g.write(f.read())
        files.append(p["script_path"])
    ws = str(getattr(profile, "workspace", "") or "")
    t0 = time.time()
    marks: dict = {}
    url = slot.get("au_push_url") or f"ssh://{slot['host']}{slots.HOST_ROOT}/AnimalUprising.git"
    r = subprocess.run(["git", "-C", ws, "push", "-q", "--force", url,
                        f"{commit}:refs/heads/{BRANCH}"], capture_output=True, timeout=600,
                       env=slots.git_ssh_env(slot))
    if r.returncode != 0:
        raise RerunError("cannot push the commit to the slot's mirror: "
                         + (r.stderr or r.stdout).decode("utf-8", "replace").strip()[-300:])
    marks["pushed"] = time.time()
    bl = (getattr(profile, "raw", None) or {}).get("blender") or {}
    library = str(bl.get("library") or "")
    wait = int(bl.get("pool_wait_seconds") or 600)
    if library and os.path.isdir(library):
        ok, msg = SB.lib_sync(slot, library, wait)
        if not ok:
            raise RerunError(msg)
    else:
        library = ""
    marks["library"] = time.time()
    script = SB.remote_script(BRANCH.split("/", 1)[1], wait, 0, args, library=bool(library),
                              job=1, jobs=int(slot.get("jobs") or 1))
    proc = subprocess.Popen(slots.exec_argv(slot, ["bash", "-c", script], interactive=True),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=SB._NO_WINDOW)
    tail, keys = collections.deque(maxlen=400), []

    def read_err():
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            tail.append(line)
            if BJ._KEY.search(line) and len(keys) < 500:
                keys.append(line[:300])
            if "foundry-blender: running on" in line:
                marks.setdefault("blender_start", time.time())
            elif "foundry-blender: blender exit" in line:
                marks["blender_end"] = time.time()
    reader = threading.Thread(target=read_err, daemon=True)
    reader.start()
    sender = threading.Thread(target=SB._send_tar, args=(proc.stdin, out_dir, files, []),
                              daemon=True)
    sender.start()
    back = SB.receive(proc.stdout, out_dir)
    try:
        while proc.stdout.read(1 << 16):
            pass
    except (OSError, ValueError):
        pass
    try:
        rc = proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait()
    sender.join(timeout=30)
    reader.join(timeout=10)
    end = time.time()
    m = {"start": t0, **marks, "end": end}

    def span(a, b):
        return round(m[b] - m[a], 1) if a in m and b in m else None
    timings = {k: v for k, v in {"push": span("start", "pushed"),
                                 "library": span("pushed", "library"),
                                 "blender": span("blender_start", "blender_end"),
                                 "copy_back": span("blender_end", "end")}.items()
               if v is not None}
    notes = [f"re-run of {run_id}: commit {commit[:12]}"
             + (", the builder script from the journal's copy" if files else
                ", the builder script as committed")
             + "; other files that were uncommitted at the original run are not included"]
    if rc == SB.BUSY_EXIT:
        notes.append("the slot stayed busy: Blender did not run")
    e = BJ.record(state_dir, project_id=pid, task_id=tid, source="replay", repo=out_dir,
                  args=args, exit_code=rc, started_ts=t0, ended_ts=end, log_lines=list(tail),
                  keys=keys, files_back=[] if rc == SB.BUSY_EXIT else back, timings=timings,
                  where={"slot": slot["id"], "host": slot.get("host"), "commit": commit,
                         "replay_of": run_id, "outputs": out_dir},
                  run_id=new_id, opts={k: bl[k] for k in BJ.DEFAULTS if k in bl}, notes=notes)
    return {"exit": rc, "files_back": len(back), "seconds": round(end - t0, 1),
            "recorded": bool(e)}


def running() -> list:
    with _LOCK:
        return [{"replay_run_id": k, **v} for k, v in _RUNNING.items()]

"""Lanes whose worktree lives on a slot: create, fetch back, stop (Distributed Foundry b-3, B6).

docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md. A lane on a slot keeps its worktree in the slot's
lane store (`<lanes_root>/<project>.git`, bare, `--shared` with the slot's game copy) at
`<lanes_root>/<project>/<lane>`. This module is the junction's side of it:

  push_base   the lane's base commit -> the store as refs/heads/slot/_base/<lane> (a private
              ref; a new lane name only, never forced)
  create      push_base, then the lane agent's `worktree-add` job, waited for
  fetch       the store's track/<lane> -> refs/lanes/<slot>/<lane> here, then this repo's
              track/<lane> moved FAST-FORWARD ONLY. A tip that is not a fast-forward is
              reported as diverged; both tips are kept, nothing is overwritten
  save_dirty  the lane's modified and untracked files copied here before a stop

The junction pulls; a slot never pushes and never needs a key to this machine or GitHub.
Every call returns a dict with "ok" and words for an event; none raises.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Callable, Optional

import foundry_platform as _plat

from . import slots as SL

JOB_WAIT_S = 600
POLL_S = 1.0
PUSH_TIMEOUT_S = SL.SLOT_PUSH_TIMEOUT_S
FETCH_TIMEOUT_S = 1800
_LANE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


def store_path(slot: dict, project: str) -> str:
    from .adapters.laneclient import lane_root
    return f"{lane_root(slot)}/{project}.git"


def store_url(slot: dict, project: str) -> str:
    """The lane store as git sees it from here: ssh://<host><path>, or the path itself for a
    slot whose docker runs on this machine (and in tests)."""
    host = str(slot.get("host") or "").strip()
    path = store_path(slot, project)
    return f"ssh://{host}{path}" if host else path


def worktree_path(slot: dict, project: str, lane: str) -> str:
    from .adapters.laneclient import lane_root
    return f"{lane_root(slot)}/{project}/{lane}"


def _git(cwd: str, args: list, slot: dict, timeout: float) -> tuple:
    env = SL.git_ssh_env(slot) if str(slot.get("host") or "").strip() else None
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL, **_plat.no_window_kw())
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {int(timeout)} s"
    except OSError as e:
        return -1, "", f"{type(e).__name__}: {e}"
    return r.returncode, r.stdout or "", r.stderr or ""


def push_base(workspace: str, slot: dict, project: str, lane: str, base: str) -> dict:
    if not _LANE_RE.match(lane or "") or not re.fullmatch(r"[0-9a-f]{40}", base or ""):
        return {"ok": False, "detail": f"bad lane {lane!r} or base {base!r}"}
    ref = f"refs/heads/slot/_base/{lane}"
    t0 = time.time()
    rc, out, err = _git(workspace, ["push", "-q", store_url(slot, project), f"{base}:{ref}"],
                        slot, PUSH_TIMEOUT_S)
    if rc != 0:
        return {"ok": False, "detail": f"base push to the lane store: {(err or out).strip()[-300:]}"}
    return {"ok": True, "seconds": int(time.time() - t0), "detail": f"{base[:12]} -> {ref}"}


def wait_job(agent, project: str, lane: str, job: str, args: dict,
             wait_s: float = JOB_WAIT_S, sleep: Callable = time.sleep) -> dict:
    """Start a lane agent job and wait for it: {"ok", "state", "rc", "output"}."""
    try:
        rid = agent.call("run", {"project": project, "lane": lane, "job": job, "args": args})["run_id"]
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "state": "unreachable", "rc": None, "output": str(e)[:500]}
    out, offset, t0 = [], 0, time.time()
    while True:
        try:
            st = agent.call("run_status", {"id": rid, "offset": offset})
        except Exception as e:                                   # noqa: BLE001
            # a network blip: the job keeps running on the slot; re-attach from the offset
            if time.time() - t0 > wait_s:
                return {"ok": False, "state": "unreachable", "rc": None,
                        "output": "".join(out)[-4000:] + f"\n{e}"[:300], "run_id": rid}
            sleep(POLL_S)
            continue
        out.append(st.get("output") or "")
        offset = int(st.get("offset") or offset)
        if st.get("state") != "running":
            return {"ok": st.get("state") == "done", "state": st.get("state"), "rc": st.get("rc"),
                    "output": "".join(out)[-20000:], "run_id": rid}
        if time.time() - t0 > wait_s:
            return {"ok": False, "state": "waiting", "rc": None,
                    "output": "".join(out)[-4000:], "run_id": rid}
        sleep(POLL_S)


def create(workspace: str, slot: dict, agent, project: str, lane: str, base: str,
           branch: str, sleep: Callable = time.sleep) -> dict:
    """The lane's worktree on the slot. {"ok", "worktree", "detail"}."""
    # the project's lane store first: setup-host.sh makes only the slot's own game's
    store = wait_job(agent, project, lane, "store-init", {}, sleep=sleep)
    if not store["ok"]:
        return {"ok": False, "worktree": "",
                "detail": f"lane store on the slot: {store['state']}: {store['output'][-300:]}"}
    pushed = push_base(workspace, slot, project, lane, base)
    if not pushed["ok"]:
        return {**pushed, "worktree": ""}
    job = wait_job(agent, project, lane, "worktree-add", {"base": base, "branch": branch},
                   sleep=sleep)
    if not job["ok"]:
        return {"ok": False, "worktree": "",
                "detail": f"worktree-add on the slot: {job['state']}: {job['output'][-400:]}"}
    return {"ok": True, "worktree": worktree_path(slot, project, lane),
            "detail": f"{pushed['detail']}; worktree on the slot"}


def fetch(workspace: str, slot: dict, sid: str, project: str, lane: str,
          branch: str = "") -> dict:
    """Bring the lane's commits here. {"ok", "tip", "moved", "diverged", "detail"}."""
    branch = branch or f"track/{lane}"
    if not _LANE_RE.match(lane or "") or not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch):
        return {"ok": False, "detail": f"bad lane {lane!r} or branch {branch!r}"}
    lref = f"refs/lanes/{sid}/{lane}"
    rc, out, err = _git(workspace, ["fetch", "-q", "--no-tags", store_url(slot, project),
                                    f"refs/heads/{branch}:{lref}"], slot, FETCH_TIMEOUT_S)
    if rc != 0:
        text = (err or out).strip()
        if "non-fast-forward" in text or "rejected" in text:
            return {"ok": False, "diverged": True, "detail":
                    f"the slot's {branch} is not a fast-forward of {lref}: kept both"}
        return {"ok": False, "detail": f"fetch from the lane store: {text[-300:]}"}
    rc, tip, _ = _git(workspace, ["rev-parse", "--verify", "-q", lref], slot, 60)
    tip = tip.strip()
    if rc != 0 or not tip:
        return {"ok": False, "detail": f"{lref} missing after the fetch"}
    href = f"refs/heads/{branch}"
    rc, cur, _ = _git(workspace, ["rev-parse", "--verify", "-q", href], slot, 60)
    cur = cur.strip() if rc == 0 else ""
    if cur == tip:
        return {"ok": True, "tip": tip, "moved": False, "detail": f"{branch} already at {tip[:12]}"}
    if cur:
        rc, _, _ = _git(workspace, ["merge-base", "--is-ancestor", cur, tip], slot, 60)
        if rc != 0:
            return {"ok": False, "diverged": True, "tip": tip, "detail":
                    f"{branch} here ({cur[:12]}) is not behind the slot's ({tip[:12]}): kept both "
                    f"({lref} holds the slot's)"}
    args = ["update-ref", "-m", f"lane fetch from {sid}", href, tip] + ([cur] if cur else [""])
    rc, out, err = _git(workspace, args, slot, 60)
    if rc != 0:
        return {"ok": False, "tip": tip, "detail": f"update-ref: {(err or out).strip()[-200:]}"}
    return {"ok": True, "tip": tip, "moved": True,
            "detail": f"{branch} {cur[:12] or '(new)'} -> {tip[:12]} from {sid}"}


def save_dirty(ws, dest_dir: str) -> dict:
    """Copy the lane's modified and untracked files here (through its RemoteWorkspace) before a
    stop: <dest_dir>/<path> plus a list. Nothing changes on the slot."""
    try:
        rows = ws.status()
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "files": 0, "detail": f"status: {e}"[:300]}
    saved = []
    for xy, rel in rows:
        if xy.strip() == "D":
            continue
        try:
            data = ws.read(rel)
        except Exception:                                        # noqa: BLE001
            data = None
        if data is None:
            continue
        dst = os.path.join(dest_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(data)
        saved.append(rel)
    if rows:
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "_list.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(f"{xy} {p}\n" for xy, p in rows))
    return {"ok": True, "files": len(saved), "dir": dest_dir if rows else "",
            "detail": f"{len(saved)} of {len(rows)} changed file(s) saved"}


__all__ = ["store_url", "store_path", "worktree_path", "push_base", "wait_job", "create",
           "fetch", "save_dirty"]

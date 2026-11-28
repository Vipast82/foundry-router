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

import json
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
# a lane fetch runs on the tick thread: a lane's commits are small and come every few
# minutes, so a fetch that takes longer than this is a stalled link (2026-11-27 review: it
# was 1800 s, and with no ServerAlive a dead link held the tick for that long)
FETCH_TIMEOUT_S = 180
SSH_KEEPALIVE = ("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4")
_LANE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


def store_path(slot: dict, project: str) -> str:
    from .adapters.laneclient import lane_root
    return f"{lane_root(slot)}/{project}.git"


def store_url(slot: dict, project: str) -> str:
    """The lane store as git sees it from here: ssh://<host><path>, or the path itself for a
    slot whose docker runs on this machine (and in tests)."""
    host = str(slot.get("host") or "").strip()
    path = store_path(slot, project)
    if host and slot.get("os") == "windows":
        return f"{host}:{path}"      # scp-style: C:/ paths reach the slot's bash as they are
    return f"ssh://{host}{path}" if host else path


def worktree_path(slot: dict, project: str, lane: str) -> str:
    from .adapters.laneclient import lane_root
    return f"{lane_root(slot)}/{project}/{lane}"


def git_env(slot: dict) -> Optional[dict]:
    """git's environment for the lane store on `slot`: slots.git_ssh_env (Foundry's key and
    known_hosts) plus ssh keepalives, so a link that dies mid-fetch ends in about a minute
    instead of at the timeout. None for a slot without a host (its store is a local path)."""
    if not str(slot.get("host") or "").strip():
        return None
    env = dict(SL.git_ssh_env(slot))
    cmd = env.get("GIT_SSH_COMMAND") or ""
    if cmd and "ServerAliveInterval" not in cmd:
        env["GIT_SSH_COMMAND"] = cmd + " " + " ".join(SSH_KEEPALIVE)
    return env


def _git(cwd: str, args: list, slot: dict, timeout: float) -> tuple:
    env = git_env(slot)
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
    """Start a lane agent job and wait for it: {"ok", "state", "rc", "output", "run_id"}.

    The whole output comes back (a check is judged on all of it). States besides the job's own
    (done, failed, cancelled): "refused" (the agent said no to the job: HTTP 4xx), "unreachable"
    (no answer for the whole wait), "lost" (the agent no longer knows the run: it restarted, and
    its runs are in memory only; reported at once, not after the wait), "waiting" (still
    running after `wait_s`: it is cancelled on the slot, so a timed-out job never keeps
    running there)."""
    from .adapters.remote_git import http_status
    try:
        rid = agent.call("run", {"project": project, "lane": lane, "job": job, "args": args})["run_id"]
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "state": "refused" if http_status(e) else "unreachable",
                "rc": None, "output": str(e)[:500]}
    out, offset, t0 = [], 0, time.time()
    while True:
        try:
            st = agent.call("run_status", {"id": rid, "offset": offset})
        except Exception as e:                                   # noqa: BLE001
            if http_status(e) == 404:
                return {"ok": False, "state": "lost", "rc": None, "run_id": rid,
                        "output": "".join(out) + f"\nthe slot's lane agent no longer knows run "
                                                 f"{rid} (it restarted?): {e}"[:300]}
            # a network blip: the job keeps running on the slot; re-attach from the offset
            if time.time() - t0 > wait_s:
                _cancel(agent, rid)
                return {"ok": False, "state": "unreachable", "rc": None,
                        "output": "".join(out) + f"\n{e}"[:300], "run_id": rid}
            sleep(POLL_S)
            continue
        out.append(st.get("output") or "")
        offset = int(st.get("offset") or offset)
        if st.get("state") != "running":
            return {"ok": st.get("state") == "done", "state": st.get("state"), "rc": st.get("rc"),
                    "output": "".join(out), "run_id": rid}
        if time.time() - t0 > wait_s:
            cancelled = _cancel(agent, rid)
            return {"ok": False, "state": "waiting", "rc": None, "cancelled": cancelled,
                    "output": "".join(out) + (f"\n(cancelled on the slot after {int(wait_s)} s)"
                                              if cancelled else ""), "run_id": rid}
        sleep(POLL_S)


def _cancel(agent, rid: str) -> bool:
    try:
        agent.call(f"run/{rid}/cancel", {}, timeout=30)
        return True
    except Exception:                                            # noqa: BLE001
        return False


def checks_of(profile) -> dict:
    """{name: argv template} of a profile's [[verify.checks]], as a lane registers them at
    worktree-add (the lane agent runs only these: foundry_client/lane.py)."""
    out = {}
    for c in getattr(profile, "checks", None) or []:
        cmd = getattr(c, "command", None)
        if isinstance(cmd, (list, tuple)) and cmd:
            out[str(c.name)] = [str(x) for x in cmd]
    return out


def create(workspace: str, slot: dict, agent, project: str, lane: str, base: str,
           branch: str, sleep: Callable = time.sleep, checks: Optional[dict] = None) -> dict:
    """The lane's worktree on the slot. {"ok", "worktree", "detail"}. `checks`: the project's
    [[verify.checks]] ({name: argv template}), the only commands the lane's verify job runs.
    The toolchain install (rokit, wally) is the agent's own job after this; it is not waited
    for. A worktree-add that fails or times out is undone on the slot, so a retry works."""
    # the project's lane store first: setup-host.sh makes only the slot's own game's
    store = wait_job(agent, project, lane, "store-init", {}, sleep=sleep)
    if not store["ok"]:
        return {"ok": False, "worktree": "",
                "detail": f"lane store on the slot: {store['state']}: {store['output'][-300:]}"}
    pushed = push_base(workspace, slot, project, lane, base)
    if not pushed["ok"]:
        return {**pushed, "worktree": ""}
    job = wait_job(agent, project, lane, "worktree-add",
                   {"base": base, "branch": branch, "checks": dict(checks or {})}, sleep=sleep)
    if not job["ok"]:
        why = {"waiting": "did not finish in time and was cancelled there (try again)",
               "unreachable": "could not be followed (the slot stopped answering; when it is "
                              "back, a retry undoes an add that never finished, or use "
                              "another track name)",
               "lost": "was lost (the slot's lane agent restarted; try again)"}.get(job["state"],
                                                                                    job["state"])
        return {"ok": False, "worktree": "",
                "detail": f"worktree-add on the slot {why}: {job['output'][-400:]}"}
    return {"ok": True, "worktree": worktree_path(slot, project, lane),
            "detail": f"{pushed['detail']}; worktree on the slot"}


def discard(agent, project: str, lane: str, base: str, sleep: Callable = time.sleep) -> dict:
    """A lane worktree made by `create` that the coordinator could not take up (its read mirror
    failed): removed on the slot again, only while it is clean and still at `base` (the agent
    checks both), so a new `tracks create` can make it again."""
    return wait_job(agent, project, lane, "worktree-remove",
                    {"tip": base, "saved": [], "drop_branch_at": base}, sleep=sleep)


# ---------------------------------------------------------------------------- the read mirror
#
# Phase c (docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md, "phase c" notes): the junction keeps a
# local worktree of each remote lane, <workspace>/.lanes/<slot>/<lane>, on branch track/<lane>,
# moved fast-forward to the slot's tip at every fetch. It is what the lane's profile.workspace
# points at, so every coordinator read of the worktree (plans, evidence, globs, images) works as
# for a local track; writes and git go to the slot (RemoteWorkspace). Sparse: no docs/evidence/
# except the lane's own sprints' folders (AU's docs/evidence is 14 GB). The sparse checkout is
# per worktree (git sets extensions.worktreeConfig in the repo config the first time; the main
# checkout is not sparse and does not change).

MIRROR_DIR = ".lanes"


def mirror_path(main_ws: str, sid: str, lane: str) -> str:
    return os.path.join(os.path.realpath(main_ws), MIRROR_DIR, sid, lane)


def _exclude_mirrors(main_ws: str) -> None:
    """`.lanes/` in the repository's info/exclude (as `.tracks/` is)."""
    rc, gd, _ = _git(main_ws, ["rev-parse", "--git-common-dir"], {}, 60)
    if rc != 0:
        return
    gd = gd.strip()
    path = os.path.join(gd if os.path.isabs(gd) else os.path.join(main_ws, gd), "info", "exclude")
    try:
        have = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if f"/{MIRROR_DIR}/" not in have.split("\n"):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(("" if have.endswith("\n") or not have else "\n") + f"/{MIRROR_DIR}/\n")
    except OSError:
        pass


def mirror_create(main_ws: str, sid: str, lane: str, branch: str, base: str,
                  sprints: list) -> dict:
    """The lane's read mirror at its base: a sparse worktree on `branch` (made at `base`)."""
    path = mirror_path(main_ws, sid, lane)
    if os.path.isdir(path):
        return {"ok": True, "path": path, "detail": "already there"}
    _exclude_mirrors(main_ws)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rc, _, err = _git(main_ws, ["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], {}, 60)
    args = (["worktree", "add", "-q", "--no-checkout", path, branch] if rc == 0 else
            ["worktree", "add", "-q", "--no-checkout", "-b", branch, path, base])
    rc, out, err = _git(main_ws, args, {}, 300)
    if rc != 0:
        return {"ok": False, "path": "", "detail": f"mirror worktree: {(err or out).strip()[-300:]}"}
    pats = ["/*", "!/docs/evidence/"] + [f"/docs/evidence/{x}/" for x in sprints
                                         if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(x))]
    rc, out, err = _git(path, ["sparse-checkout", "set", "--no-cone", *pats], {}, 300)
    if rc == 0:
        rc, out, err = _git(path, ["checkout", "-q"], {}, 1800)
    if rc != 0:
        return {"ok": False, "path": path, "detail": f"mirror checkout: {(err or out).strip()[-300:]}"}
    return {"ok": True, "path": path, "detail": f"read mirror {path} (sparse, no docs/evidence)"}


def mirror_full(path: str) -> dict:
    """Make a mirror a full checkout (bring home: it becomes the track's own worktree)."""
    rc, out, err = _git(path, ["sparse-checkout", "disable"], {}, 3600)
    return {"ok": rc == 0, "detail": (err or out).strip()[-300:] if rc else "full checkout"}


def fetch(workspace: str, slot: dict, sid: str, project: str, lane: str,
          branch: str = "", mirror: str = "") -> dict:
    """Bring the lane's commits here. {"ok", "tip", "moved", "diverged", "reached", "detail"}.
    With a `mirror` (its worktree has `branch` checked out), the branch moves by a fast-forward
    merge IN the mirror, so its files follow; a mirror with files in the way is reported,
    never overwritten.

    The slot's tip lands in refs/lanes/<sid>/_incoming/<lane> first (forced: it is only what
    the slot has now), and divergence is judged by ancestry, never by git's words: the slot
    rewrote its branch when the tip fetched before (refs/lanes/<sid>/<lane>) is not in the new
    tip's history; this repo's branch moved on its own when it is not either. Both keep both
    tips. `reached` is True once the slot's store answered: a failure after that (a mirror
    that cannot fast-forward) is not the slot being away."""
    branch = branch or f"track/{lane}"
    if not _LANE_RE.match(lane or "") or not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch):
        return {"ok": False, "detail": f"bad lane {lane!r} or branch {branch!r}"}
    lref = f"refs/lanes/{sid}/{lane}"
    inc = f"refs/lanes/{sid}/_incoming/{lane}"
    rc, out, err = _git(workspace, ["fetch", "-q", "--no-tags", store_url(slot, project),
                                    f"+refs/heads/{branch}:{inc}"], slot, FETCH_TIMEOUT_S)
    if rc != 0:
        return {"ok": False, "detail": f"fetch from the lane store: {(err or out).strip()[-300:]}"}
    rc, tip, _ = _git(workspace, ["rev-parse", "--verify", "-q", inc], slot, 60)
    tip = tip.strip()
    if rc != 0 or not tip:
        return {"ok": False, "reached": True, "detail": f"{inc} missing after the fetch"}
    rc, old, _ = _git(workspace, ["rev-parse", "--verify", "-q", lref], slot, 60)
    old = old.strip() if rc == 0 else ""
    if old and old != tip:
        rc, _, _ = _git(workspace, ["merge-base", "--is-ancestor", old, tip], slot, 60)
        if rc != 0:
            return {"ok": False, "diverged": True, "reached": True, "tip": tip, "detail":
                    f"the slot's {branch} was rewritten: {old[:12]} (fetched before) is not in "
                    f"its history now ({tip[:12]}); kept both ({lref} the earlier, {inc} the "
                    f"slot's now)"}
    if old != tip:
        args = ["update-ref", "-m", f"lane fetch from {sid}", lref, tip] + ([old] if old else [""])
        rc, out, err = _git(workspace, args, slot, 60)
        if rc != 0:
            return {"ok": False, "reached": True, "tip": tip,
                    "detail": f"update-ref {lref}: {(err or out).strip()[-200:]}"}
    href = f"refs/heads/{branch}"
    rc, cur, _ = _git(workspace, ["rev-parse", "--verify", "-q", href], slot, 60)
    cur = cur.strip() if rc == 0 else ""
    if cur == tip:
        return {"ok": True, "reached": True, "tip": tip, "moved": False,
                "detail": f"{branch} already at {tip[:12]}"}
    if cur:
        rc, _, _ = _git(workspace, ["merge-base", "--is-ancestor", cur, tip], slot, 60)
        if rc != 0:
            return {"ok": False, "diverged": True, "reached": True, "tip": tip, "detail":
                    f"{branch} here ({cur[:12]}) is not behind the slot's ({tip[:12]}): kept both "
                    f"({lref} holds the slot's)"}
    if mirror and os.path.isdir(mirror):
        rc, out, err = _git(mirror, ["merge", "-q", "--ff-only", tip], {}, 600)
        if rc != 0:
            return {"ok": False, "reached": True, "tip": tip,
                    "detail": "the read mirror did not fast-forward: " + (err or out).strip()[-300:]}
    else:
        args = ["update-ref", "-m", f"lane fetch from {sid}", href, tip] + ([cur] if cur else [""])
        rc, out, err = _git(workspace, args, slot, 60)
        if rc != 0:
            return {"ok": False, "reached": True, "tip": tip,
                    "detail": f"update-ref: {(err or out).strip()[-200:]}"}
    return {"ok": True, "reached": True, "tip": tip, "moved": True,
            "detail": f"{branch} {cur[:12] or '(new)'} -> {tip[:12]} from {sid}"}


SAVED_LIST = "_saved.json"


def dirty_rows(ws) -> list:
    """[(XY, path, orig)] of the lane's changed and untracked files, from
    `git status --porcelain=v1 -z` (names exactly as stored: quoted or non-ASCII ones too)."""
    from foundry_client.lane import parse_status_z
    return parse_status_z(ws.run("status", "--porcelain=v1", "-z", "--untracked-files=all"))


def save_dirty(ws, dest_dir: str) -> dict:
    """Copy the lane's modified and untracked files here (through its RemoteWorkspace) before a
    stop: <dest_dir>/<path>, and <dest_dir>/_saved.json {"saved", "deleted", "failed"} (with a
    readable _list.txt). Nothing changes on the slot.

    "ok" only when every changed file was copied whole: a file that could not be read (or
    only partly: RemoteWorkspace.read raises then), or whose name leads outside dest_dir, is in
    "failed" and makes ok False, and is never in "saved" (the list the slot's worktree-remove
    is given: only what is here may go with the worktree). A deleted file has nothing to copy;
    it is listed in "deleted" (its last content is in the commit)."""
    try:
        rows = dirty_rows(ws)
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "files": 0, "saved": [], "deleted": [], "failed": [],
                "dir": "", "detail": f"status: {e}"[:300]}
    root = os.path.realpath(dest_dir)
    saved, deleted, failed = [], [], []
    for xy, rel, orig in rows:
        if orig:
            deleted.append(orig)               # a rename's old name: its content is committed
        try:
            data = ws.read(rel)
        except Exception as e:                                   # noqa: BLE001
            failed.append({"path": rel, "why": f"{type(e).__name__}: {e}"[:200]})
            continue
        if data is None:
            if "D" in xy:
                deleted.append(rel)
            else:
                failed.append({"path": rel, "why": "not a file on the slot"})
            continue
        dst = os.path.realpath(os.path.join(root, *[p for p in rel.split("/") if p]))
        if not dst.startswith(root + os.sep):
            failed.append({"path": rel, "why": "its name leads outside the save folder"})
            continue
        if os.path.dirname(dst) == root and os.path.basename(dst) in (SAVED_LIST, "_list.txt"):
            failed.append({"path": rel, "why": "its name is the save folder's own list"})
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
        except OSError as e:
            failed.append({"path": rel, "why": f"writing it here: {e}"[:200]})
            continue
        saved.append(rel)
    if rows:
        os.makedirs(root, exist_ok=True)
        _plat.write_text(os.path.join(root, SAVED_LIST), json.dumps(
            {"saved": saved, "deleted": deleted, "failed": failed}, indent=1))
        lines = ([f"saved   {p}" for p in saved] + [f"deleted {p}" for p in deleted]
                 + [f"FAILED  {f['path']}: {f['why']}" for f in failed])
        _plat.write_text(os.path.join(root, "_list.txt"), "\n".join(lines) + "\n")
    detail = f"{len(saved)} of {len(saved) + len(failed)} changed file(s) saved"
    if deleted:
        detail += f", {len(deleted)} deleted"
    if failed:
        detail += f"; NOT saved: {', '.join(f['path'] for f in failed[:5])}"
    return {"ok": not failed, "files": len(saved), "saved": saved, "deleted": deleted,
            "failed": [f["path"] for f in failed], "dir": dest_dir if rows else "",
            "detail": detail}


def saved_list(dest_dir: str) -> list:
    """The paths a save_dirty folder holds (saved + deleted), from its _saved.json; [] when
    there is none (worktree-remove then refuses a worktree with any change)."""
    try:
        with open(os.path.join(dest_dir, SAVED_LIST), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    return [str(p) for p in (d.get("saved") or []) + (d.get("deleted") or [])
            if isinstance(p, str)]


__all__ = ["mirror_path", "mirror_create", "mirror_full", "MIRROR_DIR", "store_url", "store_path",
           "worktree_path", "push_base", "wait_job", "create", "discard", "checks_of",
           "git_env", "fetch", "save_dirty", "saved_list", "dirty_rows", "SAVED_LIST"]

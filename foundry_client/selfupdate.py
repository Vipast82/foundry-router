"""Update this PC's Foundry stack to a newer release, on demand or automatically (v0.181.257).

2026-11-23, Victor: "Can you build this update feature in to the client, so it can update on
demand / autoupdate". Until now a release went live by hand: move the deploy checkout
(workspace.deploy) to the new tag, then Restart client. A Restart client alone keeps the
coordinator running on its old code (it is adopted), which is how the coordinator stayed on
v0.181.253 after the router moved to .256.

One update is a job journaled to <state_dir>/client-update.json, so it survives the client's
own restart (and a crash) and carries on from the phase it reached:

  checking                fetch the tags; the tag must exist, be on origin/main, and name
                          its own release in foundry_router/__init__.py; it must be newer
                          than what the deploy checkout holds; no modified tracked files; and
                          no local change the checkout would throw away. A file is a local
                          change when its blob at HEAD appears nowhere in the tag's history
                          (a deploy snapshot that only copies main's files is fine; one that
                          kept its own copy of a file is not). A ref
                          refs/foundry/pre-update/<id> keeps the old HEAD.
  making_gap              Victor chose "make a gap": every project is paused (safe mode, the
                          same pause the upgrade supervisor uses, actor client-update), so no
                          new sprint session starts, and the job waits until nothing of the
                          coordinator's is working (components.safe_point: sessions, lead and
                          repair work, the Studio gate runner), no Lead upgrade is open and
                          the supervisor holds no upgrade lock. client.update_gap_max_minutes
                          caps the wait; then the pauses are released and the job fails.
  installing              an online backup of coordinator.sqlite, then
                          `git checkout --detach <tag>` in the deploy checkout
  restarting_coordinator  the client restarts the coordinator and waits for a heartbeat from
                          the new process naming the new release
  resuming                the job's own pauses are cleared (an operator's are never touched)
  restarting_supervisor   the supervisor loop restarts on the new code
  restarting_client       an import check of the new code, then a fresh client that adopts
                          every running component (selfrestart); the new client finishes the
                          job when it runs the new release

A failure in installing or restarting_coordinator rolls back: the checkout returns to the old
HEAD, the database is restored only when the new build migrated it, the coordinator restarts
on the old code and the pauses are released. Kilo, OpenCode, Rojo and Blender are never
restarted. The router runs on another machine and is not updated here.

Automatic: client.auto_update (off by default) starts the same job when the release check
(every client.update_check_minutes) finds a newer release than the deploy checkout holds. A
release whose automatic update failed is not tried again automatically (Update now still
can); one that only found no gap is tried again at the next check.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import __version__, selfrestart
from .components import console_python, safe_point
from .config import expand

STATE_FILE = "client-update.json"
ACTOR = "client-update"
PHASES = ("checking", "making_gap", "installing", "restarting_coordinator", "resuming",
          "restarting_supervisor", "restarting_client")
TERMINAL = ("done", "failed", "rolled_back", "cancelled")
# a failure here puts the old build back
ROLLBACK_PHASES = ("installing", "restarting_coordinator")
# phases a cancel can still stop (nothing has changed yet)
CANCELLABLE = ("checking", "making_gap")
HISTORY = 10
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
TAG_RE = re.compile(r"^v?\d+(\.\d+){1,3}$")


class UpdateFailed(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


def parse_version(v: str) -> tuple:
    nums = re.findall(r"\d+", str(v or "").split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def git(repo: str, *args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    if check and r.returncode:
        raise UpdateFailed(f"git {' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def release_at(repo: str, ref: str) -> str:
    r = git(repo, "show", f"{ref}:foundry_router/__init__.py", check=False)
    if r.returncode:
        return ""
    for line in r.stdout.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def local_changes(repo: str, head: str, target: str) -> list[str]:
    """Files whose content at `head` appears nowhere in `target`'s history: the checkout of
    `target` would throw them away. Files `head` lacks are not local changes."""
    changed = [f for f in git(repo, "diff", "--name-only", "-z", "--no-renames", head,
                              target).stdout.split("\0") if f]
    if not changed:
        return []
    want = set(changed)
    blobs = {}
    for line in git(repo, "ls-tree", "-r", "-z", head).stdout.split("\0"):
        if not line or "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        if path in want:
            blobs[path] = meta.split()[2]
    if not blobs:
        return []
    known = {line.split(" ", 1)[0] for line in
             git(repo, "rev-list", "--objects", target, timeout=300).stdout.splitlines() if line}
    return sorted(p for p, b in blobs.items() if b not in known)


def schema_versions(db: str) -> dict:
    from foundry_coordinator.supervisor import schema_versions as sv
    return sv(db)


class Ops:
    """What the update does to the running stack. The manager's own methods; tests replace
    this object."""

    def __init__(self, mgr):
        self.mgr = mgr

    # -- the gap -------------------------------------------------------------------
    def store(self):
        from foundry_coordinator.store import Store
        return Store(os.path.join(expand(self.mgr.cfg["client"]["state_dir"]), "coordinator.sqlite"))

    def busy(self) -> str:
        """'' when nothing of the coordinator's is working, else why not."""
        from foundry_coordinator import supervisor as SUP
        cfg = self.mgr.cfg
        state = expand(cfg["client"]["state_dir"])
        ok, why = safe_point(state, (cfg.get("workspace") or {}).get("game") or "")
        if not ok:
            return why
        if SUP.lock_pid(state):
            return "the upgrade supervisor is in the middle of an upgrade"
        db = os.path.join(state, "coordinator.sqlite")
        if os.path.isfile(db):
            try:
                c = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True, timeout=5)
                try:
                    n = c.execute("SELECT COUNT(*) FROM upgrade_requests WHERE status NOT IN "
                                  "('done','rolled_back','failed','cancelled')").fetchone()[0]
                except sqlite3.Error:
                    n = 0
                finally:
                    c.close()
                if n:
                    return f"{n} Lead upgrade(s) open"
            except sqlite3.Error as e:
                return f"cannot read the coordinator database ({e})"
        return ""

    # -- the stack -----------------------------------------------------------------
    def heartbeat(self) -> dict:
        p = os.path.join(expand(self.mgr.cfg["client"]["state_dir"]), "coordinator.heartbeat")
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def restart(self, name: str) -> None:
        self.mgr.restart(name, force=True)

    def stop(self, name: str) -> None:
        self.mgr.stop(name)

    def start(self, name: str) -> None:
        self.mgr.start(name)

    def component_wanted(self, name: str) -> bool:
        pr = self.mgr.procs.get(name)
        return bool(pr and pr.want)

    def supervisor_busy(self) -> bool:
        from foundry_coordinator import supervisor as SUP
        return bool(SUP.lock_pid(expand(self.mgr.cfg["client"]["state_dir"])))

    def client_runs_from(self) -> str:
        return str(Path(__file__).resolve().parents[1])

    def frozen(self) -> bool:
        return bool(getattr(sys, "frozen", False))

    def smoke(self, deploy: str) -> str:
        """'' when the new code imports, else the error."""
        r = subprocess.run([console_python(sys.executable), "-c",
                            "import foundry_client.api, foundry_client.manager, "
                            "foundry_coordinator.engine, foundry_coordinator.supervisor"],
                           cwd=deploy, capture_output=True, text=True, timeout=180,
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        return "" if r.returncode == 0 else (r.stderr or r.stdout).strip()[-600:]

    def restart_client(self, deploy: str) -> None:
        # selfrestart and components are imported with this module: once the checkout has
        # moved, a lazy import here would load the NEW code into this old client
        selfrestart.spawn_successor(self.mgr.cfg["client"]["api_port"], cwd=deploy)
        selfrestart.exit_soon(self.mgr, delay=2.0)

    def event(self, text: str) -> None:
        self.mgr._event("update", text)


class Updater:
    def __init__(self, mgr, ops: Optional[Ops] = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, poll: float = 5.0,
                 health_timeout: float = 240.0, current: str = __version__):
        self.mgr = mgr
        self.ops = ops or Ops(mgr)
        self.clock, self.sleep, self.poll = clock, sleep, poll
        self.health_timeout = health_timeout
        self.current = current
        self.lock = threading.RLock()
        self.thread: Optional[threading.Thread] = None
        self.cancel_evt = threading.Event()

    # ---------------------------------------------------------------- the journal
    @property
    def cfg(self) -> dict:
        return self.mgr.cfg

    def path(self) -> str:
        return os.path.join(expand(self.cfg["client"]["state_dir"]), STATE_FILE)

    def deploy(self) -> str:
        return expand((self.cfg.get("workspace") or {}).get("deploy") or "")

    def load(self) -> dict:
        try:
            with open(self.path(), encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                d.setdefault("job", None)
                d.setdefault("history", [])
                d.setdefault("auto_failed", [])
                return d
        except (OSError, ValueError):
            pass
        return {"job": None, "history": [], "auto_failed": []}

    def save(self, d: dict) -> None:
        p = self.path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1, sort_keys=True, default=str)
        os.replace(tmp, p)

    def _note(self, d: dict, text: str, phase: Optional[str] = None) -> None:
        job = d["job"]
        if phase:
            job["phase"] = phase
        job["updated_ts"] = self.clock()
        job.setdefault("log", []).append({"ts": self.clock(), "phase": job["phase"], "text": text})
        job["log"] = job["log"][-60:]
        self.save(d)
        try:
            self.ops.event(f"{job['tag']}: {text}")
        except Exception:                                         # noqa: BLE001
            pass

    def _finish(self, d: dict, status: str, text: str, error: str = "") -> dict:
        job = d["job"]
        job.update(status=status, error=error[:1500], ended_ts=self.clock())
        self._note(d, text)
        if status in ("failed", "rolled_back") and job.get("auto") and not job.get("retry_auto"):
            d["auto_failed"] = (d.get("auto_failed") or [])[-20:] + [job["tag"]]
        d["history"] = ([job] + list(d.get("history") or []))[:HISTORY]
        d["job"] = None
        self.save(d)
        return job

    # ---------------------------------------------------------------- the API
    def status(self) -> dict:
        with self.lock:
            d = self.load()
        cl = self.cfg["client"]
        job = d["job"]
        running = bool(self.thread and self.thread.is_alive())
        return {"job": job, "running": running or bool(job),
                "history": [{k: h.get(k) for k in ("id", "tag", "status", "error", "auto",
                                                   "started_ts", "ended_ts", "prior_release")}
                            for h in d["history"]],
                "auto_update": bool(cl.get("auto_update")),
                "installed": self.installed(),
                "cancellable": bool(job and job.get("phase") in CANCELLABLE)}

    def installed(self) -> str:
        dep = self.deploy()
        if not dep or not os.path.isdir(os.path.join(dep, ".git")):
            return ""
        try:
            return release_at(dep, "HEAD")
        except (UpdateFailed, OSError, subprocess.SubprocessError):
            return ""

    def start(self, tag: str = "", auto: bool = False) -> dict:
        """Start an update to `tag` (default: the newest release the check found)."""
        with self.lock:
            d = self.load()
            if d["job"] or (self.thread and self.thread.is_alive()):
                return {"error": "an update is already running", "job": d["job"]}
            dep = self.deploy()
            if not dep or not os.path.isdir(os.path.join(dep, ".git")):
                return {"error": "set the coordinator release checkout (workspace.deploy) in "
                                 "Settings: the update moves that checkout to the new release"}
            if not tag:
                u = self.mgr.update.snapshot()
                tag = u.get("latest") or ""
                if not tag:
                    return {"error": "no release found yet: use Check for updates first"}
            tag = tag.strip()
            if not TAG_RE.match(tag):
                return {"error": f"{tag!r} is not a release tag"}
            if not tag.startswith("v"):
                tag = "v" + tag
            job = {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6], "tag": tag,
                   "release": tag[1:], "auto": bool(auto), "phase": PHASES[0],
                   "status": "running", "started_ts": self.clock(), "updated_ts": self.clock(),
                   "log": [], "pauses": {}, "error": ""}
            d["job"] = job
            self.cancel_evt.clear()
            self._note(d, ("automatic update" if auto else "update") + f" to {tag} started")
            self._spawn()
            return {"ok": True, "job": job}

    def cancel(self) -> dict:
        with self.lock:
            d = self.load()
            job = d["job"]
            if not job:
                return {"error": "no update is running"}
            if job["phase"] not in CANCELLABLE:
                return {"error": f"too late to cancel: the update is {job['phase'].replace('_', ' ')}"}
            self.cancel_evt.set()
            if not (self.thread and self.thread.is_alive()):
                self._release(d)
                return {"ok": True, "job": self._finish(d, "cancelled", "cancelled")}
            return {"ok": True, "note": "cancelling"}

    def resume_on_start(self) -> None:
        """A job the previous client left running carries on here."""
        with self.lock:
            d = self.load()
            if d["job"] and not (self.thread and self.thread.is_alive()):
                self._note(d, "resumed after a client start")
                self._spawn()

    def maybe_auto(self) -> Optional[dict]:
        """The release check found something: start an automatic update when it is on."""
        if not self.cfg["client"].get("auto_update"):
            return None
        u = self.mgr.update.snapshot()
        latest = u.get("latest") or ""
        if not latest or u.get("error"):
            return None
        with self.lock:
            d = self.load()
            if d["job"] or (self.thread and self.thread.is_alive()):
                return None
            tag = latest if latest.startswith("v") else "v" + latest
            if tag in (d.get("auto_failed") or []):
                return None
        have = self.installed()
        if not have or parse_version(latest) <= parse_version(have):
            return None
        return self.start(tag, auto=True)

    def _spawn(self) -> None:
        self.thread = threading.Thread(target=self._drive, name="foundry-client-update",
                                       daemon=True)
        self.thread.start()

    # ---------------------------------------------------------------- the job
    def _drive(self) -> None:
        try:
            self.run_job()
        except Exception as e:                                    # noqa: BLE001
            with self.lock:
                d = self.load()
                if d["job"]:
                    self._release(d)
                    self._finish(d, "failed", "the update stopped on an internal error",
                                 f"{type(e).__name__}: {e}")

    def run_job(self) -> Optional[dict]:
        while True:
            with self.lock:
                d = self.load()
            job = d["job"]
            if not job:
                return None
            phase = job["phase"]
            if phase == "verifying_client":
                return self._verify_client(d)
            try:
                if self.cancel_evt.is_set() and phase in CANCELLABLE:
                    raise Cancelled()
                out = getattr(self, "_" + phase)(d)
            except Cancelled:
                self._release(d)
                return self._finish(d, "cancelled", "cancelled; nothing was changed")
            except UpdateFailed as e:
                if phase in ROLLBACK_PHASES:
                    return self._rollback(d, f"{phase.replace('_', ' ')}: {e}")
                self._release(d)
                return self._finish(d, "failed", f"stopped at {phase.replace('_', ' ')}; "
                                                 "nothing was changed", str(e))
            if out is not None:            # the phase ended the job (done, or a client restart)
                return out
            nxt = PHASES.index(phase) + 1
            if nxt >= len(PHASES):
                return self._finish(d, "done", f"updated to {job['tag']}")
            self._note(d, f"{phase.replace('_', ' ')} done", PHASES[nxt])

    # checking --------------------------------------------------------------------
    def _checking(self, d: dict) -> None:
        job, dep = d["job"], self.deploy()
        tag = job["tag"]
        git(dep, "fetch", "--quiet", "origin", "--tags", "+refs/heads/main:refs/remotes/origin/main",
            timeout=300)
        r = git(dep, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}", check=False)
        if r.returncode:
            raise UpdateFailed(f"the tag {tag} is not in the repository")
        sha = r.stdout.strip()
        if git(dep, "merge-base", "--is-ancestor", sha, "refs/remotes/origin/main",
               check=False).returncode:
            raise UpdateFailed(f"{tag} is not on main")
        rel = release_at(dep, sha)
        if rel != job["release"]:
            raise UpdateFailed(f"{tag} names release {rel or 'none'} in foundry_router/__init__.py")
        head = git(dep, "rev-parse", "HEAD").stdout.strip()
        have = release_at(dep, head)
        job.update(sha=sha, prior_sha=head, prior_release=have)
        if head == sha:
            raise UpdateFailed(f"the deploy checkout is already on {tag}")
        if have and parse_version(rel) <= parse_version(have):
            raise UpdateFailed(f"{tag} is not newer than the installed {have}")
        dirty = git(dep, "status", "--porcelain", "--untracked-files=no").stdout.strip()
        if dirty:
            raise UpdateFailed("the deploy checkout has modified files; commit or undo them "
                               "first: " + "; ".join(dirty.splitlines()[:10]))
        lost = local_changes(dep, head, sha)
        if lost:
            raise UpdateFailed(f"the deploy checkout has its own version of {len(lost)} file(s) "
                               f"that {tag} would replace: {', '.join(lost[:15])}"
                               + (" …" if len(lost) > 15 else "")
                               + ". Put those changes on main, or check out the release by hand")
        git(dep, "update-ref", f"refs/foundry/pre-update/{job['id']}", head)
        self._note(d, f"{tag} ({sha[:9]}) checks out cleanly over {have or head[:9]}")

    # making_gap ------------------------------------------------------------------
    def _pause_all(self, d: dict) -> int:
        from foundry_coordinator.telemetry import PROJECT_PAUSE_META
        job = d["job"]
        store = self.ops.store()
        new = 0
        try:
            for proj in store.projects():
                p = proj["project_id"]
                if p in job["pauses"]:
                    continue
                new += 1
                if proj["paused"]:
                    job["pauses"][p] = False         # someone else's pause: left alone
                    continue
                note = f"paused for the update to {job['tag']}"
                meta = {"mode": "safe", "note": note, "actor": ACTOR, "since": self.clock(),
                        "tasks": [], "resume_after_upgrade": False, "version": {},
                        "upgrade_id": job["id"]}
                store.set_meta(PROJECT_PAUSE_META + p, json.dumps(meta, sort_keys=True))
                store.set_project_paused(p, True, note)
                job["pauses"][p] = True
        finally:
            store.conn.close()
        if new:
            self.save(d)
        return new

    def _release(self, d: dict) -> list:
        """Clear the job's own pauses; a pause an operator took over is kept."""
        from foundry_coordinator.telemetry import PROJECT_PAUSE_META
        job = d.get("job") or {}
        owned = [p for p, mine in (job.get("pauses") or {}).items() if mine]
        if not owned:
            return []
        out = []
        try:
            store = self.ops.store()
        except Exception:                                         # noqa: BLE001
            return []
        try:
            for p in owned:
                try:
                    meta = json.loads(store.meta(PROJECT_PAUSE_META + p) or "{}")
                except ValueError:
                    meta = {}
                if meta and (meta.get("actor") != ACTOR or meta.get("upgrade_id") != job["id"]):
                    continue
                store.x("DELETE FROM meta WHERE key=?", (PROJECT_PAUSE_META + p,))
                if store.project(p):
                    store.set_project_paused(p, False, "")
                out.append(p)
                job["pauses"][p] = False
        finally:
            store.conn.close()
        if out:
            self.save(d)
        return out

    def _making_gap(self, d: dict) -> None:
        job = d["job"]
        limit = int(self.cfg["client"].get("update_gap_max_minutes") or 60) * 60
        job.setdefault("gap_since", self.clock())
        n = self._pause_all(d)
        if n:
            self._note(d, f"paused {sum(1 for v in job['pauses'].values() if v)} project(s) so "
                          "no new session starts; waiting for the running ones to finish")
        last = ""
        while True:
            if self.cancel_evt.is_set():
                raise Cancelled()
            self._pause_all(d)                    # a project registered meanwhile
            why = self.ops.busy()
            if not why:
                self._note(d, "nothing is working: the gap is there")
                return
            if why != last:
                job["waiting_for"] = why
                self._note(d, f"waiting: {why}")
                last = why
            if self.clock() - job["gap_since"] > limit:
                job["retry_auto"] = True          # busy, not broken: the next check tries again
                raise UpdateFailed(f"no gap within {limit // 60} min ({why}); the projects "
                                   "carry on and the update can be tried again")
            self.sleep(self.poll)

    # installing ------------------------------------------------------------------
    def _installing(self, d: dict) -> None:
        job, dep = d["job"], self.deploy()
        state = expand(self.cfg["client"]["state_dir"])
        db = os.path.join(state, "coordinator.sqlite")
        if "backup" not in job and os.path.isfile(db):
            bdir = os.path.join(state, "update_backups", job["id"])
            os.makedirs(bdir, exist_ok=True)
            dst = os.path.join(bdir, "coordinator.sqlite")
            src = sqlite3.connect(db, timeout=30)
            out = sqlite3.connect(dst + ".tmp")
            try:
                src.backup(out)
            finally:
                out.close()
                src.close()
            os.replace(dst + ".tmp", dst)
            job["backup"] = dst
            try:
                job["schema_before"] = schema_versions(dst)
            except Exception:                                     # noqa: BLE001
                job["schema_before"] = None
            self._note(d, "coordinator database backed up")
        head = git(dep, "rev-parse", "HEAD").stdout.strip()
        if head != job["sha"]:
            if head != job["prior_sha"]:
                raise UpdateFailed(f"the deploy checkout moved to {head[:9]} during the update")
            job["install_started"] = True
            self.save(d)
            git(dep, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach",
                job["sha"])
        if git(dep, "rev-parse", "HEAD").stdout.strip() != job["sha"]:
            raise UpdateFailed("the checkout did not land on the release")
        self._note(d, f"deploy checkout moved to {job['tag']}")

    # restarting_coordinator ------------------------------------------------------
    def _wait_coordinator(self, release: str, since: float, old_pid: int) -> str:
        """'' once a coordinator other than `old_pid` beats with `release` after `since`."""
        deadline = self.clock() + self.health_timeout
        seen = {}
        while self.clock() < deadline:
            seen = self.ops.heartbeat()
            pid = int(seen.get("pid") or 0)
            ver = str(seen.get("release") or seen.get("version") or "")
            if pid and pid != old_pid and float(seen.get("ts") or 0) >= since and ver == release \
                    and seen.get("ok", True):
                return ""
            self.sleep(min(self.poll, 2.0))
        return (f"no heartbeat from a coordinator on {release} within "
                f"{int(self.health_timeout)} s (last: pid {seen.get('pid')}, "
                f"{seen.get('release') or seen.get('version') or 'no version'}"
                + (f", errors {seen.get('errors')}" if seen.get("errors") else "") + ")")

    def _restarting_coordinator(self, d: dict) -> None:
        job = d["job"]
        if not self.ops.component_wanted("coordinator"):
            self._note(d, "the coordinator is not run by this client: not restarted")
            return
        old = int(self.ops.heartbeat().get("pid") or 0)
        since = self.clock()
        self.ops.restart("coordinator")
        why = self._wait_coordinator(job["release"], since, old)
        if why:
            raise UpdateFailed(why)
        self._note(d, f"coordinator running {job['release']}")

    # resuming --------------------------------------------------------------------
    def _resuming(self, d: dict) -> None:
        rel = self._release(d)
        self._note(d, f"resumed {', '.join(rel)}" if rel else "nothing to resume")

    # restarting_supervisor -------------------------------------------------------
    def _restarting_supervisor(self, d: dict) -> None:
        if not self.ops.component_wanted("supervisor"):
            return
        for _ in range(60):
            if not self.ops.supervisor_busy():
                break
            self.sleep(self.poll)
        else:
            self._note(d, "the upgrade supervisor stayed busy: it picks up the new code at its "
                          "next restart")
            return
        try:
            self.ops.restart("supervisor")
            self._note(d, "upgrade supervisor restarted on the new code")
        except Exception as e:                                    # noqa: BLE001
            self._note(d, f"the upgrade supervisor did not restart ({e}); it keeps the old "
                          "code until its next restart")

    # restarting_client -----------------------------------------------------------
    def _restarting_client(self, d: dict) -> Optional[dict]:
        job, dep = d["job"], self.deploy()
        if self.ops.frozen():
            return self._finish(d, "done", f"updated to {job['tag']}; this client is an "
                                           "installed build: install the new release to update "
                                           "the client itself")
        try:
            same = os.path.samefile(self.ops.client_runs_from(), dep)
        except OSError:
            same = False
        if not same:
            return self._finish(d, "done", f"updated to {job['tag']}; the client runs from "
                                           f"{self.ops.client_runs_from()}, not the deploy "
                                           "checkout, so it was not restarted")
        err = self.ops.smoke(dep)
        if err:
            return self._finish(d, "done", f"updated the coordinator to {job['tag']}, but the "
                                           "new client code does not import, so this client "
                                           "keeps running the old code", err)
        self._note(d, "starting a fresh client on the new code", "verifying_client")
        self.ops.restart_client(dep)
        return job

    def _verify_client(self, d: dict) -> dict:
        job = d["job"]
        if self.current == job["release"]:
            return self._finish(d, "done", f"updated to {job['tag']}")
        return self._finish(d, "done", f"updated the coordinator to {job['tag']}; the client "
                                       f"restarted but runs {self.current}",
                            f"the client runs {self.current}, not {job['release']}")

    # rollback --------------------------------------------------------------------
    def _rollback(self, d: dict, why: str) -> dict:
        job, dep = d["job"], self.deploy()
        steps = []
        runs = self.ops.component_wanted("coordinator")
        try:
            head = git(dep, "rev-parse", "HEAD").stdout.strip()
            if head != job["prior_sha"]:
                if head != job.get("sha"):
                    raise UpdateFailed(f"the checkout is at {head[:9]}, neither the old "
                                       f"{job['prior_sha'][:9]} nor the release: left for you")
                git(dep, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach",
                    job["prior_sha"])
                steps.append(f"checkout back on {job.get('prior_release') or job['prior_sha'][:9]}")
            state = expand(self.cfg["client"]["state_dir"])
            db = os.path.join(state, "coordinator.sqlite")
            before = job.get("schema_before")
            if job.get("backup") and before is not None and os.path.isfile(db):
                try:
                    after = schema_versions(db)
                except Exception:                                 # noqa: BLE001
                    after = None
                if after != before:
                    if runs:
                        self.ops.stop("coordinator")
                    src = sqlite3.connect(job["backup"])
                    dst = sqlite3.connect(db, timeout=30)
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                        src.close()
                    steps.append("database restored (the new build had migrated it)")
            if runs:
                old = int(self.ops.heartbeat().get("pid") or 0)
                since = self.clock()
                if self.ops.component_wanted("coordinator"):
                    self.ops.restart("coordinator")
                else:
                    self.ops.start("coordinator")      # stopped for the database restore
                bad = self._wait_coordinator(job.get("prior_release") or "", since, old) \
                    if job.get("prior_release") else ""
                steps.append("coordinator restarted on the old code"
                             + (f" (but {bad})" if bad else ""))
        except Exception as e:                                    # noqa: BLE001
            self._release(d)
            return self._finish(d, "failed", "the update failed and putting the old build "
                                             "back failed too: check the deploy checkout",
                                f"{why}; rollback: {type(e).__name__}: {e}")
        self._release(d)
        return self._finish(d, "rolled_back", "the update failed; " + "; ".join(steps or
                                                                              ["nothing to undo"]),
                            why)

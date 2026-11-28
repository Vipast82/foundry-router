"""The lane agent: a slot's side of a remote lane (Distributed Foundry phase b, B1).

docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md. On a Studio slot the Foundry Client runs with
`[lane] enabled = true` (the `slot` profile). Besides kilo / opencode it then answers the
coordinator, through the lane tunnel, for the lanes whose worktrees live here:

    GET  /api/lane/health                         what this slot holds and how it is
    POST /api/lane/git    {project, lane, argv, timeout, env, input}
    POST /api/lane/read   {project, lane, path, max_bytes}
    POST /api/lane/stat   {project, lane, paths}
    POST /api/lane/write  {project, lane, path, data}
    POST /api/lane/run    {project, lane, job, args}   -> {run_id}
    GET  /api/lane/run/<id>?offset=N                  output from N, state, rc
    POST /api/lane/run/<id>/cancel

Every lane call needs the LANE token (header X-Lane-Token), a different secret from the
client's own api.token: the lane token opens these routes and nothing else. It is made fresh
at each start and kept in `[lane] secrets_file` (tmpfs on a slot, mode 600), which the
coordinator reads over ssh when it opens the tunnel (decision Db1). Nothing here talks to a
remote: a lane never pushes or fetches; the junction fetches from the lane store.

Standard library only (the coordinator imports `env_allowed` and the name rules).
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets as _rnd
import shutil
import subprocess
import threading
import time
import uuid
from typing import Callable, Optional

import foundry_platform as _plat

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")          # tracks/manager.py NAME_RE
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# the git subcommands the coordinator's Workspace uses (adapters/git.py) and nothing that
# talks to another repository: no push, fetch, pull, clone, remote, ls-remote, submodule
GIT_ALLOWED = frozenset((
    "rev-parse", "rev-list", "merge-base", "diff", "diff-tree", "log", "show", "ls-tree",
    "ls-files", "status", "add", "commit", "reset", "symbolic-ref", "update-ref", "checkout",
    "cat-file", "commit-tree", "write-tree", "read-tree", "hash-object", "for-each-ref",
    "show-ref", "describe", "grep", "blame"))
# options that would write or run something outside the worktree
_BAD_ARG = re.compile(r"^(--output(=|$)|--ext-diff$|--exec(=|$)|--upload-pack|--receive-pack|"
                      r"--git-dir|--work-tree|--pathspec-from-file=(?!-$))")
_ENV_DENY = re.compile(r"^(PATH|HOME|SHELL|LD_.*|DYLD_.*|PYTHON.*|SSH_.*|GIT_(DIR|WORK_TREE|"
                       r"INDEX_FILE|EXEC_PATH|SSH.*|CONFIG.*|ASKPASS|PROXY.*|ALTERNATE.*|"
                       r"OBJECT_DIRECTORY|NAMESPACE|CEILING.*|TRACE.*))$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_READ = 8 * 1024 * 1024
MAX_WRITE = 8 * 1024 * 1024
MAX_GIT_OUT = 32 * 1024 * 1024
JOBS = ("worktree-add", "worktree-remove", "verify", "store-init")


class LaneError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def env_allowed(name: str) -> bool:
    """A variable a coordinator commit may set on the slot: author/committer names and a
    project's hook overrides (AU_ALLOW_SCOPE); never a path, a git location or a secret."""
    n = str(name or "")
    if not _ENV_NAME.match(n) or _ENV_DENY.match(n):
        return False
    if any(w in n for w in ("PASSWORD", "SECRET", "TOKEN", "_KEY", "APIKEY", "CREDENTIAL")):
        return False
    return not n.startswith("GIT_") or bool(re.match(r"^GIT_(AUTHOR|COMMITTER)_(NAME|EMAIL|DATE)$",
                                                     n))


def check_git_argv(argv) -> list:
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise LaneError("argv must be a non-empty list of strings")
    if argv[0] not in GIT_ALLOWED:
        raise LaneError(f"git {argv[0]!r} is not allowed on a lane")
    for a in argv[1:]:
        if _BAD_ARG.match(a):
            raise LaneError(f"git option {a.split('=')[0]!r} is not allowed on a lane")
    if argv[0] == "checkout" and "--" not in argv:
        raise LaneError("git checkout on a lane only restores paths (checkout -- <path>)")
    return argv


class LaneHost:
    """The lanes this machine holds, and the calls on them. `root` holds the lane stores
    (`<root>/<project>.git`, bare) and worktrees (`<root>/<project>/<lane>`)."""

    def __init__(self, root: str, state_dir: str, version: str = "", clock: Callable = time.time):
        self.root = os.path.realpath(root)
        self.state_dir = state_dir
        self.version = version
        self.clock = clock
        self.started = clock()
        self._lock = threading.Lock()
        self._runs: dict[str, dict] = {}
        os.makedirs(state_dir, exist_ok=True)

    # ------------------------------------------------------------------ registry

    @property
    def registry_path(self) -> str:
        return os.path.join(self.state_dir, "lanes.json")

    def lanes(self) -> dict:
        try:
            with open(self.registry_path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict) -> None:
        _plat.write_text(self.registry_path, json.dumps(d, indent=1, sort_keys=True))

    @staticmethod
    def key(project: str, lane: str) -> str:
        if not PROJECT_RE.match(str(project or "")):
            raise LaneError(f"bad project id {project!r}")
        if not NAME_RE.match(str(lane or "")):
            raise LaneError(f"bad lane name {lane!r}")
        return f"{project}/{lane}"

    def store(self, project: str) -> str:
        self.key(project, "x")
        return os.path.join(self.root, project + ".git")

    def worktree_of(self, project: str, lane: str) -> str:
        k = self.key(project, lane)
        row = self.lanes().get(k)
        if not row:
            raise LaneError(f"this slot holds no lane {k}", 404)
        wt = os.path.realpath(row["worktree"])
        if not (wt + os.sep).startswith(self.root + os.sep) or not os.path.isdir(wt):
            raise LaneError(f"lane {k}: its worktree {wt} is missing", 410)
        return wt

    # ------------------------------------------------------------------ git and files

    def git(self, project: str, lane: str, argv, timeout: float = 300, env=None,
            input: Optional[str] = None) -> dict:
        wt = self.worktree_of(project, lane)
        argv = check_git_argv(argv)
        extra = {k: str(v) for k, v in (env or {}).items() if env_allowed(k)}
        e = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", **extra}
        t = max(1.0, min(float(timeout or 300), 3600.0))
        try:
            r = subprocess.run(["git", "-C", wt, *argv], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=t, env=e,
                               input=input, **_plat.no_window_kw())
        except subprocess.TimeoutExpired:
            return {"rc": -1, "stdout": "", "stderr": f"timed out after {int(t)} s",
                    "timeout": True}
        return {"rc": r.returncode, "stdout": (r.stdout or "")[:MAX_GIT_OUT],
                "stderr": (r.stderr or "")[-20000:]}

    def _inside(self, wt: str, rel: str) -> str:
        r = str(rel or "").replace("\\", "/").strip()
        parts = [x for x in r.split("/") if x not in ("", ".")]
        if not parts or r.startswith("/") or re.match(r"^[A-Za-z]:", r) or ".." in parts \
                or parts[0] == ".git":
            raise LaneError(f"{rel!r}: not a path inside the lane")
        p = os.path.realpath(os.path.join(wt, *parts))
        if not (p + os.sep).startswith(wt + os.sep) and p != wt:
            raise LaneError(f"{rel!r}: leads outside the lane")
        return p

    def read(self, project: str, lane: str, path: str, max_bytes=None) -> dict:
        wt = self.worktree_of(project, lane)
        p = self._inside(wt, path)
        if not os.path.isfile(p):
            return {"exists": False}
        cap = MAX_READ if max_bytes is None else max(0, min(int(max_bytes), MAX_READ))
        with open(p, "rb") as f:
            data = f.read(cap + 1)
        return {"exists": True, "truncated": len(data) > cap,
                "data": base64.b64encode(data[:cap]).decode("ascii")}

    def stat(self, project: str, lane: str, paths) -> dict:
        wt = self.worktree_of(project, lane)
        if not isinstance(paths, list):
            raise LaneError("paths must be a list")
        out = {}
        for rel in paths[:5000]:
            p = self._inside(wt, rel)
            try:
                st = os.stat(p)
            except OSError:
                out[rel] = {"exists": False, "kind": "", "size": 0, "mtime": 0.0,
                            "mtime_ns": 0}
                continue
            kind = "dir" if os.path.isdir(p) else ("file" if os.path.isfile(p) else "other")
            out[rel] = {"exists": True, "kind": kind, "size": st.st_size, "mtime": st.st_mtime,
                        "mtime_ns": st.st_mtime_ns}
        return {"stats": out}

    def write(self, project: str, lane: str, path: str, data: str) -> dict:
        wt = self.worktree_of(project, lane)
        p = self._inside(wt, path)
        raw = base64.b64decode(data or "")
        if len(raw) > MAX_WRITE:
            raise LaneError(f"{path}: {len(raw)} bytes is over the {MAX_WRITE} byte limit")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f"{p}.tmp{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, p)
        return {"ok": True, "bytes": len(raw)}

    # ------------------------------------------------------------------ jobs

    def start_job(self, project: str, lane: str, job: str, args: Optional[dict] = None) -> dict:
        if job not in JOBS:
            raise LaneError(f"no job {job!r} (jobs: {', '.join(JOBS)})")
        k = self.key(project, lane)
        args = dict(args or {})
        rid = uuid.uuid4().hex[:16]
        log = os.path.join(self.state_dir, "runs", rid + ".log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        run = {"id": rid, "lane": k, "job": job, "state": "running", "rc": None,
               "started": self.clock(), "ended": None, "log": log, "proc": None,
               "cancel": False}
        with self._lock:
            self._runs[rid] = run
        threading.Thread(target=self._job, args=(run, project, lane, job, args), daemon=True,
                         name=f"lane-job-{rid}").start()
        return {"run_id": rid}

    def run_status(self, rid: str, offset: int = 0) -> dict:
        with self._lock:
            run = self._runs.get(rid)
        if not run:
            raise LaneError(f"no run {rid}", 404)
        try:
            with open(run["log"], "rb") as f:
                f.seek(max(0, int(offset)))
                chunk = f.read(1024 * 1024)
        except OSError:
            chunk = b""
        return {"id": rid, "job": run["job"], "lane": run["lane"], "state": run["state"],
                "rc": run["rc"], "offset": max(0, int(offset)) + len(chunk),
                "output": chunk.decode("utf-8", "replace")}

    def cancel(self, rid: str) -> dict:
        with self._lock:
            run = self._runs.get(rid)
        if not run:
            raise LaneError(f"no run {rid}", 404)
        run["cancel"] = True
        p = run.get("proc")
        if p is not None and p.poll() is None:
            p.kill()
        return {"ok": True}

    def _job(self, run: dict, project: str, lane: str, job: str, args: dict) -> None:
        rc = 1
        try:
            with open(run["log"], "ab") as log:
                def say(text: str) -> None:
                    log.write((text.rstrip("\n") + "\n").encode("utf-8"))
                    log.flush()
                if job == "worktree-add":
                    rc = self._worktree_add(project, lane, args, say, run, log)
                elif job == "worktree-remove":
                    rc = self._worktree_remove(project, lane, args, say, run, log)
                elif job == "verify":
                    rc = self._verify(project, lane, args, say, run, log)
                elif job == "store-init":
                    rc = self._store_init(project, say, run, log)
        except LaneError as e:
            _append(run["log"], f"LANE JOB FAIL: {e}")
        except Exception as e:                                    # noqa: BLE001
            _append(run["log"], f"LANE JOB FAIL: {type(e).__name__}: {e}")
        run["rc"] = rc
        run["ended"] = self.clock()
        run["state"] = "cancelled" if run["cancel"] else ("done" if rc == 0 else "failed")

    def _sub(self, argv: list, run: dict, log, cwd: Optional[str] = None,
             timeout: float = 1800, env: Optional[dict] = None) -> int:
        if run["cancel"]:
            return 130
        extra = {k: str(v) for k, v in (env or {}).items() if env_allowed(k)}
        try:
            p = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL,
                                 env={**os.environ, **extra} if extra else None,
                                 **_plat.no_window_kw())
        except FileNotFoundError as e:
            log.write(f"LANE command not found: {e}\n".encode("utf-8"))
            log.flush()
            return 127                       # as a shell says it (verify: UNKNOWN, never PASS)
        run["proc"] = p
        try:
            return p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            return 124
        finally:
            run["proc"] = None

    def _store_init(self, project, say, run, log) -> int:
        """The project's lane store, made empty when it is missing. setup-host.sh makes the
        slot's own game's store --shared with its game copy; another project's (the pilot's)
        starts empty, and the junction's base push fills it."""
        store = self.store(project)
        if os.path.isdir(store):
            say(f"LANE store-init: {store} is there")
            return 0
        os.makedirs(self.root, exist_ok=True)
        rc = self._sub(["git", "init", "-q", "--bare", store], run, log)
        say(f"LANE store-init: {store} {'made' if rc == 0 else f'FAILED rc={rc}'}")
        return rc

    def _worktree_add(self, project, lane, args, say, run, log) -> int:
        """git worktree add -b <branch> <root>/<project>/<lane> <base> in the lane store."""
        k = self.key(project, lane)
        store = self.store(project)
        if not os.path.isdir(store):
            raise LaneError(f"no lane store {store} (setup-host.sh makes it)", 409)
        with self._lock:
            have = self.lanes()
            if k in have:
                raise LaneError(f"this slot already holds lane {k} (one writer per lane)", 409)
            have[k] = {"project": project, "lane": lane, "state": "adding",
                       "worktree": os.path.join(self.root, project, lane),
                       "branch": str(args.get("branch") or f"track/{lane}"),
                       "added": self.clock()}
            self._save(have)
        row = have[k]
        base = str(args.get("base") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", base):
            self._forget(k)
            raise LaneError("worktree-add needs the base commit (40 hex)")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", row["branch"]) or ".." in row["branch"]:
            self._forget(k)
            raise LaneError(f"bad branch name {row['branch']!r}")
        os.makedirs(os.path.dirname(row["worktree"]), exist_ok=True)
        say(f"LANE worktree-add {k}: {row['branch']} at {base[:12]} -> {row['worktree']}")
        rc = self._sub(["git", "-C", store, "worktree", "add", "-b", row["branch"],
                        row["worktree"], base], run, log)
        if rc != 0:
            self._forget(k)
            say(f"LANE worktree-add FAIL rc={rc}")
            return rc
        with self._lock:
            have = self.lanes()
            if k in have:
                have[k]["state"] = "held"
                self._save(have)
        say(f"LANE worktree-add OK {k}")
        return 0

    def _worktree_remove(self, project, lane, args, say, run, log) -> int:
        """Remove a lane's worktree, only when it is clean and its tip is the one the junction
        says it holds (`args.tip`): the coordinator fetched it first. Never discards work."""
        k = self.key(project, lane)
        wt = self.worktree_of(project, lane)
        tip = str(args.get("tip") or "")
        head = self.git(project, lane, ["rev-parse", "HEAD"])["stdout"].strip()
        if not tip or head != tip:
            raise LaneError(f"lane {k}: HEAD {head[:12]} is not the fetched tip {tip[:12] or '?'}"
                            f" (fetch it first)", 409)
        dirty = self.git(project, lane, ["status", "--porcelain", "--untracked-files=all"])
        if dirty["stdout"].strip():
            raise LaneError(f"lane {k} has uncommitted files: save them first", 409)
        rc = self._sub(["git", "-C", self.store(project), "worktree", "remove", wt], run, log)
        if rc == 0:
            self._forget(k)
            say(f"LANE worktree-remove OK {k}")
        return rc

    def _verify(self, project, lane, args, say, run, log) -> int:
        """A profile's [[verify.checks]] command in the lane: argv list or a command line for
        the platform's job shell, the same one verify_cmd.run_check builds."""
        wt = self.worktree_of(project, lane)
        argv = args.get("argv")
        if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):
            cmd = argv
        elif isinstance(args.get("command"), str) and args["command"].strip():
            cmd = _plat.job_shell(args["command"], login=False)
        else:
            raise LaneError("verify needs argv (a list) or command (a string)")
        t = max(1.0, min(float(args.get("timeout") or 1800), 4 * 3600.0))
        say(f"LANE verify {k_of(project, lane)}: {' '.join(cmd)[:300]}")
        env = args.get("env") if isinstance(args.get("env"), dict) else None
        return self._sub(cmd, run, log, cwd=wt, timeout=t, env=env)

    def _forget(self, k: str) -> None:
        with self._lock:
            have = self.lanes()
            if have.pop(k, None) is not None:
                self._save(have)

    # ------------------------------------------------------------------ health

    def health(self, components: Optional[dict] = None) -> dict:
        total, used, free = (0, 0, 0)
        try:
            total, used, free = shutil.disk_usage(self.root if os.path.isdir(self.root)
                                                  else self.state_dir)
        except OSError:
            pass
        load = None
        try:
            load = [round(x, 2) for x in os.getloadavg()]
        except (AttributeError, OSError):
            pass
        lanes = {}
        for k, row in self.lanes().items():
            head = ""
            if row.get("state") == "held":
                try:
                    head = self.git(row["project"], row["lane"],
                                    ["rev-parse", "HEAD"], timeout=30)["stdout"].strip()
                except LaneError:
                    head = ""
            lanes[k] = {"branch": row.get("branch"), "state": row.get("state"), "head": head}
        return {"ok": True, "version": self.version, "os": _plat.os_kind(),
                "started": self.started, "now": self.clock(), "root": self.root,
                "disk": {"pct": round(100 * used / total) if total else None,
                         "free_gb": round(free / 2 ** 30, 1), "total_gb": round(total / 2 ** 30, 1)},
                "load": load, "mem_free_gb": _mem_free_gb(), "lanes": lanes,
                "components": components or {}}


def k_of(project: str, lane: str) -> str:
    return f"{project}/{lane}"


def _append(path: str, text: str) -> None:
    try:
        with open(path, "ab") as f:
            f.write((text.rstrip("\n") + "\n").encode("utf-8"))
    except OSError:
        pass


def _mem_free_gb() -> Optional[float]:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return round(int(line.split()[1]) / 2 ** 20, 1)
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------- the lane secrets

def ensure_secrets(path: str, passwords: Optional[dict] = None) -> dict:
    """Make a fresh lane token at each start and write {token, kilo_password,
    opencode_password} to `path` (mode 600; a tmpfs on a slot). The coordinator reads this
    file over ssh when it opens the tunnel; it is never logged. Returns the values."""
    vals = {"token": _rnd.token_urlsafe(32)}
    for k, v in (passwords or {}).items():
        if v:
            vals[k] = str(v)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(vals, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return vals


__all__ = ["LaneHost", "LaneError", "GIT_ALLOWED", "env_allowed", "check_git_argv",
           "ensure_secrets", "NAME_RE", "PROJECT_RE", "JOBS"]

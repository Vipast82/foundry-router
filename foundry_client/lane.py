"""The lane agent: a slot's side of a remote lane (Distributed Foundry phase b, B1).

docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md. On a Studio slot the Foundry Client runs with
`[lane] enabled = true` (the `slot` profile). Besides kilo / opencode it then answers the
coordinator, through the lane tunnel, for the lanes whose worktrees live here:

    GET  /api/lane/health                         what this slot holds and how it is
    POST /api/lane/git    {project, lane, argv, timeout, env, input}
    POST /api/lane/read   {project, lane, path, max_bytes, offset}  -> {exists, data, size,
                                                                       offset, truncated}
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

The token is not a shell (review 2026-11-27):
  * `git` runs only the subcommands and forms the coordinator's Workspace uses (check_git_argv:
    no options that write or run something outside the worktree, no branch switch, no reset
    of HEAD, update-ref only on the lane's own branch);
  * `verify` runs only a check REGISTERED for the lane when it was made: worktree-add takes
    {checks: {name: argv template}} (the project's [[verify.checks]] commands, placeholders
    unfilled) and keeps them in the lane registry; a verify job names a registered check and
    sends the same template, and the agent fills the placeholders itself ({task} {sha} {base}
    checked as names / hex, {workspace} the lane worktree, {python} this machine's python). A
    check that is not registered, or whose template changed since, is refused;
  * `lune` runs only `lune run tests/lune/run.luau` with its arguments.
What the token still opens: files inside lane worktrees and commits there, and so the
project's own git hooks and scripts that a later registered check runs. Keep it secret.

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
# options that would write or run something outside the worktree, or read files outside it
# (only the arguments before a "--" are options; after it git takes paths). A config override
# (`git -c x=y`, --config-env, --exec-path) is a global option before the subcommand, and argv[0]
# must be the subcommand, so none can be sent.
_BAD_ARG = re.compile(r"^(--output(=|$)|--ext-diff$|--exec(=|$)|--exec-path|--upload-pack|"
                      r"--receive-pack|--git-dir|--work-tree|--pathspec-from-file=(?!-$)|"
                      r"--open-files-in-pager|-O|--orderfile|--no-index$|--config-env|"
                      r"--index-output|--template|--textconv$)")
# per subcommand: options refused there (a commit never amends or skips hooks; a commit
# message never comes from a file; grep patterns never come from a file)
_BAD_FOR = {
    "commit": re.compile(r"^(--amend$|--no-verify$|-n$|--file(=|$)|-F.|--fixup|--squash)"),
    "commit-tree": re.compile(r"^(--file(=|$)|-F.)"),
    "grep": re.compile(r"^(-f|--file(=|$))"),
    "hash-object": re.compile(r"^(-w$|--stdin)"),
    "symbolic-ref": re.compile(r"^(-d$|--delete$)"),
    "update-ref": re.compile(r"^(-d$|--stdin$|--no-deref$|-z$)"),
}
_CHECKOUT_OPTS = frozenset(("-q", "--quiet", "-f", "--force", "--ours", "--theirs",
                            "--no-overlay", "--overlay", "--pathspec-from-file=-",
                            "--pathspec-file-nul"))
_RESET_OPTS = frozenset(("-q", "--quiet", "--pathspec-from-file=-", "--pathspec-file-nul"))
_ENV_DENY = re.compile(r"^(PATH|HOME|SHELL|LD_.*|DYLD_.*|PYTHON.*|SSH_.*|GIT_(DIR|WORK_TREE|"
                       r"INDEX_FILE|EXEC_PATH|SSH.*|CONFIG.*|ASKPASS|PROXY.*|ALTERNATE.*|"
                       r"OBJECT_DIRECTORY|NAMESPACE|CEILING.*|TRACE.*))$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_READ = 8 * 1024 * 1024
MAX_WRITE = 8 * 1024 * 1024
MAX_GIT_OUT = 32 * 1024 * 1024
JOBS = ("worktree-add", "worktree-remove", "verify", "store-init", "lune", "toolchain")
TOOLCHAIN_TIMEOUT_S = 1800
_SUB_RE = {"task": re.compile(r"^[A-Za-z0-9._-]{0,64}$"), "sha": re.compile(r"^[0-9a-f]{0,40}$"),
           "base": re.compile(r"^[0-9a-f]{0,40}$")}
LUNE_CACHE = "~/.cache/au-lune-spec-cache"     # one folder per lane (slots.SLOT_LUNE_CACHE)


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


def _outside(p: str) -> bool:
    q = p.replace("\\", "/")
    return q.startswith("/") or bool(re.match(r"^[A-Za-z]:", q)) or ".." in q.split("/")


def check_git_argv(argv, branch: str = "") -> list:
    """Refuse what the coordinator's Workspace never sends (see the module doc). `branch`:
    the lane's branch, the only ref update-ref may move."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise LaneError("argv must be a non-empty list of strings")
    sub = argv[0]
    if sub not in GIT_ALLOWED:
        raise LaneError(f"git {sub!r} is not allowed on a lane")
    rest = argv[1:]
    cut = rest.index("--") if "--" in rest else len(rest)
    opts, paths = rest[:cut], rest[cut + 1:]
    bad_for = _BAD_FOR.get(sub)
    for i, a in enumerate(opts):
        if _BAD_ARG.match(a) or (bad_for is not None and bad_for.match(a)):
            raise LaneError(f"git {sub} option {a.split('=')[0]!r} is not allowed on a lane")
        if sub in ("commit", "commit-tree") and a == "-F" and (i + 1 >= len(opts)
                                                               or opts[i + 1] != "-"):
            raise LaneError(f"git {sub} -F reads a file: only -F - (stdin) on a lane")
    pos = [a for a in opts if not a.startswith("-")]
    if sub == "checkout":
        # restores paths only: never a branch switch (`checkout <b>`, `checkout <b> --`,
        # -b/-B/--orphan/--detach); at most one tree-ish, then at least one path
        extra = [a for a in opts if a.startswith("-") and a not in _CHECKOUT_OPTS]
        stdin = "--pathspec-from-file=-" in opts
        if extra or len(pos) > 1 or (not paths and not stdin) or ("--" not in rest and not stdin):
            raise LaneError("git checkout on a lane only restores paths "
                            "(checkout [<tree-ish>] -- <path>...)")
    elif sub == "reset":
        # unstages paths only: never moves HEAD (--hard/--soft/--mixed/--merge/--keep, or a
        # commit before the paths)
        extra = [a for a in opts if a.startswith("-") and a not in _RESET_OPTS]
        if extra or pos:
            raise LaneError("git reset on a lane only unstages paths "
                            "(reset -q -- <path>... or --pathspec-from-file=-)")
    elif sub == "update-ref":
        # only the lane's own branch (commits.py rewords a lane's last commits with it)
        args, i = [], 0
        while i < len(rest):
            if rest[i] == "-m" and i + 1 < len(rest):
                i += 2
                continue
            if rest[i].startswith("-m") or rest[i].startswith("--create-reflog"):
                i += 1
                continue
            args.append(rest[i])
            i += 1
        want = f"refs/heads/{branch}" if branch else ""
        if (not want or len(args) not in (2, 3) or args[0] != want
                or any(a.startswith("-") for a in args)):
            raise LaneError(f"git update-ref on a lane moves only its own branch "
                            f"({want or 'unknown'})")
    elif sub == "symbolic-ref" and len(pos) > 1:
        raise LaneError("git symbolic-ref on a lane only reads (it never switches HEAD)")
    elif sub == "hash-object" and any(_outside(p) for p in pos + paths):
        raise LaneError("git hash-object on a lane only reads files inside it")
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
        self._lock = threading.RLock()
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
        row = self.lanes().get(self.key(project, lane)) or {}
        argv = check_git_argv(argv, branch=str(row.get("branch") or ""))
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

    def read(self, project: str, lane: str, path: str, max_bytes=None, offset=0) -> dict:
        """At most MAX_READ bytes from `offset`: a bigger file is read in pieces
        (RemoteWorkspace.read asks for the next offset while `truncated`). `size` is the
        file's whole size, `offset` the one this piece starts at."""
        wt = self.worktree_of(project, lane)
        p = self._inside(wt, path)
        if not os.path.isfile(p):
            return {"exists": False}
        cap = MAX_READ if max_bytes is None else max(0, min(int(max_bytes), MAX_READ))
        start = max(0, int(offset or 0))
        with open(p, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(start)
            data = f.read(cap + 1)
        return {"exists": True, "truncated": len(data) > cap, "size": size, "offset": start,
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

    def write(self, project: str, lane: str, path: str, data: str, delete: bool = False) -> dict:
        wt = self.worktree_of(project, lane)
        p = self._inside(wt, path)
        if delete:                              # the coordinator deleted it in its read mirror
            if os.path.isfile(p):
                os.remove(p)
            return {"ok": True, "deleted": True}
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
        if job == "verify":
            args = self._verify_args(project, lane, args)      # refused before any run starts
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
        # the state FIRST, then the log (v0.181.270 Linux CI: the log was read first, the job
        # ended in between, and "done" came back without its last lines; wait_job stops at the
        # first state that is not "running"). A job writes all of its lines before it sets
        # its state (_job), so a log read after an ended state is complete.
        with self._lock:
            state, rc = run["state"], run["rc"]
        start = max(0, int(offset))
        chunk, more = self._read_log(run["log"], start)
        if state != "running" and more:
            # ended, but more output than one reply: "running" until the caller has it all
            state, rc = "running", None
        return {"id": rid, "job": run["job"], "lane": run["lane"], "state": state,
                "rc": rc, "offset": start + len(chunk),
                "output": chunk.decode("utf-8", "replace")}

    @staticmethod
    def _read_log(path: str, start: int, size: int = 1024 * 1024) -> tuple:
        """(up to `size` bytes of a run's log from `start`, whether more follows)."""
        try:
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read(size + 1)
        except OSError:
            return b"", False
        return chunk[:size], len(chunk) > size

    def cancel(self, rid: str) -> dict:
        with self._lock:
            run = self._runs.get(rid)
        if not run:
            raise LaneError(f"no run {rid}", 404)
        run["cancel"] = True
        _plat.kill_tree(run.get("proc"))          # the job's whole process tree
        return {"ok": True}

    def _other_running(self, k: str, rid: str, job: str = "") -> Optional[dict]:
        """Another running run of lane `k` (of `job`, when given), else None."""
        with self._lock:
            for r in self._runs.values():
                if r["lane"] == k and r["id"] != rid and r["state"] == "running" \
                        and (not job or r["job"] == job):
                    return r
        return None

    def _wait_toolchain(self, k: str, run: dict, say) -> None:
        """Lune and verify jobs wait while the lane's toolchain install runs (it is its own
        job since 2026-11-27, so worktree-add returns before it ends)."""
        t = self._other_running(k, run["id"], "toolchain")
        if t is None:
            return
        say(f"LANE waiting for the toolchain install (run {t['id']})")
        deadline = time.time() + 2 * TOOLCHAIN_TIMEOUT_S + 60
        while t["state"] == "running" and not run["cancel"] and time.time() < deadline:
            time.sleep(0.5)

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
                elif job == "lune":
                    rc = self._lune(project, lane, args, say, run, log)
                elif job == "toolchain":
                    rc = self._toolchain(project, lane, say, run, log)
        except LaneError as e:
            _append(run["log"], f"LANE JOB FAIL: {e}")
        except Exception as e:                                    # noqa: BLE001
            _append(run["log"], f"LANE JOB FAIL: {type(e).__name__}: {e}")
        # every line is written (the log file closed) before the state says the job ended:
        # run_status reads the state first and the log after, so its output is then whole
        with self._lock:
            run["rc"] = rc
            run["ended"] = self.clock()
            run["state"] = "cancelled" if run["cancel"] else ("done" if rc == 0 else "failed")

    def _sub(self, argv: list, run: dict, log, cwd: Optional[str] = None,
             timeout: float = 1800, env: Optional[dict] = None,
             base_env: Optional[dict] = None) -> int:
        """`env`: variables from the coordinator (checked by env_allowed); `base_env`: this
        agent's own environment for the child (its PATH with the toolchain), unchecked."""
        if run["cancel"]:
            return 130
        extra = {k: str(v) for k, v in (env or {}).items() if env_allowed(k)}
        base = base_env if base_env is not None else os.environ
        try:
            # its own process group, so a cancel or a timeout ends everything it started
            # (a check's test runner, rokit's downloads), not only this child
            p = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL,
                                 env={**base, **extra} if (extra or base_env is not None) else None,
                                 **_plat.group_kw())
        except FileNotFoundError as e:
            log.write(f"LANE command not found: {e}\n".encode("utf-8"))
            log.flush()
            return 127                       # as a shell says it (verify: UNKNOWN, never PASS)
        run["proc"] = p
        if run["cancel"]:                    # cancelled while it started
            _plat.kill_tree(p)
        try:
            return p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _plat.kill_tree(p)
            return 124
        finally:
            run["proc"] = None

    @staticmethod
    def _tool_path() -> str:
        """PATH with the game toolchain (rokit's bin: lune, wally, selene, stylua)."""
        return os.pathsep.join([os.path.expanduser("~/.rokit/bin"), os.environ.get("PATH", "")])

    def _toolchain(self, project, lane, say, run, log) -> int:
        """The `toolchain` job, started by worktree-add when the worktree is made (its own job,
        so a long rokit download never holds up the coordinator's create wait): the worktree's
        rokit tools and wally packages (Packages/ is not committed; Lune and Rojo need it), as
        entrypoint.sh does for the Studio checkout. Best effort: a failure is logged and the
        job still ends 0 (the lane works for git; a Lune row then says lune is missing)."""
        wt = self.worktree_of(project, lane)
        if not os.path.isfile(os.path.join(wt, "rokit.toml")):
            say("LANE toolchain: no rokit.toml (nothing to install)")
            return 0
        env = {**os.environ, "PATH": self._tool_path()}
        for argv in (["rokit", "install", "--no-trust-check"], ["wally", "install"]):
            if run["cancel"]:
                return 130
            exe = shutil.which(argv[0], path=env["PATH"])
            if not exe:
                say(f"LANE toolchain: {argv[0]} not found (skipped)")
                continue
            if argv[0] == "wally" and (os.path.isdir(os.path.join(wt, "Packages"))
                                       or not os.path.isfile(os.path.join(wt, "wally.toml"))):
                continue
            rc = self._sub([exe, *argv[1:]], run, log, cwd=wt, timeout=TOOLCHAIN_TIMEOUT_S,
                           base_env=env)
            say(f"LANE toolchain: {' '.join(argv)} rc={rc}")
        return 130 if run["cancel"] else 0

    def _lune(self, project, lane, args, say, run, log) -> int:
        """`lune run tests/lune/run.luau <args>` in the lane worktree (the AU adapter's Lune
        rows, phase c): the lane's own warm spec cache, AU_LUNE_WORKERS as asked."""
        wt = self.worktree_of(project, lane)
        self._wait_toolchain(self.key(project, lane), run, say)
        extra = [str(a) for a in (args.get("args") or []) if isinstance(a, (str, int))]
        path = self._tool_path()
        exe = shutil.which("lune", path=path)
        if not exe:
            say("LANE command not found: lune (rokit install)")
            return 127
        # one cache per project AND lane ("_lanes" never clashes with a lane name the Studio
        # gate's per-lane folders use: lane names start with a letter or digit)
        cache = os.path.join(os.path.expanduser(LUNE_CACHE), "_lanes", project, lane)
        os.makedirs(cache, exist_ok=True)
        env = {"AU_LUNE_CACHE_DIR": cache}
        if args.get("workers"):
            env["AU_LUNE_WORKERS"] = str(int(args["workers"]))
        t = max(1.0, min(float(args.get("timeout") or 3600), 4 * 3600.0))
        say(f"LANE lune {k_of(project, lane)}: {' '.join(extra) or '(suite)'}")
        return self._sub([exe, "run", "tests/lune/run.luau", *extra], run, log, cwd=wt,
                         timeout=t, env=env, base_env={**os.environ, "PATH": path})

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
        base = str(args.get("base") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", base):
            raise LaneError("worktree-add needs the base commit (40 hex)")
        branch = str(args.get("branch") or f"track/{lane}")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or ".." in branch:
            raise LaneError(f"bad branch name {branch!r}")
        checks = _checks_of(args.get("checks"))
        with self._lock:
            have = self.lanes()
            old = have.get(k)
            # an add that never finished (the agent restarted, or the coordinator cancelled
            # it after its wait) may be made again: it never held a session's work
            stale = bool(old) and old.get("state") == "adding" \
                and self._other_running(k, run["id"], "worktree-add") is None
            if old and not stale:
                raise LaneError(f"this slot already holds lane {k} (one writer per lane)", 409)
            have[k] = {"project": project, "lane": lane, "state": "adding",
                       "worktree": os.path.join(self.root, project, lane),
                       "branch": branch, "base": base, "checks": checks,
                       "added": self.clock()}
            self._save(have)
        row = have[k]
        if stale:
            say(f"LANE worktree-add {k}: an earlier add did not finish; cleaning it up first")
            self._undo_add(store, old, run, log, say)
        os.makedirs(os.path.dirname(row["worktree"]), exist_ok=True)
        # what was there before this add is never undone (another lane's folder or branch)
        had_dir = os.path.exists(row["worktree"])
        had_branch = self._sub(["git", "-C", store, "rev-parse", "--verify", "-q",
                                f"refs/heads/{branch}"], {**run, "cancel": False}, log,
                               timeout=60) == 0
        say(f"LANE worktree-add {k}: {row['branch']} at {base[:12]} -> {row['worktree']}")
        rc = self._sub(["git", "-C", store, "worktree", "add", "-b", row["branch"],
                        row["worktree"], base], run, log)
        if rc != 0:
            # leave nothing half made, so the same lane can be made again
            if not had_dir and not had_branch:
                self._undo_add(store, row, run, log, say)
            self._forget(k)
            say(f"LANE worktree-add FAIL rc={rc}")
            return rc
        with self._lock:
            have = self.lanes()
            if k in have:
                have[k]["state"] = "held"
                self._save(have)
        # the toolchain install is its own job: Lune and verify jobs of this lane wait for it
        tc = self.start_job(project, lane, "toolchain")["run_id"]
        say(f"LANE toolchain: started as run {tc}")
        say(f"LANE worktree-add OK {k}")
        return 0

    def _undo_add(self, store: str, row: dict, run: dict, log, say) -> None:
        """A worktree-add that failed or never finished: its half-made worktree and its new
        branch go (the branch only while it is still at the base: no commit is ever lost)."""
        raw = str(row.get("worktree") or "")
        wt = os.path.realpath(raw) if raw else ""
        inside = bool(wt) and (wt + os.sep).startswith(self.root + os.sep) and wt != self.root
        # `run` may be cancelled already: these must run anyway
        clean = {**run, "cancel": False}
        if inside and os.path.exists(wt):
            self._sub(["git", "-C", store, "worktree", "remove", "--force", wt], clean, log,
                      timeout=300)
        self._sub(["git", "-C", store, "worktree", "prune"], clean, log, timeout=300)
        if inside and os.path.isdir(wt):
            shutil.rmtree(wt, ignore_errors=True)
        branch, base = str(row.get("branch") or ""), str(row.get("base") or "")
        if branch and re.fullmatch(r"[0-9a-f]{40}", base):
            r = subprocess.run(["git", "-C", store, "rev-parse", "--verify", "-q",
                                f"refs/heads/{branch}"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                               timeout=60, **_plat.no_window_kw())
            if r.returncode == 0 and r.stdout.strip() == base:
                self._sub(["git", "-C", store, "update-ref", "-d", f"refs/heads/{branch}", base],
                          clean, log, timeout=60)
                say(f"LANE worktree-add: removed the unfinished {branch}")

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
        dirty = self.git(project, lane, ["status", "--porcelain=v1", "-z",
                                         "--untracked-files=all"])
        if dirty["rc"] != 0:
            raise LaneError(f"lane {k}: git status failed: {dirty['stderr'][-200:]}", 409)
        # -z: names exactly as stored (quoted or non-ASCII names, renames' both sides)
        rows = [p for _xy, path, orig in parse_status_z(dirty["stdout"])
                for p in (path, orig) if p]
        # `saved`: the paths the coordinator copied to its machine (bring-home saves every
        # changed file first, and names the deleted ones); only exactly those may go with the
        # worktree, nothing else
        saved = {str(p).replace("\\", "/") for p in (args.get("saved") or [])}
        left = [p for p in rows if p.replace("\\", "/").rstrip("/") not in saved]
        if left:
            raise LaneError(f"lane {k} has uncommitted files not saved on the coordinator's "
                            f"machine: {', '.join(left[:5])}", 409)
        force = ["--force"] if rows else []
        row = self.lanes().get(k) or {}
        rc = self._sub(["git", "-C", self.store(project), "worktree", "remove", *force, wt],
                       run, log)
        if rc == 0:
            self._forget(k)
            say(f"LANE worktree-remove OK {k}")
            # `drop_branch_at`: a lane the coordinator could not take up goes with its branch
            # (only while the branch is still at that commit), so the name can be used again
            drop = str(args.get("drop_branch_at") or "")
            if re.fullmatch(r"[0-9a-f]{40}", drop) and drop == tip and row.get("branch"):
                self._undo_add(self.store(project), {**row, "worktree": "", "base": drop},
                               run, log, say)
                # the junction's base ref for it too (update-ref -d with the old value: only
                # while it still points there)
                self._sub(["git", "-C", self.store(project), "update-ref", "-d",
                           f"refs/heads/slot/_base/{lane}", drop], {**run, "cancel": False}, log,
                          timeout=60)
        return rc

    def _verify_args(self, project: str, lane: str, args: dict) -> dict:
        """A verify job names a check registered for the lane at worktree-add and sends the
        same argv template; anything else is refused (the token is not a shell)."""
        k = self.key(project, lane)
        row = self.lanes().get(k)
        if not row:
            raise LaneError(f"this slot holds no lane {k}", 404)
        name = str(args.get("check") or "")
        checks = row.get("checks") or {}
        if name not in checks:
            raise LaneError(f"check {name!r} is not registered for lane {k} (a lane's checks "
                            f"are the project's [[verify.checks]] when the lane was made)", 409)
        if args.get("command") != checks[name]:
            raise LaneError(f"check {name!r} is not the command registered for lane {k} (the "
                            f"project's check changed since the lane was made: make the lane "
                            f"again to run the new one)", 409)
        subs = args.get("subs") if isinstance(args.get("subs"), dict) else {}
        for key, rx in _SUB_RE.items():
            if not rx.match(str(subs.get(key) or "")):
                raise LaneError(f"verify: bad {key} {str(subs.get(key))[:60]!r}")
        return {**args, "subs": {key: str(subs.get(key) or "") for key in _SUB_RE}}

    def _verify(self, project, lane, args, say, run, log) -> int:
        """A registered [[verify.checks]] command in the lane, its placeholders filled here as
        verify_cmd.run_check fills them on the coordinator's machine ({python}: this machine's
        python: "python" on Windows, "python3" elsewhere)."""
        wt = self.worktree_of(project, lane)
        self._wait_toolchain(self.key(project, lane), run, say)
        subs = {**args["subs"], "workspace": wt.replace("\\", "/") if _plat.is_windows() else wt,
                "python": "python" if _plat.is_windows() else "python3"}
        try:
            cmd = [str(c).format(**subs) for c in args["command"]]
        except (KeyError, IndexError, ValueError) as e:
            raise LaneError(f"check {args.get('check')!r}: bad placeholder {e}")
        t = max(1.0, min(float(args.get("timeout") or 1800), 4 * 3600.0))
        say(f"LANE verify {k_of(project, lane)} {args.get('check')}: {' '.join(cmd)[:300]}")
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


def _checks_of(raw) -> dict:
    """worktree-add's `checks`: {name: [argv template]} (the project's [[verify.checks]])."""
    if raw in (None, {}):
        return {}
    ok = isinstance(raw, dict) and len(raw) <= 200 and all(
        isinstance(n, str) and 0 < len(n) <= 200 and isinstance(c, list) and 0 < len(c) <= 500
        and all(isinstance(x, str) and len(x) <= 20000 for x in c) for n, c in raw.items())
    if not ok:
        raise LaneError("worktree-add: checks must be {name: [argv template, ...]}")
    return {n: list(c) for n, c in raw.items()}


def parse_status_z(out: str) -> list:
    """[(XY, path, orig)] from `git status --porcelain=v1 -z`: names exactly as stored (no
    quoting, any characters). `orig` is a rename's or copy's old path ("" otherwise): git
    writes it as the next NUL-separated field."""
    toks = (out or "").split("\0")
    res, i = [], 0
    while i < len(toks):
        tok = toks[i]
        i += 1
        if len(tok) < 4:
            continue
        xy, path, orig = tok[:2], tok[3:], ""
        if xy[0] in "RC" or xy[1] in "RC":
            orig = toks[i] if i < len(toks) else ""
            i += 1
        res.append((xy, path, orig))
    return res


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
           "ensure_secrets", "NAME_RE", "PROJECT_RE", "JOBS", "parse_status_z"]

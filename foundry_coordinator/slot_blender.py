"""foundry-blender: run Blender on a Studio slot (a mini PC) instead of this PC
(docs/plans/SLOT_BLENDER_PLAN.md B1).

    python -m foundry_coordinator.slots blender [--config .foundry/blender.json | --host H
        --container C --au-push-url U] [--repo DIR] [--threads N] [--wait 600]
        -- <blender args>

Run from a sprint's worktree, with the same arguments as a local `blender` (repo-relative paths):
  1. the worktree's commit is pushed to the slot host's game mirror as `blender/<worktree>` (kept, so
     the next push sends only what is new);
  2. ONE `docker exec` on the slot holds its Blender lock (`flock`, one Blender job per slot; a
     busy slot is waited for up to --wait seconds, then exit 75 with the reason) and
       - checks the commit out in its own checkout `~/work/blender` (never Studio's checkout),
       - unpacks the worktree's uncommitted changes and new files, sent on its stdin as a tar
         (deleted files are deleted there too), so a sprint never has to commit first,
       - runs `blender -t <threads> <args>`; its output streams back live on stderr,
       - sends back, as a tar on stdout, every file the run created or changed (newer than a
         marker made just before Blender started; never .git or __pycache__);
  3. those files are written into the same paths in the worktree, and listed.
The exit code is Blender's. A slot that cannot be reached fails at once; nothing ever falls back
to a local Blender (outputs from two machines could differ).
"""

from __future__ import annotations

import collections
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import threading
import time
from typing import Optional

BUSY_EXIT = 75                       # the slot's Blender stayed busy for --wait seconds
# 2026-11-07: two VR-AS29 Blender jobs hung on slot-1 for 7 h 42 min at 0% CPU and held both
# pool locks; every other track's Blender run waited 600 s and exited 75. A job is now ended
# after [blender] max_job_seconds (0 = no limit), which frees its lock.
MAX_JOB_SECONDS = 3600
TIMEOUT_EXIT = 124                   # GNU timeout: the job ran past max_job_seconds
SEND_CAP = 512 * 1024 * 1024         # uncommitted changes sent at most (bytes)
BACK_CAP = 4 * 1024 * 1024 * 1024    # outputs taken back at most (bytes)
CONFIG = os.path.join(".foundry", "blender.json")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_DELETE_LIST = ".foundry-blender-delete"


def log(msg: str) -> None:
    print(f"foundry-blender: {msg}", file=sys.stderr, flush=True)


def _git(repo: str, *args, timeout: float = 120, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo] + list(args), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                          creationflags=_NO_WINDOW, env=env)


def _z(out: bytes) -> list:
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def local_changes(repo: str) -> tuple[list, list]:
    """(changed or new files, deleted files) of the worktree against HEAD, repo-relative."""
    changed = _z(_git(repo, "diff", "--name-only", "--diff-filter=ACMRT", "-z", "HEAD").stdout)
    new = _z(_git(repo, "ls-files", "-o", "--exclude-standard", "-z").stdout)
    deleted = _z(_git(repo, "diff", "--name-only", "--diff-filter=D", "-z", "HEAD").stdout)
    seen, files = set(), []
    for p in changed + new:
        if p not in seen and os.path.isfile(os.path.join(repo, p)):
            seen.add(p)
            files.append(p)
    return files, deleted


# ----------------------------------------------------------------------------- B2b: library

LIB_DIR = "$HOME/assetlib"                 # in the slot container's persistent home volume
_LIB_MANIFEST, _LIB_HASH, _LIB_DELETE = ".foundry-manifest.json", ".foundry-manifest.sha", \
    ".foundry-delete"
# AU's tools that hard-code the library: on Linux these are paths relative to the checkout,
# where foundry-blender puts links to the slot's copy (SLOT_BLENDER_PLAN B2b)
LIB_SHIMS = ("C:/Roblox/AssetLibrary", "C:\\Roblox\\AssetLibrary")


def lib_manifest(root: str) -> dict:
    """{posix relpath: [size, whole-second mtime]} of every file under the library (hidden
    names skipped)."""
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in files:
            if f.startswith("."):
                continue
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[os.path.relpath(p, root).replace("\\", "/")] = [st.st_size, int(st.st_mtime)]
    return out


def lib_hash(manifest: dict) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def lib_diff(local: dict, remote: dict) -> tuple[list, list]:
    """(files to send, files to delete on the slot)."""
    send = sorted(p for p, v in local.items() if remote.get(p) != v)
    gone = sorted(p for p in remote if p not in local)
    return send, gone


def _slot_out(slot, script: str, timeout: float = 120) -> tuple[int, bytes]:
    from . import slots
    r = subprocess.run(slots.exec_argv(slot, ["bash", "-c", script]), stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                       creationflags=_NO_WINDOW)
    return r.returncode, r.stdout


def lib_sync(slot: dict, root: str, wait: int = 600) -> tuple[bool, str]:
    """Bring the slot's copy of the library up to date: only new or changed files are sent (a
    tar on stdin), files gone here are removed there, and the slot's manifest is written last
    (an interrupted sync is simply redone). Holds the slot's library lock exclusive while it
    writes, so it waits for the slot's running Blender jobs (they hold it shared)."""
    from . import slots
    if not root or not os.path.isdir(root):
        return True, f"no asset library at {root or '(none set)'}: skipped"
    local = lib_manifest(root)
    h = lib_hash(local)
    rc, out = _slot_out(slot, f'cat "{LIB_DIR}/{_LIB_HASH}" 2>/dev/null || true')
    if rc != 0:
        return False, "the slot did not answer"
    if out.decode("utf-8", "replace").strip() == h:
        return True, f"asset library up to date ({len(local)} files)"
    rc, out = _slot_out(slot, f'cat "{LIB_DIR}/{_LIB_MANIFEST}" 2>/dev/null || echo "{{}}"', 300)
    try:
        remote = json.loads(out.decode("utf-8", "replace") or "{}")
        remote = remote if isinstance(remote, dict) else {}
    except ValueError:
        remote = {}
    send, gone = lib_diff(local, remote)
    size = sum(local[p][0] for p in send)
    log(f"asset library: sending {len(send)} file(s), {size / 1048576:.0f} MB; removing "
        f"{len(gone)} (first copy is the whole library)")
    script = f"""set -u
mkdir -p "$HOME/locks" "{LIB_DIR}"
exec 9>"$HOME/locks/library.lock"
flock -x -w {int(wait)} 9 || {{ echo "foundry-blender: the slot stayed busy" >&2; exit {BUSY_EXIT}; }}
tar -xf - -C "{LIB_DIR}" || exit 70
if [ -f "{LIB_DIR}/{_LIB_DELETE}" ]; then
    (cd "{LIB_DIR}" && xargs -0 -r rm -f -- < "{_LIB_DELETE}"); rm -f "{LIB_DIR}/{_LIB_DELETE}"
fi
"""
    p = subprocess.Popen(slots.exec_argv(slot, ["bash", "-c", script], interactive=True),
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=None,
                         creationflags=_NO_WINDOW)
    try:
        with tarfile.open(fileobj=p.stdin, mode="w|") as tf:
            for rel in send:
                tf.add(os.path.join(root, *rel.split("/")), arcname=rel, recursive=False)
            for name, data in ((_LIB_DELETE, "\0".join(gone).encode()),
                               (_LIB_MANIFEST, json.dumps(local).encode()),
                               (_LIB_HASH, h.encode())):           # the manifest goes last
                if name == _LIB_DELETE and not gone:
                    continue
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        p.stdin.close()
    except (BrokenPipeError, OSError) as e:
        p.kill()
        p.wait()
        return False, f"the library copy broke off ({e})"
    rc = p.wait()
    if rc != 0:
        return False, f"the library copy failed (exit {rc})"
    return True, f"asset library synced: {len(send)} sent, {len(gone)} removed"


def remote_script(run_id: str, wait: int, threads: int, blender_args: list,
                  library: bool = False, job: int = 1, jobs: int = 1,
                  max_seconds: int = MAX_JOB_SECONDS) -> str:
    """The one script the slot runs (bash, in the container). `run_id` is [A-Za-z0-9._-].
    BLENDER_POOL_PLAN P1: job N of a slot's `jobs` has its own lock, checkout and marker, and
    Blender gets the slot's CPU divided by the jobs; every job holds the library lock shared
    (lib_sync takes it exclusive while it writes the library)."""
    job, jobs = max(1, int(job)), max(1, int(jobs))
    job = min(job, jobs)
    # the preferred job first, then any other free one (two runs that probed the slot at the
    # same moment take two jobs instead of queueing on one); all busy: wait on the preferred
    order = " ".join(str(n) for n in [job] + [n for n in range(1, jobs + 1) if n != job])
    args = list(blender_args)
    if not any(a in ("-t", "--threads") for a in args):
        args = ["-t", "$T"] + args
    quoted = " ".join('"$T"' if a == "$T" else shlex.quote(a) for a in args)
    return f"""set -u
mkdir -p "$HOME/locks"
lockf() {{ if [ "$1" = 1 ]; then echo "$HOME/locks/blender.lock"; else echo "$HOME/locks/blender-$1.lock"; fi; }}
J=""
for n in {order}; do
    exec 9>"$(lockf "$n")"
    if flock -n 9; then J=$n; break; fi
    exec 9>&-
done
if [ -z "$J" ]; then
    J={job}
    exec 9>"$(lockf "$J")"
    if ! flock -w {int(wait)} 9; then
        echo "foundry-blender: this slot's Blender stayed busy for {int(wait)} s (another job)" >&2
        exit {BUSY_EXIT}
    fi
fi
exec 8>"$HOME/locks/library.lock"
if ! flock -s -w {int(wait)} 8; then
    echo "foundry-blender: the slot's asset library stayed busy for {int(wait)} s" >&2
    exit {BUSY_EXIT}
fi
SFX=""; [ "$J" = 1 ] || SFX="-$J"
W="$HOME/work/blender$SFX"
if [ ! -d "$W/.git" ]; then git clone -q /work/AnimalUprising "$W" >&2 || exit 70; fi
git -C "$W" fetch -q origin "refs/heads/blender/{run_id}" >&2 || exit 70
git -C "$W" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD >&2 || exit 70
git -C "$W" clean -q -fd >&2
tar -xf - -C "$W" || exit 70
if [ -f "$W/{_DELETE_LIST}" ]; then
    (cd "$W" && xargs -0 -r rm -f -- < "{_DELETE_LIST}"); rm -f "$W/{_DELETE_LIST}"
fi
T={int(threads)}
if [ "$T" -le 0 ]; then
    T=$(awk '{{if ($1 == "max") print 0; else print int($1 / $2)}}' /sys/fs/cgroup/cpu.max 2>/dev/null)
    [ -n "$T" ] && [ "$T" -gt 0 ] || T=$(nproc)
    T=$(( T / {jobs} )); [ "$T" -gt 0 ] || T=1
fi
{_lib_shim_script() if library else ""}M="$HOME/locks/blender$SFX.marker"
touch "$M"; sleep 1
cd "$W"
echo "foundry-blender: running on $(hostname) job $J of {jobs} with $T threads: blender {quoted}" >&2
{f"timeout --kill-after=30 {int(max_seconds)} " if int(max_seconds or 0) > 0 else ""}blender {quoted} 1>&2
rc=$?
if [ "$rc" = {TIMEOUT_EXIT} ] || [ "$rc" = 137 ]; then
    echo "foundry-blender: blender ran past {int(max_seconds or 0)} s and was stopped (the job's lock is free again)" >&2
fi
echo "foundry-blender: blender exit $rc" >&2
find . \\( -path ./.git -o -name __pycache__ \\) -prune -o -type f -newer "$M" -print0 \\
    | tar --null -T - -cf -
exit $rc
"""


def _lib_shim_script() -> str:
    """bash lines (in the slot, before Blender): AU_ASSET_LIBRARY, and the hard-coded library
    paths of AU's tools linked to the slot's copy inside the checkout (made after `git clean`,
    links only: never copied back, never committed)."""
    lines = [f'export AU_ASSET_LIBRARY="{LIB_DIR}"', 'mkdir -p "$W/C:/Roblox"']
    for shim in LIB_SHIMS:
        lines.append(f'ln -sfn "{LIB_DIR}" "$W/{shim}"')
    return "\n".join(lines) + "\n"


def _send_tar(stream, repo: str, files: list, deleted: list) -> None:
    """Write the uncommitted changes as a tar to `stream` (the slot's stdin), then close it."""
    total = 0
    try:
        with tarfile.open(fileobj=stream, mode="w|") as tf:
            for p in files:
                full = os.path.join(repo, p)
                total += os.path.getsize(full)
                if total > SEND_CAP:
                    raise RuntimeError(f"uncommitted changes are over {SEND_CAP >> 20} MB")
                tf.add(full, arcname=p.replace("\\", "/"), recursive=False)
            if deleted:
                data = "\0".join(deleted).encode("utf-8", "surrogateescape")
                ti = tarfile.TarInfo(_DELETE_LIST)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def receive(stream, repo: str) -> list:
    """Write the slot's output tar into the worktree (repo-relative paths only); the list."""
    root = os.path.realpath(repo)
    out, total = [], 0
    try:
        tf = tarfile.open(fileobj=stream, mode="r|")
    except tarfile.ReadError:
        return out                                   # no outputs (or the run never started)
    with tf:
        for m in tf:
            name = m.name.replace("\\", "/")
            while name.startswith("./"):
                name = name[2:]
            if not m.isfile() or not name or name.startswith("/") or ".." in name.split("/") \
                    or name.split("/")[0] == ".git":
                continue
            dest = os.path.realpath(os.path.join(root, *name.split("/")))
            if not dest.startswith(root + os.sep):
                continue
            total += m.size
            if total > BACK_CAP:
                log(f"outputs are over {BACK_CAP >> 30} GB: the rest stays on the slot")
                break
            src = tf.extractfile(m)
            if src is None:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            out.append(name)
    return out


def load_config(repo: str, path: str = "") -> dict:
    p = path or os.path.join(repo, CONFIG)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def run(a, blender_args: list) -> int:
    from . import slots
    repo = os.path.abspath(a.repo or ".")
    top = _git(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        log(f"{repo} is not a git checkout")
        return 2
    repo = os.path.abspath(top.stdout.decode().strip())
    blender_args, why = relative_args(repo, blender_args)
    if why:
        log(why)
        return 2
    cfg = load_config(repo, a.config)
    if cfg.get("pool"):
        # BLENDER_POOL_PLAN P1: the run goes to a free worker of the pool
        cfg_path = a.config or os.path.join(repo, CONFIG)
        wait = int(cfg.get("pool_wait_seconds") or a.wait)
        got = choose_worker(cfg, cfg_path, wait)
        if got is None:
            log(f"every Blender worker of the pool stayed busy for {wait} s")
            return BUSY_EXIT
        kind, w, job = got
        try:
            if kind == "local":
                return _run_local(a, cfg, repo, blender_args, w, job)
            slot = {**slots.DEFAULTS, **{k: v for k, v in w.items() if k in slots.DEFAULTS}}
            return _run_slot(a, {**cfg, "slot": w["id"]}, repo, blender_args, slot, job[0],
                             int(w.get("jobs") or 1))
        finally:
            try:
                job[1].close()                       # the job's reservation ends with the run
            except (AttributeError, OSError):
                pass
    slot = {**slots.DEFAULTS, **{k: v for k, v in cfg.items() if k in slots.DEFAULTS}}
    for k in ("host", "container", "au_push_url"):
        if getattr(a, k, ""):
            slot[k] = getattr(a, k)
    if a.ssh:
        slot["ssh"] = shlex.split(a.ssh)
    return _run_slot(a, cfg, repo, blender_args, slot, 1, 1)


# ----------------------------------------------------------------------------- the pool

LOCAL = "local"                        # the pool's name for this PC
LOCAL_EXE = "C:/Program Files/Blender Foundation/Blender 4.5/blender.exe"


def default_local_exe() -> str:
    """This PC's Blender when blender.local_exe is empty: the Windows install path, or on Linux
    (LINUX_WORKSTATION_PLAN W3) `blender` on the PATH, else /opt/blender/blender (the
    workstation installer's and the slot image's place)."""
    if os.name == "nt":
        return LOCAL_EXE
    import shutil
    return shutil.which("blender") or "/opt/blender/blender"
POOL_POLL_S = 5


def job_lock(job: int) -> str:
    """A slot job's lock file name (job 1 keeps the pre-pool name)."""
    return "blender.lock" if job <= 1 else f"blender-{job}.lock"


def free_job_script(jobs: int) -> str:
    """bash: print the first free job number of a slot (0 = all busy). The lock is only tried,
    never kept: the run takes it again (flock -w), so a lost race only waits."""
    return (f'mkdir -p "$HOME/locks"\nfor n in $(seq 1 {max(1, int(jobs))}); do\n'
            '  if [ "$n" = 1 ]; then f="$HOME/locks/blender.lock"; '
            'else f="$HOME/locks/blender-$n.lock"; fi\n'
            '  if flock -n "$f" true; then echo "$n"; exit 0; fi\ndone\necho 0\n')


def slot_free_job(w: dict) -> int:
    from . import slots
    slot = {**slots.DEFAULTS, **{k: v for k, v in w.items() if k in slots.DEFAULTS}}
    try:
        rc, out = _slot_out(slot, free_job_script(int(w.get("jobs") or 1)), timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 0
    try:
        return int(out.decode("utf-8", "replace").strip().splitlines()[-1]) if rc == 0 else 0
    except (ValueError, IndexError):
        return 0


def local_lock_dir() -> str:
    return os.path.join(wrapper_dir(), "locks")


def local_try(jobs: int, worker: str = LOCAL):
    """(job number, open lock file) for a free job of `worker`, or None. Every foundry-blender
    run starts on this PC, so a job is reserved here, race-free, the moment it is chosen
    (<wrapper_dir>/locks/<worker>-<n>.lock, locked while the file is open: the run's lifetime);
    a slot's own flock stays as a second guard. 2026-11-01 proof: probing the slot's lock let
    six runs started together all pick slot-1, which took its lock only after the push."""
    os.makedirs(local_lock_dir(), exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", worker) or "w"
    for n in range(1, max(1, int(jobs)) + 1):
        f = open(os.path.join(local_lock_dir(), f"{safe}-{n}.lock"), "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return n, f
        except OSError:
            f.close()
    return None


def _last_path(cfg_path: str) -> str:
    return cfg_path + ".last"


def choose_worker(cfg: dict, cfg_path: str, wait: int, clock=time.time, sleep=time.sleep,
                  slot_probe=None, local_probe=None):
    """D-P4: the worker this worktree used last when it is free, else the pool's order (slots
    first, this PC last), else wait for the first to free up. Returns (kind, worker, job) with
    job = (job number, the reservation's open lock file); None after `wait` seconds."""
    slot_probe = slot_probe or (lambda w: local_try(int(w.get("jobs") or 1), w["id"]))
    local_probe = local_probe or local_try
    pool = [w for w in cfg.get("pool") or [] if isinstance(w, dict) and w.get("id")]
    try:
        with open(_last_path(cfg_path), encoding="utf-8") as f:
            last = f.read().strip()
    except OSError:
        last = ""
    order = sorted(pool, key=lambda w: 0 if w["id"] == last else 1)
    t_end, noted = clock() + max(0, int(wait)), False
    while True:
        for w in order:
            if w["id"] == LOCAL:
                got = local_probe(int(w.get("jobs") or 1))
                if got:
                    _remember(cfg_path, w["id"])
                    log(f"pool: this PC (job {got[0]})")
                    return "local", w, got
            else:
                got = slot_probe(w)
                if got:
                    _remember(cfg_path, w["id"])
                    log(f"pool: {w['id']} (job {got[0]} of {w.get('jobs') or 1})")
                    return "slot", w, got
        if clock() >= t_end:
            return None
        if not noted:
            log("pool: every Blender worker is busy; waiting for the first to free up")
            noted = True
        sleep(POOL_POLL_S)


def _remember(cfg_path: str, wid: str) -> None:
    try:
        _write(_last_path(cfg_path), wid)
    except OSError:
        pass


def _run_local(a, cfg: dict, repo: str, args: list, w: dict, job) -> int:
    """This PC as a pool worker: the local Blender, in the worktree itself (the outputs are
    already where they belong), with the same journal as a slot run."""
    exe = str(w.get("exe") or cfg.get("local_exe") or default_local_exe())
    if not os.path.isfile(exe):
        log(f"this PC's Blender is not at {exe} (setting blender.local_exe)")
        return 2
    journal = bool(cfg.get("journal"))
    t0 = time.time()
    marks: dict = {"pushed": t0, "library": t0}
    log(f"running on this PC (job {job[0]}): blender {' '.join(args)}")
    marker = time.time()
    p = subprocess.Popen([exe] + list(args), cwd=repo, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         creationflags=_NO_WINDOW)
    tail, keys = collections.deque(maxlen=400), []
    marks["blender_start"] = time.time()
    _tee(p.stdout, tail, keys, marks)
    rc = p.wait()
    marks["blender_end"] = time.time()
    back = _written_since(repo, args, marker) if journal else []
    if journal:
        head = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        _journal({**cfg, "slot": ""}, repo, args, rc, t0, marks, list(tail), keys, back,
                 {"machine": "this PC", "worker": LOCAL, "job": job[0], "commit": head,
                  "worktree": repo})
    log(f"Blender exit {rc} after {time.time() - t0:.0f} s on this PC")
    return rc


def _written_since(repo: str, args: list, since: float) -> list:
    """Repo-relative files written during a local run: in the folders the arguments name
    (the journal's render pick needs them; the outputs themselves stay where they are)."""
    from .build_journal import renders_in_window
    return renders_in_window(repo, args, "", since, time.time())


def _run_slot(a, cfg: dict, repo: str, blender_args: list, slot: dict, job: int,
              jobs: int) -> int:
    from . import slots
    host = str(slot.get("host") or "").strip()
    if not (host and slot.get("container")):
        log("no slot to run on: give --host and --container, or a "
            f"{CONFIG} (Foundry writes it when Blender runs on a slot)")
        return 2
    url = slot.get("au_push_url") or f"ssh://{host}{slots.HOST_ROOT}/AnimalUprising.git"
    # one branch per worktree, overwritten each run and kept: a push then sends only what is new.
    # (Deleting it left the mirror without branches, and every push sent all 2.5 GB again.)
    run_id = a.id or os.path.basename(repo)
    run_id = re.sub(r"[^A-Za-z0-9._-]", "-", run_id)[:64].strip("-.") or "run"
    if not _ID_RE.fullmatch(run_id):
        log(f"bad run id {run_id!r}")
        return 2
    head = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    files, deleted = local_changes(repo)
    t0 = time.time()
    journal = bool(cfg.get("journal"))              # build replay R1 (BLENDER_BUILD_REPLAY_PLAN)
    marks: dict = {}
    log(f"{cfg.get('slot') or host}: {head[:12]} + {len(files)} changed, {len(deleted)} deleted "
        f"file(s) -> the slot")
    for attempt in range(3):
        r = _git(repo, "push", "-q", "--force", url, f"{head}:refs/heads/blender/{run_id}",
                 timeout=300, env=slots.git_ssh_env(slot))
        if r.returncode == 0:
            break
        # builds started together from one worktree push the same branch at once; the loser
        # of git's ref lock simply tries again (the branch is already at this commit)
        time.sleep(1 + attempt * 2)
    if r.returncode != 0:
        log("cannot reach the slot's game mirror: "
            + (r.stderr or r.stdout).decode("utf-8", "replace").strip()[-300:])
        return 3
    marks["pushed"] = time.time()
    # B2b: the game's asset library, copied into the slot (only what changed) before the run
    library = "" if a.no_library else (a.library or str(cfg.get("library") or ""))
    if library and os.path.isdir(library):
        ok, msg = lib_sync(slot, library, a.wait)
        log(msg)
        if not ok:
            return 3
    elif library:
        log(f"no asset library at {library} on this PC: running without it")
        library = ""
    marks["library"] = time.time()
    script = remote_script(run_id, a.wait, a.threads, blender_args, library=bool(library),
                           job=job, jobs=jobs,
                           max_seconds=int(cfg.get("max_job_seconds", MAX_JOB_SECONDS) or 0))
    p = subprocess.Popen(slots.exec_argv(slot, ["bash", "-c", script], interactive=True),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE if journal else None,
                         creationflags=_NO_WINDOW)
    tail, keys = collections.deque(maxlen=400), []
    teer = None
    if journal:
        # Blender's output still streams live; a copy is kept for the journal's log tail
        teer = threading.Thread(target=_tee, args=(p.stderr, tail, keys, marks), daemon=True)
        teer.start()
    sender = threading.Thread(target=_send_tar, args=(p.stdin, repo, files, deleted),
                              daemon=True)
    sender.start()
    back = receive(p.stdout, repo)
    # tar pads its output past the end-of-archive blocks: read it to the end, or ssh waits for
    # someone to take those bytes and never exits (2026-11-01, the first proof on slot-1 hung)
    try:
        while p.stdout.read(1 << 16):
            pass
    except (OSError, ValueError):
        pass
    try:
        p.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        rc = p.wait(timeout=120)
    except subprocess.TimeoutExpired:
        p.kill()
        log("the slot's session did not end; stopped it")
        rc = p.wait()
    sender.join(timeout=30)
    if teer is not None:
        teer.join(timeout=10)
    if journal:
        _journal(cfg, repo, blender_args, rc, t0, marks, list(tail), keys, back,
                 {"slot": cfg.get("slot") or "", "host": host, "commit": head,
                  "uncommitted": len(files), "deleted": len(deleted), "worktree": repo,
                  "branch": f"blender/{run_id}", "job": job})
    if rc == BUSY_EXIT:
        return rc
    log(f"Blender exit {rc} after {time.time() - t0:.0f} s; {len(back)} file(s) back"
        + (":" if back else ""))
    for name in back[:200]:
        print(f"  {name}", file=sys.stderr)
    if len(back) > 200:
        print(f"  ... and {len(back) - 200} more", file=sys.stderr)
    return rc


def _tee(stream, tail, keys: list, marks: dict) -> None:
    """Copy the slot session's stderr to ours as it arrives, keeping the last lines, the lines
    that matter and when Blender started and ended (the remote script's own markers)."""
    from .build_journal import _KEY
    out = getattr(sys.stderr, "buffer", None)
    try:
        for raw in iter(stream.readline, b""):
            try:
                if out is not None:
                    out.write(raw)
                else:
                    sys.stderr.write(raw.decode("utf-8", "replace"))
                sys.stderr.flush()
            except (OSError, ValueError):
                pass
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            tail.append(line)
            if _KEY.search(line) and len(keys) < 500:
                keys.append(line[:300])
            if "foundry-blender: running on" in line:
                marks.setdefault("blender_start", time.time())
            elif "foundry-blender: blender exit" in line:
                marks["blender_end"] = time.time()
    except (OSError, ValueError):
        pass


def _journal(cfg: dict, repo: str, args: list, rc: int, t0: float, marks: dict, tail: list,
             keys: list, back: list, where: dict) -> None:
    """One build-journal entry for this run; a failure is one log line, never a changed exit."""
    from . import build_journal as BJ
    end = time.time()
    m = {"start": t0, **marks, "end": end}

    def span(a, b):
        return round(m[b] - m[a], 1) if a in m and b in m else None
    timings = {"push": span("start", "pushed"), "library": span("pushed", "library"),
               "prepare": span("library", "blender_start"),
               "blender": span("blender_start", "blender_end"),
               "copy_back": span("blender_end", "end")}
    state = os.environ.get("FOUNDRY_COORD_STATE") or \
        os.path.join(os.path.expanduser("~"), ".foundry-coordinator")
    notes = ["the slot stayed busy: Blender did not run"] if rc == BUSY_EXIT else []
    e = BJ.record(state, project_id=str(cfg.get("project") or ""),
                  task_id=str(cfg.get("task") or ""),
                  source="local" if where.get("machine") == "this PC" else "slot",
                  repo=repo, args=args,
                  exit_code=rc, started_ts=t0, ended_ts=end, log_lines=tail, keys=keys,
                  files_back=[] if rc == BUSY_EXIT else back,
                  timings={k: v for k, v in timings.items() if v is not None}, where=where,
                  opts={k: cfg[k] for k in BJ.DEFAULTS if k in cfg}, notes=notes)
    log(f"build journal: {e['run_id']}" if e else "build journal: could not be written")


# ----------------------------------------------------------------------------- B2: sprints

# what a sprint may not run while its Blender is on a slot (Kilo bash rules, kilo_config)
LOCAL_BLENDER = ("*blender.exe*", "*Blender Foundation*")
# the same on a Linux workstation: `blender` itself and its usual places (the drop-in's own path,
# ~/.foundry-blender/..., never matches these)
LOCAL_BLENDER_POSIX = ("blender *", "* blender *", "*/opt/blender/*", "*/bin/blender *")


def blender_slot(profile) -> str:
    """The slot this project's Blender runs on ('' = this PC): a track bound to a slot uses
    its own slot (D-B1), else the project's `blender.slot` setting."""
    d = getattr(profile, "__dict__", {}) or {}
    if d.get("slot"):
        return str(d["slot"])
    raw = getattr(profile, "raw", None) or {}
    return str((raw.get("blender") or {}).get("slot") or "").strip()


def pool(profile) -> list:
    """BLENDER_POOL_PLAN P1: the project's Blender workers in order, [{id, jobs}], from
    `blender.pool` ("slot-1:2", "slot-2:2", "local:1"; a dict {id, jobs} works too). `local`
    (or "this-pc") is this PC. A track bound to a slot gets that slot first (D-B1). [] = no pool
    (`blender.slot` or this PC, as before)."""
    praw = getattr(profile, "raw", None) or {}
    raw = (praw.get("blender") or {}).get("pool") or []
    # slot ids as configured, matched without case (`Slot-2` is slot-2)
    known = {str(k).lower(): str(k) for k in (praw.get("slots") or {})}
    items = []
    for item in raw if isinstance(raw, list) else []:
        # one worker per entry, but `slot-1:2,slot-2:2` typed on one line works too
        items += [item] if isinstance(item, dict) else [
            x for x in re.split(r"[,\s]+", str(item)) if x]
    out, seen = [], set()
    for item in items:
        if isinstance(item, dict):
            wid, jobs = str(item.get("id") or "").strip(), item.get("jobs", 1)
        else:
            wid, _, jobs = str(item).strip().partition(":")
            jobs = jobs.strip() or 1
        wid = LOCAL if wid.lower() in ("local", "this-pc", "this pc", "pc") else             known.get(wid.lower(), wid)
        try:
            jobs = max(1, min(8, int(jobs)))
        except (TypeError, ValueError):
            jobs = 1
        if wid and wid not in seen:
            seen.add(wid)
            out.append({"id": wid, "jobs": jobs})
    if not out:
        return []
    tslot = str((getattr(profile, "__dict__", {}) or {}).get("slot") or "")
    if tslot:
        mine = [w for w in out if w["id"] == tslot] or [{"id": tslot, "jobs": 1}]
        out = mine + [w for w in out if w["id"] != tslot]
    # this PC is always the last choice (D-P1)
    return [w for w in out if w["id"] != LOCAL] + [w for w in out if w["id"] == LOCAL]


def blender_capacity(profile) -> int:
    """How many sprints may hold the coordinator's `blender` resource at once (D-P5): the
    pool's total jobs, else 1."""
    return sum(w["jobs"] for w in pool(profile)) or 1


def pooled(profile) -> bool:
    """Sprint Blender goes through the drop-in (a pool or a slot): the local blender.exe is
    denied in Kilo, the drop-in runs it when this PC is a pool worker."""
    return bool(pool(profile)) or bool(blender_slot(profile))


def deny_rules(profile) -> list:
    """Kilo rules that keep a sprint off the local Blender while its Blender is on a slot or a
    pool (the drop-in runs it then)."""
    if not pooled(profile):
        return []
    pats = LOCAL_BLENDER if os.name == "nt" else LOCAL_BLENDER_POSIX
    return [{"permission": "bash", "pattern": p, "action": "deny"} for p in pats]


def wrapper_dir() -> str:
    """Where the drop-ins live: ~/.foundry-blender (FOUNDRY_BLENDER_DIR). Not under the
    coordinator's state folder: Kilo's bash rules deny every command naming
    `.foundry-coordinator`, so a sprint could not run a drop-in from there."""
    return os.environ.get("FOUNDRY_BLENDER_DIR") or \
        os.path.join(os.path.expanduser("~"), ".foundry-blender")


def wrapper_paths(profile, state_dir: str = "") -> tuple[str, str]:
    """(<wrapper_dir>/<project>-<worktree hash>.json, the matching .cmd / shell script)."""
    import hashlib
    ws = os.path.realpath(str(getattr(profile, "workspace", "") or ""))
    h = hashlib.sha256(ws.lower().encode("utf-8")).hexdigest()[:10]
    base = os.path.join(wrapper_dir(), f"{getattr(profile, 'id', 'project')}-{h}")
    return base + ".json", base + (".cmd" if os.name == "nt" else ".sh")


def prepare(profile, state_dir: str, task_id: str = "", run_id: str = "") -> Optional[dict]:
    """When this project's Blender runs on a slot: write its settings and a drop-in for the
    `blender` command (same arguments) for this worktree; return {slot, command} or
    {slot, error}. None when Blender runs on this PC."""
    from . import slots
    if pool(profile):
        return _prepare_pool(profile, state_dir, task_id, run_id)
    slot_id = blender_slot(profile)
    if not slot_id:
        return None
    s = slots.configured(profile).get(slot_id)
    if not s:
        return {"slot": slot_id, "error": f"Blender is set to run on {slot_id}, but no enabled "
                                          f"slot {slot_id} is configured"}
    if not str(s.get("host") or "").strip():
        return {"slot": slot_id, "error": f"{slot_id} is a local container slot: foundry-blender "
                                          "runs Blender on remote slots (a mini PC)"}
    cfg_path, cmd_path = wrapper_paths(profile, state_dir)
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    cfg = {"project": getattr(profile, "id", ""), "slot": slot_id,
           "workspace": os.path.realpath(str(profile.workspace)),
           # B2b: the asset library on this PC (setting blender.library; empty = none)
           "library": str(((getattr(profile, "raw", None) or {}).get("blender") or {})
                          .get("library", "C:/Roblox/AssetLibrary") or ""),
           "max_job_seconds": int(((getattr(profile, "raw", None) or {}).get("blender") or {})
                                  .get("max_job_seconds", MAX_JOB_SECONDS) or 0),
           **{k: s.get(k) for k in ("host", "container", "au_push_url")}}
    # build replay (docs/plans/BLENDER_BUILD_REPLAY_PLAN.md R1): the task the prompt is for and
    # the journal settings, read again by every run of the drop-in
    from .build_journal import settings as journal_settings
    cfg.update(task=str(task_id or ""), run_id=str(run_id or ""), **journal_settings(profile))
    if [str(x) for x in (s.get("ssh") or [])] not in ([], slots.SSH_DEFAULT):
        cfg["ssh"] = s["ssh"]
    _write(cfg_path, json.dumps(cfg, indent=1))
    _write_dropin(cmd_path, cfg_path, cfg["workspace"], state_dir)
    return {"slot": slot_id, "command": cmd_path, "config": cfg_path}


def _prepare_pool(profile, state_dir: str, task_id: str, run_id: str) -> dict:
    """The drop-in's settings for a pool: each worker with what a run needs (a slot's host,
    container, push url; this PC's Blender), plus the journal settings."""
    from . import slots
    from .build_journal import settings as journal_settings
    bl = (getattr(profile, "raw", None) or {}).get("blender") or {}
    conf = slots.configured(profile)
    workers, notes = [], []
    for w in pool(profile):
        if w["id"] == LOCAL:
            workers.append({**w, "exe": str(bl.get("local_exe") or default_local_exe())})
            continue
        s = conf.get(w["id"])
        if not s:
            notes.append(f"{w['id']}: no enabled slot with that name")
            continue
        if not str(s.get("host") or "").strip():
            notes.append(f"{w['id']}: a local container slot (the pool needs a mini PC)")
            continue
        x = {**w, **{k: s.get(k) for k in ("host", "container", "au_push_url")}}
        if [str(v) for v in (s.get("ssh") or [])] not in ([], slots.SSH_DEFAULT):
            x["ssh"] = s["ssh"]
        workers.append(x)
    label = ", ".join(f"{'this PC' if w['id'] == LOCAL else w['id']} x{w['jobs']}"
                      for w in workers)
    if not workers:
        return {"slot": "pool", "error": "the Blender pool has no usable worker ("
                                         + "; ".join(notes) + ")"}
    cfg_path, cmd_path = wrapper_paths(profile, state_dir)
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    cfg = {"project": getattr(profile, "id", ""), "workspace": os.path.realpath(
               str(profile.workspace)),
           "library": str(bl.get("library", "C:/Roblox/AssetLibrary") or ""),
           "pool": workers, "pool_wait_seconds": int(bl.get("pool_wait_seconds") or 600),
           "max_job_seconds": int(bl.get("max_job_seconds", MAX_JOB_SECONDS) or 0),
           "local_exe": str(bl.get("local_exe") or default_local_exe()),
           "task": str(task_id or ""), "run_id": str(run_id or ""), **journal_settings(profile)}
    _write(cfg_path, json.dumps(cfg, indent=1))
    _write_dropin(cmd_path, cfg_path, cfg["workspace"], state_dir)
    return {"slot": f"pool ({label})", "pool": [w["id"] for w in workers], "command": cmd_path,
            "config": cfg_path, "notes": notes}


def _write_dropin(cmd_path: str, cfg_path: str, workspace: str, state_dir: str) -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = {"workspace": workspace}
    if os.name == "nt":
        text = ("@echo off\r\n"
                "rem foundry-blender for this worktree (SLOT_BLENDER_PLAN B2): use it wherever you\r\n"
                "rem would run blender.exe; Blender runs on the slot and the outputs come back.\r\n"
                f'set "PYTHONPATH={root}"\r\n'
                f'set "FOUNDRY_COORD_STATE={state_dir}"\r\n'
                f'"{sys.executable}" -m foundry_coordinator.slots blender --config "{cfg_path}" '
                f'--repo "{cfg["workspace"]}" -- %*\r\n'
                "exit /b %ERRORLEVEL%\r\n")
    else:
        text = ("#!/bin/sh\n# foundry-blender for this worktree (SLOT_BLENDER_PLAN B2)\n"
                f"PYTHONPATH={shlex.quote(root)} FOUNDRY_COORD_STATE={shlex.quote(state_dir)} "
                f"exec {shlex.quote(sys.executable)} -m foundry_coordinator.slots blender "
                f"--config {shlex.quote(cfg_path)} --repo {shlex.quote(cfg['workspace'])} -- \"$@\"\n")
    _write(cmd_path, text)
    if os.name != "nt":
        os.chmod(cmd_path, 0o755)


def _write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def relative_args(repo: str, args: list) -> tuple[list, str]:
    """Blender's arguments with absolute paths inside the worktree made repo-relative (the slot
    has the same tree, not the same drive); ('', why) for one outside the worktree."""
    root = os.path.realpath(repo)
    out = []
    for a in args:
        if re.match(r"^[A-Za-z]:[\\/]", a) or (os.name != "nt" and a.startswith("/")
                                               and os.path.exists(a)):
            p = os.path.realpath(a)
            if p == root or p.startswith(root + os.sep):
                out.append(os.path.relpath(p, root).replace("\\", "/"))
                continue
            return [], (f"{a} is outside the worktree: the slot only has the worktree's files "
                        "(use a path inside it)")
        out.append(a)
    return out, ""


def add_parser(sub) -> None:
    b = sub.add_parser("blender", help="run Blender on a Studio slot (SLOT_BLENDER_PLAN B1)")
    b.add_argument("--config", default="", help=f"slot settings (default <repo>/{CONFIG})")
    b.add_argument("--repo", default="", help="the worktree (default: the current folder)")
    b.add_argument("--host", default="")
    b.add_argument("--container", default="")
    b.add_argument("--au-push-url", default="")
    b.add_argument("--ssh", default="")
    b.add_argument("--id", default="", help="run id (default: worktree name + time)")
    b.add_argument("--threads", type=int, default=0, help="0 = the slot's CPU limit")
    b.add_argument("--wait", type=int, default=600, help="seconds to wait for a busy slot")
    b.add_argument("--library", default="", help="the asset library folder on this PC "
                   "(default: the settings' blender.library)")
    b.add_argument("--no-library", action="store_true", help="run without the asset library")
    b.add_argument("blender", nargs="*", help="the Blender arguments, after --")


__all__ = ["run", "add_parser", "remote_script", "receive", "local_changes", "BUSY_EXIT"]

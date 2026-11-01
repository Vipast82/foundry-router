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


def remote_script(run_id: str, wait: int, threads: int, blender_args: list) -> str:
    """The one script the slot runs (bash, in the container). `run_id` is [A-Za-z0-9._-]."""
    args = list(blender_args)
    if not any(a in ("-t", "--threads") for a in args):
        args = ["-t", "$T"] + args
    quoted = " ".join('"$T"' if a == "$T" else shlex.quote(a) for a in args)
    return f"""set -u
mkdir -p "$HOME/locks"
exec 9>"$HOME/locks/blender.lock"
if ! flock -w {int(wait)} 9; then
    echo "foundry-blender: this slot's Blender stayed busy for {int(wait)} s (another job)" >&2
    exit {BUSY_EXIT}
fi
W="$HOME/work/blender"
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
fi
M="$HOME/locks/blender.marker"
touch "$M"; sleep 1
cd "$W"
echo "foundry-blender: running on $(hostname) with $T threads: blender {quoted}" >&2
blender {quoted} 1>&2
rc=$?
find . \\( -path ./.git -o -name __pycache__ \\) -prune -o -type f -newer "$M" -print0 \\
    | tar --null -T - -cf -
exit $rc
"""


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
    cfg = load_config(repo, a.config)
    slot = {**slots.DEFAULTS, **{k: v for k, v in cfg.items() if k in slots.DEFAULTS}}
    for k in ("host", "container", "au_push_url"):
        if getattr(a, k, ""):
            slot[k] = getattr(a, k)
    if a.ssh:
        slot["ssh"] = shlex.split(a.ssh)
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
    log(f"{cfg.get('slot') or host}: {head[:12]} + {len(files)} changed, {len(deleted)} deleted "
        f"file(s) -> the slot")
    r = _git(repo, "push", "-q", "--force", url, f"{head}:refs/heads/blender/{run_id}",
             timeout=300, env=slots.git_ssh_env(slot))
    if r.returncode != 0:
        log("cannot reach the slot's game mirror: "
            + (r.stderr or r.stdout).decode("utf-8", "replace").strip()[-300:])
        return 3
    script = remote_script(run_id, a.wait, a.threads, blender_args)
    p = subprocess.Popen(slots.exec_argv(slot, ["bash", "-c", script], interactive=True),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                         creationflags=_NO_WINDOW)
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
    if rc == BUSY_EXIT:
        return rc
    log(f"Blender exit {rc} after {time.time() - t0:.0f} s; {len(back)} file(s) back"
        + (":" if back else ""))
    for name in back[:200]:
        print(f"  {name}", file=sys.stderr)
    if len(back) > 200:
        print(f"  ... and {len(back) - 200} more", file=sys.stderr)
    return rc


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
    b.add_argument("blender", nargs="*", help="the Blender arguments, after --")


__all__ = ["run", "add_parser", "remote_script", "receive", "local_changes", "BUSY_EXIT"]

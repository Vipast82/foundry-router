"""Studio slots: containerised Studio + Rojo + Blender machines a TRACK can hold (contrib/studio-slot).

A track normally has no Studio: a worktree cannot drive the one Rojo-served main checkout, so its
Studio checks are owed at the merge. A track bound to a slot (`tracks create ... --slot <id>`,
stored in the track row's data) holds the lock `slot:<id>` instead and gets:

  * its scripted Studio gate run INSIDE the slot: `slot-checkout <head>` moves the slot's own
    checkout (and Rojo, and Studio when files were added) to the track's commit, the same
    `studio_calls pipeline` runs there against the slot's Studio, and the outputs the pipeline
    writes (docs/progress/<ID>_*, docs/evidence/<ID>/, .plan/tmp/gate/<ID>/) are copied back into
    the track's worktree, where the coordinator commits them as for any run;
  * Kilo sessions whose Studio MCP servers reach the slot (`docker exec -i <container> ...`),
    never the workstation's Studio.

Config (project.toml, also editable from the router UI):

    [slots.slot-1]
    container = "studio-slot-poc"              # docker container name
    repo = "/home/studio/work/AnimalUprising"  # the slot's own checkout
    foundry = "/opt/foundry"                    # the Foundry the slot runs (the deploy, mounted)
    path_prefix = "/home/studio/.rokit/bin"     # extra PATH for lune/selene/stylua/wally
    official_mcp = "/usr/local/bin/studio-mcp-official"
    community_mcp = "/usr/local/bin/robloxstudio-mcp"
    enabled = true

A slot on ANOTHER machine (a Proxmox LXC running Docker, contrib/studio-slot/proxmox) adds:

    host = "studio@192.168.0.50"                # ssh target that runs `docker` ("" = this PC)
    ssh = ["ssh", "-o", "BatchMode=yes"]        # the ssh command (key auth, no prompts)
    au_push_url = "ssh://studio@192.168.0.50/srv/studio-slot/AnimalUprising.git"
    foundry_push_url = "ssh://studio@192.168.0.50/srv/studio-slot/foundry.git"
    foundry_host_dir = "/srv/studio-slot/foundry"   # mounted read-only at `foundry` in the slot

Every container command then runs as `ssh <host> docker exec ...` (the Kilo Studio MCP servers
too: stdio over ssh). Before a gate run the track's commit is pushed to the host's bare AU
mirror (`refs/heads/slot/<task>`; the slot's `origin`, so `slot-checkout` fetches it) and the
deployed Foundry's HEAD to the host's Foundry mirror, which `foundry_host_dir` then checks out.
No Windows path is mounted into a remote slot.

2026-10-31 (Victor): "build dispatch, real sprint" — first run AS-J05 (props live).
2026-10-31 (Victor): "start it" — remote slots for Proxmox mini PCs.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shlex
import subprocess
import sys
import tarfile
from typing import Optional

LOCK_PREFIX = "slot:"
OUTPUT_GLOBS = ("docs/progress/{task}_*", "docs/evidence/{task}", ".plan/tmp/gate/{task}")
STUDIO_MCP = {"Roblox_Studio": "official_mcp", "robloxstudio-mcp": "community_mcp"}
# Key auth only, never a prompt; a new slot host's key is accepted on first contact (LAN, trust on
# first use: Victor 2026-10-31) and checked on every later connection; an unreachable host fails
# in 15 s instead of hanging a gate.
SSH_DEFAULT = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
               "-o", "ConnectTimeout=15"]
# setup-host.sh's layout on a slot host: what a remote slot's push targets default to
HOST_ROOT = "/srv/studio-slot"
DEFAULTS = {"container": "", "repo": "/home/studio/work/AnimalUprising", "foundry": "/opt/foundry",
            "path_prefix": "/home/studio/.rokit/bin",
            "official_mcp": "/usr/local/bin/studio-mcp-official",
            "community_mcp": "/usr/local/bin/robloxstudio-mcp", "enabled": True,
            "checkout_timeout_s": 600, "host": "", "ssh": SSH_DEFAULT,
            "au_push_url": "", "foundry_push_url": "", "foundry_host_dir": ""}
FOUNDRY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ----------------------------------------------------------------------------- config

def configured(profile) -> dict:
    """{slot_id: settings} from the profile's [slots] table (enabled ones only)."""
    raw = (getattr(profile, "raw", None) or {}).get("slots") or {}
    out = {}
    for sid, cfg in raw.items() if isinstance(raw, dict) else []:
        if not isinstance(cfg, dict):
            continue
        s = {**DEFAULTS, **cfg}
        if s.get("enabled") is False or not s.get("container"):
            continue
        host = str(s.get("host") or "").strip()
        if host:                    # remote: setup-host.sh's mirrors unless set explicitly
            s["au_push_url"] = s.get("au_push_url") or f"ssh://{host}{HOST_ROOT}/AnimalUprising.git"
            s["foundry_push_url"] = s.get("foundry_push_url") or f"ssh://{host}{HOST_ROOT}/foundry.git"
            s["foundry_host_dir"] = s.get("foundry_host_dir") or f"{HOST_ROOT}/foundry"
        out[str(sid)] = s
    return out


def slot_id_of(c, P) -> str:
    """The slot a TRACK project is bound to ('' for main or an unbound track)."""
    tracks = getattr(c, "tracks", None)
    if tracks is None or not tracks.is_track(P):
        return ""
    name = tracks.track_name(P)
    try:
        row = c.store.track(P.id, name)
    except Exception:                                             # noqa: BLE001
        return ""
    return str(((row or {}).get("data") or {}).get("slot") or "")


def slot_for(c, P) -> Optional[dict]:
    """(slot settings + `id`) for a track bound to an enabled slot, else None."""
    sid = slot_id_of(c, P)
    if not sid:
        return None
    s = configured(c.tracks.main_of(P).profile if getattr(c, "tracks", None) else P.profile).get(sid)
    return {**s, "id": sid} if s else None


def lock_name(slot: dict) -> str:
    return LOCK_PREFIX + str(slot["id"])


# ----------------------------------------------------------------------------- commands

def ssh_cmd(slot: dict) -> list:
    """The ssh command of a remote slot (a list; a string is split shell-style)."""
    v = slot.get("ssh") or DEFAULTS["ssh"]
    return shlex.split(v) if isinstance(v, str) else [str(x) for x in v]


def host_argv(slot: dict, args: list) -> list:
    """argv that runs `args` on the machine that runs the slot's docker: as-is on this PC, else
    `ssh <host> <args, shell-quoted>` (ssh hands the remote side one command string)."""
    host = str(slot.get("host") or "").strip()
    if not host:
        return list(args)
    return ssh_cmd(slot) + [host, shlex.join([str(a) for a in args])]


def exec_argv(slot: dict, args: list, interactive: bool = False) -> list:
    """argv for `docker exec [-i] <container> <args>` on the slot's machine."""
    return host_argv(slot, ["docker", "exec"] + (["-i"] if interactive else [])
                     + [slot["container"]] + [str(a) for a in args])


def wrap_pipeline(slot: dict, pipeline_args: list, worktree: str, task: str, head: str) -> list:
    """The host command that runs `pipeline_args` (the `studio_calls pipeline ...` argv after the
    module, with --repo set to the HOST worktree) inside the slot and copies the outputs back."""
    remote = []
    if str(slot.get("host") or "").strip():
        remote = ["--host", str(slot["host"]), "--ssh", shlex.join(ssh_cmd(slot)),
                  "--au-push-url", slot.get("au_push_url") or "",
                  "--foundry-push-url", slot.get("foundry_push_url") or "",
                  "--foundry-host-dir", slot.get("foundry_host_dir") or ""]
    return [sys.executable, "-m", "foundry_coordinator.slots", "run",
            "--container", slot["container"], "--slot-repo", slot["repo"],
            "--foundry", slot["foundry"], "--path-prefix", slot.get("path_prefix") or "",
            "--worktree", worktree, "--task", task, "--head", head,
            "--checkout-timeout", str(int(slot.get("checkout_timeout_s") or 600))] \
        + remote + ["--"] + list(pipeline_args)


def slot_pipeline_args(args: list, slot_repo: str) -> list:
    """The pipeline argv with --repo pointing at the slot's checkout."""
    out = list(args)
    for i, a in enumerate(out[:-1]):
        if a == "--repo":
            out[i + 1] = slot_repo
    return out


def _slot_of(x) -> dict:
    """A slot dict from a slot dict or a bare (local) container name."""
    return x if isinstance(x, dict) else {"container": str(x)}


def _docker(slot, script: str, timeout: Optional[float] = None,
            stdout=None) -> subprocess.CompletedProcess:
    return subprocess.run(exec_argv(_slot_of(slot), ["bash", "-lc", script]),
                          stdin=subprocess.DEVNULL,
                          stdout=stdout if stdout is not None else subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout, creationflags=_NO_WINDOW)


def _git(cwd: str, *args, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd] + list(args), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                          creationflags=_NO_WINDOW)


def push_sources(slot: dict, worktree: str, task: str, head: str) -> tuple:
    """A remote slot's sources, before its gate run: the track's commit -> the host's bare AU
    mirror as `slot/<task>` (the slot's origin), and the deployed Foundry's HEAD -> the host's
    Foundry mirror, which `foundry_host_dir` (mounted at `foundry` in the slot) then checks out.
    Returns (ok, detail); a local slot (no host) has nothing to push."""
    if not str(slot.get("host") or "").strip():
        return True, "local slot"
    done = []
    url = slot.get("au_push_url") or ""
    if url:
        if not _TASK_RE.fullmatch(task or ""):
            return False, f"bad task id {task!r}"
        r = _git(worktree, "push", "-q", "--force", url, f"{head}:refs/heads/slot/{task}")
        if r.returncode != 0:
            return False, "AU push: " + (r.stdout or b"").decode("utf-8", "replace")[-300:]
        done.append(f"AU {head[:12]} -> slot/{task}")
    furl, fdir = slot.get("foundry_push_url") or "", slot.get("foundry_host_dir") or ""
    if furl and fdir:
        r = _git(FOUNDRY_ROOT, "push", "-q", "--force", furl, "HEAD:refs/heads/deploy")
        if r.returncode != 0:
            return False, "Foundry push: " + (r.stdout or b"").decode("utf-8", "replace")[-300:]
        q = shlex.quote(fdir)
        r = subprocess.run(host_argv(slot, ["sh", "-c",
                           f"git -C {q} fetch -q origin deploy && "
                           f"git -C {q} -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD && "
                           f"git -C {q} rev-parse --short=12 HEAD"]),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=300, creationflags=_NO_WINDOW)
        if r.returncode != 0:
            return False, "Foundry checkout on the host: " + (r.stdout or b"").decode("utf-8", "replace")[-300:]
        done.append("Foundry " + (r.stdout or b"").decode("utf-8", "replace").strip()[-12:])
    return True, "; ".join(done) or "nothing to push"


_TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def copy_back(slot, slot_repo: str, worktree: str, task: str) -> list:
    """Stream the run's outputs out of the slot (tar over `docker exec`) into the worktree.
    Only paths under the OUTPUT_GLOBS for this task; never anything else. Returns the paths."""
    if not _TASK_RE.fullmatch(task or ""):
        return []
    # the task id is [A-Za-z0-9._-] (checked above), so the globs need no quoting and expand in
    # the container's shell; nullglob drops the ones that match nothing
    globs = " ".join(g.replace("{task}", task) for g in OUTPUT_GLOBS)
    script = (f"cd {shlex.quote(slot_repo)} && shopt -s nullglob && set -- {globs}; "
              'if [ "$#" -gt 0 ]; then tar -cf - "$@"; fi')
    r = subprocess.run(exec_argv(_slot_of(slot), ["bash", "-lc", script]),
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=300, creationflags=_NO_WINDOW)
    if not r.stdout:
        return []
    allowed = tuple(g.replace("{task}", task).rstrip("*") for g in OUTPUT_GLOBS)
    root = os.path.realpath(worktree)
    out = []
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        for m in tf.getmembers():
            name = m.name.replace("\\", "/")
            while name.startswith("./"):
                name = name[2:]
            if not (m.isfile() or m.isdir()) or ".." in name.split("/") \
                    or not name.startswith(allowed):
                continue
            dest = os.path.realpath(os.path.join(root, *name.split("/")))
            if not dest.startswith(root + os.sep):
                continue
            if m.isdir():
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with open(dest, "wb") as f:
                f.write(src.read())
            out.append(name)
    return out


def run(a) -> int:
    """`python -m foundry_coordinator.slots run ...`: checkout -> pipeline in the slot -> copy back.
    Exit code = the pipeline's (or 3 when the slot could not be moved to the commit)."""
    slot = {"container": a.container, "host": a.host or "", "ssh": shlex.split(a.ssh) if a.ssh else None,
            "au_push_url": a.au_push_url or "", "foundry_push_url": a.foundry_push_url or "",
            "foundry_host_dir": a.foundry_host_dir or ""}
    c, repo = slot, a.slot_repo
    if slot["host"]:
        ok, detail = push_sources(slot, a.worktree, a.task, a.head)
        print(f"SLOT push ({slot['host']}): {detail}", flush=True)
        if not ok:
            print("SLOT FAIL: could not push the sources to the slot host", flush=True)
            return 3
    print(f"SLOT {a.container}{' @ ' + slot['host'] if slot['host'] else ''}: "
          f"slot-checkout {a.head[:12]} (task {a.task})", flush=True)
    try:
        r = _docker(c, f"slot-checkout {shlex.quote(a.head)}", timeout=a.checkout_timeout)
    except subprocess.TimeoutExpired:
        print("SLOT FAIL: slot-checkout timed out", flush=True)
        return 3
    sys.stdout.write((r.stdout or b"").decode("utf-8", "replace")[-4000:])
    text = (r.stdout or b"").decode("utf-8", "replace")
    if r.returncode != 0 or not any(ln.startswith("SLOT-CHECKOUT ") and " OK" in ln
                                    for ln in text.splitlines()):
        print(f"SLOT FAIL: slot-checkout rc={r.returncode}", flush=True)
        return 3
    args = slot_pipeline_args(a.pipeline, repo)
    env = (f"export PYTHONPATH={shlex.quote(a.foundry)} PYTHONIOENCODING=utf-8 "
           f"FOUNDRY_GATE_RUN_ID={shlex.quote(os.environ.get('FOUNDRY_GATE_RUN_ID', ''))}; ")
    if a.path_prefix:
        env += f"export PATH={shlex.quote(a.path_prefix)}:$PATH; "
    script = (env + f"cd {shlex.quote(repo)} && python3 -m foundry_coordinator.studio_calls "
              + " ".join(shlex.quote(x) for x in args))
    print("SLOT run: " + script[:400], flush=True)
    p = subprocess.Popen(exec_argv(c, ["bash", "-lc", script]), stdin=subprocess.DEVNULL,
                         stdout=sys.stdout, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
    rc = p.wait()
    copied = copy_back(c, repo, a.worktree, a.task)
    print(f"SLOT copied back {len(copied)} file(s): " + ", ".join(copied[:20]), flush=True)
    return rc


def resync(slot: dict, restart_rojo: bool = True, timeout: float = 420) -> tuple:
    """Repair a stale Studio <-> Rojo sync INSIDE the slot (never the workstation): restart the
    slot's supervised `rojo serve` (its loop starts a fresh one), then `studio-reopen --wait` so
    the Rojo plugin auto-reconnects and syncs. Returns (ok, detail)."""
    # -x rojo: the binary only (a -f 'rojo serve' match also hit this shell and the supervisor)
    script = ("pkill -x rojo || true; sleep 3; " if restart_rojo else "") + \
             "studio-reopen --wait"
    try:
        r = _docker(slot, script, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "studio-reopen timed out in the slot"
    except OSError as e:
        return False, f"docker: {e}"[:200]
    text = (r.stdout or b"").decode("utf-8", "replace").strip()
    return r.returncode == 0, (text.splitlines()[-1] if text else f"rc={r.returncode}")[:200]


def stop(slot) -> None:
    """Best effort: stop a pipeline left running in the slot (a slot dict or a local container)."""
    try:
        _docker(slot, "pkill -f 'foundry_coordinator.studio_calls' || true; "
                           "pkill -f 'foundry_coordinator.projects.animaluprising.studio_gate' || true",
                timeout=30)
    except Exception:                                             # noqa: BLE001
        pass


# ----------------------------------------------------------------------------- kilo config

def kilo_mcp_overrides(slot: dict) -> dict:
    """{mcp name: server block} pointing the Studio MCP servers at the slot."""
    out = {}
    for name, key in STUDIO_MCP.items():
        out[name] = {"type": "local", "enabled": True, "timeout": 60000,
                     "command": exec_argv(slot, [slot[key]], interactive=True)}
    return out


def patch_kilo_config(path: str, slot: dict) -> list:
    """Point the track kilo.jsonc's Studio MCP servers at the slot (only the ones it enables).
    Returns the names changed."""
    from foundry_automation import jsonc
    with open(path, encoding="utf-8") as f:
        text = f.read()
    data = jsonc.parse(text)
    mcp = data.get("mcp") if isinstance(data, dict) else None
    changed = []
    for name, block in kilo_mcp_overrides(slot).items():
        cur = (mcp or {}).get(name)
        if isinstance(cur, dict) and cur.get("enabled") is True and cur.get("command") != block["command"]:
            text = jsonc.replace_value(text, ("mcp", name), block)
            changed.append(name)
    if changed:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m foundry_coordinator.slots")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--container", required=True)
    r.add_argument("--slot-repo", required=True)
    r.add_argument("--foundry", default="/opt/foundry")
    r.add_argument("--path-prefix", default="")
    r.add_argument("--worktree", required=True)
    r.add_argument("--task", required=True)
    r.add_argument("--head", required=True)
    r.add_argument("--checkout-timeout", type=float, default=600)
    r.add_argument("--host", default="")
    r.add_argument("--ssh", default="")
    r.add_argument("--au-push-url", default="")
    r.add_argument("--foundry-push-url", default="")
    r.add_argument("--foundry-host-dir", default="")
    r.add_argument("pipeline", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        a.pipeline = [x for x in a.pipeline if x != "--"] if a.pipeline[:1] == ["--"] else a.pipeline
        return run(a)
    return 2


if __name__ == "__main__":
    sys.exit(main())

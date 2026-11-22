"""Slot upkeep (COORDINATOR_AUTOMATION_NEEDS items 17, 18 and 19).

What a watch session did by hand around the Studio slots, done by the coordinator:

  the copy stays fresh   A lane's first push to a slot sent every evidence frame committed
      (item 17)          since that slot's last lane: FIN-4a (slot-3) and BAL-2e3 (slot-1)
                         took over 30 minutes at about 50 KB/s. While a slot is idle (no
                         live lane bound, its lock free) the main branch is pushed to its
                         bare copy as `slot/_keep` every `slot_keep_fresh_hours` (default 4,
                         0 = never), so a lane's push only sends what the lane added.
  disk                   Each slot's disk use is read every ten minutes; at
      (item 18)          `slot_disk_alert_pct` (default 80, 0 = no alert) one alert a day
                         names the slot, the percent and what is free.
  version                Each slot's Foundry version and commit are read with the disk;
      (item 19)          the status board shows them, and says when a slot runs another
                         version than this coordinator.

None of it runs on the tick thread: a slot behind ssh can take 15 s to refuse and a push an
hour. `tick` starts one worker thread per slot at a time, which only runs commands and keeps
its result in memory; the next tick writes the events. The worker never touches the store.

The three operations that change files on a slot are commands, not automatic steps
(`python -m foundry_coordinator slot-keep ...`), so the first run on a slot can be watched:

  save-dirty   list a checkout's modified and untracked files and copy them to the
               workstation (<state>/slot-saves/<slot>/<time>/), before any clean-up;
  sparse       after save-dirty, set the Blender checkouts to a sparse checkout without
               docs/evidence (most of their 11-17 GB);
  repack       `git gc` on the slot's bare copy.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from typing import Optional

from . import slots as SL
from .automation import get as auto_get

CHECK_EVERY_S = 600.0
ALERT_AGAIN_S = 24 * 3600.0
KEEP_REF = "refs/heads/slot/_keep"
CMD_TIMEOUT_S = 60.0
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run(argv: list, timeout: float = CMD_TIMEOUT_S, stdout=None) -> tuple:
    """(returncode, text). -1 with the reason when it could not run or ran out of time."""
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL,
                           stdout=stdout if stdout is not None else subprocess.PIPE,
                           stderr=subprocess.PIPE if stdout is not None else subprocess.STDOUT,
                           timeout=timeout, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {int(timeout)} s"
    except OSError as e:
        return -1, f"{type(e).__name__}: {e}"[:300]
    out = r.stdout if stdout is None else r.stderr
    return r.returncode, (out or b"").decode("utf-8", "replace")


def _remote(slot: dict) -> bool:
    return bool(str(slot.get("host") or "").strip())


def bare_path(slot: dict) -> str:
    """The path of the slot's bare game copy on its host, from au_push_url ('' = none)."""
    m = re.match(r"ssh://[^/]+(/.+)$", str(slot.get("au_push_url") or ""))
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------- readings

def parse_df(text: str) -> Optional[dict]:
    """`df -P -k <path>` -> {"pct", "free_gb", "total_gb"}; None when it is not df output."""
    for line in reversed((text or "").strip().splitlines()):
        f = line.split()
        if len(f) >= 6 and f[4].endswith("%") and f[1].isdigit() and f[3].isdigit():
            return {"pct": int(f[4].rstrip("%")), "free_gb": round(int(f[3]) / 2 ** 20, 1),
                    "total_gb": round(int(f[1]) / 2 ** 20, 1)}
    return None


def read_disk(slot: dict) -> dict:
    """The disk the slot's copies live on: the host's slot folder for a remote slot, the
    container's home for a local one. {"pct", "free_gb", "total_gb"} or {"error"}."""
    if _remote(slot):
        path = os.path.dirname(bare_path(slot) or slot.get("foundry_host_dir") or "") or SL.HOST_ROOT
        rc, out = _run(SL.host_argv(slot, ["df", "-P", "-k", path]))
    else:
        rc, out = _run(SL.exec_argv(slot, ["df", "-P", "-k", os.path.dirname(
            str(slot.get("repo") or "/home/studio/work/x")) or "/home/studio"]))
    got = parse_df(out) if rc == 0 else None
    return got or {"error": (out or "no output").strip()[-200:]}


def read_foundry(slot: dict) -> dict:
    """The Foundry a slot runs: {"version", "head"} or {"error"}."""
    if _remote(slot) and slot.get("foundry_host_dir"):
        d = shlex.quote(str(slot["foundry_host_dir"]))
        script = (f"git -C {d} rev-parse --short=12 HEAD 2>/dev/null; "
                  f"sed -n 's/^__version__ *= *//p' {d}/foundry_coordinator/__init__.py")
        rc, out = _run(SL.host_argv(slot, ["sh", "-c", script]))
    else:
        d = shlex.quote(str(slot.get("foundry") or "/opt/foundry"))
        script = (f"git -C {d} rev-parse --short=12 HEAD 2>/dev/null; "
                  f"sed -n 's/^__version__ *= *//p' {d}/foundry_coordinator/__init__.py")
        rc, out = _run(SL.exec_argv(slot, ["sh", "-c", script]))
    if rc != 0:
        return {"error": (out or "no output").strip()[-200:]}
    version = head = ""
    for line in out.splitlines():
        line = line.strip().strip("\"'")
        if re.fullmatch(r"\d+\.\d+\.\d+", line):
            version = line
        elif re.fullmatch(r"[0-9a-f]{7,40}", line):
            head = line
    return {"version": version, "head": head} if version or head else {
        "error": "no version found: " + out.strip()[-120:]}


def keep_fresh(slot: dict, workspace: str, branch: str) -> dict:
    """Push the main branch's commit to the slot's bare copy as slot/_keep.
    {"ok", "head", "seconds", "detail"}."""
    url = str(slot.get("au_push_url") or "")
    if not _remote(slot) or not url:
        return {"ok": True, "head": "", "seconds": 0, "detail": "local slot: nothing to push"}
    t0 = time.time()
    rc, out = _run(["git", "-C", workspace, "rev-parse", "--verify", "--quiet",
                    f"refs/heads/{branch}"])
    head = out.strip()
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        return {"ok": False, "head": "", "seconds": 0, "detail": f"no commit for {branch}"}
    try:
        r = SL._git(workspace, "push", "-q", "--force", url, f"{head}:{KEEP_REF}",
                    env=SL.git_ssh_env(slot), timeout=SL.SLOT_PUSH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "head": head, "seconds": int(time.time() - t0),
                "detail": f"timed out after {int(SL.SLOT_PUSH_TIMEOUT_S)} s"}
    except OSError as e:
        return {"ok": False, "head": head, "seconds": 0, "detail": f"{type(e).__name__}: {e}"[:200]}
    return {"ok": r.returncode == 0, "head": head, "seconds": int(time.time() - t0),
            "detail": (r.stdout or b"").decode("utf-8", "replace").strip()[-200:]}


# ---------------------------------------------------------------------------- the tick

def _hours(P) -> float:
    try:
        return max(0.0, float(auto_get(P, "slot_keep_fresh_hours")))
    except Exception:                                            # noqa: BLE001
        return 4.0


def _alert_pct(P) -> int:
    try:
        return max(0, int(auto_get(P, "slot_disk_alert_pct")))
    except Exception:                                            # noqa: BLE001
        return 80


def idle(c, P, sid: str) -> bool:
    """No live lane is bound to the slot and nothing holds its lock."""
    from .tracks.manager import LIVE_STATES
    if any(str((r.get("data") or {}).get("slot") or "") == sid
           for r in c.store.tracks(P.id) if r["state"] in LIVE_STATES):
        return False
    return not any(l["resource"] == SL.LOCK_PREFIX + sid for l in c.store.locks())


def _work(slot: dict, jobs: dict, box: dict) -> None:
    """The worker thread: commands only, results into `box`."""
    try:
        box["disk"] = read_disk(slot)
        box["foundry"] = read_foundry(slot)
        if jobs.get("push"):
            box["push"] = keep_fresh(slot, jobs["workspace"], jobs["branch"])
    except Exception as e:                                       # noqa: BLE001
        box["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        box["done"] = time.time()


def tick(c, P, start=None) -> list:
    """Collect what the workers found (events, alerts) and start the next ones. Returns the
    events written. Never raises. `start(target, args)` runs a worker (tests pass their own)."""
    out: list = []
    try:
        if c.tracks.is_track(P):
            return out
        configured = SL.configured(P.profile)
        if not configured:
            return out
        state = c.__dict__.setdefault("_slotkeep", {}).setdefault(P.id, {})
        now = c.clock()
        for sid in sorted(configured):
            slot = {**configured[sid], "id": sid}
            st = state.setdefault(sid, {"at": -1e18})
            box = st.get("box")
            if box is not None:
                if "done" not in box:
                    continue                                     # still working
                out += _collect(c, P, sid, st, box, now)
                st.pop("box", None)
            if now - st["at"] < CHECK_EVERY_S:
                continue
            st["at"] = now
            jobs: dict = {}
            hours = _hours(P)
            if hours > 0 and _remote(slot) and slot.get("au_push_url") and idle(c, P, sid):
                last = float(c.store.meta(f"slot_fresh:{P.id}:{sid}") or 0)
                if now - last >= hours * 3600:
                    jobs = {"push": True, "workspace": P.profile.workspace,
                            "branch": P.profile.branch}
            st["box"] = box = {}
            if start is not None:
                start(_work, (slot, jobs, box))
            else:
                threading.Thread(target=_work, args=(slot, jobs, box), daemon=True,
                                 name=f"slotkeep-{sid}").start()
    except Exception as e:                                       # noqa: BLE001
        try:
            c.log(f"{P.id}: slot upkeep failed: {type(e).__name__}: {e}")
        except Exception:                                        # noqa: BLE001
            pass
    return out


def _collect(c, P, sid: str, st: dict, box: dict, now: float) -> list:
    out = []
    pid = P.id
    disk, fnd, push = box.get("disk") or {}, box.get("foundry") or {}, box.get("push")
    st["disk"], st["foundry"], st["read_ts"] = disk, fnd, now
    if push is not None:
        if push.get("ok"):
            c.store.set_meta(f"slot_fresh:{pid}:{sid}", str(now))
            st["fresh"] = {"ts": now, "head": push.get("head") or ""}
            if push.get("head"):
                d = {"slot": sid, "head": push["head"][:12], "seconds": push.get("seconds"),
                     "digest": True,
                     "text": f"{sid}: its copy now has {P.profile.branch} {push['head'][:12]} "
                             f"(pushed in {push.get('seconds') or 0} s while it was idle)"}
                c.store.event(pid, "", "slot_kept_fresh", detail=d)
                out.append(d)
        else:
            d = {"slot": sid, "error": str(push.get("detail") or "")[:300], "digest": True}
            c.store.event(pid, "", "slot_keep_failed", detail=d)
            out.append(d)
    limit = _alert_pct(P)
    pct = disk.get("pct")
    if limit and pct is not None and pct >= limit:
        if now - float(st.get("alerted") or -1e18) >= ALERT_AGAIN_S:
            st["alerted"] = now
            d = {"kind": "slot_disk", "alert_class": "watch", "slot": sid, "pct": pct,
                 "title": f"{sid}: disk {pct}% full",
                 "text": (f"{sid}: its disk is {pct}% full ({disk.get('free_gb')} GB free of "
                          f"{disk.get('total_gb')} GB; alert at {limit}%). The bare game copy, "
                          f"the Studio checkout and the Blender checkouts are the big parts: "
                          f"`slot-keep save-dirty`, then `sparse` and `repack`, free space.")}
            c.store.event(pid, "", "operator_notice", detail=d)
            out.append(d)
    elif pct is not None and limit and pct < limit - 5:
        st.pop("alerted", None)
    return out


def view(c, P) -> dict:
    """{slot: {"disk_pct", "free_gb", "foundry", "behind", "fresh_hours"}} for the board."""
    from . import __version__ as mine
    out: dict = {}
    state = (c.__dict__.get("_slotkeep") or {}).get(P.id) or {}
    now = c.clock()
    for sid, st in state.items():
        row: dict = {}
        disk, fnd = st.get("disk") or {}, st.get("foundry") or {}
        if disk.get("pct") is not None:
            row.update(disk_pct=disk["pct"], free_gb=disk.get("free_gb"))
        if fnd.get("version") or fnd.get("head"):
            row["foundry"] = fnd.get("version") or fnd.get("head")
            if fnd.get("version") and fnd["version"] != mine:
                row["behind"] = f"runs v{fnd['version']}, this coordinator v{mine}"
        try:
            last = float(c.store.meta(f"slot_fresh:{P.id}:{sid}") or 0)
        except Exception:                                        # noqa: BLE001
            last = 0.0
        if last:
            row["fresh_hours"] = round(max(0.0, now - last) / 3600, 1)
        if row:
            out[sid] = row
    return out


def words(row: dict) -> str:
    bits = []
    if row.get("disk_pct") is not None:
        bits.append(f"disk {row['disk_pct']}%")
    if row.get("foundry"):
        bits.append("Foundry " + (f"v{row['foundry']}" if "." in str(row["foundry"])
                                  else str(row["foundry"]))
                    + (" (differs: " + row["behind"] + ")" if row.get("behind") else ""))
    if row.get("fresh_hours") is not None:
        bits.append(f"copy refreshed {row['fresh_hours']} h ago")
    return ", ".join(bits)


# ---------------------------------------------------------------------------- commands

def checkouts(slot: dict) -> list:
    """The git checkouts inside a slot: Studio's, then the Blender ones (~/work/blender*)."""
    out = [str(slot.get("repo") or "")]
    rc, text = _run(SL.exec_argv(slot, ["sh", "-c", "ls -d ~/work/blender* 2>/dev/null"]))
    if rc == 0:
        out += [l.strip() for l in text.splitlines() if l.strip().startswith("/")]
    return [c for c in out if c]


def dirty(slot: dict, checkout: str) -> list:
    """Modified and untracked (not ignored) files of a checkout in the slot."""
    q = shlex.quote(checkout)
    rc, text = _run(SL.exec_argv(slot, ["sh", "-c",
                                        f"git -C {q} ls-files -m -o --exclude-standard"]),
                    timeout=300)
    if rc != 0:
        raise RuntimeError(f"{checkout}: {text.strip()[-200:]}")
    return sorted({l for l in text.splitlines() if l.strip()})


def save_dirty(slot: dict, checkout: str, dest_dir: str) -> dict:
    """Copy a checkout's modified and untracked files to `dest_dir` on this machine: a tar
    and a list. {"files", "tar", "list"}; nothing is changed on the slot."""
    files = dirty(slot, checkout)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", checkout.strip("/")) or "checkout"
    os.makedirs(dest_dir, exist_ok=True)
    lst = os.path.join(dest_dir, name + ".txt")
    with open(lst, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(files) + ("\n" if files else ""))
    out = {"files": len(files), "list": lst, "tar": ""}
    if not files:
        return out
    q = shlex.quote(checkout)
    tar = os.path.join(dest_dir, name + ".tar")
    with open(tar, "wb") as f:
        rc, err = _run(SL.exec_argv(slot, ["sh", "-c",
                       f"cd {q} && git ls-files -m -o --exclude-standard -z | "
                       f"tar --null --ignore-failed-read -T - -cf -"]), timeout=3600, stdout=f)
    if rc != 0:
        raise RuntimeError(f"{checkout}: copying its files failed: {err.strip()[-200:]}")
    out["tar"] = tar
    return out


def sparse(slot: dict, checkout: str) -> str:
    """Set a Blender checkout to a sparse checkout without docs/evidence. The caller saves
    its dirty files first. Returns what git said."""
    q = shlex.quote(checkout)
    rc, text = _run(SL.exec_argv(slot, ["sh", "-c",
                    f"git -C {q} sparse-checkout set --no-cone '/*' '!/docs/evidence/' && "
                    f"du -sh {q} | cut -f1"]), timeout=1800)
    if rc != 0:
        raise RuntimeError(f"{checkout}: {text.strip()[-300:]}")
    return text.strip()[-80:]


def repack(slot: dict) -> str:
    """`git gc` on the slot's bare game copy (remote slots). Returns its size after."""
    path = bare_path(slot)
    if not _remote(slot) or not path:
        raise RuntimeError("a local slot has no bare copy")
    q = shlex.quote(path)
    rc, text = _run(SL.host_argv(slot, ["sh", "-c",
                    f"git -C {q} gc --prune=now -q && du -sh {q} | cut -f1"]), timeout=4 * 3600)
    if rc != 0:
        raise RuntimeError(f"{path}: {text.strip()[-300:]}")
    return text.strip()[-40:]

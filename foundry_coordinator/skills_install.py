"""Foundry-shipped Kilo skills, installed into a game's local skill folder
(docs/plans/FOUNDRY_SKILLS_DELIVERY_PLAN.md, asset-pipeline review gap 7).

Kilo reads skills from `<workspace>/.kilo/skills/<name>/SKILL.md`. Sessions in the track
worktrees under `<workspace>/.tracks/` find the same folder. In AU that folder is untracked:
it is the workstation's install location, not part of the game repo. So installing a skill
there changes no tracked file and needs no sprint.

Rules:
- Only the skills `[skills] install` lists are installed (empty = off, the default).
- The target must be untracked in git. If any file under it is tracked, nothing is written.
- A folder Foundry installed carries `.foundry-skill.json` (source, hash, time) and is updated
  when the source changes. A folder without that marker and with different content (a hand
  copy) is first copied to `<state_dir>/skills_backup/<project>/<name>-<time>/`, then
  replaced. A skill removed from the list is removed only when it carries the marker.
- The kilo.jsonc agent blocks get an allow for each listed skill (allow_rules, through
  kilo_config.with_extra).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from typing import Optional

MARKER = ".foundry-skill.json"
DEFAULTS = {"install": [], "target": ".kilo/skills", "check_seconds": 300}
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)


def settings(profile) -> dict:
    raw = (getattr(profile, "raw", None) or {}).get("skills") or {}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: raw[k] for k in DEFAULTS if k in raw})
    names = out.get("install") or []
    if isinstance(names, str):
        names = [n for n in names.replace(",", " ").split() if n]
    out["install"] = [str(n).strip() for n in names if _ok_name(str(n).strip())]
    try:
        out["check_seconds"] = max(30, int(out.get("check_seconds") or 300))
    except (TypeError, ValueError):
        out["check_seconds"] = 300
    return out


def _ok_name(name: str) -> bool:
    return bool(name) and len(name) <= 64 and all(c.isalnum() or c in "-_" for c in name)


def sources(project_id: str) -> list[str]:
    """Where Foundry-shipped skills live, searched in this order."""
    return [os.path.join(_HERE, "projects", project_id, "kilo_skills"),
            os.path.join(_REPO, "contrib", "media-adapter", "skills"),
            os.path.join(_REPO, "contrib", "skills")]


def find_source(project_id: str, name: str) -> Optional[str]:
    for base in sources(project_id):
        d = os.path.join(base, name)
        if os.path.isfile(os.path.join(d, "SKILL.md")):
            return d
    return None


def _files(d: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for f in sorted(files):
            if f == MARKER or f.endswith((".pyc", ".tmp")):
                continue
            out.append(os.path.relpath(os.path.join(root, f), d).replace(os.sep, "/"))
    return out


def tree_hash(d: str) -> str:
    """Content hash of a skill folder (paths + bytes, newlines normalised, marker excluded)."""
    h = hashlib.sha256()
    for rel in _files(d):
        with open(os.path.join(d, rel), "rb") as f:
            data = f.read().replace(b"\r\n", b"\n")
        h.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


def _marker(d: str) -> Optional[dict]:
    try:
        with open(os.path.join(d, MARKER), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def tracked(workspace: str, target: str) -> list[str]:
    """Files under target that git tracks ([] = safe to write). None-safe on a non-repo."""
    try:
        r = subprocess.run(["git", "-C", workspace, "ls-files", "--", target],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [x for x in r.stdout.decode("utf-8", "replace").splitlines() if x.strip()]


def _copy_in(src: str, dst: str, meta: dict) -> None:
    """Replace dst with a copy of src (a sibling temp folder, then a swap)."""
    parent = os.path.dirname(dst)
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, f".{os.path.basename(dst)}.foundry-tmp")
    old = os.path.join(parent, f".{os.path.basename(dst)}.foundry-old")
    for p in (tmp, old):
        shutil.rmtree(p, ignore_errors=True)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", MARKER))
    with open(os.path.join(tmp, MARKER), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    if os.path.isdir(dst):
        os.replace(dst, old)
    os.replace(tmp, dst)
    shutil.rmtree(old, ignore_errors=True)


def sync(profile, state_dir: str, store=None, clock=time.time, force: bool = False) -> dict:
    """Install / update / remove the listed skills. Returns a JSON-able report; never raises
    for one bad skill (it is reported)."""
    cfg = settings(profile)
    pid = str(getattr(profile, "id", "") or "")
    ws = str(getattr(profile, "workspace", "") or "")
    target_rel = str(cfg["target"] or ".kilo/skills")
    out = {"target": "", "installed": [], "updated": [], "same": [], "removed": [],
           "replaced_hand_copy": [], "missing": [], "errors": [], "refused": ""}
    if not ws or not os.path.isdir(ws):
        out["refused"] = "no workspace"
        return out
    target = os.path.normpath(target_rel if os.path.isabs(target_rel)
                              else os.path.join(ws, target_rel))
    out["target"] = target
    if not cfg["install"] and not os.path.isdir(target):
        return out
    bad = tracked(ws, target)
    if bad:
        out["refused"] = (f"{len(bad)} file(s) under {target_rel} are tracked in git (e.g. "
                          f"{bad[0]}): installing there would change the game repo")
        _event(store, pid, "skills_install_refused", out)
        return out
    wanted = set(cfg["install"])
    now = clock()
    for name in cfg["install"]:
        src = find_source(pid, name)
        if not src:
            out["missing"].append(name)
            continue
        dst = os.path.join(target, name)
        try:
            h = tree_hash(src)
            meta = {"name": name, "source": os.path.relpath(src, _REPO).replace(os.sep, "/"),
                    "hash": h, "installed_at": now, "by": "foundry skills_install"}
            if not os.path.isdir(dst):
                _copy_in(src, dst, meta)
                out["installed"].append(name)
                continue
            m = _marker(dst)
            if tree_hash(dst) == h:
                if not m or m.get("hash") != h:
                    with open(os.path.join(dst, MARKER), "w", encoding="utf-8") as f:
                        json.dump(meta, f, indent=1)
                out["same"].append(name)
                continue
            if not m:
                bk = os.path.join(state_dir, "skills_backup", pid,
                                  f"{name}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}")
                shutil.copytree(dst, bk, dirs_exist_ok=True)
                out["replaced_hand_copy"].append({"name": name, "backup": bk})
            _copy_in(src, dst, meta)
            out["updated"].append(name)
        except OSError as e:
            out["errors"].append(f"{name}: {type(e).__name__}: {e}"[:300])
    if os.path.isdir(target):
        for name in sorted(os.listdir(target)):
            d = os.path.join(target, name)
            if name in wanted or name.startswith(".") or not os.path.isdir(d):
                continue
            if _marker(d):
                try:
                    shutil.rmtree(d)
                    out["removed"].append(name)
                except OSError as e:
                    out["errors"].append(f"{name}: {e}"[:300])
    if any(out[k] for k in ("installed", "updated", "removed", "errors", "missing")):
        _event(store, pid, "skills_installed", out)
    return out


def _event(store, pid: str, kind: str, detail: dict) -> None:
    if store is None:
        return
    try:
        store.event(pid, "", kind, detail=detail)
    except Exception:                                            # noqa: BLE001
        pass


def allow_rules(profile) -> list[dict]:
    """Kilo rules allowing the listed skills (kilo.jsonc skill maps deny '*' by default)."""
    return [{"permission": "skill", "pattern": n, "action": "allow"}
            for n in settings(profile)["install"]]


def due(profile, store, clock=time.time) -> bool:
    """Every check_seconds, or at once when the install list changed."""
    cfg = settings(profile)
    pid = str(getattr(profile, "id", "") or "")
    sig = hashlib.sha1(json.dumps([cfg["install"], cfg["target"]]).encode()).hexdigest()[:12]
    key = f"skills_install:{pid}"
    try:
        last = json.loads(store.meta(key) or "{}")
    except (ValueError, TypeError):
        last = {}
    if last.get("sig") == sig and clock() - float(last.get("t") or 0) < cfg["check_seconds"]:
        return False
    store.set_meta(key, json.dumps({"sig": sig, "t": clock()}))
    return True


def tick(engine, P) -> Optional[dict]:
    """The engine's hook: sync when due. Errors become an event, never a crash."""
    prof = getattr(P, "profile", None)
    if prof is None:
        return None
    try:
        if not due(prof, engine.store, engine.clock):
            return None
        cfg = settings(prof)
        if not cfg["install"]:
            # still clear skills Foundry installed earlier (the list was emptied)
            target = os.path.join(str(getattr(prof, "workspace", "") or ""), cfg["target"])
            if not os.path.isdir(target) or not any(
                    _marker(os.path.join(target, n)) for n in os.listdir(target)
                    if os.path.isdir(os.path.join(target, n))):
                return None
        return sync(prof, engine.state_dir, engine.store, engine.clock)
    except Exception as e:                                       # noqa: BLE001
        _event(engine.store, P.id, "skills_install_failed", {"error": f"{type(e).__name__}: {e}"})
        return None

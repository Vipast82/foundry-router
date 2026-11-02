"""Moving the work from one main workstation to another (docs/plans/LINUX_WORKSTATION_PLAN.md W4).

    foundry-client workstation export --out FILE     on the old workstation, coordinator stopped
    foundry-client workstation import FILE           on the new one (install/workstation.sh --import)

The export is ONE encrypted file (the router backup's format: scrypt + AES-256-GCM chunks,
foundry_router/backup.py) holding:
  * the coordinator's state: its SQLite DB (a consistent copy), the settings cache, the build
    journal, evidence, hand-offs, prep patches, the training spool. Not: logs, backups, repair
    worktrees, caches, the old PC's ssh key or its Windows credential files;
  * the local project.toml, every kilo.jsonc (the main checkout and each track's);
  * a `git bundle` of the game's commits that are not on its remote, plus every local branch's
    tip, so track branches travel even if they were never pushed;
  * a manifest: the old paths, branches and track worktrees.

It refuses while the coordinator runs, or when a checkout has uncommitted changes to tracked
files (it lists them: commit or stash first). Keys never travel: the new workstation gets its
own from enrollment. By default the old workstation then steps down: the router's
one-active-workstation record is released and its coordinator is switched off in client.toml.

Import restores the state and rewrites the old paths (Windows or Linux, any separator, also
inside the state DB and JSON) to the new layout, recreates the branches and the track worktrees,
and turns MCP servers that only run on Windows off in kilo.jsonc (it lists them).
"""
from __future__ import annotations

import getpass
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

FORMAT = 1
KIND = "foundry-workstation-export"
SKIP_DIRS = {"backups", "upgrade_backups", "lead_backups", "prep-backups", "repairs", "procs",
             "check_cache", "client-logs", "ssh", "cache", "restore", "kilo"}
SKIP_FILE = re.compile(r"(\.log|\.pid|\.heartbeat|\.xml|-wal|-shm|-journal|\.tmp)$|"
                       r"-before-[^/\\]*\.sqlite", re.I)
HEARTBEAT_FRESH_S = 90


class MoveError(RuntimeError):
    pass


def _git(cwd: str, *args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise MoveError(f"git {' '.join(args)} in {cwd}: {(r.stderr or r.stdout).strip()[:300]}")
    return r


# ----------------------------------------------------------------------------- paths

_SEP = r"(?:\\\\|\\|/)"                 # JSON-escaped backslash, backslash, slash
_TAIL = r"((?:" + _SEP + r"[^\s\"'<>|*?\\/:,;\]\)}]+)*)"


def _pattern(old: str) -> re.Pattern:
    parts = [p for p in re.split(r"[\\/]+", old.strip()) if p]
    lead = _SEP if old.startswith(("/", "\\")) else ""
    body = _SEP.join(re.escape(p) for p in parts)
    return re.compile(lead + body + r"(?=$|" + _SEP + r"|[\s\"'<>|,;\]\)}])" + _TAIL,
                      re.I if re.match(r"^[A-Za-z]:", old) else 0)


class Rewriter:
    """Old path prefixes -> new ones, in any separator style; the rest of each path gets '/'."""

    def __init__(self, mapping: dict[str, str]):
        pairs = sorted(((o, n) for o, n in mapping.items() if o and n and o != n),
                       key=lambda x: -len(x[0]))
        # new paths always with '/': safe inside JSON and TOML strings, and Windows takes them
        self.rules = [(_pattern(o), n.replace("\\", "/").rstrip("/")) for o, n in pairs]
        self.hits = 0

    def __call__(self, text):
        if not isinstance(text, str) or not text:
            return text
        out = text
        for pat, new in self.rules:
            def sub(m, new=new):
                return new + re.sub(_SEP, "/", m.group(1))
            out, n = pat.subn(sub, out)
            self.hits += n
        return out

    def hit(self, text) -> bool:
        return isinstance(text, str) and any(p.search(text) for p, _ in self.rules)


def rewrite_sqlite(path: str, rw: Rewriter) -> dict:
    """Every TEXT value in every ordinary table (FTS tables are skipped; their index is
    rebuilt from memory_* at the next memory refresh)."""
    con = sqlite3.connect(path)
    con.create_function("fw_rw", 1, rw, deterministic=True)
    con.create_function("fw_hit", 1, lambda v: 1 if rw.hit(v) else 0, deterministic=True)
    virtual = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL%'")]
    changed = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT "
                            "LIKE 'sqlite_%'").fetchall():
        if t in virtual or any(t.startswith(v + "_") for v in virtual):
            continue
        for col in con.execute(f'PRAGMA table_info("{t}")').fetchall():
            c = col[1]
            n = con.execute(f'UPDATE "{t}" SET "{c}" = fw_rw("{c}") WHERE typeof("{c}")=\'text\' '
                            f'AND fw_hit("{c}")').rowcount
            if n:
                changed[f"{t}.{c}"] = n
    con.commit()
    con.close()
    return changed


# ----------------------------------------------------------------------------- export

def _project(cfg: dict) -> str:
    from .manager import project_ids
    ids = project_ids(cfg["workspace"]["profiles"])
    p = cfg["workspace"].get("project") or (ids[0] if len(ids) == 1 else "")
    if not p:
        raise MoveError("which project? set workspace.project in the client's Settings")
    return p


def _worktrees(game: str) -> list[dict]:
    out, cur = [], {}
    for line in _git(game, "worktree", "list", "--porcelain").stdout.splitlines() + [""]:
        if not line:
            if cur.get("worktree"):
                out.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v or True
    return [{"path": w["worktree"], "head": w.get("HEAD") or "",
             "branch": str(w.get("branch") or "").removeprefix("refs/heads/")} for w in out]


def dirty(paths: list[str]) -> dict[str, list[str]]:
    """{checkout: [changed tracked files]} (untracked files are not checked)."""
    out = {}
    for p in paths:
        if not os.path.isdir(p):
            continue
        st = _git(p, "status", "--porcelain", "--untracked-files=no", check=False).stdout
        lines = [ln[3:] for ln in st.splitlines() if ln.strip()]
        if lines:
            out[p] = lines
    return out


def coordinator_running(state: str, clock: Callable[[], float] = time.time) -> bool:
    try:
        with open(os.path.join(state, "coordinator.heartbeat"), encoding="utf-8") as f:
            hb = json.load(f)
        return clock() - float(hb.get("ts") or 0) < HEARTBEAT_FRESH_S
    except (OSError, ValueError, TypeError):
        return False


def _state_files(state: str) -> list[tuple[str, str]]:
    out = []
    for dp, dns, fns in os.walk(state):
        rel = os.path.relpath(dp, state)
        if rel == ".":
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if SKIP_FILE.search(fn):
                continue
            full = os.path.join(dp, fn)
            out.append((os.path.relpath(full, state).replace("\\", "/"), full))
    return sorted(out)


def _sqlite_copy(src: str, dst: str) -> None:
    a = sqlite3.connect(f"file:{Path(src).as_posix()}?mode=ro", uri=True)
    b = sqlite3.connect(dst)
    with b:
        a.backup(b)
    a.close()
    b.close()


def _is_sqlite(p: str) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    ti = tarfile.TarInfo(name)
    ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o600
    tar.addfile(ti, io.BytesIO(data))


def export(out_path: str, passphrase: str, cfg_path: Optional[Path] = None,
           keep_active: bool = False, release: Optional[Callable[[str], dict]] = None,
           log: Callable[[str], None] = print) -> dict:
    """`cfg_path`: the client.toml to read and, unless keep_active, to switch the coordinator
    off in (None = this client's own)."""
    from . import config
    from foundry_router.backup import _EncryptingWriter
    cfg = config.load(cfg_path)
    ex = config.expand
    project = _project(cfg)
    game, state = ex(cfg["workspace"]["game"]), ex(cfg["client"]["state_dir"])
    profiles = ex(cfg["workspace"]["profiles"])
    if coordinator_running(state):
        raise MoveError("the coordinator is running: stop it first, once no sprint session is "
                        "running (Foundry Client → Status → Coordinator → Stop)")
    wts = _worktrees(game)
    bad = dirty([w["path"] for w in wts])
    if bad:
        lines = [f"  {p}: {', '.join(fs[:8])}{' …' if len(fs) > 8 else ''}" for p, fs in bad.items()]
        raise MoveError("uncommitted changes to tracked files (commit or stash them first):\n"
                        + "\n".join(lines))
    heads = {}
    for ln in _git(game, "for-each-ref", "--format=%(refname:short) %(objectname)",
                   "refs/heads").stdout.splitlines():
        b, _, sha = ln.partition(" ")
        heads[b] = sha
    remote = (cfg.get("workspace") or {}).get("remote") or "origin"
    kilo = [w["path"] for w in wts if os.path.isfile(os.path.join(w["path"], "kilo.jsonc"))]
    manifest = {"format": FORMAT, "kind": KIND, "made_ts": time.time(),
                "source": {"host": socket.gethostname(), "os": sys.platform},
                "project": project, "remote": remote,
                "paths": {"game": game, "state": state, "profiles": profiles,
                          "deploy": ex(cfg["workspace"].get("deploy") or ""),
                          "home": os.path.expanduser("~")},
                "heads": heads, "worktrees": wts, "kilo_jsonc": kilo}
    tmp = tempfile.mkdtemp(prefix="foundry-export-")
    try:
        bundle = os.path.join(tmp, "game.bundle")
        r = subprocess.run(["git", "-C", game, "bundle", "create", bundle, "--branches",
                            "--not", f"--remotes={remote}"], capture_output=True, text=True)
        has_bundle = r.returncode == 0 and os.path.isfile(bundle)
        if not has_bundle and "empty bundle" not in (r.stderr or "").lower():
            raise MoveError(f"git bundle failed: {(r.stderr or '').strip()[:300]}")
        manifest["bundle"] = has_bundle
        files = _state_files(state)
        with open(out_path, "wb") as fout:
            w = _EncryptingWriter(fout, passphrase, {"kind": KIND, "project": project,
                                                     "made_ts": manifest["made_ts"]})
            with tarfile.open(fileobj=w, mode="w|") as tar:
                _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=1).encode())
                for rel, full in files:
                    if _is_sqlite(full):
                        cp = os.path.join(tmp, "db.sqlite")
                        _sqlite_copy(full, cp)
                        tar.add(cp, arcname="state/" + rel)
                        os.remove(cp)
                    else:
                        tar.add(full, arcname="state/" + rel)
                ptoml = os.path.join(profiles, project, "project.toml")
                if os.path.isfile(ptoml):
                    tar.add(ptoml, arcname="profile/project.toml")
                for i, p in enumerate(kilo):
                    tar.add(os.path.join(p, "kilo.jsonc"), arcname=f"kilo/{i}.jsonc")
                if has_bundle:
                    tar.add(bundle, arcname="game.bundle")
            w.finish()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    res = {"file": out_path, "size": os.path.getsize(out_path), "state_files": len(files),
           "branches": len(heads), "worktrees": len(wts), "bundle": manifest["bundle"]}
    if not keep_active:
        cfg["components"]["coordinator"]["enabled"] = False
        config.save(cfg, cfg_path)
        res["coordinator_switched_off"] = True
        if release is not None:
            try:
                res["router"] = release(project)
            except Exception as e:                               # noqa: BLE001
                res["router"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    return res


# ----------------------------------------------------------------------------- import

WINDOWS_ONLY = re.compile(r"(\.exe|\.cmd|\.bat|\.ps1)$|^[A-Za-z]:[\\/]", re.I)


def _kilo_for_linux(text: str, rw: Rewriter, L: dict) -> tuple[str, list[str]]:
    from foundry_automation import jsonc
    d = jsonc.parse(rw(text))
    off = []
    for name, m in (d.get("mcp") or {}).items():
        cmd = m.get("command") if isinstance(m, dict) else None
        if name == "foundry-media" and isinstance(m, dict):
            m.update({"type": "local", "command": [
                L["python"], L["app"] + "/contrib/media-adapter/foundry_media_mcp.py",
                "--config", L["config"] + "/foundry-media.json"]})
            continue
        if isinstance(cmd, list) and cmd and any(WINDOWS_ONLY.search(str(c)) for c in cmd):
            if m.get("enabled", True):
                off.append(name)
            m["enabled"] = False
    return json.dumps(d, indent=2) + "\n", off


def import_(path: str, passphrase: str, root: str, cfg: Optional[dict] = None,
            log: Callable[[str], None] = print) -> dict:
    from . import config
    from .workstation import layout
    from foundry_router.backup import _DecryptingReader
    cfg = cfg or config.load()
    tmp = tempfile.mkdtemp(prefix="foundry-import-")
    try:
        with open(path, "rb") as f:
            r = _DecryptingReader(f, passphrase)
            if r.header.get("kind") != KIND:
                raise MoveError("this file is not a workstation export (a router backup?)")
            with tarfile.open(fileobj=io.BufferedReader(r, 1 << 20), mode="r|") as tar:
                for m in tar:
                    name = m.name
                    if name.startswith("/") or ".." in Path(name).parts or not m.isfile():
                        continue
                    dst = os.path.join(tmp, *name.split("/"))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with tar.extractfile(m) as src, open(dst, "wb") as out:
                        shutil.copyfileobj(src, out)
        with open(os.path.join(tmp, "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
        project = man["project"]
        L = layout(root, project)
        game, state = config.expand(cfg["workspace"]["game"]) or L["game"], \
            config.expand(cfg["client"]["state_dir"]) or L["state"]
        if not os.path.isdir(os.path.join(game, ".git")):
            raise MoveError(f"clone the game into {game} first (install/workstation.sh does)")
        if dirty([game]):
            raise MoveError(f"{game} has uncommitted changes: import needs a fresh clone")
        old = man["paths"]
        rw = Rewriter({old["game"]: game, old["state"]: state, old["profiles"]: L["profiles"],
                       old.get("deploy") or "": L["app"]})
        res = {"project": project, "from": man["source"], "rewrites": {}}
        # 1. the coordinator's state
        if os.path.exists(os.path.join(state, "coordinator.sqlite")):
            keep = os.path.join(state, f"before-import-{time.strftime('%Y%m%d-%H%M%S')}")
            os.makedirs(keep, exist_ok=True)
            for n in os.listdir(state):
                if n.startswith("before-import-"):
                    continue
                shutil.move(os.path.join(state, n), os.path.join(keep, n))
            res["previous_state"] = keep
        src_state = os.path.join(tmp, "state")
        n_files = 0
        for dp, _, fns in os.walk(src_state):
            for fn in fns:
                s = os.path.join(dp, fn)
                d = os.path.join(state, os.path.relpath(s, src_state))
                os.makedirs(os.path.dirname(d), exist_ok=True)
                if _is_sqlite(s):
                    shutil.copy2(s, d)
                    res["rewrites"].update(rewrite_sqlite(d, rw))
                elif fn.endswith((".json", ".toml", ".md", ".txt", ".jsonl", ".patch", ".diff")):
                    with open(s, encoding="utf-8", errors="surrogateescape") as f:
                        text = f.read()
                    with open(d, "w", encoding="utf-8", errors="surrogateescape") as f:
                        f.write(rw(text))
                else:
                    shutil.copy2(s, d)
                n_files += 1
        res["state_files"] = n_files
        # 2. git: the commits that were only on the old PC, then every local branch's tip
        if man.get("bundle"):
            _git(game, "fetch", "-q", os.path.join(tmp, "game.bundle"),
                 "refs/heads/*:refs/foundry-import/*")
        current = _git(game, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        made, missing = [], []
        for b, sha in sorted(man["heads"].items()):
            if _git(game, "cat-file", "-e", sha + "^{commit}", check=False).returncode != 0:
                missing.append(b)
                continue
            if b == current:
                _git(game, "reset", "-q", "--hard", sha)
            else:
                _git(game, "branch", "-f", b, sha)
            made.append(b)
        for ln in _git(game, "for-each-ref", "--format=%(refname)", "refs/foundry-import",
                       check=False).stdout.split():
            _git(game, "update-ref", "-d", ln, check=False)
        res["branches"], res["branches_missing"] = made, missing
        # 3. the track worktrees at their new paths
        wts = []
        for w in man["worktrees"]:
            newp = rw(w["path"])
            if os.path.normcase(newp.rstrip("/")) == os.path.normcase(game.rstrip("/")):
                continue
            if not w.get("branch") or os.path.exists(newp):
                continue
            os.makedirs(os.path.dirname(newp), exist_ok=True)
            r2 = _git(game, "worktree", "add", newp, w["branch"], check=False)
            wts.append({"path": newp, "branch": w["branch"], "ok": r2.returncode == 0,
                        "error": (r2.stderr or "").strip()[:200] if r2.returncode else ""})
        res["worktrees"] = wts
        # 4. project.toml and kilo.jsonc, rewritten (the generated ones are kept as .new-install)
        ptoml = os.path.join(tmp, "profile", "project.toml")
        if os.path.isfile(ptoml):
            dst = os.path.join(L["profiles"], project, "project.toml")
            if os.path.isfile(dst):
                shutil.copy2(dst, dst + ".new-install")
            with open(ptoml, encoding="utf-8") as f:
                text = rw(f.read())
            with open(dst, "w", encoding="utf-8") as f:
                f.write(text)
            res["project_toml"] = dst
        off_all = {}
        for i, oldp in enumerate(man.get("kilo_jsonc") or []):
            src = os.path.join(tmp, "kilo", f"{i}.jsonc")
            newp = rw(oldp)
            if not os.path.isfile(src) or not os.path.isdir(newp):
                continue
            with open(src, encoding="utf-8") as f:
                text, off = _kilo_for_linux(f.read(), rw, L)
            dst = os.path.join(newp, "kilo.jsonc")
            if os.path.isfile(dst):
                shutil.copy2(dst, dst + ".new-install")
            with open(dst, "w", encoding="utf-8") as f:
                f.write(text)
            if off:
                off_all[newp] = off
        res["kilo_servers_off"] = off_all
        res["path_rewrites"] = rw.hits
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------- CLI

def _passphrase(stdin: bool, confirm: bool) -> str:
    if stdin:
        return sys.stdin.readline().rstrip("\r\n")
    p = getpass.getpass("Export passphrase (12+ characters; typing is hidden): ")
    if confirm and getpass.getpass("Again: ") != p:
        raise MoveError("the two passphrases differ")
    return p


def _release_via_router(cfg: dict) -> Callable[[str], dict]:
    def call(project: str) -> dict:
        import urllib.request
        from . import secrets
        key = secrets.get("FOUNDRY_COORDINATOR_KEY") or ""
        body = json.dumps({"project": project, "name": socket.gethostname(),
                           "release": True}).encode()
        req = urllib.request.Request(
            cfg["client"]["backend_url"].rstrip("/") + "/v1/foundry/control/workstation-active",
            data=body, method="POST", headers={"Content-Type": "application/json",
                                               "Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    return call


def main(a) -> int:
    from . import config, secrets
    cfg = config.load()
    secrets.configure(cfg)
    try:
        if a.cmd == "export":
            res = export(a.out, _passphrase(a.passphrase_stdin, True),
                         keep_active=a.keep_active, release=_release_via_router(cfg))
        else:
            res = import_(a.file, _passphrase(a.passphrase_stdin, False), a.root, cfg)
    except MoveError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    except Exception as e:                                       # noqa: BLE001
        if type(e).__name__ == "BackupError":
            print(f"✗ {e}", file=sys.stderr)
            return 1
        raise
    print(json.dumps(res, indent=2, default=str))
    return 0

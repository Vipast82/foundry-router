"""Lune result reuse by input content (never rerun passed work).

A Lune run's key is a hash of everything the suite can read, taken at the
revision being verified:
  - git object ids of src, tests, default.project.json, wally.lock, wally.toml,
    rokit.toml (the runner tests/lune/run.luau and tests/lib are inside tests);
  - every `$path` in default.project.json (the Loader maps the place tree onto
    them; Packages/ServerPackages are gitignored and hashed from disk);
  - every path a string literal in a Lune script (tests/lune, *.luau outside src)
    names under another top-level entry ("docs/progress/" .. id, "assets/models/
    model_ids.json"), at the deepest prefix that exists, so a dynamic file name
    keys its whole folder;
  - the lune version, and (mode_key) the mode: normal or negative.
A key is only used when the workspace HEAD is the revision and none of those
paths has modified or untracked files (gitignored files inside them are not
inputs; Packages/ServerPackages are, by content).
Only PASS results are stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time

KEY_VERSION = 1
FIXED_INPUTS = ("src", "tests", "default.project.json", "wally.lock", "wally.toml", "rokit.toml")
# only Lune scripts can read files (code the Loader loads gets no @lune/fs)
SCAN_PATHS = ("tests/lune", ":(glob)**/*.luau", ":(exclude)src")
MAX_ENTRIES = 200
DISK_MAX_FILES = 20000
DISK_MAX_BYTES = 512 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_lock = threading.Lock()


def _git(ws: str, *args, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", ws, *args], capture_output=True, text=True,
                          input=input, encoding="utf-8", errors="replace", timeout=120)


def _project_paths(ws: str, rev: str) -> list[str]:
    r = _git(ws, "show", f"{rev}:default.project.json")
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return []
    out = []

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "$path" and isinstance(x, str):
                    out.append(x)
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(data)
    return out


def _literal_paths(ws: str, rev: str, tops: list[str]) -> list[str]:
    names = [n for n in tops if _NAME.match(n) and n not in ("src", "tests", ".git")]
    if not names:
        return []
    alt = "|".join(n.replace(".", "[.]") for n in names)
    q = "[\"'`]"
    pat = f"{q}({alt})(/[^\"'`]*)?{q}"
    r = _git(ws, "grep", "-h", "-o", "-I", "-E", pat, rev, "--", *SCAN_PATHS)
    if r.returncode not in (0, 1):
        raise RuntimeError("git grep: " + r.stderr.strip()[:200])
    return sorted({ln.strip()[1:-1] for ln in r.stdout.splitlines() if len(ln.strip()) >= 2})


def _norm(p: str) -> str:
    p = p.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def _prefixes(p: str) -> list[str]:
    parts = [x for x in p.split("/") if x]
    return ["/".join(parts[:i]) for i in range(len(parts), 0, -1)]


def _disk_hash(path: str) -> str | None:
    """Content hash of a file or folder on disk (gitignored inputs); None past the caps."""
    h = hashlib.sha256()
    if os.path.isfile(path):
        with open(path, "rb") as f:
            h.update(f.read())
        return "disk:" + h.hexdigest()
    files, total = 0, 0
    for root, dirs, names in os.walk(path):
        dirs.sort()
        for n in sorted(names):
            fp = os.path.join(root, n)
            rel = os.path.relpath(fp, path).replace("\\", "/")
            try:
                size = os.path.getsize(fp)
                files += 1
                total += size
                if files > DISK_MAX_FILES or total > DISK_MAX_BYTES:
                    return None
                with open(fp, "rb") as f:
                    data = f.read()
            except OSError:
                return None
            h.update(rel.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return "disk:" + h.hexdigest()


def input_key(ws: str, revision: str, lune_version: str) -> dict:
    """{"key", "paths", "clean", "why"}; key is None when the inputs can't be read
    or the working tree doesn't match the revision for them."""
    res = {"key": None, "paths": {}, "clean": False, "why": ""}
    try:
        head = _git(ws, "rev-parse", "HEAD").stdout.strip()
        rev = _git(ws, "rev-parse", "--verify", f"{revision}^{{commit}}").stdout.strip()
        if not rev:
            res["why"] = f"revision {revision[:12]} is not a commit here"
            return res
        if head != rev:
            res["why"] = f"workspace HEAD {head[:12]} is not {rev[:12]}"
            return res
        r = _git(ws, "ls-tree", "--name-only", rev)
        if r.returncode != 0:
            res["why"] = "git ls-tree failed"
            return res
        tree_tops = r.stdout.split()
        try:
            disk_tops = os.listdir(ws)
        except OSError:
            disk_tops = []
        tops = sorted(set(tree_tops) | set(disk_tops))
        wanted = {_norm(p) for p in (*FIXED_INPUTS, *_project_paths(ws, rev),
                                     *_literal_paths(ws, rev, tops))}
        wanted = {p for p in wanted if p and ".." not in p.split("/")}
        cands = sorted({x for p in wanted for x in _prefixes(p)})
        r = _git(ws, "cat-file", "--batch-check", input="".join(f"{rev}:{c}\n" for c in cands))
        lines = r.stdout.splitlines()
        if r.returncode != 0 or len(lines) != len(cands):
            res["why"] = "git cat-file failed"
            return res
        oid = {c: ln.split()[0] for c, ln in zip(cands, lines) if not ln.endswith(" missing")}
        chosen: dict[str, str] = {}
        for p in sorted(wanted):
            hit = next((x for x in _prefixes(p) if x in oid), None)
            if hit is not None:
                chosen[hit] = oid[hit]
                continue
            # not in the commit: an ignored/untracked input (Packages) or absent
            disk = next((x for x in _prefixes(p) if os.path.exists(os.path.join(ws, x))), None)
            if disk is None:
                chosen[p] = "-"
            else:
                d = _disk_hash(os.path.join(ws, disk))
                if d is None:
                    res["why"] = f"{disk} is too large to hash"
                    return res
                chosen[disk] = d
        # a path inside another chosen tree path adds nothing
        keys = sorted(chosen)
        paths = {k: v for k, v in chosen.items()
                 if not any(k.startswith(o + "/") and not chosen[o].startswith(("disk:", "-"))
                            for o in keys if o != k)}
        res["paths"] = paths
        tracked = [k for k, v in paths.items() if not v.startswith(("disk:", "-"))]
        st = _git(ws, "status", "--porcelain", "--untracked-files=all", "--", *tracked)
        if st.returncode != 0:
            res["why"] = "git status failed"
            return res
        dirty = [ln[3:] for ln in st.stdout.splitlines() if ln.strip()]
        if dirty:
            res["why"] = "working tree differs from the revision: " + ", ".join(dirty[:5])
            return res
        res["clean"] = True
        blob = json.dumps({"v": KEY_VERSION, "lune": lune_version.strip(),
                           "paths": paths}, sort_keys=True)
        res["key"] = hashlib.sha256(blob.encode()).hexdigest()
        return res
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:
        res["why"] = f"inputs unreadable: {e}"[:300]
        return res


def mode_key(info: dict | None, mode: str) -> str | None:
    """The cache key of one run (normal / negative) over these inputs."""
    if not info or not info.get("key"):
        return None
    return hashlib.sha256(f"{info['key']}:{mode}".encode()).hexdigest()


def cache_path(state_dir: str, project: str) -> str:
    return os.path.join(state_dir, "cache", "lune", re.sub(r"[^\w.-]", "_", project) + ".json")


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get(path: str, key: str) -> dict | None:
    with _lock:
        e = _load(path).get(key)
    return e if isinstance(e, dict) and e.get("result") == "PASS" else None


def put(path: str, key: str, entry: dict) -> None:
    """Store a PASS entry; anything else drops the key."""
    with _lock:
        data = _load(path)
        if entry.get("result") == "PASS":
            data[key] = {**entry, "ts": time.time()}
        else:
            data.pop(key, None)
        if len(data) > MAX_ENTRIES:
            for k in sorted(data, key=lambda k: data[k].get("ts", 0))[:len(data) - MAX_ENTRIES]:
                data.pop(k)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)

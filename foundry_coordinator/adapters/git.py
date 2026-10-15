"""Git operations on a project workspace (read-mostly; one push helper)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from typing import Optional


class GitError(RuntimeError):
    pass


class Workspace:
    def __init__(self, path: str):
        self.path = path

    def run(self, *args, check: bool = True, timeout: int = 120) -> str:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        try:
            r = subprocess.run(["git", "-C", self.path, *args], capture_output=True, text=True,
                               timeout=timeout, env=env, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            raise GitError(f"git {' '.join(args)} timed out") from None
        if check and r.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:400]}")
        return r.stdout

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").strip()

    def branch(self) -> str:
        return self.run("rev-parse", "--abbrev-ref", "HEAD").strip()

    def is_ancestor(self, a: str, b: str) -> bool:
        return subprocess.run(["git", "-C", self.path, "merge-base", "--is-ancestor", a, b],
                              capture_output=True).returncode == 0

    def changed_files(self, base: str, head: str) -> list[str]:
        if base == head:
            return []
        out = self.run("diff", "--name-only", "--no-renames", f"{base}..{head}")
        return sorted({l.strip() for l in out.splitlines() if l.strip()})

    def commits(self, base: str, head: str) -> list[tuple[str, str]]:
        if base == head:
            return []
        out = self.run("log", "--format=%H %s", f"{base}..{head}")
        return [tuple(l.split(" ", 1)) if " " in l else (l, "") for l in out.splitlines()]

    def diff(self, base: str, head: str, max_bytes: int = 60000) -> tuple[str, bool]:
        out = self.run("diff", "--stat", "--patch", "--no-color", f"{base}..{head}")
        if len(out.encode()) > max_bytes:
            return out.encode()[:max_bytes].decode(errors="ignore"), True
        return out, False

    def status(self) -> list[tuple[str, str]]:
        """[(XY, path)] for modified/untracked files (porcelain v1)."""
        out = self.run("status", "--porcelain", "--untracked-files=all")
        res = []
        for line in out.splitlines():
            if len(line) < 4:
                continue
            p = line[3:].strip().strip('"')
            if " -> " in p:
                p = p.split(" -> ")[-1]
            res.append((line[:2], p))
        return res

    def file_hash(self, rel: str) -> str:
        p = os.path.join(self.path, rel)
        if not os.path.exists(p):
            return "missing"
        if os.path.isdir(p):
            return "dir"
        h = hashlib.sha256()
        with open(p, "rb") as f:
            h.update(f.read())
        return h.hexdigest()[:16]

    def remote_head(self, remote: str, branch: str) -> Optional[str]:
        out = self.run("ls-remote", remote, f"refs/heads/{branch}", timeout=60)
        line = out.strip().splitlines()
        return line[0].split()[0] if line else None

    def push(self, remote: str, branch: str) -> str:
        """One plain push of the current branch (never --force, never tags)."""
        return self.run("push", remote, f"HEAD:refs/heads/{branch}", timeout=300)

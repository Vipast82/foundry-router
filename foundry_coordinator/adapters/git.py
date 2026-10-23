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

    def run(self, *args, check: bool = True, timeout: int = 120,
            env: Optional[dict] = None) -> str:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", **(env or {}))
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

    # -- immutable reads at a revision (review packets never read the working tree) --

    def show_at(self, revision: str, rel: str) -> Optional[str]:
        """The file's text at `revision`, or None when it doesn't exist there."""
        r = subprocess.run(["git", "-C", self.path, "show", f"{revision}:{rel}"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120)
        return r.stdout if r.returncode == 0 else None

    def blob_at(self, revision: str, rel: str) -> Optional[str]:
        """The git blob id of the file at `revision` (provenance), or None."""
        r = subprocess.run(["git", "-C", self.path, "rev-parse", f"{revision}:{rel}"],
                           capture_output=True, text=True, timeout=60)
        return r.stdout.strip() if r.returncode == 0 else None

    def ls_tree_at(self, revision: str, rel_dir: str) -> list[tuple[str, str]]:
        """[(path, blob)] of files under `rel_dir` at `revision` (recursive)."""
        out = self.run("ls-tree", "-r", revision, "--", rel_dir.rstrip("/") + "/", check=False)
        res = []
        for line in out.splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) >= 3 and parts[1] == "blob":
                res.append((path, parts[2]))
        return res

    def diff_files(self, base: str, head: str) -> list[dict]:
        """Per-file change sizes base..head: [{path, added, deleted, binary}]."""
        if base == head:
            return []
        out = self.run("diff", "--numstat", "--no-renames", f"{base}..{head}")
        res = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, d, path = parts
                res.append({"path": path, "added": int(a) if a.isdigit() else 0,
                            "deleted": int(d) if d.isdigit() else 0, "binary": a == "-"})
        return res

    def file_diff(self, base: str, head: str, rel: str) -> str:
        """The patch for one file base..head."""
        return self.run("diff", "--patch", "--no-color", "--no-renames", f"{base}..{head}",
                        "--", rel)

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

    def tree(self, revision: str) -> Optional[str]:
        """The root tree id of `revision` (a content address: equal trees = equal code)."""
        r = subprocess.run(["git", "-C", self.path, "rev-parse", f"{revision}^{{tree}}"],
                           capture_output=True, text=True, timeout=60)
        return r.stdout.strip() if r.returncode == 0 else None

    def commit_files(self, base: str, head: str) -> list[tuple[str, list[str]]]:
        """[(sha, [files])] for each commit in base..head (newest first)."""
        if base == head:
            return []
        out = self.run("log", "--format=@@%H", "--name-only", "--no-renames", f"{base}..{head}")
        res: list[tuple[str, list[str]]] = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("@@"):
                res.append((line[2:], []))
            elif line and res:
                res[-1][1].append(line)
        return res

    def commit_paths(self, paths: list[str], message: str, env: Optional[dict] = None) -> str:
        """Commit exactly `paths` (added, modified or deleted) with `message`; other
        staged or dirty files are left alone. Returns the new HEAD, or "" when the
        paths had nothing to commit. Never --no-verify, never amend. `env` adds
        variables to the commit (the repo's hooks read their overrides there)."""
        paths = [p for p in dict.fromkeys(paths) if p]
        if not paths:
            return ""
        import tempfile
        before = self.head()
        # a pathspec file, not argv: ~500 capture paths overflow the Windows command line
        fd, spec = tempfile.mkstemp(prefix="foundry-pathspec-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(paths) + "\n")
            self.run("add", "-A", f"--pathspec-from-file={spec}")
            want = {p.replace("\\", "/") for p in paths}
            staged = [p for p in self.run("diff", "--cached", "--name-only",
                                          "--no-renames").splitlines() if p.strip() in want]
            if not staged:
                return ""
            self.run("commit", "-q", "-m", message, f"--pathspec-from-file={spec}", env=env)
        finally:
            try:
                os.remove(spec)
            except OSError:
                pass
        after = self.head()
        return after if after != before else ""

    def remote_head(self, remote: str, branch: str) -> Optional[str]:
        out = self.run("ls-remote", remote, f"refs/heads/{branch}", timeout=60)
        line = out.strip().splitlines()
        return line[0].split()[0] if line else None

    def push(self, remote: str, branch: str) -> str:
        """One plain push of the current branch (never --force, never tags)."""
        return self.run("push", remote, f"HEAD:refs/heads/{branch}", timeout=300)

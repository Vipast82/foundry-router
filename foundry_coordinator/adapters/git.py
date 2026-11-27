"""Git operations on a project workspace (read-mostly; one push helper)."""

from __future__ import annotations
import foundry_platform as _plat

import hashlib
import os
import subprocess
from typing import Optional


class GitError(RuntimeError):
    pass


LONGPATHS = "'core.longpaths'='true'"


def git_env(base: Optional[dict] = None, **extra) -> dict:
    """The environment for a coordinator git command: `base` (os.environ by default) plus
    `extra`, with core.longpaths on (Windows only). 2026-11-22: a worktree deeper than
    about 178 characters made git die with "failed to stat '<sha>..<sha>': Filename too
    long" - git checks whether a revision argument is also a file, and on Windows that
    check fails past 260 characters unless core.longpaths is set, also inside git's own
    rebase and merge, where no "--" can be added. Passed as GIT_CONFIG_PARAMETERS
    (appended to what is already there), so argv and the repo's config stay as they are;
    the AU gate's GIT_CONFIG_COUNT keys are a separate list and are untouched."""
    env = dict(os.environ if base is None else base, **extra)
    if _plat.is_windows():
        have = env.get("GIT_CONFIG_PARAMETERS", "")
        if "core.longpaths" not in have.lower():
            env["GIT_CONFIG_PARAMETERS"] = f"{have} {LONGPATHS}".strip()
    return env


class Workspace:
    def __init__(self, path: str):
        self.path = path

    def run(self, *args, check: bool = True, timeout: int = 120,
            env: Optional[dict] = None) -> str:
        env = git_env(None, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", **(env or {}))
        try:
            from ..crashguard import note_command
            note_command(["git", "-C", self.path, *args])
        except Exception:                                        # noqa: BLE001
            pass
        try:
            rc, out, err = self._proc(list(args), timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise GitError(f"git {' '.join(args)} timed out") from None
        if check and rc != 0:
            raise GitError(f"git {' '.join(args)}: {(err or out).strip()[:400]}")
        return out

    def _proc(self, args: list, timeout: Optional[float] = None, env: Optional[dict] = None,
              input: Optional[str] = None) -> tuple:
        """(returncode, stdout, stderr) of `git -C <path> args`: the ONE place this workspace
        runs git (Distributed Foundry phase b: RemoteWorkspace replaces it with the slot's
        lane agent). Raises subprocess.TimeoutExpired like subprocess.run."""
        r = subprocess.run(["git", "-C", self.path, *args], capture_output=True, text=True,
                           timeout=timeout, env=env, encoding="utf-8", errors="replace",
                           input=input)
        return r.returncode, r.stdout, r.stderr

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").strip()

    def branch(self) -> str:
        return self.run("rev-parse", "--abbrev-ref", "HEAD").strip()

    def is_ancestor(self, a: str, b: str) -> bool:
        return self._proc(["merge-base", "--is-ancestor", a, b], env=git_env())[0] == 0

    def changed_files(self, base: str, head: str) -> list[str]:
        if base == head:
            return []
        out = self.run("diff", "--name-only", "--no-renames", f"{base}..{head}", "--")
        return sorted({l.strip() for l in out.splitlines() if l.strip()})

    def commits(self, base: str, head: str) -> list[tuple[str, str]]:
        if base == head:
            return []
        out = self.run("log", "--format=%H %s", f"{base}..{head}", "--")
        return [tuple(l.split(" ", 1)) if " " in l else (l, "") for l in out.splitlines()]

    def diff(self, base: str, head: str, max_bytes: int = 60000) -> tuple[str, bool]:
        out = self.run("diff", "--stat", "--patch", "--no-color", f"{base}..{head}", "--")
        if len(out.encode()) > max_bytes:
            return out.encode()[:max_bytes].decode(errors="ignore"), True
        return out, False

    # -- immutable reads at a revision (review packets never read the working tree) --

    def show_at(self, revision: str, rel: str) -> Optional[str]:
        """The file's text at `revision`, or None when it doesn't exist there."""
        rc, out, _ = self._proc(["show", f"{revision}:{rel}"], timeout=120, env=git_env())
        return out if rc == 0 else None

    def blob_at(self, revision: str, rel: str) -> Optional[str]:
        """The git blob id of the file at `revision` (provenance), or None."""
        rc, out, _ = self._proc(["rev-parse", f"{revision}:{rel}"], timeout=60, env=git_env())
        return out.strip() if rc == 0 else None

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
        out = self.run("diff", "--numstat", "--no-renames", f"{base}..{head}", "--")
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
        st = self.stat([rel]).get(rel) or {}
        if not st.get("exists"):
            return "missing"
        if st.get("kind") == "dir":
            return "dir"
        h = hashlib.sha256()
        h.update(self.read(rel) or b"")
        return h.hexdigest()[:16]

    # -- files in the working tree (phase b: the same four calls reach a slot's lane) --

    def read(self, rel: str, max_bytes: Optional[int] = None) -> Optional[bytes]:
        """The file's bytes (at most `max_bytes`), or None when it is not a file."""
        p = os.path.join(self.path, rel)
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as f:
            return f.read() if max_bytes is None else f.read(max_bytes)

    def stat(self, rels: list) -> dict:
        """{rel: {"exists", "kind" (file|dir|other), "size", "mtime"}} for each path."""
        out = {}
        for rel in rels:
            p = os.path.join(self.path, rel)
            try:
                st = os.stat(p)
            except OSError:
                out[rel] = {"exists": False, "kind": "", "size": 0, "mtime": 0.0}
                continue
            kind = "dir" if os.path.isdir(p) else ("file" if os.path.isfile(p) else "other")
            out[rel] = {"exists": True, "kind": kind, "size": st.st_size, "mtime": st.st_mtime}
        return out

    def exists(self, rel: str) -> bool:
        return bool((self.stat([rel]).get(rel) or {}).get("exists"))

    def write(self, rel: str, data: bytes) -> None:
        """Write a file the coordinator itself makes in the working tree (atomic replace)."""
        p = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = f"{p}.tmp{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)

    def tree(self, revision: str) -> Optional[str]:
        """The root tree id of `revision` (a content address: equal trees = equal code)."""
        rc, out, _ = self._proc(["rev-parse", f"{revision}^{{tree}}"], timeout=60, env=git_env())
        return out.strip() if rc == 0 else None

    def operator_commits(self, base: str, head: str) -> set:
        """Commits in base..head made from an operator's Claude Code chat: their message has
        a `Co-Authored-By: Claude ...` trailer, which sprint sessions never write."""
        if base == head:
            return set()
        out = self.run("log", "--format=%H%x1f%(trailers:key=Co-Authored-By,valueonly,"
                              "separator=%x1e)", f"{base}..{head}", "--")
        res = set()
        for line in out.splitlines():
            sha, _, who = line.partition("\x1f")
            if any(w.strip().lower().startswith("claude") for w in who.split("\x1e")):
                res.add(sha.strip())
        return res

    def commit_files(self, base: str, head: str) -> list[tuple[str, list[str]]]:
        """[(sha, [files])] for each commit in base..head (newest first)."""
        if base == head:
            return []
        out = self.run("log", "--format=@@%H", "--name-only", "--no-renames", f"{base}..{head}",
                       "--")
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
            try:
                self.run("commit", "-q", "-m", message, f"--pathspec-from-file={spec}", env=env)
            except (GitError, OSError) as e:
                # audit D: a commit a hook refused left the paths staged, and the next
                # track merge (or any later commit) stalled on them. Unstage exactly them.
                try:
                    self.run("reset", "-q", f"--pathspec-from-file={spec}", check=False)
                except (GitError, OSError):
                    pass
                raise GitError(f"{e} (the coordinator unstaged the {len(paths)} path(s) "
                               f"again)") from e
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

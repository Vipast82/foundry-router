"""A lane's workspace on a Studio slot (Distributed Foundry phase b, B3).

docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md. A lane whose worktree lives on a slot gets a
`RemoteWorkspace` instead of a `Workspace`: the same methods and return values, but git runs
in the slot's worktree through the slot's lane agent (foundry_client/lane.py), reached
through the lane tunnel (lane_tunnel.py). Everything above it (engine, scope, commits,
reviews) is unchanged: it calls `P.ws.*` as it always has.

    LaneAgent(endpoint)      POST /api/lane/{git,read,stat,write}, header X-Lane-Token
    RemoteWorkspace(agent, project, lane, path)

`endpoint` is a callable returning {"url", "token"} (or None while the tunnel is down), so a
tunnel that comes back on new local ports is picked up on the next call.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from typing import Callable, Optional

from .git import GitError, Workspace

HTTP_SLACK_S = 15          # the HTTP call may take this much longer than the git timeout


class LaneUnreachable(GitError):
    """The slot's lane agent could not be reached (tunnel down, slot away)."""


class LaneHTTPError(GitError):
    """The lane agent answered with an HTTP error (`status`: 401 wrong token, 404 no such
    lane or run, 409 refused, ...)."""

    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = int(status or 0)


def http_status(e: Exception) -> int:
    """The HTTP status a lane call failed with (0 = none: unreachable or not HTTP). Also
    reads "HTTP 404" from a plain GitError's text (test agents raise those)."""
    st = getattr(e, "status", None)
    if isinstance(st, int) and st:
        return st
    import re
    m = re.search(r"\bHTTP (\d{3})\b", str(e))
    return int(m.group(1)) if m else 0


class LaneAgent:
    """`refresh(name, used_token) -> bool`: called once when the agent answers 401 (it
    restarted and made a new token); True when new secrets were read, and the call is sent
    again. Default: the lane tunnel manager re-reads the slot's secrets."""

    def __init__(self, endpoint: Callable[[], Optional[dict]], name: str = "slot",
                 refresh: Optional[Callable] = None):
        self.endpoint = endpoint
        self.name = name
        self.refresh = refresh

    def call(self, route: str, body: dict, timeout: float = 60) -> dict:
        try:
            return self._call(route, body, timeout)
        except LaneHTTPError as e:
            if e.status != 401 or not self._refresh(getattr(e, "token", "")):
                raise
        return self._call(route, body, timeout)          # once, with the new token

    def _refresh(self, used_token: str) -> bool:
        fn = self.refresh
        if fn is None:
            from .. import lane_tunnel
            fn = lane_tunnel.MANAGER.refresh
        try:
            return bool(fn(self.name, used_token))
        except Exception:                                         # noqa: BLE001
            return False

    def _call(self, route: str, body: dict, timeout: float) -> dict:
        ep = self.endpoint() or {}
        if not ep.get("url"):
            raise LaneUnreachable(f"{self.name}: the lane tunnel is down")
        token = ep.get("token") or ""
        req = urllib.request.Request(
            ep["url"].rstrip("/") + "/api/lane/" + route, method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Lane-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read() or b"{}").get("error") or ""
            except ValueError:
                why = ""
            err = LaneHTTPError(f"{self.name} lane agent: HTTP {e.code} {why}".strip(), e.code)
            err.token = token                   # which token was refused (refresh compares)
            raise err from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise LaneUnreachable(f"{self.name}: lane agent unreachable: {e}"[:300]) from None


class RemoteWorkspace(Workspace):
    """`Workspace` for a lane on a slot. `path` is the worktree's path ON THE SLOT (what the
    coding sessions get as their directory)."""

    def __init__(self, agent: LaneAgent, project: str, lane: str, path: str,
                 mirror: str = "", on_commit=None, fetch=None):
        super().__init__(path)
        self.agent = agent
        self.project = project
        self.lane = lane
        # phase c: the lane's read mirror on this machine (lane_fetch.mirror_path), where the
        # coordinator's own reads and file writes land; on_commit(sha) brings the new tip back;
        # fetch() brings it back before a write is judged against the mirror (2026-11-27)
        self.mirror = mirror
        self.on_commit = on_commit
        self.fetch = fetch

    def _where(self) -> dict:
        return {"project": self.project, "lane": self.lane}

    def _proc(self, args: list, timeout: Optional[float] = None, env: Optional[dict] = None,
              input: Optional[str] = None) -> tuple:
        t = float(timeout) if timeout else 300.0
        out = self.agent.call("git", {**self._where(), "argv": [str(a) for a in args],
                                      "timeout": t, "env": _forwarded(env), "input": input},
                              timeout=t + HTTP_SLACK_S)
        if out.get("timeout"):
            raise subprocess.TimeoutExpired(["git", *args], t)
        return int(out.get("rc", 1)), str(out.get("stdout") or ""), str(out.get("stderr") or "")

    def read(self, rel: str, max_bytes: Optional[int] = None) -> Optional[bytes]:
        """The file's bytes (at most `max_bytes`), or None when it is not a file. A file bigger
        than one agent reply (8 MB) comes in pieces; a file the agent can only send part of
        (an older agent, or it changed while it was read) raises GitError, never a short
        copy."""
        out = self.agent.call("read", {**self._where(), "path": rel, "max_bytes": max_bytes})
        if not out.get("exists"):
            return None
        data = base64.b64decode(out.get("data") or "")
        if not out.get("truncated") or (max_bytes is not None and len(data) >= max_bytes):
            return data
        if "size" not in out:
            raise GitError(f"{rel}: the slot's lane agent sent only the first {len(data)} bytes "
                           f"(it has no ranged reads: update the Foundry Client there)")
        size = int(out["size"])
        want = size if max_bytes is None else min(size, int(max_bytes))
        parts, got = [data], len(data)
        while got < want:
            nxt = self.agent.call("read", {**self._where(), "path": rel, "offset": got,
                                           "max_bytes": None if max_bytes is None
                                           else want - got})
            chunk = base64.b64decode(nxt.get("data") or "") if nxt.get("exists") else b""
            if not chunk or int(nxt.get("offset", -1)) != got or int(nxt.get("size", -1)) != size:
                raise GitError(f"{rel}: changed on the slot while it was read "
                               f"({got} of {size} bytes)")
            parts.append(chunk)
            got += len(chunk)
        return b"".join(parts)[:want]

    def stat(self, rels: list) -> dict:
        out = self.agent.call("stat", {**self._where(), "paths": [str(r) for r in rels]})
        return dict(out.get("stats") or {})

    def write(self, rel: str, data: bytes) -> None:
        if self.mirror and os.path.isdir(self.mirror):
            # never over a newer edit on the slot: the mirror is brought up to the slot's
            # commit first, and the slot's file must be the one that commit has (or the one the
            # coordinator itself wrote last)
            bad = self._conflicts([rel.replace("\\", "/")])
            if bad:
                raise GitError(f"not writing {rel} on {self.agent.name}: it changed there since "
                               f"the last commit the coordinator has (a session's edit is "
                               f"kept)")
        self.agent.call("write", {**self._where(), "path": rel,
                                  "data": base64.b64encode(bytes(data)).decode("ascii")})
        if self.mirror:                         # the coordinator reads it here too
            Workspace(self.mirror).write(rel, data)

    def _same_head(self) -> str:
        """The slot's HEAD, once the mirror holds it too (a fetch when it does not). GitError
        when the mirror cannot reach it: the coordinator never judges the slot's files from an
        older copy."""
        local = Workspace(self.mirror)
        there = self.head()
        if local.head() != there and callable(self.fetch):
            try:
                self.fetch()
            except Exception:                                     # noqa: BLE001
                pass
        here = local.head()
        if here != there:
            raise GitError(f"the read mirror is at {here[:12]}, {self.agent.name}'s lane at "
                           f"{there[:12]}: not writing over the slot from an older copy")
        return there

    def _blobs(self, paths: list) -> tuple:
        """({path: blob at HEAD}, {path: the mirror's file blob}, {path: the slot's file blob})
        for `paths` (a path missing on a side has no entry)."""
        local = Workspace(self.mirror)
        head = {}
        out = local.run("ls-tree", "-r", "-z", "HEAD", "--", *paths, check=False)
        for ent in out.split("\0"):
            meta, _, p = ent.partition("\t")
            parts = meta.split()
            if len(parts) >= 3 and parts[1] == "blob" and p in paths:
                head[p] = parts[2]
        mine_present = [p for p in paths if os.path.isfile(os.path.join(self.mirror, p))]
        mine = {}
        if mine_present:
            hs = local.run("hash-object", "--", *mine_present).split()
            if len(hs) == len(mine_present):
                mine = dict(zip(mine_present, hs))
        st = self.stat(paths)
        there_present = [p for p in paths if (st.get(p) or {}).get("kind") == "file"]
        theirs = {}
        if there_present:
            hs = self.run("hash-object", "--", *there_present).split()
            if len(hs) != len(there_present):
                raise GitError(f"hash-object on {self.agent.name} gave {len(hs)} ids for "
                               f"{len(there_present)} files")
            theirs = dict(zip(there_present, hs))
        return head, mine, theirs

    def _conflicts(self, paths: list) -> list:
        """The paths whose file on the slot is neither the one at the commit both sides hold
        nor the mirror's own (what the coordinator wrote): sending the mirror's would undo a
        session's edit there."""
        self._same_head()
        head, mine, theirs = self._blobs(paths)
        return [p for p in paths if theirs.get(p) != head.get(p) and
                not (p in theirs and theirs.get(p) == mine.get(p))]

    def _upload_mirror_changes(self, paths: list) -> list:
        """Files the coordinator wrote into the mirror (they show as changed there: sessions
        never write in the mirror) go to the slot before a commit of those paths, so a write
        made here is never lost. Only over a slot file the mirror's commit has (or that is new
        on both sides): one a session changed on the slot since is a conflict, GitError, and
        nothing is sent. Returns the paths sent."""
        if not self.mirror or not os.path.isdir(self.mirror):
            return []
        local = Workspace(self.mirror)
        try:
            changed = {p for _xy, p in local.status()}
        except Exception:                                         # noqa: BLE001
            return []
        # a requested folder (a gate's evidence folder) covers every changed file under it
        want = []
        for rel in paths:
            r = rel.replace("\\", "/").rstrip("/")
            want += sorted(c for c in changed if c == r or c.startswith(r + "/"))
        want = list(dict.fromkeys(want))
        if not want:
            return []
        self._same_head()
        head, mine, theirs = self._blobs(want)
        # files the slot already has with the same content (a remote gate's outputs are copied
        # into the lane on the slot too: slots.copy_to_lane) are not sent again
        same = {r for r in want if theirs.get(r) == mine.get(r)}
        bad = [r for r in want if r not in same and theirs.get(r) != head.get(r)]
        if bad:
            raise GitError(f"{len(bad)} file(s) changed on {self.agent.name} since the commit "
                           f"the coordinator's copy has, and differ from its copy: "
                           f"{', '.join(bad[:5])} (nothing was sent; a session's edit is kept)")
        sent = []
        for r in want:
            if r in same:
                continue
            data = local.read(r)
            if data is None:                    # deleted here: delete on the slot too (add -A
                self.agent.call("write", {**self._where(), "path": r, "data": "",   # stages it)
                                          "delete": True})
            else:
                self.agent.call("write", {**self._where(), "path": r,
                                          "data": base64.b64encode(data).decode("ascii")})
            sent.append(r)
        return sent

    def commit_paths(self, paths: list[str], message: str, env: Optional[dict] = None) -> str:
        """As Workspace.commit_paths, with the pathspec sent on stdin (the slot cannot read a
        file on this machine). Never --no-verify, never amend."""
        paths = [p for p in dict.fromkeys(paths) if p]
        if not paths:
            return ""
        self._upload_mirror_changes(paths)
        spec = "\n".join(paths) + "\n"
        before = self.head()
        self._run_in(["add", "-A", "--pathspec-from-file=-"], spec)
        want = {p.replace("\\", "/") for p in paths}
        staged = [p for p in self.run("diff", "--cached", "--name-only",
                                      "--no-renames").splitlines() if p.strip() in want]
        if not staged:
            return ""
        try:
            self._run_in(["commit", "-q", "-m", message, "--pathspec-from-file=-"], spec, env=env)
        except (GitError, OSError) as e:
            try:
                self._run_in(["reset", "-q", "--pathspec-from-file=-"], spec, check=False)
            except (GitError, OSError):
                pass
            raise GitError(f"{e} (the coordinator unstaged the {len(paths)} path(s) "
                           f"again)") from e
        after = self.head()
        if after != before and callable(self.on_commit):
            try:
                self.on_commit(after)          # fetch it back, so the mirror shows the commit
            except Exception:                                     # noqa: BLE001
                pass
        return after if after != before else ""

    def _run_in(self, args: list, stdin: str, check: bool = True,
                env: Optional[dict] = None) -> str:
        try:
            rc, out, err = self._proc(args, timeout=120, env=env, input=stdin)
        except subprocess.TimeoutExpired:
            raise GitError(f"git {' '.join(args)} timed out") from None
        if check and rc != 0:
            raise GitError(f"git {' '.join(args)}: {(err or out).strip()[:400]}")
        return out

    def remote_head(self, remote: str, branch: str) -> Optional[str]:
        raise GitError("a lane on a slot never talks to a remote (the junction publishes)")

    def push(self, remote: str, branch: str) -> str:
        raise GitError("a lane on a slot never pushes (the junction publishes)")


def _forwarded(env: Optional[dict]) -> dict:
    """Only the variables a commit needs travel to the slot (author/committer names, a
    project's hook overrides such as AU_ALLOW_SCOPE); the coordinator's own environment
    (git_env() hands over a whole os.environ) stays here. The agent checks again."""
    from foundry_client.lane import env_allowed
    return {k: str(v) for k, v in (env or {}).items() if env_allowed(k)}


__all__ = ["LaneAgent", "LaneUnreachable", "LaneHTTPError", "http_status", "RemoteWorkspace"]

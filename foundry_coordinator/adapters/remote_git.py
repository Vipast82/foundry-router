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
import subprocess
import urllib.error
import urllib.request
from typing import Callable, Optional

from .git import GitError, Workspace

HTTP_SLACK_S = 15          # the HTTP call may take this much longer than the git timeout


class LaneUnreachable(GitError):
    """The slot's lane agent could not be reached (tunnel down, slot away)."""


class LaneAgent:
    def __init__(self, endpoint: Callable[[], Optional[dict]], name: str = "slot"):
        self.endpoint = endpoint
        self.name = name

    def call(self, route: str, body: dict, timeout: float = 60) -> dict:
        ep = self.endpoint() or {}
        if not ep.get("url"):
            raise LaneUnreachable(f"{self.name}: the lane tunnel is down")
        req = urllib.request.Request(
            ep["url"].rstrip("/") + "/api/lane/" + route, method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Lane-Token": ep.get("token") or ""})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read() or b"{}").get("error") or ""
            except ValueError:
                why = ""
            raise GitError(f"{self.name} lane agent: HTTP {e.code} {why}".strip()) from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise LaneUnreachable(f"{self.name}: lane agent unreachable: {e}"[:300]) from None


class RemoteWorkspace(Workspace):
    """`Workspace` for a lane on a slot. `path` is the worktree's path ON THE SLOT (what the
    coding sessions get as their directory)."""

    def __init__(self, agent: LaneAgent, project: str, lane: str, path: str):
        super().__init__(path)
        self.agent = agent
        self.project = project
        self.lane = lane

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
        out = self.agent.call("read", {**self._where(), "path": rel, "max_bytes": max_bytes})
        if not out.get("exists"):
            return None
        return base64.b64decode(out.get("data") or "")

    def stat(self, rels: list) -> dict:
        out = self.agent.call("stat", {**self._where(), "paths": [str(r) for r in rels]})
        return dict(out.get("stats") or {})

    def write(self, rel: str, data: bytes) -> None:
        self.agent.call("write", {**self._where(), "path": rel,
                                  "data": base64.b64encode(bytes(data)).decode("ascii")})

    def commit_paths(self, paths: list[str], message: str, env: Optional[dict] = None) -> str:
        """As Workspace.commit_paths, with the pathspec sent on stdin (the slot cannot read a
        file on this machine). Never --no-verify, never amend."""
        paths = [p for p in dict.fromkeys(paths) if p]
        if not paths:
            return ""
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


__all__ = ["LaneAgent", "LaneUnreachable", "RemoteWorkspace"]

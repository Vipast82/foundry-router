"""Several agent servers behind one CodingClient: Kilo or OpenCode per role.

A profile with only `[client]` gets that one client object, exactly as before
(`adapters.build_client`); this module is used only when the profile adds servers:

    [client]                 # the default (every role not named below)
    kind = "kilo"
    [clients.opencode]       # more agent servers this project may use
    kind = "opencode"
    url = "http://127.0.0.1:4097"
    password_env = "OPENCODE_SERVER_PASSWORD"
    agent = "build"          # the agent sessions run as when the role asks for [client].agent
    worker_client = "opencode"   # the router client key its model calls arrive with
    [client_roles]           # role -> server; roles: implementer, helper, lead
    helper = "opencode"

`ClientSet.start_session` picks the server from `metadata["role"]`; every later call on
a session goes to the server that started it. Ownership is kept in memory and, after a
restart, found again by asking each server whether it has the session (one GET per
session per process). `name` is the server last used on this thread, so the engine's
`store.add_session(..., P.client.name, ...)` right after `start_session` / `children`
records the right client.
"""

from __future__ import annotations

import threading
from typing import Optional

from .base import CodingClient, SessionState

ROLES = ("implementer", "helper", "lead")


class Member:
    def __init__(self, key: str, client, table: dict, default_agent: str = ""):
        self.key = key
        self.client = client
        self.table = dict(table or {})
        self.agent = str(self.table.get("agent") or "")
        self.default_agent = default_agent       # the [client].agent the engine passes
        amap = self.table.get("agents") if isinstance(self.table.get("agents"), dict) else {}
        self.agent_map = {str(k): str(v) for k, v in amap.items()}
        self.worker_client = str(self.table.get("worker_client") or "")
        # Distributed Foundry phase b: a member on a slot (laneclient.TunnelledClient) serves
        # only the lane folders under its lane root; a local member serves every folder
        self.lane_host = str(self.table.get("lane_host") or "")
        self.lane_root = str(self.table.get("lane_root") or "") if self.lane_host else ""

    def serves(self, directory: str) -> bool:
        from .laneclient import serves
        return serves(self.lane_root, directory)

    def agent_for(self, agent: str) -> str:
        if agent in self.agent_map:
            return self.agent_map[agent]
        if self.agent and agent == self.default_agent:
            return self.agent
        return agent


class ClientSet(CodingClient):
    def __init__(self, default, extra: dict, roles: Optional[dict] = None,
                 default_table: Optional[dict] = None):
        dt = dict(default_table or {})
        base_agent = str(dt.get("agent") or "code")
        self.members: dict[str, Member] = {"default": Member("default", default, {}, base_agent)}
        for k, (client, table) in (extra or {}).items():
            self.members[k] = Member(k, client, table, base_agent)
        self.roles = {str(r): str(m) for r, m in (roles or {}).items() if m}
        self.default = self.members["default"]
        self.provider = getattr(default, "provider", "foundry")
        self.url = getattr(default, "url", "")
        self._owner: dict[str, Member] = {}
        self._lock = threading.Lock()
        self._tls = threading.local()

    # -- which server -------------------------------------------------------------------------

    @property
    def name(self) -> str:                                   # type: ignore[override]
        m = getattr(self._tls, "last", None) or self.default
        return getattr(m.client, "name", "kilo")

    def member_for_role(self, role: str) -> Member:
        key = self.roles.get(role or "")
        return self.members.get(key) if key in self.members else self.default

    def member_for(self, role: str, lane_host: str = "") -> Member:
        """The role's member; for a session of a lane on a slot (`lane_host`), that slot's
        member of the same kind (`<slot>/kilo` or `<slot>/opencode`). Never falls back to a
        local server: the session's folder exists only on the slot."""
        m = self.member_for_role(role)
        if not lane_host:
            return m
        name = str(getattr(m.client, "name", "kilo"))
        kind = "opencode" if name.startswith("opencode") else "kilo"
        key = f"{lane_host}/{kind}"
        if key not in self.members:
            from .oc1 import AgentServerError
            raise AgentServerError(f"lane host {lane_host} has no {kind} server here "
                                   f"([slots.{lane_host}] lanes is off?)")
        return self.members[key]

    def for_role(self, role: str):
        """The client object a role's sessions run on (for config checks before a start)."""
        return self.member_for_role(role).client

    def _remember(self, sid: str, m: Member) -> None:
        with self._lock:
            self._owner[sid] = m
        self._tls.last = m

    def owner(self, sid: str, directory: str = "") -> Member:
        with self._lock:
            m = self._owner.get(sid)
        if m is None:
            from .laneclient import LaneDown
            m, down = self.default, False
            for cand in self.members.values():
                if directory and not cand.serves(directory):
                    continue
                try:
                    ex = getattr(cand.client, "exists", None)   # a tunnelled one may raise here
                    if callable(ex) and ex(sid, directory=directory):
                        m = cand
                        break
                except Exception as e:                           # noqa: BLE001
                    if not cand.lane_host:
                        continue
                    # a slot's server cannot be asked now (tunnel down, its kilo restarting):
                    # the session may well be there, so no other server is picked and nothing
                    # is remembered; for a folder that slot serves the call fails as a
                    # restarting server does and is tried again (2026-11-27 review: the
                    # default server was cached as the owner, and a second writer started)
                    if directory:
                        if isinstance(e, LaneDown):
                            raise
                        raise LaneDown(f"{cand.key}: cannot tell whether session {sid} is "
                                       f"there: {e}"[:300]) from e
                    down = True
                    continue
            if down and m is self.default:
                # asked without a folder while a slot was down: an answer for now, never
                # remembered (the session may be on that slot)
                self._tls.last = m
                return m
            with self._lock:
                self._owner[sid] = m
        self._tls.last = m
        return m

    def worker_client_of(self, sid: str, default: str) -> str:
        m = self.owner(sid)
        return m.worker_client or default if m is not self.default else default

    # -- CodingClient ---------------------------------------------------------------------------

    def start_session(self, *, directory, title, persona, agent, metadata, permissions) -> str:
        m = self.member_for(str((metadata or {}).get("role") or ""),
                            str((metadata or {}).get("lane_host") or ""))
        sid = m.client.start_session(directory=directory, title=title, persona=persona,
                                     agent=m.agent_for(agent), metadata=metadata,
                                     permissions=permissions)
        self._remember(sid, m)
        return sid

    def prompt(self, session_id, text, *, directory, persona, agent) -> None:
        m = self.owner(session_id, directory)
        return m.client.prompt(session_id, text, directory=directory, persona=persona,
                               agent=m.agent_for(agent))

    def state(self, session_id, *, directory, since_ts) -> SessionState:
        return self.owner(session_id, directory).client.state(session_id, directory=directory,
                                                              since_ts=since_ts)

    def abort(self, session_id, *, directory):
        return self.owner(session_id, directory).client.abort(session_id, directory=directory)

    def answer(self, session_id, request_id, answers, *, directory, kind="question") -> None:
        return self.owner(session_id, directory).client.answer(
            session_id, request_id, answers, directory=directory, kind=kind)

    def transcript(self, session_id, *, directory) -> list:
        return self.owner(session_id, directory).client.transcript(session_id,
                                                                   directory=directory)

    def recent(self, session_id, *, directory, n: int = 4) -> list:
        return self.owner(session_id, directory).client.recent(session_id, directory=directory,
                                                               n=n)

    def children(self, session_id, *, directory) -> list[dict]:
        m = self.owner(session_id, directory)
        kids = m.client.children(session_id, directory=directory) or []
        for k in kids:
            if isinstance(k, dict) and k.get("id"):
                with self._lock:
                    self._owner[k["id"]] = m
        self._tls.last = m
        return kids

    def exists(self, session_id: str, *, directory: str) -> bool:
        m = self.owner(session_id, directory)
        ex = getattr(m.client, "exists", None)
        return bool(ex(session_id, directory=directory)) if callable(ex) else True

    def has_prompt(self, session_id: str, *, directory: str) -> bool:
        return self.owner(session_id, directory).client.has_prompt(session_id,
                                                                   directory=directory)

    def messages(self, session_id: str, directory: str) -> list:
        return self.owner(session_id, directory).client.messages(session_id, directory)

    def find_session(self, directory: str, marker: str) -> str:
        for m in self.members.values():
            if not m.serves(directory):
                continue
            fn = getattr(m.client, "find_session", None)
            try:
                sid = fn(directory, marker) if callable(fn) else ""
            except Exception:                                    # noqa: BLE001
                sid = ""
            if sid:
                self._remember(sid, m)
                return sid
        return ""

    # -- per folder: every server -----------------------------------------------------------------

    def dispose(self, directory: str) -> bool:
        ok = True
        for m in self.members.values():
            if not m.serves(directory):
                continue
            fn = getattr(m.client, "dispose", None)
            if callable(fn):
                try:
                    ok = bool(fn(directory)) and ok
                except Exception:                                # noqa: BLE001
                    ok = False
        return ok

    reload = dispose

    def preflight(self, directory: str) -> list:
        out = []
        for k, m in self.members.items():
            if not m.serves(directory):
                continue
            for row in m.client.preflight(directory) or []:
                st, check, detail = row
                out.append((st, check if k == "default" else f"{k}: {check}", detail))
        return out

    # the default server's config (a role's own server: for_role(role).effective_config)
    def effective_config(self, directory: str) -> dict:
        return self.default.client.effective_config(directory)

    def model_config_problem(self, directory: str, persona: str, cfg=None) -> str:
        return self.default.client.model_config_problem(directory, persona, cfg)

    def inference_models(self, directory: str) -> dict:
        return self.default.client.inference_models(directory)

    def health(self) -> dict:
        return self.default.client.health()


def for_role(client, role: str):
    """The client a role runs on: ClientSet.for_role, or the client itself."""
    fn = getattr(client, "for_role", None)
    return fn(role) if callable(fn) else client


def worker_client_of(client, sid: str, default: str) -> str:
    """The router client key a session's model calls arrive with: its server's
    `worker_client` in a ClientSet, else the project's setting (unchanged)."""
    fn = getattr(client, "worker_client_of", None)
    return fn(sid, default) if callable(fn) else default


__all__ = ["ClientSet", "Member", "for_role", "worker_client_of", "ROLES"]

"""OpenCode coding clients.

  OpenCodeV1Client   OpenCode 1.19.x: the opencode 1.x API Kilo also speaks (Oc1Client)
                     with OpenCode's names (user `opencode`, merged config at /config)
  Oc2Client          OpenCode 2.x: the `/api/*` server (adapters/oc2.py)
  OpenCodeClient     picks one of the two from the server itself (`api = "auto"`): a server
                     that answers GET /api/info is 2.x, one that answers /global/health
                     is 1.x. The answer is cached; a failed probe is retried on the next call.

Profiles name it with [client] kind = "opencode" (or a [clients.<name>] table), url, a
password_env, provider (default "foundry"), agent (default "build") and api.
"""

from __future__ import annotations

import threading
from typing import Optional

from foundry_automation.agent_backends import OPENCODE, OPENCODE_GUARD

from .base import CodingClient, SessionState
from .oc1 import AgentServerError, Oc1Client
from .oc2 import Oc2Client


class OpenCodeV1Client(Oc1Client):
    name = "opencode"
    spec = OPENCODE
    label = "OpenCode"
    serve_check = "opencode serve"
    config_check = "opencode config"
    config_paths = ("/config",)
    paid_fallback = "OpenCode's own providers"
    dialect = "oc1"

    def start_session(self, *, directory, title, persona, agent, metadata, permissions) -> str:
        return super().start_session(directory=directory, title=title, persona=persona,
                                     agent=agent, metadata=metadata,
                                     permissions=list(permissions or []) + OPENCODE_GUARD)


class OpenCodeClient(CodingClient):
    """OpenCode of either API line, decided by asking the server."""
    name = "opencode"
    spec = OPENCODE
    label = "OpenCode"

    def __init__(self, url: str, password: str = "", username: str = "",
                 provider: str = "foundry", timeout: float = 30, api: str = "auto"):
        self.url = url.rstrip("/")
        self.provider = provider
        self.api = (api or "auto").strip().lower()
        self._args = dict(url=url, password=password, username=username, provider=provider,
                          timeout=timeout)
        self._impl: Optional[CodingClient] = None
        self._lock = threading.Lock()

    # -- which API ---------------------------------------------------------------------------

    def detect(self) -> str:
        """'oc2' | 'oc1': the server's API line (raises AgentServerError when unreachable)."""
        try:
            Oc2Client(**self._args)._req("GET", "/api/info")
            return "oc2"
        except AgentServerError:
            pass                          # 404 on 1.x; anything else: let the 1.x probe say
        v1 = OpenCodeV1Client(**self._args)
        v1.health()                       # raises when this is not a 1.x server either
        return "oc1"

    @property
    def impl(self) -> CodingClient:
        with self._lock:
            if self._impl is None:
                api = self.api if self.api in ("oc1", "oc2") else self.detect()
                self._impl = Oc2Client(**self._args) if api == "oc2" else \
                    OpenCodeV1Client(**self._args)
            return self._impl

    @property
    def dialect(self) -> str:
        return getattr(self.impl, "dialect", "oc1")

    # -- CodingClient, delegated -------------------------------------------------------------
    # Every method the engine, lead, helpers or CLI call (some through getattr) is defined
    # here explicitly, so a hasattr() check never triggers a network probe.

    def start_session(self, **kw) -> str:
        return self.impl.start_session(**kw)

    def prompt(self, session_id, text, **kw) -> None:
        return self.impl.prompt(session_id, text, **kw)

    def state(self, session_id, **kw) -> SessionState:
        return self.impl.state(session_id, **kw)

    def abort(self, session_id, **kw):
        return self.impl.abort(session_id, **kw)

    def answer(self, session_id, request_id, answers, **kw) -> None:
        return self.impl.answer(session_id, request_id, answers, **kw)

    def transcript(self, session_id, **kw) -> list:
        return self.impl.transcript(session_id, **kw)

    def recent(self, session_id, **kw) -> list:
        return self.impl.recent(session_id, **kw)

    def children(self, session_id, **kw) -> list:
        return self.impl.children(session_id, **kw)

    def preflight(self, directory: str) -> list:
        try:
            impl = self.impl
        except AgentServerError as e:
            return [("FAIL", "opencode serve", str(e))]
        return impl.preflight(directory)

    def messages(self, session_id: str, directory: str) -> list:
        return self.impl.messages(session_id, directory)

    def exists(self, session_id: str, **kw) -> bool:
        return self.impl.exists(session_id, **kw)

    def has_prompt(self, session_id: str, **kw) -> bool:
        return self.impl.has_prompt(session_id, **kw)

    def find_session(self, directory: str, marker: str) -> str:
        return self.impl.find_session(directory, marker)

    def health(self) -> dict:
        return self.impl.health()

    def dispose(self, directory: str) -> bool:
        return self.impl.dispose(directory)

    def reload(self, directory: str) -> bool:
        return self.impl.reload(directory)

    def effective_config(self, directory: str) -> dict:
        return self.impl.effective_config(directory)

    def model_config_problem(self, directory: str, persona: str, cfg=None) -> str:
        return self.impl.model_config_problem(directory, persona, cfg)

    def inference_models(self, directory: str) -> dict:
        return self.impl.inference_models(directory)


__all__ = ["OpenCodeClient", "OpenCodeV1Client", "Oc2Client"]

"""opencode 1.x server API client (`kilo serve` 7.x, `opencode serve` 1.19).

This is the HTTP code that was `adapters/kilo.py:KiloClient` up to v0.181.246, unchanged
in what it sends and parses. `KiloClient` is now this class with the Kilo spec, and
`OpenCodeV1Client` (adapters/opencode.py) is it with the OpenCode spec.

Kilo coding client: the supported `kilo serve` HTTP API.

Verified against Kilo CLI 7.8.8 (`@kilocode/sdk` 7.8.8 types plus live calls):
  POST /session?directory=        {title, agent, model:{providerID,id}, metadata, permission[]}
  POST /session/{id}/prompt_async {agent, model:{providerID,modelID}, parts:[{type:text,text}]} -> 204
  GET  /session/status?directory= {sessionID: {type: busy|retry|offline}}; idle sessions absent
  GET  /session/{id}/message      [{info, parts}]; assistant info: time.completed, finish,
                                   error, tokens, modelID. `?limit=N` (Kilo 7.8.8,
                                   MessageV2.page): the newest N messages, oldest first
                                   (a Link header pages further back with `before`)
  GET  /session/{id}              404 when the session doesn't exist
  GET  /session/{id}/children     subagent sessions (Kilo `task` tool)
  POST /session/{id}/abort
  GET  /question, /permission     pending requests (all sessions); reply via
  POST /question/{rid}/reply {answers}, /permission/{rid}/reply {reply, message}
  GET  /config/effective?directory=   merged config (providers, permissions, subagent depth)

No GUI automation, no reads of Kilo's private storage. A session-level
`deny` permission removes that tool from the model entirely (checked live:
`bash` disappears from the tool list).

Every model call Kilo makes carries `x-session-id: <session id>`, which is
how Foundry ties it to the binding the engine registers.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from foundry_automation.agent_backends import KILO

from .base import CodingClient, SessionState, parse_report
from .denial import foundry_denial


class AgentServerError(RuntimeError):
    """An agent server (Kilo or OpenCode) call failed. `KiloError` is this class."""


class Oc1Client(CodingClient):
    """The opencode 1.x server API (Kilo CLI 7.x, OpenCode 1.19). Subclasses set `spec`;
    `label` names the product in errors and checks ("Kilo" for Kilo, as before)."""
    name = "oc1"
    spec = KILO
    label = "Kilo"
    serve_check = "kilo serve"
    config_check = "kilo config"
    # where the merged config is read: Kilo has /config/effective, OpenCode 1.x only /config
    config_paths = ("/config/effective",)
    paid_fallback = "the Kilo Gateway"       # what a subagent could fall back to

    def __init__(self, url: str, password: str = "", username: str = "",
                 provider: str = "foundry", timeout: float = 30):
        username = username or self.spec.default_username
        self.url = url.rstrip("/")
        self.provider = provider
        self.timeout = timeout
        self._auth = ("Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
                      if password else "")
        # E (2026-10-28): token totals of a finished turn, by (session, last message id,
        # message count): an idle session polled again is never re-read in full
        self._totals: dict = {}

    # -- http ----------------------------------------------------------------------

    def _req(self, method: str, path: str, body=None, query: Optional[dict] = None,
             ok404: bool = False):
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("content-type", "application/json")
        if self._auth:
            req.add_header("authorization", self._auth)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 404 and ok404:
                return None
            raise AgentServerError(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise AgentServerError(f"{method} {path}: {e}") from None

    def dispose(self, directory: str) -> bool:
        """Drop Kilo's instance for `directory` (opencode-derived POST /instance/dispose), so
        the next session there re-reads its kilo.jsonc. Other folders are untouched."""
        self._req("POST", "/instance/dispose", {}, query={"directory": directory})
        return True

    def health(self) -> dict:
        return self._req("GET", "/global/health") or {}

    # -- sessions --------------------------------------------------------------------

    def start_session(self, *, directory, title, persona, agent, metadata, permissions) -> str:
        body = {"title": title[:120], "agent": agent,
                "model": {"providerID": self.provider, "id": persona},
                "metadata": {"coordinator": metadata}, "permission": permissions}
        s = self._req("POST", "/session", body, {"directory": directory})
        if not s or not s.get("id"):
            raise AgentServerError(f"{self.label} did not return a session id")
        return s["id"]

    def prompt(self, session_id, text, *, directory, persona, agent) -> None:
        self._req("POST", f"/session/{session_id}/prompt_async",
                  {"agent": agent, "model": {"providerID": self.provider, "modelID": persona},
                   "parts": [{"type": "text", "text": text}]}, {"directory": directory})

    def find_session(self, directory: str, marker: str) -> str:
        """Reconcile a create whose response was lost: the newest session whose
        title carries `marker` (the coordinator puts one in every helper title)."""
        rows = self._req("GET", "/session", query={"directory": directory}) or []
        hits = [r for r in rows if isinstance(r, dict) and marker in str(r.get("title") or "")]
        hits.sort(key=lambda r: ((r.get("time") or {}).get("created") or 0), reverse=True)
        return str(hits[0].get("id") or "") if hits else ""

    def has_prompt(self, session_id: str, *, directory: str) -> bool:
        """Reconcile a prompt whose response was lost: did Kilo record a user turn?"""
        return any(((m.get("info") or {}).get("role") == "user")
                   for m in self.messages(session_id, directory))

    def exists(self, session_id: str, *, directory: str) -> bool:
        return self._req("GET", f"/session/{session_id}", query={"directory": directory},
                         ok404=True) is not None

    def messages(self, session_id: str, directory: str) -> list:
        return self._req("GET", f"/session/{session_id}/message",
                         query={"directory": directory}) or []

    def recent(self, session_id: str, *, directory: str, n: int = 4) -> list:
        """The newest `n` messages, oldest first (`?limit=n`; a server that ignores the
        limit returns them all and the tail is kept). E (2026-10-28): a 262k-token
        transcript is no longer fetched whole on every poll."""
        n = max(1, int(n))
        rows = self._req("GET", f"/session/{session_id}/message",
                         query={"directory": directory, "limit": n}) or []
        return list(rows)[-n:]

    def _token_totals(self, session_id: str, directory: str, last: dict) -> dict:
        """{input, output} over every message of the session (SessionState.tokens): one
        full read per finished turn, cached by the last message's id."""
        info = (last or {}).get("info") or {}
        key = (session_id, info.get("id") or "", (info.get("time") or {}).get("completed"))
        hit = self._totals.get(key)
        if hit is not None and info.get("id"):
            return dict(hit)
        tokens = {"input": 0, "output": 0}
        for m in self.messages(session_id, directory):
            t = ((m.get("info") or {}).get("tokens") or {})
            tokens["input"] += int(t.get("input") or 0)
            tokens["output"] += int(t.get("output") or 0)
        if len(self._totals) > 256:
            self._totals.clear()
        self._totals[key] = dict(tokens)
        return tokens

    def state(self, session_id, *, directory, since_ts) -> SessionState:
        if not self.exists(session_id, directory=directory):
            return SessionState(status="missing", error=f"{self.label} has no such session")
        q = self._pending(session_id, directory)
        if q:
            return SessionState(status="attention", question=q)
        st = self._session_status(session_id, directory)
        if st.get("type") in ("busy", "retry"):
            s = SessionState(status="busy")
            if st.get("type") == "retry":
                s.retry = True                    # even when Kilo sends no message
                s.error = str(st.get("message") or "")[:300]
                s.denial = foundry_denial(s.error)
            return s
        # E (2026-10-28): status first (above), then only the newest message; the whole
        # transcript is read once, for the token totals of a finished turn
        msgs = self.recent(session_id, directory=directory, n=2)
        last = msgs[-1] if msgs else None
        info = (last or {}).get("info") or {}
        times = info.get("time") or {}
        created = (times.get("created") or 0) / 1000.0
        completed = (times.get("completed") or 0) / 1000.0
        if not last or info.get("role") != "assistant" or max(created, completed) + 1 < since_ts:
            # Prompt sent but not admitted yet (Kilo reports idle for a moment).
            return SessionState(status="busy", error="waiting for the prompt to be admitted",
                                unanswered=True)
        if not (info.get("time") or {}).get("completed") and not info.get("error"):
            # Kilo's status list does not have it (busy/retry returned above): a turn
            # nobody runs any more unless it completes within moments (a dead session)
            return SessionState(status="busy", stalled=True)
        if _is_compaction(last) and not info.get("error"):
            # Kilo compacted the context; that summary is not the work's result.
            # Kilo stays the compaction manager: the coordinator only waits for
            # its continuation (and nudges the same session once if none comes)
            return SessionState(status="busy", error=f"context compacted; waiting for {self.label} "
                                "to continue", compaction={"message_id": info.get("id") or ""})
        text = "\n".join(p.get("text") or "" for p in last.get("parts") or []
                         if p.get("type") == "text")
        err = info.get("error") or {}
        err_text = json.dumps(err)[:1500] if err else ""
        tokens = self._token_totals(session_id, directory, last)
        lt = info.get("tokens") or {}
        cache = lt.get("cache") or {}
        context = (int(lt.get("input") or 0) + int(cache.get("read") or 0)
                   + int(cache.get("write") or 0)) if lt else None
        return SessionState(status="error" if err else "idle", error=err_text,
                            denial=foundry_denial(err_text) if err else None, final_text=text,
                            report=parse_report(text), tokens=tokens, context=context)

    def _pending(self, session_id: str, directory: str) -> Optional[dict]:
        for q in self._req("GET", "/question", query={"directory": directory}) or []:
            if q.get("sessionID") == session_id:
                qs = q.get("questions") or []
                return {"request_id": q.get("id"), "kind": "question",
                        "text": "\n".join(x.get("question", "") for x in qs)[:2000],
                        "options": [[o.get("label") for o in x.get("options") or []] for x in qs]}
        for p in self._req("GET", "/permission", query={"directory": directory}) or []:
            if p.get("sessionID") == session_id:
                return {"request_id": p.get("id"), "kind": "permission",
                        "text": f"permission {p.get('permission')} for "
                                f"{', '.join(p.get('patterns') or [])}"[:2000], "options": []}
        return None

    def abort(self, session_id, *, directory) -> bool:
        """False when Kilo did not confirm the abort (the session may still run)."""
        try:
            self._req("POST", f"/session/{session_id}/abort", {}, {"directory": directory})
            return True
        except AgentServerError:
            return False

    def answer(self, session_id, request_id, answers, *, directory, kind="question") -> None:
        if kind == "permission":
            body = {"reply": answers[0] if answers else "reject"}
            if len(answers) > 1 and answers[1]:
                body["message"] = str(answers[1])[:500]       # shown to the model with the reply
            self._req("POST", f"/permission/{request_id}/reply", body, {"directory": directory})
        else:
            self._req("POST", f"/question/{request_id}/reply", {"answers": answers},
                      {"directory": directory})

    def transcript(self, session_id, *, directory) -> list:
        return self.messages(session_id, directory)

    def children(self, session_id, *, directory) -> list[dict]:
        return self._req("GET", f"/session/{session_id}/children",
                         query={"directory": directory}) or []

    # -- seams the OpenCode clients override ----------------------------------------------

    def _session_status(self, session_id: str, directory: str) -> dict:
        """{type: busy|retry|idle, ...} for one session; {} when the server lists it as idle
        (Kilo leaves idle sessions out of /session/status)."""
        return (self._req("GET", "/session/status", query={"directory": directory}) or {}).get(
            session_id) or {}

    def _config_read(self, directory: str) -> dict:
        """The merged config from the first of `config_paths` the server has (Kilo:
        /config/effective; OpenCode 1.x: /config)."""
        for i, path in enumerate(self.config_paths):
            try:
                return self._req("GET", path, query={"directory": directory}) or {}
            except AgentServerError as e:
                if i + 1 < len(self.config_paths) and "HTTP 404" in str(e):
                    continue
                raise
        return {}

    def reload(self, directory: str) -> bool:
        """Make the next session in `directory` re-read its config (instance dispose)."""
        return self.dispose(directory)

    # -- configuration checks ------------------------------------------------------------

    def model_config_problem(self, directory: str, persona: str, cfg: dict | None = None) -> str:
        """Catch local filters before prompt_async can silently reject a model.

        A configured model and a Foundry catalog entry do not prove that Kilo
        exposes it: its provider whitelist/blacklist are applied separately.
        """
        if cfg is None:
            cfg = self.effective_config(directory)
        provider = (cfg.get('provider') or {}).get(self.provider)
        if provider is None:
            return f"{self.label} provider {self.provider!r} is not configured"
        if 'whitelist' in provider and persona not in (provider['whitelist'] or []):
            return f"{self.label} provider whitelist excludes {self.provider}/{persona}"
        if persona in (provider.get('blacklist') or []):
            return f"{self.label} provider blacklist excludes {self.provider}/{persona}"
        return ''

    def effective_config(self, directory: str) -> dict:
        """Kilo's merged config for a directory (one read serves many checks)."""
        return self._config_read(directory)

    def inference_models(self, directory: str) -> dict:
        """Read model visibility using Kilo's actual inference credential.

        The coordinator key need not allow the local worker's model. Keep
        provider options/credentials in memory; return only catalog entries.
        """
        cfg = self.effective_config(directory)
        options = ((cfg.get('provider') or {}).get(self.provider) or {}).get('options') or {}
        base = options.get('baseURL') or ''
        if not base.startswith(('http://', 'https://')):
            raise AgentServerError(f'{self.label} inference provider baseURL is unavailable')
        req = urllib.request.Request(base.rstrip('/') + '/models')
        key = options.get('apiKey') or ''
        if key:
            req.add_header('authorization', 'Bearer ' + key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                catalog = json.load(response)
        except urllib.error.HTTPError as e:
            raise AgentServerError(f'{self.label} inference model catalog: HTTP {e.code}') from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            raise AgentServerError(f'{self.label} inference model catalog is unreachable') from None
        return {m.get('id'): m for m in catalog.get('data') or []}

    def preflight(self, directory: str) -> list[tuple[str, str, str]]:
        out = []
        try:
            h = self.health()
            out.append(("PASS", self.serve_check, f"reachable, version {h.get('version')}"))
        except AgentServerError as e:
            return [("FAIL", self.serve_check, str(e))]
        try:
            cfg = self.effective_config(directory)
        except AgentServerError as e:
            return out + [("FAIL", self.config_check, str(e))]
        enabled = cfg.get("enabled_providers")
        providers = list((cfg.get("provider") or {}).keys())
        if self.provider not in providers:
            out.append(("FAIL", "provider", f"no '{self.provider}' provider in {self.label}'s config "
                                            f"(have {providers})"))
        else:
            out.append(("PASS", "provider", f"'{self.provider}' configured"))
        if enabled != [self.provider]:
            out.append(("WARN", "enabled_providers",
                        f"is {enabled!r}; set it to [\"{self.provider}\"] so a subagent can't "
                        f"fall back to {self.paid_fallback} (paid credits)"))
        else:
            out.append(("PASS", "enabled_providers", f"only {self.provider}"))
        depth = cfg.get("subagent_depth")
        out.append(("PASS" if depth in (0, 1) else "WARN", "subagent_depth",
                    f"{depth!r} (1 = helpers can't start their own helpers)"))
        return out


def _is_compaction(msg: dict) -> bool:
    """A Kilo/opencode compaction summary turn: the message is flagged summary,
    or runs as the compaction agent/mode, or carries a compaction part."""
    info = (msg or {}).get("info") or {}
    if info.get("summary") is True or "compaction" in (info.get("mode"), info.get("agent")):
        return True
    return any((p or {}).get("type") == "compaction" for p in (msg or {}).get("parts") or [])


def wait_until_idle(client: "Oc1Client", session_id: str, directory: str, since_ts: float,
                    timeout: float = 600, poll: float = 1.0) -> SessionState:
    deadline = time.time() + timeout
    while True:
        s = client.state(session_id, directory=directory, since_ts=since_ts)
        if s.status != "busy" or time.time() > deadline:
            return s
        time.sleep(poll)

"""Kilo coding client: the supported `kilo serve` HTTP API.

Verified against Kilo CLI 7.8.8 (`@kilocode/sdk` 7.8.8 types plus live calls):
  POST /session?directory=        {title, agent, model:{providerID,id}, metadata, permission[]}
  POST /session/{id}/prompt_async {agent, model:{providerID,modelID}, parts:[{type:text,text}]} -> 204
  GET  /session/status?directory= {sessionID: {type: busy|retry|offline}}; idle sessions absent
  GET  /session/{id}/message      [{info, parts}]; assistant info: time.completed, finish,
                                   error, tokens, modelID
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

from .base import CodingClient, SessionState, parse_report

# Kilo's reported errors that are Foundry refusals: the provider error text
# carries Foundry's JSON body (code / foundry_category).
_FOUNDRY_CATEGORIES = ("budget", "pause", "route", "config", "auth")


class KiloError(RuntimeError):
    pass


class KiloClient(CodingClient):
    name = "kilo"

    def __init__(self, url: str, password: str = "", username: str = "kilo",
                 provider: str = "foundry", timeout: float = 30):
        self.url = url.rstrip("/")
        self.provider = provider
        self.timeout = timeout
        self._auth = ("Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
                      if password else "")

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
            raise KiloError(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise KiloError(f"{method} {path}: {e}") from None

    def health(self) -> dict:
        return self._req("GET", "/global/health") or {}

    # -- sessions --------------------------------------------------------------------

    def start_session(self, *, directory, title, persona, agent, metadata, permissions) -> str:
        body = {"title": title[:120], "agent": agent,
                "model": {"providerID": self.provider, "id": persona},
                "metadata": {"coordinator": metadata}, "permission": permissions}
        s = self._req("POST", "/session", body, {"directory": directory})
        if not s or not s.get("id"):
            raise KiloError("Kilo did not return a session id")
        return s["id"]

    def prompt(self, session_id, text, *, directory, persona, agent) -> None:
        self._req("POST", f"/session/{session_id}/prompt_async",
                  {"agent": agent, "model": {"providerID": self.provider, "modelID": persona},
                   "parts": [{"type": "text", "text": text}]}, {"directory": directory})

    def exists(self, session_id: str, *, directory: str) -> bool:
        return self._req("GET", f"/session/{session_id}", query={"directory": directory},
                         ok404=True) is not None

    def messages(self, session_id: str, directory: str) -> list:
        return self._req("GET", f"/session/{session_id}/message",
                         query={"directory": directory}) or []

    def state(self, session_id, *, directory, since_ts) -> SessionState:
        if not self.exists(session_id, directory=directory):
            return SessionState(status="missing", error="Kilo has no such session")
        q = self._pending(session_id, directory)
        if q:
            return SessionState(status="attention", question=q)
        st = (self._req("GET", "/session/status", query={"directory": directory}) or {}).get(
            session_id) or {}
        if st.get("type") in ("busy", "retry"):
            s = SessionState(status="busy")
            if st.get("type") == "retry":
                s.error = str(st.get("message") or "")[:300]
                s.denial = foundry_denial(s.error)
            return s
        msgs = self.messages(session_id, directory)
        last = msgs[-1] if msgs else None
        info = (last or {}).get("info") or {}
        created = ((info.get("time") or {}).get("created") or 0) / 1000.0
        if not last or info.get("role") != "assistant" or created + 1 < since_ts:
            # Prompt sent but not admitted yet (Kilo reports idle for a moment).
            return SessionState(status="busy", error="waiting for the prompt to be admitted")
        if not (info.get("time") or {}).get("completed") and not info.get("error"):
            return SessionState(status="busy")
        text = "\n".join(p.get("text") or "" for p in last.get("parts") or []
                         if p.get("type") == "text")
        err = info.get("error") or {}
        err_text = json.dumps(err)[:1500] if err else ""
        tokens = {"input": 0, "output": 0}
        for m in msgs:
            t = ((m.get("info") or {}).get("tokens") or {})
            tokens["input"] += int(t.get("input") or 0)
            tokens["output"] += int(t.get("output") or 0)
        return SessionState(status="error" if err else "idle", error=err_text,
                            denial=foundry_denial(err_text) if err else None, final_text=text,
                            report=parse_report(text), tokens=tokens)

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

    def abort(self, session_id, *, directory) -> None:
        try:
            self._req("POST", f"/session/{session_id}/abort", {}, {"directory": directory})
        except KiloError:
            pass

    def answer(self, session_id, request_id, answers, *, directory, kind="question") -> None:
        if kind == "permission":
            self._req("POST", f"/permission/{request_id}/reply",
                      {"reply": answers[0] if answers else "reject"}, {"directory": directory})
        else:
            self._req("POST", f"/question/{request_id}/reply", {"answers": answers},
                      {"directory": directory})

    def transcript(self, session_id, *, directory) -> list:
        return self.messages(session_id, directory)

    def children(self, session_id, *, directory) -> list[dict]:
        return self._req("GET", f"/session/{session_id}/children",
                         query={"directory": directory}) or []

    # -- configuration checks ------------------------------------------------------------

    def model_config_problem(self, directory: str, persona: str) -> str:
        """Catch local filters before prompt_async can silently reject a model.

        A configured model and a Foundry catalog entry do not prove that Kilo
        exposes it: its provider whitelist/blacklist are applied separately.
        """
        cfg = self._req('GET', '/config/effective', query={'directory': directory}) or {}
        provider = (cfg.get('provider') or {}).get(self.provider)
        if provider is None:
            return f"Kilo provider {self.provider!r} is not configured"
        if 'whitelist' in provider and persona not in (provider['whitelist'] or []):
            return f"Kilo provider whitelist excludes {self.provider}/{persona}"
        if persona in (provider.get('blacklist') or []):
            return f"Kilo provider blacklist excludes {self.provider}/{persona}"
        return ''

    def inference_models(self, directory: str) -> dict:
        """Read model visibility using Kilo's actual inference credential.

        The coordinator key need not allow the local worker's model. Keep
        provider options/credentials in memory; return only catalog entries.
        """
        cfg = self._req('GET', '/config/effective', query={'directory': directory}) or {}
        options = ((cfg.get('provider') or {}).get(self.provider) or {}).get('options') or {}
        base = options.get('baseURL') or ''
        if not base.startswith(('http://', 'https://')):
            raise KiloError('Kilo inference provider baseURL is unavailable')
        req = urllib.request.Request(base.rstrip('/') + '/models')
        key = options.get('apiKey') or ''
        if key:
            req.add_header('authorization', 'Bearer ' + key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                catalog = json.load(response)
        except urllib.error.HTTPError as e:
            raise KiloError(f'Kilo inference model catalog: HTTP {e.code}') from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            raise KiloError('Kilo inference model catalog is unreachable') from None
        return {m.get('id'): m for m in catalog.get('data') or []}

    def preflight(self, directory: str) -> list[tuple[str, str, str]]:
        out = []
        try:
            h = self.health()
            out.append(("PASS", "kilo serve", f"reachable, version {h.get('version')}"))
        except KiloError as e:
            return [("FAIL", "kilo serve", str(e))]
        try:
            cfg = self._req("GET", "/config/effective", query={"directory": directory}) or {}
        except KiloError as e:
            return out + [("FAIL", "kilo config", str(e))]
        enabled = cfg.get("enabled_providers")
        providers = list((cfg.get("provider") or {}).keys())
        if self.provider not in providers:
            out.append(("FAIL", "provider", f"no '{self.provider}' provider in Kilo's config "
                                            f"(have {providers})"))
        else:
            out.append(("PASS", "provider", f"'{self.provider}' configured"))
        if enabled != [self.provider]:
            out.append(("WARN", "enabled_providers",
                        f"is {enabled!r}; set it to [\"{self.provider}\"] so a subagent can't "
                        f"fall back to the Kilo Gateway (paid credits)"))
        else:
            out.append(("PASS", "enabled_providers", f"only {self.provider}"))
        depth = cfg.get("subagent_depth")
        out.append(("PASS" if depth in (0, 1) else "WARN", "subagent_depth",
                    f"{depth!r} (1 = helpers can't start their own helpers)"))
        return out


def foundry_denial(text: str) -> Optional[dict]:
    """Find Foundry's refusal in a provider error Kilo recorded."""
    if not text:
        return None
    cat = None
    for c in _FOUNDRY_CATEGORIES:
        if f'"foundry_category": "{c}"' in text or f'\\"foundry_category\\":\\"{c}\\"' in text \
                or f'"foundry_category":"{c}"' in text or f'\\"foundry_category\\": \\"{c}\\"' in text:
            cat = c
            break
    if cat is None:
        for code, c in (("budget_denied", "budget"), ("paused", "pause"),
                        ("route_unavailable", "route"), ("persona_disabled", "config")):
            if code in text:
                cat = c
                break
    if cat is None:
        return None
    retry = None
    for key in ("reset_seconds", "retry_after"):
        i = text.find(key)
        if i >= 0:
            digits = "".join(ch for ch in text[i + len(key):i + len(key) + 16] if ch.isdigit())
            if digits:
                retry = int(digits)
                break
    return {"category": cat, "retry_after": retry}


def wait_until_idle(client: KiloClient, session_id: str, directory: str, since_ts: float,
                    timeout: float = 600, poll: float = 1.0) -> SessionState:
    deadline = time.time() + timeout
    while True:
        s = client.state(session_id, directory=directory, since_ts=since_ts)
        if s.status != "busy" or time.time() > deadline:
            return s
        time.sleep(poll)

"""Foundry client: work bindings, state reporting and review inference.

Uses Foundry's orchestration API (/v1/foundry/work/*, client key with the
`orchestration` scope) and its OpenAI-compatible chat endpoint for reviews
(the key also needs `inference`). A refusal comes back as FoundryDenied with
Foundry's category (budget | pause | route | config | auth), so the engine
pauses on a budget or pause instead of counting it as a failed attempt.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


class FoundryError(RuntimeError):
    pass


class FoundryDenied(FoundryError):
    def __init__(self, category: str, code: str, message: str, retry_after: Optional[int]):
        super().__init__(f"Foundry refused ({category}/{code}): {message}")
        self.category, self.code, self.message, self.retry_after = \
            category, code, message, retry_after


def pinned_meta(adm: Optional[dict]) -> dict:
    """Binding meta for a helper admitted on a pinned fallback route
    (helper_admission answered pinned=true): Foundry then serves the bound
    session only on that route. {} when nothing is pinned."""
    r = (adm or {}).get("route") or {}
    if (adm or {}).get("pinned") and r.get("backend") and r.get("model"):
        return {"pinned_route": {"backend": r["backend"], "model": r["model"]}}
    return {}


def admitted_routes(adm: Optional[dict]) -> list:
    """The routes a helper's calls may be served on: only the pinned route when
    the admission pinned one, else every reported route of the chain."""
    if pinned_meta(adm):
        r = adm["route"]
        hit = [x for x in adm.get("routes") or [] if x.get("backend") == r["backend"]
               and x.get("model") == r["model"]]
        return hit or [dict(r)]
    return list((adm or {}).get("routes") or [])


class FoundryClient:
    def __init__(self, url: str, key: str = "", timeout: float = 600):
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout

    def _req(self, method: str, path: str, body=None, query: Optional[dict] = None,
             timeout: Optional[float] = None, headers: Optional[dict] = None) -> tuple[dict, dict]:
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None
                                     else None, method=method)
        req.add_header("content-type", "application/json")
        if self.key:
            req.add_header("authorization", f"Bearer {self.key}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
                return (json.loads(raw) if raw else {}), dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"error": raw[:300].decode(errors="replace")}
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                code, msg = str(err.get("code") or ""), str(err.get("message") or "")
                cat = str(err.get("foundry_category") or "")
                retry = err.get("reset_seconds")
            else:
                code, msg = str((body or {}).get("code") or ""), str(err or "")
                cat = str((body or {}).get("category") or "")
                retry = (body or {}).get("reset_seconds")
            if not retry and e.headers.get("retry-after", "").isdigit():
                retry = int(e.headers["retry-after"])
            if cat:
                raise FoundryDenied(cat, code, msg, int(retry) if retry else None) from None
            raise FoundryError(f"{method} {path}: HTTP {e.code} {code} {msg[:300]}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise FoundryError(f"{method} {path}: {e}") from None
        except (ValueError, http.client.HTTPException, OSError) as e:
            # a 200 that isn't JSON (proxy/captive page), a cut-off body, a reset
            raise FoundryError(f"{method} {path}: {type(e).__name__}: {e}"[:400]) from None

    # -- bindings / state ----------------------------------------------------------

    def bind(self, session_id: str, project_id: str, task_id: str, run_id: str, role: str,
             persona: str, parent: str = "", meta: Optional[dict] = None, purpose: str = "",
             worker_client: str = "", assignment: Optional[dict] = None) -> dict:
        body = {"session_id": session_id, "project_id": project_id, "task_id": task_id,
                "run_id": run_id, "role": role, "persona": persona, "parent_session_id": parent,
                "meta": meta or {}}
        if purpose:
            body["purpose"] = purpose
        if worker_client:
            body["worker_client"] = worker_client
        if assignment:
            body["assignment"] = assignment
        return self._req("POST", "/v1/foundry/work/sessions", body, timeout=20)[0]

    def helper_admission(self, persona: str, vision: bool = False,
                         models: Optional[list] = None, purpose: str = "") -> dict:
        """Would optional helper work run on this persona now? (quota sources; Foundry
        may start one bounded background refresh probe for a stale idle reading)
        vision / models / purpose are the helper's requirements: when the first
        route is not admitted Foundry may admit (and pin) a later chain route
        that meets them; the answer then has pinned=true and route={backend, model},
        and the session must be bound with meta.pinned_route = that route."""
        q = {"persona": persona}
        if vision:
            q["vision"] = "true"
        if models:
            q["models"] = ",".join(str(m) for m in models)
        if purpose:
            q["purpose"] = purpose
        return self._req("GET", "/v1/foundry/helpers/admission", query=q, timeout=20)[0]

    def end(self, session_id: str, outcome: str) -> None:
        self._req("POST", f"/v1/foundry/work/sessions/{session_id}/end", {"outcome": outcome},
                  timeout=20)

    def put_task(self, project_id: str, task_id: str, data: dict) -> None:
        self._req("PUT", f"/v1/foundry/work/tasks/{urllib.parse.quote(project_id)}/"
                         f"{urllib.parse.quote(task_id)}", data, timeout=20)

    def push_events(self, events: list[dict]) -> int:
        return int(self._req("POST", "/v1/foundry/work/events", {"events": events},
                             timeout=20)[0].get("recorded") or 0)

    def usage(self, **q) -> dict:
        return self._req("GET", "/v1/foundry/work/usage", query={k: v for k, v in q.items() if v},
                         timeout=20)[0]

    def denials(self, session: str = "", run: str = "", since: float = 0) -> list[dict]:
        return self._req("GET", "/v1/foundry/work/denials",
                         query={k: v for k, v in (("session", session), ("run", run),
                                                  ("since", since)) if v}, timeout=20)[0].get(
            "denials") or []

    def _get_optional(self, path: str, query: Optional[dict] = None) -> Optional[dict]:
        """GET that answers None when the endpoint/resource is missing (404, an
        older Foundry) or Foundry can't be reached; other errors raise."""
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, method="GET")
        req.add_header("content-type", "application/json")
        if self.key:
            req.add_header("authorization", f"Bearer {self.key}")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                data = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raw = e.read()
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"error": raw[:300].decode(errors="replace")}
            err = body.get("error") if isinstance(body, dict) else None
            code = str((err.get("code") if isinstance(err, dict) else (body or {}).get("code"))
                       or "")
            msg = str((err.get("message") if isinstance(err, dict) else err) or "")
            raise FoundryError(f"GET {path}: HTTP {e.code} {code} {msg[:300]}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return None
        except (ValueError, http.client.HTTPException, OSError) as e:
            raise FoundryError(f"GET {path}: {type(e).__name__}: {e}"[:400]) from None
        if not isinstance(data, dict):
            raise FoundryError(f"GET {path}: unexpected response {str(data)[:200]}")
        return data

    def served_models(self, session_id: str) -> Optional[dict]:
        """{session_id, calls, served_models (first use first), backends, routes,
        last_ts} from Foundry's ledger; None when unavailable (404 / unreachable)."""
        return self._get_optional(
            f"/v1/foundry/work/sessions/{urllib.parse.quote(session_id, safe='')}/served")

    def persona_info(self, persona: str, client: str = "") -> Optional[dict]:
        """{name, exists, enabled, strict, routes: [{backend, model, source, vision}],
        client_allowed, reason}; `client` checks another configured client's
        grant instead of this key's. None when unavailable (404 / unreachable)."""
        return self._get_optional(
            f"/v1/foundry/personas/{urllib.parse.quote(persona, safe='')}/routes",
            {"client": client} if client else None)

    # -- control center (control_api.py) ----------------------------------------------

    def telemetry(self, batch: dict) -> dict:
        return self._req("POST", "/v1/foundry/telemetry/batch", batch, timeout=30)[0]

    def heartbeat(self, payload: dict) -> dict:
        return self._req("POST", "/v1/foundry/telemetry/heartbeat", payload, timeout=15)[0]

    def claim_actions(self, source_id: str, projects: list[str], limit: int = 20) -> list[dict]:
        return self._req("POST", "/v1/foundry/control/claim",
                         {"source_id": source_id, "projects": projects, "limit": limit},
                         timeout=20)[0].get("actions") or []

    def action_status(self, action_id: str, source_id: str, status: str, detail: str,
                      result: Optional[dict] = None) -> dict:
        return self._req("POST", f"/v1/foundry/control/actions/{urllib.parse.quote(action_id)}/status",
                         {"source_id": source_id, "status": status, "detail": detail[:1000],
                          "result": result or {}}, timeout=20)[0]

    # -- inference -----------------------------------------------------------------

    def chat(self, persona: str, messages: list, *, session_id: str = "",
             max_tokens: int = 4000, schema: Optional[dict] = None,
             timeout: Optional[float] = None) -> tuple[str, dict]:
        """One non-streaming completion. session_id is sent as x-session-id so
        the call is bound like any Kilo call (project/task/run/role)."""
        body = {"model": persona, "messages": messages, "stream": False,
                "max_tokens": max_tokens, "temperature": 0}
        if schema:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "review", "schema": schema}}
        data, headers = self._req("POST", "/v1/chat/completions", body,
                                  headers={"x-session-id": session_id} if session_id else None,
                                  timeout=timeout)
        choice = (data.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content")) or ""
        # HTTP field names are case-insensitive. Uvicorn/proxies commonly send
        # these in lowercase; using the virtual response model instead makes a
        # valid visual call fail its served-route capability check.
        response_headers = {k.lower(): v for k, v in headers.items()}
        meta = {"served_model": response_headers.get("x-foundry-served-model") or data.get("model"),
                "served_backend": response_headers.get("x-foundry-served-backend"),
                "usage": data.get("usage") or {}, "finish": choice.get("finish_reason")}
        return text, meta

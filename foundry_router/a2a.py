"""A2A (Agent2Agent protocol) in both directions (v0.181.158).

A2A is the open protocol agents use to call each other: a JSON "agent card" says
what an agent is and where it listens, and JSON-RPC over HTTP carries a message
to it and the answer back.

  Router as an agent   Other agents and harnesses can call a persona here.
                       GET  /.well-known/agent-card.json      (the default persona)
                       POST /v1/a2a                           (the default persona)
                       GET  /v1/a2a/{persona}/agent-card.json
                       POST /v1/a2a/{persona}
                       Off unless a2a.serve is on. /v1/a2a sits behind the same
                       client key as every /v1 path.

  Agents as helpers    A helper row of a persona may name a remote agent
                       (a2a.agents) instead of models; ask_helper then sends
                       the task to it and returns its answer.

Two protocol versions are spoken, told apart by the method name (server side) or
the agent card (client side):

  1.0   SendMessage / GetTask, role "ROLE_USER", parts [{"text": ...}], results
        wrapped as {"message": ...} or {"task": ...}, states "TASK_STATE_*",
        card: supportedInterfaces[{url, protocolBinding: "JSONRPC"}],
        header A2A-Version: 1.0
  0.3   message/send / tasks/get, role "user", parts [{"kind": "text", ...}],
        results told apart by "kind", card: url + preferredTransport

This module is the protocol only (no FastAPI, no router state); a2a_routes.py
serves it and brain/agent.py calls ask().
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict
from typing import Optional

V1, V03 = "1.0", "0.3"
CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")
METHODS = {"SendMessage": ("send", V1), "SendStreamingMessage": ("stream", V1),
           "GetTask": ("get", V1), "CancelTask": ("cancel", V1),
           "message/send": ("send", V03), "message/stream": ("stream", V03),
           "tasks/get": ("get", V03), "tasks/cancel": ("cancel", V03)}
ERR_TASK_NOT_FOUND, ERR_UNSUPPORTED = -32001, -32004
ERR_INVALID_REQUEST, ERR_NO_METHOD, ERR_PARAMS, ERR_INTERNAL = -32600, -32601, -32602, -32603
DONE = {"completed", "failed", "canceled", "cancelled", "rejected"}
WAITS_FOR_US = {"input-required", "input_required", "auth-required", "auth_required"}
MAX_CONTEXTS, MAX_TURNS = 200, 40


def new_id() -> str:
    return uuid.uuid4().hex


# -- text in and out of messages ------------------------------------------------------------

def parts_text(parts) -> str:
    """The text of a message's parts, either version ({"text": ...} or
    {"kind": "text", "text": ...}); other kinds of part are skipped."""
    out = []
    for p in parts if isinstance(parts, list) else []:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            out.append(p["text"])
    return "\n".join(t for t in out if t)


def message(text: str, ver: str, *, agent: bool, context_id: Optional[str] = None) -> dict:
    if ver == V1:
        m = {"messageId": new_id(), "role": "ROLE_AGENT" if agent else "ROLE_USER",
             "parts": [{"text": text}]}
    else:
        m = {"kind": "message", "messageId": new_id(), "role": "agent" if agent else "user",
             "parts": [{"kind": "text", "text": text}]}
    if context_id:
        m["contextId"] = context_id
    return m


# -- server side ----------------------------------------------------------------------------

def card(base_url: str, persona: dict, version: str, others: list) -> dict:
    """The agent card for one persona. `others` are further personas offered here,
    listed as skills so a caller can find their endpoints."""
    name = persona.get("virtual_name") or "Foundry"
    url = f"{base_url.rstrip('/')}/v1/a2a/{name}"
    skills = [{"id": name, "name": name,
               "description": (persona.get("description") or f"The {name} persona of this Foundry Router."),
               "tags": ["chat"], "examples": []}]
    for o in others:
        on = o.get("virtual_name")
        if on and on != name:
            skills.append({"id": on, "name": on,
                           "description": ((o.get("description") or f"The {on} persona.")
                                           + f" Its own endpoint: {base_url.rstrip('/')}/v1/a2a/{on}"),
                           "tags": ["chat", "persona"], "examples": []})
    return {
        "name": name,
        "description": persona.get("description") or f"The {name} persona of a Foundry Router.",
        "version": version,
        "supportedInterfaces": [{"url": url, "protocolBinding": "JSONRPC", "protocolVersion": V1},
                                {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": V03}],
        # 0.3 readers look here
        "url": url, "preferredTransport": "JSONRPC", "protocolVersion": V03,
        "capabilities": {"streaming": True, "pushNotifications": False},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
        "skills": skills,
        "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
    }


def rpc_error(rpc_id, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": msg}}


def parse_call(body) -> tuple:
    """(rpc_id, kind, version, params, error) of one JSON-RPC call."""
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        return (body.get("id") if isinstance(body, dict) else None, None, V1, {},
                rpc_error(body.get("id") if isinstance(body, dict) else None, ERR_INVALID_REQUEST,
                          "not a JSON-RPC 2.0 request"))
    rid = body.get("id")
    hit = METHODS.get(body["method"])
    if hit is None:
        return rid, None, V1, {}, rpc_error(rid, ERR_NO_METHOD, f"unknown method {body['method']!r}")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    return rid, hit[0], hit[1], params, None


def incoming_text(params: dict) -> tuple:
    """(text, context_id) of a SendMessage / message/send call."""
    msg = params.get("message") if isinstance(params.get("message"), dict) else {}
    return parts_text(msg.get("parts")).strip(), (msg.get("contextId") or params.get("contextId") or "")


def send_result(text: str, ver: str, context_id: str) -> dict:
    m = message(text, ver, agent=True, context_id=context_id)
    return {"message": m} if ver == V1 else m


class Contexts:
    """The turns of each A2A conversation (contextId), kept in memory so a caller's
    follow-up message continues it. Oldest conversations are dropped first."""

    def __init__(self):
        self._c: "OrderedDict[str, list]" = OrderedDict()

    def history(self, key: str) -> list:
        h = self._c.get(key)
        if h is not None:
            self._c.move_to_end(key)
        return list(h or [])

    def add(self, key: str, user: str, answer: str) -> None:
        h = self._c.setdefault(key, [])
        h += [{"role": "user", "content": user}, {"role": "assistant", "content": answer}]
        del h[:-MAX_TURNS]
        self._c.move_to_end(key)
        while len(self._c) > MAX_CONTEXTS:
            self._c.popitem(last=False)


# -- client side ----------------------------------------------------------------------------

class A2AError(RuntimeError):
    pass


def _headers(agent: dict, ver: str = "") -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if agent.get("token"):
        h["Authorization"] = f"Bearer {agent['token']}"
    if ver:
        h["A2A-Version"] = ver
    return h


async def fetch_card(client, agent: dict) -> dict:
    """The agent's card: the address itself when it names a .json file, else the
    well-known paths under it."""
    base = str(agent.get("url") or "").strip()
    if not base:
        raise A2AError("no address is set for this agent")
    urls = [base] if base.lower().endswith(".json") else [base.rstrip("/") + p for p in CARD_PATHS]
    last = ""
    for u in urls:
        try:
            r = await client.get(u, headers=_headers(agent), timeout=15.0)
        except Exception as e:                                    # noqa: BLE001
            last = f"{u}: {type(e).__name__}: {e}"
            continue
        if r.status_code == 200:
            try:
                c = r.json()
            except ValueError:
                last = f"{u}: not JSON"
                continue
            if isinstance(c, dict) and (c.get("name") or c.get("skills") is not None):
                return c
            last = f"{u}: not an agent card"
        else:
            last = f"{u}: HTTP {r.status_code}"
    raise A2AError(f"no agent card found ({last})")


def endpoint(card_: dict) -> tuple:
    """(url, version) of the JSON-RPC interface a card offers."""
    for i in card_.get("supportedInterfaces") or []:
        if isinstance(i, dict) and i.get("url") and str(i.get("protocolBinding") or "").upper() == "JSONRPC":
            ver = str(i.get("protocolVersion") or V1)
            return i["url"], (V03 if ver.startswith("0.") else V1)
    pref = str(card_.get("preferredTransport") or "JSONRPC").upper()
    if card_.get("url") and pref == "JSONRPC":
        return card_["url"], V03
    for i in card_.get("additionalInterfaces") or []:
        if isinstance(i, dict) and i.get("url") and str(i.get("transport") or "").upper() == "JSONRPC":
            return i["url"], V03
    raise A2AError("the agent card offers no JSON-RPC interface")


def _state(task: dict) -> str:
    s = str((task.get("status") or {}).get("state") or "").lower()
    return s[len("task_state_"):].replace("_", "-") if s.startswith("task_state_") else s


def _task_text(task: dict) -> str:
    out = [parts_text(a.get("parts")) for a in task.get("artifacts") or [] if isinstance(a, dict)]
    text = "\n\n".join(t for t in out if t)
    if not text:
        sm = (task.get("status") or {}).get("message")
        text = parts_text(sm.get("parts")) if isinstance(sm, dict) else ""
    if not text:
        for m in reversed(task.get("history") or []):
            if isinstance(m, dict) and str(m.get("role") or "").lower().endswith("agent"):
                text = parts_text(m.get("parts"))
                if text:
                    break
    return text


def _unwrap(result) -> tuple:
    """("message" | "task", object) from a result of either version."""
    if not isinstance(result, dict):
        raise A2AError("the agent's reply has no result")
    if isinstance(result.get("message"), dict) and "parts" in result["message"]:
        return "message", result["message"]
    if isinstance(result.get("task"), dict):
        return "task", result["task"]
    if result.get("kind") == "message" or ("parts" in result and "status" not in result):
        return "message", result
    return "task", result


async def _rpc(client, url: str, agent: dict, ver: str, method: str, params: dict, timeout: float) -> dict:
    r = await client.post(url, headers=_headers(agent, ver), timeout=timeout,
                          content=json.dumps({"jsonrpc": "2.0", "id": new_id(), "method": method,
                                              "params": params}))
    if r.status_code >= 400:
        raise A2AError(f"HTTP {r.status_code}: {(r.text or '')[:200]}")
    try:
        body = r.json()
    except ValueError:
        raise A2AError("the agent did not answer with JSON") from None
    if isinstance(body, dict) and body.get("error"):
        e = body["error"]
        raise A2AError(f"{e.get('message') or 'error'} (code {e.get('code')})")
    return body.get("result") if isinstance(body, dict) else None


async def ask(client, agent: dict, text: str, *, poll_seconds: float = 2.0) -> str:
    """Send `text` to a remote agent and return its answer. A task that is not
    finished at once is asked for until it is, or until the agent's time limit."""
    timeout = float(agent.get("timeout_s") or 300)
    deadline = time.monotonic() + timeout
    url, ver = endpoint(await fetch_card(client, agent))
    send, get = ("SendMessage", "GetTask") if ver == V1 else ("message/send", "tasks/get")
    params = {"message": message(text, ver, agent=False)}
    params["configuration"] = {"acceptedOutputModes": ["text/plain"]} if ver == V1 else \
        {"acceptedOutputModes": ["text/plain"], "blocking": True}
    kind, obj = _unwrap(await _rpc(client, url, agent, ver, send, params, timeout))
    while True:
        if kind == "message":
            out = parts_text(obj.get("parts")).strip()
            if not out:
                raise A2AError("the agent answered with no text")
            return out
        st = _state(obj)
        if st in DONE or st in WAITS_FOR_US:
            out = _task_text(obj).strip()
            if st == "completed" and out:
                return out
            if st in WAITS_FOR_US and out:
                return f"[the agent is waiting for more input and said:]\n{out}"
            raise A2AError(f"the agent's task ended as {st or 'unknown'}" + (f": {out[:300]}" if out else ""))
        if time.monotonic() >= deadline or not obj.get("id"):
            raise A2AError(f"the agent did not finish within {timeout:.0f}s (task state {st or 'unknown'})")
        await asyncio.sleep(poll_seconds)
        kind, obj = _unwrap(await _rpc(client, url, agent, ver, get, {"id": obj["id"]},
                                       max(5.0, min(60.0, deadline - time.monotonic()))))


def find_agent(agents, name: str) -> Optional[dict]:
    name = str(name or "").strip().lower()
    for a in agents or []:
        d = a if isinstance(a, dict) else a.model_dump()
        if str(d.get("name") or "").strip().lower() == name and d.get("enabled", True):
            return d
    return None

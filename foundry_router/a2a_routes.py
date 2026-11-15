"""The router as an A2A agent (v0.181.158): agent cards and the JSON-RPC endpoint.

A call is answered by sending the message through the router's own
/v1/chat/completions (in process), so a persona behaves for an A2A caller exactly
as for a chat program: same route, tools, helpers, guardrails, logging, and the
same client key when client keys are on. Protocol details: a2a.py.
"""

from __future__ import annotations

import json
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__, a2a
from .errors import describe_exception

router = APIRouter()
_contexts = a2a.Contexts()


def _svc(request: Request):
    return request.app.state.services


def _cfg(request: Request):
    return _svc(request).config_store.config.a2a


def offered(svc, cfg) -> list:
    """The personas an A2A caller may reach: the default one first, then the
    listed ones. Disabled personas are never offered."""
    rows = {p["virtual_name"].lower(): p for p in svc.personas.list(enabled_only=True)}
    names = [cfg.default_persona] + [n for n in cfg.personas if n.lower() != cfg.default_persona.lower()]
    return [rows[n.lower()] for n in names if n and n.lower() in rows]


def _persona(svc, cfg, name: Optional[str]):
    rows = offered(svc, cfg)
    if not name:
        return (rows[0] if rows and rows[0]["virtual_name"].lower() == cfg.default_persona.lower()
                else None), rows
    return next((p for p in rows if p["virtual_name"].lower() == name.lower()), None), rows


def _base(request: Request, cfg) -> str:
    return (cfg.public_url or str(request.base_url)).rstrip("/")


def _off():
    return JSONResponse({"error": "A2A is switched off on this router (Backends → Pool → A2A agents)."},
                        status_code=404)


async def _card(request: Request, name: Optional[str]):
    svc, cfg = _svc(request), _cfg(request)
    if not cfg.serve:
        return _off()
    p, rows = _persona(svc, cfg, name)
    if p is None:
        return JSONResponse({"error": f"no persona {name or cfg.default_persona!r} is offered over A2A"},
                            status_code=404)
    return a2a.card(_base(request, cfg), p, __version__, rows)


@router.get("/.well-known/agent-card.json")
async def card_default(request: Request):
    return await _card(request, None)


@router.get("/.well-known/agent.json")
async def card_default_old(request: Request):
    return await _card(request, None)


@router.get("/v1/a2a/{persona}/agent-card.json")
async def card_persona(request: Request, persona: str):
    return await _card(request, persona)


@router.get("/v1/a2a/{persona}/.well-known/agent-card.json")
async def card_persona_wk(request: Request, persona: str):
    return await _card(request, persona)


async def _chat(request: Request, persona: str, messages: list) -> str:
    """One chat through the router's own endpoint, in process."""
    headers = {k: v for k, v in request.headers.items()
               if k.lower() in ("authorization", "x-api-key", "x-foundry-client", "user-agent")}
    headers["content-type"] = "application/json"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app),
                                 base_url="http://a2a.internal", timeout=None) as c:
        r = await c.post("/v1/chat/completions", headers=headers,
                         content=json.dumps({"model": persona, "messages": messages, "stream": False}))
    try:
        body = r.json()
    except ValueError:
        raise a2a.A2AError(f"HTTP {r.status_code}: {(r.text or '')[:300]}") from None
    if r.status_code >= 400 or not isinstance(body, dict) or body.get("error"):
        err = body.get("error") if isinstance(body, dict) else None
        msg = err.get("message") if isinstance(err, dict) else err
        raise a2a.A2AError(str(msg or f"HTTP {r.status_code}"))
    msg = ((body.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content") or ""
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    return str(text).strip()


async def _rpc(request: Request, name: Optional[str]):
    svc, cfg = _svc(request), _cfg(request)
    if not cfg.serve:
        return _off()
    try:
        body = await request.json()
    except Exception:                                             # noqa: BLE001
        return JSONResponse(a2a.rpc_error(None, -32700, "the request body is not JSON"))
    rid, kind, ver, params, err = a2a.parse_call(body)
    if err is not None:
        return JSONResponse(err)
    if kind == "get":
        return JSONResponse(a2a.rpc_error(rid, a2a.ERR_TASK_NOT_FOUND,
                                          "this agent answers with a message at once and keeps no tasks"))
    if kind == "cancel":
        return JSONResponse(a2a.rpc_error(rid, a2a.ERR_UNSUPPORTED, "there are no tasks to cancel"))
    p, _ = _persona(svc, cfg, name)
    if p is None:
        return JSONResponse(a2a.rpc_error(rid, a2a.ERR_PARAMS,
                                          f"no persona {name or cfg.default_persona!r} is offered over A2A"))
    text, ctx_id = a2a.incoming_text(params)
    if not text:
        return JSONResponse(a2a.rpc_error(rid, a2a.ERR_PARAMS, "the message has no text part"))
    ctx_id = ctx_id or a2a.new_id()
    key = f"{p['virtual_name']}:{ctx_id}"
    try:
        answer = await _chat(request, p["virtual_name"],
                             _contexts.history(key) + [{"role": "user", "content": text}])
    except Exception as e:                                        # noqa: BLE001
        return JSONResponse(a2a.rpc_error(rid, a2a.ERR_INTERNAL, describe_exception(e)))
    _contexts.add(key, text, answer)
    result = a2a.send_result(answer or "(no answer)", ver, ctx_id)
    if kind == "stream":
        # one event: the finished message (the card says so; no partial pieces)
        async def events():
            yield "data: " + json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})


@router.post("/v1/a2a")
async def rpc_default(request: Request):
    return await _rpc(request, None)


@router.post("/v1/a2a/{persona}")
async def rpc_persona(request: Request, persona: str):
    return await _rpc(request, persona)

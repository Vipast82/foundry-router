"""The router as an A2A agent (v0.181.158): agent cards and the JSON-RPC endpoint.

A call is answered by sending the message through the router's own
/v1/chat/completions (in process), so a persona behaves for an A2A caller exactly
as for a chat program: same route, tools, helpers, guardrails, logging, and the
same client key when client keys are on. Protocol details: a2a.py.

v0.181.160: a streaming call gets the answer piece by piece, and a call that
asks not to wait gets a task it can read (GetTask) or stop (CancelTask). Tasks
and conversations are kept in memory: a router restart forgets them.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__, a2a
from .errors import describe_exception

router = APIRouter()
_contexts = a2a.Contexts()
_tasks = a2a.Tasks()
_FORWARD = ("authorization", "x-api-key", "x-foundry-client", "user-agent")


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


async def _chat_stream(app, scope_from: dict, fwd: dict, persona: str, messages: list):
    """The same chat, streamed: yields the answer's text piece by piece. The app is
    called directly (httpx's in-process transport hands a response over only when
    it is complete)."""
    body = json.dumps({"model": persona, "messages": messages, "stream": True}).encode()
    headers = [(k.encode(), v.encode()) for k, v in fwd.items()]
    headers += [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                (b"host", b"a2a.internal")]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
             "query_string": b"", "root_path": "", "headers": headers,
             "client": scope_from.get("client"), "server": scope_from.get("server"), "app": app}
    q: asyncio.Queue = asyncio.Queue()
    given = False

    async def receive():
        nonlocal given
        if not given:
            given = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()                    # the caller never hangs up on us

    async def send(msg):
        await q.put(msg)

    async def run():
        try:
            await app(scope, receive, send)
        except Exception as e:                                    # noqa: BLE001
            await q.put({"type": "error", "error": describe_exception(e)})
        finally:
            await q.put(None)
    runner = asyncio.create_task(run())
    status, buf = 200, b""
    try:
        while True:
            msg = await q.get()
            if msg is None:
                break
            if msg["type"] == "error":
                raise a2a.A2AError(msg["error"])
            if msg["type"] == "http.response.start":
                status = msg["status"]
            elif msg["type"] == "http.response.body":
                buf += msg.get("body", b"")
                if status >= 400:
                    continue
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                        continue
                    try:
                        d = json.loads(line[5:])
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get("error"):
                        e = d["error"]
                        raise a2a.A2AError(str(e.get("message") if isinstance(e, dict) else e))
                    delta = ((d.get("choices") or [{}])[0].get("delta") or {}).get("content") \
                        if isinstance(d, dict) else None
                    if isinstance(delta, str) and delta:
                        yield delta
        if status >= 400:
            try:
                e = json.loads(buf).get("error")
                msg = e.get("message") if isinstance(e, dict) else e
            except Exception:                                     # noqa: BLE001
                msg = None
            raise a2a.A2AError(str(msg or f"HTTP {status}"))
    finally:
        if not runner.done():
            runner.cancel()


async def _run_task(app, scope_from: dict, fwd: dict, rec: dict, key: str, text: str,
                    on_piece=None) -> None:
    """Work on one task to its end. The result is kept in `rec` for GetTask."""
    parts: list = []
    try:
        async for piece in _chat_stream(app, scope_from, fwd, rec["persona"],
                                        _contexts.history(key) + [{"role": "user", "content": text}]):
            parts.append(piece)
            if on_piece is not None:
                await on_piece(piece)
        rec["text"] = "".join(parts).strip() or "(no answer)"
        rec["state"] = "completed"
        _contexts.add(key, text, rec["text"])
    except asyncio.CancelledError:
        rec["state"] = "canceled"
        raise
    except Exception as e:                                        # noqa: BLE001
        rec["state"], rec["error"] = "failed", describe_exception(e)
    finally:
        rec["finished"] = time.time()


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
    if kind in ("get", "cancel"):
        rec = _tasks.get(params.get("id"))
        if rec is None:
            return JSONResponse(a2a.rpc_error(rid, a2a.ERR_TASK_NOT_FOUND,
                                              f"no task {params.get('id')!r} (tasks are kept in memory "
                                              "and lost when the router restarts)"))
        if kind == "cancel":
            if rec["state"] != "working":
                if rec["state"] == "canceled":                   # asking twice is fine
                    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": _tasks.view(rec, ver)})
                return JSONResponse(a2a.rpc_error(rid, a2a.ERR_NOT_CANCELABLE,
                                                  f"the task already ended as {rec['state']}"))
            runner = rec.get("runner")
            if runner is not None and not runner.done():
                runner.cancel()
            rec["state"] = "canceled"
        out = _tasks.view(rec, ver)
        return JSONResponse({"jsonrpc": "2.0", "id": rid,
                             "result": out})
    p, _ = _persona(svc, cfg, name)
    if p is None:
        return JSONResponse(a2a.rpc_error(rid, a2a.ERR_PARAMS,
                                          f"no persona {name or cfg.default_persona!r} is offered over A2A"))
    text, ctx_id = a2a.incoming_text(params)
    if not text:
        return JSONResponse(a2a.rpc_error(rid, a2a.ERR_PARAMS, "the message has no text part"))
    ctx_id = ctx_id or a2a.new_id()
    key = f"{p['virtual_name']}:{ctx_id}"
    fwd = {k.lower(): v for k, v in request.headers.items() if k.lower() in _FORWARD}
    scope_from = {"client": request.scope.get("client"), "server": request.scope.get("server")}
    if kind == "stream":
        # v0.181.160: a task, then the text as it is written, then the final status
        rec = _tasks.new(p["virtual_name"], ctx_id, ver)
        q: asyncio.Queue = asyncio.Queue()

        async def piece(t):
            await q.put(t)

        async def work():
            try:
                await _run_task(request.app, scope_from, fwd, rec, key, text, piece)
            finally:
                await q.put(None)
        rec["runner"] = asyncio.create_task(work())

        def frame(result) -> str:
            return "data: " + json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n\n"

        async def events():
            try:
                yield frame(a2a.stream_task(_tasks.view(rec, ver), ver))
                first = True
                while True:
                    t = await q.get()
                    if t is None:
                        break
                    yield frame(a2a.stream_text(rec["id"], ctx_id, t, ver, append=not first))
                    first = False
                if rec["state"] == "completed" and first:        # an answer that came in no pieces
                    yield frame(a2a.stream_text(rec["id"], ctx_id, rec["text"], ver, append=False, last=True))
                yield frame(a2a.stream_status(_tasks.view(rec, ver), ver))
            finally:
                if not rec["runner"].done():                     # the caller went away
                    rec["runner"].cancel()
        return StreamingResponse(events(), media_type="text/event-stream")
    if a2a.wants_task(params, ver):
        # the caller does not want to wait: a task now, the answer through GetTask
        rec = _tasks.new(p["virtual_name"], ctx_id, ver)
        rec["runner"] = asyncio.create_task(_run_task(request.app, scope_from, fwd, rec, key, text))
        out = _tasks.view(rec, ver)
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {"task": out} if ver == a2a.V1 else out})
    try:
        answer = await _chat(request, p["virtual_name"],
                             _contexts.history(key) + [{"role": "user", "content": text}])
    except Exception as e:                                        # noqa: BLE001
        return JSONResponse(a2a.rpc_error(rid, a2a.ERR_INTERNAL, describe_exception(e)))
    _contexts.add(key, text, answer)
    result = a2a.send_result(answer or "(no answer)", ver, ctx_id)
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})


@router.post("/v1/a2a")
async def rpc_default(request: Request):
    return await _rpc(request, None)


@router.post("/v1/a2a/{persona}")
async def rpc_persona(request: Request, persona: str):
    return await _rpc(request, persona)

"""HTTP surface of the operator chat (chat_engine.py): /control/api/chat/*.

Same credential as the rest of the control center: the operator session cookie
(fr_cc) on every call, plus the CSRF header and same-origin checks on every
POST/PUT/DELETE (control_api._op). A turn runs under the signed-in operator's
name and project allow-list; nothing here is reachable with a bare client key.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .chat_engine import CLASS_LABEL, CONFIRM_CLASSES, ChatError
from .control_api import _body, _op
from .control_center import ControlError

router = APIRouter()
P = "/control/api/chat"


def _eng(request: Request):
    return request.app.state.services.chat


def _guard(fn):
    import functools

    @functools.wraps(fn)
    async def wrap(*a, **k):
        try:
            return await fn(*a, **k)
        except (ChatError, ControlError) as e:
            return JSONResponse({"error": str(e), "code": e.code, **e.extra}, status_code=e.status)
    return wrap


@router.get(P + "/threads")
@_guard
async def threads(request: Request, limit: int = 50):
    s = _op(request)
    return {"threads": _eng(request).threads(s["actor"], limit)}


@router.post(P + "/threads")
@_guard
async def create_thread(request: Request):
    s = _op(request, mutate=True)
    b = await _body(request)
    return _eng(request).create_thread(s["actor"], str(b.get("title") or ""),
                                       str(b.get("project") or ""))


@router.get(P + "/threads/{thread_id}")
@_guard
async def thread(request: Request, thread_id: str):
    s = _op(request)
    return _eng(request).thread(thread_id, s["actor"])


@router.post(P + "/threads/{thread_id}/rename")
@_guard
async def rename_thread(request: Request, thread_id: str):
    s = _op(request, mutate=True)
    b = await _body(request)
    return _eng(request).rename_thread(thread_id, s["actor"], str(b.get("title") or ""))


@router.post(P + "/threads/{thread_id}/delete")
@_guard
async def delete_thread(request: Request, thread_id: str):
    s = _op(request, mutate=True)
    _eng(request).delete_thread(thread_id, s["actor"])
    return {"ok": True}


@router.post(P + "/threads/{thread_id}/turns")
@_guard
async def turn(request: Request, thread_id: str):
    """One operator message -> one turn (model calls, auto tool calls, a confirm
    card when needed). Synchronous: the reply is the stored turn."""
    s = _op(request, mutate=True)
    b = await _body(request)
    eng = _eng(request)
    if not eng.svc.control.rate_ok(s["actor"]):
        raise ControlError("too many chat turns; wait a minute", "rate_limited", 429)
    return await eng.turn(thread_id, str(b.get("text") or ""), s["actor"], s["projects"],
                          s["scopes"])


@router.post(P + "/confirms/{confirm_id}")
@_guard
async def decide(request: Request, confirm_id: str):
    """{"decision": "confirm"|"decline", "picked": [call ids]}"""
    s = _op(request, mutate=True)
    b = await _body(request)
    picked = b.get("picked")
    if picked is not None and not isinstance(picked, list):
        raise ChatError("picked must be a list of call ids")
    return await _eng(request).decide(confirm_id, s["actor"], s["projects"],
                                      [str(x) for x in picked] if picked is not None else None,
                                      str(b.get("decision") or "confirm"), s["scopes"])


@router.get(P + "/settings")
@_guard
async def get_settings(request: Request):
    _op(request)
    eng = _eng(request)
    return {"auto_classes": eng.auto_classes(), "classes": [
        {"id": c, "label": CLASS_LABEL[c]} for c in CONFIRM_CLASSES], "catalog": eng.catalog()}


@router.put(P + "/settings")
@_guard
async def put_settings(request: Request):
    s = _op(request, mutate=True)
    if s["projects"]:
        raise ControlError("auto-run classes are global; they need an operator session that is not "
                           "restricted to projects", "forbidden", 403)
    b = await _body(request)
    return {"auto_classes": _eng(request).set_auto_classes(b.get("auto_classes") or {}, s["actor"])}


@router.get(P + "/backend")
@_guard
async def get_backend(request: Request):
    """The chat's own model chain and what it could use (Chat settings)."""
    _op(request)
    from .chat_router_tools import chat_backend_view
    return chat_backend_view(_eng(request))


@router.put(P + "/backend")
@_guard
async def put_backend(request: Request):
    """{routes: [{backend, model}]} — [] = the built-in chain."""
    s = _op(request, mutate=True)
    if s["projects"]:
        raise ControlError("the chat backend is global; it needs an operator session that is not "
                           "restricted to projects", "forbidden", 403)
    from .chat_router_tools import apply_chat_routes, chat_backend_view
    b = await _body(request)
    eng = _eng(request)
    await apply_chat_routes(eng, b.get("routes") or [], f"{s['actor']} (Chat settings)")
    eng._audit("", "", "chat_backend", s["actor"], "chat backend: " + (", ".join(
        f"{r.get('model')}@{r.get('backend')}" for r in b.get("routes") or []) or "built-in chain"))
    return chat_backend_view(eng)


@router.get(P + "/audit")
@_guard
async def audit(request: Request, thread: str = "", limit: int = 200):
    s = _op(request)
    eng = _eng(request)
    if thread:
        eng._thread_row(thread, s["actor"])          # the operator's own thread only
        return {"audit": eng.audit(thread, limit)}
    rows = [r for r in eng.audit("", limit * 3) if r.get("actor") == s["actor"]][:limit]
    return {"audit": rows}


@router.get("/control/api/services")
@_guard
async def services(request: Request):
    """The service registry for the Automation page's Services tab: what the Foundry Client
    reports (status, provider, needed-by, MCP probes) and the sprints waiting for a service.
    Restart / enable buttons post the client_restart / client_service_enable actions."""
    s = _op(request)
    return _eng(request).services_view(s["projects"], full=True)

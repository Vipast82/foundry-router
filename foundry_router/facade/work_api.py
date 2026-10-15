"""Work-binding API for a development coordinator (orchestration.py).

Needs a client key with the `orchestration` scope (or `admin`):
  POST /v1/foundry/work/sessions            bind a coding session to project/task/run/role
  POST /v1/foundry/work/sessions/{id}/end   the session is finished (outcome)
  GET  /v1/foundry/work/sessions/{id}       its binding (or 404)
  PUT  /v1/foundry/work/tasks/{project}/{task}   the coordinator's latest task state
  POST /v1/foundry/work/events              lifecycle events (event_id dedupes resends)
  GET  /v1/foundry/work/usage?project=&task=&run=&session=   measured usage
  GET  /v1/foundry/work/denials?session=&since=   Foundry refusals for a session

The admin UI reads GET /admin/api/work (ui/routes.py).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import client_auth
from ..orchestration import WorkError

router = APIRouter()


def _work(request: Request):
    return request.app.state.services.work


def _bad(e: Exception, status: int = 400):
    return JSONResponse({"error": str(e), "code": "invalid_work_binding"}, status_code=status)


@router.post("/v1/foundry/work/sessions")
async def bind_session(request: Request):
    try:
        data = await request.json()
        rec = _work(request).bind(data if isinstance(data, dict) else {},
                                  client_id=client_auth.current().get("client_id") or "")
    except (WorkError, ValueError) as e:
        return _bad(e, 409 if "bound to project" in str(e) else 400)
    return {"ok": True, "binding": rec}


@router.post("/v1/foundry/work/sessions/{session_id}/end")
async def end_session(session_id: str, request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    try:
        ended = _work(request).end(session_id, (body or {}).get("outcome") or "")
    except WorkError as e:
        return _bad(e)
    return {"ok": True, "ended": ended}


@router.get("/v1/foundry/work/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    rows = request.app.state.services.db.query(
        "SELECT * FROM work_sessions WHERE session_id=?", (session_id,))
    if not rows:
        return JSONResponse({"error": "unknown session", "code": "not_found"}, status_code=404)
    return {"binding": dict(rows[0])}


@router.put("/v1/foundry/work/tasks/{project_id}/{task_id}")
async def put_task(project_id: str, task_id: str, request: Request):
    try:
        data = await request.json()
        return {"ok": True, **_work(request).put_task(project_id, task_id,
                                                         data if isinstance(data, dict) else {})}
    except (WorkError, ValueError) as e:
        return _bad(e)


@router.post("/v1/foundry/work/events")
async def add_events(request: Request):
    try:
        data = await request.json()
    except ValueError as e:
        return _bad(e)
    events = data.get("events") if isinstance(data, dict) else data
    return {"ok": True, "recorded": _work(request).add_events(events if isinstance(events, list)
                                                               else [])}


@router.get("/v1/foundry/work/usage")
async def usage(request: Request, project: str = "", task: str = "", run: str = "",
                session: str = ""):
    if not (project or task or run or session):
        return _bad(ValueError("give at least one of project, task, run, session"))
    return _work(request).usage(project, task, run, session)


@router.get("/v1/foundry/work/denials")
async def denials(request: Request, session: str = "", run: str = "", since: float = 0):
    """Refusals recorded by the budget ledger, so a coordinator can tell a
    budget/pause stop from a coding failure without parsing transcripts."""
    if not (session or run):
        return _bad(ValueError("give session or run"))
    col, val = ("session_id", session) if session else ("run_id", run)
    rows = request.app.state.services.db.query(
        f"SELECT ts, persona, backend, model, account, rule, detail FROM usage_events "
        f"WHERE status='denied' AND {col}=? AND ts >= ? ORDER BY ts DESC LIMIT 50",
        (val, since or time.time() - 86400))
    return {"denials": [dict(r) for r in rows]}

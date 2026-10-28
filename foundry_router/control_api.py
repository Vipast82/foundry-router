"""HTTP surface of the coordinator control center (control_center.py).

Three audiences, three credentials:

  browser (operator)   /ui/control (static page) and /control/api/*: an HttpOnly,
                       SameSite=Strict session cookie made once from a client key
                       with the `operator` (or `admin`) scope. The key itself never
                       stays in the browser. Every mutation also needs the
                       session's CSRF token header and a same-origin request.
  workstation bridge   /v1/foundry/telemetry/*: a client key with the `telemetry`
                       (or `orchestration`) scope. Read-only observations in.
  coordinator          /v1/foundry/control/*: the `orchestration` scope. Claims
                       operator actions and reports what happened to them.

Viewing (any GET, the event stream) changes nothing anywhere and causes no
model call.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import client_auth
from .control_center import CONTRACT, ControlError

router = APIRouter()
STATIC = Path(__file__).resolve().parent / "ui" / "static" / "control.html"
COOKIE = "fr_cc"
MAX_STREAMS_PER_ACTOR = 16
_streams: dict[str, int] = {}


def _cc(request: Request):
    return request.app.state.services.control


def _err(e: ControlError) -> JSONResponse:
    return JSONResponse({"error": str(e), "code": e.code, **e.extra}, status_code=e.status)


def _op(request: Request, mutate: bool = False) -> dict:
    s = _cc(request).op_session(request.cookies.get(COOKIE, ""))
    if not s:
        raise ControlError("sign in to the control center", "login_required", 401)
    if mutate:
        tok = request.headers.get("x-foundry-csrf", "")
        if not tok or not hmac.compare_digest(tok.encode(), (s["csrf"] or "").encode()):
            raise ControlError("missing or wrong CSRF token", "csrf", 403)
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin:
            host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
            if urlsplit(origin).netloc.lower() != host.lower():
                raise ControlError("cross-origin request refused", "csrf", 403)
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            raise ControlError("cross-site request refused", "csrf", 403)
    return s


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except ValueError:
        raise ControlError("body must be JSON") from None
    if not isinstance(b, dict):
        raise ControlError("body must be a JSON object")
    return b


def _guard(fn):
    import functools

    @functools.wraps(fn)
    async def wrap(*a, **k):
        try:
            return await fn(*a, **k)
        except ControlError as e:
            return _err(e)
    return wrap


# --------------------------------------------------------------------------- page

@router.get("/ui/control")
async def control_page():
    return FileResponse(STATIC, media_type="text/html",
                        headers={"Cache-Control": "no-store",
                                 "Content-Security-Policy":
                                     "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                     "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                     "connect-src 'self'; frame-ancestors 'none'; "
                                     "base-uri 'none'; form-action 'none'",
                                 "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "same-origin"})


# --------------------------------------------------------------------------- operator session

@router.post("/control/api/login")
@_guard
async def login(request: Request):
    b = await _body(request)
    key = str(b.get("key") or "").strip()
    if not key:
        raise ControlError("paste a client key with the operator scope", "operator_key_required", 401)
    cfg = request.app.state.services.config_store.config
    client, _ = client_auth.identify(cfg, {"authorization": f"Bearer {key}"})
    out = _cc(request).login(key, client)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp = JSONResponse({k: v for k, v in out.items() if k != "token"})
    resp.set_cookie(COOKIE, out["token"], httponly=True, samesite="strict", secure=secure,
                    path="/control", max_age=int(out["expires_ts"] - _cc(request).clock()))
    return resp


@router.get("/control/api/session")
@_guard
async def session_info(request: Request):
    s = _op(request)
    return {"actor": s["actor"], "csrf": s["csrf"], "expires_ts": s["expires_ts"],
            "projects": s["projects"], "contract": CONTRACT}


@router.post("/control/api/logout")
@_guard
async def logout(request: Request):
    _op(request, mutate=True)
    _cc(request).logout(request.cookies.get(COOKIE, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/control")
    return resp


# --------------------------------------------------------------------------- reads

@router.get("/control/api/overview")
@_guard
async def overview(request: Request, hours: float = 168):
    s = _op(request)
    return _cc(request).overview(s["projects"], hours=max(1.0, min(24 * 90.0, hours)))


@router.get("/control/api/sessions/{session_id}")
@_guard
async def session(request: Request, session_id: str):
    s = _op(request)
    return _cc(request).session(session_id, s["projects"])


@router.get("/control/api/sessions/{session_id}/parts")
@_guard
async def parts(request: Request, session_id: str, after: int = 0, before: float = 0,
                limit: int = 200):
    s = _op(request)
    return _cc(request).parts(session_id, s["projects"], after, before, limit)


@router.get("/control/api/tasks/{project_id}/{task_id}/transitions")
@_guard
async def transitions(request: Request, project_id: str, task_id: str, limit: int = 200):
    s = _op(request)
    if s["projects"] and project_id not in s["projects"]:
        raise ControlError("unknown task", "not_found", 404)
    return {"transitions": _cc(request).transitions(project_id, task_id, limit)}


@router.get("/control/api/tasks/{project_id}/{task_id}/view")
@_guard
async def task_view(request: Request, project_id: str, task_id: str):
    """v0.122 sprint view of one task: readiness, required-work checklist,
    evidence per check, recovered results, repeated findings, budgets and
    decisions (control_center.task_view), plus the verification view."""
    s = _op(request)
    return _cc(request).task_view_for(project_id, task_id, s["projects"])


@router.get("/control/api/tasks/{project_id}/{task_id}/helpers")
@_guard
async def task_helpers(request: Request, project_id: str, task_id: str):
    """Ordinary and lane helper units of one task with phase, reason code and
    disposition (a superset of /lanes' units; /lanes is unchanged)."""
    s = _op(request)
    return _cc(request).task_helpers(project_id, task_id, s["projects"])


@router.get("/control/api/tasks/{project_id}/{task_id}/totals")
@_guard
async def task_totals(request: Request, project_id: str, task_id: str):
    s = _op(request)
    return _cc(request).task_totals(project_id, task_id, s["projects"])


@router.get("/control/api/search")
@_guard
async def search(request: Request, q: str = "", project: str = "", session: str = "",
                 kind: str = "", limit: int = 50, offset: int = 0):
    s = _op(request)
    return _cc(request).search(q, s["projects"], project, session, kind, limit, offset)


@router.get("/control/api/snapshots")
@_guard
async def snapshots(request: Request, kind: str, project: str = "", session: str = "",
                    at: float = 0):
    s = _op(request)
    if kind not in ("mcp", "todo", "capabilities", "config", "memory"):
        raise ControlError("kind must be mcp|todo|capabilities|config|memory")
    if s["projects"] and project not in s["projects"]:
        raise ControlError("unknown project", "not_found", 404)
    snap = _cc(request).snapshot(kind, project, session, at)
    return {"snapshot": snap, "requested_at": at or None,
            "note": None if snap else "nothing captured for this scope"
                                      + (" at or before that time" if at else "")}


@router.get("/control/api/memory")
@_guard
async def memory(request: Request, project: str):
    """The project's memory as the coordinator last reported it (files, entries,
    the slice each session got, recent notes); the Memory tab's data."""
    s = _op(request)
    if not project:
        raise ControlError("project is required")
    # "@playbook" is the cross-project playbook snapshot: readable by any operator
    if project != "@playbook" and s["projects"] and project not in s["projects"]:
        raise ControlError("unknown project", "not_found", 404)
    from .memory_view import summary
    snap = _cc(request).snapshot("memory", project)
    return {"project": project, "snapshot": snap, "summary": summary(snap),
            "note": None if snap else (
                "the coordinator has not reported a playbook (no --playbook path, or the "
                "coordinator is offline)" if project == "@playbook" else
                "the coordinator has not reported memory for this project "
                "(docs/memory missing, [automation] project_memory off, or the "
                "coordinator is offline)")}


@router.get("/control/api/memory/search")
@_guard
async def memory_search(request: Request, project: str, q: str = "", file: str = "",
                        limit: int = 20):
    s = _op(request)
    if project != "@playbook" and s["projects"] and project not in s["projects"]:
        raise ControlError("unknown project", "not_found", 404)
    from .memory_view import search
    snap = _cc(request).snapshot("memory", project)
    ents = ((snap or {}).get("data") or {}).get("entries") or []
    return {"project": project, "query": q, "results": search(ents, q, max(1, min(int(limit), 50)),
                                                               file)}


@router.get("/control/api/graph")
@_guard
async def graph(request: Request, project: str = "", task: str = "", minutes: float = 30):
    s = _op(request)
    return _cc(request).route_graph(s["projects"], project, task, max(1.0, min(1440.0, minutes)))


@router.get("/control/api/tracks")
@_guard
async def tracks(request: Request, project: str = ""):
    """Team Lead tracks as the coordinator's heartbeat reports them: per project
    max_agents, the tracks (sprints, state, worktree, needs, merge stage) and
    the Studio timeline (resource journal). Read-only; actions go through
    POST /control/api/actions (track_start, track_stop, track_merge, ...)."""
    s = _op(request)
    if project and s["projects"] and project not in s["projects"]:
        raise ControlError("unknown project", "not_found", 404)
    return _cc(request).tracks_view(s["projects"], project)


@router.get("/control/api/queue")
@_guard
async def queue(request: Request, project: str):
    """One project's approved queue (position = priority, deps, approved_by,
    note, track), from the coordinator bridge's task observations. Read-only;
    edits go through POST /control/api/actions (queue_approve, queue_reorder,
    queue_deps, queue_requeue)."""
    s = _op(request)
    return _cc(request).queue_view(s["projects"], project)


@router.get("/control/api/inbox")
@_guard
async def inbox(request: Request):
    """"Your decisions" (review A3): every open decision across projects with
    its recommended default, deadline and — for Lead escalations — the one-line
    reason it reached you, plus open operator questions and how many decisions
    the Lead resolved itself this week."""
    s = _op(request)
    return _cc(request).decisions_inbox(s["projects"])


@router.get("/control/api/lanes")
@_guard
async def lanes(request: Request, project: str, task: str = ""):
    """Proactive helper lanes: units (assignment, parent, model/backend, defer
    reason, findings, proposed diff, feedback), artifacts and kill-switch state."""
    s = _op(request)
    return _cc(request).lanes(s["projects"], project, task)


@router.get("/control/api/artifacts/{artifact_id}/thumbnail")
@_guard
async def artifact_thumbnail(request: Request, artifact_id: str):
    from fastapi.responses import Response
    s = _op(request)
    data, mime = _cc(request).artifact_thumb(artifact_id, s["projects"])
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "private, max-age=3600",
                             "X-Content-Type-Options": "nosniff",
                             "Content-Security-Policy": "default-src 'none'"})


@router.get("/control/api/capabilities")
@_guard
async def capabilities(request: Request):
    s = _op(request)
    cc = _cc(request)
    return {"contract": CONTRACT, "capture_boundary": cc.capture_boundary(),
            "sources": cc.sources(s["projects"]),
            "delivery": {"visual": "in-page, while a control-center page is open",
                         "audio": "Web Audio in an open page after you press Enable sound",
                         "notification": "browser notifications from an open page after you "
                                         "allow them; no background push service is installed",
                         "closed_page": "nothing plays; alerts stay stored here and appear "
                                        "when a page opens again"}}


# --------------------------------------------------------------------------- alerts

@router.get("/control/api/alerts")
@_guard
async def alerts(request: Request, status: str = "open", limit: int = 100, log: int = 0):
    s = _op(request)
    return {"alerts": _cc(request).alerts(s["projects"], status, limit, bool(log))}


@router.post("/control/api/alerts/{alert_id}/ack")
@_guard
async def ack(request: Request, alert_id: str):
    s = _op(request, mutate=True)
    return _cc(request).acknowledge(alert_id, s["actor"], s["projects"])


@router.post("/control/api/alerts/{alert_id}/delivery")
@_guard
async def delivery(request: Request, alert_id: str):
    s = _op(request, mutate=True)
    b = await _body(request)
    _cc(request).delivery(alert_id, s["actor"], str(b.get("channel")), str(b.get("result")),
                          str(b.get("detail") or ""))
    return {"ok": True}


# --------------------------------------------------------------------------- actions

@router.get("/control/api/actions")
@_guard
async def actions(request: Request, project: str = "", task: str = "", status: str = "",
                  limit: int = 100):
    s = _op(request)
    return {"actions": _cc(request).actions(s["projects"], project, task, status, limit)}


@router.get("/control/api/actions/{action_id}")
@_guard
async def action(request: Request, action_id: str):
    s = _op(request)
    a = _cc(request).action_view(action_id)
    if not a or (s["projects"] and a["project_id"] not in s["projects"]):
        raise ControlError("no such action", "not_found", 404)
    return a


@router.post("/control/api/actions")
@_guard
async def create_action(request: Request):
    s = _op(request, mutate=True)
    b = await _body(request)
    return _cc(request).create_action(s["actor"], s["projects"], b, scopes=s["scopes"])


@router.post("/control/api/actions/{action_id}/cancel")
@_guard
async def cancel_action(request: Request, action_id: str):
    s = _op(request, mutate=True)
    return _cc(request).cancel_action(action_id, s["actor"], s["projects"])


# --------------------------------------------------------------------------- settings / drafts

@router.get("/control/api/settings")
@_guard
async def get_settings(request: Request):
    _op(request)
    from .control_center import DEFAULTS, SETTING_LIMITS
    return {"settings": _cc(request).settings(), "defaults": DEFAULTS, "limits": SETTING_LIMITS}


@router.put("/control/api/settings")
@_guard
async def put_settings(request: Request):
    s = _op(request, mutate=True)
    if not {"admin", "operator"} & set(s["scopes"]):
        raise ControlError("operator scope required", "forbidden", 403)
    return {"settings": _cc(request).update_settings(await _body(request), s["actor"])}


# --------------------------------------------------------------------------- daily digest

@router.get("/control/api/digests")
@_guard
async def digests(request: Request):
    """The last 7 daily digests (alerts.py). They span every project, so a
    project-restricted operator gets none."""
    s = _op(request)
    al = request.app.state.services.alerts
    if s["projects"]:
        return {"digests": [], "note": "digests cover every project; this session is restricted"}
    return {"digests": await asyncio.to_thread(al.digests, 7),
            "digest_hour": al.cfg().digest_hour}


@router.post("/control/api/digests")
@_guard
async def build_digest(request: Request):
    s = _op(request, mutate=True)
    if s["projects"]:
        raise ControlError("digests cover every project; this session is restricted",
                           "forbidden", 403)
    al = request.app.state.services.alerts
    return {"digest": await asyncio.to_thread(al.build_digest, "manual", s["actor"])}


@router.get("/control/api/drafts")
@_guard
async def drafts(request: Request):
    s = _op(request)
    return {"drafts": _cc(request).drafts(s["actor"])}


@router.put("/control/api/drafts")
@_guard
async def put_draft(request: Request):
    s = _op(request, mutate=True)
    b = await _body(request)
    _cc(request).save_draft(s["actor"], str(b.get("target") or ""), str(b.get("text") or ""))
    return {"ok": True}


# --------------------------------------------------------------------------- helper policy

HELPER_POLICY_FIELDS = ("optional_enabled", "cutoff_pct", "hysteresis_pct", "cooldown_seconds",
                        "max_observation_age_seconds", "backend_preference",
                        "selective_backends", "required_helper_sessions_per_run",
                        "trust_fetch_time_for", "refresh_probe",
                        "refresh_probe_min_interval_seconds")


def _helper_status(svc) -> dict:
    gate = svc.quota_gate
    out = gate.status()
    caps = []
    streams = {b["backend"]: b for b in svc.ledger.backend_streams()}
    for name, s in (getattr(svc.pool, "backends", None) or {}).items():
        c = s.config
        st = streams.get(name) or {}
        caps.append({"backend": name, "type": c.type, "flavor": c.effective_flavor,
                     "enabled": bool(getattr(c, "enabled", True)),
                     "helpers_allowed": c.helpers_allowed,
                     "streams_allowed": None if c.helpers_allowed is None else c.helpers_allowed + 1,
                     "max_concurrent": c.max_concurrent,
                     "active_sessions": [{"session": a["session"][-12:], "persona": a["persona"],
                                          "idle_s": a["idle_s"]} for a in st.get("active") or []],
                     "in_flight": sum(1 for a in svc.ledger._active.values() if a.backend == name)})
    out["capacity"] = caps
    out["note"] = ("Capacity is a ceiling (helpers_allowed + 1 streams, max_concurrent calls), not "
                   "current activity. Optional work is admitted only while every applicable "
                   "window of the serving account is below the cutoff AND was observed "
                   "recently; UNKNOWN is never admitted.")
    return out


@router.get("/control/api/helpers")
@_guard
async def helpers_status(request: Request):
    _op(request)
    return _helper_status(request.app.state.services)


@router.post("/control/api/helpers/evaluate")
@_guard
async def helpers_evaluate(request: Request):
    """Re-read the quota sources for one backend + model (non-inference)."""
    _op(request, mutate=True)
    b = await _body(request)
    gate = request.app.state.services.quota_gate
    gate._cache.pop(f"{b.get('backend')}|{b.get('model')}", None)
    return await gate.evaluate(str(b.get("backend") or ""), str(b.get("model") or ""))


@router.put("/control/api/helpers/policy")
@_guard
async def helpers_policy(request: Request):
    """Edit helper_policy and per-backend helper ceilings (config.yaml; shows in
    config history). Required-work guardrails are not edited here."""
    s = _op(request, mutate=True)
    if s["projects"]:
        # the policy and backend ceilings are global: a project-restricted
        # operator may read them but not change them for every project
        raise ControlError("helper policy is global; it needs an operator session that is not "
                           "restricted to projects", "forbidden", 403)
    b = await _body(request)
    svc = request.app.state.services
    pol = {k: v for k, v in (b.get("policy") or {}).items() if k in HELPER_POLICY_FIELDS}
    caps = b.get("backends") or {}
    if "cutoff_pct" in pol and not 1 <= float(pol["cutoff_pct"]) <= 99:
        raise ControlError("cutoff_pct must be between 1 and 99")
    for name, v in caps.items():
        for k in ("helpers_allowed", "max_concurrent"):
            if k in v and v[k] is not None and (int(v[k]) < 0 or int(v[k]) > 32):
                raise ControlError(f"{name}.{k} must be 0..32 or empty")

    def mutate(raw):
        hp = raw.setdefault("helper_policy", {}) or {}
        hp.update(pol)
        raw["helper_policy"] = hp
        for be in ((raw.get("backend_pool") or {}).get("internal") or {}).get("backends") or []:
            v = caps.get(be.get("name"))
            if not v:
                continue
            for k in ("helpers_allowed", "max_concurrent"):
                if k in v:
                    be[k] = None if v[k] in (None, "") else int(v[k])
    try:
        svc.config_store.save(mutate)
    except Exception as e:                                       # noqa: BLE001
        raise ControlError(f"not saved: {e}") from None
    if caps:
        await svc.rebuild_pool()
    svc.quota_gate._cache.clear()
    svc.db.log_event("info", "helper_policy", f"helper policy changed by {s['actor']}: "
                     + json.dumps({"policy": pol, "backends": caps}, default=str)[:400])
    return _helper_status(svc)


# --------------------------------------------------------------------------- event stream

class _Bump:
    """Wakes every open stream when an observation lands (one ingest, many
    viewers: viewers never reach the workstation)."""

    def __init__(self):
        self.waiters: set[asyncio.Event] = set()
        self.loop = None

    def __call__(self, seq: int) -> None:
        for ev in list(self.waiters):
            try:
                if self.loop and self.loop.is_running():
                    self.loop.call_soon_threadsafe(ev.set)
                else:
                    ev.set()
            except RuntimeError:
                pass


def _bump(request: Request) -> _Bump:
    st = request.app.state
    b = getattr(st, "cc_bump", None)
    if b is None:
        b = st.cc_bump = _Bump()
        _cc(request).add_listener(b)
    if b.loop is None:
        b.loop = asyncio.get_running_loop()
    return b


@router.get("/control/api/stream")
async def stream(request: Request, after: int = 0, sessions: str = ""):
    try:
        s = _op(request)
    except ControlError as e:
        return _err(e)
    actor = s["actor"]
    if _streams.get(actor, 0) >= MAX_STREAMS_PER_ACTOR:
        return _err(ControlError("too many open streams for this operator", "rate_limited", 429))
    hdr_after = request.headers.get("last-event-id", "")
    cursor = int(hdr_after) if hdr_after.isdigit() else max(0, int(after or 0))
    subs = [x for x in sessions.split(",") if x][:12]
    cc = _cc(request)
    bump = _bump(request)
    ev = asyncio.Event()
    bump.waiters.add(ev)
    _streams[actor] = _streams.get(actor, 0) + 1

    async def gen():
        nonlocal cursor
        try:
            yield f"retry: 3000\nevent: hello\ndata: {json.dumps({'seq': cc.last_seq(), 'contract': CONTRACT})}\n\n"
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    return
                if cc.op_session(request.cookies.get(COOKIE, "")) is None:
                    yield "event: expired\ndata: {}\n\n"
                    return
                items, last = await asyncio.to_thread(cc.feed, cursor, s["projects"], subs)
                if items or last != cursor:
                    cursor = last
                    if items:
                        yield (f"id: {cursor}\nevent: batch\ndata: "
                               f"{json.dumps({'items': items, 'seq': cursor}, default=str)}\n\n")
                        idle = 0.0
                        continue
                ev.clear()
                try:
                    await asyncio.wait_for(ev.wait(), timeout=5)
                except asyncio.TimeoutError:
                    idle += 5
                if idle >= 15:
                    idle = 0.0
                    yield (f"event: heartbeat\ndata: "
                           f"{json.dumps({'server_ts': cc.clock(), 'seq': cursor, 'sources': cc.sources(s['projects'])}, default=str)}\n\n")
        finally:
            bump.waiters.discard(ev)
            _streams[actor] = max(0, _streams.get(actor, 1) - 1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- workstation side

def _client() -> dict:
    return client_auth.current()


@router.post("/v1/foundry/telemetry/batch")
@_guard
async def telemetry_batch(request: Request):
    b = await _body(request)
    return _cc(request).ingest(b, _client())


def _relay_only(c: dict) -> bool:
    """A key admitted on the client relay routes by its operator scope alone
    (no telemetry/orchestration): it may act as a Foundry Client, nothing else."""
    return not ({"telemetry", "orchestration", "admin"} & set(c.get("scopes") or []))


@router.post("/v1/foundry/telemetry/heartbeat")
@_guard
async def telemetry_heartbeat(request: Request):
    b = await _body(request)
    c = _client()
    if _relay_only(c) and ((b.get("source") or {}).get("kind") != "client"):
        raise ControlError("an operator key may heart-beat as a Foundry Client (source.kind client) "
                           "only", "forbidden", 403)
    return _cc(request).heartbeat(b, c)


@router.post("/v1/foundry/control/claim")
@_guard
async def control_claim(request: Request):
    b = await _body(request)
    c = _client()
    if str(b.get("source_kind") or "") == "client":
        # the Foundry Client: stack actions only (control_center.claim_client);
        # an operator, orchestration or admin key may claim them
        if not {"operator", "orchestration", "admin"} & set(c.get("scopes") or []):
            raise ControlError("claiming stack actions needs the operator or orchestration scope",
                               "forbidden", 403)
        return {"actions": _cc(request).claim_client(str(b.get("source_id") or ""),
                                                     int(b.get("limit") or 20))}
    if not {"orchestration", "admin"} & set(c.get("scopes") or []):
        raise ControlError("claiming operator actions needs the orchestration scope", "forbidden", 403)
    projects = [str(p) for p in (b.get("projects") or [])][:50]
    return {"actions": _cc(request).claim(str(b.get("source_id") or ""), projects,
                                          c.get("projects") or [], int(b.get("limit") or 20))}


@router.post("/v1/foundry/control/actions/{action_id}/status")
@_guard
async def control_status(request: Request, action_id: str):
    b = await _body(request)
    c = _client()
    cc = _cc(request)
    if _relay_only(c):
        from .control_center import CLIENT_ACTIONS
        a = cc.action_view(action_id, with_log=False)
        if not a or a["kind"] not in CLIENT_ACTIONS:
            raise ControlError("an operator key reports stack actions only", "forbidden", 403)
    elif not {"orchestration", "admin"} & set(c.get("scopes") or []):
        raise ControlError("reporting operator actions needs the orchestration scope", "forbidden",
                           403)
    return cc.report(action_id, str(b.get("source_id") or ""), str(b.get("status") or ""),
                     str(b.get("detail") or ""), b.get("result") if isinstance(
                         b.get("result"), dict) else None)

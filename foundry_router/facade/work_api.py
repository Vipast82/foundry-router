"""Work-binding API for a development coordinator (orchestration.py).

Needs a client key with the `orchestration` scope (or `admin`):
  POST /v1/foundry/work/sessions            bind a coding session to project/task/run/role
  POST /v1/foundry/work/sessions/{id}/end   the session is finished (outcome)
  GET  /v1/foundry/work/sessions/{id}       its binding (or 404)
  PUT  /v1/foundry/work/tasks/{project}/{task}   the coordinator's latest task state
  POST /v1/foundry/work/events              lifecycle events (event_id dedupes resends)
  GET  /v1/foundry/work/usage?project=&task=&run=&session=   measured usage
  GET  /v1/foundry/work/denials?session=&since=   Foundry refusals for a session
  GET  /v1/foundry/work/sessions/{id}/served   models/backends that served it (ledger)
  GET  /v1/foundry/personas/{name}/routes      a persona's route chain + client grant

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


def _project_allowed(project_id: str) -> bool:
    """A key restricted to some projects (ClientConfig.projects) sees only those."""
    allowed = client_auth.current().get("projects") or []
    return not allowed or project_id in allowed


@router.get("/v1/foundry/work/sessions/{session_id}/served")
async def session_served(session_id: str, request: Request):
    """Which models and backends served (or were reached for) a session's
    calls, from the budget ledger, oldest first, so a coordinator can verify a
    session ran only on the route it expected. Every row except a denial
    counts once it names a model: settled calls, errored calls (the backend
    was reached) and calls still in flight (`pending`, ledger status
    reserved; wait until it is 0 before trusting the answer). An unknown
    session is an empty result, not an error. A key restricted to projects
    sees only sessions bound to one of them."""
    db = request.app.state.services.db
    b = db.query_one("SELECT project_id FROM work_sessions WHERE session_id=?", (session_id,))
    project = (b or {}).get("project_id") or ""
    empty = {"session_id": session_id, "project_id": project, "calls": 0, "served_models": [],
             "backends": [], "routes": [], "errors": 0, "denied": 0, "pending": 0,
             "settled": 0, "last_ts": None}
    if client_auth.current().get("projects"):
        if not b:
            return empty                 # unbound: nothing a restricted key may see
        if not _project_allowed(project):
            return JSONResponse({"error": f"session {session_id} belongs to a project this "
                                          f"client key can't see", "code": "project_forbidden"},
                                status_code=403)
    rows = db.query("SELECT ts, backend, model, status FROM usage_events WHERE session_id=? "
                    "ORDER BY ts, id", (session_id,))
    out = dict(empty)
    models, backends, routes = [], [], []
    for r in rows:
        st = r["status"] or ""
        if st == "denied":
            out["denied"] += 1
            continue
        if st == "error":
            out["errors"] += 1
        elif st == "reserved":
            out["pending"] += 1
        elif st == "settled":
            out["settled"] += 1
        if not (r["model"] or r["backend"]):
            continue
        out["calls"] += 1
        out["last_ts"] = r["ts"]
        m, be = r["model"] or "", r["backend"] or ""
        if m and m not in models:
            models.append(m)
        if be and be not in backends:
            backends.append(be)
        if (be or m) and {"backend": be, "model": m} not in routes:
            routes.append({"backend": be, "model": m})
    out.update(served_models=models, backends=backends, routes=routes)
    return out


def _route_source(svc, backend: str) -> str:
    """What kind of backend a route is on, from config only (no quota read):
    subscription | metered | local | unknown."""
    s = (getattr(svc.pool, "backends", None) or {}).get(backend)
    if s is None:
        return "unknown"
    cfg = s.config
    from ..budget import is_paid
    if cfg.type == "anthropic-compatible" or cfg.effective_flavor == "cliproxyapi"             or cfg.is_subscription:
        return "subscription"
    return "metered" if is_paid(cfg) else "local"


def _may_inspect_client(svc, client: str) -> bool:
    """May the caller see another client's persona grant? An admin key or an
    unrestricted (no `projects`) key: yes. A project-restricted key: only for
    a configured client that is unrestricted or shares one of its projects
    (an unknown client is refused, so existence isn't revealed either)."""
    me = client_auth.current()
    if "admin" in (me.get("scopes") or []):
        return True
    mine = set(me.get("projects") or [])
    if not mine:
        return True
    c = next((c for c in svc.config_store.config.clients or [] if c.name == client), None)
    if c is None:
        return False
    theirs = set(getattr(c, "projects", None) or [])
    return not theirs or bool(mine & theirs)


@router.get("/v1/foundry/personas/{name}/routes")
async def persona_route_info(name: str, request: Request, client: str = ""):
    """A persona's reachable route chain and whether a client may use it, for
    a coordinator's readiness check. A missing, disabled or ungranted persona
    is reported (exists/enabled/client_allowed false + reason), never a 404.
    `client` names another configured client to check the grant for (default:
    the calling key): allowed for an admin-scoped key, an unrestricted
    caller, or a project-restricted caller asking about a client that serves
    one of its projects; anything else is 403 (no probing of other projects'
    clients). Config only: no quota read, no model call, no secrets."""
    svc = request.app.state.services
    if client and not _may_inspect_client(svc, client):
        return JSONResponse({"error": f"this client key may not inspect client {client!r}",
                             "code": "client_forbidden"}, status_code=403)
    p = svc.personas.get(name)
    res = {"name": name, "exists": p is not None, "enabled": bool(p and p.get("enabled")),
           "strict": False, "routes": [], "client_allowed": False, "client": "", "reason": ""}
    if client:
        c = next((c for c in svc.config_store.config.clients or [] if c.name == client), None)
        res["client"] = client
        if c is None or not c.enabled:
            allowed, why = False, f"client {client!r} is not configured or disabled"
        else:
            grant = list(c.personas or [])
            allowed = not grant or name in grant or name.split(":")[0] in grant
            why = "" if allowed else f"persona not granted to client {client!r}"
    else:
        res["client"] = client_auth.current().get("client_id") or ""
        allowed = client_auth.persona_allowed(name)
        why = "" if allowed else "persona not granted to the calling client key"
    res["client_allowed"] = bool(allowed)
    if p is None:
        res["reason"] = f"persona {name!r} does not exist"
        return res
    res["name"] = p.get("virtual_name") or name
    from .ollama_api import is_strict
    from ..quota_gate import persona_routes
    from ..brain.agent import _model_has_vision
    res["strict"] = is_strict(p)
    for be, m in persona_routes(svc, p):
        try:
            vision = bool(_model_has_vision(svc.registry, m))
        except Exception:                                        # noqa: BLE001
            vision = None
        res["routes"].append({"backend": be, "model": m, "source": _route_source(svc, be),
                              "vision": vision})
    reasons = []
    if not res["enabled"]:
        reasons.append("persona is disabled")
    if why:
        reasons.append(why)
    if not res["routes"]:
        reasons.append("no reachable route" + (" (strict routing refuses)" if res["strict"]
                                               else ""))
    if not res["strict"]:
        reasons.append("flexible routing: routes are candidates, not a fixed chain")
    res["reason"] = "; ".join(reasons) or "ready"
    return res


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


@router.get("/v1/foundry/helpers/admission")
async def helper_admission(request: Request, persona: str = "", backend: str = "",
                           model: str = "", vision: bool = False, purpose: str = "",
                           models: str = ""):
    """Would optional helper work be admitted right now? For a coordinator
    deciding whether to dispatch a helper unit: quota sources only, except one
    tiny refresh probe started IN THE BACKGROUND when a reading merely went
    stale while idle and the backend is not verified serving (bounded per
    backend by refresh_probe_min_interval_seconds); this call never waits on it. A persona is checked on each reachable route of its chain (the call
    gate re-checks every fallback when it actually runs).

    The first route decides. Only when it is not admitted (UNKNOWN / DEFER)
    may a LATER route of a strict persona's reachable chain be admitted
    instead, and only one that is ADMIT itself, not local, has a free helper
    stream, can see images when the helper needs them (vision=1 or a
    visual purpose) and is in `models` (the coordinator's approved models,
    comma-separated) when given. That answer is PINNED: {route, pinned: true};
    the coordinator binds the session with meta.pinned_route, Foundry then
    serves that session only on that route and refuses (optional_deferred),
    never re-routes, once it is no longer admitted. When the first route is
    admitted nothing is pinned (route = the first route, pinned: false): the
    chain and its call-time gating per route stay exactly as before."""
    svc = request.app.state.services
    gate = svc.quota_gate
    routes = []
    p = None
    if persona:
        p = svc.personas.get(persona)
        if p is None or not p.get("enabled"):
            return JSONResponse({"admit": False, "state": "blocked",
                                 "reason": f"persona {persona!r} missing or disabled"}, status_code=200)
        from ..quota_gate import persona_routes
        routes = persona_routes(svc, p)
        if not routes:
            return {"admit": False, "state": "blocked", "persona": persona, "routes": [],
                    "reason": "no reachable route for this persona (strict routing refuses)"}
    elif backend and model:
        routes = [(backend, model)]
    else:
        return _bad(ValueError("give persona, or backend and model"))
    out = []
    from ..brain.agent import _model_has_vision
    from ..quota_gate import ADMIT, DEFER, UNKNOWN
    for i, (be, m) in enumerate(routes):
        # the first route decides admission: refresh a reading that only went
        # stale while idle (one tiny probe) instead of answering UNKNOWN
        d = dict(await (gate.evaluate_fresh(be, m, wait=False) if i == 0 else gate.evaluate(be, m)))
        # image modality per route, from the model registry (capabilities/tags):
        # a coordinator sending images refuses any route without it
        try:
            d["vision"] = bool(_model_has_vision(svc.registry, m))
        except Exception:                                        # noqa: BLE001
            d["vision"] = None
        out.append(d)
    streams = {b["backend"]: b for b in svc.ledger.backend_streams()}

    def _free(be: str):
        st = streams.get(be) or {}
        allowed = st.get("streams_allowed")
        return None if allowed is None else max(0, allowed - len(st.get("active") or []))
    first = out[0]
    free = _free(first["backend"])
    pol = svc.config_store.config.helper_policy
    admit = pol.optional_enabled and first["state"] == "admit" and (free is None or free > 0)
    reason = ("optional coding helpers are turned off" if not pol.optional_enabled else
              first["reason"] if first["state"] != "admit" else
              "no free helper stream on " + first["backend"] if free == 0 else first["reason"])
    res = {"admit": admit, "state": first["state"] if pol.optional_enabled else "blocked",
           "reason": reason, "persona": persona, "backend": first["backend"],
           "model": first["model"], "next_eligible": first.get("next_eligible"),
           "streams_free": free, "routes": out, "pinned": False,
           "route": {"backend": first["backend"], "model": first["model"]}}
    if admit or not pol.optional_enabled or first["state"] not in (UNKNOWN, DEFER):
        return res
    from .ollama_api import is_strict
    if p is None or not is_strict(p):
        return res                       # the pin is honoured by strict routing only
    needs_vision = bool(vision) or purpose == "visual_observation"
    approved = {m.strip() for m in (models or "").split(",") if m.strip()}
    skipped = []
    for d in out[1:]:
        name = f"{d['backend']}/{d['model']}"
        why = ("not admitted (" + str(d["state"]).upper() + ")" if d["state"] != ADMIT else
               "local backend (held by the primary)" if d.get("source") == "local" else
               "no image input" if needs_vision and d.get("vision") is not True else
               "not in the approved models" if approved and d["model"] not in approved else
               "no free helper stream" if _free(d["backend"]) == 0 else "")
        if why:
            skipped.append(f"{name}: {why}")
            continue
        res.update(admit=True, state=ADMIT, backend=d["backend"], model=d["model"],
                   next_eligible=None, streams_free=_free(d["backend"]), pinned=True,
                   route={"backend": d["backend"], "model": d["model"]},
                   first_route={"backend": first["backend"], "model": first["model"],
                                "state": first["state"], "reason": first["reason"]},
                   reason=f"first route {first['backend']} {str(first['state']).upper()} "
                          f"({first['reason']}); fallback {d['backend']} admitted: "
                          f"{d['reason']}")
        return res
    if skipped:
        res["fallbacks_ineligible"] = skipped
    return res


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


# -- training-data capture (capture.py) and pipeline status (foundry_training) ---------

@router.get("/v1/foundry/capture/records")
async def capture_records(request: Request, after_id: int = 0, limit: int = 200,
                          project: str = ""):
    """Captured inference records (already redacted), oldest first, after
    `after_id`: the training pipeline's incremental, idempotent source."""
    cap = request.app.state.services.capture
    if not cap.cfg.enabled and cap._store is None:
        return {"records": [], "enabled": False, "next_after_id": after_id}
    rows = cap.records(after_id, limit, project)
    return {"records": rows, "enabled": bool(cap.cfg.enabled),
            "next_after_id": rows[-1]["id"] if rows else after_id, "boundary": rows[0]["boundary"]
            if rows else None}


_DATASET_ID = __import__("re").compile(r"^[A-Za-z0-9._-]{8,120}$")
MAX_DATASET_BYTES = 200 * 1024 * 1024


@router.post("/v1/foundry/training/datasets/{dataset_id}")
async def put_training_dataset(dataset_id: str, request: Request):
    """A validated fine-tuning dataset from `foundry_training auto`. Stored as
    files under <data>/training/datasets/; every line is re-scanned for
    secrets and refused if anything still looks like one. Train sets only.
    Re-sending the same bytes is a no-op; different bytes under the same id
    are refused."""
    import hashlib
    import json as _json
    import os
    from foundry_training.redact import scan
    if not _DATASET_ID.match(dataset_id):
        return _bad(ValueError("bad dataset id"))
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_DATASET_BYTES + 1_000_000:          # refuse before reading the body
        return JSONResponse({"error": f"dataset over {MAX_DATASET_BYTES // 2**20} MB",
                             "code": "too_large"}, status_code=413)
    raw = await request.body()
    if len(raw) > MAX_DATASET_BYTES + 1_000_000:
        return JSONResponse({"error": f"dataset over {MAX_DATASET_BYTES // 2**20} MB",
                             "code": "too_large"}, status_code=413)
    try:
        body = _json.loads(raw)
    except ValueError as e:
        return _bad(e)
    m, data = body.get("manifest") or {}, body.get("data")
    if not isinstance(m, dict) or not isinstance(data, str) or m.get("dataset_id") != dataset_id:
        return _bad(ValueError("body needs manifest (with this dataset_id) and data"))
    if m.get("purpose") != "train":
        return _bad(ValueError("only train datasets are accepted (evaluation sets stay local)"))
    sha = hashlib.sha256(data.encode()).hexdigest()
    if body.get("sha256") and body["sha256"] != sha:
        return _bad(ValueError("sha256 does not match the data (truncated upload?)"))
    lines = [l for l in data.splitlines() if l.strip()]
    for i, line in enumerate(lines, 1):
        try:
            rec = _json.loads(line)
        except ValueError:
            return _bad(ValueError(f"line {i} is not JSON"))
        if scan(rec).count:
            return JSONResponse({"error": f"line {i} still looks like it contains a secret; "
                                          f"refused (nothing stored)", "code": "secret_detected"},
                                status_code=422)
    svc = request.app.state.services
    db = svc.db
    prior = db.query_one("SELECT sha256, path FROM training_datasets WHERE dataset_id=?",
                         (dataset_id,))
    if prior:
        if prior["sha256"] == sha:
            return {"ok": True, "duplicate": True, "path": prior["path"], "examples": len(lines)}
        return JSONResponse({"error": "a different dataset with this id already exists",
                             "code": "conflict"}, status_code=409)
    pid = str((m.get("projects") or ["unknown"])[0] if len(m.get("projects") or []) == 1
              else "multi")
    safe_pid = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in pid)[:80].strip(".")
    safe_pid = safe_pid or "unknown"                      # never "." / ".." as a folder
    base = svc.capture.data_dir / "training" / "datasets" / safe_pid / dataset_id
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / "data.jsonl.tmp"
    tmp.write_text(data if data.endswith("\n") else data + "\n", encoding="utf-8")
    os.replace(tmp, base / "data.jsonl")
    (base / "manifest.json").write_text(_json.dumps(m, indent=2), encoding="utf-8")
    db.execute("INSERT INTO training_datasets (dataset_id, project_id, kind, target, examples, "
               "sha256, bytes, path, manifest, client_id, uploaded_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (dataset_id, pid, str(m.get("kind") or ""), str(m.get("target") or ""), len(lines),
                sha, len(data.encode()), str(base), _json.dumps(m)[:200000],
                client_auth.current().get("client_id") or "", time.time()))
    return {"ok": True, "path": str(base), "examples": len(lines)}


@router.put("/v1/foundry/training/status/{pipeline_id}")
async def put_training_status(pipeline_id: str, request: Request):
    import json as _json
    data = await request.json()
    request.app.state.services.db.execute(
        "INSERT INTO training_status (pipeline_id, data, updated_ts) VALUES (?,?,?) "
        "ON CONFLICT(pipeline_id) DO UPDATE SET data=excluded.data, updated_ts=excluded.updated_ts",
        (pipeline_id[:80], _json.dumps(data, default=str)[:200000], time.time()))
    return {"ok": True}


@router.post("/v1/foundry/training/episodes/{pipeline_id}")
async def put_training_episodes(pipeline_id: str, request: Request):
    """Episode summaries (redacted previews only). Re-sending replaces."""
    import json as _json
    from foundry_training.redact import scan
    body = await request.json()
    n = 0
    db = request.app.state.services.db
    for e in (body.get("episodes") or [])[:500]:
        if not isinstance(e, dict) or not e.get("episode_id"):
            continue
        if scan(e).count:              # never store anything that still looks secret
            continue
        db.execute("INSERT INTO training_episodes (episode_id, pipeline_id, project_id, task_id, "
                   "kind, status, data, updated_ts) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT"
                   "(episode_id) DO UPDATE SET status=excluded.status, data=excluded.data, "
                   "kind=excluded.kind, updated_ts=excluded.updated_ts",
                   (str(e["episode_id"])[:120], pipeline_id[:80], str(e.get("project_id") or ""),
                    str(e.get("task_id") or ""), str(e.get("kind") or ""),
                    str(e.get("status") or ""), _json.dumps(e, default=str)[:60000], time.time()))
        n += 1
    return {"ok": True, "recorded": n}

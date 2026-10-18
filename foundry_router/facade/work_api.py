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


@router.get("/v1/foundry/helpers/admission")
async def helper_admission(request: Request, persona: str = "", backend: str = "",
                           model: str = ""):
    """Would optional helper work be admitted right now? For a coordinator
    deciding whether to dispatch a helper unit: no inference, quota sources
    only. A persona is checked on each reachable route of its chain (the call
    gate re-checks every fallback when it actually runs)."""
    svc = request.app.state.services
    gate = svc.quota_gate
    routes = []
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
    for i, (be, m) in enumerate(routes):
        # the first route decides admission: refresh a reading that only went
        # stale while idle (one tiny probe) instead of answering UNKNOWN
        d = dict(await (gate.evaluate_fresh(be, m) if i == 0 else gate.evaluate(be, m)))
        # image modality per route, from the model registry (capabilities/tags):
        # a coordinator sending images refuses any route without it
        try:
            d["vision"] = bool(_model_has_vision(svc.registry, m))
        except Exception:                                        # noqa: BLE001
            d["vision"] = None
        out.append(d)
    streams = {b["backend"]: b for b in svc.ledger.backend_streams()}
    first = out[0]
    st = streams.get(first["backend"]) or {}
    allowed = st.get("streams_allowed")
    free = None if allowed is None else max(0, allowed - len(st.get("active") or []))
    pol = svc.config_store.config.helper_policy
    admit = pol.optional_enabled and first["state"] == "admit" and (free is None or free > 0)
    reason = ("optional coding helpers are turned off" if not pol.optional_enabled else
              first["reason"] if first["state"] != "admit" else
              "no free helper stream on " + first["backend"] if free == 0 else first["reason"])
    return {"admit": admit, "state": first["state"] if pol.optional_enabled else "blocked",
            "reason": reason, "persona": persona, "backend": first["backend"],
            "model": first["model"], "next_eligible": first.get("next_eligible"),
            "streams_free": free, "routes": out}


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
    safe_pid = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in pid)[:80]
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

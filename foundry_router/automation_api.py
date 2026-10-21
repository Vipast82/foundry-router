"""HTTP surface of the automation config store (automation_store.py).

  GUI / admin   /admin/api/automation/*  (the admin key when protect_admin is on).
                Every PUT/POST needs `X-Foundry-Automation: 1`: a custom header
                forces a CORS preflight, so another site can't post here.
  coordinator   GET  /v1/foundry/control/config?project=   the deployed revision
                POST /v1/foundry/control/config/applied     what it applied
                (orchestration scope, checked by client_auth for /v1/foundry/*)

Contract: side-session notes revamp/config_api.md.
"""

from __future__ import annotations

import functools
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from foundry_automation import ROUTER_DOC
from foundry_automation import role_rules as RR

from . import client_auth
from .automation_store import StoreError, check_project

log = logging.getLogger(__name__)
router = APIRouter()
P = "/admin/api/automation"


def _st(request: Request):
    return request.app.state.services.automation


def _err(e: StoreError) -> JSONResponse:
    return JSONResponse({"error": str(e), "code": e.code, **e.extra}, status_code=e.status)


def _guard(fn):
    @functools.wraps(fn)
    async def wrap(*a, **k):
        try:
            return await fn(*a, **k)
        except StoreError as e:
            return _err(e)
    return wrap


def _actor(request: Request) -> str:
    try:
        cc = request.app.state.services.control
        s = cc.op_session(request.cookies.get("fr_cc", ""))
        if s and s.get("actor"):
            return str(s["actor"])[:80]
    except Exception:                                            # noqa: BLE001
        pass
    c = client_auth.current()
    cid = c.get("client_id") or ""
    if cid and cid != "anonymous":
        return cid[:80]
    hdr = (request.headers.get("x-foundry-actor") or "").strip()
    return hdr[:80] or "admin-ui"


def _mutating(request: Request) -> None:
    if (request.headers.get("x-foundry-automation") or "").strip() != "1":
        raise StoreError("missing X-Foundry-Automation: 1 header", "csrf", 403)


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except ValueError:
        raise StoreError("body must be JSON") from None
    if not isinstance(b, dict):
        raise StoreError("body must be a JSON object")
    return b


async def _apply_router_doc(request: Request, doc: dict) -> list[str]:
    """The _router document's per-backend cutoffs -> each backend's
    usage_limit_pct in config.yaml, then rebuild the pool."""
    svc = request.app.state.services
    want = ((doc or {}).get("settings") or {}).get("backends") or {}
    notes: list[str] = []

    def mutate(raw: dict) -> None:
        backends = (((raw.get("backend_pool") or {}).get("internal") or {}).get("backends")
                    or [])
        names = {b.get("name") for b in backends if isinstance(b, dict)}
        for name in want:
            if name not in names:
                notes.append(f"backend {name!r} is not configured; its cutoff waits")
        for b in backends:
            if not isinstance(b, dict) or b.get("name") not in want:
                continue
            pct = (want[b["name"]] or {}).get("cutoff_pct")
            if pct is None:
                b.pop("usage_limit_pct", None)
            else:
                b["usage_limit_pct"] = float(pct)

    svc.config_store.save(mutate)
    try:
        await svc.rebuild_pool()
    except Exception as e:                                       # noqa: BLE001
        notes.append(f"pool rebuild failed: {type(e).__name__}: {e}")
    return notes


# --------------------------------------------------------------------------- admin (GUI)

@router.get(P + "/schema")
async def get_schema(request: Request):
    return _st(request).schema()


@router.get(P + "/projects")
async def list_projects(request: Request):
    return {"projects": _st(request).projects()}


@router.get(P + "/projects/{project}/draft")
@_guard
async def get_draft(request: Request, project: str):
    return _st(request).draft(project, _actor(request))


@router.put(P + "/projects/{project}/draft")
@_guard
async def put_draft(request: Request, project: str):
    _mutating(request)
    b = await _body(request)
    return _st(request).save_draft(project, b.get("doc"), str(b.get("etag") or ""),
                                   _actor(request))


@router.post(P + "/projects/{project}/draft/reset")
@_guard
async def reset_draft(request: Request, project: str):
    _mutating(request)
    b = await _body(request)
    rev = b.get("rev")
    return _st(request).reset_draft(project, int(rev) if rev is not None else None,
                                    _actor(request))


@router.post(P + "/projects/{project}/validate")
@_guard
async def validate(request: Request, project: str):
    _mutating(request)
    b = await _body(request)
    return _st(request).validate(project, b.get("doc"))


@router.post(P + "/projects/{project}/import")
@_guard
async def import_profile(request: Request, project: str):
    _mutating(request)
    b = await _body(request)
    text = b.get("toml")
    if not isinstance(text, str) or not text.strip():
        raise StoreError("send the profile text as {\"toml\": \"...\"}", "bad_toml")
    return _st(request).import_profile(project, text, _actor(request),
                                       dry_run=bool(b.get("dry_run")))


@router.post(P + "/projects/{project}/deploy")
@_guard
async def deploy(request: Request, project: str):
    _mutating(request)
    b = await _body(request)
    st = _st(request)
    out = st.deploy(project, str(b.get("etag") or ""), str(b.get("note") or ""),
                    _actor(request))
    if check_project(project) == ROUTER_DOC and not out.get("unchanged"):
        out["router_notes"] = await _apply_router_doc(request, st.deployed(project)["doc"])
    return out


@router.get(P + "/projects/{project}/revisions")
@_guard
async def revisions(request: Request, project: str, limit: int = 50):
    return {"revisions": _st(request).revisions(project, limit)}


@router.get(P + "/projects/{project}/revisions/{rev}")
@_guard
async def revision(request: Request, project: str, rev: int):
    return _st(request).revision(project, rev)


@router.post(P + "/projects/{project}/rollback")
@_guard
async def rollback(request: Request, project: str):
    _mutating(request)
    b = await _body(request)
    try:
        rev = int(b.get("rev"))
    except (TypeError, ValueError):
        raise StoreError("rollback needs {\"rev\": <number>}") from None
    st = _st(request)
    out = st.rollback(project, rev, str(b.get("note") or ""), _actor(request),
                      reset_draft=bool(b.get("reset_draft", True)))
    if check_project(project) == ROUTER_DOC:
        out["router_notes"] = await _apply_router_doc(request, st.deployed(project)["doc"])
    return out


@router.get(P + "/projects/{project}/diff")
@_guard
async def diff(request: Request, project: str, to: str = "draft"):
    frm = request.query_params.get("from", "deployed")
    return _st(request).diff(project, frm, to)


@router.get(P + "/projects/{project}/audit")
@_guard
async def audit(request: Request, project: str, limit: int = 100):
    return {"audit": _st(request).audit(project, limit)}


@router.get(P + "/projects/{project}/applied")
@_guard
async def applied(request: Request, project: str, limit: int = 50):
    return {"applied": _st(request).applied(project, limit)}


def _rules_for(request: Request, b: dict) -> tuple[str, dict]:
    role = str(b.get("role") or "")
    if role not in RR.RULE_ROLES:
        raise StoreError(f"role must be one of {', '.join(RR.RULE_ROLES)}", "unknown_role")
    rules = b.get("rules")
    if rules is None and b.get("project"):
        doc = _st(request).draft(str(b["project"]))["doc"]
        rules = (doc.get("role_rules") or {}).get(role)
    errs, _ = RR.validate({role: rules or {}})
    if errs:
        raise StoreError("the role rules do not validate", "invalid", 422,
                         validation={"ok": False, "errors": errs, "warnings": []})
    return role, RR.normalize(role, rules)


@router.post(P + "/role-rules/preview")
@_guard
async def role_rules_preview(request: Request):
    _mutating(request)
    b = await _body(request)
    role, rules = _rules_for(request, b)
    ws = str(b.get("workspace") or "")
    return {"role": role, "rules": rules,
            "session_permission": RR.session_rules(role, rules, ws),
            "agent": rules.get("agent") or "",
            "agent_permission": RR.agent_block(role, rules, ws),
            "hard_denies_applied": [dict(h) for h in RR.HARD_DENIES]}


@router.post(P + "/role-rules/test-path")
@_guard
async def role_rules_test(request: Request):
    _mutating(request)
    b = await _body(request)
    role, rules = _rules_for(request, b)
    op = str(b.get("op") or "edit")
    value = b.get("command") if op == "bash" else b.get("path")
    if not isinstance(value, str) or not value.strip():
        raise StoreError("give path (read/edit/write) or command (bash)")
    return RR.test(role, rules, op, value, str(b.get("workspace") or ""))


# --------------------------------------------------------------------------- coordinator

def _coord_project(project: str) -> str:
    p = check_project(project)
    c = client_auth.current()
    if not {"orchestration", "admin"} & set(c.get("scopes") or []):
        raise StoreError("needs the orchestration scope", "forbidden", 403)
    allowed = c.get("projects") or []
    if allowed and p not in allowed:
        raise StoreError(f"this key may not read project {p!r}", "forbidden", 403)
    return p


@router.get("/v1/foundry/control/config")
@_guard
async def coordinator_config(request: Request, project: str = ""):
    p = _coord_project(project)
    dep = _st(request).deployed(p)
    if not dep:
        raise StoreError(f"nothing deployed for {p!r}", "no_revision", 404)
    etag = f'"{dep["hash"]}"'
    if (request.headers.get("if-none-match") or "").strip() == etag:
        return Response(status_code=304, headers={"ETag": etag})
    body = {"project": p, "rev": dep["rev"], "hash": dep["hash"], "ts": dep["ts"],
            "actor": dep["actor"], "format": dep["doc"].get("format"), "doc": dep["doc"]}
    return JSONResponse(body, headers={"ETag": etag})


@router.post("/v1/foundry/control/config/applied")
@_guard
async def coordinator_applied(request: Request):
    b = await _body(request)
    p = _coord_project(str(b.get("project") or ""))
    return _st(request).record_applied(
        p, str(b.get("source_id") or client_auth.current().get("client_id") or ""),
        b.get("rev"), str(b.get("hash") or ""), str(b.get("status") or ""),
        str(b.get("detail") or ""), b.get("restart_needed") if isinstance(
            b.get("restart_needed"), list) else None)

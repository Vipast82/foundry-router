"""Automation area (/ui/automation): the page and its read-side glue.

The page edits one automation document through the C1 config store
(/control/api/config*, its own module). What lives here only reads:

  GET  /ui/automation                    the page
  GET  /control/api/automation/usage     live 5-hour / weekly usage per backend
  GET  /control/api/automation/seed      STUB until C1 is merged: today's
                                         personas, backends and role bindings
  POST /control/api/automation/dry-run   which route each role would use now
  POST /control/api/automation/path-test may role X read/edit/write P or run command C?
  POST /control/api/automation/check     rule warnings for a document
  POST /control/api/automation/diff      changed paths between two documents

Same operator session as the control center (cookie + CSRF + same origin on
every POST, even these read-only ones). Nothing here changes config, calls a
model or probes a backend beyond the cached usage readings.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from .. import automation_view as av
from ..control_api import _body, _guard, _op
from ..control_center import ControlError

router = APIRouter()
PAGE = Path(__file__).resolve().parent / "static" / "automation.html"
# SortableJS is the one external script (drag handles); the page works without it
# (keyboard and arrow buttons), so a LAN without internet loses only dragging.
CDN = "https://cdn.jsdelivr.net"


@router.get("/ui/automation")
async def automation_page():
    return FileResponse(PAGE, media_type="text/html",
                        headers={"Cache-Control": "no-store",
                                 "Content-Security-Policy":
                                     f"default-src 'self'; script-src 'self' 'unsafe-inline' {CDN}; "
                                     "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                     "connect-src 'self'; frame-ancestors 'none'; "
                                     "base-uri 'none'; form-action 'none'",
                                 "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "same-origin"})


def _cutoffs(svc) -> dict:
    """Per-backend cutoff % in force today. A1-A4 (router-budgets) replace the
    scattered settings with one per-backend value; until then: the backend's
    usage_limit_pct, else subscription_usage.limit_pct (ChatGPT) or Meridian's
    conserve_premium_at (Claude)."""
    cfg = svc.config_store.config
    out = {}
    explicit = getattr(cfg, "cutoffs", None)                # A4, when merged
    for name, s in (getattr(svc.pool, "backends", None) or {}).items():
        c = s.config
        v = None
        if isinstance(explicit, dict):
            v = explicit.get(name)
        if v is None and c.usage_limit_pct:
            v = c.usage_limit_pct
        if v is None:
            fam = av.backend_family(name, c.effective_flavor or "", c.type or "")
            if fam == "chatgpt":
                v = cfg.subscription_usage.limit_pct
            elif fam == "claude":
                v = round(float(svc.meridian_usage.cfg.conserve_premium_at) * 100, 1)
        if v is not None:
            out[name] = float(v)
    return out


async def live_usage(svc) -> dict:
    from .routes import _cliproxy_infos
    status = svc.pool.backend_status()
    mer = []
    for s in getattr(svc.pool, "backends_of_type", lambda t: [])("anthropic-compatible"):
        try:
            snap = await svc.meridian_usage.snapshot(s.config.url, s.config.api_key)
        except Exception as e:                                   # noqa: BLE001
            snap = {"buckets": [], "note": f"usage unreadable: {str(e)[:120]}"}
        mer.append({"backend": s.config.name, **snap})
    cli = []
    cu = getattr(svc, "cliproxy_usage", None)
    for info in _cliproxy_infos(svc):
        if cu is None or not cu.configured(info):
            cli.append({"backend": info["name"], "ok": False, "configured": False,
                        "error": "no management_key on this backend", "accounts": []})
            continue
        try:
            cli.append({**await cu.snapshot(info), "configured": True})
        except Exception as e:                                   # noqa: BLE001
            cli.append({"backend": info["name"], "ok": False, "error": str(e)[:200], "accounts": []})
    return av.normalize_usage(status, mer, cli, _cutoffs(svc))


@router.get("/control/api/automation/usage")
@_guard
async def automation_usage(request: Request):
    _op(request)
    return {"usage": await live_usage(request.app.state.services), "ts": time.time(),
            "stale_after_s": av.STALE_AFTER_S}


def _role_personas(svc) -> dict:
    """STUB helper: the persona each role last ran with (work bindings), else
    the canonical Kilo ids."""
    from ..kilo_roles import CANONICAL
    out = dict(CANONICAL)
    out.setdefault("lead", "Foundry-Kilo-Lead-Manager")
    seen = set()
    try:
        rows = svc.db.query("SELECT role, persona FROM work_sessions ORDER BY created_ts DESC LIMIT 500")
    except Exception:                                            # noqa: BLE001
        rows = []
    for r in rows:
        role, persona = r.get("role") or "", r.get("persona") or ""
        if role == "coordinator" and "lead" in persona.lower():
            role = "lead"                  # the coordinator binds its Lead sessions as "coordinator"
        if role in seen or role == "helper" or not persona:
            continue
        seen.add(role)
        out[role] = persona
    return out


@router.get("/control/api/automation/seed")
@_guard
async def automation_seed(request: Request):
    _op(request)
    svc = request.app.state.services
    personas = [dict(p) for p in svc.personas.list()]
    doc = av.seed_document(personas, svc.pool.backend_status(), _role_personas(svc), _cutoffs(svc))
    return {"doc": doc, "stub": True,
            "note": "Read from today's personas and backends. The C1 config store is not merged, "
                    "so drafts stay in this browser and Deploy is off."}


def _doc(b: dict) -> dict:
    d = b.get("doc")
    if not isinstance(d, dict):
        raise ControlError("doc must be an automation document (JSON object)")
    return d


@router.post("/control/api/automation/dry-run")
@_guard
async def automation_dry_run(request: Request):
    _op(request, mutate=True)
    b = await _body(request)
    doc = _doc(b)
    usage = b.get("usage") if isinstance(b.get("usage"), dict) else \
        await live_usage(request.app.state.services)
    if b.get("role"):
        roles = [str(b["role"])]
    elif isinstance(b.get("roles"), list):
        roles = [str(x) for x in b["roles"]][:20]
    else:
        roles = [r.get("id") for r in doc.get("roles") or []]
    return {"results": [av.dry_run(doc, r, usage) for r in roles if r], "ts": time.time()}


@router.post("/control/api/automation/path-test")
@_guard
async def automation_path_test(request: Request):
    _op(request, mutate=True)
    b = await _body(request)
    path = str(b.get("path") or "").strip()
    if not path or len(path) > 1000:
        raise ControlError("path must be 1-1000 characters")
    op = str(b.get("op") or b.get("tool") or "edit")
    return av.path_test(_doc(b), str(b.get("role") or ""), op, path,
                        str(b.get("workspace") or "")[:500])


@router.post("/control/api/automation/check")
@_guard
async def automation_check(request: Request):
    _op(request, mutate=True)
    return {"warnings": av.rule_warnings(_doc(await _body(request)))}


@router.post("/control/api/automation/diff")
@_guard
async def automation_diff(request: Request):
    _op(request, mutate=True)
    b = await _body(request)
    return {"changes": av.diff_docs(b.get("before") or {}, b.get("after") or {})}

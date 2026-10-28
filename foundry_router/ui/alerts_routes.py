"""Admin API of Alerts & digest (alerts.py): settings in config.yaml (shown in
config history like every other edit), a test alert, the digest list and an
on-demand digest. The webhook token is write-only: reads say only whether one
is set."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

FIELDS = ("enabled", "coordinator_down_minutes", "webhook_url", "webhook_format", "classes",
          "digest_enabled", "digest_hour", "digest_to_webhook")


def _svc(request: Request):
    return request.app.state.services


@router.get("/admin/api/alerts/settings")
async def alerts_settings(request: Request):
    from ..alerts import CLASS_LABELS
    from ..config import ALERT_CLASSES
    svc = _svc(request)
    return {"settings": await asyncio.to_thread(svc.alerts.settings_view),
            "classes": list(ALERT_CLASSES), "labels": CLASS_LABELS}


@router.put("/admin/api/alerts/settings")
async def put_alerts_settings(request: Request):
    """{enabled?, coordinator_down_minutes?, webhook_url?, webhook_format?,
    webhook_token? ('' keeps the stored one), clear_token?, classes?: {cls: bool},
    digest_enabled?, digest_hour?, digest_to_webhook?}"""
    from ..config import ALERT_CLASSES, AlertsConfig
    svc = _svc(request)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    raw_now = dict((svc.config_store.raw or {}).get("alerts") or {})
    cur = svc.config_store.config.alerts.model_dump()
    for k in FIELDS:
        if k in body:
            cur[k] = body[k]
    if isinstance(body.get("classes"), dict):
        unknown = [c for c in body["classes"] if c not in ALERT_CLASSES]
        if unknown:
            return JSONResponse({"error": f"unknown alert class(es): {', '.join(unknown)}"},
                                status_code=400)
        cur["classes"] = {c: bool(body["classes"].get(c, svc.config_store.config.alerts
                                                       .class_on(c))) for c in ALERT_CLASSES}
    url = str(cur.get("webhook_url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        return JSONResponse({"error": "the webhook URL must start with http:// or https://"},
                            status_code=400)
    cur["webhook_url"] = url
    try:
        validated = AlertsConfig.model_validate(cur)
    except Exception as e:                                        # noqa: BLE001
        return JSONResponse({"error": f"invalid alert settings: {e}"}, status_code=400)
    dumped = validated.model_dump(exclude_defaults=True)
    dumped.pop("webhook_token", None)
    token = body.get("webhook_token")
    if body.get("clear_token"):
        pass                                         # dropped: no token
    elif isinstance(token, str) and token.strip():
        dumped["webhook_token"] = token.strip()
    elif raw_now.get("webhook_token"):
        dumped["webhook_token"] = raw_now["webhook_token"]   # keeps a ${VAR} reference

    def mutate(raw):
        if dumped:
            raw["alerts"] = dumped
        else:
            raw.pop("alerts", None)
    svc.config_store.save(mutate)
    return {"ok": True, "settings": await asyncio.to_thread(svc.alerts.settings_view)}


@router.post("/admin/api/alerts/test")
async def alerts_test(request: Request):
    svc = _svc(request)
    return await asyncio.to_thread(svc.alerts.test_alert, "admin")


@router.get("/admin/api/alerts/digests")
async def alerts_digests(request: Request, limit: int = 7):
    svc = _svc(request)
    return {"digests": await asyncio.to_thread(svc.alerts.digests, limit)}


@router.post("/admin/api/alerts/digest")
async def alerts_digest_now(request: Request):
    svc = _svc(request)
    return {"digest": await asyncio.to_thread(svc.alerts.build_digest, "manual", "admin")}

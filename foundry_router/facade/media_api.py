"""Image endpoints.

OpenAI-compatible (OpenWebUI, AnythingLLM, scripts) — synchronous, like OpenAI:
  POST /v1/images/generations        JSON
  POST /v1/images/edits              JSON (images[].image_url) or multipart (image / image[] / mask)

Foundry job API (the local media adapter, Kilo/Cline tools) — asynchronous:
  POST /media/jobs                   submit {op, prompt, model, ..., images, mask, idempotency_key}
  GET  /media/jobs/{id}?wait=N       status; long-poll up to 60 s (never resubmits)
  POST /media/jobs/{id}/cancel
  GET  /media/jobs                   this client's recent jobs
  GET  /media/artifacts/{id}/{n}     image bytes
  GET  /media/models                 allowed models, final/draft, backend, last verified

All of these need the `media` client scope (client_auth.py). A client sees and
downloads only its own jobs unless it also holds the admin scope.
"""

from __future__ import annotations

import base64
import time

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import client_auth
from ..media import MediaError

router = APIRouter()

_MAX_WAIT = 60.0


def _media(request: Request):
    return request.app.state.services.media


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _err(e: MediaError) -> JSONResponse:
    headers = {}
    ra = e.extra.get("retry_after") or e.extra.get("reset_seconds")
    if e.status == 429 and ra:
        headers["Retry-After"] = str(int(ra))
    return JSONResponse(e.body(), status_code=e.status, headers=headers)


def _bad(message: str, code: str = "invalid_request", status: int = 400) -> JSONResponse:
    return _err(MediaError(status, code, message))


def _owned(row: dict) -> bool:
    c = client_auth.current()
    return "admin" in (c.get("scopes") or []) or row.get("client_id") == c.get("client_id")


def _job_or_404(media, job_id: str):
    row = media.get(job_id)
    if row is None or not _owned(row):
        return None
    return row


def _idem(request: Request, body: dict):
    return body.get("idempotency_key") or request.headers.get("idempotency-key")


# -- OpenAI-compatible (synchronous) ----------------------------------------------

async def _sync_images(request: Request, op: str, fields: dict, images: list, mask):
    media = _media(request)
    key = fields.get("idempotency_key") or request.headers.get("idempotency-key")
    try:
        row, _dup = await media.submit(
            op, prompt=fields.get("prompt") or "", model=fields.get("model"),
            size=fields.get("size"), quality=fields.get("quality"),
            background=fields.get("background"), output_format=fields.get("output_format"),
            n=fields.get("n") or 1, images=images, mask=mask,
            input_fidelity=fields.get("input_fidelity"),
            # OpenAI clients regenerate on purpose; only an explicit key dedupes.
            idempotency_key=key, auto_dedupe=False)
    except MediaError as e:
        return _err(e)
    row = await media.wait(row["id"], float(media.cfg.job_timeout_seconds) + 60)
    if row["status"] != "succeeded":
        if row["status"] in ("queued", "running"):
            return _bad(f"image job {row['id']} is still running; poll GET /media/jobs/{row['id']}",
                        "job_pending", 504)
        return JSONResponse({"error": {"message": row.get("error") or row["status"],
                                       "code": row.get("error_code") or row["status"],
                                       "type": "api_error", "job_id": row["id"]}},
                            status_code=int(row.get("error_status") or 502))
    view = media.view(row, _base(request))
    want_url = str(fields.get("response_format") or "").lower() == "url"
    data = []
    for a in view["artifacts"]:
        item = {"url": a["url"]} if want_url else {
            "b64_json": base64.b64encode(media.artifact_path(row["id"], a["index"])[0]
                                         .read_bytes()).decode()}
        if a.get("revised_prompt"):
            item["revised_prompt"] = a["revised_prompt"]
        data.append(item)
    out = {"created": int(time.time()), "data": data}
    for k in ("size", "quality", "background", "output_format"):
        if view["settings"].get(k):
            out[k] = view["settings"][k]
    usage = row.get("usage")
    if usage:
        import json
        try:
            out["usage"] = json.loads(usage)
        except ValueError:
            pass
    return JSONResponse(out, headers={"x-foundry-media-job": row["id"],
                                      "x-foundry-served-model": row["model"],
                                      "x-foundry-served-backend": row["backend"] or ""})


@router.post("/v1/images/generations")
async def images_generations(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return _bad("body must be JSON")
    if not isinstance(body, dict):
        return _bad("body must be a JSON object")
    return await _sync_images(request, "generate", body, [], None)


@router.post("/v1/images/edits")
async def images_edits(request: Request):
    ctype = request.headers.get("content-type", "").lower()
    if ctype.startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            return _bad("body must be JSON")
        images = body.get("images") or ([body["image"]] if body.get("image") else [])
        return await _sync_images(request, "edit", body, images, body.get("mask"))
    form = await request.form()
    fields = {k: v for k, v in form.items() if isinstance(v, str)}
    images = []
    for name in ("image[]", "image"):
        for f in form.getlist(name):
            if hasattr(f, "read"):
                images.append(base64.b64encode(await f.read()).decode())
    mask = form.get("mask")
    mask = base64.b64encode(await mask.read()).decode() if hasattr(mask, "read") else None
    return await _sync_images(request, "edit", fields, images, mask)


# -- Foundry job API (asynchronous) -----------------------------------------------

@router.post("/media/jobs")
async def submit_job(request: Request):
    media = _media(request)
    try:
        body = await request.json()
    except ValueError:
        return _bad("body must be JSON")
    if not isinstance(body, dict):
        return _bad("body must be a JSON object")
    op = body.get("op") or ("edit" if body.get("images") else "generate")
    try:
        row, dup = await media.submit(
            op, prompt=body.get("prompt") or "", model=body.get("model"),
            size=body.get("size"), quality=body.get("quality"),
            background=body.get("background"), output_format=body.get("output_format"),
            n=body.get("n") or 1, images=body.get("images") or [], mask=body.get("mask"),
            input_fidelity=body.get("input_fidelity"),
            idempotency_key=_idem(request, body),
            auto_dedupe=not body.get("allow_duplicate"))
    except MediaError as e:
        return _err(e)
    wait = max(0.0, min(_MAX_WAIT, float(body.get("wait_seconds") or 0)))
    if wait:
        row = await media.wait(row["id"], wait)
    return JSONResponse(media.view(row, _base(request), deduplicated=dup),
                        status_code=200 if dup else 202)


@router.get("/media/jobs")
async def list_jobs(request: Request, limit: int = 20):
    media = _media(request)
    c = client_auth.current()
    cid = None if "admin" in (c.get("scopes") or []) else c.get("client_id")
    return {"jobs": [media.view(r, _base(request)) for r in media.list_jobs(limit, cid)]}


@router.get("/media/jobs/{job_id}")
async def job_status(request: Request, job_id: str, wait: float = 0):
    media = _media(request)
    if _job_or_404(media, job_id) is None:
        return _bad(f"no image job {job_id!r}", "job_not_found", 404)
    row = await media.wait(job_id, max(0.0, min(_MAX_WAIT, wait)))
    return media.view(row, _base(request))


@router.post("/media/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str):
    media = _media(request)
    if _job_or_404(media, job_id) is None:
        return _bad(f"no image job {job_id!r}", "job_not_found", 404)
    return media.view(await media.cancel(job_id), _base(request))


@router.get("/media/artifacts/{job_id}/{index}")
async def artifact(request: Request, job_id: str, index: int):
    media = _media(request)
    if _job_or_404(media, job_id) is None:
        return _bad("no such artifact", "artifact_not_found", 404)
    hit = media.artifact_path(job_id, index)
    if hit is None:
        return _bad("no such artifact (not finished, or pruned)", "artifact_not_found", 404)
    path, meta = hit
    return FileResponse(path, media_type=meta["mime"],
                        filename=f"{job_id}-{index}.{path.suffix.lstrip('.')}",
                        headers={"x-content-sha256": meta["sha256"],
                                 "cache-control": "private, max-age=86400"})


@router.get("/media/models")
async def media_models(request: Request):
    s = _media(request).summary()
    return {k: s[k] for k in ("enabled", "backend", "backend_error", "models",
                              "used_5h", "used_day", "jobs_per_5h", "jobs_per_day", "active")}

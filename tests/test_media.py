"""Image jobs: verified model/backend, duplicate protection, bounded retries,
no resubmission on timeout, separate budget, artifacts, and the OpenAI and MCP
surfaces. Every test counts the requests the image backend actually received."""

import asyncio
import base64
import hashlib
import json
import struct
import time
import zlib

import httpx
import pytest

from foundry_router.config import BackendConfig, BackendPoolConfig, MediaConfig
from foundry_router.pool.internal import InternalPool

HITS: list = []
STATE: dict = {}


def png(w=64, h=48, tag=b"x") -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", b"k\x00" + tag) + chunk(b"IEND", b""))


def _now_iso(offset_s=0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


async def _router(req: httpx.Request):
    path = req.url.path
    if path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "gpt-5.5"},
                                                  {"id": "gpt-image-2.5-sunburst"},
                                                  {"id": "gpt-image-2.5-flare"}]})
    if path == "/v0/management/auth-files":
        return httpx.Response(200, json={"files": [{"provider": "codex", "label": "me", "quota": {
            "observed_at": _now_iso(-30), "signals": {
                "X-Codex-Primary-Used-Percent": str(STATE.get("used", 10)),
                "X-Codex-Primary-Window-Minutes": "300",
                "X-Codex-Primary-Reset-After-Seconds": "3000"}}}]})
    if "/images/" not in path:
        return httpx.Response(404)
    body = json.loads(req.content)
    HITS.append((path, body))
    mode = STATE.get("mode", "ok")
    if mode == "slow":
        await asyncio.sleep(STATE.get("delay", 0.5))
    if mode == "timeout":
        raise httpx.ReadTimeout("read timed out", request=req)
    if mode == "connect" or (mode == "connect_once" and len(HITS) == 1):
        raise httpx.ConnectError("refused", request=req)
    if mode == "429":
        return httpx.Response(429, headers={"retry-after": "120"},
                              json={"error": {"message": "usage limit reached"}})
    n = int(body.get("n") or 1)
    return httpx.Response(200, json={"created": 1, "data": [
        {"b64_json": base64.b64encode(png(tag=b"%d" % i)).decode(), "revised_prompt": "rp"}
        for i in range(n)], "usage": {"total_tokens": 10}, "size": body.get("size")})


@pytest.fixture()
def media(app, client):
    svc = app.state.services
    HITS.clear()
    STATE.clear()
    http = httpx.AsyncClient(transport=httpx.MockTransport(_router))
    pool = InternalPool([
        BackendConfig(name="cliproxy", type="openai-compatible", url="http://cpa:8317/v1",
                      api_key="k", flavor="cliproxyapi", management_key="m"),
        BackendConfig(name="openai-api", type="openai-compatible",
                      url="https://api.example.com/v1", api_key="sk")],
        BackendPoolConfig(), http, svc.db)
    s = pool.backends["cliproxy"]
    s.healthy = s.ever_checked = True
    s.models = ["gpt-5.5", "gpt-image-2.5-sunburst", "gpt-image-2.5-flare"]
    pool.backends["openai-api"].healthy = True
    real_pool, real_http, real_cu = svc.pool, svc.http, svc.cliproxy_usage.client
    svc.pool, svc.http, svc.cliproxy_usage.client = pool, http, http
    svc.cliproxy_usage._cache.clear()
    cfg = svc.config_store.config
    saved = cfg.media
    cfg.media = MediaConfig(max_attempts=2)
    yield svc
    cfg.media = saved
    svc.pool, svc.http, svc.cliproxy_usage.client = real_pool, real_http, real_cu


def _submit(client, **body):
    body.setdefault("prompt", "a red fox icon")
    return client.post("/media/jobs", json=body)


def _done(client, job_id, wait=10):
    r = client.get(f"/media/jobs/{job_id}", params={"wait": wait})
    assert r.status_code == 200
    return r.json()


def test_generate_job_artifact_roundtrip(media, client):
    r = _submit(client, size="1024x1024", quality="high", background="transparent")
    assert r.status_code == 202
    job = r.json()
    assert job["status"] in ("queued", "running", "succeeded")
    assert job["model"] == "gpt-image-2.5-sunburst" and job["backend"] == "cliproxy"
    job = _done(client, job["job_id"])
    assert job["status"] == "succeeded", job
    (path, sent), = HITS
    assert path == "/v1/images/generations"
    assert sent == {"model": "gpt-image-2.5-sunburst", "prompt": "a red fox icon", "n": 1,
                    "response_format": "b64_json", "size": "1024x1024", "quality": "high",
                    "background": "transparent"}
    a, = job["artifacts"]
    assert (a["mime"], a["width"], a["height"]) == ("image/png", 64, 48)
    got = client.get(f"/media/artifacts/{job['job_id']}/0")
    assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    assert hashlib.sha256(got.content).hexdigest() == a["sha256"] == got.headers["x-content-sha256"]
    assert "b64" not in json.dumps(job)          # no image bytes in the status reply


def test_duplicate_requests_return_the_same_job(media, client):
    first = _submit(client).json()
    _done(client, first["job_id"])
    again = _submit(client)
    assert again.status_code == 200 and again.json()["deduplicated"] is True
    assert again.json()["job_id"] == first["job_id"]
    assert len(HITS) == 1
    # a deliberate variation
    var = _submit(client, allow_duplicate=True).json()
    assert var["job_id"] != first["job_id"]
    _done(client, var["job_id"])
    assert len(HITS) == 2
    # an explicit idempotency key always returns its job, even a failed one
    STATE["mode"] = "429"
    k1 = _submit(client, prompt="other", idempotency_key="task-7-img").json()
    assert _done(client, k1["job_id"])["status"] == "failed"
    k2 = _submit(client, prompt="other", idempotency_key="task-7-img").json()
    assert k2["job_id"] == k1["job_id"] and len(HITS) == 3


def test_timeout_is_never_resubmitted_and_polling_never_submits(media, client):
    STATE["mode"] = "timeout"
    job = _submit(client).json()
    job = _done(client, job["job_id"])
    assert job["status"] == "failed" and job["error"]["code"] == "backend_timeout"
    assert "NOT resubmitted" in job["error"]["message"]
    for _ in range(3):
        _done(client, job["job_id"], wait=0)
    assert len(HITS) == 1


def test_connect_failure_retries_are_bounded(media, client):
    STATE["mode"] = "connect_once"
    job = _done(client, _submit(client).json()["job_id"], wait=15)
    assert job["status"] == "succeeded" and job["attempts"] == 2
    HITS.clear()
    STATE["mode"] = "connect"
    job = _done(client, _submit(client, prompt="p2").json()["job_id"], wait=15)
    assert job["status"] == "failed" and job["error"]["code"] == "backend_unreachable"
    assert len(HITS) == 2                         # max_attempts, then stop


def test_quota_refusal_is_not_retried_elsewhere(media, client):
    STATE["mode"] = "429"
    job = _done(client, _submit(client).json()["job_id"])
    assert job["status"] == "failed" and job["error"]["code"] == "quota_exceeded"
    assert job["error"]["status"] == 429 and len(HITS) == 1


def test_model_selection_fails_closed(media, client):
    r = _submit(client, model="dall-e-3")
    assert r.status_code == 400 and r.json()["error"]["code"] == "model_not_allowed"
    r = _submit(client, model="gpt-image-2")    # allowed but this backend doesn't list it
    assert r.status_code == 503 and r.json()["error"]["code"] == "model_unavailable"
    assert _submit(client, model="draft").json()["model"] == "gpt-image-2.5-flare"
    media.personas.upsert("Foundry-Kilo-Images", execution_mode="direct", routing="strict",
                          model_allowlist=["gpt-image-2.5-sunburst"])
    j = _submit(client, model="Foundry-Kilo-Images", prompt="via persona").json()
    assert j["model"] == "gpt-image-2.5-sunburst" and j["persona"] == "Foundry-Kilo-Images"
    media.personas.upsert("Chatty", execution_mode="direct", model_allowlist=["gpt-5.5"])
    assert _submit(client, model="Chatty").json()["error"]["code"] == "model_not_allowed"
    assert all(b["model"] in ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst") for _, b in HITS)


def test_paid_api_backend_is_refused(media, client):
    media.config_store.config.media.backend = "openai-api"
    r = _submit(client)
    assert r.status_code == 403 and r.json()["error"]["code"] == "paid_backend_refused"
    assert HITS == []


def test_image_budget_and_subscription_limit(media, client):
    media.config_store.config.media.jobs_per_5h = 1
    _done(client, _submit(client).json()["job_id"])
    r = _submit(client, prompt="second")
    assert r.status_code == 429 and r.json()["error"]["code"] == "budget_denied"
    assert r.headers.get("retry-after")
    assert len(HITS) == 1
    media.config_store.config.media.jobs_per_5h = 0
    STATE["used"] = 91                           # ChatGPT window above the 85% limit
    media.cliproxy_usage._cache.clear()
    r = _submit(client, prompt="third")
    assert r.status_code == 429 and "usage" in r.json()["error"]["message"]
    assert len(HITS) == 1


def test_queued_job_cancel_is_never_sent(media, client):
    STATE.update(mode="slow", delay=1.0)
    a = _submit(client, prompt="slow one").json()
    b = _submit(client, prompt="queued one").json()
    r = client.post(f"/media/jobs/{b['job_id']}/cancel").json()
    assert r["status"] == "cancelled" and "nothing was generated" in r["error"]["message"]
    assert _done(client, a["job_id"])["status"] == "succeeded"
    assert [body["prompt"] for _, body in HITS] == ["slow one"]


def test_edit_json_and_openai_multipart(media, client):
    src = "data:image/png;base64," + base64.b64encode(png(32, 32)).decode()
    mask = base64.b64encode(png(32, 32, b"m")).decode()
    job = _submit(client, op="edit", prompt="make it blue", images=[src], mask=mask,
                  input_fidelity="high").json()
    assert _done(client, job["job_id"])["status"] == "succeeded"
    path, sent = HITS[-1]
    assert path == "/v1/images/edits"
    assert sent["images"][0]["image_url"].startswith("data:image/png;base64,")
    assert sent["mask"]["image_url"].startswith("data:image/png;base64,")
    assert sent["input_fidelity"] == "high"
    bad = _submit(client, op="edit", prompt="x", images=["bm90IGFuIGltYWdl"])
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_image"
    r = client.post("/v1/images/edits", data={"model": "gpt-image-2.5-sunburst",
                                              "prompt": "edit via form"},
                    files=[("image[]", ("a.png", png(16, 16), "image/png"))])
    assert r.status_code == 200, r.text
    assert base64.b64decode(r.json()["data"][0]["b64_json"])[:8] == b"\x89PNG\r\n\x1a\n"


def test_openai_generations_sync(media, client):
    r = client.post("/v1/images/generations", json={"model": "gpt-image-2.5-sunburst",
                                                    "prompt": "owui", "n": 2,
                                                    "size": "1024x1024"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["data"]) == 2 and body["data"][0]["revised_prompt"] == "rp"
    assert r.headers["x-foundry-served-model"] == "gpt-image-2.5-sunburst"
    # OpenAI clients regenerate on purpose: no automatic dedupe here
    r2 = client.post("/v1/images/generations", json={"model": "gpt-image-2.5-sunburst",
                                                     "prompt": "owui", "n": 2,
                                                     "size": "1024x1024",
                                                     "response_format": "url"})
    assert r2.json()["data"][0]["url"].startswith("http")
    assert len(HITS) == 2
    STATE["mode"] = "429"
    r3 = client.post("/v1/images/generations", json={"prompt": "fails"})
    assert r3.status_code == 429 and r3.json()["error"]["code"] == "quota_exceeded"


def test_jobs_are_private_to_their_client(media, client):
    from foundry_router.config import ClientConfig
    cfg = media.config_store.config
    cfg.clients = [ClientConfig(name="kilo", key="kk", scopes=["inference", "media"]),
                   ClientConfig(name="owui", key="ok", scopes=["inference", "media"])]
    try:
        r = client.post("/media/jobs", json={"prompt": "kilo's"},
                        headers={"Authorization": "Bearer kk"})
        jid = r.json()["job_id"]
        assert client.get(f"/media/jobs/{jid}", headers={"Authorization": "Bearer ok"}
                          ).status_code == 404
        assert client.get(f"/media/artifacts/{jid}/0", headers={"Authorization": "Bearer ok"}
                          ).status_code == 404
        assert client.get(f"/media/jobs/{jid}", headers={"Authorization": "Bearer kk"},
                          params={"wait": 10}).json()["client_id"] == "kilo"
    finally:
        cfg.clients = []


def test_restart_marks_inflight_jobs_failed(media):
    from foundry_router.media import MediaService
    media.db.execute("INSERT INTO media_jobs (id, created_ts, op, status, client_id) "
                     "VALUES ('img_00000000000000aa', ?, 'generate', 'running', 'anonymous')",
                     (time.time(),))
    MediaService(media)
    row = media.media.get("img_00000000000000aa")
    assert row["status"] == "failed" and row["error_code"] == "interrupted"


def test_image_models_are_not_chat_candidates(media):
    from foundry_router.media import is_image_model
    assert is_image_model("gpt-image-2.5-sunburst") and is_image_model("grok-imagine-image")
    assert not is_image_model("gpt-5.5") and not is_image_model("qwen3.8-flash")
    media.register_discovered()
    assert media.registry.get("gpt-image-2.5-sunburst")["embedding"] == 1
    assert not media.registry.get("gpt-5.5").get("embedding")


def test_mcp_media_tools(media, client):
    agg = media.mcp_aggregator
    names = {t.name for t in agg._media_tools()}
    assert names == {"generate_image", "edit_image", "image_job_status", "cancel_image_job"}

    async def go():
        out = await agg._media_call("generate_image", {"prompt": "mcp fox", "wait_seconds": 10,
                                                       "return_image": True})
        view = json.loads(out[0].text)
        return view, out

    view, out = client.portal.call(go)
    assert view["status"] == "succeeded" and view["artifacts"][0]["url"].endswith("/0")
    assert out[1].type == "image" and out[1].mimeType == "image/png"

    async def status():
        return await agg._media_call("image_job_status", {"job_id": view["job_id"]})
    again = json.loads(client.portal.call(status)[0].text)
    assert again["job_id"] == view["job_id"] and len(HITS) == 1

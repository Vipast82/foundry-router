"""v0.181.159: Host Admin load / unload, a first load that does not wait on slow
hosts, and the capture record list read off the event loop."""
import asyncio
import inspect
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from foundry_router.config import BackendConfig
from foundry_router.db import Database
from foundry_router.pool.ollama_admin import OllamaAdmin
from foundry_router.pool.openai_admin import OpenAIAdmin


class _State:
    def __init__(self, cfg):
        self.config, self.healthy = cfg, True


class _Pool:
    def __init__(self, cfgs):
        self.backends = {c.name: _State(c) for c in cfgs}

    async def check_all(self):
        pass


def _strata(seen, in_flight=0, service="strata", fail=False):
    def handler(request):
        p = request.url.path
        seen.append((request.method, p, request.headers.get("authorization")))
        if p == "/health":
            return httpx.Response(200, json={"status": "ok", "service": service, "loaded": True})
        if p == "/v1/status":
            return httpx.Response(200, json={"activity": {"in_flight": in_flight}})
        if p in ("/unload", "/load") and request.method == "POST":
            if fail:
                return httpx.Response(409, json={"error": {"message": "Model is busy"}})
            return httpx.Response(200, json={"status": "unloaded" if p == "/unload" else "loaded"})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _oa(tmp_path, transport):
    cfg = BackendConfig(name="strata", type="openai-compatible", url="http://strata:1/v1",
                        flavor="llamacpp", api_key="sk-strata")
    return OpenAIAdmin(httpx.AsyncClient(transport=transport), _Pool([cfg]), Database(tmp_path / "a.sqlite"))


@pytest.mark.asyncio
async def test_strata_is_unloaded_and_loaded_with_the_backends_key(tmp_path):
    seen = []
    adm = _oa(tmp_path, _strata(seen))
    assert await adm.control("strata", "unload") == {"status": "unloaded"}
    assert ("POST", "/unload", "Bearer sk-strata") in seen and ("GET", "/v1/status", "Bearer sk-strata") in seen
    assert await adm.control("strata", "load") == {"status": "loaded"}
    assert any("model unload asked from Host Admin" in r["message"]
               for r in adm.db.query("SELECT message FROM event_log"))


@pytest.mark.asyncio
async def test_an_unload_is_refused_while_a_request_runs_or_on_another_server(tmp_path):
    seen = []
    adm = _oa(tmp_path, _strata(seen, in_flight=2))
    with pytest.raises(ValueError) as e:
        await adm.control("strata", "unload")
    assert "2 request(s) are running" in str(e.value) and not any(p == "/unload" for _, p, _ in seen)
    assert await adm.control("strata", "unload", force=True) == {"status": "unloaded"}     # the operator insists
    plain = _oa(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, _strata([], service="llama.cpp"))
    with pytest.raises(ValueError) as e:
        await plain.control("strata", "unload")
    assert "no load / unload over HTTP" in str(e.value)
    with pytest.raises(ValueError):
        await adm.control("strata", "restart")
    busy = _oa(tmp_path / "c" if (tmp_path / "c").mkdir() is None else tmp_path, _strata([], fail=True))
    with pytest.raises(RuntimeError) as e:
        await busy.control("strata", "load")
    assert "Model is busy" in str(e.value)


@pytest.mark.asyncio
async def test_ollama_unloads_a_chat_model_and_an_embedding_model(tmp_path):
    loaded = {"llama3:8b", "nomic-embed-text:latest"}
    calls = []

    def handler(request):
        p = request.url.path
        if p == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": n} for n in sorted(loaded)]})
        body = json.loads(request.content or b"{}")
        calls.append((p, body))
        if p == "/api/generate":
            if "embed" in body["model"]:
                return httpx.Response(400, json={"error": "does not support generate"})
            loaded.discard(body["model"])
            return httpx.Response(200, json={"done": True})
        if p == "/api/embed":
            if body["model"] != "stuck-embed:latest":
                loaded.discard(body["model"])
            return httpx.Response(200, json={"embeddings": []})
        return httpx.Response(404)
    cfg = BackendConfig(name="emb", type="ollama", url="http://ollama:11434")
    adm = OllamaAdmin(httpx.AsyncClient(transport=httpx.MockTransport(handler)), _Pool([cfg]),
                      Database(tmp_path / "o.sqlite"))
    assert await adm.unload("emb", "llama3:8b") == {"unloaded": True}
    assert calls[-1] == ("/api/generate", {"model": "llama3:8b", "keep_alive": 0})
    assert await adm.unload("emb", "nomic-embed-text:latest") == {"unloaded": True}
    assert calls[-1] == ("/api/embed", {"model": "nomic-embed-text:latest", "input": "x", "keep_alive": 0})
    loaded.add("stuck-embed:latest")                     # Ollama answers, but keeps it loaded
    assert await adm.unload("emb", "stuck-embed:latest") == {"unloaded": False}


def test_the_route_refuses_what_it_cannot_do(client):
    r = client.post("/admin/api/host/control", json={"backend": "nope", "action": "unload"})
    assert r.status_code == 400 and "unknown backend" in r.json()["error"]


def test_the_first_load_does_not_wait_for_a_slow_host():
    from foundry_router.ui import routes
    src = inspect.getsource(routes.host_backends)
    assert "asyncio.wait_for(probe(), _VERSION_WAIT)" in src and "asyncio.gather(" in src
    assert routes._VERSION_WAIT == 2.5

    async def slow():
        await asyncio.sleep(30)
        return "9.9"

    async def fast():
        return "1.2.3"
    admins = SimpleNamespace(
        ollama_admin=SimpleNamespace(backends=lambda: [{"name": "o1", "url": "u", "healthy": True}],
                                     version=lambda n: slow(), job_snapshot=lambda: []),
        openai_admin=SimpleNamespace(backends=lambda: [{"name": "l1", "url": "u", "flavor": "llamacpp", "healthy": True}]),
        pool=SimpleNamespace(backends={"l1": SimpleNamespace(protocol=SimpleNamespace(server_version=fast),
                                                             config=SimpleNamespace(type="openai-compatible"))}))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=admins)))
    routes._VERSION_WAIT = 0.2
    try:
        t0 = time.monotonic()
        out = asyncio.run(routes.host_backends(req))
        took = time.monotonic() - t0
        again = asyncio.run(routes.host_backends(req))
    finally:
        routes._VERSION_WAIT = 2.5
    rows = {b["name"]: b for b in out["backends"]}
    assert took < 1.5 and rows["o1"]["version"] == "" and rows["l1"]["version"] == "1.2.3"
    assert {b["name"]: b["version"] for b in again["backends"]}["l1"] == "1.2.3"          # remembered


def test_the_record_list_is_read_off_the_event_loop_and_the_page_has_the_buttons():
    from foundry_router.facade import work_api
    src = inspect.getsource(work_api.capture_records)
    assert "await asyncio.to_thread(build)" in src and "cap.records(after_id" in src
    assert src.index("def build()") < src.index("cap.records(after_id")       # inside the thread's function
    from foundry_router.ui import routes
    html = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'data-host-act="unload"' in html and 'data-host-act="load"' in html
    assert "window.ollUnload = async name =>" in html and 'api("host/control"' in html
    assert html.count("confirm(") >= 2 and "Running sprints that use it are held up" in html

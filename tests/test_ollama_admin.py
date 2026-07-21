"""Ollama model management proxied to a chosen backend. Uses an httpx
MockTransport standing in for the Ollama host so the lifecycle ops (tags / show /
copy / delete / rename / streaming pull) are exercised without a real server."""

import asyncio
import json

import httpx
import pytest

from foundry_router.db import Database
from foundry_router.pool.ollama_admin import OllamaAdmin, parse_modelfile

_CREATE_PAYLOADS = []   # captured /api/create request bodies


class _Cfg:
    def __init__(self, name, url, type="ollama"):
        self.name, self.url, self.type = name, url, type


class _State:
    def __init__(self, cfg, healthy=True):
        self.config, self.healthy = cfg, healthy


class _Pool:
    def __init__(self):
        self.backends = {"truenas": _State(_Cfg("truenas", "http://ollama")),
                         "claude": _State(_Cfg("claude", "http://c", type="anthropic-compatible"))}
        self.checked = 0

    async def check_all(self):
        self.checked += 1


def _handler(request):
    p = request.url.path
    if p == "/api/tags":
        return httpx.Response(200, json={"models": [
            {"name": "llama3:8b", "size": 4_000_000_000,
             "details": {"parameter_size": "8B", "quantization_level": "Q4_0"}}]})
    if p == "/api/show":
        body = json.loads(request.content)
        return httpx.Response(200, json={"details": {"family": "llama"},
                                         "modelfile": "FROM llama3", "model": body.get("model")})
    if p == "/api/ps":
        return httpx.Response(200, json={"models": [{"name": "llama3:8b"}]})
    if p in ("/api/copy", "/api/delete"):
        return httpx.Response(200)
    if p == "/api/pull":
        nd = (b'{"status":"pulling manifest"}\n'
              b'{"status":"downloading","total":100,"completed":50}\n'
              b'{"status":"success"}\n')
        return httpx.Response(200, content=nd)
    if p == "/api/create":
        _CREATE_PAYLOADS.append(json.loads(request.content))
        return httpx.Response(200, content=b'{"status":"creating"}\n{"status":"success"}\n')
    return httpx.Response(404, text="not found")


def _admin(tmp_path):
    db = Database(tmp_path / "o.sqlite")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return OllamaAdmin(client, _Pool(), db), db


def test_backends_lists_only_ollama(tmp_path):
    admin, _ = _admin(tmp_path)
    names = [b["name"] for b in admin.backends()]
    assert names == ["truenas"]            # the anthropic backend is excluded


async def test_tags_and_show(tmp_path):
    admin, _ = _admin(tmp_path)
    models = await admin.tags("truenas")
    assert models[0]["name"] == "llama3:8b"
    info = await admin.show("truenas", "llama3:8b")
    assert info["details"]["family"] == "llama"


async def test_loaded_reports_vram_resident(tmp_path):
    admin, _ = _admin(tmp_path)
    assert await admin.loaded("truenas") == ["llama3:8b"]


async def test_copy_and_delete_refresh_pool_and_log(tmp_path):
    admin, db = _admin(tmp_path)
    await admin.copy("truenas", "llama3:8b", "llama3:mine")
    await admin.delete("truenas", "old:model")
    assert admin.pool.checked == 2         # both refreshed the model map
    msgs = [e["message"] for e in db.query("SELECT message FROM event_log")]
    assert any("copied" in m for m in msgs) and any("deleted" in m for m in msgs)


async def test_rename_is_copy_plus_delete(tmp_path):
    admin, db = _admin(tmp_path)
    await admin.rename("truenas", "a:b", "c:d")
    msgs = [e["message"] for e in db.query("SELECT message FROM event_log")]
    assert any("renamed a:b -> c:d" in m for m in msgs)


def test_unknown_backend_raises(tmp_path):
    admin, _ = _admin(tmp_path)
    with pytest.raises(ValueError):
        admin.start_pull("nope", "x")      # validated before any task is scheduled


# -- create: raw Modelfile -> structured fields (modern Ollama) --------------------

def test_parse_modelfile_to_structured():
    mf = ('FROM hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL\n'
          'PARAMETER num_ctx 262144\n'
          'PARAMETER temperature 0.6\n'
          'PARAMETER repeat_penalty 1.05\n'
          'SYSTEM """You are a senior software engineer.\n'
          'Write good code."""\n')
    out = parse_modelfile(mf)
    assert out["from"] == "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL"
    assert out["parameters"]["num_ctx"] == 262144            # int-coerced
    assert out["parameters"]["temperature"] == 0.6           # float-coerced
    assert out["parameters"]["repeat_penalty"] == 1.05
    assert out["system"].startswith("You are a senior software engineer.")
    assert "Write good code." in out["system"] and '"""' not in out["system"]


def test_parse_modelfile_repeated_param_is_list():
    out = parse_modelfile("FROM x\nPARAMETER stop <a>\nPARAMETER stop <b>\n")
    assert out["parameters"]["stop"] == ["<a>", "<b>"]


def test_create_without_from_raises(tmp_path):
    admin, _ = _admin(tmp_path)
    with pytest.raises(ValueError):     # the exact 400 the user hit, caught early
        admin.start_create("truenas", "x", modelfile="PARAMETER temperature 0.5")


async def test_create_sends_structured_not_modelfile(tmp_path):
    _CREATE_PAYLOADS.clear()
    admin, _ = _admin(tmp_path)
    key = admin.start_create("truenas", "claude-qwen36",
        modelfile='FROM base:latest\nSYSTEM """hi there"""\nPARAMETER temperature 0.6')
    for _ in range(200):
        if admin.jobs[key]["state"] != "running":
            break
        await asyncio.sleep(0.01)
    assert _CREATE_PAYLOADS, "create should have been called"
    pl = _CREATE_PAYLOADS[-1]
    assert pl["from"] == "base:latest" and "modelfile" not in pl   # structured, no legacy field
    assert pl["system"] == "hi there" and pl["parameters"]["temperature"] == 0.6


async def test_pull_streams_to_completion(tmp_path):
    admin, db = _admin(tmp_path)
    key = admin.start_pull("truenas", "llama3:8b")
    for _ in range(200):
        if admin.jobs[key]["state"] != "running":
            break
        await asyncio.sleep(0.01)
    job = admin.jobs[key]
    assert job["state"] == "done" and job["percent"] == 100.0
    assert admin.pool.checked == 1         # refreshed after a successful pull


# -- endpoint guards --------------------------------------------------------------

def test_endpoints_validate_and_report(client):
    # no ollama backend configured in the test app
    assert client.get("/admin/api/ollama/backends").json()["backends"] == []
    # missing fields -> 400
    assert client.post("/admin/api/ollama/show", json={}).status_code == 400
    assert client.post("/admin/api/ollama/copy", json={"backend": "x"}).status_code == 400
    # create needs from or modelfile
    assert client.post("/admin/api/ollama/create",
                       json={"backend": "x", "model": "m"}).status_code == 400
    # pull at an unknown backend -> clean 400, not a 500
    assert client.post("/admin/api/ollama/pull",
                       json={"backend": "nope", "model": "m"}).status_code == 400

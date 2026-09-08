"""Read-only host diagnostics for openai-dialect backends (llama.cpp / Unsloth /
vLLM / OpenAI). An httpx MockTransport stands in for each server so the per-flavor
probe set and the graceful per-probe error handling are exercised without a real
host. Also covers BackendConfig.effective_flavor (which drives panel selection)."""

import httpx
import pytest

from foundry_router.config import BackendConfig
from foundry_router.db import Database
from foundry_router.pool.openai_admin import OpenAIAdmin


class _State:
    def __init__(self, cfg, healthy=True):
        self.config, self.healthy = cfg, healthy


class _Pool:
    def __init__(self, states):
        self.backends = {s.config.name: s for s in states}


def _handler(request):
    host = request.url.host
    p = request.url.path
    auth = request.headers.get("Authorization", "")
    if host == "llamacpp":
        if p == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if p == "/props":
            return httpx.Response(200, json={
                "model_path": "/models/qwen.gguf", "total_slots": 4,
                "chat_template": "{{ }}",
                "default_generation_settings": {"n_ctx": 8192}})
        if p == "/slots":
            return httpx.Response(501, text="slots disabled")   # --no-slots
        if p == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen.gguf"}]})
    if host == "unsloth":
        if p == "/v1/models":
            if not auth.startswith("Bearer sk-unsloth-"):
                return httpx.Response(401, text="missing key")
            return httpx.Response(200, json={"data": [{"id": "unsloth/gpt-oss"}]})
    if host == "vllm":
        if p == "/health":
            return httpx.Response(200, text="")
        if p == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "vllm-model"}]})
        if p == "/metrics":
            return httpx.Response(200, text=(
                "# HELP\nvllm:num_requests_running 2.0\n"
                "vllm:num_requests_waiting 1.0\nother_metric 99\n"))
    return httpx.Response(404, text="not found")


def _admin(tmp_path, *cfgs):
    db = Database(tmp_path / "oa.sqlite")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return OpenAIAdmin(client, _Pool([_State(c) for c in cfgs]), db)


LLAMA = BackendConfig(name="llamacpp-1", type="openai-compatible",
                      url="http://llamacpp:8080", flavor="llamacpp")
UNSLOTH = BackendConfig(name="unsloth-1", type="openai-compatible",
                        url="http://unsloth:8000/v1", flavor="unsloth",
                        api_key="sk-unsloth-abc")
VLLM = BackendConfig(name="vllm-1", type="openai-compatible",
                     url="http://vllm:8000", flavor="vllm")
OLLA = BackendConfig(name="oll", type="ollama", url="http://ollama")


# -- effective_flavor: drives which Host Admin panel shows -------------------------

def test_effective_flavor_explicit_and_inferred():
    assert LLAMA.effective_flavor == "llamacpp"
    assert OLLA.effective_flavor == "ollama"                  # inferred from type
    # openai-compatible with no explicit flavor -> generic openai diagnostics
    assert BackendConfig(name="x", type="openai-compatible",
                         url="http://x").effective_flavor == "openai"
    # anthropic (Meridian) never surfaces in Host Admin, but the property is defined
    assert BackendConfig(name="m", type="anthropic-compatible",
                         url="http://m").effective_flavor == "openai"


# -- backends(): only openai-compatible, tagged with flavor ------------------------

def test_backends_lists_only_openai_with_flavor(tmp_path):
    admin = _admin(tmp_path, LLAMA, UNSLOTH, OLLA)
    got = admin.backends()
    assert [b["name"] for b in got] == ["llamacpp-1", "unsloth-1"]   # ollama excluded
    assert {b["name"]: b["flavor"] for b in got} == {
        "llamacpp-1": "llamacpp", "unsloth-1": "unsloth"}


# -- URL helpers -------------------------------------------------------------------

def test_root_and_v1_helpers():
    assert OpenAIAdmin._root("http://h:8080") == "http://h:8080"
    assert OpenAIAdmin._root("http://h:8080/v1") == "http://h:8080"
    assert OpenAIAdmin._v1("http://h:8080") == "http://h:8080/v1"
    assert OpenAIAdmin._v1("http://h:8080/v1") == "http://h:8080/v1"


# -- diagnostics: per-flavor probe sets + graceful per-probe errors ----------------

async def test_llamacpp_diagnostics_full_set(tmp_path):
    admin = _admin(tmp_path, LLAMA)
    rep = await admin.diagnostics("llamacpp-1")
    assert rep["flavor"] == "llamacpp"
    assert rep["health"]["ok"] and rep["health"]["body"] == {"status": "ok"}
    assert rep["props"]["ok"] and rep["props"]["model_path"] == "/models/qwen.gguf"
    assert rep["props"]["n_ctx"] == 8192 and rep["props"]["total_slots"] == 4
    # /slots returns 501 (disabled) -> captured as a per-probe error, not raised
    assert rep["slots"]["ok"] is False and rep["slots"]["error"]
    assert rep["models"]["ok"] and rep["models"]["models"] == ["qwen.gguf"]


async def test_unsloth_diagnostics_models_only_with_auth(tmp_path):
    admin = _admin(tmp_path, UNSLOTH)
    rep = await admin.diagnostics("unsloth-1")
    assert rep["flavor"] == "unsloth"
    assert set(rep) == {"backend", "flavor", "url", "root", "models"}  # models-only probe set
    assert rep["models"]["ok"] and rep["models"]["models"] == ["unsloth/gpt-oss"]


async def test_unsloth_missing_key_surfaces_as_probe_error(tmp_path):
    nokey = BackendConfig(name="unsloth-2", type="openai-compatible",
                          url="http://unsloth:8000/v1", flavor="unsloth")
    admin = _admin(tmp_path, nokey)
    rep = await admin.diagnostics("unsloth-2")
    assert rep["models"]["ok"] is False and rep["models"]["error"]


async def test_vllm_diagnostics_includes_metrics(tmp_path):
    admin = _admin(tmp_path, VLLM)
    rep = await admin.diagnostics("vllm-1")
    assert rep["flavor"] == "vllm"
    assert rep["health"]["ok"]
    assert rep["models"]["models"] == ["vllm-model"]
    assert rep["metrics"]["ok"]
    m = rep["metrics"]["metrics"]
    assert m.get("vllm:num_requests_running") == 2.0
    assert "other_metric" not in m                     # only the wanted gauges kept


def test_unknown_backend_raises(tmp_path):
    admin = _admin(tmp_path, LLAMA)
    with pytest.raises(ValueError):
        admin._server("nope")


# -- endpoint guards ---------------------------------------------------------------

def test_host_endpoints_shape_and_guarded(client):
    r = client.get("/admin/api/host/backends").json()
    assert isinstance(r["backends"], list) and "jobs" in r
    # unknown backend -> clean {ok:False,error}, never a 500
    d = client.get("/admin/api/host/diag", params={"backend": "nope"}).json()
    assert d["ok"] is False and d["error"]

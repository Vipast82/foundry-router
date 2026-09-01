"""Activity awareness: in-flight model calls (pool) and MCP tool calls
(manager), the /activity endpoint, and the aggregator's progress heartbeat."""

import asyncio
import time
import types

import pytest

from foundry_router.config import BackendPoolConfig, MCPAggregatorConfig
from foundry_router.db import Database
from foundry_router.pool.internal import InternalPool
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.facade.mcp_aggregator import MCPAggregator
from foundry_router.tools.sync import ToolDef


# -- pool in-flight model tracking -------------------------------------------------

def test_pool_active_calls(tmp_path):
    pool = InternalPool([], BackendPoolConfig(), None, Database(tmp_path / "d.sqlite"))
    assert pool.active_calls() == []
    pool._inflight_enter("qwen3.8:27b")
    pool._inflight_enter("qwen3.8:27b")          # two concurrent
    pool._inflight_enter("claude-opus-5")
    active = {a["model"]: a for a in pool.active_calls()}
    assert active["qwen3.8:27b"]["count"] == 2
    assert "seconds" in active["qwen3.8:27b"]
    assert set(active) == {"qwen3.8:27b", "claude-opus-5"}
    pool._inflight_exit("qwen3.8:27b")
    assert {a["model"] for a in pool.active_calls()} == {"qwen3.8:27b", "claude-opus-5"}
    pool._inflight_exit("qwen3.8:27b")
    assert {a["model"] for a in pool.active_calls()} == {"claude-opus-5"}   # dropped at 0


# -- MCP in-flight tool tracking ---------------------------------------------------

def test_mcp_active_calls(tmp_path):
    mgr = MCPManager([], Database(tmp_path / "d.sqlite"))
    assert mgr.active_calls() == []
    mgr._inflight[1] = {"server": "acestep-music", "tool": "generate_music",
                        "since": time.monotonic() - 12}
    a = mgr.active_calls()
    assert a[0]["server"] == "acestep-music" and a[0]["tool"] == "generate_music"
    assert a[0]["seconds"] >= 12


# -- aggregator progress heartbeat -------------------------------------------------

def _hb_agg(hb):
    fake = ToolDef(name="slow__gen", kind="mcp", description="", parameters={},
                   server="slow", mcp_tool="gen")

    async def slow(s, t, a):
        await asyncio.sleep(0.25)
        return "URL"

    svc = types.SimpleNamespace(
        tool_registry=types.SimpleNamespace(get=lambda n: fake),
        mcp=types.SimpleNamespace(call_tool=slow),
        config_store=types.SimpleNamespace(config=types.SimpleNamespace(
            mcp_aggregator=MCPAggregatorConfig(progress_heartbeat_seconds=hb))),
        db=types.SimpleNamespace(log_event=lambda *a, **k: None))
    return MCPAggregator(svc)


class _Session:
    def __init__(self):
        self.notes = []

    async def send_progress_notification(self, token, progress, total=None,
                                         message=None, **k):
        self.notes.append((token, progress, message))


def _fake_server(token):
    sess = _Session()
    ctx = types.SimpleNamespace(
        meta=types.SimpleNamespace(progressToken=token), session=sess)
    srv = types.SimpleNamespace(request_context=ctx)
    return srv, sess


async def test_heartbeat_emits_progress_with_token():
    agg = _hb_agg(1)
    srv, sess = _fake_server("tok-1")
    out = await agg._dispatch_with_heartbeat(srv, "slow__gen", {}, None, 0.05)
    assert out == "URL"
    assert len(sess.notes) >= 2                    # ~4 ticks over 0.25s at 0.05
    assert all(n[0] == "tok-1" for n in sess.notes)
    assert "still working" in sess.notes[0][2]


async def test_heartbeat_no_token_still_completes():
    agg = _hb_agg(1)
    srv, sess = _fake_server(None)                 # client sent no progressToken
    out = await agg._dispatch_with_heartbeat(srv, "slow__gen", {}, None, 0.05)
    assert out == "URL"
    assert sess.notes == []                        # nothing sent, but no crash


# -- /activity endpoint ------------------------------------------------------------

ACT_CONFIG = """
server: {host: 127.0.0.1, port: 11435}
agent_brain: {provider: ollama, endpoint: "http://127.0.0.1:9", model: b}
backend_pool: {mode: internal, internal: {backends: []}}
guardrails: {authority: internal}
registry: {research: {enabled: false}}
mcp_servers: []
"""


@pytest.fixture()
def act_client(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(ACT_CONFIG, encoding="utf-8")
    from foundry_router.main import create_app
    from fastapi.testclient import TestClient
    app = create_app(config_path=cfg, database_path=tmp_path / "t.sqlite")
    with TestClient(app) as c:
        yield c


def test_activity_endpoint_reports_inflight(act_client):
    svc = act_client.app.state.services
    svc.pool._inflight_enter("qwen3.8:27b")
    # give the model a measured warm speed: 100 tokens in 1s = 100 tok/s
    svc.registry.upsert_auto("qwen3.8:27b", source="discovery")
    svc.registry.note_inference("qwen3.8:27b", 100, 1_000_000_000)
    svc.mcp._inflight[1] = {"server": "acestep-music", "tool": "generate_music",
                            "since": time.monotonic()}
    d = act_client.get("/admin/api/activity").json()
    m = next(x for x in d["models"] if x["model"] == "qwen3.8:27b")
    assert m["tps"] == 100.0                          # rolling tok/s joined onto the live view
    assert any(m["model"] == "qwen3.8:27b" for m in d["models"])
    assert any(t["tool"] == "generate_music" for t in d["tools"])
    assert "loaded" in d
    assert isinstance(d["backends"], list)          # backend health for the Live tab
    assert isinstance(d["recent"], list)            # recent finished requests tail
    assert isinstance(d["loaded_detail"], list)     # per-model VRAM residency
    assert d["brain"]["model"] == "b"               # routing-brain block (per ACT_CONFIG)
    assert "loaded" in d["brain"] and "health" in d["brain"]


async def test_ollama_loaded_detail_parses_vram():
    from foundry_router.pool.protocols import OllamaProtocol

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [
                {"name": "qwen3.8:27b", "size_vram": 18_000_000_000, "size": 19_000_000_000},
                {"model": "gemma:2b", "size_vram": 2_000_000_000}]}

    class _Client:
        async def get(self, url, timeout=10):
            return _Resp()

    proto = OllamaProtocol("http://x", None, _Client())
    by = {d["model"]: d for d in await proto.loaded_models_detail()}
    assert by["qwen3.8:27b"]["size_vram"] == 18_000_000_000
    assert by["gemma:2b"]["size_vram"] == 2_000_000_000

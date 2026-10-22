"""Router start-up: the port opens before the slow MCP/agent/context work, a server
that never answers its handshake costs one capped wait (not minutes, not serial), and
/health reports the warm-up."""
import asyncio
import time

import pytest

from foundry_router.config import MCPServerConfig
from foundry_router.db import Database
from foundry_router.tools import mcp_client
from foundry_router.tools.mcp_client import MCPManager


def _servers(*names, timeout=300):
    return [MCPServerConfig(name=n, url=f"http://x/{n}", transport="sse",
                            timeout_seconds=timeout) for n in names]


async def test_list_all_runs_servers_concurrently_with_a_per_server_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_client, "LIST_TOOLS_TIMEOUT", 0.3)
    mgr = MCPManager(_servers("fast", "hung1", "hung2", "hung3"), Database(tmp_path / "d.sqlite"))

    async def fake_list_tools(name):
        if name.startswith("hung"):
            await asyncio.sleep(60)             # accepts TCP, never answers the handshake
        return [{"name": f"{name}_tool"}]
    monkeypatch.setattr(mgr, "list_tools", fake_list_tools)
    t0 = time.monotonic()
    out = await mgr.list_all()
    took = time.monotonic() - t0
    assert out == {"fast": [{"name": "fast_tool"}]}
    assert took < 1.5                           # three hung servers cost one cap, not three


async def test_list_all_cap_never_exceeds_the_servers_own_timeout(tmp_path, monkeypatch):
    mgr = MCPManager(_servers("short", timeout=1), Database(tmp_path / "d.sqlite"))

    async def slow(name):
        await asyncio.sleep(30)
    monkeypatch.setattr(mgr, "list_tools", slow)
    t0 = time.monotonic()
    assert await mgr.list_all() == {}
    assert time.monotonic() - t0 < 3        # capped at its own 1 s budget, not 20 s


async def test_list_all_skips_disabled_servers(tmp_path, monkeypatch):
    cfgs = _servers("on", "off")
    cfgs[1].enabled = False
    mgr = MCPManager(cfgs, Database(tmp_path / "d.sqlite"))
    seen = []

    async def fake(name):
        seen.append(name)
        return []
    monkeypatch.setattr(mgr, "list_tools", fake)
    assert set((await mgr.list_all()).keys()) == {"on"}
    assert seen == ["on"]


async def test_start_background_returns_before_the_slow_warmup(app, monkeypatch):
    svc = app.state.services
    gate = asyncio.Event()
    calls = []

    async def slow_probe():
        calls.append("probe")
        await gate.wait()
        return []

    async def slow_sync(pool):
        calls.append("sync")
        await gate.wait()

    async def ctx():
        calls.append("ctx")

    async def brain():
        calls.append("brain")
        return {}
    monkeypatch.setattr(svc.agents, "probe_all", slow_probe)
    monkeypatch.setattr(svc.tool_registry, "sync", slow_sync)
    monkeypatch.setattr(svc, "populate_context_lengths", ctx)
    monkeypatch.setattr(svc, "refresh_brain_health", brain)
    monkeypatch.setattr(svc.research, "start", lambda: calls.append("research"))
    t0 = time.monotonic()
    await svc.start_background()
    try:
        assert time.monotonic() - t0 < 5          # the port would be open by now
        assert svc.warmup["state"] == "running"
        await asyncio.sleep(0.05)
        assert calls == ["probe"]                  # warm-up is in progress, in the background
        gate.set()
        await asyncio.wait_for(svc._warmup_task, 5)
        assert svc.warmup["state"] == "done"
        assert set(svc.warmup["steps"]) == {"agent probe", "tool sync", "context lengths"}
        assert calls[-2:] == ["brain", "research"]
    finally:
        await svc.shutdown()


async def test_a_failing_or_stuck_warmup_step_does_not_stop_the_rest(app, monkeypatch):
    import foundry_router.main as M
    monkeypatch.setattr(M, "WARMUP_STEP_TIMEOUT", 0.2)
    svc = app.state.services

    async def boom():
        raise RuntimeError("probe broke")

    async def hang(pool):
        await asyncio.sleep(30)

    async def ctx():
        pass
    monkeypatch.setattr(svc.agents, "probe_all", boom)
    monkeypatch.setattr(svc.tool_registry, "sync", hang)
    monkeypatch.setattr(svc, "populate_context_lengths", ctx)
    monkeypatch.setattr(svc, "refresh_brain_health", lambda: asyncio.sleep(0))
    monkeypatch.setattr(svc.research, "start", lambda: None)
    await svc.start_background()
    try:
        await asyncio.wait_for(svc._warmup_task, 5)
        st = svc.warmup["steps"]
        assert st["agent probe"]["ok"] is False and st["tool sync"]["ok"] is False
        assert st["context lengths"]["ok"] is True and svc.warmup["state"] == "done"
    finally:
        await svc.shutdown()


def test_shutdown_cancels_an_unfinished_warmup(app, monkeypatch):
    svc = app.state.services

    async def go():
        async def hang(*a):
            await asyncio.sleep(60)
        monkeypatch.setattr(svc.agents, "probe_all", hang)
        await svc.start_background()
        task = svc._warmup_task
        await asyncio.sleep(0.05)
        await svc.shutdown()
        await asyncio.sleep(0.05)
        return task
    task = asyncio.run(go())
    assert task.cancelled()


def test_health_and_version_answer_without_credentials_while_warming(client):
    svc = client.app.state.services
    svc.warmup = {"state": "running", "steps": {"agent probe": {"ok": True}}}
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["warming_up"] is True and h["warmup"] == "running"
    assert "agent probe" in h["warmup_steps"]
    v = client.get("/api/version").json()
    assert "foundry_router" in v
    svc.warmup = {"state": "done", "steps": {}}
    assert client.get("/health").json()["warming_up"] is False

"""v0.181.156: Host Admin for every kind of backend."""
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from foundry_router import host_traffic
from foundry_router.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    now = datetime.now(timezone.utc)

    def add(backend, model, hours_ago, prompt, out, cached=0, ttft=500.0, eff=20.0, finish="stop"):
        ts = (now - timedelta(hours=hours_ago, minutes=1)).replace(tzinfo=None).isoformat()
        db.execute("INSERT INTO perf_samples (ts, model, backend, prompt_tokens, completion_tokens, cached_tokens, "
                   "ttft_ms, eff_tps, wall_ms, finish_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (ts, model, backend, prompt, out, cached, ttft, eff, 2000, finish))
    add("strata", "qwen", 0, 1000, 100, cached=800)
    add("strata", "qwen", 0, 1000, 300, ttft=1500.0, eff=40.0, finish="length")
    add("strata", "other", 5, 10, 10)
    add("strata", "qwen", 30, 9, 9)                 # outside the window
    add("meridian", "sonnet", 1, 50, 50)
    return db


def test_the_routers_own_calls_to_one_backend(tmp_path):
    t = host_traffic.traffic(_db(tmp_path), "strata", 24)
    assert t["calls"] == 3 and t["prompt_tokens"] == 2010 and t["completion_tokens"] == 410
    assert t["cached_tokens"] == 800 and t["cut_short"] == 1
    assert t["ttft_ms"] == 833 and t["eff_tps"] == 26.7
    assert len(t["buckets"]) == 24 and t["buckets"][-1]["hour"].endswith(":00:00Z")
    assert sum(b["calls"] for b in t["buckets"]) == 3 and t["buckets"][0]["calls"] == 0
    assert [(m["model"], m["calls"]) for m in t["models"]] == [("qwen", 2), ("other", 1)]
    empty = host_traffic.traffic(_db(tmp_path / "b"), "nobody", 24) if (tmp_path / "b").mkdir() is None else None
    assert empty["calls"] == 0 and empty["models"] == [] and len(empty["buckets"]) == 24
    assert host_traffic.traffic(_db(tmp_path / "c"), "strata", 10 ** 6)["hours"] == 24 * 14 \
        if (tmp_path / "c").mkdir() is None else False


@pytest.mark.asyncio
async def test_ollama_reports_what_is_loaded(tmp_path):
    from tests.test_ollama_admin import _Pool
    from foundry_router.pool.ollama_admin import OllamaAdmin

    def handler(request):
        assert request.url.path == "/api/ps"
        return httpx.Response(200, json={"models": [
            {"name": "llama3:8b", "size": 6_000_000_000, "size_vram": 4_500_000_000, "context_length": 8192,
             "expires_at": "2026-11-15T16:00:00Z", "details": {"parameter_size": "8B", "quantization_level": "Q4_0"}},
            {"size": 1}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        adm = OllamaAdmin(c, _Pool(), Database(str(tmp_path / "o.db")))
        assert await adm.running("truenas") == [{
            "name": "llama3:8b", "size": 6_000_000_000, "size_vram": 4_500_000_000, "context_length": 8192,
            "expires_at": "2026-11-15T16:00:00Z", "parameter_size": "8B", "quantization_level": "Q4_0"}]


def test_routes_and_page():
    from foundry_router.ui import routes
    src = inspect.getsource(routes)
    assert '@router.get("/admin/api/host/traffic")' in src and "host_traffic.traffic, svc.db, backend, hours" in src
    assert '"loaded": [m["name"] for m in running], "running": running' in src
    html = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    for needle in ("function renderHostTraffic(t) {", "async function loadHostTraffic(name) {",
                   "function renderExpertTiers(t) {", 'id="host-traffic"', 'id="host-auto"',
                   'localStorage.getItem("fr.hostAuto")', "Loaded now <span", "loadHostTraffic(be);",
                   "else if (h && h.ok && isObj(body.tiers)) {", 'el.querySelector("details[open]")'):
        assert needle in html, needle
    assert html.count('<div id="host-traffic"></div>') == 2            # the Ollama page and every other

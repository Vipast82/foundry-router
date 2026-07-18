"""Brain health surface: the brain runs on EVERY request but had no health
indicator like the backends do, so an outage silently degraded every request to
the static fallback rule with no visible signal. A cheap GET-based probe reports
reachable + whether the configured model is actually present."""

import httpx
import pytest

from foundry_router.brain.client import BrainClient
from foundry_router.config import AgentBrainConfig


class _FakeProto:
    def __init__(self, models=None, raise_exc=None):
        self._models = models or []
        self._raise = raise_exc

    async def list_models(self):
        if self._raise:
            raise self._raise
        return self._models


def _brain(models=None, raise_exc=None, model="ornith-brain:latest"):
    b = BrainClient(AgentBrainConfig(provider="ollama", endpoint="http://x",
                                     model=model), client=httpx.AsyncClient())
    b.protocol = _FakeProto(models, raise_exc)
    return b


async def test_health_up_with_model_present():
    h = await _brain(models=["ornith-brain:latest", "qwen:7b"]).health()
    assert h["healthy"] is True
    assert h["model_present"] is True
    assert h["error"] == "" and h["model"] == "ornith-brain:latest"


async def test_health_up_but_model_missing():
    # endpoint reachable, configured model not pulled — a distinct failure
    h = await _brain(models=["some-other:latest"]).health()
    assert h["healthy"] is True and h["model_present"] is False


async def test_health_down_when_unreachable():
    h = await _brain(raise_exc=httpx.ConnectError("connection refused")).health()
    assert h["healthy"] is False
    assert h["model_present"] is None
    assert "refus" in h["error"].lower()   # unwrapped, readable


async def test_health_never_raises_on_weird_error():
    h = await _brain(raise_exc=RuntimeError("boom")).health()
    assert h["healthy"] is False and "boom" in h["error"]


# -- endpoints (via the app) ------------------------------------------------------

def test_status_exposes_brain_health(client):
    st = client.get("/admin/api/status").json()
    assert "health" in st["brain"]
    assert st["brain"]["provider"] and st["brain"]["model"]


def test_brain_health_endpoint_probes(client):
    r = client.get("/admin/api/brain/health")
    assert r.status_code == 200
    body = r.json()
    assert "healthy" in body and "checked_at" in body

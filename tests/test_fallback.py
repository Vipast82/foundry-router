"""Brain-unreachable degradation (design doc §4.2): the static fallback rule,
unit-level and end-to-end through the facade with a genuinely dead brain
endpoint — the doc says to test this by actually stopping the brain, and the
conftest config does exactly that (endpoint nothing listens on)."""

import json

from foundry_router.brain.fallback import guess_category, pick_fallback_model
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry


class FakePool:
    def __init__(self, models):  # {model_id: backend_type}
        self._models = models

    def available_models(self):
        return {m: ["b"] for m in self._models}

    def backend_info(self, model):
        t = self._models.get(model)
        return {"name": "b", "type": t, "url": "http://x"} if t else None


def test_guess_category():
    assert guess_category("def foo():\n    pass") == "coding"
    assert guess_category("what's the capital of France?") == "general_chat"


def test_fallback_prefers_local(tmp_path):
    db = Database(tmp_path / "f.sqlite")
    registry = ModelRegistry(db)
    pool = FakePool({"local-model": "ollama", "claude-sonnet": "anthropic-compatible"})
    # even with Claude scoring higher, the conservative static rule stays local
    registry.upsert_benchmark("claude-sonnet", "coding", 95, "measured", "independent",
                              confidence=0.9)
    registry.upsert_benchmark("local-model", "coding", 60, "estimated", "community_report",
                              confidence=0.4)
    picked = pick_fallback_model(pool, registry,
                                 {"benchmark_category": "coding"}, "fix this def foo()")
    assert picked == "local-model"


def test_fallback_uses_remote_only_when_nothing_local(tmp_path):
    db = Database(tmp_path / "f2.sqlite")
    registry = ModelRegistry(db)
    pool = FakePool({"claude-sonnet": "anthropic-compatible"})
    assert pick_fallback_model(pool, registry, None, "hello") == "claude-sonnet"


def test_fallback_none_when_nothing_reachable(tmp_path):
    db = Database(tmp_path / "f3.sqlite")
    registry = ModelRegistry(db)
    assert pick_fallback_model(FakePool({}), registry, None, "hello") is None


def test_e2e_brain_unreachable_degrades_not_fails(client):
    """Full request path with a dead brain and zero backends: the request must
    still return 200 with the explanation in Ollama's NATIVE thinking field
    (never literal <think> tags in content — clients render those as raw
    text) and never a 500 (§4.2: degrade, not fail)."""
    r = client.post("/api/chat", json={
        "model": "Foundry-Chat", "stream": False,
        "messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200
    msg = r.json()["message"]
    assert "unreachable" in msg["thinking"].lower()
    assert "<think>" not in msg["content"]         # the live formatting bug
    assert "reachable" in msg["content"].lower()   # clean user-facing text


def test_e2e_brain_unreachable_streaming(client):
    r = client.post("/api/chat", json={
        "model": "Foundry-Chat", "stream": True,
        "messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200
    lines = [json.loads(l) for l in r.text.strip().splitlines()]
    assert lines[-1]["done"] is True
    thinking = "".join(l["message"].get("thinking") or ""
                       for l in lines if not l["done"])
    content = "".join(l["message"].get("content") or ""
                      for l in lines if not l["done"])
    assert "unreachable" in thinking.lower()   # narration in the native field
    assert "<think>" not in content            # never raw tags in content
    # thinking-typed chunks carry no content and vice versa
    for line in lines:
        if not line["done"] and line["message"].get("thinking"):
            assert line["message"]["content"] == ""

"""Persona expansion (spec §3/§4/§5), media artifact forwarding + MCP timeout
(spec §6 shape tests), and the literal-extraction research fix (spec §7)."""

import asyncio
import json
from contextlib import asynccontextmanager

from foundry_router.brain.agent import _filter_required_tags
from foundry_router.config import MCPServerConfig
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.research_agent import measured_score_in_text
from foundry_router.tools.mcp_client import MCPManager


# -- §3/§5 seeds and clone --------------------------------------------------------

def test_new_personas_advertised(client):
    tags = {m["name"] for m in client.get("/api/tags").json()["models"]}
    assert {"Foundry-Vision", "Foundry-Creative", "Foundry-Agent"} <= tags


def test_seed_v2_upgrades(client):
    personas = {p["virtual_name"]: p
                for p in client.get("/admin/api/personas").json()["personas"]}
    assert personas["Foundry-Coding"]["execution_mode"] == "pipeline"
    for name in ("Foundry-RAG", "Foundry-Research"):
        assert personas[name]["local_bias_strength"] == "strong"
        assert personas[name]["outcome_judge"] == "local_large"
    assert json.loads(personas["Foundry-Vision"]["required_tags"]) == ["vision"]
    assert personas["Foundry-Creative"]["prefer_permissive"] == 1


def test_clone_persona(client):
    r = client.post("/admin/api/personas/clone",
                    json={"source": "Foundry-Creative", "new_name": "Foundry-Creative-SFW"})
    assert r.status_code == 200
    clone = r.json()["persona"]
    assert clone["prefer_permissive"] == 1                    # settings copied
    assert "clone of Foundry-Creative" in clone["description"]
    # name collision refused
    r2 = client.post("/admin/api/personas/clone",
                     json={"source": "Foundry-Chat", "new_name": "Foundry-Creative-SFW"})
    assert r2.status_code == 409


# -- §4/§5 candidate shaping --------------------------------------------------------

def _row(mid, tags=None, policy=None):
    return {"id": mid, "tags": json.dumps(tags or []), "content_policy": policy,
            "relative_cost_tier": "free", "score": None}


def test_required_tags_filters_when_matches_exist():
    persona = {"required_tags": json.dumps(["vision"])}
    ranked = [_row("text-model"), _row("llava", tags=["vision"])]
    out = _filter_required_tags(ranked, persona)
    assert [r["id"] for r in out] == ["llava"]


def test_required_tags_degrades_to_full_list_when_nothing_matches():
    persona = {"required_tags": json.dumps(["vision"])}
    ranked = [_row("text-a"), _row("text-b")]
    assert _filter_required_tags(ranked, persona) == ranked


def test_permissive_avoided_by_default_preferred_only_when_asked(tmp_path):
    """The operator rule: uncensored/abliterated models exist for content other
    models refuse — a normal persona must NOT pick one over a standard model
    just because it scores higher, and a permissive persona should."""
    reg = ModelRegistry(Database(tmp_path / "perm.sqlite"))
    reg.upsert_auto("standard", source="discovery", relative_cost_tier="free")
    reg.upsert_auto("wild", source="discovery", relative_cost_tier="free",
                    content_policy="permissive")
    # the permissive model even scores HIGHER on the category
    reg.upsert_benchmark("standard", "general_chat", 60, "estimated",
                         "community_report", confidence=0.5)
    reg.upsert_benchmark("wild", "general_chat", 95, "estimated",
                         "community_report", confidence=0.5)
    order = lambda mode: [r["id"] for r in reg.ranked_for_category(
        "general_chat", ["standard", "wild"], permissive_mode=mode)]
    assert order("avoid") == ["standard", "wild"]    # normal persona: standard wins
    assert order("prefer") == ["wild", "standard"]   # permissive persona: wild first
    assert order("neutral") == ["wild", "standard"]  # pure score: higher wins


# -- §6 media artifact forwarding + per-server timeout --------------------------------

async def test_mcp_artifact_url_forwards_via_use_last_result(tmp_path):
    """Media tools return a URL/artifact reference — the existing
    use_last_result forwarding must deliver it verbatim."""
    from foundry_router.brain.agent import AgentRunner, RequestContext
    from foundry_router.config import AgentBrainConfig, GuardrailsConfig, MeridianConfig
    from foundry_router.guardrails import GuardrailEngine, RequestGuardState
    from foundry_router.pool.protocols import ChatResult
    from foundry_router.registry.models_db import ModelRegistry
    from foundry_router.tools.sync import ToolDef, ToolRegistry
    from foundry_router.usage import MeridianUsage, RequestLogger

    URL = "http://comfy.local/artifacts/img_8412.png"

    class FakeMCP:
        servers = {"comfyui": None}
        async def call_tool(self, server, tool, args):
            return f"Image generated: {URL}"
        async def list_all(self):
            return {}

    class EmptyPool:
        def available_models(self):
            return {}
        def backend_info(self, m):
            return None

    class ScriptedBrain:
        def __init__(self):
            self.cfg = AgentBrainConfig()
            self.responses = [
                ChatResult(tool_calls=[{"id": "1", "name": "generate_image",
                                        "arguments": {"prompt": "a fox"}}]),
                ChatResult(tool_calls=[{"id": "2", "name": "return_to_user",
                                        "arguments": {"use_last_result": True}}]),
            ]
        async def chat(self, messages, tools=None, **kwargs):
            return self.responses.pop(0)
        async def complete(self, prompt):
            return ""

    db = Database(tmp_path / "m.sqlite")
    registry = ModelRegistry(db)
    tool_registry = ToolRegistry(db, registry, FakeMCP())
    tool_registry.tools["generate_image"] = ToolDef(
        name="generate_image", kind="mcp", description="[MCP:comfyui] gen",
        parameters={"type": "object", "properties": {}},
        server="comfyui", mcp_tool="generate_image")
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    runner = AgentRunner(ScriptedBrain(), EmptyPool(), tool_registry, registry,
                         GuardrailEngine(GuardrailsConfig(), db, meridian), meridian)
    ctx = RequestContext(
        persona={"virtual_name": "Foundry-Chat"},
        messages=[{"role": "user", "content": "draw me a fox"}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "agent", "draw"))
    events = [ev async for ev in runner.run(ctx)]
    answers = [ev for ev in events if ev.kind == "answer"]
    assert URL in answers[0].text


async def test_mcp_per_server_timeout(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    mgr = MCPManager([MCPServerConfig(name="slow", url="http://x/mcp",
                                      timeout_seconds=1)], db)

    class SlowSession:
        async def call_tool(self, tool, args):
            await asyncio.sleep(5)

    @asynccontextmanager
    async def fake_session(name):
        yield SlowSession()

    mgr._session = fake_session
    try:
        await mgr.call_tool("slow", "generate", {})
        assert False, "should have timed out"
    except RuntimeError as e:
        assert "timed out after 1s" in str(e)


# -- §7 literal-number extraction --------------------------------------------------

def test_measured_score_verification():
    corpus = "LiveCodeBench for this model is consistently reported at 57.2%."
    assert measured_score_in_text(57.2, corpus) is True
    # the confirmed real synthesis error: 45.3 appears nowhere in the sources
    assert measured_score_in_text(45.3, corpus) is False
    assert measured_score_in_text(57.0, "scored 57 across runs") is True

"""Regression tests for image attachments (found live: a real AnythingLLM
photo was silently dropped by _canonical_messages, the brain routed to a
text model, and the user was asked to upload the picture they'd already sent).

The chain, each link tested: facade preserves images -> sanitize keeps
image-only messages -> the brain sees a marker but NEVER the bytes ->
candidates steer to vision-tagged models -> delivery via include_images flag
or vision auto-attach -> each wire protocol translates to its native format.
"""

import json

from foundry_router.brain import prompts
from foundry_router.brain.agent import (AgentRunner, RequestContext,
                                        _steer_vision_when_images)
from foundry_router.config import AgentBrainConfig, GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.facade.ollama_api import _canonical_messages
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.pool.protocols import (AnthropicProtocol, ChatResult,
                                           OllamaProtocol, OpenAIProtocol,
                                           _image_media_type)
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import _ASK_PARAMS, ToolDef, ToolRegistry
from foundry_router.usage import MeridianUsage, RequestLogger

PNG_B64 = "iVBORw0KGgoAAAANSUhEUg_fake_png_payload"
JPG_B64 = "/9j/4AAQSkZJRg_fake_jpeg_payload"


# -- facade entry point ----------------------------------------------------------

def test_canonical_messages_preserves_images():
    out = _canonical_messages([
        {"role": "user", "content": "what is this?", "images": [PNG_B64]}])
    assert out[0]["images"] == [PNG_B64]


def test_sanitize_history_keeps_image_only_messages():
    out = prompts.sanitize_history([
        {"role": "user", "content": "", "images": [PNG_B64]}])
    assert len(out) == 1 and out[0]["images"] == [PNG_B64]


# -- media-type sniffing -----------------------------------------------------------

def test_media_type_from_magic_bytes():
    assert _image_media_type(PNG_B64) == "image/png"
    assert _image_media_type(JPG_B64) == "image/jpeg"
    assert _image_media_type("unknownprefix") == "image/jpeg"


# -- wire protocols -----------------------------------------------------------------

class FakeResp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class CaptureClient:
    def __init__(self, response_data):
        self.payload = None
        self._data = response_data

    async def post(self, url, json=None, headers=None):
        self.payload = json
        return FakeResp(self._data)


def test_ollama_payload_carries_images():
    proto = OllamaProtocol("http://x", None, None)
    payload = proto._payload("llava", [{"role": "user", "content": "what is this?",
                                        "images": [PNG_B64]}],
                             None, None, None, stream=False)
    assert payload["messages"][0]["images"] == [PNG_B64]


async def test_anthropic_converts_to_base64_blocks():
    client = CaptureClient({"content": [{"type": "text", "text": "a cat"}],
                            "usage": {"input_tokens": 1, "output_tokens": 1}})
    proto = AnthropicProtocol("http://m", "key", client)
    await proto.chat("claude-sonnet-4-6",
                     [{"role": "user", "content": "what is this?",
                       "images": [PNG_B64]}])
    blocks = client.payload["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"] == {"type": "base64", "media_type": "image/png",
                                   "data": PNG_B64}
    assert blocks[1] == {"type": "text", "text": "what is this?"}


async def test_openai_converts_to_data_uri_parts():
    client = CaptureClient({"choices": [{"message": {"content": "a cat"}}],
                            "usage": {}})
    proto = OpenAIProtocol("http://o/v1", "key", client)
    await proto.chat("gpt-5", [{"role": "user", "content": "what is this?",
                                "images": [JPG_B64]}])
    parts = client.payload["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "what is this?"}
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,/9j/")


# -- brain view + steering ------------------------------------------------------------

class VisionPool:
    def __init__(self):
        self.messages_seen: list = []

    def available_models(self):
        return {"llava:13b": ["b"], "text-model": ["b"]}

    def backend_info(self, m):
        return {"name": "b", "type": "ollama", "url": "http://x", "api_key": None}

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        self.messages_seen.append((model, messages[-1]))
        return ChatResult(content="It is a cat.", prompt_tokens=5,
                          completion_tokens=5), "b"


class ScriptedBrain:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []
        self.cfg = AgentBrainConfig()

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return self.responses.pop(0)

    async def complete(self, prompt):
        return ""


def _make(tmp_path, brain_responses):
    db = Database(tmp_path / "v.sqlite")
    registry = ModelRegistry(db)
    registry.upsert_auto("llava:13b", source="discovery", relative_cost_tier="free",
                         tags=json.dumps(["vision"]))
    registry.upsert_auto("text-model", source="discovery", relative_cost_tier="free")
    tool_registry = ToolRegistry(db, registry, MCPManager([], db))
    for m in ("llava:13b", "text-model"):
        name = "ask_" + m.replace(":", "_").replace("-", "_").replace(".", "_")
        tool_registry.tools[name] = ToolDef(name=name, kind="model", description="",
                                            model_id=m, parameters=_ASK_PARAMS)
    pool = VisionPool()
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    brain = ScriptedBrain(brain_responses)
    runner = AgentRunner(brain, pool, tool_registry, registry,
                         GuardrailEngine(GuardrailsConfig(), db, meridian), meridian)
    ctx = RequestContext(
        persona={"virtual_name": "Foundry-Chat", "benchmark_category": "general_chat"},
        messages=[{"role": "user", "content": "Identify what this is in the picture",
                   "images": [PNG_B64]}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "agent", "identify"))
    return runner, ctx, brain, pool


async def test_brain_sees_marker_never_bytes(tmp_path):
    runner, ctx, brain, pool = _make(tmp_path, [
        ChatResult(content="I need an image to answer that.")])  # any reply ends it
    [ev async for ev in runner.run(ctx)]
    user_view = next(m for m in brain.calls[0] if m["role"] == "user")
    assert PNG_B64 not in user_view["content"]
    assert "images" not in user_view
    assert "[ATTACHED: 1 image(s)" in user_view["content"]
    assert "include_images" in user_view["content"]
    assert ctx.messages[0]["images"] == [PNG_B64]  # originals intact for workers


def test_vision_steering_filters_candidates_dynamically():
    ranked = [{"id": "text-model", "tags": "[]"},
              {"id": "llava:13b", "tags": json.dumps(["vision"])}]
    msgs = [{"role": "user", "content": "x", "images": [PNG_B64]}]
    assert [r["id"] for r in _steer_vision_when_images(ranked, msgs)] == ["llava:13b"]
    # no image -> untouched; no vision candidate -> degrade to full list
    assert _steer_vision_when_images(ranked, [{"role": "user", "content": "x"}]) == ranked
    text_only = [{"id": "text-model", "tags": "[]"}]
    assert _steer_vision_when_images(text_only, msgs) == text_only


async def test_images_delivered_via_flag(tmp_path):
    runner, ctx, brain, pool = _make(tmp_path, [
        ChatResult(tool_calls=[{"id": "1", "name": "ask_text_model",
                                "arguments": {"prompt": "describe the image",
                                              "include_images": True}}]),
        ChatResult(tool_calls=[{"id": "2", "name": "return_to_user",
                                "arguments": {"use_last_result": True}}]),
    ])
    events = [ev async for ev in runner.run(ctx)]
    model, message = pool.messages_seen[0]
    assert message.get("images") == [PNG_B64]      # explicit flag delivers
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "It is a cat."


async def test_vision_tagged_worker_gets_images_automatically(tmp_path):
    # brain FORGETS the flag — the vision-tagged worker still gets the photo
    runner, ctx, brain, pool = _make(tmp_path, [
        ChatResult(tool_calls=[{"id": "1", "name": "ask_llava_13b",
                                "arguments": {"prompt": "describe the image"}}]),
        ChatResult(tool_calls=[{"id": "2", "name": "return_to_user",
                                "arguments": {"use_last_result": True}}]),
    ])
    events = [ev async for ev in runner.run(ctx)]
    model, message = pool.messages_seen[0]
    assert model == "llava:13b"
    assert message.get("images") == [PNG_B64]
    thinks = " | ".join(ev.text for ev in events if ev.kind == "think")
    assert "Attaching the user's 1 image(s)" in thinks


async def test_untagged_worker_without_flag_gets_no_images(tmp_path):
    runner, ctx, brain, pool = _make(tmp_path, [
        ChatResult(tool_calls=[{"id": "1", "name": "ask_text_model",
                                "arguments": {"prompt": "just summarize the caption"}}]),
        ChatResult(tool_calls=[{"id": "2", "name": "return_to_user",
                                "arguments": {"use_last_result": True}}]),
    ])
    [ev async for ev in runner.run(ctx)]
    model, message = pool.messages_seen[0]
    assert "images" not in message  # no flag + not vision-tagged = text only

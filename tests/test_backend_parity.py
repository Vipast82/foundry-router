"""Cross-backend capability parity: whatever a client sends (reasoning, the full
sampling knob set, structured output) must reach EACH backend in the shape that
backend understands, and each backend's reply (content, tool calls, reasoning,
usage) must come back normalized — streaming and non-streaming alike. An httpx
MockTransport captures the wire body and returns canned responses so we assert on
exactly what Foundry puts on the wire and pulls back off it."""

import json

import httpx
import pytest

from foundry_router import thinking
from foundry_router.pool.protocols import (AnthropicProtocol, OpenAIProtocol,
                                           make_protocol)

_SEEN = []          # captured request bodies (most recent last)


def _collect(handler):
    def _h(request):
        try:
            _SEEN.append(json.loads(request.content))
        except Exception:
            _SEEN.append(None)
        return handler(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(_h))


async def _drain(agen):
    return [c async for c in agen]


# =============================================================================
# thinking helpers — the mapping every openai-dialect backend relies on
# =============================================================================

def test_openai_reasoning_effort_mapping():
    assert thinking.openai_reasoning_effort("high") == "high"
    assert thinking.openai_reasoning_effort("max") == "high"     # no OpenAI "max"
    assert thinking.openai_reasoning_effort(True) == "medium"    # bare on
    assert thinking.openai_reasoning_effort("low") == "low"
    assert thinking.openai_reasoning_effort(False) is None
    assert thinking.openai_reasoning_effort(None) is None
    assert thinking.openai_reasoning_effort("off") is None


def test_supported_levels_openai_family_gated():
    # openai-dialect: inferred from model family (no capability discovery)
    assert thinking.supported_levels("gpt-oss:20b", None,
                                     backend_type="openai-compatible") == \
        ["off", "low", "medium", "high"]
    assert thinking.supported_levels("qwen3.8:27b", None,
                                     backend_type="openai-compatible") == \
        ["off", "low", "medium", "high", "max"]
    # a non-reasoning model on an openai endpoint gets NO menu (so we never send
    # reasoning_effort to something that would reject it)
    assert thinking.supported_levels("gpt-4o", None,
                                     backend_type="openai-compatible") == []


# =============================================================================
# OpenAIProtocol — reasoning, full sampling (flavor-gated), structured output
# =============================================================================

def _openai_json(request):
    return httpx.Response(200, json={
        "choices": [{"message": {
            "content": "hello",
            "reasoning_content": "let me think",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "get_weather",
                                         "arguments": '{"city": "NYC"}'}}]}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7}})


async def test_openai_reasoning_effort_on_the_wire():
    _SEEN.clear()
    proto = OpenAIProtocol("http://x", None, _collect(_openai_json), flavor="llamacpp")
    await proto.chat("gpt-oss:20b", [{"role": "user", "content": "hi"}], think="max")
    assert _SEEN[-1]["reasoning_effort"] == "high"      # max -> high on the wire


async def test_openai_no_reasoning_when_off():
    _SEEN.clear()
    proto = OpenAIProtocol("http://x", None, _collect(_openai_json), flavor="llamacpp")
    await proto.chat("gpt-oss:20b", [{"role": "user", "content": "hi"}], think=None)
    assert "reasoning_effort" not in _SEEN[-1]


async def test_openai_response_surfaces_reasoning_and_tools():
    proto = OpenAIProtocol("http://x", None, _collect(_openai_json), flavor="vllm")
    res = await proto.chat("m", [{"role": "user", "content": "weather?"}])
    assert res.content == "hello"
    assert res.thinking == "let me think"                # reasoning_content -> thinking
    assert res.tool_calls[0]["name"] == "get_weather"
    assert res.tool_calls[0]["arguments"] == {"city": "NYC"}
    assert res.prompt_tokens == 11 and res.completion_tokens == 7


async def test_openai_local_flavor_forwards_full_sampling():
    _SEEN.clear()
    proto = OpenAIProtocol("http://x", None, _collect(_openai_json), flavor="llamacpp")
    opts = {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "min_p": 0.05,
            "repeat_penalty": 1.1, "seed": 42, "num_predict": 256}
    await proto.chat("m", [{"role": "user", "content": "hi"}], options=opts)
    p = _SEEN[-1]
    assert p["temperature"] == 0.7 and p["top_p"] == 0.9 and p["seed"] == 42
    assert p["top_k"] == 40 and p["min_p"] == 0.05 and p["repeat_penalty"] == 1.1
    assert p["max_tokens"] == 256                        # num_predict -> max_tokens


async def test_openai_strict_flavor_drops_nonstandard_sampling():
    _SEEN.clear()
    # flavor "openai" (or unset) = strict endpoint: no top_k/min_p/repeat_penalty
    proto = OpenAIProtocol("http://x", None, _collect(_openai_json), flavor="openai")
    opts = {"temperature": 0.7, "top_k": 40, "min_p": 0.05, "repeat_penalty": 1.1}
    await proto.chat("m", [{"role": "user", "content": "hi"}], options=opts)
    p = _SEEN[-1]
    assert p["temperature"] == 0.7                       # standard kept
    assert "top_k" not in p and "min_p" not in p and "repeat_penalty" not in p


async def test_openai_structured_output_response_format():
    _SEEN.clear()
    proto = OpenAIProtocol("http://x", None, _collect(_openai_json))
    await proto.chat("m", [{"role": "user", "content": "hi"}], fmt="json")
    assert _SEEN[-1]["response_format"] == {"type": "json_object"}
    _SEEN.clear()
    schema = {"name": "s", "schema": {"type": "object"}}
    await proto.chat("m", [{"role": "user", "content": "hi"}], fmt=schema)
    assert _SEEN[-1]["response_format"] == {"type": "json_schema", "json_schema": schema}


# -- OpenAI streaming: deltas + accumulated tool calls + usage ---------------------

_OAI_STREAM = (
    'data: {"choices":[{"delta":{"content":"Hel"}}]}\n'
    'data: {"choices":[{"delta":{"content":"lo"}}]}\n'
    'data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}\n'
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
    '"function":{"name":"get_weather","arguments":"{\\"city\\":"}}]}}]}\n'
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":"\\"NYC\\"}"}}]}}]}\n'
    'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":9,"completion_tokens":4}}\n'
    'data: [DONE]\n')


async def test_openai_stream_content_reasoning_tools_usage():
    proto = OpenAIProtocol("http://x", None,
                           _collect(lambda req: httpx.Response(200, text=_OAI_STREAM)),
                           flavor="llamacpp")
    chunks = await _drain(proto.chat_stream("m", [{"role": "user", "content": "hi"}],
                                            tools=[{"type": "function",
                                                    "function": {"name": "get_weather"}}]))
    content = "".join(c.get("content", "") for c in chunks)
    thinking_txt = "".join(c.get("thinking", "") for c in chunks)
    done = chunks[-1]
    assert content == "Hello" and thinking_txt == "hmm"
    assert done["done"] is True
    assert done["prompt_tokens"] == 9 and done["completion_tokens"] == 4
    assert done["tool_calls"][0]["name"] == "get_weather"
    assert done["tool_calls"][0]["arguments"] == {"city": "NYC"}   # fragments reassembled


async def test_openai_stream_requests_stream_and_usage():
    _SEEN.clear()
    proto = OpenAIProtocol("http://x", None,
                           _collect(lambda req: httpx.Response(200, text=_OAI_STREAM)))
    await _drain(proto.chat_stream("m", [{"role": "user", "content": "hi"}]))
    assert _SEEN[-1]["stream"] is True
    assert _SEEN[-1]["stream_options"] == {"include_usage": True}


# =============================================================================
# AnthropicProtocol — structured-output nudge + real streaming
# =============================================================================

def _anthropic_json(request):
    return httpx.Response(200, json={
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 3, "output_tokens": 2}})


async def test_anthropic_fmt_json_adds_system_nudge():
    _SEEN.clear()
    proto = AnthropicProtocol("http://m", "k", _collect(_anthropic_json))
    await proto.chat("claude-opus-5", [{"role": "user", "content": "hi"}], fmt="json")
    assert "valid JSON" in _SEEN[-1]["system"]


async def test_anthropic_fmt_schema_includes_schema():
    _SEEN.clear()
    proto = AnthropicProtocol("http://m", "k", _collect(_anthropic_json))
    await proto.chat("claude-opus-5", [{"role": "user", "content": "hi"}],
                     fmt={"type": "object", "properties": {"n": {"type": "integer"}}})
    sys = _SEEN[-1]["system"]
    assert "valid JSON" in sys and '"properties"' in sys


_ANTHROPIC_STREAM = (
    'data: {"type":"message_start","message":{"usage":{"input_tokens":7}}}\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi "}}\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"reasoning"}}\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"there"}}\n'
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tu_1","name":"lookup"}}\n'
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"q\\":"}}\n'
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"x\\"}"}}\n'
    'data: {"type":"message_delta","usage":{"output_tokens":12}}\n'
    'data: {"type":"message_stop"}\n')


async def test_anthropic_stream_text_thinking_tools_usage():
    proto = AnthropicProtocol("http://m", "k",
                              _collect(lambda req: httpx.Response(200, text=_ANTHROPIC_STREAM)))
    chunks = await _drain(proto.chat_stream("claude-opus-5",
                                            [{"role": "user", "content": "hi"}]))
    content = "".join(c.get("content", "") for c in chunks)
    thinking_txt = "".join(c.get("thinking", "") for c in chunks)
    done = chunks[-1]
    assert content == "Hi there" and thinking_txt == "reasoning"
    assert done["done"] is True
    assert done["prompt_tokens"] == 7 and done["completion_tokens"] == 12
    assert done["tool_calls"][0]["name"] == "lookup"
    assert done["tool_calls"][0]["arguments"] == {"q": "x"}


async def test_anthropic_stream_sends_stream_flag():
    _SEEN.clear()
    proto = AnthropicProtocol("http://m", "k",
                              _collect(lambda req: httpx.Response(200, text=_ANTHROPIC_STREAM)))
    await _drain(proto.chat_stream("claude-opus-5", [{"role": "user", "content": "hi"}],
                                   think="high"))
    assert _SEEN[-1]["stream"] is True
    assert _SEEN[-1]["thinking"]["type"] == "enabled"    # thinking still carried in stream


# =============================================================================
# make_protocol threads flavor
# =============================================================================

def test_make_protocol_threads_flavor():
    p = make_protocol("openai-compatible", "http://x", None, None, flavor="unsloth")
    assert isinstance(p, OpenAIProtocol) and p.flavor == "unsloth"
    p2 = make_protocol("ollama", "http://y", None, None)
    assert p2.flavor is None

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


def _llamacpp_spec_json(request):
    """A llama.cpp response with speculative-decoding timings and a KV
    prefix-cache hit reported in usage.prompt_tokens_details."""
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 8,
                  "prompt_tokens_details": {"cached_tokens": 90}},
        "timings": {"prompt_n": 100, "prompt_ms": 50.0,
                    "predicted_n": 8, "predicted_ms": 40.0,
                    "draft_n": 7, "draft_n_accepted": 5}})


async def test_openai_surfaces_spec_decode_and_cache():
    proto = OpenAIProtocol("http://x", None, _collect(_llamacpp_spec_json),
                           flavor="llamacpp")
    res = await proto.chat("m", [{"role": "user", "content": "hi"}])
    assert res.draft_n == 7 and res.draft_n_accepted == 5   # spec acceptance
    assert res.cached_tokens == 90                           # KV prefix-cache hit


_SPEC_STREAM = (
    'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":100,"completion_tokens":8,'
    '"prompt_tokens_details":{"cached_tokens":90}},'
    '"timings":{"prompt_n":100,"prompt_ms":50.0,"predicted_n":8,'
    '"predicted_ms":40.0,"draft_n":7,"draft_n_accepted":5}}\n'
    'data: [DONE]\n')


async def test_openai_stream_carries_spec_and_cache_on_done():
    proto = OpenAIProtocol("http://x", None,
                           _collect(lambda req: httpx.Response(200, text=_SPEC_STREAM)),
                           flavor="llamacpp")
    chunks = await _drain(proto.chat_stream("m", [{"role": "user", "content": "hi"}]))
    done = chunks[-1]
    assert done["done"] is True
    assert done["draft_n"] == 7 and done["draft_n_accepted"] == 5
    assert done["cached_tokens"] == 90


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


async def test_openai_off_disables_thinking_via_template_kwarg_local():
    # Qwen3/DeepSeek on llama.cpp think by default; reasoning_effort has no "off",
    # so OFF must go out as the chat-template kwarg the local runners honor. This
    # is what makes ACT-on-llama.cpp actually fast (parity with Ollama think:false).
    _SEEN.clear()
    proto = OpenAIProtocol("http://x", None, _collect(_openai_json), flavor="llamacpp")
    await proto.chat("qwen3.8:27b", [{"role": "user", "content": "hi"}], think=False)
    assert _SEEN[-1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in _SEEN[-1]


async def test_openai_off_not_sent_to_strict_openai():
    # A strict OpenAI endpoint would 400 on chat_template_kwargs, so OFF sends
    # nothing there.
    _SEEN.clear()
    proto = OpenAIProtocol("http://x", None, _collect(_openai_json), flavor="openai")
    await proto.chat("gpt-4o", [{"role": "user", "content": "hi"}], think=False)
    assert "chat_template_kwargs" not in _SEEN[-1]
    assert "reasoning_effort" not in _SEEN[-1]


async def test_openai_level_uses_reasoning_effort_not_kwarg():
    _SEEN.clear()
    proto = OpenAIProtocol("http://x", None, _collect(_openai_json), flavor="llamacpp")
    await proto.chat("qwen3.8:27b", [{"role": "user", "content": "hi"}], think="high")
    assert _SEEN[-1]["reasoning_effort"] == "high"
    assert "chat_template_kwargs" not in _SEEN[-1]


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


async def test_anthropic_counts_cache_tokens_as_context_and_hit():
    # Anthropic's input_tokens is ONLY the uncached delta; the real context lives
    # in cache_read/creation. prompt_tokens must sum all three (so context isn't
    # reported as ~2), and cache_read must surface as cached_tokens (cache hit %).
    def handler(request):
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 2, "output_tokens": 40,
                      "cache_read_input_tokens": 90000,
                      "cache_creation_input_tokens": 8000}})
    proto = AnthropicProtocol("http://m", "k", _collect(handler))
    res = await proto.chat("claude-sonnet-5", [{"role": "user", "content": "hi"}])
    assert res.prompt_tokens == 98002          # 2 + 90000 + 8000, the true context
    assert res.cached_tokens == 90000          # cache_read → KV cache hit
    assert res.completion_tokens == 40


async def test_anthropic_stream_counts_cache_tokens():
    stream = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":2,'
        '"cache_read_input_tokens":90000,"cache_creation_input_tokens":8000}}}\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n'
        'data: {"type":"message_delta","usage":{"output_tokens":40}}\n'
        'data: {"type":"message_stop"}\n')
    proto = AnthropicProtocol("http://m", "k",
                              _collect(lambda req: httpx.Response(200, text=stream)))
    done = (await _drain(proto.chat_stream("claude-sonnet-5",
                                           [{"role": "user", "content": "hi"}])))[-1]
    assert done["prompt_tokens"] == 98002 and done["cached_tokens"] == 90000


async def test_anthropic_sends_meridian_profile_header():
    # A backend pinned to a Meridian profile must send x-meridian-profile so
    # Meridian routes the call to that Claude account; unset = no header.
    seen = {}

    def _cap(request):
        seen["h"] = dict(request.headers)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}],
                                         "usage": {"input_tokens": 1, "output_tokens": 1}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(_cap))
    proto = AnthropicProtocol("http://m", "k", client, meridian_profile="victor")
    await proto.chat("claude-sonnet-5", [{"role": "user", "content": "hi"}])
    assert seen["h"].get("x-meridian-profile") == "victor"

    proto2 = AnthropicProtocol("http://m", "k", client)   # no profile
    await proto2.chat("claude-sonnet-5", [{"role": "user", "content": "hi"}])
    assert "x-meridian-profile" not in seen["h"]


def test_make_protocol_threads_meridian_profile():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    p = make_protocol("anthropic-compatible", "http://m", "k", client,
                      meridian_profile="acct2")
    assert p.meridian_profile == "acct2"


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

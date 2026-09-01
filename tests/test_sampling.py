"""Sampling defaults (global < persona < client) and structured-output format."""

import json

from foundry_router import sampling
from foundry_router.pool.protocols import OllamaProtocol


# -- resolve_options precedence ----------------------------------------------------

def test_options_precedence_client_wins():
    out = sampling.resolve_options(
        {"temperature": 0.7, "top_p": 0.9},          # global
        {"sampling_options": {"temperature": 0.2}},   # persona overrides temp
        {"top_p": 0.5})                               # client overrides top_p
    assert out == {"temperature": 0.2, "top_p": 0.5}


def test_options_empty_is_none():
    assert sampling.resolve_options({}, {}, None) is None


def test_options_persona_json_string():
    out = sampling.resolve_options({}, {"sampling_options": '{"top_k": 40}'}, None)
    assert out == {"top_k": 40}


# -- resolve_format ----------------------------------------------------------------

def test_format_none_and_blank():
    assert sampling.resolve_format({}) is None
    assert sampling.resolve_format({"output_format": ""}) is None


def test_format_json():
    assert sampling.resolve_format({"output_format": "json"}) == "json"
    assert sampling.resolve_format({"output_format": "JSON"}) == "json"


def test_format_schema_string_parsed():
    schema = '{"type": "object", "properties": {"x": {"type": "number"}}}'
    out = sampling.resolve_format({"output_format": schema})
    assert isinstance(out, dict) and out["type"] == "object"


# -- Ollama payload carries format + merged options --------------------------------

def _payload(options=None, fmt=None):
    p = OllamaProtocol("http://x", None, None)
    return p._payload("m", [{"role": "user", "content": "hi"}], None, options, None,
                      stream=False, fmt=fmt)


def test_ollama_payload_sets_format_json():
    assert _payload(fmt="json")["format"] == "json"


def test_ollama_payload_sets_format_schema():
    schema = {"type": "object"}
    assert _payload(fmt=schema)["format"] == schema


def test_ollama_payload_no_format_when_none():
    assert "format" not in _payload()


def test_ollama_payload_passes_sampling_options():
    assert _payload(options={"temperature": 0.2})["options"]["temperature"] == 0.2


# -- OpenAI maps format to response_format -----------------------------------------

class _Resp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "{}"}}], "usage": {}}


class _Capture:
    def __init__(self):
        self.payload = None

    async def post(self, url, json=None, headers=None):
        self.payload = json
        return _Resp()


async def test_openai_format_json_object():
    from foundry_router.pool.protocols import OpenAIProtocol
    c = _Capture()
    proto = OpenAIProtocol("http://o/v1", "k", c)
    await proto.chat("gpt", [{"role": "user", "content": "hi"}], fmt="json")
    assert c.payload["response_format"] == {"type": "json_object"}


async def test_openai_format_schema():
    from foundry_router.pool.protocols import OpenAIProtocol
    c = _Capture()
    proto = OpenAIProtocol("http://o/v1", "k", c)
    await proto.chat("gpt", [{"role": "user", "content": "hi"}], fmt={"name": "s"})
    assert c.payload["response_format"]["type"] == "json_schema"

"""Regression tests for the malformed-tool-call retry (found in deployment).

A small local brain sometimes ignores the delegation rule, tries to write a
long answer directly into return_to_user's JSON arguments, and runs out of
generation budget mid-JSON — llama-server then 500s with "invalid tool call
arguments ... unexpected end of JSON input". That brain is NOT unreachable;
BrainClient must retry once with corrective feedback before degrading to the
static fallback, and only for this specific error, only when tools were
offered, and only once.
"""

import pytest

from foundry_router.brain.client import BrainClient, BrainUnreachable
from foundry_router.config import AgentBrainConfig
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult, ProtocolError

MALFORMED_ERROR = ('ollama http://x HTTP 500: {"error":"llama-server returned '
                   'invalid tool call arguments for \\"return_to_user\\": '
                   'unexpected end of JSON input"}')

TOOLS = [{"type": "function", "function": {"name": "ask_m", "description": "",
                                           "parameters": {"type": "object", "properties": {}}}}]


class ScriptedProtocol:
    """Raises/returns per a fixed script; records every message list sent."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list[dict]] = []

    async def chat(self, model, messages, tools=None, options=None,
                   keep_alive=None, max_tokens=4096):
        self.calls.append(messages)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def make_client(tmp_path, script):
    client = BrainClient(AgentBrainConfig(model="test-brain"), client=None,
                         db=Database(tmp_path / "b.sqlite"))
    client.protocol = ScriptedProtocol(script)
    return client


async def test_malformed_tool_call_retries_once_with_correction(tmp_path):
    client = make_client(tmp_path, [ProtocolError(MALFORMED_ERROR),
                                    ChatResult(content="ok")])
    result = await client.chat([{"role": "user", "content": "hi"}], tools=TOOLS)
    assert result.content == "ok"
    assert len(client.protocol.calls) == 2
    # the retry carries corrective feedback as the newest message
    correction = client.protocol.calls[1][-1]
    assert correction["role"] == "user"
    assert "delegat" in correction["content"]  # delegate/delegating
    assert "use_last_result" in correction["content"]
    # and it landed in the troubleshooting log
    events = client.db.query("SELECT * FROM event_log WHERE source='brain'")
    assert any("malformed tool call" in e["message"] for e in events)


async def test_second_malformed_attempt_degrades(tmp_path):
    client = make_client(tmp_path, [ProtocolError(MALFORMED_ERROR),
                                    ProtocolError(MALFORMED_ERROR)])
    with pytest.raises(BrainUnreachable):
        await client.chat([{"role": "user", "content": "hi"}], tools=TOOLS)
    assert len(client.protocol.calls) == 2  # exactly one retry, never a loop


async def test_no_retry_without_tools(tmp_path):
    """complete()-style calls offer no tools — the correction text would make
    no sense there, so the error degrades immediately."""
    client = make_client(tmp_path, [ProtocolError(MALFORMED_ERROR)])
    with pytest.raises(BrainUnreachable):
        await client.chat([{"role": "user", "content": "hi"}])
    assert len(client.protocol.calls) == 1


async def test_other_errors_still_degrade_immediately(tmp_path):
    client = make_client(tmp_path, [ProtocolError("connection refused")])
    with pytest.raises(BrainUnreachable):
        await client.chat([{"role": "user", "content": "hi"}], tools=TOOLS)
    assert len(client.protocol.calls) == 1

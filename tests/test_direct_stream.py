"""OllamaProtocol.chat_stream carries tools through and surfaces tool_calls +
thinking per chunk — the plumbing behind direct-dispatch live streaming for
coding clients (Cline): each chunk proves the backend is generating, content
streams live, and tool_calls are delivered for the client to execute."""

import json

import httpx

from foundry_router.pool.protocols import OllamaProtocol


def _ndjson(*objs) -> bytes:
    return "".join(json.dumps(o) + "\n" for o in objs).encode()


async def test_chat_stream_streams_content_and_delivers_tool_calls():
    lines = _ndjson(
        {"message": {"content": "Hel", "thinking": "planning…"}, "done": False},
        {"message": {"content": "lo"}, "done": False},
        {"message": {"content": "", "tool_calls": [
            {"function": {"name": "write_to_file", "arguments": {"path": "a.txt"}}}]},
         "done": False},
        {"message": {"content": ""}, "done": True, "prompt_eval_count": 10,
         "eval_count": 5, "eval_duration": 111, "load_duration": 222},
    )

    def handler(request):
        # tools must have been forwarded into the payload
        body = json.loads(request.content)
        assert body["stream"] is True and body.get("tools")
        return httpx.Response(200, content=lines)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        proto = OllamaProtocol("http://x", None, client)
        chunks = [c async for c in proto.chat_stream(
            "m", [{"role": "user", "content": "hi"}], tools=[{"type": "function"}])]
    finally:
        await client.aclose()

    # content streamed live across chunks
    content = "".join(c.get("content") or "" for c in chunks if not c.get("done"))
    assert content == "Hello"
    # thinking surfaced (native reasoning field)
    assert any((c.get("thinking") or "") for c in chunks)
    # tool_call parsed to the internal shape
    tc_chunk = next(c for c in chunks if c.get("tool_calls"))
    tc = tc_chunk["tool_calls"][0]
    assert tc["name"] == "write_to_file" and tc["arguments"] == {"path": "a.txt"}
    # done chunk carries token counts + timing
    done = next(c for c in chunks if c.get("done"))
    assert done["prompt_tokens"] == 10 and done["completion_tokens"] == 5
    assert done["eval_duration_ns"] == 111 and done["load_duration_ns"] == 222

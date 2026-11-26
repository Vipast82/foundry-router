"""v0.181.261: misspelt text tool calls are read too.

Media_Gen chat test round 6 (2026-11-26, Foundry-Chat on qwen3.8-flash-next, Strata):
asked "is it done?" about a comic, the model wrote its media_check_job call with
`<function(` instead of `<function=`. toolcall_text read only `<function=NAME>`, logged
"the block starts with neither { nor <function=", and the chat showed the markup as the
answer; nothing ran (job 5da124e2770d)."""

import json

import httpx

from foundry_router import tool_finder, toolcall_text
from foundry_router.pool.protocols import OpenAIProtocol
from foundry_router.toolcall_text import Gate, extract, only_calls, why_unread

TOOLS = [{"type": "function", "function": {"name": tool_finder.FIND_TOOL,
                                           "parameters": tool_finder.FIND_PARAMS}},
         {"type": "function", "function": {"name": tool_finder.CALL_TOOL,
                                           "parameters": tool_finder.CALL_PARAMS}}]
# exactly what the chat got
SEEN = ('<tool_call>\n<function(foundry_call_tool>\n<parameter=name>\nmedia_check_job\n</parameter>\n'
        '<parameter=arguments>\n{"job_id": "5da124e2770d"}\n</parameter>\n</function>\n</tool_call>')
CHECK = {"name": "foundry_call_tool",
         "arguments": {"name": "media_check_job", "arguments": {"job_id": "5da124e2770d"}}}


def test_the_call_from_round_6_is_read():
    assert extract(SEEN, TOOLS) == ([CHECK], "")
    assert only_calls(SEEN)                 # kept out of the history as an example, like the others
    assert why_unread(SEEN, TOOLS) == "every block read, so something after this rejected them"


def test_the_other_misspellings_are_read():
    for opening in ("<function(foundry_call_tool)>", "<function: foundry_call_tool>",
                    "<function foundry_call_tool>", "<function ( foundry_call_tool )>"):
        text = SEEN.replace("<function(foundry_call_tool>", opening)
        assert extract(text, TOOLS) == ([CHECK], ""), opening
    text = SEEN.replace("<parameter=name>", "<parameter(name)>").replace(
        "<parameter=arguments>", "<parameter arguments>")
    assert extract(text, TOOLS) == ([CHECK], "")
    # without the wrapper: the "(" form is read, a short name resolves as before
    bare = "<function(find_tools>\n<parameter(query>\nweb search\n</function>"
    assert extract(bare, TOOLS) == ([{"name": "foundry_find_tools", "arguments": {"query": "web search"}}], "")


def test_prose_is_still_left_alone():
    for text in ("In C you write <function name> and then the body.",
                 "Call it as <function(x)> with any value you like.",
                 "The template shows <function: NAME> where the call goes."):
        assert extract(text, TOOLS) == ([], text), text
    # the space form outside a <tool_call> block is not even looked at
    assert toolcall_text._norm("a <function foo> b") == "a <function foo> b"
    # a tool that was not offered is still not called (no finder in the request)
    only_find = TOOLS[:1]
    text = "<tool_call>\n<function(rm_rf>\n<parameter=path>\n/\n</function>\n</tool_call>"
    assert extract(text, only_find) == ([], text)


def test_the_stream_holds_the_misspelt_call_back():
    g = Gate()
    out = [g.feed(p) for p in ("Checking. ", "<func", "tion(foundry_call_tool>\n<parameter=name>\n",
                               "media_check_job\n<parameter=arguments>\n", '{"job_id": "5da124e2770d"}\n',
                               "</function>")]
    assert "".join(out) == "Checking. " and g.holding
    assert g.finish(TOOLS) == ([CHECK], "")


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sse(parts):
    lines = [json.dumps({"choices": [{"delta": {"content": p}}]}) for p in parts]
    lines.append(json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    return "".join(f"data: {x}\n\n" for x in lines) + "data: [DONE]\n\n"


async def test_the_pool_turns_it_into_a_tool_call():
    def h(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": SEEN}, "finish_reason": "stop"}]})
    res = await OpenAIProtocol("http://x", None, _client(h), flavor="llamacpp").chat(
        "m", [{"role": "user", "content": "is it done?"}], tools=TOOLS)
    assert [(c["name"], c["arguments"]) for c in res.tool_calls] == [(CHECK["name"], CHECK["arguments"])]
    assert res.content == "" and res.finish_reason == "tool_calls"

    parts = [SEEN[i:i + 7] for i in range(0, len(SEEN), 7)]

    def hs(request):
        return httpx.Response(200, text=_sse(parts), headers={"content-type": "text/event-stream"})
    chunks = [c async for c in OpenAIProtocol("http://x", None, _client(hs), flavor="llamacpp").chat_stream(
        "m", [{"role": "user", "content": "is it done?"}], tools=TOOLS)]
    assert "".join(c.get("content") or "" for c in chunks) == ""
    assert chunks[-1]["finish_reason"] == "tool_calls"
    assert [(c["name"], c["arguments"]) for c in chunks[-1]["tool_calls"]] == [(CHECK["name"], CHECK["arguments"])]

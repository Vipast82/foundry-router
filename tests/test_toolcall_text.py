"""v0.181.129: tool calls a local model wrote as text are read and run.

2026-11-11, AnythingLLM, persona Foundry-Chat on qwen3.8-flash-next (Strata): every
turn ended with the model's tool calls shown as text and nothing happened. The model
shortened the tool names (find_tools for foundry_find_tools) and left out the closing
</parameter> and </function> tags, so the engine did not turn the text into calls and
the router saw a finished answer with zero tool calls."""

import json

import httpx

from foundry_router import tool_finder, toolcall_text
from foundry_router.pool.protocols import OpenAIProtocol
from foundry_router.toolcall_text import Gate, extract

TOOLS = [{"type": "function", "function": {"name": tool_finder.FIND_TOOL,
                                           "parameters": tool_finder.FIND_PARAMS}},
         {"type": "function", "function": {"name": tool_finder.CALL_TOOL,
                                           "parameters": tool_finder.CALL_PARAMS}}]
# exactly what the chat showed (AnythingLLM prints newlines as spaces)
SEEN_1 = ("<tool_call> <function=find_tools> <parameter=query> web search </tool_call> <tool_call> "
          "<function=find_tools> <parameter=query> crawl or fetch a web page to markdown </tool_call>")
SEEN_2 = ('<tool_call> <function=call_tool> <parameter=arguments> {"query": "Wan 2.2 14B lightning '
          'LoRA VRAM requirements 16GB ComfyUI"} <parameter=name> searxng_web_search </tool_call>')
WELL_FORMED = ("<tool_call>\n<function=foundry_call_tool>\n<parameter=name>\nsearxng_web_search\n"
               '</parameter>\n<parameter=arguments>\n{"query": "x", "n": 3}\n</parameter>\n'
               "</function>\n</tool_call>")


def test_the_calls_from_the_screenshot_are_read():
    calls, rest = extract(SEEN_1, TOOLS)
    assert rest == "" and calls == [
        {"name": "foundry_find_tools", "arguments": {"query": "web search"}},
        {"name": "foundry_find_tools", "arguments": {"query": "crawl or fetch a web page to markdown"}}]
    calls, rest = extract(SEEN_2, TOOLS)
    assert rest == "" and calls == [{"name": "foundry_call_tool", "arguments": {
        "name": "searxng_web_search",
        "arguments": {"query": "Wan 2.2 14B lightning LoRA VRAM requirements 16GB ComfyUI"}}}]


def test_the_well_formed_and_the_json_formats_are_read_too():
    assert extract(WELL_FORMED, TOOLS)[0] == [{"name": "foundry_call_tool", "arguments": {
        "name": "searxng_web_search", "arguments": {"query": "x", "n": 3}}}]
    hermes = '<tool_call>\n{"name": "foundry_find_tools", "arguments": {"query": "q", "limit": 3}}\n</tool_call>'
    assert extract(hermes, TOOLS)[0] == [{"name": "foundry_find_tools",
                                          "arguments": {"query": "q", "limit": 3}}]
    as_string = '<tool_call>{"name": "find_tools", "arguments": "{\\"query\\": \\"q\\"}"}</tool_call>'
    assert extract(as_string, TOOLS)[0] == [{"name": "foundry_find_tools", "arguments": {"query": "q"}}]
    # without the <tool_call> wrapper, and with text before the call
    bare = "I will search.\n<function=find_tools>\n<parameter=query>\nweb search\n</parameter>\n</function>"
    assert extract(bare, TOOLS) == ([{"name": "foundry_find_tools", "arguments": {"query": "web search"}}],
                                    "I will search.")
    # a last block cut off before its closing tag
    cut = "<tool_call>\n<function=find_tools>\n<parameter=query>\nimage generation"
    assert extract(cut, TOOLS)[0] == [{"name": "foundry_find_tools", "arguments": {"query": "image generation"}}]


def test_values_take_the_type_the_tool_asks_for():
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {
        "text": {"type": "string"}, "n": {"type": "integer"}, "on": {"type": "boolean"},
        "items": {"type": "array"}, "opts": {"type": "object"}}}}}]
    body = ("<tool_call><function=t><parameter=text>123</parameter><parameter=n>5</parameter>"
            "<parameter=on>true</parameter><parameter=items>[1, 2]</parameter>"
            '<parameter=opts>{"a": 1}</parameter><parameter=extra>{"k": "v"}</parameter>'
            "<parameter=other>plain words</parameter></function></tool_call>")
    args = extract(body, tools)[0][0]["arguments"]
    assert args == {"text": "123", "n": 5, "on": True, "items": [1, 2], "opts": {"a": 1},
                    "extra": {"k": "v"}, "other": "plain words"}
    # a string parameter keeps text that happens to look like JSON
    assert extract('<tool_call><function=t><parameter=text>{"a": 1}</parameter></function></tool_call>',
                   tools)[0][0]["arguments"] == {"text": '{"a": 1}'}


def test_text_that_is_not_a_call_is_left_alone():
    for text in ("To call a tool write <tool_call> and the function name.",
                 "<tool_call>\n<function=delete_everything>\n<parameter=path>\n/\n</tool_call>",   # not offered
                 '<tool_call>{"name": "unknown_tool", "arguments": {}}</tool_call>',
                 "<tool_call>not json and not a function</tool_call>",
                 "<tool_call><function=find_tools>some prose<parameter=query>x</tool_call>",
                 SEEN_1 + " <tool_call>{broken</tool_call>",                                       # one bad block
                 "no markers at all"):
        assert extract(text, TOOLS) == ([], text), text
    assert extract(SEEN_1, None) == ([], SEEN_1) and extract(SEEN_1, []) == ([], SEEN_1)   # no tools offered
    # an ambiguous short name is not guessed
    two = TOOLS + [{"type": "function", "function": {"name": "other_find_tools", "parameters": {}}}]
    assert extract(SEEN_1, two) == ([], SEEN_1)
    assert toolcall_text.has_marker(SEEN_1) and not toolcall_text.has_marker("plain") and not toolcall_text.has_marker("")


def test_the_gate_holds_a_call_back_and_lets_ordinary_text_through():
    g = Gate()
    out = [g.feed(p) for p in ("Let me ", "look. <tool", "_call>\n<function=find_tools>\n<para",
                               "meter=query>\nweb search\n", "</tool_call>")]
    assert "".join(out) == "Let me look. " and g.holding and g.held_chars > 20
    calls, owed = g.finish(TOOLS)
    assert calls == [{"name": "foundry_find_tools", "arguments": {"query": "web search"}}] and owed == ""
    # text that only looked like the start of a call is handed over, nothing lost
    g = Gate()
    out = [g.feed(p) for p in ("a < b and <tool", "s> are fine", " <fun", "ny>")]
    assert "".join(out) + g.finish(TOOLS)[1] == "a < b and <tools> are fine <funny>"
    g = Gate()
    out = [g.feed(p) for p in ("see <tool_call> in the docs", " for details")]
    calls, owed = g.finish(TOOLS)
    assert calls == [] and "".join(out) + owed == "see <tool_call> in the docs for details"
    g = Gate()
    assert g.feed("ends with <tool_c") == "ends with " and g.finish(TOOLS) == ([], "<tool_c")
    # switched off (no tools in the request): text passes untouched
    g = Gate(active=False)
    assert g.feed(SEEN_1) == SEEN_1 and g.finish(TOOLS) == ([], "")


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_reply_with_text_calls_comes_back_as_tool_calls():
    def h(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "<think>need a search</think>" + SEEN_1.replace(" ", "\n")
                                     .replace("web\nsearch", "web search")},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20}})
    proto = OpenAIProtocol("http://x", None, _client(h), flavor="llamacpp")
    res = await proto.chat("m", [{"role": "user", "content": "hi"}], tools=TOOLS)
    assert [(c["name"], c["arguments"]["query"][:5]) for c in res.tool_calls] == [
        ("foundry_find_tools", "web s"), ("foundry_find_tools", "crawl")]
    assert all(c["id"].startswith("call_") for c in res.tool_calls)
    assert res.content == "" and res.finish_reason == "tool_calls" and res.thinking == "need a search"
    # the same text with no tools in the request is an ordinary answer
    res = await proto.chat("m", [{"role": "user", "content": "hi"}])
    assert not res.tool_calls and "<tool_call>" in res.content and res.finish_reason == "stop"


async def test_structured_calls_from_the_engine_are_used_as_they_are():
    def h(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "foundry_find_tools",
                                                          "arguments": '{"query": "q"}'}}]},
            "finish_reason": "tool_calls"}]})
    res = await OpenAIProtocol("http://x", None, _client(h), flavor="llamacpp").chat(
        "m", [{"role": "user", "content": "hi"}], tools=TOOLS)
    assert [(c["id"], c["name"], c["arguments"]) for c in res.tool_calls] == [
        ("c1", "foundry_find_tools", {"query": "q"})]


def _sse(parts, finish="stop"):
    lines = [json.dumps({"choices": [{"delta": {"content": p}}]}) for p in parts]
    lines.append(json.dumps({"choices": [{"delta": {}, "finish_reason": finish}],
                             "usage": {"prompt_tokens": 9, "completion_tokens": 7}}))
    return "".join(f"data: {x}\n\n" for x in lines) + "data: [DONE]\n\n"


async def test_a_streamed_text_call_is_never_shown_and_arrives_as_a_tool_call():
    parts = ["Searching", " now.\n", "<tool", "_call>\n<function=", "call_tool>\n<parameter=name>\nsearxng_web_search\n",
             '<parameter=arguments>\n{"query": "LTX-2 workflow"}\n', "</tool_call>"]

    def h(request):
        return httpx.Response(200, text=_sse(parts), headers={"content-type": "text/event-stream"})
    proto = OpenAIProtocol("http://x", None, _client(h), flavor="llamacpp")
    chunks = [c async for c in proto.chat_stream("m", [{"role": "user", "content": "hi"}], tools=TOOLS)]
    shown = "".join(c.get("content") or "" for c in chunks)
    assert shown == "Searching now.\n" and "<" not in shown
    done = chunks[-1]
    assert done["done"] and done["finish_reason"] == "tool_calls"
    assert [(c["name"], c["arguments"]) for c in done["tool_calls"]] == [
        ("foundry_call_tool", {"name": "searxng_web_search", "arguments": {"query": "LTX-2 workflow"}})]
    # while the call is being written the stream reports progress (it is not a stall)
    assert any((c.get("progress") or {}).get("tool_chars") for c in chunks)


async def test_streamed_text_that_only_looks_like_a_call_is_delivered_whole():
    parts = ["Write <tool", "_call> then the name", ", like this."]

    def h(request):
        return httpx.Response(200, text=_sse(parts), headers={"content-type": "text/event-stream"})
    proto = OpenAIProtocol("http://x", None, _client(h), flavor="llamacpp")
    chunks = [c async for c in proto.chat_stream("m", [{"role": "user", "content": "hi"}], tools=TOOLS)]
    assert "".join(c.get("content") or "" for c in chunks) == "Write <tool_call> then the name, like this."
    assert not chunks[-1]["tool_calls"] and chunks[-1]["finish_reason"] == "stop"
    # with no tools in the request nothing is held back at all
    chunks = [c async for c in proto.chat_stream("m", [{"role": "user", "content": "hi"}])]
    assert [c["content"] for c in chunks if c.get("content")] == parts


def test_past_replies_that_were_only_call_text_are_not_fed_back_as_examples():
    """The failed turns stay in the AnythingLLM thread; the model copied them each turn."""
    from foundry_router.brain import prompts
    history = [
        {"role": "user", "content": "find the best models"},
        {"role": "assistant", "content": SEEN_1},
        {"role": "user", "content": "What did you find out?"},
        {"role": "assistant", "content": "<think>hm</think>" + SEEN_2},
        {"role": "user", "content": "Im not seeing any output"},
        {"role": "assistant", "content": "Here is how a call looks: <tool_call> then the name."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "user", "content": "<tool_call> <function=find_tools> <parameter=query> x </tool_call>"},
    ]
    out = prompts.sanitize_history(history)
    assert [m["content"] for m in out[:4:2]] == ["find the best models", "What did you find out?"]
    assert out[1]["content"] == toolcall_text.UNRUN_NOTE and out[3]["content"] == toolcall_text.UNRUN_NOTE
    assert out[5]["content"].startswith("Here is how a call looks")      # prose about calls is kept
    assert out[6]["tool_calls"] and out[7]["content"].startswith("<tool_call>")   # a user message is never touched
    assert toolcall_text.only_calls(SEEN_1) and toolcall_text.only_calls(WELL_FORMED)
    assert not toolcall_text.only_calls("text <tool_call>x</tool_call>") and not toolcall_text.only_calls("")
    assert not toolcall_text.only_calls("<tool_call>just words</tool_call>")

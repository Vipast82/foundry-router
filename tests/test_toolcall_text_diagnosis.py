"""v0.181.194: when a tool call cannot be read, record which branch rejected it.

Three answers of campaign 2 came back as "<worker> wrote tool calls that could
not be read" and the text that could not be read was kept nowhere, so the fault
was unfalsifiable after the fact. I tested the likeliest explanation and it was
wrong: find-on-demand offers only `foundry_find_tools` and `foundry_call_tool`,
so I expected a model calling a tool it had just looked up - by that tool's own
name - to fail to resolve. `_named` already wraps an unresolved name in
foundry_call_tool, and its docstring describes that exact case. All five shapes
of that call parse, so that is not it.

What is left cannot be told apart without the text, so the text is kept with the
reason. These tests pin the reasons apart, because a diagnostic that says the
same thing for every cause is no better than none.
"""
import pytest

from foundry_router import toolcall_text as T

FINDER = [
    {"type": "function", "function": {
        "name": "foundry_find_tools",
        "parameters": {"properties": {"query": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "foundry_call_tool",
        "parameters": {"properties": {"name": {"type": "string"},
                                      "arguments": {"type": "object"}}}}},
]


def test_no_tools_offered_says_so():
    """The one cause that is not the reader's fault: the model called a tool it
    was never given, so there was nothing to resolve against."""
    why = T.why_unread('<tool_call>{"name":"x","arguments":{}}</tool_call>', None)
    assert "no tools were offered" in why
    assert T.why_unread('<tool_call>{"name":"x"}</tool_call>', []) == why


def test_json_that_does_not_parse_says_so_and_quotes_it():
    why = T.why_unread('<tool_call>{"name": "x", "arguments": {oops}}</tool_call>',
                       FINDER)
    assert "does not parse" in why
    assert "block 1" in why


def test_prose_between_the_tags_says_so():
    """The rule that stops a model explaining the syntax being run as a call."""
    why = T.why_unread(
        "<tool_call><function=foundry_find_tools>thinking out loud"
        "<parameter=query>a</parameter></function></tool_call>", FINDER)
    assert "not a parameter" in why


def test_a_block_with_no_name_says_so():
    why = T.why_unread('<tool_call>{"arguments": {"query":"a"}}</tool_call>', FINDER)
    assert "no name" in why


def test_a_marker_with_no_readable_block():
    why = T.why_unread("<function=", FINDER)
    assert why


def test_text_that_reads_fine_says_the_rejection_came_later():
    """So a log line can never read as "the model wrote it wrong" when the model
    wrote it correctly and something downstream dropped it."""
    why = T.why_unread(
        '<tool_call>{"name":"foundry_find_tools","arguments":{"query":"a"}}</tool_call>',
        FINDER)
    assert "every block read" in why
    assert "something after this" in why


def test_the_hypothesis_that_was_wrong_stays_wrong():
    """A call to a tool the finder RETURNED, not one of the two offered: this
    resolves through foundry_call_tool and must keep doing so, or the finder
    breaks for every model that writes its calls as text."""
    for text in (
        '<tool_call>{"name": "searxng__web_search", "arguments": {"query": "x"}}</tool_call>',
        "<tool_call><function=searxng__web_search><parameter=query>x</parameter>"
        "</function></tool_call>",
    ):
        calls, _rest = T.extract(text, FINDER)
        assert len(calls) == 1, text
        assert calls[0]["name"] == "foundry_call_tool"
        assert calls[0]["arguments"]["name"] == "searxng__web_search"


@pytest.mark.parametrize("bad", [None, "", "just a sentence."])
def test_it_never_raises_on_rubbish(bad):
    """It runs inside the failure path; it must not add a second failure."""
    assert isinstance(T.why_unread(bad, FINDER), str)


def test_the_brain_path_records_it_with_the_text():
    import inspect

    from foundry_router.brain.agent import AgentRunner
    src = inspect.getsource(AgentRunner._worker_tool_loop)
    assert "why_unread" in src
    # the services object, which main.py sets on the runner - not self.brain.svc,
    # which does not exist and would have logged nothing while looking fine
    assert "self.svc.db.log_event" in src
    assert "clean.strip()[:600]" in src

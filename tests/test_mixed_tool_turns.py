"""Mixed tool turns on the direct path (audit A4): when the model calls a
persona MCP tool AND a client tool in one turn, Foundry runs its own call,
hands the client call back (nothing dropped, no extra model round), and puts
the persona call + result back into that turn on the next request, so every
call stays paired (Ollama and OpenAI dialects) and the history is stable."""
import json

import pytest

from foundry_router import mixed_rounds
from foundry_router.pool.protocols import ChatResult
from foundry_router.tools.mcp_client import ToolResult
from foundry_router.tools.sync import ToolDef


class ScriptPool:
    """Turn 1 of a conversation: search (persona) + write_to_file (client) in
    one turn. Once a write_to_file result is in the history: a final answer."""

    def __init__(self, first_calls=None):
        self.convos = []
        self.first_calls = first_calls or [
            {"id": "m1", "name": "searxng__search", "arguments": {"q": "strata"}},
            {"id": "m2", "name": "write_to_file", "arguments": {"path": "a.md", "text": "x"}}]

    def available_models(self):
        return {"local-a": ["o1"]}

    def backend_info(self, m):
        return {"name": "o1", "type": "ollama", "url": "http://o"} if m == "local-a" else None

    def backend_status(self):
        return []

    async def loaded_models(self):
        return set()

    def active_calls(self):
        return []

    def _turn(self, messages):
        self.convos.append(messages)
        if any(m.get("role") == "tool" and m.get("name") == "write_to_file" for m in messages):
            return ChatResult(content="Done: wrote a.md from the search.", prompt_tokens=9,
                              completion_tokens=5, finish_reason="stop")
        return ChatResult(content="Searching and writing.", tool_calls=self.first_calls,
                          prompt_tokens=5, completion_tokens=2, finish_reason="tool_calls")

    async def chat(self, model, messages, tools=None, **kw):
        return self._turn(messages), "o1"

    async def chat_stream(self, model, messages, tools=None, **kw):
        res = self._turn(messages)
        if res.content:
            yield {"content": res.content}
        yield res.done_frame()


@pytest.fixture()
def mixed(app, client):
    svc = app.state.services
    cfg = svc.config_store.config.agent_brain
    persona = next(p for p in svc.personas.list(enabled_only=True))
    fields = dict(preferred_mcp_tools=json.dumps(["searxng"]), mcp_tools_in_direct=1)
    if "mcp_with_client_tools" in persona:          # v0.142.0 merge policy
        fields["mcp_with_client_tools"] = "all"
    svc.personas.upsert(persona["virtual_name"], **fields)
    td = ToolDef(name="searxng__search", kind="mcp", description="search",
                 parameters={"type": "object"}, server="searxng", mcp_tool="search")
    svc.tool_registry.tools[td.name] = td
    ran = []

    async def fake_rich(server, tool, args, **kw):
        ran.append(args)
        return ToolResult([{"type": "text", "text": "SEARCH RESULTS: strata forks A, B"}])
    old = (svc.mcp.call_tool_rich, svc.pool, cfg.worker_tool_max_steps, cfg.direct_stream,
           cfg.direct_stream_heartbeat_seconds)
    svc.mcp.call_tool_rich = fake_rich
    cfg.worker_tool_max_steps = 4

    def use(pool, stream=False):
        svc.pool = pool
        cfg.direct_stream, cfg.direct_stream_heartbeat_seconds = stream, 0
    use.persona = persona["virtual_name"]
    use.ran = ran
    use.svc = svc
    use.cfg = cfg
    yield use
    (svc.mcp.call_tool_rich, svc.pool, cfg.worker_tool_max_steps, cfg.direct_stream,
     cfg.direct_stream_heartbeat_seconds) = old


TOOLS = [{"type": "function", "function": {"name": "write_to_file",
                                           "parameters": {"type": "object"}}}]
USER = {"role": "user", "content": "research strata and write a.md"}


def _ndjson(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _assert_paired(convo):
    """Every assistant tool call is answered by a tool message, in call order."""
    for i, m in enumerate(convo):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            following = []
            for r in convo[i + 1:]:
                if r.get("role") != "tool":
                    break
                following.append(r.get("tool_call_id"))
            assert following == ids, (ids, following)


def _check_second_turn(pool, ran):
    second = pool.convos[-1]
    _assert_paired(second)
    turn = next(m for m in second if m.get("role") == "assistant" and m.get("tool_calls"))
    names = [tc["function"]["name"] for tc in turn["tool_calls"]]
    assert names == ["searxng__search", "write_to_file"]      # model order
    results = [m for m in second if m.get("role") == "tool"]
    assert "SEARCH RESULTS" in results[0]["content"]
    assert results[1]["content"] == "wrote a.md"
    assert len(ran) == 1                                       # not run again


def test_ollama_buffered_mixed_turn_keeps_the_client_call(mixed, client):
    pool = ScriptPool()
    mixed(pool)
    r = client.post("/api/chat", json={"model": mixed.persona, "stream": False,
                                       "tools": TOOLS, "messages": [USER]}).json()
    calls = r["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["write_to_file"]
    assert len(pool.convos) == 1                               # no extra model round
    assert mixed.ran == [{"q": "strata"}]
    # the client runs write_to_file and sends its history back (Ollama: no ids)
    hist = [USER, {"role": "assistant", "content": r["message"]["content"],
                   "tool_calls": [{"function": c["function"]} for c in calls]},
            {"role": "tool", "tool_name": "write_to_file", "content": "wrote a.md"}]
    r2 = client.post("/api/chat", json={"model": mixed.persona, "stream": False,
                                        "tools": TOOLS, "messages": hist}).json()
    assert r2["message"]["content"].endswith("Done: wrote a.md from the search.")
    _check_second_turn(pool, mixed.ran)
    # the same history again -> the same rebuilt prompt (prompt cache)
    client.post("/api/chat", json={"model": mixed.persona, "stream": False,
                                   "tools": TOOLS, "messages": hist})
    assert pool.convos[-1] == pool.convos[-2]


def test_ollama_streamed_mixed_turn(mixed, client):
    pool = ScriptPool()
    mixed(pool, stream=True)
    frames = _ndjson(client.post("/api/chat", json={"model": mixed.persona, "stream": True,
                                                    "tools": TOOLS, "messages": [USER]}).text)
    calls = [c for f in frames for c in (f["message"].get("tool_calls") or [])]
    assert [c["function"]["name"] for c in calls] == ["write_to_file"]
    thinking = "".join(f["message"].get("thinking") or "" for f in frames)
    assert "mixed turn" in thinking and len(pool.convos) == 1
    hist = [USER, {"role": "assistant", "content": "Searching and writing.",
                   "tool_calls": [{"function": c["function"]} for c in calls]},
            {"role": "tool", "tool_name": "write_to_file", "content": "wrote a.md"}]
    frames2 = _ndjson(client.post("/api/chat", json={"model": mixed.persona, "stream": True,
                                                     "tools": TOOLS, "messages": hist}).text)
    assert "Done: wrote a.md" in "".join(f["message"].get("content") or "" for f in frames2)
    _check_second_turn(pool, mixed.ran)


@pytest.mark.parametrize("stream", [False, True])
def test_openai_mixed_turn_pairs_by_id(mixed, client, stream):
    pool = ScriptPool()
    mixed(pool, stream=stream)
    body = {"model": mixed.persona, "stream": stream, "tools": TOOLS, "messages": [USER]}
    r = client.post("/v1/chat/completions", json=body)
    if stream:
        chunks = [json.loads(line[5:]) for line in r.text.splitlines()
                  if line.startswith("data:") and line.strip() != "data: [DONE]"]
        calls = [tc for c in chunks for ch in c.get("choices") or []
                 for tc in (ch.get("delta") or {}).get("tool_calls") or []]
    else:
        calls = r.json()["choices"][0]["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["write_to_file"]
    cid = calls[0]["id"]
    assert cid
    args = calls[0]["function"]["arguments"]
    hist = [USER, {"role": "assistant", "content": "Searching and writing.",
                   "tool_calls": [{"id": cid, "type": "function",
                                   "function": {"name": "write_to_file", "arguments": args}}]},
            {"role": "tool", "tool_call_id": cid, "content": "wrote a.md"}]
    client.post("/v1/chat/completions", json={**body, "messages": hist})
    _check_second_turn(pool, mixed.ran)
    turn = next(m for m in pool.convos[-1] if m.get("role") == "assistant" and m.get("tool_calls"))
    assert turn["tool_calls"][1]["id"] == cid                  # the client's id kept


def test_history_without_a_saved_round_passes_unchanged(mixed, client):
    pool = ScriptPool()
    mixed(pool)
    hist = [USER, {"role": "assistant", "content": "",
                   "tool_calls": [{"function": {"name": "write_to_file",
                                                "arguments": {"path": "other.md"}}}]},
            {"role": "tool", "tool_name": "write_to_file", "content": "wrote a.md"}]
    client.post("/api/chat", json={"model": mixed.persona, "stream": False,
                                   "tools": TOOLS, "messages": hist})
    names = [tc["function"]["name"] for m in pool.convos[-1] if m.get("tool_calls")
             for tc in m["tool_calls"]]
    assert names == ["write_to_file"] and mixed.ran == []


def test_mixed_turn_at_the_round_cap_answers_persona_calls_not_run(mixed, client):
    """Cap 1: round 0 runs a persona-only call, round 1 is mixed -> its persona
    call is "not run", the client call still goes back, pairing holds."""
    class CapPool(ScriptPool):
        def _turn(self, messages):
            n = len(self.convos)
            if n == 0:
                self.convos.append(messages)
                return ChatResult(content="first", tool_calls=[
                    {"id": "p0", "name": "searxng__search", "arguments": {"q": "0"}}],
                    prompt_tokens=1, completion_tokens=1, finish_reason="tool_calls")
            return super()._turn(messages)
    pool = CapPool()
    mixed(pool)
    mixed.cfg.worker_tool_max_steps = 1
    r = client.post("/api/chat", json={"model": mixed.persona, "stream": False,
                                       "tools": TOOLS, "messages": [USER]}).json()
    calls = r["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["write_to_file"]
    assert mixed.ran == [{"q": "0"}]                           # the capped call never ran
    hist = [USER, {"role": "assistant", "content": "",
                   "tool_calls": [{"function": c["function"]} for c in calls]},
            {"role": "tool", "tool_name": "write_to_file", "content": "wrote a.md"}]
    client.post("/api/chat", json={"model": mixed.persona, "stream": False,
                                   "tools": TOOLS, "messages": hist})
    second = pool.convos[-1]
    _assert_paired(second)
    assert any(m.get("role") == "tool" and "NOT RUN" in m["content"] for m in second)


def test_restore_unit_order_and_idempotence(tmp_path):
    from foundry_router.db import Database
    db = Database(tmp_path / "m.sqlite")
    calls = [{"id": "c", "name": "write_to_file", "arguments": {"p": 1}},
             {"id": "o", "name": "web__search", "arguments": {"q": "z"}}]
    mixed_rounds.save(db, "P", calls, {"web__search"},
                      [{"role": "tool", "tool_call_id": "o", "content": "R"}], calls[:1])
    hist = [{"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c", "function": {"name": "write_to_file", "arguments": '{"p": 1}'}}]},
            {"role": "tool", "tool_call_id": "c", "content": "ok"},
            {"role": "user", "content": "next"}]
    out, n = mixed_rounds.restore(db, "P", hist)
    assert n == 1
    assert [tc.get("id") for tc in out[1]["tool_calls"]] == ["c", "o"]   # model order
    assert [m.get("tool_call_id") for m in out[2:4]] == ["c", "o"]
    assert out[4] == hist[3]
    again, n2 = mixed_rounds.restore(db, "P", out)
    assert n2 == 0 and again == out
    other, n3 = mixed_rounds.restore(db, "Other-persona", hist)
    assert n3 == 0 and other == hist

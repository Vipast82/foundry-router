"""AnythingLLM agent-mode silent waits (v0.142.0).

1. MCP tools with client tools: when the client brings its own tools, which
   persona MCP tools ride along — all (default), off, auto-dedupe or a list;
   set on the client key (or for keyless clients), else on the persona.
2. Prompt-reading progress lines before the first token, a few per call, and
   an earlier first "still working" line when there's no progress report.
3. Thinking stays in message.thinking, content stays clean, and the status
   lines scrub out of echoed history exactly as before.
"""
import asyncio
import json

import pytest

from foundry_router import keepalive, persona_rules, router_ops, tool_policy
from foundry_router.brain import prompts
from foundry_router.pool.protocols import ChatResult
from foundry_router.tools.sync import ToolDef


def _td(name, server, tool, desc=None):
    return ToolDef(name=name, kind="mcp", description=desc or name, parameters={"type": "object"},
                   server=server, mcp_tool=tool)


SEARCH = _td("searxng__search", "searxng", "search", "[MCP:searxng] Search the web with SearXNG")
CRAWL = _td("crawl4ai__crawl", "crawl4ai", "crawl", "[MCP:crawl4ai] Crawl a web page to markdown")
SQL = _td("SQLite__read_query", "general-mcp-gateway", "SQLite__read_query",
          "[MCP:general-mcp-gateway] Execute a SELECT query on the SQLite database")
MUSIC = _td("acestep-music__generate_music", "acestep-music", "generate_music",
            "[MCP:acestep-music] Generate a song or music track from a prompt")
TTS = _td("kokoro-tts__generate_speech", "kokoro-tts", "generate_speech",
          "[MCP:kokoro-tts] Text to speech with Kokoro")


def _client_tools(*specs):
    return [{"type": "function", "function": {"name": n, "description": d}} for n, d in specs]


ANYTHINGLLM = _client_tools(
    ("web-browsing", "Searches the internet for a query and returns results"),
    ("web-scraping", "Scrapes the content of a webpage or URL"),
    ("filesystem-read-text-file", "Read a text file from the allowed directories"),
    ("create-chart", "Create a chart from data"))


# -- 1. policy ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,parsed", [
    (None, None), ("", None), ("garbage", None), ('{"x": 1}', None), (5, None),
    ("off", ("off", [])), ("OFF", ("off", [])), ("none", ("off", [])), ("all", ("all", [])),
    ("dedupe", ("dedupe", [])), ("auto-dedupe", ("dedupe", [])),
    ('["searxng"]', ("list", ["searxng"])), (["searxng"], ("list", ["searxng"])),
    ("[]", ("off", []))])
def test_parse(raw, parsed):
    assert tool_policy.parse(raw) == parsed


def test_policy_precedence_client_then_persona_then_all():
    assert tool_policy.policy({}) == ("all", [], "default")
    assert tool_policy.policy({"mcp_with_client_tools": "off"}) == ("off", [], "persona")
    assert tool_policy.policy({"mcp_with_client_tools": "off"}, ["searxng"]) \
        == ("list", ["searxng"], "client")
    assert tool_policy.policy({"mcp_with_client_tools": "off"}, None)[2] == "persona"


def test_select_without_client_tools_offers_every_grant():
    tools, mode, _src = tool_policy.select({"mcp_with_client_tools": "off"}, [SEARCH, CRAWL], [])
    assert tools == [SEARCH, CRAWL] and mode == "all"


def test_select_modes():
    granted = [SEARCH, CRAWL, SQL]
    ct = ANYTHINGLLM
    assert tool_policy.select({}, granted, ct)[0] == granted                       # default all
    assert tool_policy.select({"mcp_with_client_tools": "off"}, granted, ct)[0] == []
    assert tool_policy.select({}, granted, ct, override="off")[:3] == ([], "off", "client")


def test_select_list_narrows_never_widens():
    p = {"mcp_with_client_tools": json.dumps(["searxng", "SQLite__read_query", "not-granted"])}
    tools, mode, _ = tool_policy.select(p, [SEARCH, CRAWL, SQL], ANYTHINGLLM)
    assert tools == [SEARCH, SQL] and mode == "list"
    p = {"mcp_with_client_tools": [{"server": "crawl4ai", "tools": ["crawl"]}]}
    assert tool_policy.select(p, [SEARCH, CRAWL], ANYTHINGLLM)[0] == [CRAWL]
    # a name the persona isn't granted can't sneak in, from either level
    assert tool_policy.select({}, [SEARCH], ANYTHINGLLM, override=["kokoro-tts"])[0] == []


def test_dedupe_by_purpose_keeps_what_the_client_lacks():
    granted = [SEARCH, CRAWL, SQL, MUSIC, TTS]
    kept, mode, _ = tool_policy.select({}, granted, ANYTHINGLLM, override="dedupe")
    # AnythingLLM has web search + scraping: searxng and crawl4ai go; SQL, music, speech stay
    assert kept == [SQL, MUSIC, TTS] and mode == "dedupe"
    assert "web search" in tool_policy.duplicates(SEARCH, ANYTHINGLLM)
    assert tool_policy.duplicates(MUSIC, ANYTHINGLLM) is None
    # a client with no web tools keeps searxng
    files_only = _client_tools(("read_file", "Read a file"))
    assert SEARCH in tool_policy.select({}, granted, files_only, override="dedupe")[0]


def test_dedupe_by_name_ignores_case_and_punctuation():
    ct = _client_tools(("Generate-Music", "make a tune"))
    assert "same name" in tool_policy.duplicates(MUSIC, ct)


def test_dedupe_is_stable_for_the_same_client_tools():
    granted = [SEARCH, CRAWL, SQL, MUSIC, TTS]
    runs = [tool_policy.select({}, granted, ANYTHINGLLM, override="dedupe")[0] for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


def test_purposes():
    assert tool_policy.purposes("generate_image", "Generate an image with ComfyUI") \
        == {"image generation"}
    assert tool_policy.purposes("stable-audio__generate_sfx", "Generate a sound effect") \
        == {"music / sound"}
    assert tool_policy.purposes("SQLite__append_insight", "Add a business insight to the memo") \
        == {"sql"}
    assert tool_policy.purposes("memo__add_note", "Add a note to the memo") == set()


def test_describe():
    assert tool_policy.describe(None) == "default (all)"
    assert tool_policy.describe("all") == "all"
    assert tool_policy.describe("dedupe") == "auto-dedupe"
    assert tool_policy.describe('["searxng"]') == "only searxng"


class _Svc:
    class tool_registry:                                          # noqa: N801
        tools = {}

    config_store = None


@pytest.mark.parametrize("value,clean", [
    (None, None), ("", None), ("default", None), ("off", "off"), ("none", "off"),
    (" all ", "all"), ("dedupe", "dedupe"), (True, "all"), (False, "off"),
    ("searxng, crawl4ai", ["searxng", "crawl4ai"]), ('["searxng"]', ["searxng"]),
    (["searxng"], ["searxng"]), ([], "off"),
    ([{"server": "crawl4ai", "tools": ["crawl"]}], [{"server": "crawl4ai", "tools": ["crawl"]}])])
def test_validate_accepts(value, clean, monkeypatch):
    monkeypatch.setattr(persona_rules, "known_mcp", lambda svc: (set(), set()))
    out, errors, _w = persona_rules.validate(_Svc(), {"mcp_with_client_tools": value})
    assert not errors and out["mcp_with_client_tools"] == clean


@pytest.mark.parametrize("value", [5, {"server": "x"}, [5], "[oops"])
def test_validate_refuses(value, monkeypatch):
    monkeypatch.setattr(persona_rules, "known_mcp", lambda svc: (set(), set()))
    _out, errors, _w = persona_rules.validate(_Svc(), {"mcp_with_client_tools": value})
    assert errors and "mcp_with_client_tools" in errors[0]


def test_validate_warns_on_unknown_names(monkeypatch):
    monkeypatch.setattr(persona_rules, "known_mcp", lambda svc: ({"searxng"}, set()))
    _out, errors, warnings = persona_rules.validate(_Svc(), {"mcp_with_client_tools": "searxng, comfy"})
    assert not errors and warnings and "comfy" in warnings[0]


# -- integration: what the model is offered ------------------------------------------------

class StreamPool:
    """One model call: optional prompt-progress frames, then thinking + content
    and (optionally) a client tool call."""

    def __init__(self, frames=(), tool_call=None, thinking="I should check the file.",
                 content="Here it is."):
        self.frames, self.tool_call = list(frames), tool_call
        self.thinking, self.content = thinking, content
        self.seen_tools = []

    def available_models(self):
        return {"local-a": ["o1"]}

    def backend_info(self, m):
        return ({"name": "o1", "type": "openai-compatible", "flavor": "llamacpp",
                 "url": "http://o"} if m == "local-a" else None)

    def backend_status(self):
        return []

    async def loaded_models(self):
        return set()

    def active_calls(self):
        return []

    def _res(self):
        return ChatResult(content=self.content, thinking=self.thinking,
                          tool_calls=[self.tool_call] if self.tool_call else [],
                          prompt_tokens=30000, completion_tokens=12,
                          finish_reason="tool_calls" if self.tool_call else "stop")

    async def chat(self, model, messages, tools=None, **kw):
        self.seen_tools.append([t["function"]["name"] for t in tools or []])
        return self._res(), "o1"

    async def chat_stream(self, model, messages, tools=None, **kw):
        self.seen_tools.append([t["function"]["name"] for t in tools or []])
        for f in self.frames:
            yield {"content": "", "done": False, "progress": {"prefill": f}}
            await asyncio.sleep(0)
        res = self._res()
        yield {"content": "", "thinking": res.thinking, "done": False}
        yield {"content": res.content, "done": False}
        yield res.done_frame()


@pytest.fixture()
def rig(app, client):
    svc = app.state.services
    persona = next(p for p in svc.personas.list(enabled_only=True))
    svc.personas.upsert(persona["virtual_name"],
                        preferred_mcp_tools=json.dumps(["searxng", "acestep-music"]),
                        mcp_tools_in_direct=1, quiet_narration=0, mcp_with_client_tools=None)
    svc.tool_registry.tools[SEARCH.name] = SEARCH
    svc.tool_registry.tools[MUSIC.name] = MUSIC
    old_pool = svc.pool

    def base(raw):          # in the raw config, so later saves keep it
        raw["clients"] = [{"name": "anythingllm", "key": "allm-key"},
                          {"name": "cline", "key": "cline-key", "mcp_with_client_tools": "off"}]
        raw.setdefault("agent_brain", {})["direct_stream_heartbeat_seconds"] = 0
    svc.config_store.save(base)

    def use(pool, stream=True, **fields):
        svc.pool = pool
        svc.config_store.save(lambda raw: raw["agent_brain"].update(direct_stream=stream))
        if fields:
            svc.personas.upsert(persona["virtual_name"], **fields)
    use.persona = persona["virtual_name"]
    use.svc = svc
    yield use
    svc.pool = old_pool


CLIENT_TOOLS = [{"type": "function", "function": {"name": "filesystem-read-file",
                                                   "parameters": {"type": "object"}}}]
BOTH = ["filesystem-read-file", "searxng__search", "acestep-music__generate_music"]


def _agent_post(client, model, stream=True, tools=CLIENT_TOOLS, key=None):
    body = {"model": model, "stream": stream, "think": True,
            "messages": [{"role": "user", "content": "read notes.txt"}]}
    if tools is not None:
        body["tools"] = tools
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return client.post("/api/chat", json=body, headers=headers)


def _lines(r):
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


def _joined(lines, key):
    return "".join((ln.get("message") or {}).get(key) or "" for ln in lines)


def test_default_merges_everything_as_before(rig, client):
    pool = StreamPool()
    rig(pool)
    thinking = _joined(_lines(_agent_post(client, rig.persona)), "thinking")
    assert pool.seen_tools[0] == BOTH
    assert "2 persona MCP tool(s) available alongside 1 client tool(s)\n" in thinking


def test_persona_off_offers_only_the_client_tools(rig, client):
    pool = StreamPool()
    rig(pool, mcp_with_client_tools="off")
    thinking = _joined(_lines(_agent_post(client, rig.persona)), "thinking")
    assert pool.seen_tools[0] == ["filesystem-read-file"]
    assert "2 persona MCP tool(s) left out" in thinking and "off" in thinking
    ev = rig.svc.db.query_one("SELECT guardrail_events FROM request_log ORDER BY id DESC LIMIT 1")
    assert "left out" in (ev["guardrail_events"] or "")


def test_persona_list_and_plain_chat(rig, client):
    pool = StreamPool()
    rig(pool, stream=False, mcp_with_client_tools=json.dumps(["acestep-music"]))
    _agent_post(client, rig.persona, stream=False)
    assert pool.seen_tools[-1] == ["filesystem-read-file", "acestep-music__generate_music"]
    rig(pool, stream=False, execution_mode="direct")
    _agent_post(client, rig.persona, stream=False, tools=None)              # no client tools
    assert pool.seen_tools[-1] == ["searxng__search", "acestep-music__generate_music"]


def test_client_key_override_wins_over_the_persona(rig, client):
    pool = StreamPool()
    rig(pool, mcp_with_client_tools="all")
    _agent_post(client, rig.persona, key="cline-key")                        # cline: off
    assert pool.seen_tools[-1] == ["filesystem-read-file"]
    _agent_post(client, rig.persona, key="allm-key")                         # no override
    assert pool.seen_tools[-1] == BOTH
    router_ops.set_client_tool_policy(rig.svc, "anythingllm", "acestep-music, kokoro-tts")
    lines = _lines(_agent_post(client, rig.persona, key="allm-key"))
    assert pool.seen_tools[-1] == ["filesystem-read-file", "acestep-music__generate_music"]
    assert "client anythingllm: only acestep-music, kokoro-tts" in _joined(lines, "thinking")


def test_keyless_override(rig, client):
    pool = StreamPool()
    rig(pool)
    router_ops.set_client_tool_policy(rig.svc, "keyless", "off")
    lines = _lines(_agent_post(client, rig.persona))
    assert pool.seen_tools[-1] == ["filesystem-read-file"]
    assert "keyless clients: off" in _joined(lines, "thinking")
    _agent_post(client, rig.persona, key="allm-key")      # a keyed client isn't keyless
    assert pool.seen_tools[-1] == BOTH
    router_ops.set_client_tool_policy(rig.svc, "keyless", "default")
    _agent_post(client, rig.persona)
    assert pool.seen_tools[-1] == BOTH


def test_dedupe_end_to_end(rig, client):
    pool = StreamPool()
    rig(pool, mcp_with_client_tools="dedupe")
    _agent_post(client, rig.persona, tools=[{"type": "function", "function": {
        "name": "web-browsing", "description": "Searches the internet"}}])
    assert pool.seen_tools[-1] == ["web-browsing", "acestep-music__generate_music"]


def test_set_client_tool_policy_validates(rig):
    with pytest.raises(router_ops.OpError):
        router_ops.set_client_tool_policy(rig.svc, "nobody", "off")
    with pytest.raises(router_ops.OpError):
        router_ops.set_client_tool_policy(rig.svc, "anythingllm", "[oops")


def test_clients_api_round_trip(rig, client):
    r = client.patch("/admin/api/setup/clients/anythingllm",
                     json={"mcp_with_client_tools": "acestep-music, searxng"}).json()
    assert r["ok"]
    view = {c["name"]: c for c in client.get("/admin/api/setup/clients").json()["clients"]}
    assert view["anythingllm"]["mcp_with_client_tools"] == ["acestep-music", "searxng"]
    r = client.post("/admin/api/setup/client_auth",
                    json={"keyless_mcp_with_client_tools": "dedupe"}).json()
    assert r["ok"] and r["auth"]["keyless_mcp_with_client_tools"] == "dedupe"
    bad = client.patch("/admin/api/setup/clients/anythingllm", json={"mcp_with_client_tools": 5})
    assert bad.status_code == 400


def test_policy_is_stable_across_agent_steps(rig, client):
    """Same tool list on every step: the prompt prefix stays cached."""
    pool = StreamPool(tool_call={"id": "t1", "name": "filesystem-read-file",
                                 "arguments": {"path": "notes.txt"}})
    rig(pool, mcp_with_client_tools="dedupe")
    for _ in range(3):
        _agent_post(client, rig.persona)
    assert pool.seen_tools == [pool.seen_tools[0]] * 3


# -- 2. prompt-reading progress ------------------------------------------------------------

def _f(processed, total=40000, cache=0):
    return {"processed": processed, "total": total, "cache": cache, "time_ms": 1}


def test_reporter_first_frame_then_25_percent_steps():
    r = keepalive.PrefillReporter()
    shown = [r.line({"prefill": _f(p)}, 5) for p in (8192, 9000, 12000, 16384, 24576, 32768)]
    pct = [s.split("%")[0].rsplit(" ", 1)[1] if s else None for s in shown]
    # 20% (first), 22 same bucket, 30 crosses 25, 40 same, 61 crosses 50, 81 crosses 75
    assert pct == ["20", None, "30", None, "61", "81"]
    assert all(s.startswith("⏳ reading prompt… ") for s in shown if s)


def test_reporter_bounds_and_skips_small_prompts():
    r = keepalive.PrefillReporter(max_lines=2)
    lines = [r.line({"prefill": _f(p)}, 1) for p in (1000, 11000, 21000, 31000)]
    assert sum(1 for x in lines if x) == 2
    small = keepalive.PrefillReporter()
    assert small.line({"prefill": _f(2048, total=3000)}, 1) is None       # < 4k to read
    cached = keepalive.PrefillReporter()   # 60k prompt, 57k cached: only 3k to read
    assert cached.line({"prefill": _f(58000, total=60000, cache=57000)}, 1) is None
    assert keepalive.PrefillReporter(enabled=False).line({"prefill": _f(9000)}, 1) is None
    assert keepalive.PrefillReporter().line({"prefill": _f(9000), "tool_chars": 40}, 1) is None
    r.reset()
    assert r.line({"prefill": _f(9000)}, 1)


def test_prefill_line_wording_counts_new_tokens():
    s = keepalive.prefill_line(processed=26384, total=60000, cache=18000, elapsed=12)
    assert s == "⏳ reading prompt… 19% (8.4k / 42.0k new tokens, 18.0k cached) · 12s\n"


def test_wait_pacer_first_line_at_10s():
    p = keepalive.Pacer(60, first=10)
    assert [t for t in range(0, 130, 5) if p.due(t)] == [10, 20, 40, 60, 120]
    old = keepalive.Pacer(60)
    assert [t for t in range(0, 130, 5) if old.due(t)] == [30, 60, 120]


def test_heartbeat_wrapper_progress_events():
    async def src():
        yield {"content": "", "done": False, "progress": {"prefill": _f(8192)}}
        yield {"content": "hi", "done": False}
        yield {"content": "", "done": True}

    async def run(events):
        prog = {}
        out = []
        async for kind, payload in keepalive.stream_with_heartbeat(
                src(), 0, 0, 0, prog, progress_events=events):
            out.append((kind, dict(prog) if kind == "progress" else payload))
        return out
    with_ev = asyncio.run(run(True))
    assert with_ev[0][0] == "progress" and with_ev[0][1]["prefill"]["processed"] == 8192
    assert [k for k, _ in with_ev] == ["progress", "chunk", "chunk"]
    assert [k for k, _ in asyncio.run(run(False))] == ["chunk", "chunk"]       # unchanged


def test_progress_lines_stream_before_the_first_token(rig, client):
    pool = StreamPool(frames=[_f(8192), _f(16384), _f(24576), _f(32768)])
    rig(pool)
    lines = _lines(_agent_post(client, rig.persona))
    thinking = _joined(lines, "thinking")
    shown = [ln for ln in thinking.splitlines() if ln.startswith("⏳ reading prompt")]
    assert len(shown) == 4 and "20%" in shown[0] and "81%" in shown[-1]
    # all before the model's own reasoning
    assert thinking.index(shown[-1]) < thinking.index("I should check the file.")


def test_quiet_persona_gets_no_progress_lines(rig, client):
    pool = StreamPool(frames=[_f(8192), _f(24576)])
    rig(pool, quiet_narration=1)
    thinking = _joined(_lines(_agent_post(client, rig.persona)), "thinking")
    assert "reading prompt" not in thinking


def test_progress_lines_can_be_turned_off(rig, client):
    pool = StreamPool(frames=[_f(8192), _f(24576)])
    rig(pool)
    cfg = rig.svc.config_store.config.agent_brain
    old, cfg.prefill_progress_lines = cfg.prefill_progress_lines, False
    try:
        thinking = _joined(_lines(_agent_post(client, rig.persona)), "thinking")
    finally:
        cfg.prefill_progress_lines = old
    assert "reading prompt" not in thinking


# -- 3. AnythingLLM still renders; history scrubs exactly as before ------------------------

def test_anythingllm_agent_step_renders(rig, client):
    pool = StreamPool(frames=[_f(8192), _f(20000)], content="",
                      tool_call={"id": "t1", "name": "filesystem-read-file",
                                 "arguments": {"path": "notes.txt"}})
    rig(pool)
    lines = _lines(_agent_post(client, rig.persona))
    content = _joined(lines, "content")
    thinking = _joined(lines, "thinking")
    assert "⏳" not in content and "🔧" not in content and "reading prompt" not in content
    assert "I should check the file." in thinking
    done = lines[-1]
    assert done["done"] is True
    tcs = done["message"]["tool_calls"]
    assert [t["function"]["name"] for t in tcs] == ["filesystem-read-file"]
    assert tcs[0]["function"]["arguments"] == {"path": "notes.txt"}


def test_scrub_drops_progress_lines_exactly_like_before():
    """What a client echoes back must scrub to the same text v0.141 sent back,
    or the Strata/llama.cpp prompt cache misses on the next step."""
    header = ("⚙️ llamacpp · qwen — streaming… [think: high (client) · Foundry 0.142.0 · "
              "req abcd1234 · arrived 04:00:16]\n")
    model = "The user wants the file.\n\nI'll read it first."
    before = header + model                                               # v0.141 stream
    after = (header + "🔧 58 persona MCP tool(s) left out — the client brought 1 of its own "
             "(persona setting: MCP tools with client tools = off)\n"
             + keepalive.prefill_line(8192, 40000, 0, 7)
             + keepalive.prefill_line(24576, 40000, 0, 21)
             + keepalive.status_line("llamacpp · qwen", 10, "sent — waiting")
             + model)
    assert prompts.scrub_router_lines(after) == prompts.scrub_router_lines(before) == model

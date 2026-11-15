"""Main-model mode (v0.181.140): a persona with no router model in front of it.
The first model on its route that is free reads the request, runs the tools and
answers; a busy local model is skipped at once; a model that fails hands the
request to the next one; a conversation stays on the model it landed on. Plus the
one-chat-persona setup that moves an install to it."""

import asyncio
import json
import types

import pytest

from foundry_router import lanes, main_route, persona_rules, persona_setup
from foundry_router.brain.agent import AgentRunner, RequestContext
from foundry_router.config import AgentBrainConfig, GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.facade.ollama_api import _run_events
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.personas import PersonaStore
from foundry_router.pool.base import AllBackendsFailed
from foundry_router.pool.protocols import ChatResult
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.sync import ToolDef, ToolRegistry
from foundry_router.usage import MeridianUsage, RequestLogger


# -- the order of the route for one request (main_route.order) --------------------------

def _svc(monkeypatch, busy=(), paid=(), idle=None):
    """A stand-in pool: which models are busy, which are paid, how long idle."""
    monkeypatch.setattr(lanes, "is_local", lambda svc, m: m not in paid)
    monkeypatch.setattr(lanes, "model_free", lambda svc, m: m not in busy)
    monkeypatch.setattr(main_route, "idle_seconds", lambda svc, m, now=None: (idle or {}).get(m, 1e9))
    return types.SimpleNamespace()


ROUTE = ["strata", "small", "haiku", "sonnet"]
PAID = ("haiku", "sonnet")


def test_a_free_main_model_takes_the_request(monkeypatch):
    svc = _svc(monkeypatch, paid=PAID)
    out, notes = main_route.order(svc, ROUTE, est_tokens=900, home=None)
    assert out == ROUTE and notes == []


def test_a_busy_main_model_is_skipped_at_once(monkeypatch):
    svc = _svc(monkeypatch, busy=("strata",), paid=PAID)
    out, notes = main_route.order(svc, ROUTE, est_tokens=900, home=None)
    assert out == ["small", "haiku", "sonnet", "strata"]        # bottom up; the busy one last
    assert "no waiting" in notes[0]
    # both local models busy: the cheapest paid model answers
    svc = _svc(monkeypatch, busy=("strata", "small"), paid=PAID)
    out, _ = main_route.order(svc, ROUTE, est_tokens=900, home=None)
    assert out[:2] == ["haiku", "sonnet"] and set(out[2:]) == {"strata", "small"}
    # nothing else on the route: it queues on the busy model as before
    out, notes = main_route.order(svc, ["strata"], est_tokens=900, home=None)
    assert out == ["strata"] and notes == []


def test_a_conversation_stays_where_it_landed_until_the_main_model_is_idle(monkeypatch):
    # overflowed to haiku; strata free but idle for 2 s only
    svc = _svc(monkeypatch, paid=PAID, idle={"strata": 2.0})
    out, notes = main_route.order(svc, ROUTE, est_tokens=900, home="haiku")
    assert out[0] == "haiku" and "idle for less than 5 s" in notes[0]
    # idle 6 s and a short conversation: back to the main model
    svc = _svc(monkeypatch, paid=PAID, idle={"strata": 6.0})
    out, notes = main_route.order(svc, ROUTE, est_tokens=900, home="haiku")
    assert out == ROUTE and "moves back" in notes[0]
    # idle, but the conversation is long: the main model would re-read all of it
    out, notes = main_route.order(svc, ROUTE, est_tokens=30_000, home="haiku")
    assert out[0] == "haiku" and "30,000 tokens" in notes[0]
    # the settings decide
    out, _ = main_route.order(svc, ROUTE, est_tokens=30_000, home="haiku", return_max_tokens=50_000)
    assert out == ROUTE
    out, _ = main_route.order(svc, ROUTE, est_tokens=900, home="haiku", return_idle_seconds=10)
    assert out[0] == "haiku"
    # still busy: stays
    svc = _svc(monkeypatch, busy=("strata",), paid=PAID)
    out, notes = main_route.order(svc, ROUTE, est_tokens=900, home="small")
    assert out[0] == "small" and "strata is busy" in notes[0]
    # a home that left the route is forgotten
    out, _ = main_route.order(_svc(monkeypatch, paid=PAID), ROUTE, est_tokens=900, home="gone")
    assert out == ROUTE


def test_a_model_too_small_for_the_conversation_is_left_out():
    reg = types.SimpleNamespace(get=lambda m: {"strata": {"context_length": 262144},
                                               "small": {"context_length": 32768}}.get(m))
    keep, notes = main_route.fit(reg, ["strata", "small", "unknown"], 40_000)
    assert keep == ["strata", "unknown"] and "small is left out" in notes[0]
    assert main_route.fit(reg, ["small"], 40_000) == (["small"], [])     # never empties the route


def test_idle_seconds_reads_the_backends_last_call():
    st = types.SimpleNamespace(busy=0, last_done=100.0)
    svc = types.SimpleNamespace(pool=types.SimpleNamespace(
        backend_info=lambda m: {"name": "b"}, backends={"b": st}))
    assert main_route.idle_seconds(svc, "m", now=107.5) == 7.5
    st.busy = 1
    assert main_route.idle_seconds(svc, "m", now=107.5) == 0.0
    st.busy, st.last_done = 0, None
    assert main_route.idle_seconds(svc, "m") > 1e6                       # never used: idle


def test_the_pool_stamps_when_a_backend_finished():
    import inspect

    from foundry_router.pool import internal
    src = inspect.getsource(internal)
    assert src.count("s.last_done = time.monotonic()") == 2               # chat and chat_stream


# -- the loop (agent.run_main) ----------------------------------------------------------------

class FakeMCP:
    servers = {"searxng": None}

    def __init__(self):
        self.calls = []

    def executes_code(self, server):
        return False

    async def call_tool(self, server, tool, args):
        self.calls.append((server, tool, args))
        return "search result: foundry router is an LLM router"

    async def list_all(self):
        return {}


class Pool:
    """Two local models on two backends; each has its own script."""

    def __init__(self, scripts):
        self.scripts = {m: list(s) for m, s in scripts.items()}
        self.asked = []
        self.backends = {}

    def available_models(self):
        return {m: [m + "-be"] for m in self.scripts}

    def backend_info(self, m):
        return ({"name": m + "-be", "type": "ollama", "url": "http://x", "api_key": None}
                if m in self.scripts else None)

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096, think=None, fmt=None):
        self.asked.append((model, tools is not None))
        step = self.scripts[model].pop(0)
        if isinstance(step, Exception):
            raise step
        return step, model + "-be"


class NoBrain:
    """A router model that must never be asked."""

    def __init__(self, skip=None):
        self.cfg = AgentBrainConfig()
        self._skip = skip

    def skip_reason(self, aux=False):
        return self._skip

    async def chat(self, *a, **k):
        raise AssertionError("the router model was called in main-model mode")

    complete = chat


def _tool(name, **args):
    return ChatResult(tool_calls=[{"id": "1", "name": name, "arguments": args}])


def _make(tmp_path, scripts, pins=("main", "small"), brain=None):
    db = Database(tmp_path / "m.sqlite")
    registry = ModelRegistry(db)
    for i, m in enumerate(scripts):
        registry.upsert_auto(m, source="discovery", relative_cost_tier="free")
        registry.upsert_benchmark(m, "general_chat", 70 - i, "measured", "independent", confidence=0.9)
    mcp = FakeMCP()
    tools = ToolRegistry(db, registry, mcp)
    tools.tools["searxng_web_search"] = ToolDef(
        name="searxng_web_search", kind="mcp", description="[MCP:searxng] search",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        server="searxng", mcp_tool="search")
    pool = Pool(scripts)
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    runner = AgentRunner(brain or NoBrain(), pool, tools, registry,
                         GuardrailEngine(GuardrailsConfig(), db, meridian), meridian)
    ctx = RequestContext(
        persona={"virtual_name": "Foundry-Chat", "benchmark_category": "general_chat",
                 "execution_mode": "main", "pinned_models": json.dumps(list(pins)),
                 "preferred_mcp_tools": json.dumps(["searxng"])},
        messages=[{"role": "user", "content": "what is foundry router"}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "main", "q"))
    return runner, ctx, pool, mcp


def _run(gen):
    async def go():
        return [ev async for ev in gen]
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def _no_homes():
    lanes._PINS.clear()
    yield
    lanes._PINS.clear()


def test_the_main_model_runs_the_tools_and_answers(tmp_path):
    runner, ctx, pool, mcp = _make(tmp_path, {
        "main": [_tool("searxng_web_search", query="foundry router"), ChatResult(content="It is a router.")],
        "small": []})
    evs = _run(runner.run_main(ctx))
    assert [e.text for e in evs if e.kind == "answer"] == ["It is a router."]
    assert [m for m, _ in pool.asked] == ["main", "main"] and len(mcp.calls) == 1
    assert any("main takes this request with 1 tool" in e.text for e in evs if e.kind == "think")
    # the conversation's home is the model that answered
    assert main_route.home_get(main_route.conv_key(ctx.persona, ctx.messages)) == "main"


def test_a_failed_model_hands_over_to_the_next_on_the_route(tmp_path):
    runner, ctx, pool, _ = _make(tmp_path, {
        "main": [AllBackendsFailed("main down")],
        "small": [ChatResult(content="Answered by the small model.")]})
    evs = _run(runner.run_main(ctx))
    assert [e.text for e in evs if e.kind == "answer"] == ["Answered by the small model."]
    assert [m for m, _ in pool.asked] == ["main", "small"]
    assert any("next model on this persona's route takes over" in e.text for e in evs)
    assert not [e for e in evs if e.kind in ("error", "brain_down")]
    assert main_route.home_get(main_route.conv_key(ctx.persona, ctx.messages)) == "small"


def test_every_model_failing_is_an_error_not_a_router_call(tmp_path):
    runner, ctx, pool, _ = _make(tmp_path, {
        "main": [AllBackendsFailed("main down")], "small": [AllBackendsFailed("small down")]})
    evs = _run(runner.run_main(ctx))
    assert evs[-1].kind == "error" and "could answer" in evs[-1].text and "small" in evs[-1].text


def test_a_busy_main_model_is_not_waited_for(tmp_path, monkeypatch):
    runner, ctx, pool, _ = _make(tmp_path, {"main": [], "small": [ChatResult(content="quick")]})
    monkeypatch.setattr(lanes, "model_free", lambda svc, m: m != "main")
    evs = _run(runner.run_main(ctx))
    assert [e.text for e in evs if e.kind == "answer"] == ["quick"]
    assert [m for m, _ in pool.asked] == ["small"]
    assert any("no waiting" in e.text for e in evs if e.kind == "think")


def test_the_route_is_the_whole_ladder(tmp_path):
    """Pins set a route: a model that is not on it is never used, even when it ranks."""
    runner, ctx, pool, _ = _make(tmp_path, {"main": [AllBackendsFailed("down")],
                                            "small": [ChatResult(content="x")],
                                            "other": [ChatResult(content="never")]},
                                 pins=("main",))
    evs = _run(runner.run_main(ctx))
    assert [m for m, _ in pool.asked] == ["main"] and evs[-1].kind == "error"


def test_the_step_cap_ends_in_an_answer_from_the_same_model(tmp_path):
    runner, ctx, pool, _ = _make(tmp_path, {
        "main": [_tool("searxng_web_search", query="a"), ChatResult(content="From what I found: a router.")],
        "small": [ChatResult(content="never")]})
    runner.brain.cfg.worker_tool_max_steps = 1
    evs = _run(runner.run_main(ctx))
    assert [e.text for e in evs if e.kind == "answer"] == ["From what I found: a router."]
    assert pool.asked == [("main", True), ("main", False)]             # the last call offers no tools


def test_router_model_off_sends_a_tool_persona_through_the_main_path(tmp_path):
    runner, ctx, pool, _ = _make(tmp_path, {"main": [ChatResult(content="ok")], "small": []},
                                 brain=NoBrain(skip="routing mode is passthrough"))
    ctx.persona["execution_mode"] = None                                 # an older persona
    evs = _run(runner.run_worker_tools(ctx))
    assert [e.text for e in evs if e.kind == "answer"] == ["ok"]


def test_dispatch_sends_main_mode_to_run_main():
    agent = types.SimpleNamespace(run=lambda c: "brain", run_pipeline=lambda c: "pipeline",
                                  run_worker_tools=lambda c: "worker", run_main=lambda c: "main")
    ctx = types.SimpleNamespace(persona={"preferred_mcp_tools": '["*"]'},
                                logger=types.SimpleNamespace(mode="main"))
    assert _run_events(types.SimpleNamespace(agent=agent), ctx) == "main"
    import inspect

    from foundry_router.facade import ollama_api
    src = inspect.getsource(ollama_api)
    entry = src[src.index('if exec_mode == "main" and not client_tools'):src.index("brain_skip = svc.brain.skip_reason()")]
    assert 'mode="main"' in entry and 'log_mode="main"' in entry         # with tools / without
    assert 'exec_mode in ("direct", "main")' in src                      # a client's own tools: direct
    assert src.index('if exec_mode == "main" and not client_tools') > src.index("if is_strict(persona):")


def test_main_is_a_valid_execution_mode():
    assert "main" in persona_rules.ENUMS["execution_mode"]


# -- the one-chat-persona setup ---------------------------------------------------------------

def _setup_svc(tmp_path, monkeypatch):
    db = Database(tmp_path / "p.sqlite")
    svc = types.SimpleNamespace(db=db, personas=PersonaStore(db), pool=types.SimpleNamespace())
    monkeypatch.setattr(persona_setup, "_is_local", lambda s, m: m.startswith("qwen"))
    svc.personas.upsert("Foundry-Chat", pinned_models=["qwen-big", "claude-haiku", "claude-sonnet"],
                        preferred_mcp_tools=["*"], mcp_tool_mode="finder")
    return svc


def test_the_plan_changes_nothing_and_counts_traffic(tmp_path, monkeypatch):
    svc = _setup_svc(tmp_path, monkeypatch)
    from foundry_router.db import utcnow
    for _ in range(3):
        svc.db.execute("INSERT INTO request_log (ts, persona, client_model, mode, status) VALUES (?,?,?,?,?)",
                       (utcnow(), "Foundry-Agent", "Foundry-Agent", "agent", "ok"))
    p = persona_setup.plan(svc)
    assert p["applied"] is False and p["main"]["is_main"] is False
    assert [v["name"] for v in p["variants"]] == ["Foundry-Local", "Foundry-Max"]
    assert p["variants"][0]["route"] == ["qwen-big"]
    assert p["variants"][1]["route"] == ["claude-haiku", "claude-sonnet", "qwen-big"]
    agent = next(r for r in p["retire"] if r["name"] == "Foundry-Agent")
    assert agent["requests"] == 3 and agent["enabled"] and agent["last_used"]
    assert svc.personas.get("Foundry-Local") is None                     # read-only
    assert (svc.personas.get("Foundry-Chat")["execution_mode"] or "") != "main"


def test_apply_switches_off_and_never_deletes(tmp_path, monkeypatch):
    svc = _setup_svc(tmp_path, monkeypatch)
    out = persona_setup.apply(svc)
    assert out["ok"] and out["changes"]
    chat = svc.personas.get("Foundry-Chat")
    assert chat["execution_mode"] == "main"
    assert json.loads(chat["pinned_models"]) == ["qwen-big", "claude-haiku", "claude-sonnet"]   # his route is kept
    local = svc.personas.get("Foundry-Local")
    assert json.loads(local["pinned_models"]) == ["qwen-big"] == json.loads(local["model_allowlist"])
    assert json.loads(local["guardrail_overrides"])["max_paid_calls_per_request"] == 0
    assert local["execution_mode"] == "main" and json.loads(local["preferred_mcp_tools"]) == ["*"]
    assert json.loads(svc.personas.get("Foundry-Max")["pinned_models"]) == ["claude-haiku", "claude-sonnet", "qwen-big"]
    for name in persona_setup.RETIRE:
        p = svc.personas.get(name)
        if p is not None:
            assert p["enabled"] == 0, name                               # off, still there
    assert svc.personas.get("Foundry-Agent") is not None
    assert persona_setup.plan(svc)["applied"] is True
    # a second apply changes nothing and keeps an edited variant
    svc.personas.upsert("Foundry-Local", description="mine")
    assert persona_setup.apply(svc)["changes"] == []
    assert svc.personas.get("Foundry-Local")["description"] == "mine"
    # personas outside the list are untouched
    assert svc.personas.get("claude-cline-act")["enabled"] == 1


def test_only_the_ticked_personas_are_retired(tmp_path, monkeypatch):
    svc = _setup_svc(tmp_path, monkeypatch)
    persona_setup.apply(svc, retire=["Foundry-Agent", "claude-cline-act"])
    assert svc.personas.get("Foundry-Agent")["enabled"] == 0
    assert svc.personas.get("Foundry-Coding")["enabled"] == 1
    assert svc.personas.get("claude-cline-act")["enabled"] == 1          # not a retirable name
    assert list(persona_setup.retired(svc.db)) == ["Foundry-Agent"]


def test_an_old_name_is_answered_by_the_main_persona(tmp_path, monkeypatch):
    svc = _setup_svc(tmp_path, monkeypatch)
    assert svc.personas.resolve("Foundry-Agent")["virtual_name"] == "Foundry-Agent"
    persona_setup.apply(svc)
    assert svc.personas.resolve("Foundry-Agent")["virtual_name"] == "Foundry-Chat"
    assert svc.personas.resolve("foundry-agent:latest")["virtual_name"] == "Foundry-Chat"
    assert svc.personas.get("Foundry-Agent")["virtual_name"] == "Foundry-Agent"      # editing sees the row
    # a persona switched off by hand (not retired) is not redirected
    svc.personas.upsert("claude-cline-act", enabled=0)
    assert svc.personas.resolve("claude-cline-act")["virtual_name"] == "claude-cline-act"
    assert svc.personas.resolve("nope") is None
    # restore: it answers for itself again
    assert persona_setup.restore(svc, "Foundry-Agent") is True
    assert svc.personas.resolve("Foundry-Agent")["virtual_name"] == "Foundry-Agent"
    assert svc.personas.get("Foundry-Agent")["enabled"] == 1


def test_removed_personas_stay_removed_and_keep_answering(tmp_path, monkeypatch):
    svc = _setup_svc(tmp_path, monkeypatch)
    persona_setup.apply(svc)
    persona_setup.restore(svc, "Foundry-Coding")                         # switched back on: kept
    gone = persona_setup.remove_retired(svc)
    assert "Foundry-Agent" in gone and "Foundry-Coding" not in gone
    assert svc.personas.get("Foundry-Agent") is None
    assert svc.personas.resolve("Foundry-Agent")["virtual_name"] == "Foundry-Chat"
    # a restart seeds the starters again, but not the removed ones
    db2 = Database(tmp_path / "p.sqlite")
    store = PersonaStore(db2)
    assert store.get("Foundry-Agent") is None and store.get("Foundry-Coding") is not None
    # a starter deleted by hand stays deleted too
    store.delete("claude-cline-plan")
    persona_setup.forget(db2, "claude-cline-plan")
    assert PersonaStore(Database(tmp_path / "p.sqlite")).get("claude-cline-plan") is None


def test_the_admin_page_has_the_controls():
    from pathlib import Path
    html = (Path(persona_setup.__file__).parent / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    assert '<select id="pe-mode"><option value="main">main model (no router)</option><option value="">router model picks</option>' in html
    assert '["passthrough", "off — no router model"]' in html
    for fid in ("brain-mri", "brain-mrt", "brain-mmt"):
        assert f'id="{fid}"' in html
    assert "main_return_idle_seconds: Math.max(0, +$(\"#brain-mri\").value || 0)" in html
    card = html[html.index("async function personaSetupCard() {"):html.index("/* ---------------------------------------------------------------- help -- */")]
    assert 'api("personas/setup")' in card and 'api("personas/setup", {method: "POST", body})' in card
    assert "confirm(`Delete the retired personas for good?" in card
    assert '<div id="ps-setup"></div>' in html and "setTimeout(personaSetupCard, 0);" in html


def test_the_setup_routes(tmp_path, monkeypatch):
    import inspect

    from foundry_router.ui import routes
    src = inspect.getsource(routes)
    assert '@router.get("/admin/api/personas/setup")' in src and '@router.post("/admin/api/personas/setup")' in src
    assert "asyncio.to_thread(persona_setup.plan" in src                 # a 30-day count, off the event loop
    assert "persona_setup.forget(svc.db, name)" in src
    for key in ("main_return_idle_seconds", "main_return_max_tokens", "main_max_tries"):
        assert f'"{key}"' in src
        assert key in AgentBrainConfig.model_fields


# -- the answer streams word by word (v0.181.141) -----------------------------------------------

class StreamPool(Pool):
    """Each script step is a list of stream chunks (or an exception)."""

    async def chat_stream(self, model, messages, tools=None, options=None, keep_alive=None,
                          think=None, max_tokens=None, fmt=None):
        self.asked.append((model, tools is not None))
        step = self.scripts[model].pop(0)
        if isinstance(step, Exception):
            raise step
        for chunk in step:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def _say(*pieces, think=""):
    out = [{"content": "", "thinking": think, "done": False}] if think else []
    out += [{"content": p, "thinking": "", "done": False} for p in pieces]
    return out + [{"done": True, "content": "", "prompt_tokens": 10, "completion_tokens": 5}]


def _call(name, text="", **args):
    out = [{"content": text, "thinking": "", "done": False}] if text else []
    return out + [{"done": True, "content": "",
                   "tool_calls": [{"id": "1", "name": name, "arguments": args}]}]


def _stream(tmp_path, scripts, **kw):
    runner, ctx, pool, mcp = _make(tmp_path, scripts, **kw)
    sp = StreamPool(scripts)
    runner.pool = sp
    return runner, ctx, sp, mcp


def test_the_answer_goes_out_word_by_word(tmp_path):
    runner, ctx, pool, _ = _stream(tmp_path, {
        "main": [_say("RAM is ", "system memory. ", "VRAM is on the GPU.", think="comparing the two")],
        "small": []})
    evs = _run(runner.run_main(ctx))
    kinds = [e.kind for e in evs]
    assert [e.text for e in evs if e.kind == "answer_delta"] == ["RAM is ", "system memory. ", "VRAM is on the GPU."]
    assert "answer" not in kinds                                         # not sent a second time, whole
    assert [e.text for e in evs if e.kind == "think_raw"] == ["comparing the two"]
    assert kinds.index("think_raw") < kinds.index("answer_delta")
    row = ctx.logger.models_used[-1]
    assert row["model"] == "main" and row["prompt_tokens"] == 10         # the call is still accounted


def test_tools_then_a_streamed_answer_with_the_tool_trail(tmp_path):
    runner, ctx, pool, mcp = _stream(tmp_path, {
        "main": [_call("searxng_web_search", "Let me look that up.", query="foundry router"),
                 _say("It is ", "an LLM router.")],
        "small": []})
    ctx.persona["expose_tool_trail"] = 1
    evs = _run(runner.run_main(ctx))
    deltas = [e.text for e in evs if e.kind == "answer_delta"]
    assert deltas[:4] == ["Let me look that up.", "\n\n", "It is ", "an LLM router."]
    assert len(deltas) == 5 and "search" in deltas[4]                    # the trail follows the answer
    assert len(mcp.calls) == 1 and not [e for e in evs if e.kind == "answer"]


def test_a_stream_that_breaks_hands_over_to_the_next_model(tmp_path):
    runner, ctx, pool, _ = _stream(tmp_path, {
        "main": [[{"content": "Half an ans", "thinking": "", "done": False}, RuntimeError("socket closed")]],
        "small": [_say("The whole answer.")]})
    evs = _run(runner.run_main(ctx))
    assert [e.text for e in evs if e.kind == "answer_delta"] == ["Half an ans", "\n\n", "The whole answer."]
    assert [m for m, _ in pool.asked] == ["main", "small"]
    assert not [e for e in evs if e.kind == "error"]


def test_a_reviewed_persona_and_the_switch_keep_the_answer_whole(tmp_path):
    """Switched off, the answer arrives whole. A persona with a review pass never
    streams (the review may change the answer): checked in the source below."""
    runner, ctx, pool, _ = _stream(tmp_path, {"main": [ChatResult(content="Whole answer.")], "small": []})
    runner.brain.cfg.stream_answers = False
    evs = _run(runner.run_main(ctx))
    assert [e.text for e in evs if e.kind == "answer"] == ["Whole answer."]
    assert not [e for e in evs if e.kind == "answer_delta"]
    import inspect
    src = inspect.getsource(AgentRunner._worker_tool_loop)
    assert 'and not (ctx.persona or {}).get("review_enabled"))' in src
    assert "stream_answer = bool(on_fail is not None" in src            # main-model mode only


def test_the_chat_stream_carries_the_pieces():
    import inspect

    from foundry_router import evalharness
    from foundry_router.facade import ollama_api
    src = inspect.getsource(ollama_api._agent_events_to_chat_chunks)
    assert 'elif ev.kind == "answer_delta":\n                deltas.append(ev.text)\n                yield tr.chat_chunk(model_name, ev.text)' in src
    assert 'answers.insert(0, "".join(deltas))' in src                  # the route footer and the cache see it
    assert 'if ev.kind == "answer_delta":' in inspect.getsource(evalharness.EvalHarness._answer_for)
    assert AgentBrainConfig().stream_answers is True
    from pathlib import Path
    html = (Path(persona_setup.__file__).parent / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="brain-sa"' in html and 'stream_answers: $("#brain-sa").checked' in html


def test_a_silent_stretch_shows_what_the_model_is_doing(tmp_path):
    """v0.181.142: live, a search chat still had a 21 s gap with nothing on screen (the
    model was writing a tool call). A status line now goes out every few seconds of
    silence, with the prompt-reading or tool-writing progress."""
    class SlowPool(StreamPool):
        async def chat_stream(self, model, messages, **kw):
            self.asked.append((model, True))
            yield {"content": "", "done": False, "progress": {"prefill": {"processed": 2000, "total": 6000, "cache": 0}}}
            await asyncio.sleep(0.35)
            yield {"content": "Done.", "thinking": "", "done": False}
            yield {"done": True, "content": ""}

    runner, ctx, pool, _ = _make(tmp_path, {"main": [], "small": []})
    runner.pool = SlowPool({"main": [], "small": []})
    runner.brain.cfg.main_progress_seconds = 0.1
    evs = _run(runner.run_main(ctx))
    lines = [e.text for e in evs if e.kind == "think" and "still working" in e.text]
    assert len(lines) >= 2 and all(l.startswith("⏳ main") for l in lines)
    assert "reading the prompt · 2,000 / 6,000 new tokens (33%)" in lines[0]
    assert [e.text for e in evs if e.kind == "answer_delta"] == ["Done."]
    # 0 = the ordinary keep-alive only
    runner, ctx, pool, _ = _make(tmp_path, {"main": [], "small": []})
    runner.pool = SlowPool({"main": [], "small": []})
    runner.brain.cfg.main_progress_seconds = 0
    lanes._PINS.clear()
    evs = _run(runner.run_main(ctx))
    assert not [e for e in evs if e.kind == "think" and "still working" in e.text]
    assert AgentBrainConfig().main_progress_seconds == 3.0

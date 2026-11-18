"""v0.181.184: the approved rules and the retrieved precedent have to reach the
prompt of the requests this router actually serves.

They did not. Both blocks were assembled inside the brain's worker tool loop,
and a strict persona never involves the brain - in passthrough routing mode
nothing does - so every real Kilo request went through the facade's direct
dispatch, which builds its own prompt and never ran that code. Three persona
toggles and a global switch could all be on, three rules could be approved, and
the prompt was the same as before.

Every test of lessons and recall passed throughout, because they all called the
feature's own functions directly. None of them asked what a model is actually
sent. These do.
"""
import inspect
import json

import pytest

from foundry_router import lessons
from foundry_router.pool.protocols import ChatResult

TOOLS = [{"type": "function", "function": {"name": "write_to_file",
                                           "parameters": {"type": "object"}}}]
RULE = "Build the tail to the size and colour the reference describes."


class Pool:
    """One model, and every prompt it was sent."""

    def __init__(self):
        self.asked = []

    def available_models(self):
        return {"m1": ["m1-be"]}

    def backend_info(self, m):
        return ({"name": "m1-be", "type": "ollama", "url": "http://x", "api_key": None}
                if m == "m1" else None)

    def backend_status(self):
        return []

    async def loaded_models(self):
        return set()

    def active_calls(self):
        return []

    async def chat(self, model, messages, tools=None, **kw):
        self.asked.append([dict(m) for m in messages])
        return ChatResult(content="done", finish_reason="stop"), "m1-be"

    async def chat_stream(self, model, messages, tools=None, **kw):
        res, _ = await self.chat(model, messages, tools=tools)
        yield {"content": res.content}
        yield res.done_frame()


@pytest.fixture()
def strict_persona(app, client):
    """A strict, direct persona with one pinned model: what every Kilo persona
    is, and the shape that never reached the injection."""
    svc = app.state.services
    name = "Strict-Direct"
    svc.personas.upsert(name, execution_mode="direct", routing="strict",
                        benchmark_category="coding", pinned_models=json.dumps(["m1"]),
                        model_allowlist=json.dumps(["m1"]), enabled=1, lessons=1)
    svc.registry.upsert_auto("m1", source="discovery", relative_cost_tier="free")
    old_pool, cfg = svc.pool, svc.config_store.config
    svc.pool = svc.agent.pool = Pool()
    old_enabled = cfg.lessons.enabled
    cfg.lessons.enabled = True
    svc.db.execute(
        "INSERT INTO lessons (ts, anchor, rule, draft, seen, tasks, sources, examples, "
        "first_seen, last_seen, state) VALUES ('now','a1','','',9,'[\"VQ-1\"]',"
        "'[\"human\"]','[\"x\"]','a','b','proposed')")
    lid = svc.db.query_one("SELECT id FROM lessons WHERE anchor='a1'")["id"]
    lessons.decide(svc, lid, "active", RULE)
    yield svc, name
    svc.pool = svc.agent.pool = old_pool
    cfg.lessons.enabled = old_enabled


def _systems(prompt):
    return "\n".join(m.get("content") or "" for m in prompt if m.get("role") == "system")


def test_an_approved_rule_reaches_a_strict_direct_persona(strict_persona, client):
    svc, name = strict_persona
    r = client.post("/api/chat", json={"model": name, "stream": False, "tools": TOOLS,
                                       "messages": [{"role": "user", "content": "build EN-WOLF"}]})
    assert r.status_code == 200, r.text
    assert svc.pool.asked, "the model was never asked"
    assert RULE in _systems(svc.pool.asked[0])


def test_the_rule_sits_with_the_system_turns_not_in_the_conversation(strict_persona, client):
    svc, name = strict_persona
    client.post("/api/chat", json={
        "model": name, "stream": False, "tools": TOOLS,
        "messages": [{"role": "system", "content": "you build models"},
                     {"role": "user", "content": "build EN-WOLF"}]})
    prompt = svc.pool.asked[0]
    where = [i for i, m in enumerate(prompt) if RULE in (m.get("content") or "")]
    assert where, "the rule never arrived"
    assert prompt[where[0]]["role"] == "system"
    # after the persona's own instructions, before the request
    assert prompt[where[0] - 1].get("content") == "you build models"
    assert prompt[where[0] + 1]["role"] == "user"


def test_a_persona_that_did_not_opt_in_gets_nothing(strict_persona, client):
    svc, name = strict_persona
    svc.personas.upsert(name, lessons=0)
    client.post("/api/chat", json={"model": name, "stream": False, "tools": TOOLS,
                                   "messages": [{"role": "user", "content": "build EN-WOLF"}]})
    assert RULE not in _systems(svc.pool.asked[0])


def test_the_switch_being_off_is_enough(strict_persona, client):
    svc, name = strict_persona
    svc.config_store.config.lessons.enabled = False
    client.post("/api/chat", json={"model": name, "stream": False, "tools": TOOLS,
                                   "messages": [{"role": "user", "content": "build EN-WOLF"}]})
    assert RULE not in _systems(svc.pool.asked[0])


def test_what_the_client_holds_is_not_what_we_added(strict_persona, client):
    """The direct path keeps a copy of the client's own history to judge whether
    its compaction is stuck. Adding to the prompt must not change that copy, or
    the router reports the client's context as larger than the client's."""
    from foundry_router.facade import ollama_api
    src = inspect.getsource(ollama_api._direct_dispatch_chat)
    assert "client_convo = list(base_convo)" in src
    assert src.index("client_convo = list(base_convo)") < src.index("precedent.inject(")


def test_both_paths_call_the_one_place_the_blocks_are_built():
    """The bug was one copy of this code living where only one of two callers
    could reach it. Each caller now calls precedent.py, and neither builds the
    block itself."""
    from foundry_router.brain import agent
    from foundry_router.facade import ollama_api
    direct = inspect.getsource(ollama_api._direct_dispatch_chat)
    loop = inspect.getsource(agent.AgentRunner._worker_tool_loop)
    for src in (direct, loop):
        assert "precedent.inject(" in src
        assert "lessons.block(" not in src and "recall.note(" not in src

"""Who uses the router and how the brain performs (v0.141.0).

Caller identity (key name, or "untracked · <IP>" from x-forwarded-for / the
socket, plus the user agent), brain_calls rows for every outcome and the
brain metrics endpoint, the client activity and live endpoints, the route
graph's untracked / brain / live paths and active-only mode, and the opt-in
route footer across the request shapes AnythingLLM and Open WebUI send."""
import asyncio
import json

import pytest

from foundry_router import observability as ob
from foundry_router.brain.client import BrainClient, BrainUnreachable
from foundry_router.config import AgentBrainConfig, ClientConfig
from foundry_router.pool.protocols import ChatResult

ALLM_KEY = "allm-key-1234567890abc"


# ---------------------------------------------------------------------------- labels

def test_client_labels():
    assert ob.client_label("cline") == "cline"
    assert ob.client_label("anonymous", "192.168.0.50") == "untracked · 192.168.0.50"
    assert ob.client_label(None, None) == "untracked · unknown address"
    assert ob.parse_label("untracked · 10.0.0.2") == ("anonymous", "10.0.0.2")
    assert ob.parse_label("untracked · unknown address") == ("anonymous", None)
    assert ob.parse_label("kilo") == ("kilo", None)


# ---------------------------------------------------------------------------- fake pool

class AnswerPool:
    """One local model on backend o1 that answers (or calls a client tool)."""

    def __init__(self, tool_call=False):
        self.tool_call = tool_call

    def available_models(self):
        return {"local-a": ["o1"]}

    def backend_info(self, m):
        return {"name": "o1", "type": "ollama", "url": "http://o"} if m == "local-a" else None

    def backend_status(self):
        return [{"name": "o1", "type": "ollama", "models": ["local-a"], "enabled": True,
                 "healthy": True}]

    async def loaded_models(self):
        return set()

    def active_calls(self):
        return []

    def active_requests(self):
        return []

    def _res(self):
        if self.tool_call:
            return ChatResult(content="", tool_calls=[{"id": "t1", "name": "write_to_file",
                                                       "arguments": {"path": "a"}}],
                              prompt_tokens=5, completion_tokens=2, finish_reason="tool_calls")
        return ChatResult(content="The answer is 42.", thinking="adding up",
                          prompt_tokens=5, completion_tokens=4, finish_reason="stop")

    async def chat(self, model, messages, tools=None, **kw):
        return self._res(), "o1"

    async def chat_stream(self, model, messages, tools=None, **kw):
        r = self._res()
        if r.content:
            yield {"content": r.content}
        yield r.done_frame()


@pytest.fixture()
def direct(app, client):
    """A direct persona on the fake pool, plus an AnythingLLM key."""
    svc = app.state.services
    svc.personas.upsert("Chat-Direct", execution_mode="direct", enabled=1,
                        model_allowlist=["local-a"], pinned_models=["local-a"])
    svc.config_store.config.clients.append(ClientConfig(name="anythingllm", key=ALLM_KEY,
                                                        scopes=["inference"]))
    cfg = svc.config_store.config.agent_brain
    old = (svc.pool, cfg.direct_stream, cfg.direct_stream_heartbeat_seconds, cfg.heartbeat_seconds)

    def use(pool=None, stream=False):
        svc.pool = pool or AnswerPool()
        cfg.direct_stream, cfg.direct_stream_heartbeat_seconds, cfg.heartbeat_seconds = stream, 0, 0
    use.svc = svc
    use()
    yield use
    svc.pool, cfg.direct_stream, cfg.direct_stream_heartbeat_seconds, cfg.heartbeat_seconds = old


def _last_req(svc):
    return svc.db.query_one("SELECT * FROM request_log ORDER BY id DESC LIMIT 1")


# ---------------------------------------------------------------------------- identity

def test_ip_and_agent_recorded_and_untracked_grouped_per_ip(direct, client):
    body = {"model": "Chat-Direct", "stream": False, "messages": [{"role": "user", "content": "hi"}]}
    client.post("/api/chat", json=body, headers={"x-forwarded-for": "192.168.0.50, 10.0.0.1",
                                                 "user-agent": "AnythingLLM/1.9"})
    r = _last_req(direct.svc)
    assert r["client_addr"] == "192.168.0.50" and r["user_agent"] == "AnythingLLM/1.9"
    assert r["client_id"] == "anonymous"
    client.post("/api/chat", json=body, headers={"x-real-ip": "192.168.0.60"})
    client.post("/api/chat", json=body)                               # the socket address
    assert _last_req(direct.svc)["client_addr"] == "testclient"
    client.post("/api/chat", json=body, headers={"Authorization": f"Bearer {ALLM_KEY}",
                                                 "x-forwarded-for": "192.168.0.50"})
    act = client.get("/admin/api/clients/activity?hours=1").json()
    by = {c["client"]: c for c in act["clients"]}
    assert by["untracked · 192.168.0.50"]["requests"] == 1
    assert by["untracked · 192.168.0.50"]["agents"] == ["AnythingLLM/1.9"]
    assert by["untracked · 192.168.0.60"]["requests"] == 1
    assert by["anythingllm"]["requests"] == 1 and by["anythingllm"]["tracked"]
    assert act["untracked"] == 3 and sum(by["anythingllm"]["series"]) == 1


def test_route_graph_untracked_live_brain_and_active_only(direct, client):
    svc = direct.svc
    client.post("/api/chat", headers={"x-forwarded-for": "192.168.0.77"}, json={
        "model": "Chat-Direct", "stream": False, "messages": [{"role": "user", "content": "x"}]})
    rid = _last_req(svc)["request_id"]
    from foundry_router.db import utcnow
    svc.db.execute("INSERT INTO brain_calls (ts, purpose, provider, model, latency_ms, outcome, "
                   "request_id, persona, client_id) VALUES (?,?,?,?,?,?,?,?,?)",
                   (utcnow(), "routing", "ollama", "test-brain", 900, "ok", rid, "Chat-Direct",
                    "anonymous"))
    pool = svc.pool
    pool.active_requests = lambda: [{"model": "local-a", "backend": "o1", "seconds": 3.0,
                                     "request_id": "r9", "client": "anythingllm", "addr": "1.2.3.4",
                                     "persona": "Chat-Direct", "phase": "generating"}]
    g = client.get("/admin/api/routes?hours=1").json()
    ids = {n["id"] for n in g["nodes"]}
    assert "client:untracked · 192.168.0.77" in ids and "brain:test-brain" in ids
    kinds = {(e["kind"], e["from"], e["to"]) for e in g["edges"]}
    assert ("brain", "persona:Chat-Direct", "brain:test-brain") in kinds
    assert ("live", "client:anythingllm", "persona:Chat-Direct") in kinds
    assert ("live", "model:local-a", "backend:o1") in kinds and g["live"] == 1
    assert any(e["kind"] == "config" for e in g["edges"])
    ga = client.get("/admin/api/routes?hours=1&active=1").json()
    assert ga["active"] and not any(e["kind"] in ("config", "fallback") for e in ga["edges"])
    # drill-down by the untracked label, and the brain edge
    reqs = client.get("/admin/api/routes/edge", params={
        "kind": "traffic", "src": "client:untracked · 192.168.0.77",
        "dst": "persona:Chat-Direct", "hours": 1}).json()["requests"]
    assert len(reqs) == 1 and reqs[0]["client"] == "untracked · 192.168.0.77"
    reqs = client.get("/admin/api/routes/edge", params={
        "kind": "brain", "src": "persona:Chat-Direct", "dst": "brain:test-brain",
        "hours": 1}).json()["requests"]
    assert [q["request_id"] for q in reqs] == [rid]
    t = client.get(f"/admin/api/requests/{rid}/trace").json()
    assert t["brain_calls"][0]["model"] == "test-brain"
    lv = client.get("/admin/api/live").json()
    assert lv["calls"][0]["client_label"] == "anythingllm"


# ---------------------------------------------------------------------------- brain

class FakeProto:
    def __init__(self, script):
        self.script = list(script)

    async def chat(self, model, messages, **kw):
        await asyncio.sleep(0)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _brain(app, script, **cfg):
    c = AgentBrainConfig(provider="ollama", endpoint="http://b", model="brain-9b", **cfg)
    b = BrainClient(c, None, db=app.state.services.db)
    b.protocol = FakeProto(script)
    return b


def test_brain_calls_recorded_for_every_outcome(app, client):
    db = app.state.services.db
    ok = ChatResult(content="route to local", prompt_tokens=100, completion_tokens=20)

    async def run():
        b = _brain(app, [ok])
        await b.chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
        b = _brain(app, [RuntimeError("invalid tool call arguments: cut off"), ok])
        await b.chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
        b = _brain(app, [OSError("connection refused")])
        with pytest.raises(BrainUnreachable):
            await b.complete("refine this")
        b = _brain(app, [], routing_mode="passthrough")
        with pytest.raises(BrainUnreachable):
            await b.chat([{"role": "user", "content": "x"}])
        b = _brain(app, [ok])
        await b.complete("research", aux=True)
    asyncio.run(run())
    rows = db.query("SELECT purpose, outcome, reason, completion_tokens FROM brain_calls ORDER BY id")
    assert [(r["purpose"], r["outcome"]) for r in rows] == [
        ("routing", "ok"), ("routing", "retried"), ("assist", "failed"), ("assist", "skipped"),
        ("background", "ok")]
    assert "connection refused" in rows[2]["reason"] and "passthrough" in rows[3]["reason"]
    m = client.get("/admin/api/brain/metrics?hours=1").json()
    assert m["calls"] == 5 and m["called"] == 4
    assert m["outcomes"] == {"ok": 2, "retried": 1, "failed": 1, "skipped": 1}
    assert m["failure_rate"] == 0.25 and m["fallback_rate"] == 0.4
    assert m["by_purpose"]["routing"]["calls"] == 2 and m["models"] == ["brain-9b"]
    assert sum(s["calls"] for s in m["series"]) == 5


def test_brain_active_calls_are_visible_while_running(app):
    gate = asyncio.Event()

    class Slow:
        async def chat(self, model, messages, **kw):
            await gate.wait()
            return ChatResult(content="ok")

    async def run():
        b = _brain(app, [])
        b.protocol = Slow()
        task = asyncio.create_task(b.chat([{"role": "user", "content": "x"}]))
        await asyncio.sleep(0.01)
        during = b.active()
        gate.set()
        await task
        return during, b.active()
    during, after = asyncio.run(run())
    assert len(during) == 1 and during[0]["model"] == "brain-9b" and after == []


# ---------------------------------------------------------------------------- route footer

def _content(r):
    return r.json()["message"]["content"]


def test_route_footer_is_opt_in_per_persona_and_per_client(direct, client):
    svc = direct.svc
    body = {"model": "Chat-Direct", "stream": False, "messages": [{"role": "user", "content": "q"}]}
    assert _content(client.post("/api/chat", json=body)) == "The answer is 42."
    svc.personas.upsert("Chat-Direct", route_footer=1)
    r = client.post("/api/chat", json=body).json()["message"]
    assert r["content"] == "The answer is 42.\n\n— via local-a @ o1 (local)"
    assert r["thinking"].startswith("⚙️ local · local-a @ o1 [Foundry ") and "adding up" in r["thinking"]
    # never on structured output or a tool-call-only turn
    assert _content(client.post("/api/chat", json={**body, "format": "json"})) == "The answer is 42."
    direct(AnswerPool(tool_call=True))
    tc = client.post("/api/chat", json={**body, "tools": [{"type": "function", "function": {
        "name": "write_to_file"}}]}).json()["message"]
    assert tc["content"] == "" and tc["tool_calls"]
    # per client key instead of per persona
    direct()
    svc.personas.upsert("Chat-Direct", route_footer=0)
    next(c for c in svc.config_store.config.clients if c.name == "anythingllm").route_footer = True
    assert _content(client.post("/api/chat", json=body)) == "The answer is 42."
    keyed = client.post("/api/chat", json=body, headers={"Authorization": f"Bearer {ALLM_KEY}"})
    assert _content(keyed).endswith("— via local-a @ o1 (local)")


def test_setup_client_route_footer_switch(direct, client):
    svc = direct.svc

    def mutate(raw):
        raw.setdefault("clients", []).append({"name": "openwebui", "key": "owui-key-1234567890",
                                              "scopes": ["inference"]})
    svc.config_store.save(mutate)
    r = client.patch("/admin/api/setup/clients/openwebui", json={"route_footer": True})
    assert r.status_code == 200
    assert next(c for c in svc.config_store.config.clients if c.name == "openwebui").route_footer
    view = next(c for c in client.get("/admin/api/setup/clients").json()["clients"]
                if c["name"] == "openwebui")
    assert view["route_footer"] is True


# ---------------------------------------------------------------------------- compat matrix

def _sse(text):
    out = []
    for line in text.splitlines():
        if line.startswith("data: ") and line[6:].strip() != "[DONE]":
            out.append(json.loads(line[6:]))
    return out


SHAPES = [
    # (who, path, body, stream)
    ("anythingllm chat", "/v1/chat/completions",
     {"model": "Chat-Direct", "stream": True, "temperature": 0.7,
      "messages": [{"role": "system", "content": "You are helpful."},
                   {"role": "user", "content": "q"}]}, True),
    ("anythingllm @agent", "/v1/chat/completions",
     {"model": "Chat-Direct", "stream": False, "temperature": 0,
      "messages": [{"role": "system", "content": "You are an agent."},
                   {"role": "user", "content": "q"}],
      "tools": [{"type": "function", "function": {"name": "web-browsing", "parameters": {
          "type": "object", "properties": {"query": {"type": "string"}}}}}]}, False),
    ("open webui chat", "/api/chat",
     {"model": "Chat-Direct", "stream": True, "options": {"temperature": 0.8},
      "messages": [{"role": "user", "content": "q"}]}, True),
    ("open webui tools", "/v1/chat/completions",
     {"model": "Chat-Direct", "stream": True,
      "messages": [{"role": "user", "content": "q"}],
      "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {
          "type": "object", "properties": {}}}}]}, True),
]


@pytest.mark.parametrize("live_stream", [False, True])
@pytest.mark.parametrize("who,path,body,stream", SHAPES, ids=[s[0] for s in SHAPES])
def test_compat_matrix_answers_and_route_footer(direct, client, who, path, body, stream,
                                               live_stream):
    """Each shape gets a complete answer in its own dialect, and with the
    client key's route_footer on, the answer ends with the route line."""
    svc = direct.svc
    direct(stream=live_stream)
    next(c for c in svc.config_store.config.clients if c.name == "anythingllm").route_footer = True
    r = client.post(path, json=body, headers={"Authorization": f"Bearer {ALLM_KEY}"})
    assert r.status_code == 200, r.text
    if path.startswith("/v1"):
        if stream:
            chunks = _sse(r.text)
            text = "".join((c["choices"][0].get("delta") or {}).get("content") or ""
                           for c in chunks if c.get("choices"))
            assert any(c["choices"][0].get("finish_reason") == "stop"
                       for c in chunks if c.get("choices"))
        else:
            msg = r.json()["choices"][0]["message"]
            text = msg["content"]
            assert "local-a @ o1" in (msg.get("reasoning_content") or "")
    else:
        lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
        text = "".join(x["message"].get("content") or "" for x in lines)
        assert lines[-1]["done"] is True
    assert text.startswith("The answer is 42.")
    assert text.endswith("— via local-a @ o1 (local)")


def test_route_footer_on_the_brain_path_fallback(direct, client):
    """AnythingLLM's normal chat (no tools) goes through the brain path; here
    the brain is unreachable, so the static fallback answers — the footer
    names the worker and no brain."""
    svc = direct.svc
    svc.personas.upsert("Chat-Agent", enabled=1, route_footer=1, model_allowlist=["local-a"],
                        pinned_models=["local-a"])
    r = client.post("/api/chat", json={"model": "Chat-Agent", "stream": False,
                                       "messages": [{"role": "user", "content": "q"}]})
    text = r.json()["message"]["content"]
    assert "The answer is 42." in text
    assert text.endswith("— via local-a @ o1 (local)")
    assert "routed by brain" not in text

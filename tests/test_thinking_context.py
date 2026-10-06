"""Reasoning reaches the thinking panel (inline <think> split out, the think
setting shown), and the context guard keeps requests inside the model's
window with honest prompt-size reporting for client auto-compaction."""

import json

import httpx

from foundry_router import context_guard
from foundry_router.pool.protocols import OllamaProtocol, OpenAIProtocol, ThinkSplitter


# -- <think> splitting ---------------------------------------------------------------

def _stream(chunks):
    sp, c, t = ThinkSplitter(), "", ""
    for x in chunks:
        a, b = sp.feed(x)
        c, t = c + a, t + b
    a, b = sp.flush()
    return c + a, t + b


def test_splitter_handles_tags_split_across_chunks():
    c, t = _stream(["\n<th", "ink>Let me ", "check the fi", "le.</thi", "nk>\n\nDone."])
    assert t == "Let me check the file." and c == "Done."


def test_splitter_leaves_normal_and_later_tags_alone():
    assert _stream(["Hello ", "world"]) == ("Hello world", "")
    c, t = _stream(["Use the ", "<think> tag like this: <think>x</think>"])
    assert t == "" and "<think>" in c                  # not at the start -> content


def test_splitter_unterminated_think_is_all_thinking():
    assert _stream(["<think>still going"]) == ("", "still going")
    assert ThinkSplitter.split("<think>a</think>b", "prior") == ("b", "prior\na")


async def test_adapters_split_inline_think_streaming_and_not():
    sse = "".join(f"data: {json.dumps(f)}\n\n" for f in [
        {"choices": [{"delta": {"content": "<think>plan"}}]},
        {"choices": [{"delta": {"content": " steps</think>Answer"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]}]) + "data: [DONE]\n\n"
    llama = OpenAIProtocol("http://l", None, httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=sse))))
    ch = [c async for c in llama.chat_stream("m", [{"role": "user", "content": "x"}])]
    assert "".join(c.get("thinking") or "" for c in ch) == "plan steps"
    assert "".join(c.get("content") or "" for c in ch) == "Answer"
    nd = "\n".join(json.dumps(x) for x in [
        {"message": {"content": "<think>hmm"}, "done": False},
        {"message": {"content": "</think>Hi"}, "done": False},
        {"message": {"content": ""}, "done": True, "eval_count": 2}])
    olm = OllamaProtocol("http://o", None, httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=nd))))
    ch = [c async for c in olm.chat_stream("m", [{"role": "user", "content": "x"}])]
    assert "".join(c.get("thinking") or "" for c in ch) == "hmm"
    assert "".join(c.get("content") or "" for c in ch) == "Hi"
    one = OpenAIProtocol("http://l", None, httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"choices": [{"message": {
            "content": "<think>why</think>Because."}, "finish_reason": "stop"}]}))))
    res = await one.chat("m", [{"role": "user", "content": "x"}])
    assert res.thinking == "why" and res.content == "Because."


# -- context guard ------------------------------------------------------------------

def _convo(turns: int, tool_chars: int = 40000):
    msgs = [{"role": "system", "content": "You are Cline. " * 200},
            {"role": "user", "content": "TASK: refactor the combat system"}]
    for i in range(turns):
        msgs.append({"role": "assistant", "content": f"step {i}", "tool_calls": [
            {"id": f"c{i}", "function": {"name": "read_file", "arguments": {"path": f"f{i}.lua"}}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "read_file",
                     "content": f"-- file {i}\n" + "x = 1\n" * (tool_chars // 6)})
    msgs.append({"role": "user", "content": "now fix the bug"})
    return msgs


def test_guard_noop_when_it_fits():
    msgs = _convo(2, 1000)
    out, rep = context_guard.fit(msgs, None, 262144, 8192)
    assert rep is None and out is msgs


def test_guard_trims_old_tool_results_then_drops_oldest_turns_keeping_pairs():
    msgs = _convo(40)                                   # ~40 x 40k chars ≈ 500k tokens
    out, rep = context_guard.fit(msgs, None, 131072, 8192)
    assert rep and rep["after"] <= rep["budget"]
    assert out[0]["role"] == "system" and "TASK" in out[1]["content"]   # task kept
    assert "TOOL LEDGER" in out[2]["content"]                            # model is told
    assert "read_file(" in out[2]["content"] and "sha1" in out[2]["content"]
    assert out[-1]["content"] == "now fix the bug"                       # latest kept
    ids = {tc["id"] for m in out if m.get("tool_calls") for tc in m["tool_calls"]}
    results = {m["tool_call_id"] for m in out if m["role"] == "tool"}
    assert results <= ids                                # no orphaned tool result
    assert rep["dropped"] > 0 and "tool ledger" in context_guard.describe(rep)
    assert msgs[3]["content"].startswith("-- file 0")   # client's copy untouched


def test_guard_cuts_a_single_huge_message():
    msgs = [{"role": "user", "content": "y" * 1_000_000}]
    out, rep = context_guard.fit(msgs, None, 32768, 4096)
    assert rep and len(out[0]["content"]) < 200_000 and "cut by Foundry" in out[0]["content"]


def test_estimator_learns_chars_per_token():
    msgs = [{"role": "user", "content": "z" * 40000}]
    context_guard.learn("m-learn", msgs, None, 10000)              # 4 chars/token
    assert 3.5 < context_guard.ratio_for("m-learn") <= 4.1


# -- through the app ----------------------------------------------------------------

class CapturePool:
    def __init__(self, btype="openai-compatible"):
        self.btype, self.sent = btype, []

    def backend_info(self, m):
        return {"name": "b1", "type": self.btype, "url": "http://b", "flavor": "llamacpp"}

    def available_models(self):
        return {"qwen": ["b1"]}

    def active_calls(self):
        return []

    def backend_status(self):
        return []

    async def chat(self, model, messages, **kw):
        from foundry_router.pool.protocols import ChatResult
        self.sent.append(messages)
        return ChatResult(content="ok", prompt_tokens=900, completion_tokens=3), "b1"


def test_direct_path_guards_context_and_reports_total_prompt(app, client):
    svc = app.state.services
    pool = CapturePool("ollama")
    real, svc.pool = svc.pool, pool
    svc.registry.upsert_auto("qwen", source="discovery", context_length=65536)
    svc.personas.upsert("Act", execution_mode="direct", model_allowlist=["qwen"],
                        pinned_models=[], context_window=65536)
    try:
        r = client.post("/api/chat", json={"model": "Act", "stream": False,
                                           "messages": _convo(30)}).json()
    finally:
        svc.pool = real
    sent = pool.sent[0]
    assert context_guard.estimate(sent, None, "qwen") <= 65536
    assert len(sent) < len(_convo(30))
    # Ollama reported only 900 re-processed tokens; the client is told the
    # real prompt size so its auto-compact threshold works.
    assert r["prompt_eval_count"] > 900
    assert r["foundry"]["prompt_evaluated"] == 900
    ev = svc.db.query("SELECT message FROM event_log WHERE source='context'")
    assert ev and "context guard" in ev[0]["message"]


def test_think_label_says_who_decided(app):
    from foundry_router.facade.ollama_api import _think_label
    svc = app.state.services
    lbl = _think_label(svc, "claude-sonnet-5", {"reasoning_effort": "off",
                                                "force_reasoning_effort": 1}, "high")
    assert lbl.startswith("think") and ("off" in lbl or "default" in lbl)


class TruncPool(CapturePool):
    def __init__(self, ct=32768):
        super().__init__("openai-compatible")
        self.kw = []
        self.ct = ct

    async def chat(self, model, messages, **kw):
        from foundry_router.pool.protocols import ChatResult
        self.kw.append(kw)
        return ChatResult(content="partial edit", prompt_tokens=50, completion_tokens=self.ct,
                          finish_reason="length"), "b1"

    async def chat_stream(self, model, messages, **kw):
        from foundry_router.pool.protocols import ChatResult
        self.kw.append(kw)
        yield {"content": "partial edit", "done": False}
        yield ChatResult(prompt_tokens=50, completion_tokens=self.ct, finish_reason="length").done_frame()


def test_persona_output_cap_reaches_backend_and_truncation_is_explained(app, client):
    svc = app.state.services
    pool = TruncPool()
    real, svc.pool = svc.pool, pool
    svc.personas.upsert("Act", execution_mode="direct", model_allowlist=["qwen"],
                        pinned_models=[], max_output_tokens=32768)
    cfg = svc.config_store.config.agent_brain
    try:
        for ds in (True, False):
            cfg.direct_stream = ds
            r = client.post("/api/chat", json={"model": "Act", "tools": [
                {"type": "function", "function": {"name": "write_to_file"}}],
                "messages": [{"role": "user", "content": "write it"}]})
            lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
            thinking = "".join(l["message"].get("thinking") or "" for l in lines)
            assert "cut at the 32,768-token output limit" in thinking
            assert "tool call was incomplete" in thinking
            assert lines[-1]["done_reason"] == "length"
    finally:
        svc.pool = real
    assert all(k["max_tokens"] == 32768 for k in pool.kw)


def test_router_status_lines_never_reach_the_model(app, client):
    """Cline stores Foundry's status lines as the turn's thinking and sends
    them back; fed to the model as prior reasoning they were imitated (fake
    'still working… 5s/10s' clocks generated as reasoning)."""
    svc = app.state.services
    pool = CapturePool("openai-compatible")
    real, svc.pool = svc.pool, pool
    svc.personas.upsert("Act", execution_mode="direct", model_allowlist=["qwen"],
                        pinned_models=[])
    echoed = ("⚙️ llamacpp · qwen — streaming… [think: model default · Foundry 0.90.1 · req d6]\n"
              "⚙️ /cache/q.gguf — still working… 5s\n"
              "/cache/q.gguf — still working… 10s\n"
              "⏳ llamacpp · qwen — still working · 1m 00s (reading the prompt)\n"
              "The file needs one more bullet after line 45.")
    try:
        client.post("/api/chat", json={"model": "Act", "stream": False, "messages": [
            {"role": "user", "content": "edit it"},
            {"role": "assistant", "content": "[router: stream failed — boom]\nOK, editing.",
             "thinking": echoed},
            {"role": "user", "content": "go on"}]})
    finally:
        svc.pool = real
    a = next(m for m in pool.sent[0] if m["role"] == "assistant")
    assert a["thinking"] == "The file needs one more bullet after line 45."
    assert a["content"] == "OK, editing."
    assert "still working" not in json.dumps(pool.sent[0])



def test_backend_side_cap_is_diagnosed(app, client):
    svc = app.state.services
    pool = TruncPool(ct=8192)            # server stopped at 8192 though 32768 was sent
    real, svc.pool = svc.pool, pool
    svc.personas.upsert("Act", execution_mode="direct", model_allowlist=["qwen"],
                        pinned_models=[], max_output_tokens=32768)
    try:
        r = client.post("/api/chat", json={"model": "Act", "messages": [
            {"role": "user", "content": "write it"}]})
    finally:
        svc.pool = real
    thinking = "".join(json.loads(x)["message"].get("thinking") or ""
                       for x in r.text.splitlines() if x.strip())
    assert "cut at 8,192 output tokens" in thinking and "BACKEND stopped it" in thinking
    assert "--n-predict" in thinking
    ev = svc.db.query("SELECT message FROM event_log WHERE source='truncation'")
    assert ev and "8192" in ev[0]["message"]


def test_cline_personas_seeded_with_32k_output(app):
    svc = app.state.services
    for name in ("claude-cline-act", "claude-cline-plan"):
        p = svc.personas.get(name)
        assert p and int(p["max_output_tokens"]) == 32768


def test_guard_keeps_the_prefix_stable_across_turns():
    """Trimming must not move the cut point every turn — that would force a
    full re-prefill each time (the prompt cache reuses the unchanged prefix)."""
    history = _convo(40, 20000)
    prefixes = []
    for extra in range(6):                       # the conversation grows turn by turn
        msgs = history[:-1] + [m for i in range(extra) for m in (
            {"role": "assistant", "content": f"more {i}"},
            {"role": "user", "content": f"next {i}"})] + [history[-1]]
        out, rep = context_guard.fit(msgs, None, 131072, 8192, "stable-model")
        assert rep and rep["after"] <= rep["budget"]
        prefixes.append(json.dumps(out[:12]))
    # identical from turn to turn, changing only when a block boundary is
    # crossed (once over these 6 turns) — not on every turn
    changes = sum(1 for a, b in zip(prefixes, prefixes[1:]) if a != b)
    assert changes <= 1


def test_trimmed_request_reports_the_clients_full_size(app, client):
    """Cline decides to compact from the reported prompt size; reporting the
    trimmed size kept it under its threshold forever ('Compaction skipped')."""
    svc = app.state.services

    class P(CapturePool):
        async def chat(self, model, messages, **kw):
            from foundry_router.pool.protocols import ChatResult
            self.sent.append(messages)
            sent = context_guard.estimate(messages, None, model)
            return ChatResult(content="ok", prompt_tokens=sent, completion_tokens=3), "b1"
    pool = P("openai-compatible")
    real, svc.pool = svc.pool, pool
    svc.registry.upsert_auto("qwen", source="discovery", context_length=65536)
    svc.personas.upsert("Act", execution_mode="direct", model_allowlist=["qwen"],
                        pinned_models=[], context_window=65536)
    history = _convo(30)
    try:
        r = client.post("/api/chat", json={"model": "Act", "stream": False,
                                           "messages": history}).json()
    finally:
        svc.pool = real
    sent = r["foundry"]["prompt_sent"]
    assert sent <= 65536                                  # what the model processed
    assert r["prompt_eval_count"] > 65536 > sent          # what Cline is told: its real size


def test_guard_keeps_latest_typed_prompt_after_client_compaction():
    """Post-compaction Cline transcript: [system, summary(user), TASK(user),
    long tool loop]. Trimming drops old tool rounds but never the task."""
    from foundry_router import context_guard
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "[compaction summary] " + "s" * 2000},
            {"role": "user", "content": "TASK: implement sprint S39"}]
    for i in range(40):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "read_file",
                                                  "arguments": {"path": f"f{i}"}}}]})
        msgs.append({"role": "tool", "content": "x" * 12000})
    out, rep = context_guard.fit(msgs, None, 100_000, 8192, "m")
    assert rep and rep["dropped"] > 0
    task = [m for m in out if "TASK: implement" in str(m.get("content"))]
    assert len(task) == 1
    i = out.index(task[0])
    assert out[1]["content"].startswith("[compaction summary]")   # prefix kept
    assert i == 2                                                 # task keeps its place
    assert out[-1]["role"] == "tool"                              # recent rounds kept
    # no orphaned tool results right after the task
    assert out[i + 1]["role"] in ("assistant", "user")
    # stable: trimming the same history again gives the same prefix
    out2, _ = context_guard.fit(msgs + [{"role": "assistant", "content": "ok"}], None,
                                100_000, 8192, "m")
    assert out2[:6] == out[:6]


def _loop(n, big=12000):
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "[summary]"},
            {"role": "user", "content": "TASK: S39"}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": f"checking step {i}",
                     "tool_calls": [{"function": {"name": "execute_command" if i % 3 else "read_file",
                                                  "arguments": {"cmd": f"run {i}"}}}]})
        body = ("Error: test C3 failed\n" if i == 5 else f"PASS step {i}\n") + "x" * big
        msgs.append({"role": "tool", "content": body})
    return msgs


def test_tool_ledger_records_every_dropped_call_verifiably():
    msgs = _loop(40)
    out, rep = context_guard.fit(msgs, None, 100_000, 8192, "m")
    ledger = next(m["content"] for m in out if "TOOL LEDGER" in str(m.get("content")))
    assert out.index(next(m for m in out if "TOOL LEDGER" in str(m.get("content")))) == 3
    lines = [l for l in ledger.splitlines() if l.strip().startswith("#")]
    assert len(lines) == rep["dropped"] // 2                  # one line per dropped call
    assert lines[0].strip().startswith("#1 read_file(") and "sha1 " in lines[0]
    assert "ERROR Error: test C3 failed" in ledger            # failures stay visible
    assert "note: checking step 0" in ledger                  # the model's reasoning notes
    assert rep["after"] <= rep["budget"]


def test_tool_ledger_is_deterministic_for_the_prompt_cache():
    a, _ = context_guard.fit(_loop(40), None, 100_000, 8192, "m")
    b, _ = context_guard.fit(_loop(40) + [{"role": "user", "content": "go on"}],
                             None, 100_000, 8192, "m")
    la = next(m["content"] for m in a if "TOOL LEDGER" in str(m.get("content")))
    lb = next(m["content"] for m in b if "TOOL LEDGER" in str(m.get("content")))
    assert la == lb


def test_tool_ledger_folds_oldest_lines_above_its_cap():
    text = context_guard.tool_ledger(context_guard._units(_loop(300, big=10)[3:]), 262144,
                                     cap_chars=3000)
    assert "oldest" in text and "execute_command×" in text
    assert len(text) < 5000


def test_client_cap_cut_is_blamed_on_the_client_not_the_server(app, client):
    """Cline sends max_tokens = window - its prompt estimate; near a 'full'
    window (screenshots inflate its estimate) that is a few hundred tokens.
    The cut must be explained as the client's cap, and the advisor must not
    claim the server has an -n limit."""
    from foundry_router import perf_advisor
    svc = app.state.services
    pool = TruncPool(ct=390)
    real, svc.pool = svc.pool, pool
    svc.personas.upsert("Act", execution_mode="direct", model_allowlist=["qwen"],
                        pinned_models=[], max_output_tokens=65536)
    try:
        r = client.post("/api/chat", json={"model": "Act", "options": {"num_predict": 390},
                                           "messages": [{"role": "user", "content": "go"}]})
        thinking = "".join(json.loads(x)["message"].get("thinking") or ""
                           for x in r.text.splitlines() if x.strip())
        assert "limit the CLIENT asked for" in thinking and "BACKEND" not in thinking
    finally:
        svc.pool = real
    ev = svc.db.query("SELECT message FROM event_log WHERE source='truncation'")
    assert ev and "client cap" in ev[-1]["message"]
    ids = {f["id"] for f in perf_advisor._event_rules(svc.db, 24)}
    assert "backend_output_cap" not in ids and "client_output_cap" in ids

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
    assert "context guard" in out[1]["content"]                          # model is told
    assert out[-1]["content"] == "now fix the bug"                       # latest kept
    ids = {tc["id"] for m in out if m.get("tool_calls") for tc in m["tool_calls"]}
    results = {m["tool_call_id"] for m in out if m["role"] == "tool"}
    assert results <= ids                                # no orphaned tool result
    assert rep["dropped"] > 0 and "dropped" in context_guard.describe(rep)
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

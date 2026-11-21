"""v0.181.167: your own benchmark — cases cut from work this router did, run
against one model and scored on what can be measured."""
import asyncio
import inspect
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry_router import bench
from foundry_router.config import CodeCheckRuleConfig, CodeChecksConfig
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult


# -- scoring ------------------------------------------------------------------------------

def test_the_tools_it_reached_for():
    def t(*names):
        return [{"name": n} for n in names]
    assert bench.tool_score(t("search", "read"), t("search", "read")) == 1.0
    assert bench.tool_score(t(), t()) == 1.0                       # nothing was needed
    assert bench.tool_score(t(), t("search")) == 0.5               # calling anyway is not wrong
    assert bench.tool_score(t("search"), t()) == 0.0               # it should have
    assert bench.tool_score(t("search", "read"), t("search")) == 0.65     # half of them, right first
    assert bench.tool_score(t("search", "read"), t("read", "search")) == 1.0 - 0.3
    assert bench.tool_score(t("search"), t("delete")) == 0.0


def test_the_answer_it_gave():
    assert bench.answer_score("", "x") == 0.0
    assert bench.answer_score("ok", "x" * 100) == 0.3
    assert bench.answer_score("a sensible answer, about as long", "a reference of some length") == 1.0
    assert bench.answer_score("x" * 20, "x" * 4000) == 0.6        # far shorter than what was done


def test_the_parts_make_one_score():
    assert bench.combine(1.0, 1.0, 1.0) == 100.0
    assert bench.combine(0.0, 0.0, 0.0) == 0.0
    assert bench.combine(1.0, None, None) == 100.0                 # only what applies counts
    assert bench.combine(None, None, None) == 0.0
    assert bench.combine(1.0, 0.0, 1.0) == 70.0                    # code is 30% of it


@pytest.mark.asyncio
async def test_code_in_an_answer_is_checked(tmp_path):
    cfg = SimpleNamespace(code_checks=CodeChecksConfig(
        enabled=False,       # the benchmark checks whatever rules exist, switch or no switch
        rules=[CodeCheckRuleConfig(name="py", kind="python", languages=["python"])]))
    svc = SimpleNamespace(config_store=SimpleNamespace(config=cfg))
    good, detail = await bench.code_score(svc, "here:\n```python\nx = 1\n```")
    assert good == 1.0 and detail == ""
    bad, detail = await bench.code_score(svc, "here:\n```python\ndef f(:\n```")
    assert bad == 0.0 and "block 1 py" in detail
    none, _ = await bench.code_score(svc, "no code at all")
    assert none is None                                            # nothing to check


# -- cutting cases out of a conversation --------------------------------------------------

def _example(tool_calls=None, answer="Done."):
    msgs = [{"role": "system", "content": "You are a coder."},
            {"role": "user", "content": "fix the spin script"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "local x = 1"},
            {"role": "assistant", "content": answer}]
    if tool_calls:
        msgs[-1] = {"role": "assistant", "content": "", "tool_calls": tool_calls}
    return {"messages": msgs, "tools": [{"type": "function", "function": {"name": "read_file"}}],
            "meta": {"task_id": "UI-7", "tier": "verified", "tags": ["luau"], "session": "s1"}}


def test_a_case_is_the_conversation_up_to_what_the_model_did():
    c = bench._case_from(_example())
    assert c["task_id"] == "UI-7" and c["tier"] == "verified" and c["tags"] == ["luau"]
    assert [m["role"] for m in c["messages"]] == ["system", "user", "assistant", "tool"]
    assert c["reference"] == {"content": "Done.", "tool_calls": []}
    assert c["tools"][0]["function"]["name"] == "read_file"
    with_tool = bench._case_from(_example(tool_calls=[
        {"id": "9", "function": {"name": "write_to_file", "arguments": '{"path":"a"}'}}]))
    assert with_tool["reference"]["tool_calls"] == [{"name": "write_to_file",
                                                     "arguments": '{"path":"a"}'}]
    assert bench._case_from({"messages": [{"role": "assistant", "content": "hi"}]}) is None
    assert bench._case_from({"messages": [{"role": "user", "content": "hi"}]}) is None


def test_a_conversation_too_big_to_send_is_left_out():
    small = {"messages": [{"role": "user", "content": "fix the spin script"}]}
    huge = {"messages": [{"role": "user", "content": "x" * (bench.MAX_PROMPT_TOKENS * 4)}]}
    assert bench.too_long(small) is False
    assert bench.too_long(huge) is True                # and never raises on the pair


# -- a run --------------------------------------------------------------------------------

class Pool:
    def __init__(self, reply):
        self.reply, self.asked = reply, []

    async def chat(self, model, messages, tools=None, max_tokens=4096, **kw):
        self.asked.append({"model": model, "messages": messages,
                           "tools": [t["function"]["name"] for t in (tools or [])]})
        r = self.reply(len(self.asked))
        if isinstance(r, Exception):
            raise r
        return r, model + "-be"


def _svc(tmp_path, pool, rules=()):
    db = Database(tmp_path / "b.sqlite")
    cfg = SimpleNamespace(code_checks=CodeChecksConfig(
        rules=[CodeCheckRuleConfig(**r) for r in rules]))
    return SimpleNamespace(db=db, pool=pool, capture=SimpleNamespace(data_dir=tmp_path),
                           config_store=SimpleNamespace(config=cfg))


def _suite(svc, name="s", n=2, answer="A sensible answer about the spin script."):
    d = Path(svc.capture.data_dir) / "bench"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{name}.jsonl").open("w", encoding="utf-8") as f:
        for i in range(n):
            c = bench._case_from(_example(answer=answer))
            c["id"] = f"case-{i}"
            f.write(json.dumps(c) + "\n")
    return name


def _wait(svc, timeout=20):
    for _ in range(int(timeout * 20)):
        if bench._state.get("state") != "running":
            return
        time.sleep(0.05)
    raise AssertionError("the run did not finish")


def test_a_run_scores_every_case_and_is_kept(tmp_path):
    pool = Pool(lambda n: ChatResult(content="A sensible answer about the spin script.",
                                     prompt_tokens=10, completion_tokens=5))
    svc = _svc(tmp_path, pool)
    name = _suite(svc)
    out = bench.start(svc, name, "qwen", label="before the change")
    assert out["state"] in ("running", "done")      # a fake pool can finish before this returns
    _wait(svc)
    assert len(pool.asked) == 2 and pool.asked[0]["tools"] == ["read_file"]
    assert [m["role"] for m in pool.asked[0]["messages"]] == ["system", "user", "assistant", "tool"]
    runs = bench.runs(svc.db)
    assert len(runs) == 1 and runs[0]["status"] == "done" and runs[0]["scored"] == 2
    assert runs[0]["model"] == "qwen" and runs[0]["label"] == "before the change"
    assert runs[0]["score"] == 100.0 and runs[0]["tools_score"] == 100.0
    assert runs[0]["prompt_tokens"] == 20 and runs[0]["completion_tokens"] == 10
    rows = bench.results(svc.db, runs[0]["id"])
    assert len(rows) == 2 and rows[0]["case_id"] == "case-0" and rows[0]["error"] == ""
    assert json.loads(rows[0]["tools_expected"]) == [] and rows[0]["code_pass"] is None


def test_a_model_that_fails_or_writes_broken_code_scores_lower(tmp_path):
    broken = "Here:\n```python\ndef f(:\n```"
    pool = Pool(lambda n: RuntimeError("the backend is down") if n == 1
                else ChatResult(content=broken))
    svc = _svc(tmp_path, pool, rules=[{"name": "py", "kind": "python", "languages": ["python"]}])
    name = _suite(svc)
    bench.start(svc, name, "qwen")
    _wait(svc)
    run = bench.runs(svc.db)[0]
    rows = bench.results(svc.db, run["id"])
    assert "the backend is down" in rows[0]["error"] and rows[0]["score"] == 0.0
    assert rows[1]["code_pass"] == 0 and "block 1 py" in rows[1]["code_detail"]
    assert run["score"] < 50 and run["note"] == "1 call(s) failed"


def test_two_runs_are_compared_case_by_case(tmp_path):
    pool = Pool(lambda n: ChatResult(content="A sensible answer about the spin script."))
    svc = _svc(tmp_path, pool)
    name = _suite(svc)
    bench.start(svc, name, "qwen", label="before")
    _wait(svc)
    svc.pool = Pool(lambda n: ChatResult(content="no"))
    bench.start(svc, name, "qwen-next", label="after")
    _wait(svc)
    a, b = sorted(r["id"] for r in bench.runs(svc.db))
    c = bench.compare(svc.db, a, b)
    assert c["shared"] == 2 and c["worse"] == 2 and c["better"] == 0
    assert c["a"]["label"] == "before" and c["b"]["model"] == "qwen-next"
    assert c["cases"][0]["change"] < 0


def test_only_one_run_at_a_time(tmp_path):
    class Slow(Pool):
        async def chat(self, model, messages, tools=None, max_tokens=4096, **kw):
            await asyncio.sleep(0.25)
            return await super().chat(model, messages, tools=tools, max_tokens=max_tokens)
    svc = _svc(tmp_path, Slow(lambda n: ChatResult(content="ok, a sensible answer here")))
    name = _suite(svc, n=2)
    first = bench.start(svc, name, "qwen")
    second = bench.start(svc, name, "other")                 # while the first is still going
    assert first["state"] == "running" and second["model"] == "qwen"
    _wait(svc)
    assert [r["model"] for r in bench.runs(svc.db)] == ["qwen"]


def test_an_empty_suite_is_refused(tmp_path):
    svc = _svc(tmp_path, Pool(lambda n: ChatResult(content="x")))
    with pytest.raises(ValueError):
        asyncio.run(bench._run(svc, "nothing", "qwen", "", 0))
    assert bench.suites(svc) == []


def test_routes_and_page():
    from foundry_router.ui import routes
    src = inspect.getsource(routes)
    for needle in ('@router.get("/admin/api/bench")', '@router.post("/admin/api/bench/suite")',
                   '@router.post("/admin/api/bench/run")', '@router.get("/admin/api/bench/compare")'):
        assert needle in src, needle
    html = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "async function tabBench()" in html and '"Benchmark":      tabBench,' in html
    assert '["Labs",        ["Research", "Benchmark", "Evals"]]' in html
    for fid in ("bs-name", "bs-limit", "bs-tier", "bs-build", "br-suite", "br-model", "br-go"):
        assert f'id="{fid}"' in html, fid
    schema = (Path(routes.__file__).parent.parent / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS bench_runs" in schema
    assert "CREATE TABLE IF NOT EXISTS bench_results" in schema


def test_a_checkers_own_log_lines_are_not_sent_to_the_model():
    from foundry_router import code_checks
    lines = [
        "[INFO] Loading definitions file: @roblox - /usr/local/share/luau/globalTypes.d.luau",
        "[INFO] sourcemap is disabled",
        "WARNING: --platform is set to 'roblox' but no definitions files are provided",
        "[WARN] No definitions file provided by client",
        "snippet.luau(2,7): TypeError: Key 'bar' not found in table 't'",
    ]
    assert code_checks.clean_output(chr(10).join(lines)) == lines[-1]
    assert code_checks.clean_output("") == ""
    assert code_checks.clean_output("[INFO] only noise") == ""


# -- where a case is cut out of a conversation --------------------------------------------

def _long_example(turns=6, filler=400):
    """A session like the real ones: a request, then many tool calls, then a summary."""
    msgs = [{"role": "system", "content": "You are a coder."},
            {"role": "user", "content": "fix the spin script"}]
    for i in range(turns):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": str(i), "function": {"name": f"tool_{i}", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": "x" * filler})
    msgs.append({"role": "assistant", "content": "Done, committed as abc123."})
    return {"messages": msgs, "tools": [{"type": "function",
                                         "function": {"name": "tool_0"}}],
            "meta": {"task_id": "UI-7", "tier": "verified", "tags": ["luau"], "session": "s1"}}


def test_cases_are_cut_where_the_model_reached_for_a_tool():
    cases = bench.cases_from(_long_example())
    assert len(cases) == bench.CASES_PER_CONVERSATION
    # every one of them can measure a tool call, which is half the score
    assert all(c["reference"]["tool_calls"] for c in cases)
    assert [c["reference"]["tool_calls"][0]["name"] for c in cases] == ["tool_5", "tool_4"]


def test_a_closing_answer_is_not_a_replay_case():
    """v0.181.218: a words-only turn was an end-of-turn report, and cut to its
    newest turns it had lost the context that made reporting right; those are
    graded in the protocol suite. The tool turn is still the case."""
    one_call = {"messages": [{"role": "user", "content": "fix it"},
                             {"role": "assistant", "content": "", "tool_calls": [
                                 {"id": "1", "function": {"name": "read", "arguments": "{}"}}]},
                             {"role": "tool", "tool_call_id": "1", "content": "ok"},
                             {"role": "assistant", "content": "Done."}],
                "tools": [], "meta": {"task_id": "T", "session": "s"}}
    cases = bench.cases_from(one_call)
    assert len(cases) == 1
    assert cases[0]["reference"]["tool_calls"]


def test_a_session_far_too_long_to_send_still_yields_a_case():
    """The sessions here run to hundreds of thousands of tokens. Keeping the NEWEST
    turns is what lets one become a case at all."""
    huge = _long_example(turns=30, filler=8000)
    assert bench.too_long(bench._case_from(huge)) is True     # the old way: rejected
    cases = bench.cases_from(huge)
    assert cases and not any(bench.too_long(c) for c in cases)
    assert len(cases[0]["messages"]) < len(huge["messages"])


def test_a_case_never_opens_on_an_orphaned_tool_result():
    huge = _long_example(turns=30, filler=8000)
    for c in bench.cases_from(huge):
        body = [m for m in c["messages"] if m.get("role") != "system"]
        assert body[0]["role"] in ("user", "assistant")
        assert not (body[0]["role"] == "assistant" and body[0].get("tool_calls"))
        # every tool result in the case has its call in the case too
        calls = {tc["id"] for m in body for tc in (m.get("tool_calls") or [])}
        for m in body:
            if m.get("role") == "tool":
                assert m["tool_call_id"] in calls


def test_a_conversation_with_nothing_to_answer_for_gives_no_cases():
    assert bench.cases_from({"messages": [{"role": "assistant", "content": "hi"}]}) == []
    assert bench.cases_from({"messages": [{"role": "user", "content": "hi"}]}) == []
    assert bench.cases_from({"messages": []}) == []


# -- the stored format is not the wire format ---------------------------------------------

def test_a_stored_conversation_is_converted_for_the_wire():
    """What the prepared store keeps is a neutral training format. Sending it as-is
    is what made 48 of the first 60 real cases fail."""
    stored = [{"role": "system", "content": "be careful", "author": {"kind": "client"}},
              {"role": "user", "content": "fix the spin script", "author": {"kind": "client"}},
              {"role": "assistant", "content": "", "author": {"kind": "model"},
               "tool_calls": [{"id": "1", "name": "read_file",
                               "arguments": {"path": "a.luau"}}]},      # a dict, not a string
              {"role": "tool", "tool_call_id": "1", "content": "local x = 1"}]
    w = bench.wire(stored)
    call = w[2]["tool_calls"][0]
    assert call["type"] == "function"                       # the wrapper the wire format has
    assert call["function"]["name"] == "read_file"
    assert call["function"]["arguments"] == '{"path": "a.luau"}'   # a JSON STRING
    assert isinstance(call["function"]["arguments"], str)
    assert w[3]["tool_call_id"] == "1"
    assert all("author" not in m for m in w)                # the store's own bookkeeping
    assert all(isinstance(m["content"], str) for m in w)


def test_content_that_is_not_text_still_becomes_text():
    w = bench.wire([{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    assert isinstance(w[0]["content"], str) and "hi" in w[0]["content"]
    assert bench.wire([{"role": "user"}])[0]["content"] == ""
    assert bench.wire([None, "nope", {}]) == [{"role": "user", "content": ""}]


def test_a_tool_call_already_in_wire_shape_is_left_alone():
    w = bench.wire([{"role": "assistant", "content": "", "tool_calls": [
        {"id": "9", "type": "function",
         "function": {"name": "write", "arguments": '{"path":"b"}'}}]}])
    fn = w[0]["tool_calls"][0]["function"]
    assert fn["name"] == "write" and fn["arguments"] == '{"path":"b"}'


def test_tool_definitions_are_converted_too():
    stored = [{"name": "read_file", "description": "reads", "parameters": {"type": "object"}},
              {"name": ""},                                  # nameless: not a tool
              {"type": "function", "function": {"name": "write_to_file"}}]
    t = bench.wire_tools(stored)
    assert [x["function"]["name"] for x in t] == ["read_file", "write_to_file"]
    assert all(x["type"] == "function" for x in t)
    assert t[1]["function"]["parameters"] == {"type": "object", "properties": {}}
    assert bench.wire_tools(None) == []


# -- putting past work in front of a case -------------------------------------------------

def _recall_svc(tmp_path, pool=None, hits=None):
    svc = _svc(tmp_path, pool or Pool(lambda n: ChatResult(content="ok, a sensible answer")))
    from foundry_router.config import RecallConfig
    svc.config_store.config.recall = RecallConfig(enabled=True, top_k=3)

    async def search(s, query, top_k=0, min_similarity=None, tiers=(), tags=()):
        return list(hits if hits is not None else [])
    import foundry_router.recall as recall_mod
    svc._search = search
    return svc, recall_mod, search


@pytest.mark.asyncio
async def test_a_case_never_retrieves_its_own_sprint(tmp_path, monkeypatch):
    """Recall is built from the same store the suite is cut from. Without holding the
    case's own task out, a case retrieves the very turn being scored and the model
    copies the answer — an improvement that measures nothing but contamination."""
    svc, recall_mod, _ = _recall_svc(tmp_path)
    own = {"case_id": "UI-7-s1-4", "task_id": "UI-7", "similarity": 0.99, "tier": "verified",
           "tools": [], "request": "the very same request", "gist": "the very same request",
           "solution": "THE ANSWER BEING SCORED"}
    other = {"case_id": "GF-1-s2-2", "task_id": "GF-1", "similarity": 0.70, "tier": "verified",
             "tools": ["bash"], "request": "a different sprint", "gist": "a different sprint",
             "solution": "something else that passed"}

    async def search(s, query, top_k=0, min_similarity=None, tiers=(), tags=()):
        return [own, other]
    monkeypatch.setattr(recall_mod, "search", search)
    case = bench._case_from(_example())
    out, used = await bench.with_recall(svc, case)
    assert [h["case_id"] for h in used] == ["GF-1-s2-2"]          # its own task held out
    text = json.dumps(out["messages"])
    assert "THE ANSWER BEING SCORED" not in text
    assert "something else that passed" in text


@pytest.mark.asyncio
async def test_past_work_goes_in_as_a_system_turn_after_the_system_prompt(tmp_path, monkeypatch):
    svc, recall_mod, _ = _recall_svc(tmp_path)

    async def search(s, query, top_k=0, min_similarity=None, tiers=(), tags=()):
        return [{"case_id": "GF-1-s2-2", "task_id": "GF-1", "similarity": 0.8,
                 "tier": "verified", "tools": ["bash"], "request": "a past request",
                 "gist": "a past request", "solution": "what was done then"}]
    monkeypatch.setattr(recall_mod, "search", search)
    case = bench._case_from(_example())
    out, used = await bench.with_recall(svc, case)
    roles = [m["role"] for m in out["messages"]]
    assert roles[0] == "system" and roles[1] == "system"          # ours comes after theirs
    assert "what was done then" in out["messages"][1]["content"]
    assert len(out["messages"]) == len(case["messages"]) + 1
    assert case["messages"] == bench._case_from(_example())["messages"]   # case untouched


@pytest.mark.asyncio
async def test_nothing_found_means_the_case_is_sent_as_it_was(tmp_path, monkeypatch):
    svc, recall_mod, _ = _recall_svc(tmp_path)

    async def nothing(s, query, top_k=0, min_similarity=None, tiers=(), tags=()):
        return []
    monkeypatch.setattr(recall_mod, "search", nothing)
    case = bench._case_from(_example())
    out, used = await bench.with_recall(svc, case)
    assert used == [] and out["messages"] == case["messages"]


@pytest.mark.asyncio
async def test_a_case_with_no_request_asks_recall_nothing(tmp_path, monkeypatch):
    svc, recall_mod, _ = _recall_svc(tmp_path)
    asked = []

    async def search(s, query, top_k=0, min_similarity=None, tiers=(), tags=()):
        asked.append(query)
        return []
    monkeypatch.setattr(recall_mod, "search", search)
    out, used = await bench.with_recall(svc, {"messages": [{"role": "system", "content": "x"}]})
    assert used == [] and asked == []


def test_a_run_records_whether_past_work_was_used(tmp_path, monkeypatch):
    svc = _svc(tmp_path, Pool(lambda n: ChatResult(content="a sensible answer about the script")))
    from foundry_router.config import RecallConfig
    svc.config_store.config.recall = RecallConfig(enabled=True)
    import foundry_router.recall as recall_mod

    async def search(s, query, top_k=0, min_similarity=None, tiers=(), tags=()):
        return [{"case_id": "GF-1-s2-2", "task_id": "GF-1", "similarity": 0.8,
                 "tier": "verified", "tools": [], "request": "r", "gist": "r",
                 "solution": "what was done"}]
    monkeypatch.setattr(recall_mod, "search", search)
    name = _suite(svc, n=2)
    bench.start(svc, name, "qwen", label="with past work", use_recall=True)
    _wait(svc)
    row = bench.runs(svc.db)[0]
    assert row["recall"] == 1
    assert "past work found for 2" in (row["note"] or "")
    hit = bench.results(svc.db, row["id"])[0]
    assert json.loads(hit["recalled"]) == ["GF-1-s2-2"]
    plain = bench.start(svc, name, "qwen", label="without")
    _wait(svc)
    assert bench.runs(svc.db)[0]["recall"] == 0


def test_every_benchmark_call_pins_the_sampling(tmp_path):
    """A benchmark that cannot repeat itself cannot measure anything: two runs of the
    same suite against the same model came back 65.5 and 75.0 before this."""
    seen = {}

    class Recording(Pool):
        async def chat(self, model, messages, tools=None, max_tokens=4096, options=None, **kw):
            seen.update(options or {})
            return await super().chat(model, messages, tools=tools, max_tokens=max_tokens)
    svc = _svc(tmp_path, Recording(lambda n: ChatResult(content="a sensible answer here")))
    name = _suite(svc, n=1)
    bench.start(svc, name, "qwen")
    _wait(svc)
    assert seen.get("temperature") == 0.0
    assert seen.get("seed") == bench.SAMPLING["seed"]
    assert seen.get("top_p") == 1.0 and seen.get("top_k") == 1


def test_a_run_the_restart_cut_off_is_not_left_saying_running(tmp_path):
    """Nothing was alive to write the end of it, so the row would read as a result
    that is still coming. A rebuild mid-run left exactly that."""
    svc = _svc(tmp_path, Pool(lambda n: ChatResult(content="ok")))
    svc.db.execute("INSERT INTO bench_runs (ts, suite, model, label, cases, scored, status) "
                   "VALUES (datetime('now'), 's', 'm', 'cut off', 60, 0, 'running')")
    svc.db.execute("INSERT INTO bench_runs (ts, suite, model, label, cases, scored, status) "
                   "VALUES (datetime('now'), 's', 'm', 'finished', 60, 60, 'done')")
    assert bench.reconcile(svc.db) == 1
    rows = {r["label"]: r for r in bench.runs(svc.db)}
    assert rows["cut off"]["status"] == "interrupted"
    assert "restarted" in rows["cut off"]["note"]
    assert rows["finished"]["status"] == "done"          # a real result is untouched
    assert bench.reconcile(svc.db) == 0                  # nothing left to reconcile

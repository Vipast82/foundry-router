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

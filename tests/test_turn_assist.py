"""Turn assist: rule-based history transforms (deterministic for the prompt
cache), adaptive thinking, the pre-judge, the supervisor, the auditor and
advisor in the direct-dispatch loop, Plan's refuse fallback, subscription call
budgets, and quiet narration."""

import json

import pytest

from foundry_router import turn_assist as ta
from foundry_router import frontier_assist as fa
from foundry_router.pool.protocols import ChatResult


# -- helpers ---------------------------------------------------------------------------

def call(i, name, args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def result(i, text, name=None):
    m = {"role": "tool", "tool_call_id": f"c{i}", "content": text}
    if name:
        m["tool_name"] = name
    return m


FILE = "\n".join(f"{n} | line {n} of the file" for n in range(1, 60))
OK = json.dumps([{"query": "x", "result": "done", "success": True}])
FAIL = json.dumps([{"query": "x", "result": "[Command exited with code 1] boom",
                    "success": False}])


def history(*pairs):
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "<user_input mode=\"act\">S51 Tier A. do the task"}]
    for i, (name, args, text) in enumerate(pairs):
        msgs += [call(i, name, args), result(i, text)]
    return msgs


# -- D3 dedupe ---------------------------------------------------------------------------

def test_dedupe_replaces_identical_reread_with_pointer():
    msgs = history(("read_files", {"files": [{"path": "a.lua"}]}, FILE),
                   ("run_commands", {"commands": ["git status"]}, OK),
                   ("read_files", {"files": [{"path": "a.lua"}]}, FILE))
    out, n = ta.dedupe_reads(msgs)
    assert n == 1
    assert out[3]["content"] == FILE                       # first copy untouched
    assert out[7]["content"].startswith("[Foundry: identical to the read_files result at message 3")
    assert "a.lua" in out[7]["content"]


def test_transforms_are_stable_turn_to_turn():
    """Cache rule: rewriting turn N's history and turn N+1's history must give
    the same messages for the shared prefix."""
    cfg = ta.config({"assist": {"dedupe_reads": True, "roblox_cleanup": True,
                                "supervisor": "nudge", "image_keep": 2}})
    base = history(*[("read_files", {"files": [{"path": "a.lua"}]}, FILE)] * 4)
    longer = base + [call(9, "run_commands", {"commands": ["ls"]}), result(9, OK)]
    a = ta.prepare(base, cfg).messages
    b = ta.prepare(longer, cfg).messages
    assert b[:len(a)] == a


# -- D5 Roblox cleanup -----------------------------------------------------------------

def test_roblox_wrapper_stripped_real_error_kept():
    raw = json.dumps({"content": [{"type": "text", "text":
        "sabuiltin_Assistant.rbxm.Assistant.Packages._Index.AssistantUI.AssistantUI.Tools."
        "ExecuteLuauTool:86: sabuiltin_Assistant.rbxm.Assistant.Packages._Index.AssistantUI."
        "AssistantUI.Util.CommandExecution:54: AssistantCommand:14: attempt to index nil"}],
        "isError": True})
    msgs = history(("Roblox_Studio__execute_luau", {"code": "x"}, raw))
    out, n = ta.clean_roblox(msgs)
    assert n == 1
    t = out[3]["content"]
    assert "builtin_Assistant" not in t
    assert "your Luau code, line 14: attempt to index nil" in t


def test_roblox_stale_studio_hint():
    msgs = history(("Roblox_Studio__get_studio_state", {"studio_id": "x"},
                    "The requested `studio_id` is not connected — closed?"))
    out, _ = ta.clean_roblox(msgs)
    assert "call list_roblox_studios once" in out[3]["content"]


# -- D4 images ------------------------------------------------------------------------------

def test_prune_images_keeps_newest():
    msgs = [{"role": "user", "content": f"shot {i}", "images": ["b64"]} for i in range(5)]
    out, n = ta.prune_images(msgs, 2)
    assert n == 3
    assert [bool(m.get("images")) for m in out] == [False, False, False, True, True]
    assert "screenshot removed" in out[0]["content"]


# -- D1 adaptive think ------------------------------------------------------------------

LV = {"routine": "off", "after_edit": "low", "after_failure": "high"}


def test_adaptive_think_levels():
    assert ta.adaptive_think(history(("read_files", {"files": []}, FILE)), LV) == ("off", "routine result")
    assert ta.adaptive_think(history(("editor", {"path": "a"}, OK)), LV)[0] == "low"
    assert ta.adaptive_think(history(("run_commands", {"commands": ["x"]}, FAIL)), LV)[0] == "high"
    msgs = history(("read_files", {"files": []}, FILE)) + [{"role": "assistant", "content": "done"},
                                                           {"role": "user", "content": "now fix y"}]
    assert ta.adaptive_think(msgs, LV) == ("high", "new user message")


def test_file_text_mentioning_fail_is_not_a_tool_error():
    assert not ta.is_error(json.dumps([{"result": "-- FAIL: test x\nTraceback", "success": True}]))
    assert ta.is_error(FAIL)


# -- D2 pre-judge ------------------------------------------------------------------------

def tc(name, args, i="t1"):
    return {"id": i, "name": name, "arguments": args}


def test_prejudge_blocks_and_fixes_in_enforce():
    calls = [tc("run_commands", {"commands": ["git push --force origin master"]}, "a"),
             tc("run_commands", {"commands": ["ls -la", "npx vitest run | tail -n 20"]}, "b")]
    j = ta.prejudge(calls, {"prejudge": "enforce", "prejudge_max_edit_chars": 6000})
    assert [r for _, r, _ in j.blocked] == ["force push"]
    assert [c["id"] for c in j.calls] == ["b"]
    assert j.calls[0]["arguments"]["commands"] == [
        "Get-ChildItem -Force", "npx.cmd vitest run | Select-Object -Last 20"]


def test_prejudge_observe_changes_nothing():
    calls = [tc("run_commands", {"commands": ["git add -A"]}),
             tc("run_commands", {"commands": ["ls -la"]}, "t2")]
    j = ta.prejudge(calls, {"prejudge": "observe"})
    assert j.blocked and j.fixes
    assert j.calls == calls


def test_prejudge_allows_tmp_delete_and_normal_git():
    calls = [tc("run_commands", {"commands": ["Remove-Item -Recurse tmp\\run1",
                                              "git push origin master", "git add src/a.lua"]})]
    assert not ta.prejudge(calls, {"prejudge": "enforce"}).blocked


# -- D6 supervisor -------------------------------------------------------------------------

def test_supervisor_flags_loop_and_nudges_at_the_result():
    same = ("run_commands", {"commands": ["lune run t"]}, FAIL)
    msgs = history(same, same, same)
    sup = ta.supervise(msgs, "nudge")
    kinds = [f.kind for f in sup.findings]
    assert "loop" in kinds
    f = next(f for f in sup.findings if f.kind == "loop")
    assert ta.SUPERVISOR_TAG in sup.messages[f.at]["content"]
    assert sup.level >= 1


def test_supervisor_observe_does_not_touch_history():
    same = ("run_commands", {"commands": ["lune run t"]}, FAIL)
    msgs = history(same, same, same)
    sup = ta.supervise(msgs, "observe")
    assert sup.findings and sup.messages == msgs


def test_supervisor_playtest_cycling():
    pairs = []
    for i in range(8):
        pairs.append(("robloxstudio-mcp__solo_playtest", {"action": "start" if i % 2 == 0 else "stop"}, OK))
    sup = ta.supervise(history(*pairs), "observe")
    assert any(f.kind == "cycling" for f in sup.findings)


# -- auditor lint -----------------------------------------------------------------------

def test_auditor_lint_flags_unverified_claims_and_missing_evidence():
    msgs = history(("run_commands", {"commands": ["python done_check.py"]}, OK))
    d = fa.lint("All done, it should work now.", msgs,
                {"auditor_required_patterns": [r"done_check.*PASS"]})
    assert any("required evidence" in x for x in d)
    assert any("should work" in x for x in d)
    assert not fa.lint("done_check: PASS (12/12). git show --stat attached.", msgs,
                       {"auditor_required_patterns": [r"done_check.*PASS"]})


# -- request-path integration -----------------------------------------------------------

class ScriptPool:
    """Returns scripted ChatResults per model; records what each call saw."""

    def __init__(self, script: dict, types: dict | None = None):
        self.script = {k: list(v) for k, v in script.items()}
        self.types = types or {}
        self.sent, self.kw = [], []

    def backend_info(self, m):
        t = self.types.get(m, "openai-compatible")
        return {"name": "b-" + m, "type": t, "url": "http://b", "flavor": "llamacpp",
                "subscription": t == "anthropic-compatible", "call_budget_5h": None,
                "call_budget_daily": None}

    def available_models(self):
        return {m: ["b-" + m] for m in self.script}

    def active_calls(self):
        return []

    def backend_status(self):
        return []

    async def stop(self):
        pass

    async def chat(self, model, messages, **kw):
        self.sent.append((model, messages))
        self.kw.append(kw)
        return self.script[model].pop(0), "b-" + model


@pytest.fixture()
def act(app):
    svc = app.state.services
    svc.config_store.config.agent_brain.direct_stream = False

    def make(assist, script, types=None, **persona):
        pool = ScriptPool(script, types)
        svc.pool = pool
        svc.personas.upsert("Act", execution_mode="direct", model_allowlist=["qwen"],
                            pinned_models=[], assist=assist, **persona)
        return pool
    return make


TOOLS = [{"type": "function", "function": {"name": "run_commands", "parameters": {}}}]


def post(client, msgs, model="Act", stream=False):
    return client.post("/api/chat", json={"model": model, "stream": stream,
                                          "messages": msgs, "tools": TOOLS})


def test_prejudge_enforce_reasks_the_model_in_the_same_request(app, client, act):
    pool = act({"prejudge": "enforce"}, {"qwen": [
        ChatResult(content="", tool_calls=[tc("run_commands", {"commands": ["git reset --hard"]})]),
        ChatResult(content="", tool_calls=[tc("run_commands", {"commands": ["ls -la"]}, "t2")]),
    ]})
    r = post(client, history(("read_files", {"files": []}, FILE))).json()
    calls = r["message"]["tool_calls"]
    assert calls[0]["function"]["arguments"]["commands"] == ["Get-ChildItem -Force"]
    second = pool.sent[1][1]
    assert "pre-judge] blocked before it ran: git reset --hard" in second[-1]["content"]
    kinds = {row["kind"] for row in app.state.services.db.query("SELECT kind FROM interventions")}
    assert {"prejudge_block", "prejudge_fix"} <= kinds


def test_adaptive_think_reaches_the_backend(app, client, act):
    pool = act({"think_policy": "adaptive", "think_levels": LV},
               {"qwen": [ChatResult(content="ok")]})
    post(client, history(("read_files", {"files": []}, FILE)))
    assert pool.kw[0]["think"] is False                 # routine turn: thinking off


def test_auditor_enforce_rejects_then_accepts(app, client, act):
    pool = act({"auditor": "enforce", "auditor_min_tool_turns": 1, "auditor_max_rejections": 2},
               {"qwen": [ChatResult(content="All finished — it should work now, trust me."),
                         ChatResult(content="Ran the check: 12/12 passed. git status clean.")]})
    r = post(client, history(("run_commands", {"commands": ["lune run"]}, OK))).json()
    content = r["message"]["content"]
    assert "[Foundry auditor] NOT ACCEPTED (1/2)" in content
    assert content.rstrip().endswith("git status clean.")
    assert "NOT ACCEPTED" in pool.sent[1][1][-1]["content"]


def test_auditor_observe_never_rejects(app, client, act):
    pool = act({"auditor": "observe", "auditor_min_tool_turns": 1},
               {"qwen": [ChatResult(content="Done with the whole task. It should work now.")]})
    r = post(client, history(("run_commands", {"commands": ["lune run"]}, OK))).json()
    assert r["message"]["content"] == "Done with the whole task. It should work now."
    assert len(pool.sent) == 1
    rows = app.state.services.db.query("SELECT kind FROM interventions")
    assert [x["kind"] for x in rows] == ["audit_lint"]


def test_consult_senior_runs_inside_the_router(app, client, act):
    pool = act({"advisor": True, "frontier_models": ["claude-sonnet-5"]},
               {"qwen": [ChatResult(content="", tool_calls=[tc("consult_senior", {
                   "question": "why nil?", "what_i_tried": "two things"})]),
                         ChatResult(content="Applying the advice.")],
                "claude-sonnet-5": [ChatResult(content="Root cause: X. Next: 1 2 3",
                                               prompt_tokens=5000, completion_tokens=80)]},
               types={"claude-sonnet-5": "anthropic-compatible"})
    app.state.services.config_store.config.backend_pool.internal.backends = []
    r = post(client, history(("run_commands", {"commands": ["x"]}, FAIL))).json()
    assert "Root cause: X" in r["message"]["content"]          # survives into history
    assert "tool_calls" not in r["message"]                    # client never sees the tool
    assert any(t["function"]["name"] == "consult_senior"
               for t in pool.kw[0]["tools"])
    row = app.state.services.db.query_one("SELECT * FROM interventions WHERE kind='advisor'")
    assert row["frontier_model"] == "claude-sonnet-5"


def test_supervisor_escalate_blocks_without_a_model_call(app, client, act):
    same = ("run_commands", {"commands": ["lune run t"]}, FAIL)
    pool = act({"supervisor": "escalate"}, {"qwen": []})
    # 4 separate findings since the last user message -> BLOCKED reply
    msgs = history(same, same, same, *[("editor", {"path": "a"},
                   json.dumps({"error": "Editor operation failed: no match", "success": False}))] * 2,
                   ("run_commands", {"commands": ["Start-Sleep 5"]}, OK),
                   *[("run_commands", {"commands": ["Start-Sleep 5; get"]}, OK)] * 3,
                   *[("run_commands", {"commands": ["y"]}, FAIL)] * 5)
    r = post(client, msgs).json()
    assert r["message"]["content"].startswith("BLOCKED by Foundry supervisor")
    assert pool.sent == []


def test_plan_refuses_instead_of_local_fallback(app, client):
    svc = app.state.services
    pool = ScriptPool({"claude-opus": [], "qwen": [ChatResult(content="local plan")]},
                      types={"claude-opus": "anthropic-compatible", "qwen": "ollama"})
    svc.pool = pool
    svc.personas.upsert("Plan", execution_mode="direct", local_bias_strength="prefer_paid",
                        pinned_models=["claude-opus"], model_allowlist=["claude-opus"],
                        fallback_policy="refuse",
                        guardrail_overrides={"max_paid_calls_per_request": 0})
    r = client.post("/api/chat", json={"model": "Plan", "stream": False,
                                       "messages": [{"role": "user", "content": "plan it"}]})
    assert r.status_code == 503
    assert "fallback_policy=refuse" in r.json()["error"]
    assert pool.sent == []


def test_subscription_call_budget(app):
    import asyncio
    from foundry_router.guardrails import RequestGuardState
    from foundry_router.usage import log_subscription_usage
    svc = app.state.services
    info = {"name": "cpa", "type": "openai-compatible", "url": "http://cpa",
            "subscription": True, "call_budget_5h": 2, "call_budget_daily": None}
    eff = {**svc.guardrails.effective(None), "max_paid_calls_per_request": 9}
    for _ in range(2):
        log_subscription_usage(svc.db, "cpa-gpt", "cpa", 10, 10)
    v = asyncio.run(svc.guardrails.check_paid_call("cpa-gpt", info, {"cost_per_1k_input": 1.0},
                                                   RequestGuardState(), eff))
    assert not v.allowed and "5-hour call budget for cpa used (2/2)" in v.reason
    # a subscription is never metered: no dollar cap applies even with a list price
    info["call_budget_5h"] = 99
    v = asyncio.run(svc.guardrails.check_paid_call("cpa-gpt", info, {"cost_per_1k_input": 1.0},
                                                   RequestGuardState(),
                                                   {**eff, "daily_spend_cap_usd": 0.0}))
    assert v.allowed


def test_quiet_narration_strips_status_lines_only(app, client):
    svc = app.state.services
    svc.config_store.config.agent_brain.direct_stream = False
    pool = ScriptPool({"qwen": [ChatResult(content="answer", thinking="my own reasoning")]})
    svc.pool = pool
    svc.personas.upsert("Gen", execution_mode="direct", model_allowlist=["qwen"],
                        quiet_narration=1)
    r = client.post("/api/chat", json={"model": "Gen", "stream": True,
                                       "messages": [{"role": "user", "content": "q"}]})
    thinking = "".join(json.loads(l)["message"].get("thinking", "")
                       for l in r.text.splitlines() if l.strip())
    assert "my own reasoning" in thinking
    assert "⚙️" not in thinking and "Foundry" not in thinking


def test_scoreboard_and_export(app, client, act):
    act({"supervisor": "observe", "dedupe_reads": True},
        {"qwen": [ChatResult(content="ok", completion_tokens=40)]})
    same = ("run_commands", {"commands": ["lune run t"]}, FAIL)
    post(client, history(same, same, same))
    sb = client.get("/admin/api/scoreboard?hours=24").json()
    conv = sb["conversations"][0]
    assert conv["calls"] == 1 and conv["errors"] == 1          # this turn's new result
    assert sb["interventions"].get("supervisor_loop") == 1
    text = client.get("/admin/api/interventions/export?hours=24").text
    assert json.loads(text.splitlines()[0])["kind"] == "supervisor_loop"


class StreamScriptPool(ScriptPool):
    """Live-streaming variant (the path Cline uses with direct_stream on)."""

    async def chat_stream(self, model, messages, **kw):
        self.sent.append((model, messages))
        self.kw.append(kw)
        r = self.script[model].pop(0)
        if r.content:
            yield {"content": r.content, "done": False}
        yield {"done": True, "prompt_tokens": 10, "completion_tokens": 5,
               "tool_calls": r.tool_calls or []}


def test_live_stream_prejudge_and_audit(app, client):
    svc = app.state.services
    svc.config_store.config.agent_brain.direct_stream = True
    pool = StreamScriptPool({"qwen": [
        ChatResult(content="", tool_calls=[tc("run_commands", {"commands": ["git push -f"]})]),
        ChatResult(content="All finished — it should work now, trust me on this one."),
        ChatResult(content="", tool_calls=[tc("run_commands", {"commands": ["ls -la"]}, "t3")]),
    ]})
    svc.pool = pool
    svc.personas.upsert("Act", execution_mode="direct", model_allowlist=["qwen"],
                        pinned_models=[], assist={"prejudge": "enforce", "auditor": "enforce",
                                                  "auditor_min_tool_turns": 1})
    r = post(client, history(("run_commands", {"commands": ["lune run"]}, OK)), stream=True)
    lines = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    content = "".join(l["message"].get("content", "") for l in lines)
    done = lines[-1]
    assert done["done"] is True
    assert "[Foundry auditor] NOT ACCEPTED (1/2)" in content
    assert done["message"]["tool_calls"][0]["function"]["arguments"]["commands"] == [
        "Get-ChildItem -Force"]
    assert len(pool.sent) == 3
    assert "pre-judge] blocked before it ran: force push" in pool.sent[1][1][-1]["content"]

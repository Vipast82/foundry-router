"""The keep-everything dataset (v0.181.145): one example per conversation, labelled
instead of filtered, long conversations cut between turns, two file formats."""

import json

from foundry_training import sessions
from foundry_training.schema import Event
from foundry_training.store import TrainingStore

TOOLS = [{"type": "function", "function": {"name": "write_file", "description": "write",
                                            "parameters": {"type": "object", "properties": {}}}}]
SYS = {"role": "system", "content": "You are a Roblox developer."}


def _turns(n, pad=0):
    """n assistant/tool exchanges after the system prompt and the first user message."""
    out = [SYS, {"role": "user", "content": "Build the shop UI." + " pad" * pad}]
    for i in range(n):
        out.append({"role": "assistant", "content": f"step {i}" + " pad" * pad,
                    "tool_calls": [{"id": f"c{i}", "type": "function", "function": {
                        "name": "write_file", "arguments": json.dumps({"path": f"src/ui/Shop{i}.luau",
                                                                       "text": "local function f() end"})}}]})
        out.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"wrote {i}"})
    return out


def _call(store, sid, n, model, ts, *, reply="done", status="ok", labels=(), outcome="ok", task="UI-1",
          tool_calls=None, pad=0, msgs=None, role="implementer"):
    store.put_events([Event(
        "foundry", f"capture:{sid}-{ts}", "model_call", project_id="au", timestamp=float(ts),
        session_id=sid, task_id=task, request_id=f"r-{sid}-{ts}", actor_role="assistant",
        actor_model=model, actual_model=model, actual_backend="b", requested_persona="P",
        capture_status="complete" if status == "ok" else "partial",
        facts={"status": status, "messages": msgs if msgs is not None else _turns(n, pad), "tools": TOOLS,
               "content": reply, "tool_calls": tool_calls or [], "reasoning": "thinking it through",
               "role": role, "purpose": "agent_turn",
               "quality": {"outcome": outcome, "labels": [{"label": x} for x in labels]}})])


def _step_call(n):
    return [{"id": f"c{n}", "name": "write_file",
             "arguments": {"path": f"src/ui/Shop{n}.luau", "text": "local function f() end"}}]


def _read(path):
    return [json.loads(x) for x in open(path, encoding="utf-8")]


def test_one_example_per_conversation_with_everything_kept(tmp_path):
    st = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)
    # session A: three calls of one growing conversation, written by a frontier model, gate passed
    _call(st, "A", 0, "claude-sonnet", 10, reply="step 0", tool_calls=_step_call(0))
    _call(st, "A", 1, "claude-sonnet", 11, reply="step 1", tool_calls=_step_call(1))
    _call(st, "A", 2, "claude-sonnet", 12, reply="All done.", labels=("gate_pass",))
    # session B: the local model, review asked for changes; a failed call after it
    _call(st, "B", 1, "qwen", 20, reply="first try", labels=("review_changes",), task="BL-2")
    _call(st, "B", 2, "qwen", 21, status="error", task="BL-2")
    # session C: a compaction: the second call does not continue the first
    _call(st, "C", 2, "gpt", 30, reply="before compaction", task=None)
    _call(st, "C", 0, "gpt", 31, reply="after compaction", outcome=None, task=None,
          msgs=[SYS, {"role": "user", "content": "Summary of earlier work. Continue the export."}])
    m = sessions.export(st, ["au"], name="all", validation_fraction=0.0, log=lambda s: None)
    assert m["sessions"] == 3 and m["conversations"] == 4            # A, B, and both halves of C
    assert m["examples"] == {"train": 4} and m["skipped"] == {}
    assert m["tiers"] == {"verified": 1, "failed": 1, "ok": 1, "unknown": 1}
    assert m["frontier_targets_allowed"] is True
    rows = _read(tmp_path / "s" / "datasets" / "all" / "train.jsonl")
    a = next(r for r in rows if r["meta"]["session_id"] == "A")
    assert [x["role"] for x in a["messages"]] == ["system", "user", "assistant", "tool", "assistant",
                                                   "tool", "assistant"]
    assert a["messages"][-1]["content"] == "All done." and a["meta"]["calls"] == 3
    assert a["meta"]["models"] == {"claude-sonnet": 3} and a["meta"]["tier"] == "verified"
    assert a["meta"]["labels"] == ["gate_pass"] and a["meta"]["role"] == "implementer"
    assert {"luau", "roblox_ui", "tool_use", "role:implementer"} <= set(a["meta"]["tags"])
    assert all("weight" not in x for x in a["messages"])               # every assistant turn is a target
    assert a["messages"][2]["reasoning_content"] == "thinking it through"
    assert a["tools"][0]["function"]["name"] == "write_file"
    b = next(r for r in rows if r["meta"]["session_id"] == "B")
    assert b["meta"]["tier"] == "failed" and b["messages"][-1]["content"] == "first try"   # kept, labelled
    c = [r for r in rows if r["meta"]["session_id"] == "C"]
    assert sorted(x["messages"][-1]["content"] for x in c) == ["after compaction", "before compaction"]
    # the text file holds the same examples in Qwen ChatML
    texts = _read(tmp_path / "s" / "datasets" / "all" / "train.text.jsonl")
    assert len(texts) == 4
    t = next(x for x in texts if x["meta"]["session_id"] == "A")["text"]
    assert t.startswith("<|im_start|>system\nYou are a Roblox developer.") and "<tools>" in t
    assert "<tool_call>" in t and "<tool_response>\nwrote 0\n</tool_response>" in t
    assert t.endswith("All done.<|im_end|>")
    card = (tmp_path / "s" / "datasets" / "all" / "README.md").read_text(encoding="utf-8")
    assert "| verified | 1 |" in card and "claude-sonnet" in card and "hosted models as training targets" in card
    man = json.loads((tmp_path / "s" / "datasets" / "all" / "manifest.json").read_text())
    assert man["files"]["train.jsonl"] > 0 and man["files"]["validation.jsonl"] == 0


def test_filters_narrow_only_when_asked(tmp_path):
    st = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)
    _call(st, "A", 1, "claude", 1, labels=("gate_pass",))
    _call(st, "B", 1, "qwen", 2, labels=("gate_fail",), task="X-2")
    m = sessions.export(st, ["au"], name="v", tiers=["verified"], validation_fraction=0.0, log=lambda s: None)
    assert m["examples"] == {"train": 1} and m["skipped"] == {"tier_filter": 1}
    m = sessions.export(st, ["au"], name="t", tags=["blender"], validation_fraction=0.0, log=lambda s: None)
    assert m["examples"] == {} and m["skipped"] == {"tag_filter": 2}


def test_a_long_conversation_is_cut_between_turns(tmp_path):
    st = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)
    _call(st, "L", 12, "qwen", 1, reply="finished", pad=120)
    m = sessions.export(st, ["au"], name="cut", max_tokens=1500, validation_fraction=0.0, log=lambda s: None)
    n = m["examples"]["train"]
    assert n > 2 and m["examples_from_long_conversations"] == n
    rows = _read(tmp_path / "s" / "datasets" / "cut" / "train.jsonl")
    seen_steps = []
    for i, r in enumerate(rows):
        ms = r["messages"]
        assert r["meta"]["tokens"] <= 1500 and r["meta"]["pieces"] == n and r["meta"]["piece"] == i + 1
        assert ms[0]["role"] == "system"
        assert (ms[1]["content"] == sessions.OMITTED) == (i > 0)       # later pieces say what is missing
        calls = {tc["id"] for x in ms for tc in x.get("tool_calls") or []}
        answers = {x["tool_call_id"] for x in ms if x["role"] == "tool"}
        assert answers <= calls                                         # never a result without its call
        seen_steps += [x["content"].split(" pad")[0] for x in ms if x["role"] == "assistant"]
    assert seen_steps == [f"step {i}" for i in range(12)] + ["finished"]   # every turn once, in order


def test_validation_is_split_by_task_and_secrets_are_blanked(tmp_path):
    st = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)
    for i in range(40):
        _call(st, f"S{i}", 1, "qwen", i, task=f"T-{i % 10}", reply=f"answer {i}")
    _call(st, "K", 0, "qwen", 99, task="T-K", reply="the key is sk-ant-api03-" + "a" * 40)
    m = sessions.export(st, ["au"], name="sp", validation_fraction=0.3, log=lambda s: None)
    d = tmp_path / "s" / "datasets" / "sp"
    tr = {r["meta"]["task_id"] for r in _read(d / "train.jsonl")}
    va = {r["meta"]["task_id"] for r in _read(d / "validation.jsonl")}
    assert tr and va and not (tr & va)
    assert m["examples_redacted_again"] == 1 and sum(m["examples"].values()) == 41
    blob = (d / "train.jsonl").read_text(encoding="utf-8") + (d / "validation.jsonl").read_text(encoding="utf-8")
    assert "sk-ant-api03-" not in blob and "the key is" in blob


def test_tiers():
    assert sessions.tier_of(["ok"], ["gate_pass"]) == "verified"
    assert sessions.tier_of(["ok"], ["gate_pass", "review_changes"]) == "failed"
    assert sessions.tier_of(["ok", "ok"], []) == "ok"
    assert sessions.tier_of([None], []) == "unknown" and sessions.tier_of([], []) == "unknown"
    assert sessions.tier_of(["ok", "error"], []) == "failed"


def test_only_the_tools_an_example_uses_are_listed(tmp_path):
    """v0.181.146: 60-180 tool definitions (about 40,000 tokens) came before the first
    word of every coding example; the first live run left 5,540 conversations out."""
    st = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)
    many = TOOLS + [{"type": "function", "function": {"name": f"tool_{i}", "description": "d" * 2000,
                                                      "parameters": {"type": "object"}}} for i in range(60)]
    st.put_events([Event(
        "foundry", "capture:T-1", "model_call", project_id="au", timestamp=1.0, session_id="T",
        task_id="UI-1", request_id="r", actor_role="assistant", actor_model="qwen", actual_model="qwen",
        actual_backend="b", requested_persona="P", capture_status="complete",
        facts={"status": "ok", "messages": _turns(2), "tools": many, "content": "done", "tool_calls": [],
               "role": "implementer", "quality": {"outcome": "ok"}})])
    m = sessions.export(st, ["au"], name="used", max_tokens=8000, validation_fraction=0.0, log=lambda s: None)
    assert m["examples"] == {"train": 1} and m["skipped"] == {} and m["tools"] == "used"
    row, = _read(tmp_path / "s" / "datasets" / "used" / "train.jsonl")
    assert [t["function"]["name"] for t in row["tools"]] == ["write_file"]      # of 61
    assert row["meta"]["tokens"] < 2000
    # all of them: the 61 definitions alone are larger than the example
    m = sessions.export(st, ["au"], name="alltools", max_tokens=8000, tools="all",
                        validation_fraction=0.0, log=lambda s: None)
    assert m["examples"] == {} and m["skipped"] == {"system_prompt_fills_the_example": 1}
    m = sessions.export(st, ["au"], name="alltools2", max_tokens=80000, tools="all",
                        validation_fraction=0.0, log=lambda s: None)
    row, = _read(tmp_path / "s" / "datasets" / "alltools2" / "train.jsonl")
    assert len(row["tools"]) == 61


def test_a_turn_larger_than_an_example_is_shortened_not_dropped(tmp_path):
    st = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)
    msgs = _turns(3)
    msgs[3]["content"] = "START " + "line of tool output\n" * 3000 + " END"      # one huge tool result
    _call(st, "H", 0, "qwen", 1, reply="finished", msgs=msgs)
    m = sessions.export(st, ["au"], name="big", max_tokens=3000, validation_fraction=0.0, log=lambda s: None)
    assert m["skipped"] == {} and m["examples_with_a_shortened_turn"] >= 1
    rows = _read(tmp_path / "s" / "datasets" / "big" / "train.jsonl")
    assert all(r["meta"]["tokens"] <= 3000 for r in rows)
    steps = [x["content"] for r in rows for x in r["messages"] if x["role"] == "assistant"]
    assert steps == ["step 0", "step 1", "step 2", "finished"]                 # no turn was lost
    big = next(x["content"] for r in rows for x in r["messages"] if x["role"] == "tool" and "START" in x["content"])
    assert big.startswith("START ") and big.endswith(" END") and "characters left out of this message" in big


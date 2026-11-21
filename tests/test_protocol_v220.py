"""v0.181.220: a real protocol case says the turn is over.

Flash's first protocol run scored 16.3 on the 20 real coordinator turns and
95.5 on the hard scenarios. Read case by case, 15 of its 19 misses were one
more read or git command, written as tool-call text because no tools were
offered: the real case ended on a tool result or on "Continue task X ... then
report", with nothing saying the turn was over - and the report format itself
had often been cut away with the coordinator's first prompt. The scenarios say
"you cannot run tools now" in their system turn; the real cases said nothing.

Replayed against flash with the note (and with it in the system turn), it
still called a tool in 4 of 4 - so this makes the case fair, it does not make
it easier: a miss now means the model was told the turn was over and kept
going, and the card says that instead of "no coordinator-report block".
"""
import json

from foundry_router import bench, benchcard, protocolbench as pb

F = "```"

# flash's reply to PROTO-real-GF-SAVE1-9, profile 5, verbatim
FLASH_KEPT_WORKING = ("<tool_call> <function=Read> <parameter=file_path> "
                      "C:\\Roblox\\AnimalUprising\\docs\\progress\\GF-SAVE1_PLAN.md "
                      "</parameter> </function> </tool_call>")


def _real(monkeypatch, tmp_path, ex):
    from foundry_router import training_prep
    store = tmp_path / "store"
    store.mkdir()
    (store / "normalized.sqlite").write_text("")
    monkeypatch.setattr(training_prep, "_dirs", lambda svc: {"store": store})
    import foundry_training.store as fts
    monkeypatch.setattr(fts, "TrainingStore", lambda *a, **k: object())
    monkeypatch.setattr(bench, "_examples", lambda *a, **k: iter([ex]))
    return pb.real_cases(type("S", (), {})(), limit=5)


def _report(d):
    return f"{F}coordinator-report\n{json.dumps(d)}\n{F}"


def test_a_case_ending_on_continue_says_the_turn_is_over_in_that_message(monkeypatch, tmp_path):
    ex = {"messages": [
        {"role": "system", "content": "You are Kilo, a highly skilled software engineer"},
        {"role": "user", "content": "Continue task GF-FIX1 where you stopped (the scripted "
                                    "Studio gate PASS at e04a7acb92d7). Same scope."},
        {"role": "assistant", "content": _report({"status": "DONE", "summary": "BUILD done"})}],
        "meta": {"task_id": "GF-FIX1"}}
    c = _real(monkeypatch, tmp_path, ex)[0]
    roles = [m["role"] for m in c["messages"]]
    assert roles == ["system", "user"], "no second user turn in a row"
    last = c["messages"][-1]["content"]
    assert last.startswith("Continue task GF-FIX1")
    assert "end of your turn: no more tools can run" in last
    # the whole format, not "the same block as before" - before may be cut away
    assert '"blocker_kind": "product | tooling | user_action"' in last
    assert c["tools"] == []
    assert ex["messages"][1]["content"].endswith("Same scope."), "the corpus is not changed"


def test_a_case_ending_on_a_tool_result_gets_the_note_after_it(monkeypatch, tmp_path):
    ex = {"messages": [
        {"role": "system", "content": "You are Kilo"},
        {"role": "user", "content": "Continue task UI-D1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "[master 887adb0] UI-D1: evidence"},
        {"role": "assistant", "content": _report({"status": "DONE", "summary": "committed"})}],
        "meta": {"task_id": "UI-D1"}}
    c = _real(monkeypatch, tmp_path, ex)[0]
    assert [m["role"] for m in c["messages"]][-2:] == ["tool", "user"]
    assert c["messages"][-1]["content"] == pb.END_OF_TURN


def test_scenarios_are_unchanged():
    """Their system turn already says it; a second note would only differ from
    the cases the hard-tier 95.5 was measured on."""
    for c in pb.scenario_cases():
        assert all(pb.END_OF_TURN not in str(m.get("content")) for m in c["messages"])


def test_a_reply_that_kept_working_is_named_for_what_it_did():
    s = pb.protocol_score(FLASH_KEPT_WORKING, {"status": "DONE"})
    assert s["score"] == 0.0 and "wrote_call=1" in s["detail"]
    row = {"score": 0.0, "error": "", "tags": '["protocol","real","want:done"]',
           "code_detail": s["detail"], "tools_expected": "[]", "tools_called": "[]"}
    assert benchcard.miss_kind("protocol", row) == \
        "wrote a tool call as text instead of ending the turn"


def test_no_report_and_no_call_is_still_no_report():
    s = pb.protocol_score("All done, the gate passed.", {"status": "DONE"})
    assert "wrote_call" not in s["detail"]
    row = {"score": s["score"], "error": "", "tags": '["protocol"]',
           "code_detail": s["detail"], "tools_expected": "[]", "tools_called": "[]"}
    assert benchcard.miss_kind("protocol", row) == "no coordinator-report block"


def test_a_report_that_quotes_markup_is_a_report():
    text = "I looked at <function=read> output.\n\n" + _report(
        {"status": "DONE", "summary": "s"})
    s = pb.protocol_score(text, {"status": "DONE"})
    assert s["score"] == 100.0 and "wrote_call" not in s["detail"]

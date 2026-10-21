"""Stage 3: the visual pipeline end to end with the primary BUSY:
ready capture -> optional visual admission (per-route image modality) -> the
real image bytes in the mocked vision request -> scoped findings -> code
proposal -> ONE safe-point delivery -> primary acknowledgement -> a new capture
generation. Plus the failure modes. No pre-seeded results: every unit comes
from a capture tool result in the fake Kilo primary transcript."""

import base64
import json
import threading

import pytest

from foundry_coordinator.adapters.foundry import FoundryDenied
from tests.coordinator.helpers import git
from tests.coordinator.lane_env import CLAUDE, VISION, admit, answer_helper, capture_output, \
    finish_primary, png, primary_tool, setup

VIS = """
[automation]
helpers = "proactive"  # revamp B5: lanes are opt-in
[lanes]
enabled = true
transcript_poll_seconds = 0

[lanes.visual]
enabled = true
debounce_seconds = 0
capture_globs = ["docs/evidence/{task}/*.png"]
reference_globs = ["docs/design/{task}_ref.png", "../outside_ref.png"]

[lanes.proposer]
enabled = true
"""
SKILL = "---\nname: au-ui-review\n---\nCheck safe areas and clipping."
VROUTES = [{"backend": "meridian", "model": "claude-sonnet", "vision": True},
           {"backend": "cpa", "model": "gpt-vision", "vision": True}]


@pytest.fixture()
def env(tmp_path):
    (tmp_path / "outside_ref.png").write_bytes(png(2, 2))
    e = setup(tmp_path, lanes=VIS, extra_files={".kilo/skills/au-ui-review/SKILL.md": SKILL})
    e["ff"].admission[VISION] = admit(routes=VROUTES)
    e["ff"].admission[CLAUDE] = admit()
    yield e
    e["fake"].stop()


def lane_units(c, lane=None):
    return [u for u in c.helpers.units("game", "T2") if u.get("lane") and
            (lane is None or u["lane"] == lane)]


def primary_prompts(env):
    return [p for p in env["fake"].prompts if p["session_id"] == env["primary"]]


def capture(env, name="menu.png", rgb=(10, 20, 30), valid=True):
    data = png(6, 4, rgb)
    f = env["root"] / "docs/evidence/T2" / name
    f.write_bytes(data)
    primary_tool(env, f"pwsh tools/capture-window.ps1 -Out docs/evidence/T2/{name}",
                 capture_output(str(f), data, 6, 4, valid=valid))
    return data


def vision_says(env, findings, served="claude-sonnet", image_seen=True, validity="ok"):
    def answer(messages):
        return ("Looks off.\n```helper-result\n" + json.dumps({
            "image_seen": image_seen, "subject_present": True, "validity": validity,
            "summary": "menu clipped", "findings": findings}) + "\n```",
                {"served_model": served, "usage": {"prompt_tokens": 1500, "completion_tokens": 200}})
    env["ff"].vision_answer = answer


HIGH = [{"id": "V1", "severity": "high", "confidence": 0.9, "region": "bottom right",
         "observed": "Play button clipped by the safe area",
         "hypothesis": "AnchorPoint/Position ignore the safe-area inset",
         "paths": ["src/menu.luau:1"], "in_scope": True}]


def step(env, n=1):
    for _ in range(n):
        env["c"].tick()
        env["c"].helpers.lanes.wait_direct()


def test_capture_to_proposal_to_one_delivery_to_recapture(env):
    c, ff = env["c"], env["ff"]
    vision_says(env, HIGH)
    data = capture(env)
    step(env, 2)
    [obs] = lane_units(c, "visual")
    assert obs["status"] == "done" and obs["depth"] == 1
    # the actual image bytes reached the mocked vision request, plus the approved reference
    [call] = ff.chat_calls
    assert call["persona"] == VISION and call["session_id"] == obs["session_id"]
    imgs = [p["image_url"]["url"] for p in call["messages"][1]["content"] if p["type"] == "image_url"]
    assert base64.b64decode(imgs[0].split(",", 1)[1]) == data
    ref = (env["root"] / "docs/design/T2_ref.png").read_bytes()
    assert base64.b64decode(imgs[1].split(",", 1)[1]) == ref and len(imgs) == 2
    assert "Check safe areas" in call["messages"][0]["content"]           # skill inlined
    text = call["messages"][1]["content"][0]["text"]
    assert obs["artifact_id"] and "generation 1" in text and "VALID=OK" in text
    pk = json.loads(obs["packet"])
    assert any("outside" in n for n in pk["reference_notes"])             # unauthorized ref
    assert [s["name"] for s in pk["skills_loaded"]] == ["au-ui-review"]
    b = [x for x in ff.bind_kw if x["session_id"] == obs["session_id"]][0]
    assert b["purpose"] == "visual_observation" and b["parent"] == env["primary"]
    # the confident, in-scope finding became a code-proposal unit (depth 2)
    [prop] = lane_units(c, "proposer")
    assert prop["parent_unit"] == obs["unit_id"] and prop["depth"] == 2
    assert prop["status"] == "running" and prop["artifact_id"] == obs["artifact_id"]
    hp = [p for p in env["fake"].prompts if p["session_id"] == prop["session_id"]][0]["text"]
    assert "Play button clipped" in hp and "au-roblox-ui-development" in hp
    assert obs["artifact_id"] in hp or data and "sha256" in hp
    assert len(primary_prompts(env)) == 1                    # zero extra prompts while busy
    patch = ("--- a/src/menu.luau\n+++ b/src/menu.luau\n@@ -1,2 +1,2 @@\n-local M = {}\n"
             "+local M = {inset = true}\n return M\n")
    answer_helper(env, prop["session_id"], {
        "assignment": prop["unit_id"], "base": prop["snapshot_id"], "summary": "honor inset",
        "findings": [{"id": "P1", "severity": "high", "confidence": 0.8,
                      "observed": "V1 clipped", "hypothesis": "inset", "paths": ["src/menu.luau"]}],
        "patch": patch, "checks_proposed": ["recapture 5 devices"],
        "skills_used": ["au-luau-development"]})
    step(env)
    assert lane_units(c, "proposer")[0]["status"] == "done"
    assert len(primary_prompts(env)) == 1
    finish_primary(env, {"status": "NOT DONE", "summary": "UI pass 1"})
    step(env)
    pp = primary_prompts(env)
    assert len(pp) == 2                                      # exactly one delivery
    card = pp[-1]["text"]
    assert prop["unit_id"] in card and "Visual evidence" in card and "```diff" in card
    assert obs["unit_id"] + " —" not in card                 # observer covered by the proposer card
    assert lane_units(c, "visual")[0]["feedback"] == "display_only"
    # integration acknowledged, then a NEW capture generation of the same subject
    (env["root"] / "src/menu.luau").write_text("local M = {inset = true}\nreturn M\n")
    git(env["root"], "commit", "-qam", "T2: honor inset")
    capture(env, rgb=(50, 60, 70))
    finish_primary(env, {"status": "NOT DONE", "summary": "recaptured",
                         "helper_feedback": [{"unit": prop["unit_id"], "status": "integrated",
                                              "note": "applied; recaptured"}]})
    step(env, 2)
    prop = [u for u in lane_units(c, "proposer") if u["unit_id"] == prop["unit_id"]][0]
    assert prop["feedback"] == "integrated"
    assert json.loads(prop["feedback_detail"])["recaptured_as"]
    arts = c.helpers.lanes.artifacts.list("game", "T2", "capture")
    assert [a["generation"] for a in arts] == [1, 2] and arts[0]["status"] == "superseded"
    assert arts[1]["prev_artifact"] == arts[0]["artifact_id"]
    assert len(lane_units(c, "visual")) == 2                 # the recapture is reviewed afresh
    assert [e for e in c.store.events("game", 200) if e["kind"] == "helper_recapture"]


def test_invalid_capture_is_never_sent(env):
    capture(env, valid=False)
    step(env, 2)
    assert lane_units(env["c"]) == [] and env["ff"].chat_calls == []
    assert [e for e in env["c"].store.events("game", 50) if e["kind"] == "visual_not_reviewed"]


@pytest.mark.parametrize("routes,why", [
    ([{"backend": "meridian", "model": "claude-sonnet"}], "did not report image modality"),
    ([{"backend": "meridian", "model": "claude-sonnet", "vision": True},
      {"backend": "local", "model": "qwen-text", "vision": False}], "has no image input"),
    ([], "no routes")])
def test_unverifiable_or_text_only_routes_never_get_the_image(env, routes, why):
    env["ff"].admission[VISION] = admit(routes=routes)
    capture(env)
    step(env, 2)
    [u] = lane_units(env["c"], "visual")
    assert u["status"] == "deferred" and why in u["reason"]
    assert env["ff"].chat_calls == []


@pytest.mark.parametrize("served,seen,why", [
    ("qwen-text", True, "not one of the vision-validated routes"),
    ("", True, "did not report the served model"),
    ("claude-sonnet", False, "did not confirm it saw the image")])
def test_result_without_confirmed_image_inspection_is_discarded(env, served, seen, why):
    vision_says(env, HIGH, served=served, image_seen=seen)
    capture(env)
    step(env, 2)
    [u] = lane_units(env["c"], "visual")
    assert u["feedback"] == "rejected" and why in u["reason"]
    assert lane_units(env["c"], "proposer") == []
    finish_primary(env, {"status": "DONE", "summary": "x"})
    step(env)
    assert len(primary_prompts(env)) == 1


def test_speculative_finding_goes_to_the_primary_not_to_a_rewrite(env):
    vision_says(env, [{**HIGH[0], "confidence": 0.4}])
    capture(env)
    step(env, 2)
    assert lane_units(env["c"], "proposer") == []
    finish_primary(env, {"status": "NOT DONE", "summary": "x"})
    step(env)
    pp = primary_prompts(env)
    assert len(pp) == 2 and "confidence 0.40" in pp[-1]["text"]


def test_overwrite_before_dispatch_supersedes_and_duplicates_are_ignored(env):
    env["ff"].admission[VISION] = admit("defer", "Session 71% used", routes=VROUTES)
    capture(env, rgb=(1, 1, 1))
    step(env)
    capture(env, rgb=(1, 1, 1))                              # same bytes again: duplicate
    step(env)
    assert len(lane_units(env["c"], "visual")) == 1
    capture(env, rgb=(2, 2, 2))                              # overwritten: new generation
    step(env)
    units = lane_units(env["c"], "visual")
    assert len(units) == 2
    old, new = units
    assert old["status"] == "skipped" and old["feedback"] == "superseded"
    env["ff"].admission[VISION] = admit(routes=VROUTES)
    vision_says(env, [])
    env["clock"][0] += 120
    step(env, 2)
    assert len(env["ff"].chat_calls) == 1
    sent = json.loads(lane_units(env["c"], "visual")[1]["packet"])["sent"][0]
    assert sent["artifact"] == new["artifact_id"]


def test_stale_image_versus_changed_code_flags_a_rebase(env):
    vision_says(env, HIGH)
    capture(env)
    step(env, 2)
    [prop] = lane_units(env["c"], "proposer")
    (env["root"] / "src/menu.luau").write_text("local M = {moved = true}\nreturn M\n")
    answer_helper(env, prop["session_id"], {
        "assignment": prop["unit_id"], "base": prop["snapshot_id"],
        "findings": [{"severity": "high", "confidence": 0.9, "observed": "o"}],
        "patch": "--- a/src/menu.luau\n+++ b/src/menu.luau\n@@ -1 +1 @@\n-local M = {}\n+x\n"})
    step(env)
    finish_primary(env, {"status": "NOT DONE", "summary": "x"})
    step(env)
    assert "rebase and review before applying" in primary_prompts(env)[-1]["text"]


def test_call_time_refusal_defers_without_a_result(env):
    env["ff"].deny = FoundryDenied("budget", "optional_deferred", "fallback over the cutoff", 600)
    capture(env)
    step(env, 2)
    [u] = lane_units(env["c"], "visual")
    assert u["status"] == "deferred" and "call time" in u["reason"] and not u["result"]


def test_restart_during_an_inflight_vision_call_is_not_repeated(env):
    gate = threading.Event()

    def slow(messages):
        gate.wait(5)
        return "late", {"served_model": "claude-sonnet"}
    env["ff"].vision_answer = slow
    capture(env)
    env["c"].tick()
    env["c"].tick()
    assert lane_units(env["c"], "visual")[0]["status"] == "running"
    c2 = env["make"]()                                       # the coordinator restarted
    c2.tick()
    [u] = lane_units(c2, "visual")
    assert u["status"] == "failed" and "restarted" in u["reason"]
    gate.set()
    for _ in range(3):
        c2.tick()
    assert len(env["ff"].chat_calls) == 1                    # never repeated
    assert lane_units(c2, "visual")[0]["status"] == "failed"


def test_pause_discards_an_inflight_result_and_cancels_units(env):
    gate = threading.Event()

    def slow(messages):
        gate.wait(5)
        return "```helper-result\n{\"image_seen\": true}\n```", {"served_model": "claude-sonnet"}
    env["ff"].vision_answer = slow
    capture(env)
    env["c"].tick()
    env["c"].tick()
    env["c"].pause("game", "T2", "Victor stepping in")
    gate.set()
    env["c"].helpers.lanes.wait_direct()
    [u] = lane_units(env["c"], "visual")
    assert u["status"] == "cancelled" and not u["result"]


def test_proposer_never_spawns_more_units(env):
    vision_says(env, HIGH)
    capture(env)
    step(env, 2)
    [prop] = lane_units(env["c"], "proposer")
    answer_helper(env, prop["session_id"], {"assignment": prop["unit_id"], "findings": HIGH})
    step(env, 2)
    assert len(lane_units(env["c"])) == 2


def test_full_helper_capacity_queues_visual_work(tmp_path):
    lanes = VIS + "\n[lanes.failure_triage]\nenabled = true\nmax_running = 3\n"
    (tmp_path / "outside_ref.png").write_bytes(png(2, 2))
    env = setup(tmp_path, lanes=lanes)
    try:
        env["ff"].admission[VISION] = admit(routes=VROUTES)
        env["ff"].admission[CLAUDE] = admit()
        for i in range(3):
            primary_tool(env, "lune run tests/lune/run.luau", f"FAIL case{'abc'[i]} broke",
                         exit_code=1)
        env["c"].tick()
        assert len([u for u in lane_units(env["c"]) if u["status"] == "running"]) == 3
        capture(env)
        step(env, 2)
        [v] = lane_units(env["c"], "visual")
        assert v["status"] == "pending" and env["ff"].chat_calls == []
    finally:
        env["fake"].stop()

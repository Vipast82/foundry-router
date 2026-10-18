"""Scenario review of the visual lane (fakes only: fake `kilo serve`, fake
Foundry admission/vision endpoint, simulated clock, synthetic PNG/JPEG).

  S1  capture -> vision observation -> proposal -> safe-point delivery ->
      primary acts -> recapture -> feedback closes the loop (integrated and
      rejected), and feedback for a unit that was never delivered is ignored
  S2  stale image vs changed code, self-review, unconfirmed image inspection,
      routes without vision modality, invalid/occluded/truncated frames
  S3  filesystem-fallback artifacts (kind artifact, status new) planned and
      observed

Tests marked xfail(strict=True, reason="DEFECT: ...") assert the CORRECT
behavior for a defect this review found; they flip to a failure (XPASS) the
moment the defect is fixed, so the marker must then be removed."""

import base64
import json

import pytest

from tests.coordinator.helpers import git
from tests.coordinator.lane_env import CLAUDE, VISION, admit, answer_helper, capture_output, \
    finish_primary, jpeg, png, primary_tool, setup

VIS = """
[lanes]
enabled = true
transcript_poll_seconds = 0

[lanes.visual]
enabled = true
debounce_seconds = 0
capture_globs = ["docs/evidence/{task}/*.png", "docs/evidence/{task}/*.jpg"]
reference_globs = ["docs/design/{task}_ref.png"]
%s

[lanes.proposer]
enabled = true
"""
VROUTES = [{"backend": "meridian", "model": "claude-sonnet", "vision": True},
           {"backend": "cpa", "model": "gpt-vision", "vision": True}]
HIGH = [{"id": "V1", "severity": "high", "confidence": 0.9, "region": "bottom right",
         "observed": "Play button clipped by the safe area",
         "hypothesis": "AnchorPoint/Position ignore the safe-area inset",
         "paths": ["src/menu.luau:1"], "in_scope": True}]


def make_env(tmp_path, visual_extra=""):
    e = setup(tmp_path, lanes=VIS % visual_extra)
    e["ff"].admission[VISION] = admit(routes=VROUTES)
    e["ff"].admission[CLAUDE] = admit()
    return e


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    yield e
    e["fake"].stop()


def lane_units(c, lane=None):
    return [u for u in c.helpers.units("game", "T2") if u.get("lane") and
            (lane is None or u["lane"] == lane)]


def unit(c, uid):
    return [u for u in lane_units(c) if u["unit_id"] == uid][0]


def primary_prompts(env):
    return [p for p in env["fake"].prompts if p["session_id"] == env["primary"]]


def capture(env, name="menu.png", rgb=(10, 20, 30), valid=True, data=None, rec_wh=None):
    """The busy primary runs the capture procedure; its tool result is the record."""
    data = data if data is not None else png(6, 4, rgb)
    f = env["root"] / "docs/evidence/T2" / name
    f.write_bytes(data)
    w, h = rec_wh or (6, 4)
    primary_tool(env, f"pwsh tools/capture-window.ps1 -Out docs/evidence/T2/{name}",
                 capture_output(str(f), data, w, h, valid=valid))
    return data


def vision_answer(env, findings, served="claude-sonnet", image_seen=True, validity="ok",
                  subject_present=True, extra=None):
    def answer(messages):
        body = {"summary": "menu observation", "findings": findings, "validity": validity,
                "subject_present": subject_present, **(extra or {})}
        if image_seen is not None:
            body["image_seen"] = image_seen
        return ("```helper-result\n" + json.dumps(body) + "\n```",
                {"served_model": served, "usage": {"prompt_tokens": 1500,
                                                   "completion_tokens": 200}})
    env["ff"].vision_answer = answer


def step(env, n=1):
    for _ in range(n):
        env["c"].tick()
        env["c"].helpers.lanes.wait_direct()


def events(env, kind):
    return [e for e in env["c"].store.events("game", 500) if e["kind"] == kind]


PATCH = ("--- a/src/menu.luau\n+++ b/src/menu.luau\n@@ -1,2 +1,2 @@\n-local M = {}\n"
         "+local M = {inset = true}\n return M\n")


def _observe_and_propose(env):
    vision_answer(env, HIGH)
    data = capture(env)
    step(env, 2)
    [obs] = lane_units(env["c"], "visual")
    [prop] = lane_units(env["c"], "proposer")
    return data, obs, prop


def _answer_proposer(env, prop, patch=PATCH):
    answer_helper(env, prop["session_id"], {
        "assignment": prop["unit_id"], "base": prop["snapshot_id"], "summary": "honor inset",
        "findings": [{"id": "P1", "severity": "high", "confidence": 0.8, "observed": "V1",
                      "hypothesis": "inset", "paths": ["src/menu.luau"]}],
        "patch": patch, "checks_proposed": ["recapture"]})
    step(env)


# ============================================================================ S1 loop

def test_s1_loop_closes_on_integrated_with_recapture_lineage(env):
    c, ff = env["c"], env["ff"]
    data, obs, prop = _observe_and_propose(env)
    # the observation saw the snapshotted bytes, bound as optional visual_observation
    [call] = ff.chat_calls
    imgs = [p["image_url"]["url"] for p in call["messages"][1]["content"]
            if p["type"] == "image_url"]
    assert base64.b64decode(imgs[0].split(",", 1)[1]) == data
    assert [b["purpose"] for b in ff.bind_kw if b["session_id"] == obs["session_id"]] == \
        ["visual_observation"]
    assert prop["parent_unit"] == obs["unit_id"] and prop["depth"] == 2
    assert len(primary_prompts(env)) == 1                    # busy primary: nothing sent
    _answer_proposer(env, prop)
    finish_primary(env, {"status": "NOT DONE", "summary": "pass 1"})
    step(env)
    pp = primary_prompts(env)
    assert len(pp) == 2 and prop["unit_id"] in pp[-1]["text"]          # ONE safe-point card
    assert unit(c, prop["unit_id"])["status"] == "delivered"
    assert unit(c, obs["unit_id"])["feedback"] == "display_only"       # covered by the card
    # the primary acts: applies, commits, recaptures, acknowledges
    (env["root"] / "src/menu.luau").write_text("local M = {inset = true}\nreturn M\n")
    git(env["root"], "commit", "-qam", "T2: honor inset")
    vision_answer(env, [])
    capture(env, rgb=(50, 60, 70))
    finish_primary(env, {"status": "NOT DONE", "summary": "recaptured",
                         "helper_feedback": [{"unit": prop["unit_id"], "status": "integrated",
                                              "note": "applied; recaptured"}]})
    step(env, 3)
    p2 = unit(c, prop["unit_id"])
    det = json.loads(p2["feedback_detail"])
    arts = c.helpers.lanes.artifacts.list("game", "T2", "capture")
    assert p2["feedback"] == "integrated" and det["claimed_by"] == "primary"
    assert det["recaptured_as"] == arts[1]["artifact_id"]
    assert [a["generation"] for a in arts] == [1, 2] and arts[0]["status"] == "superseded"
    obs2 = [u for u in lane_units(c, "visual") if u["unit_id"] != obs["unit_id"]]
    assert len(obs2) == 1 and obs2[0]["artifact_id"] == arts[1]["artifact_id"]
    assert json.loads(obs2[0]["packet"])["previous"] == arts[0]["artifact_id"]
    assert len(ff.chat_calls) == 2                           # the recapture is observed afresh
    # an observation is advice only: it never becomes a review or an approval
    assert c.store.reviews("game", "T2") == []
    assert VISION not in c.helpers.authors("game", "T2")


def test_s1_loop_closes_on_rejected_and_is_never_redelivered(env):
    c = env["c"]
    _, obs, prop = _observe_and_propose(env)
    _answer_proposer(env, prop)
    finish_primary(env, {"status": "NOT DONE", "summary": "pass 1"})
    step(env)
    assert len(primary_prompts(env)) == 2
    vision_answer(env, [])
    capture(env, rgb=(90, 90, 90))
    # rejected for the proposal; a claim about the never-delivered observer is ignored
    finish_primary(env, {"status": "NOT DONE", "summary": "not applied",
                         "helper_feedback": [
                             {"unit": prop["unit_id"], "status": "rejected",
                              "note": "inset already handled by the parent frame"},
                             {"unit": obs["unit_id"], "status": "integrated", "note": "x"}]})
    step(env, 3)
    p2 = unit(c, prop["unit_id"])
    det = json.loads(p2["feedback_detail"])
    assert p2["feedback"] == "rejected" and "parent frame" in det["note"]
    assert det["claimed_by"] == "primary" and det["recaptured_as"]
    # the claim about the observer (never delivered) is not recorded; the recapture
    # then marks that older observation superseded
    assert unit(c, obs["unit_id"])["feedback"] in ("display_only", "superseded")
    fb = events(env, "helper_feedback")
    assert [e["detail"]["unit"] for e in fb] == [prop["unit_id"]]
    # no second delivery of the rejected card
    assert sum(prop["unit_id"] in p["text"] for p in primary_prompts(env)) == 1


# ============================================================================ S2 stale / self-review

def test_s2_capture_revision_and_snapshot_are_bound_to_the_observation(env):
    head = git(env["root"], "rev-parse", "HEAD")
    vision_answer(env, [])
    capture(env)
    step(env, 2)
    [obs] = lane_units(env["c"], "visual")
    a = env["c"].helpers.lanes.artifacts.get(obs["artifact_id"])
    assert a["revision"] == head and obs["snapshot_id"]
    text = env["ff"].chat_calls[0]["messages"][1]["content"][0]["text"]
    assert f"revision {head[:12]}" in text and a["sha256"] in text


# fixed in v0.121.7
def test_s2_stale_capture_judged_after_the_code_changed_is_flagged(env):
    c = env["c"]
    env["ff"].admission[VISION] = admit("defer", "Session 71% used", routes=VROUTES)
    vision_answer(env, HIGH)
    capture(env)
    step(env)
    [obs] = lane_units(c, "visual")
    assert obs["status"] in ("pending", "deferred") and env["ff"].chat_calls == []
    # the code the image shows changes before the observer runs (no recapture)
    (env["root"] / "src/menu.luau").write_text("local M = {moved = true}\nreturn M\n")
    git(env["root"], "commit", "-qam", "T2: move menu")
    env["ff"].admission[VISION] = admit(routes=VROUTES)
    env["clock"][0] += 120
    step(env, 2)
    [prop] = lane_units(c, "proposer")
    _answer_proposer(env, prop, patch=(
        "--- a/src/menu.luau\n+++ b/src/menu.luau\n@@ -1,2 +1,2 @@\n-local M = {moved = true}\n"
        "+local M = {moved = true, inset = true}\n return M\n"))
    finish_primary(env, {"status": "NOT DONE", "summary": "x"})
    step(env)
    card = primary_prompts(env)[-1]["text"]
    assert prop["unit_id"] in card
    # correct: the delivered advice must not present a pre-change image as current
    assert "still match the snapshot" not in card
    assert any(w in card.lower() for w in ("changed since the capture", "stale", "rebase"))


# fixed in v0.121.7
def test_s2_compliant_observer_base_is_not_a_base_mismatch(env):
    capture(env)
    env["ff"].admission[VISION] = admit(routes=VROUTES)

    def answer(messages):
        aid = lane_units(env["c"], "visual")[0]["artifact_id"]
        return ("```helper-result\n" + json.dumps({
            "assignment": "x", "base": aid, "image_seen": True, "subject_present": True,
            "validity": "ok", "summary": "fine", "findings": []}) + "\n```",
            {"served_model": "claude-sonnet"})
    env["ff"].vision_answer = answer
    step(env, 2)
    [obs] = lane_units(env["c"], "visual")
    assert obs["status"] == "done"
    assert "base_mismatch" not in json.loads(obs["envelope"])["checks"]


def test_s2_observation_never_counts_as_review_approval_or_author(env):
    """Self-review guard that IS implemented: the observer is bound as an
    optional helper purpose, never as reviewer, and its persona is not a code
    author. (Model-level independence is NOT checked: see the report.)"""
    c, ff = env["c"], env["ff"]
    _, obs, prop = _observe_and_propose(env)
    b = [x for x in ff.bind_kw if x["session_id"] == obs["session_id"]][0]
    assert b["role"] == "helper" and b["purpose"] == "visual_observation"
    _answer_proposer(env, prop)
    finish_primary(env, {"status": "NOT DONE", "summary": "x"})
    step(env)
    assert c.helpers.authors("game", "T2") == {CLAUDE}       # the proposer, not the observer
    assert c.store.reviews("game", "T2") == [] and c.store.approvals("game", "T2") == []


def test_s2_missing_image_seen_claim_is_not_an_inspection(env):
    vision_answer(env, HIGH, image_seen=None)                # key absent, not just false
    capture(env)
    step(env, 2)
    [u] = lane_units(env["c"], "visual")
    env_ = json.loads(u["envelope"])
    assert u["feedback"] == "rejected" and "did not confirm it saw the image" in u["reason"]
    assert env_["not_inspected"] and env_["findings"] == []
    assert lane_units(env["c"], "proposer") == []


def test_s2_served_model_outside_validated_routes_is_discarded(env):
    vision_answer(env, HIGH, served="qwen3.8-flash-next-iq3_s")
    capture(env)
    step(env, 2)
    [u] = lane_units(env["c"], "visual")
    assert u["feedback"] == "rejected" and "no image inspection" in u["reason"]
    assert u["served_model"] == "qwen3.8-flash-next-iq3_s"
    finish_primary(env, {"status": "NOT DONE", "summary": "x"})
    step(env)
    assert len(primary_prompts(env)) == 1                    # nothing delivered


def test_s2_route_outside_approved_models_waits(tmp_path):
    env = make_env(tmp_path, 'approved_models = ["claude-sonnet"]')
    try:
        capture(env)
        step(env, 2)
        [u] = lane_units(env["c"], "visual")
        assert u["status"] == "deferred" and "approved_models" in u["reason"]
        assert env["ff"].chat_calls == []
    finally:
        env["fake"].stop()


@pytest.mark.parametrize("validity,subject", [("occluded", True), ("invalid", True),
                                              ("ok", False)])
def test_s2_frame_the_model_calls_unusable_yields_no_findings(env, validity, subject):
    vision_answer(env, HIGH, validity=validity, subject_present=subject)
    capture(env)
    step(env, 2)
    [u] = lane_units(env["c"], "visual")
    e = json.loads(u["envelope"])
    assert u["status"] == "done" and e["findings"] == [] and e["frame_problem"]
    assert "recapture" in e["limitations"]
    assert lane_units(env["c"], "proposer") == []
    finish_primary(env, {"status": "NOT DONE", "summary": "x"})
    step(env)
    assert len(primary_prompts(env)) == 1


@pytest.mark.parametrize("data", [png(6, 4, truncate=7), jpeg(6, 4, complete=False)],
                         ids=["png-truncated", "jpeg-no-eoi"])
def test_s2_truncated_frame_is_retried_then_rejected_never_sent(env, data):
    name = "menu.png" if data[:4] == b"\x89PNG" else "menu.jpg"
    capture(env, name=name, data=data)
    step(env, 7)
    evs = env["c"].helpers.lanes.events("game", "T2", "capture")
    assert len(evs) == 1 and evs[0]["status"] == "rejected"
    assert "incomplete image" in evs[0]["detail"]["not_ready"]
    assert env["c"].helpers.lanes.artifacts.list("game", "T2") == []
    assert lane_units(env["c"]) == [] and env["ff"].chat_calls == []


def test_s2_frame_with_wrong_dimensions_is_rejected(env):
    capture(env, rec_wh=(8, 8))                              # record says 8x8, file is 6x4
    step(env, 2)
    [ev] = env["c"].helpers.lanes.events("game", "T2", "capture")
    assert ev["status"] == "rejected"
    assert [e for e in events(env, "artifact_rejected") if "8x8" in e["detail"]["reason"]]
    assert lane_units(env["c"]) == [] and env["ff"].chat_calls == []


# ============================================================================ S3 fs fallback

def test_s3_filesystem_fallback_artifact_is_planned_and_observed(tmp_path):
    env = make_env(tmp_path, "fs_fallback = true")
    try:
        c = env["c"]
        primary_tool(env, "ls src", "menu.luau")             # busy, no capture record at all
        ev_dir = env["root"] / "docs/evidence/T2"
        (ev_dir / "menu_INVALID.png").write_bytes(png(6, 4, (1, 1, 1)))   # never a candidate
        part = png(6, 4, (7, 8, 9), truncate=9)
        (ev_dir / "menu.png").write_bytes(part)              # still being written
        vision_answer(env, [])
        step(env, 3)
        assert c.helpers.lanes.artifacts.list("game", "T2") == []
        assert c.helpers.lanes.events("game", "T2", "artifact") == []
        full = png(6, 4, (7, 8, 9))
        (ev_dir / "menu.png").write_bytes(full)              # the write completes
        step(env, 1)                                         # first sight: size/mtime noted
        assert c.helpers.lanes.artifacts.list("game", "T2") == []
        step(env, 3)                                         # stable on the next scan
        [a] = c.helpers.lanes.artifacts.list("game", "T2", "capture")
        assert a["source"] == "filesystem" and a["valid"] == "unknown"
        assert a["rel_path"] == "docs/evidence/T2/menu.png"
        [ev] = c.helpers.lanes.events("game", "T2", "artifact")
        assert ev["detail"]["source"] == "filesystem" and ev["status"] == "planned"
        [obs] = lane_units(c, "visual")
        assert obs["artifact_id"] == a["artifact_id"] and obs["status"] == "done"
        [call] = env["ff"].chat_calls
        img = [p for p in call["messages"][1]["content"] if p["type"] == "image_url"][0]
        assert base64.b64decode(img["image_url"]["url"].split(",", 1)[1]) == full
        text = call["messages"][1]["content"][0]["text"]
        assert "capture record: filesystem" in text and "(none reported)" in text
        step(env, 2)                                         # unchanged file: nothing new
        assert len(c.helpers.lanes.artifacts.list("game", "T2", "capture")) == 1
        assert len(env["ff"].chat_calls) == 1
    finally:
        env["fake"].stop()


def test_s3_filesystem_fallback_off_by_default_ignores_files(env):
    primary_tool(env, "ls src", "menu.luau")
    (env["root"] / "docs/evidence/T2/menu.png").write_bytes(png(6, 4))
    step(env, 3)
    assert env["c"].helpers.lanes.artifacts.list("game", "T2") == []
    assert lane_units(env["c"]) == []

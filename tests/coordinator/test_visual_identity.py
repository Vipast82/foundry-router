"""v0.121.7: image/source identity and stale capture (fakes only).

Every capture is bound at plan time to the source snapshot it was judged
against. The observer and its proposer compare THAT snapshot with the current
code; when they differ both results carry `stale_image` and the delivered card
says so plainly. The observer names the image and the source separately
(artifact_id / source_snapshot_id); a legacy `base`-only answer matches on the
artifact id."""

import json

import pytest

from foundry_coordinator import lanes as L
from foundry_coordinator.visual import STALE_IMAGE_NOTE, visual_identity_mismatch
from tests.coordinator.helpers import git
from tests.coordinator.lane_env import CLAUDE, VISION, admit, answer_helper, capture_output, \
    finish_primary, png, primary_tool, setup

VIS = """
[lanes]
enabled = true
transcript_poll_seconds = 0

[lanes.visual]
enabled = true
debounce_seconds = 0
capture_globs = ["docs/evidence/{task}/*.png"]
reference_globs = ["docs/design/{task}_ref.png"]

[lanes.proposer]
enabled = true
"""
VROUTES = [{"backend": "meridian", "model": "claude-sonnet", "vision": True}]
HIGH = {"id": "V1", "severity": "high", "confidence": 0.9, "region": "bottom right",
        "observed": "Play button clipped by the safe area",
        "hypothesis": "AnchorPoint/Position ignore the safe-area inset",
        "paths": ["src/menu.luau:1"], "in_scope": True}
# out of scope -> not routed to the proposer, so the observer card is delivered too
SIDE = {"id": "V2", "severity": "high", "confidence": 0.9, "region": "top",
        "observed": "Title overlaps the HUD", "hypothesis": "", "paths": [], "in_scope": False}
PATCH = ("--- a/src/menu.luau\n+++ b/src/menu.luau\n@@ -1,2 +1,2 @@\n-local M = {}\n"
         "+local M = {inset = true}\n return M\n")


@pytest.fixture()
def env(tmp_path):
    e = setup(tmp_path, lanes=VIS)
    e["ff"].admission[VISION] = admit(routes=VROUTES)
    e["ff"].admission[CLAUDE] = admit()
    yield e
    e["fake"].stop()


def lane_units(c, lane=None):
    return [u for u in c.helpers.units("game", "T2") if u.get("lane") and
            (lane is None or u["lane"] == lane)]


def unit(c, uid):
    return [u for u in lane_units(c) if u["unit_id"] == uid][0]


def envelope(c, uid):
    return json.loads(unit(c, uid)["envelope"] or "{}")


def primary_prompts(env):
    return [p for p in env["fake"].prompts if p["session_id"] == env["primary"]]


def step(env, n=1):
    for _ in range(n):
        env["c"].tick()
        env["c"].helpers.lanes.wait_direct()


def capture(env, name="menu.png", rgb=(10, 20, 30)):
    data = png(6, 4, rgb)
    f = env["root"] / "docs/evidence/T2" / name
    f.write_bytes(data)
    primary_tool(env, f"pwsh tools/capture-window.ps1 -Out docs/evidence/T2/{name}",
                 capture_output(str(f), data, 6, 4))
    return data


def vision(env, findings, ident=None):
    """ident(unit) -> extra identity keys for the answer (artifact_id, ...)."""
    def answer(messages):
        [u] = [x for x in lane_units(env["c"], "visual") if x["status"] == "running"]
        body = {"assignment": u["unit_id"], "image_seen": True, "subject_present": True,
                "validity": "ok", "summary": "menu observation", "findings": findings,
                **((ident or (lambda _u: {}))(u))}
        return ("```helper-result\n" + json.dumps(body) + "\n```",
                {"served_model": "claude-sonnet", "usage": {}})
    env["ff"].vision_answer = answer


def change_code(env, text="local M = {moved = true}\nreturn M\n"):
    (env["root"] / "src/menu.luau").write_text(text)
    git(env["root"], "commit", "-qam", "T2: change menu")


def answer_proposer(env, prop, patch=PATCH):
    answer_helper(env, prop["session_id"], {
        "assignment": prop["unit_id"], "base": prop["snapshot_id"], "summary": "honor inset",
        "findings": [{"id": "P1", "severity": "high", "confidence": 0.8, "observed": "V1",
                      "hypothesis": "inset", "paths": ["src/menu.luau"]}],
        "patch": patch})
    step(env)


def stale_observation(env, findings):
    """Capture, defer the observer, change the code, then let it run."""
    env["ff"].admission[VISION] = admit("defer", "Session 71% used", routes=VROUTES)
    vision(env, findings)
    capture(env)
    step(env)
    [obs] = lane_units(env["c"], "visual")
    assert obs["status"] in ("pending", "deferred") and obs["snapshot_id"]
    change_code(env)
    env["ff"].admission[VISION] = admit(routes=VROUTES)
    env["clock"][0] += 120
    step(env, 2)
    return unit(env["c"], obs["unit_id"])


# ============================================================================ stale

def test_stale_flag_on_both_cards(env):
    c = env["c"]
    obs = stale_observation(env, [HIGH, SIDE])
    oe = envelope(c, obs["unit_id"])
    st = oe["stale_image"]
    snap = c.helpers.lanes.artifacts.snapshot(obs["snapshot_id"], "game", "T2")
    assert st["captured_at_snapshot"] == obs["snapshot_id"]
    assert st["captured_fp"] == snap["content_fp"] and st["current_fp"] != snap["content_fp"]
    # the proposer targets the image's snapshot, not a new one taken after the change
    [prop] = lane_units(c, "proposer")
    assert prop["snapshot_id"] == obs["snapshot_id"]
    answer_proposer(env, prop)
    assert envelope(c, prop["unit_id"])["stale_image"]["captured_at_snapshot"] == \
        obs["snapshot_id"]
    finish_primary(env, {"status": "NOT DONE", "summary": "x"})
    step(env)
    card = primary_prompts(env)[-1]["text"]
    assert obs["unit_id"] in card and prop["unit_id"] in card
    flags = [ln for ln in card.splitlines() if ln.startswith("STALE IMAGE: ")]
    assert len(flags) == 2 and all(STALE_IMAGE_NOTE in ln for ln in flags)   # one per card
    assert "still match the snapshot" not in card
    # the patch was written against the snapshot the image shows: it no longer applies
    assert "rebase and review before applying" in card


def test_no_stale_flag_when_the_code_is_unchanged(env):
    c = env["c"]
    vision(env, [HIGH, SIDE])
    capture(env)
    step(env, 2)
    [obs] = lane_units(c, "visual")
    [prop] = lane_units(c, "proposer")
    assert prop["snapshot_id"] == obs["snapshot_id"]
    answer_proposer(env, prop)
    assert "stale_image" not in envelope(c, obs["unit_id"])
    assert "stale_image" not in envelope(c, prop["unit_id"])
    finish_primary(env, {"status": "NOT DONE", "summary": "x"})
    step(env)
    card = primary_prompts(env)[-1]["text"]
    assert obs["unit_id"] in card and prop["unit_id"] in card
    assert "STALE IMAGE" not in card and STALE_IMAGE_NOTE not in card
    assert "the affected files still match the snapshot" in card


def test_no_findings_carried_silently_onto_newer_code(env):
    """Code that moves AFTER the observation returned is caught at delivery."""
    c = env["c"]
    vision(env, [HIGH])
    capture(env)
    step(env, 2)
    [obs] = lane_units(c, "visual")
    [prop] = lane_units(c, "proposer")
    answer_proposer(env, prop)
    assert "stale_image" not in envelope(c, prop["unit_id"])
    change_code(env, "local M = {inset = false}\nreturn M\n")
    finish_primary(env, {"status": "NOT DONE", "summary": "x"})
    step(env)
    card = primary_prompts(env)[-1]["text"]
    assert prop["unit_id"] in card and STALE_IMAGE_NOTE in card
    assert "older frame" in card and "still match the snapshot" not in card
    assert envelope(c, prop["unit_id"])["stale_image"]["captured_at_snapshot"] == \
        obs["snapshot_id"]


def test_stale_observer_tells_its_proposer_the_image_predates_the_code(env):
    c = env["c"]
    stale_observation(env, [HIGH])
    [prop] = lane_units(c, "proposer")
    pk = json.loads(prop["packet"])
    assert pk["stale_image"] and "STALE IMAGE" in pk["evidence"]
    text = "\n".join(p["text"] for p in env["fake"].prompts
                     if p["session_id"] == prop["session_id"])
    assert "image predates the current code" in text


# ============================================================================ identity

def test_observer_request_carries_both_exact_ids(env):
    vision(env, [])
    capture(env)
    step(env, 2)
    [obs] = lane_units(env["c"], "visual")
    text = env["ff"].chat_calls[0]["messages"][1]["content"][0]["text"]
    assert f"artifact_id {obs['artifact_id']}" in text
    assert f"source_snapshot_id {obs['snapshot_id']}" in text
    system = env["ff"].chat_calls[0]["messages"][0]["content"]
    assert '"artifact_id"' in system and '"source_snapshot_id"' in system
    assert '"base"' not in system


def test_compliant_identity_is_not_a_mismatch(env):
    vision(env, [], ident=lambda u: {"artifact_id": u["artifact_id"],
                                     "source_snapshot_id": u["snapshot_id"]})
    capture(env)
    step(env, 2)
    [obs] = lane_units(env["c"], "visual")
    assert obs["status"] == "done"
    assert "base_mismatch" not in envelope(env["c"], obs["unit_id"])["checks"]


@pytest.mark.parametrize("field", ["artifact_id", "source_snapshot_id"])
def test_mismatched_identity_is_still_flagged(env, field):
    def ident(u):
        good = {"artifact_id": u["artifact_id"], "source_snapshot_id": u["snapshot_id"]}
        return {**good, field: "wrong-" + field}
    vision(env, [], ident=ident)
    capture(env)
    step(env, 2)
    [obs] = lane_units(env["c"], "visual")
    mm = envelope(env["c"], obs["unit_id"])["checks"]["base_mismatch"]
    assert "wrong-" + field in mm


def test_image_sha_is_accepted_as_the_artifact_id(env):
    vision(env, [], ident=lambda u: {
        "artifact_id": env["c"].helpers.lanes.artifacts.get(u["artifact_id"])["sha256"],
        "source_snapshot_id": u["snapshot_id"]})
    capture(env)
    step(env, 2)
    [obs] = lane_units(env["c"], "visual")
    assert "base_mismatch" not in envelope(env["c"], obs["unit_id"])["checks"]


def test_legacy_base_only_result_is_accepted(env):
    vision(env, [], ident=lambda u: {"base": u["artifact_id"]})
    capture(env)
    step(env, 2)
    [obs] = lane_units(env["c"], "visual")
    assert "base_mismatch" not in envelope(env["c"], obs["unit_id"])["checks"]


def test_legacy_base_only_wrong_value_is_flagged():
    u = {"artifact_id": "art-1", "snapshot_id": "ss-1"}
    assert visual_identity_mismatch(u, {"base": "art-1"}) == ""
    assert visual_identity_mismatch(u, {"base": "ss-1"}) == ""
    assert "elsewhere" in visual_identity_mismatch(u, {"base": "elsewhere"})
    assert visual_identity_mismatch(u, {}) == ""
    assert visual_identity_mismatch({}, {"base": "x"}) == ""     # old row without ids


def test_stored_legacy_row_with_old_base_mismatch_is_reusable(env):
    """A pre-v0.121.7 row recorded base_mismatch for a compliant base ==
    artifact id answer; it must not crash or force a re-observation."""
    c = env["c"]
    vision(env, [])
    capture(env)
    step(env, 2)
    [obs] = lane_units(c, "visual")
    legacy = {"structured": True, "assignment": obs["unit_id"], "base": obs["artifact_id"],
              "summary": "old", "findings": [],
              "checks": {"base_mismatch": f"result names base {obs['artifact_id']}, "
                                          f"assignment was {obs['snapshot_id']}"}}
    c.store.x("UPDATE helper_units SET envelope=? WHERE unit_id=?",
              (json.dumps(legacy), obs["unit_id"]))
    P = c.projects["game"]
    t = c.store.task("game", "T2")
    lanes = c.helpers.lanes
    assert lanes.add(P, t, "visual", obs["artifact_id"], {}, L.settings(P)) == \
        (obs["unit_id"], "coalesced")
    # a genuinely wrong legacy base is still not reused
    legacy["base"] = "art-elsewhere"
    c.store.x("UPDATE helper_units SET envelope=? WHERE unit_id=?",
              (json.dumps(legacy), obs["unit_id"]))
    uid, how = lanes.add(P, t, "visual", obs["artifact_id"], {}, L.settings(P))
    assert uid != obs["unit_id"]
    # the stale check tolerates the old row's packet (no plan-time identity keys)
    c.store.x("UPDATE helper_units SET packet=? WHERE unit_id=?", ("{}", obs["unit_id"]))
    assert lanes.stale_image(P, unit(c, obs["unit_id"])) is None

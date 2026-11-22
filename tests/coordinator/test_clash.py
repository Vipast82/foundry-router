"""A code review finding that forbids a file the visual fix needed is asked about once
(clash.py; item 25 of COORDINATOR_AUTOMATION_NEEDS)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from foundry_coordinator import clash
from foundry_coordinator import states as S
from tests.coordinator.test_budget_policy import game, task

HUD = "src/client/UI/HudLayout.lua"
OTHER = "src/client/UI/TimerLabel.lua"


def review(findings, sha="b" * 40, repairs=()):
    return {"review_id": "rev-1", "reviewed_sha": sha, "verdict": "CHANGES_REQUIRED", "valid": 1,
            "result": {"findings": findings, "required_repairs": list(repairs)}}


def test_which_findings_forbid_a_file_that_was_changed():
    changed = [HUD, OTHER, "docs/evidence/POL-7/a.png"]
    # POL-7's own words
    hit = clash.find(review([{"severity": "blocker", "file": HUD, "message":
                              "The plan forbids editing completed sprints' files; UI-04 owns "
                              "HudLayout.lua."}]), changed)
    assert hit == [(HUD, "The plan forbids editing completed sprints' files; UI-04 owns "
                         "HudLayout.lua.")]
    # named in the text only, other wordings, a required repair
    for msg in ("src/client/UI/HudLayout.lua is out of scope for this sprint",
                "HudLayout.lua must not be modified here",
                "Revert the changes to HudLayout.lua: it belongs to a completed sprint"):
        assert [p for p, _ in clash.find(review([{"severity": "major", "message": msg}]),
                                         changed)] == [HUD], msg
    assert clash.find(review([], repairs=["TimerLabel.lua is not in the file list; revert the "
                                          "edits to it"]), changed) == [
        (OTHER, "TimerLabel.lua is not in the file list; revert the edits to it")]
    # a finding ABOUT the file that forbids nothing, or one that forbids another file: no clash
    assert clash.find(review([{"severity": "major", "file": HUD, "message":
                               "the anchor math is off by one"}]), changed) == []
    assert clash.find(review([{"severity": "major", "message":
                               "src/server/Match.lua must not be edited"}]), changed) == []
    assert clash.find(review([]), changed) == []


def world(tmp_path, changed):
    c, P, clk = game(tmp_path)
    P.ws = SimpleNamespace(changed_files=lambda base, head: list(changed), head=lambda: "b" * 40,
                           path=P.ws.path)
    clash.record_visual(c, P, task(c), "a" * 40, [
        "docs/evidence/POL-7/hud_phone.png: the timer overlaps the wave label"], 3)
    return c, P


FORBID = [{"severity": "blocker", "file": HUD,
           "message": "The plan forbids editing completed sprints' files (HudLayout.lua)."}]


def test_replay_the_review_forbids_what_the_visual_fix_needed_and_it_is_asked_once(tmp_path):
    c, P = world(tmp_path, [HUD, "docs/evidence/T1/hud_phone.png"])
    text = clash.check(c, P, task(c), review(FORBID))
    assert text.startswith("two required findings clash. Visual review round 3 asked for: "
                           "docs/evidence/POL-7/hud_phone.png: the timer overlaps the wave label.")
    assert f"The fix changed {HUD}." in text and "forbids editing completed sprints" in text
    assert "Unblock with which one stands" in text
    [e] = [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind='findings_clash'")]
    assert e["files"] == [HUD] and e["visual_round"] == 3
    # the same files again: not asked again, the review's findings go on as usual
    assert clash.check(c, P, task(c), review(FORBID, sha="c" * 40)) is None
    assert c.store.q("SELECT COUNT(*) AS n FROM events WHERE kind='findings_clash'")[0]["n"] == 1
    # another file clashing later is its own question
    P.ws.changed_files = lambda base, head: [HUD, OTHER]
    again = clash.check(c, P, task(c), review(FORBID + [
        {"severity": "major", "message": "TimerLabel.lua is outside the scope of this sprint"}],
        sha="d" * 40))
    assert again and OTHER in again


def test_no_visual_round_no_changed_file_or_a_broken_diff_never_stops_anything(tmp_path):
    c, P, clk = game(tmp_path)
    P.ws = SimpleNamespace(changed_files=lambda base, head: [HUD], path=P.ws.path)
    assert clash.check(c, P, task(c), review(FORBID)) is None          # no visual round yet
    c2, P2 = world(tmp_path / "b", ["src/client/UI/Unrelated.lua"])
    assert clash.check(c2, P2, task(c2), review(FORBID)) is None       # the fix did not touch it
    c3, P3 = world(tmp_path / "c", [HUD])
    P3.ws.changed_files = lambda base, head: 1 / 0
    assert clash.check(c3, P3, task(c3), review(FORBID)) is None       # never raises
    c4, P4 = world(tmp_path / "d", [HUD])
    assert clash.check(c4, P4, task(c4), review(FORBID, sha="a" * 40)) is None   # same revision


def test_the_engine_stops_the_sprint_with_the_question_instead_of_looping(tmp_path):
    c, P = world(tmp_path, [HUD])
    c.store.update_task("game", "T1", state=S.REVIEWING, phase="review")
    rev = review(FORBID)
    c._apply_review(P, task(c), rev)
    t = task(c)
    assert t["state"] == S.BLOCKED and t["blocker"].startswith("two required findings clash.")
    # asked once: the next review that says the same sends its findings to the implementer
    c.store.update_task("game", "T1", state=S.REVIEWING, phase="review", blocker=None)
    c._apply_review(P, task(c), review(FORBID, sha="c" * 40))
    t = task(c)
    assert t["state"] == S.CHANGES_REQUIRED
    assert any("forbids editing completed sprints" in f for f in t["data"]["findings"])


def test_the_visual_review_records_what_it_sent_back(tmp_path):
    from foundry_coordinator import visual_approval as VA
    c, P, clk = game(tmp_path)
    sent = []
    c.reject_human = lambda pid, tid, findings, actor: sent.append(findings)
    P.profile.raw.setdefault("approval", {}).update(visual_rounds_max=8,
                                                    visual_rounds_before_judge=8)
    VA._changes(c, P, task(c), "a" * 40, {}, "model:vision/x", ["f.png: overlap"])
    vf = task(c)["data"]["visual_fix"]
    assert vf["revision"] == "a" * 40 and vf["round"] == 1 and vf["findings"] == ["f.png: overlap"]
    assert sent == [["f.png: overlap"]]

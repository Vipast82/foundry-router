"""TURNAROUND_RECIPE_PLAN (gap 5, D3): a sprint whose plan lists `ref_views` beside `images`
draws reference views from an approved 3D sheet. With [automation] ref_views_images =
"allow" it starts without the 2D decision; with the default "ask" it waits like any images
sprint."""

from __future__ import annotations

import os
from types import SimpleNamespace

from foundry_coordinator import services as SV
from foundry_coordinator import states as S
from tests.coordinator.helpers import (by_task, coordinator, make_repo, run_until, write_ok,
                                       write_profile)

TASKS = [
    {"id": "R1", "title": "turnaround views", "spec": "Write art/r.txt.", "scope": ["art/r.txt"],
     "tags": ["needs:images", "needs:ref_views"]},
    {"id": "I1", "title": "icon sprint", "spec": "Write art/i.txt.", "scope": ["art/i.txt"],
     "tags": ["needs:images"]},
]

SKILL = os.path.join(os.path.dirname(__file__), "..", "..", "contrib", "media-adapter",
                     "skills", "foundry-images", "SKILL.md")


def env(tmp_path, extra=""):
    root, _ = make_repo(tmp_path, "game", TASKS, extra_files={"kilo.jsonc": '{"mcp": {}}\n'})
    prof = write_profile(tmp_path, "game", root, extra=extra, publish="none", review=False)
    c = coordinator(tmp_path, [prof], {"game": by_task({
        "R1": write_ok("art/r.txt"), "I1": write_ok("art/i.txt")})})
    return c, c.projects["game"]


ALLOW = '\n[automation]\nref_views_images = "allow"\n'


def test_the_word_is_found_in_needs_data_and_tags():
    plan = "# X\n\n## Needs\n- blender\n- images (ref_views: tq, side_-X)\n\n## Steps\nref_views\n"
    assert SV.ref_views_declared({}, SimpleNamespace(spec=plan, tags=[], data={}))
    assert SV.parse_needs(plan) == ["blender", "images"]           # the need words are unchanged
    late = "# X\n\n## Needs\n- images\n\n## Steps\n- draw ref views\n"
    assert not SV.ref_views_declared({}, SimpleNamespace(spec=late, tags=[], data={}))
    assert SV.ref_views_declared({"data": {"ref_views": True}}, None)
    assert SV.ref_views_declared({}, SimpleNamespace(spec="", tags=["needs:ref-views"], data={}))
    assert not SV.ref_views_declared({}, SimpleNamespace(spec="", tags=["needs:images"], data={}))


def test_ask_is_the_default_and_keeps_the_2d_decision(tmp_path):
    c, P = env(tmp_path)
    assert SV.ref_views_mode(P) == "ask"
    c.approve("game", "R1", [], "victor")
    for _ in range(4):
        c.tick()
    assert c.store.task("game", "R1")["state"] in (S.QUEUED, S.READY) and not P.client.sessions
    assert SV.images_decision(c, "game", "R1") is not None


def test_allow_starts_a_ref_views_sprint_without_a_decision_but_not_an_icon_sprint(tmp_path):
    c, P = env(tmp_path, extra=ALLOW)
    c.approve("game", "R1", [], "victor")
    assert run_until(c, lambda: c.store.task("game", "R1")["state"] == S.COMPLETE, limit=40)
    assert SV.images_decision(c, "game", "R1") is None
    t = c.store.task("game", "R1")
    assert SV.images_allowed(c, P, t, ["images"], SimpleNamespace(spec="", data={},
                                                                  tags=["needs:ref_views"]))
    ev = [e for e in c.store.events("game", 200) if e["kind"] == "ref_views_standing_ok"]
    assert len(ev) == 1                                             # once per task
    c.approve("game", "I1", [], "victor")
    for _ in range(4):
        c.tick()
    assert c.store.task("game", "I1")["state"] in (S.QUEUED, S.READY)
    assert SV.images_decision(c, "game", "I1") is not None          # icons still ask


def test_deny_beats_the_standing_ok_and_the_brief_names_the_limits(tmp_path):
    c, P = env(tmp_path, extra='\n[automation]\nref_views_images = "allow"\n'
                               'media_gen_images = "deny"\n')
    spec = SimpleNamespace(spec="", data={}, tags=["needs:ref_views"])
    t = {"task_id": "R1"}
    assert not SV.ref_views_ok(P, t, ["images"], spec)
    assert not SV.images_allowed(c, P, t, ["images"], spec)
    c2, P2 = env(tmp_path / "b", extra=ALLOW)
    brief = SV.images_brief(c2, P2, t, ["images"], spec)
    assert brief == SV.REF_VIEWS_BRIEF
    for word in ("Turnaround", "ref/drawn/", "six images", "BLOCKED `user_action`",
                 "ref_align.py"):
        assert word in brief
    assert "reference views only" not in SV.images_brief(c2, P2, t, ["images"], None)


def test_the_skill_has_the_turnaround_recipe():
    text = open(SKILL, encoding="utf-8").read()
    assert "## Turnaround views" in text
    for view in ("tq_+X", "side_+X", "side_-X", "back", "top", "under", "sheet"):
        assert f"`{view}`" in text
    assert "ref/drawn/" in text and "screen-left" in text and "--check-only" in text

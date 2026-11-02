"""BUILDER_PER_MODEL_PLAN (gap 9): with [builder] enabled, Foundry writes the au-builder
subagent into kilo.jsonc and the matching AS- implementer may call it; off by default."""

from __future__ import annotations

from types import SimpleNamespace

from foundry_automation import jsonc
from foundry_coordinator import builder as B
from foundry_coordinator import config_pull, kilo_config as K
from foundry_coordinator.adapters.base import TaskSpec
from foundry_coordinator.lead import kilo_decision
from foundry_coordinator.profile import Profile
from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter

SOURCE = """{
  "agent": {
    "code": {"model": "foundry/Foundry-Kilo-Local", "permission": {"*": "allow"}},
    "au-review": {"permission": {"*": "deny"}}
  },
  "mcp": {"foundry-media": {"type": "local", "command": ["x.ps1"], "enabled": true}}
}"""
RULES = {"implementer": {"permissions": {"studio_read": "allow", "studio_write": "allow"}},
         "reviewer": {}}
ON = {"builder": {"enabled": True, "tasks": ["AS-J03*", "AS-G02R"]}}


def adapter(tmp_path, raw=None):
    profile = Profile(id="animaluprising", name="AU", version="1", path="x.toml", hash="h",
                      workspace=str(tmp_path), allowed_dirs=["src"], branch="master",
                      remote="origin", raw=raw or {}, client={},
                      roles={"implementer": "Foundry-Kilo-Implementer"})
    return AnimalUprisingAdapter(profile, str(tmp_path / "state"))


def spec(tid):
    return TaskSpec(project_id="animaluprising", task_id=tid, title="t", spec="plan",
                    scope=["tools/blender/build_x.py"], data={"tier": "C"})


def test_off_by_default(tmp_path):
    p = SimpleNamespace(raw={})
    assert not B.settings(p)["enabled"] and B.agent_block(p, RULES, str(tmp_path)) is None
    assert B.notice(p, "AS-J03") == "" and B.session_rules(p, "AS-J03", "implementer") == []
    rules = adapter(tmp_path).session_permissions(spec("AS-J03"), "implement")
    assert kilo_decision(rules, "task", "au-builder") == "deny"


def test_the_agent_block(tmp_path):
    blk = B.agent_block(SimpleNamespace(raw=ON), RULES, str(tmp_path))
    assert blk["mode"] == "subagent" and blk["steps"] == 200 and "ONE 3D asset" in blk["prompt"]
    assert "model" not in blk                      # the calling session's model
    perm = blk["permission"]
    assert perm["task"] == "deny" and perm["skill"]["au-blender-asset"] == "allow"
    for p in ("git commit*", "git push*", "git tag*"):
        assert perm["bash"][p] == "deny"
    assert perm["Roblox_Studio_*"] == "deny" and perm["foundry-media_*"] == "deny"
    with_model = B.agent_block(SimpleNamespace(raw={"builder": {**ON["builder"],
                                                                "model": "foundry/X"}}),
                               RULES, str(tmp_path))
    assert with_model["model"] == "foundry/X"


def test_written_into_the_main_and_track_kilo_jsonc(tmp_path):
    path = tmp_path / "kilo.jsonc"
    path.write_text(SOURCE, encoding="utf-8")
    prof = SimpleNamespace(id="g", workspace=str(tmp_path), raw={**ON, "role_rules": RULES})
    out = K.regenerate(prof, apply=True, path=str(path))
    assert "au-builder" in out["changed"]
    data = jsonc.parse(path.read_text(encoding="utf-8"))
    assert data["agent"]["au-builder"]["mode"] == "subagent"
    assert data["agent"]["code"]["model"] == "foundry/Foundry-Kilo-Local"    # untouched
    again = K.regenerate(prof, apply=True, path=str(path))
    assert "au-builder" not in again["changed"]                              # idempotent
    track = SimpleNamespace(worktree=str(tmp_path / "wt"), needs=["blender"], name="t")
    tp = K.track_kilo_config(track, profile=prof, source=str(path))
    assert jsonc.parse(open(tp, encoding="utf-8").read())["agent"]["au-builder"]["steps"] == 200
    off = SimpleNamespace(id="g", workspace=str(tmp_path), raw={"role_rules": RULES})
    assert K.extra_agents(off, RULES, str(tmp_path)) == {}


def test_only_a_matching_implementer_may_call_it_and_its_prompt_says_so(tmp_path):
    a = adapter(tmp_path, raw=ON)
    assert kilo_decision(a.session_permissions(spec("AS-J03R"), "implement"),
                         "task", "au-builder") == "allow"
    assert kilo_decision(a.session_permissions(spec("AS-J03R"), "implement"),
                         "task", "au-planner") == "deny"
    assert kilo_decision(a.session_permissions(spec("AS-E10"), "implement"),
                         "task", "au-builder") == "deny"
    assert kilo_decision(a.session_permissions(spec("AS-J03R"), "helper"),
                         "task", "au-builder") == "deny"
    assert "BUILDER PER MODEL" in B.notice(a.p, "as-g02r")
    assert B.notice(a.p, "AS-E10") == ""
    assert config_pull.role_rules_changed({"settings": {"builder": {"enabled": False}}},
                                          {"settings": {"builder": {"enabled": True}}})

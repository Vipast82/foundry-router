"""Revamp B6: sessions may not spawn their own subagents except `explore`; the
AU prompts no longer ask for a per-attempt planner or model-run vision review."""

from foundry_coordinator.adapters.base import TaskSpec
from foundry_coordinator.lead import kilo_decision, lead_permissions
from foundry_coordinator.profile import Profile
from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter


def adapter(tmp_path, raw=None, client=None):
    profile = Profile(id="animaluprising", name="AU", version="1", path="x.toml", hash="h",
                      workspace=str(tmp_path), allowed_dirs=["src"], branch="master",
                      remote="origin", raw=raw or {}, client=client or {},
                      roles={"implementer": "Foundry-Kilo-Implementer",
                             "planner": "Foundry-Kilo-Planner"})
    return AnimalUprisingAdapter(profile, str(tmp_path / "state"))


SPEC = TaskSpec(project_id="animaluprising", task_id="UI-02b", title="HUD", spec="plan",
                scope=["src/client/UI/Hud.lua"], data={"tier": "B"})


def test_implementer_may_spawn_explore_only(tmp_path):
    rules = adapter(tmp_path).session_permissions(SPEC, "implement")
    assert kilo_decision(rules, "task", "explore") == "allow"
    for agent in ("au-planner", "au-visual-review", "au-helper", "general"):
        assert kilo_decision(rules, "task", agent) == "deny", agent


def test_a_profile_task_allow_cannot_widen_the_subagent_list(tmp_path):
    a = adapter(tmp_path, client={"permissions": {"task": "allow"}})
    rules = a.session_permissions(SPEC, "implement")
    assert kilo_decision(rules, "task", "au-planner") == "deny"


def test_the_allow_list_is_a_profile_key(tmp_path):
    a = adapter(tmp_path, raw={"automation": {"subagents_allowed": ["explore", "au-lint"]}})
    rules = a.session_permissions(SPEC, "implement")
    assert kilo_decision(rules, "task", "au-lint") == "allow"
    assert kilo_decision(rules, "task", "au-planner") == "deny"


def test_helpers_and_the_read_only_lead_spawn_nothing(tmp_path):
    a = adapter(tmp_path)
    assert kilo_decision(a.helper_permissions(SPEC), "task", "explore") == "deny"

    class P:
        adapter = a
        profile = a.p
    assert kilo_decision(lead_permissions(P, SPEC), "task", "explore") == "deny"


def test_prompts_name_no_planner_or_vision_children(tmp_path):
    a = adapter(tmp_path)
    for text in (a.implement_prompt(SPEC, {"attempt": 1, "run_id": "r"}),
                 a.repair_prompt(SPEC, ["S46 failed"], {"attempt": 2, "run_id": "r"})):
        assert "au-planner" not in text and "au-visual-review" not in text
        assert "use the enabled vision subagent" not in text
        assert "do not spawn planner or vision subagents" in text

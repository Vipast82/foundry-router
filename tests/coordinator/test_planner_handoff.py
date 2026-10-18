"""Planner advice is opt-in; the coordinator still owns checks and advancement."""

from foundry_coordinator.adapters.base import TaskSpec
from foundry_coordinator.profile import Profile
from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter


def adapter(tmp_path, planner=True):
    roles = {"implementer": "Foundry-Kilo-Local"}
    if planner:
        roles["planner"] = "Foundry-Kilo-Planner"
    profile = Profile(id="animaluprising", name="Animal Uprising", version="1",
                      path="fixture.toml", hash="fixture", workspace=str(tmp_path),
                      allowed_dirs=["src", "tests", "docs"], branch="master",
                      remote="origin", raw={}, roles=roles)
    return AnimalUprisingAdapter(profile, str(tmp_path / "state"))


def spec():
    return TaskSpec(project_id="animaluprising", task_id="UI-01", title="Resume UI",
                    spec="Existing plan and recorded NEXT", scope=["src/client/Ui.lua"],
                    data={"tier": "B"})


def test_build_delegates_planning_but_retains_execution_and_gates(tmp_path):
    prompt = adapter(tmp_path).implement_prompt(spec(), {"attempt": 1, "run_id": "r1"})
    assert "invoke the read-only `au-planner` subagent once" in prompt
    assert "continue the approved BUILD work in this Code session" in prompt
    assert "not acceptance evidence or permission to widen scope" in prompt
    assert "Do NOT write UI-01_SUMMARY.md" in prompt
    assert "GATE UI-01 PASS|FAIL" in prompt


def test_repairs_include_findings_without_widening_scope(tmp_path):
    prompt = adapter(tmp_path).repair_prompt(spec(), ["S46 still failed"], {"attempt": 2})
    assert "au-planner" in prompt and "S46 still failed" in prompt
    assert "never permission to change scope" in prompt


def test_profiles_without_planner_and_closeout_do_not_add_planning_calls(tmp_path):
    assert "au-planner" not in adapter(tmp_path, planner=False).implement_prompt(spec(), {})
    prompt = adapter(tmp_path).closeout_prompt(spec(), {"base": "abc123"})
    assert "au-planner" not in prompt
    assert "Change no code or test file" in prompt


def test_workstation_external_sources_do_not_remove_secret_or_publish_guards(tmp_path):
    from pathlib import Path
    from foundry_coordinator.profile import load
    profile = load(str(Path(__file__).resolve().parents[2] /
                       'foundry_coordinator/projects/animaluprising/project.example.toml'))
    a = AnimalUprisingAdapter(profile, str(tmp_path))
    rules = a.session_permissions(spec(), 'implement')
    assert {'permission': 'external_directory', 'pattern': 'C:/Roblox/*', 'action': 'allow'} in rules
    assert {'permission': 'read', 'pattern': 'C:/Roblox/_secrets/*', 'action': 'deny'} in rules
    assert {'permission': 'bash', 'pattern': 'git push*', 'action': 'deny'} in rules

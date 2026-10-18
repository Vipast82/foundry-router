"""The adapter review contract: phase, deferred duties, revision-bound task specs,
legacy evidence wrapping and diff priority (generic adapters; synthetic repos)."""

import json

import pytest

from foundry_coordinator.adapters.base import (ReviewContractDefaults, TaskSpec, bound_text,
                                               duty_bullets, evidence_item,
                                               generic_diff_priority)
from foundry_coordinator.adapters.git import Workspace
from foundry_coordinator.adapters.project import AdapterError, GenericAdapter
from foundry_coordinator.profile import Profile
from tests.coordinator.helpers import git

TASKS = [{"id": "T1", "title": "One", "spec_file": "specs/T1.md", "scope": ["src/a.py"]},
         {"id": "T2", "title": "Two", "spec": "inline spec", "scope": ["src/b.py"]}]


def _profile(ws, closeout=None):
    return Profile(id="gen", name="Generic", version="1", path="x.toml", hash="x",
                   workspace=str(ws), allowed_dirs=["src", "docs"], branch="main",
                   remote="origin", raw={}, tasks={"file": "tasks.json"},
                   closeout=closeout or {})


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "gen"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "main")
    (ws / "tasks.json").write_text(json.dumps(TASKS))
    (ws / "specs").mkdir()
    (ws / "specs" / "T1.md").write_text("spec as committed\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "init")
    return ws, git(ws, "rev-parse", "HEAD")


def _spec(scope=("src/a.py",)):
    return TaskSpec(project_id="gen", task_id="T1", title="One", spec="", scope=list(scope))


def test_one_phase_project_keeps_the_full_task_contract(repo):
    ws, _ = repo
    for closeout in ({}, {"mode": "none"}, {"mode": "commands", "steps_text": "- publish"}):
        c = GenericAdapter(_profile(ws, closeout), "").review_contract(_spec())
        assert c["phase"] == "full" and c["deferred"] == [] and c["reviewed"]


def test_session_closeout_project_defers_its_closeout_steps(repo):
    ws, _ = repo
    steps = ("1. Write CHANGELOG.md\n   from the merged code\n2) Tag nothing\n- push the branch\n")
    a = GenericAdapter(_profile(ws, {"mode": "session", "steps_text": steps}), "")
    c = a.review_contract(_spec())
    assert c["phase"] == "build"
    assert c["deferred"] == ["Write CHANGELOG.md from the merged code", "Tag nothing",
                             "push the branch"]
    assert any("implementation" in r for r in c["reviewed"])
    assert any("reviewed commit" in r for r in c["reviewed"])
    assert any("Scope" in r for r in c["reviewed"])
    bare = GenericAdapter(_profile(ws, {"mode": "session"}), "").review_contract(_spec())
    assert bare["phase"] == "build" and len(bare["deferred"]) == 1


def test_duty_bullets_folds_free_text():
    assert duty_bullets("") == []
    assert duty_bullets("Run the closeout steps.") == ["Run the closeout steps."]
    assert duty_bullets("* a\n  b\n(c) d") == ["a b", "d"]


def test_generic_describe_at_reads_task_and_spec_file_at_the_revision(repo):
    ws, rev = repo
    a = GenericAdapter(_profile(ws), "")
    w = Workspace(str(ws))
    (ws / "specs" / "T1.md").write_text("EDITED in the working tree\n")
    (ws / "tasks.json").write_text(json.dumps([dict(TASKS[0], scope=["src/zzz.py"])]))
    s = a.describe_at("T1", rev, w)
    assert s.spec == "spec as committed\n" and s.scope == ["src/a.py"]
    assert s.data["spec_source"] == "revision" and s.data["spec_revision"] == rev
    assert s.data["task_file_blob"] == git(ws, "rev-parse", f"{rev}:tasks.json")
    assert s.data["spec_blobs"]["specs/T1.md"] == git(ws, "rev-parse", f"{rev}:specs/T1.md")
    assert a.describe("T1").spec.startswith("EDITED")          # working tree differs
    assert a.describe_at("T2", rev, w).spec == "inline spec"
    with pytest.raises(AdapterError, match="not in the task file"):
        a.describe_at("T9", rev, w)


def test_generic_describe_at_falls_back_to_the_working_tree_honestly(tmp_path):
    ws = tmp_path / "untracked"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "main")
    (ws / "README").write_text("x")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "init")
    (ws / "tasks.json").write_text(json.dumps(TASKS[1:]))       # never committed
    s = GenericAdapter(_profile(ws), "").describe_at("T2", git(ws, "rev-parse", "HEAD"),
                                                     Workspace(str(ws)))
    assert s.data["spec_source"] == "working tree" and "not tracked" in s.data["spec_note"]


def test_base_defaults_wrap_legacy_review_extra_as_unverified():
    class Legacy(ReviewContractDefaults):
        def describe(self, task_id):
            return _spec()

        def review_extra(self, spec, revision):
            return "gate excerpt\n" * 3

    a = Legacy()
    assert a.review_contract(_spec())["phase"] == "full"
    assert a.describe_at("T1", "abc", None).data["spec_source"] == "working tree"
    [item] = a.review_evidence(_spec(), "abc123", None)
    assert item["status"] == "unverified" and "working tree" in item["note"]
    assert item["revision"] == "abc123" and item["text"].startswith("gate excerpt")
    assert set(item) == {"title", "path", "revision", "blob", "lines", "text", "status",
                         "truncated", "note", "priority"}

    class Empty(Legacy):
        def review_extra(self, spec, revision):
            return ""
    assert Empty().review_evidence(_spec(), "abc", None) == []


def test_generic_adapter_without_extra_has_no_evidence_items(repo):
    ws, rev = repo
    assert GenericAdapter(_profile(ws), "").review_evidence(_spec(), rev, Workspace(str(ws))) == []


def test_bound_text_flags_truncation_on_whole_lines():
    text = "\n".join(f"line {i}" for i in range(100))
    out, cut, omitted = bound_text(text, 50)
    assert cut and out.startswith("line 0") and omitted > 0 and len(out) <= 50
    tail, cut2, _ = bound_text(text, 50, keep="tail")
    assert cut2 and tail.endswith("line 99")
    assert bound_text("short", 50) == ("short", False, 0)
    with pytest.raises(ValueError):
        evidence_item("x", status="PASS")


def test_generic_diff_priority_ordering(repo):
    ws, _ = repo
    p = GenericAdapter(_profile(ws), "").diff_priority
    s = _spec()
    assert p("src/a.py", s) == 0 and p("lib/core.lua", s) == 0
    assert p("tests/test_a.py", s) == 1 and p("src/a_test.go", s) == 1
    assert p("pyproject.toml", s) == 2 and p("tools/build.py", s) == 2
    assert p("docs/guide.md", s) == 5 and p("README.md", s) == 5
    assert p("logs/run.log", s) == 8 and p("docs/evidence/x/run.txt", s) == 8
    assert p("package-lock.json", s) == 8
    assert p("art/shot.png", s) == 9 and p("docs/evidence/x/a.png", s) == 9
    assert generic_diff_priority("src/a.py") < generic_diff_priority("tests/t.py") \
        < generic_diff_priority("docs/a.md") < generic_diff_priority("x.log") \
        < generic_diff_priority("x.png")

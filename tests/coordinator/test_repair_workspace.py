"""repair.py: isolated Foundry-Router worktrees, scope and test-expectation
gates, the reproducing-test check and the independent review gate. Uses a
throwaway git repo as the "router repo"; never the real checkout."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap

import pytest

from foundry_coordinator import repair as RP
from foundry_coordinator.repair import RepairError, RepairWorkspace

CALC_BUG = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
CALC_FIX = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST_CALC = textwrap.dedent("""\
    import calc


    def test_mul():
        assert calc.mul(2, 3) == 6
        assert calc.mul(0, 3) == 0


    def test_mul_neg():
        assert calc.mul(-1, 3) == -3
    """)
REGRESSION = "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n"


def g(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def write(root, rel, text):
    p = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


@pytest.fixture
def router(tmp_path):
    repo = tmp_path / "router"
    repo.mkdir()
    g(repo, "init", "-q", "-b", "main")
    g(repo, "config", "core.autocrlf", "false")
    write(repo, "calc.py", CALC_BUG)
    write(repo, "tests/test_calc.py", TEST_CALC)
    write(repo, "README.md", "router\n")
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", "base")
    return str(repo)


@pytest.fixture
def ws(router, tmp_path):
    return RepairWorkspace(str(tmp_path / "repairs"), router)


def base(router):
    return g(router, "rev-parse", "HEAD")


def fix(path, regression=True):
    write(path, "calc.py", CALC_FIX)
    if regression:
        write(path, "tests/test_regression.py", REGRESSION)


SCOPE = ["calc.py", "tests/test_regression.py"]


# -------------------------------------------------------------------- create/diff/commit

def test_worktree_create_diff_commit(ws, router):
    b = base(router)
    path = ws.create("case-1", b)
    assert os.path.isdir(path) and os.path.dirname(path) == ws.root
    assert g(path, "rev-parse", "--abbrev-ref", "HEAD") == "lead/case-1"
    assert ws.create("case-1", b) == path                     # idempotent
    fix(path)
    assert ws.changed_files(path) == ["calc.py", "tests/test_regression.py"]
    d = ws.diff(path)
    assert "+    return a + b" in d and "test_regression.py" in d
    sha = ws.commit(path, "fix add", scope=SCOPE)
    assert sha == g(path, "rev-parse", "HEAD") == g(router, "rev-parse", "lead/case-1")
    assert g(router, "rev-parse", "HEAD") == b                # the router checkout is untouched
    m = ws.manifest(path, scope=SCOPE, tests=["tests/test_regression.py"])
    assert m["candidate_sha"] == sha and m["base_sha"] == b
    assert m["files"] == ["calc.py", "tests/test_regression.py"]
    assert m["tests"]["focused"] == ["tests/test_regression.py"] and m["migrations"] == []


def test_refuses_paths_outside_root_and_running_checkout(ws, router, tmp_path):
    with pytest.raises(RepairError):
        ws.check_path(router)                                  # the router checkout itself
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(RepairError, match="outside the repair root"):
        ws.check_path(str(other))
    with pytest.raises(RepairError):
        ws.check_path(ws.root)
    # a repair root that is (or contains) the running checkout is refused
    running = RP._running_checkout()
    with pytest.raises(RepairError):
        RepairWorkspace(running, router)
    with pytest.raises(RepairError):
        RepairWorkspace(os.path.join(router, "inner"), router)
    with pytest.raises(RepairError, match="invalid case id"):
        ws.create("../escape", base(router))


def test_dirty_scope_refused(ws, router):
    path = ws.create("case-2", base(router))
    fix(path)
    write(path, "README.md", "router, edited outside scope\n")
    with pytest.raises(RepairError, match="dirty scope") as ei:
        ws.commit(path, "fix", scope=SCOPE)
    assert any("README.md" in v for v in ei.value.violations)
    assert g(path, "rev-parse", "HEAD") == base(router)       # nothing committed
    with pytest.raises(RepairError, match="no repair scope"):
        ws.commit(path, "fix", scope=[])
    # declaring the extra file in the scope makes it acceptable
    assert ws.commit(path, "fix", scope=SCOPE + ["README.md"])


@pytest.mark.parametrize("edit, why", [
    (TEST_CALC.replace("    assert calc.mul(0, 3) == 0\n", ""), "assertion"),
    (TEST_CALC.replace("== -3", "== 3"), "assertion"),
    (TEST_CALC.split("\n\ndef test_mul_neg")[0] + "\n", "test function"),
    (TEST_CALC.replace("def test_mul_neg", "@pytest.mark.skip\ndef test_mul_neg")
     .replace("import calc", "import calc\nimport pytest"), "skip"),
    (None, "deleted"),
])
def test_undeclared_test_expectation_change_refused(ws, router, edit, why):
    path = ws.create("case-3", base(router))
    fix(path)
    if edit is None:
        os.remove(os.path.join(path, "tests", "test_calc.py"))
    else:
        write(path, "tests/test_calc.py", edit)
    scope = SCOPE + ["tests/"]
    with pytest.raises(RepairError) as ei:
        ws.commit(path, "fix", scope=scope)
    assert any(why in v and "tests/test_calc.py" in v for v in ei.value.violations), \
        ei.value.violations
    # declared by the model only: still refused (it needs the operator's approval)
    with pytest.raises(RepairError, match="operator approval"):
        ws.commit(path, "fix", scope=scope, declared_test_changes=["tests/test_calc.py"])
    # an approval without the declaration is not enough either
    with pytest.raises(RepairError):
        ws.commit(path, "fix", scope=scope, approved_test_changes=["tests/test_calc.py"])
    # declared AND approved by the operator: allowed
    sha = ws.commit(path, "fix", scope=scope, declared_test_changes=["tests/test_calc.py"],
                    approved_test_changes=["tests/test_calc.py"])
    assert sha


def test_added_tests_and_assertions_are_allowed(ws, router):
    path = ws.create("case-4", base(router))
    fix(path)
    write(path, "tests/test_calc.py",
          TEST_CALC + "\n\ndef test_mul_more():\n    assert calc.mul(3, 3) == 9\n")
    assert ws.commit(path, "fix", scope=SCOPE + ["tests/test_calc.py"])


# -------------------------------------------------------------------- reproduce

def test_reproducing_test_fails_before_and_passes_after(ws, router):
    path = ws.create("case-5", base(router))
    fix(path)
    ws.commit(path, "fix", scope=SCOPE)
    assert ws.reproduces(path, "tests/test_regression.py::test_add", timeout=120) is True
    r = ws.last_reproduce
    assert r["base"]["rc"] == 1 and r["candidate"]["rc"] == 0
    # the temporary base worktree is gone
    assert not [d for d in os.listdir(ws.root) if d.startswith("_base-")]
    assert "_base-" not in g(router, "worktree", "list")


def test_non_reproducing_test_refused(ws, router):
    path = ws.create("case-6", base(router))
    fix(path)
    ws.commit(path, "fix", scope=SCOPE)
    # passes at base too: it proves nothing about the defect
    assert ws.reproduces(path, "tests/test_calc.py::test_mul", timeout=120) is False
    assert "passes at base" in ws.last_reproduce["reason"]
    # a test id that doesn't exist is not a reproduction either
    assert ws.reproduces(path, "tests/test_regression.py::test_nope", timeout=120) is False


def test_candidate_must_pass_the_regression_test(ws, router):
    path = ws.create("case-7", base(router))
    write(path, "tests/test_regression.py", REGRESSION)      # test only, no fix
    ws.commit(path, "test only", scope=SCOPE)
    assert ws.reproduces(path, "tests/test_regression.py::test_add", timeout=120) is False
    assert "does not pass at the candidate" in ws.last_reproduce["reason"]


def test_run_tests_focused_and_broader(ws, router):
    path = ws.create("case-8", base(router))
    fix(path)
    ws.commit(path, "fix", scope=SCOPE)
    r = ws.run_tests(path, ["tests/test_regression.py"], ["tests"], timeout=120)
    assert r["ok"] and r["focused"]["ok"] and r["broader"]["ok"]
    assert ws.run_tests(path, [], ["tests"], timeout=60)["ok"] is False
    write(path, "calc.py", CALC_BUG)
    r = ws.run_tests(path, ["tests/test_regression.py"], [], timeout=120)
    assert r["ok"] is False and r["focused"]["rc"] == 1


# -------------------------------------------------------------------- review

class FakeFoundry:
    def __init__(self, verdict="APPROVED", findings=None, sha=None, case="case-r"):
        self.verdict, self.findings, self.sha, self.case = verdict, findings or [], sha, case
        self.calls = []

    def chat(self, persona, messages, *, session_id="", max_tokens=4000, schema=None,
             timeout=None):
        self.calls.append({"persona": persona, "session_id": session_id, "schema": schema,
                           "packet": messages[-1]["content"]})
        sha = self.sha or messages[-1]["content"].split("reviewed_commit_sha): ")[1].split()[0]
        return json.dumps({"sprint_id": self.case, "project_id": "p1",
                           "reviewed_commit_sha": sha, "verdict": self.verdict,
                           "findings": self.findings, "evidence_inspected": ["diff"],
                           "missing_evidence": [], "required_repairs": [],
                           "residual_risks": []}), {"served_model": "gpt-review"}


class P:
    def __init__(self, foundry):
        self.foundry = foundry


def _committed(ws, router, case="case-r"):
    path = ws.create(case, base(router))
    fix(path)
    ws.commit(path, "fix", scope=SCOPE)
    return path


def test_review_refuses_the_lead_persona(ws, router):
    path = _committed(ws, router)
    f = FakeFoundry()
    for rev in ("Foundry-Kilo-Lead-Manager", " foundry-kilo-lead-manager "):
        with pytest.raises(RepairError, match="independent"):
            ws.review(P(f), path, case_id="case-r", lead_persona="Foundry-Kilo-Lead-Manager",
                      reviewer_persona=rev, project_id="p1")
    with pytest.raises(RepairError):
        ws.review(P(f), path, case_id="case-r", lead_persona="", reviewer_persona="Reviewer")
    assert f.calls == []                                       # never called


def test_review_approved_and_changes_required(ws, router):
    path = _committed(ws, router)
    f = FakeFoundry("APPROVED")
    r = ws.review(P(f), path, case_id="case-r", lead_persona="Lead", reviewer_persona="Reviewer",
                  project_id="p1")
    assert r["approved"] and r["verdict"] == "APPROVED" and r["persona"] == "Reviewer"
    assert f.calls[0]["persona"] == "Reviewer" and f.calls[0]["schema"]["required"]
    assert "+    return a + b" in f.calls[0]["packet"]
    r = ws.review(P(FakeFoundry("CHANGES_REQUIRED")), path, case_id="case-r",
                  lead_persona="Lead", reviewer_persona="Reviewer", project_id="p1")
    assert not r["approved"] and r["verdict"] == "CHANGES_REQUIRED"
    # APPROVED with a blocking finding is not an approval (review.validate)
    r = ws.review(P(FakeFoundry("APPROVED", [{"severity": "blocker", "file": "calc.py",
                                              "line": 2, "message": "wrong"}])),
                  path, case_id="case-r", lead_persona="Lead", reviewer_persona="Reviewer",
                  project_id="p1")
    assert not r["approved"] and r["verdict"] == "CHANGES_REQUIRED"
    # a review of another commit is invalid
    r = ws.review(P(FakeFoundry(sha="0" * 40)), path, case_id="case-r", lead_persona="Lead",
                  reviewer_persona="Reviewer", project_id="p1")
    assert not r["approved"] and r["verdict"] == "INVALID"


def test_validate_gate(ws, router):
    path = _committed(ws, router)
    kw = dict(case_id="case-r", regression_test="tests/test_regression.py::test_add",
              focused=["tests/test_regression.py"], broader=["tests"], scope=SCOPE,
              lead_persona="Lead", reviewer_persona="Reviewer", project_id="p1", timeout=120)
    blocked = ws.validate(P(FakeFoundry("CHANGES_REQUIRED")), path, **kw)
    assert not blocked["ok"] and "CHANGES_REQUIRED" in blocked["reasons"][0]
    assert blocked["manifest"] is None
    ok = ws.validate(P(FakeFoundry("APPROVED")), path, **kw)
    assert ok["ok"], ok["reasons"]
    m = ok["manifest"]
    assert m["candidate_sha"] == ws.head(path) and m["review_id"].startswith("rr-")
    assert m["tests"]["regression"] == "tests/test_regression.py::test_add"
    # a non-reproducing regression test stops the gate before review
    f = FakeFoundry("APPROVED")
    bad = ws.validate(P(f), path, **dict(kw, regression_test="tests/test_calc.py::test_mul"))
    assert not bad["ok"] and "does not reproduce" in bad["reasons"][0] and f.calls == []


# -------------------------------------------------------------------- review B regressions

def test_import_error_at_base_is_not_a_reproduction(ws, router):
    """A test that only imports a symbol the candidate adds errors at base
    (collection error, rc 2): it proves nothing about the defect."""
    p = ws.create("rb-1", base(router))
    write(p, "calc.py", CALC_FIX + "\n\ndef helper():\n    return 1\n")
    write(p, "tests/test_regression.py", "from calc import helper\n\ndef test_x():\n"
                                         "    assert helper() == 1\n")
    ws.commit(p, "m", scope=SCOPE)
    # a bare file is not a test node
    assert ws.reproduces(p, "tests/test_regression.py", timeout=120) is False
    assert "path::test" in ws.last_reproduce["reason"]
    assert ws.last_reproduce["base"] is None                   # never ran
    # the exact node errors at collection: not a test failure
    assert ws.reproduces(p, "tests/test_regression.py::test_x", timeout=120) is False
    assert ws.last_reproduce["base"]["rc"] in (2, 4)           # collection / usage error
    assert "not a reproduction" in ws.last_reproduce["reason"]


def test_reproduction_needs_the_exact_test_to_fail(ws, router):
    """Another test in the file failing at base doesn't make a passing test a
    reproduction (rc 1 alone is not enough: the named node must fail)."""
    p = ws.create("rb-1b", base(router))
    fix(p)
    write(p, "tests/test_regression.py", REGRESSION + "\n\ndef test_other():\n"
                                                     "    assert calc.mul(1, 1) == 1\n")
    ws.commit(p, "m", scope=SCOPE)
    assert ws.reproduces(p, "tests/test_regression.py::test_other", timeout=120) is False
    assert ws.reproduces(p, "tests/test_regression.py::test_add", timeout=120) is True
    assert ws.last_reproduce["base"]["rc"] == 1


@pytest.mark.parametrize("src, why", [
    (TEST_CALC.replace("def test_mul():\n", "def test_mul():\n    return\n"), "early return"),
    (TEST_CALC + "\n\ndef test_mul():\n    pass\n", "duplicate definition"),
    (TEST_CALC.replace("    assert calc.mul(2, 3) == 6\n    assert calc.mul(0, 3) == 0\n",
                       "    try:\n        assert calc.mul(2, 3) == 6\n"
                       "        assert calc.mul(0, 3) == 0\n    except AssertionError:\n"
                       "        pass\n"), "exception handling"),
    (TEST_CALC.replace("import calc", "import calc\nimport pytest")
     .replace("def test_mul():", "@pytest.mark.parametrize('x', [])\ndef test_mul(x):"),
     "decorators changed"),
])
def test_neutered_existing_test_refused(ws, router, src, why):
    p = ws.create("rb-2", base(router))
    fix(p)
    write(p, "tests/test_calc.py", src)
    problems = ws.check_scope(p, SCOPE + ["tests/test_calc.py"])
    assert any(why in v and "tests/test_calc.py" in v for v in problems), problems
    with pytest.raises(RepairError):
        ws.commit(p, "m", scope=SCOPE + ["tests/test_calc.py"])


@pytest.mark.parametrize("rel, text", [
    ("tests/conftest.py", "def pytest_collection_modifyitems(items):\n"
                          "    items[:] = [i for i in items if 'mul' not in i.name]\n"),
    ("conftest.py", "collect_ignore = ['tests/test_calc.py']\n"),
    ("pytest.ini", "[pytest]\naddopts = --deselect tests/test_calc.py::test_mul\n"),
    ("pyproject.toml", "[tool.pytest.ini_options]\naddopts = '-k not mul'\n"),
    ("setup.cfg", "[tool:pytest]\naddopts = -k 'not mul'\n"),
    ("tox.ini", "[pytest]\naddopts = -k 'not mul'\n"),
    ("tests/__init__.py", "import sys\n"),
])
def test_pytest_configuration_change_is_an_expectation_change(ws, router, rel, text):
    p = ws.create("rb-3", base(router))
    fix(p)
    write(p, rel, text)
    problems = ws.check_scope(p, SCOPE + [rel])
    assert any(rel in v and "pytest configuration" in v for v in problems), problems
    # declared by the model is not enough; the operator's approval is
    assert ws.check_scope(p, SCOPE + [rel], declared_test_changes=[rel])
    assert ws.check_scope(p, SCOPE + [rel], declared_test_changes=[rel],
                          approved_test_changes=[rel]) == []


def test_pyproject_change_outside_pytest_sections_is_fine(ws, router):
    p = ws.create("rb-3b", base(router))
    fix(p)
    write(p, "pyproject.toml", "[project]\nname = 'router'\nversion = '1.0.1'\n")
    assert ws.check_scope(p, SCOPE + ["pyproject.toml"]) == []


@pytest.mark.parametrize("ids", [
    ["--co", "tests/test_bad.py"],
    ["tests/test_bad.py", "-k", "nothing"],
    ["--deselect", "tests/test_bad.py::test_fail", "tests/test_bad.py"],
    ["-p", "no:python", "tests/test_bad.py"],
    ["-c", "other.ini", "tests/test_bad.py"],
    ["tests/test_bad.py --co"],
    ["../outside/test_x.py"],
    ["C:/abs/test_x.py"],
])
def test_option_like_test_ids_refused(ws, router, ids):
    p = ws.create("rb-4", base(router))
    write(p, "tests/test_bad.py", "def test_fail():\n    assert False\n")
    ws.commit(p, "m", scope=["tests/test_bad.py"])
    r = ws.run_tests(p, ids, ["tests"])
    assert r["ok"] is False and "invalid test id" in r["log"]
    with pytest.raises(RepairError, match="invalid test id"):
        RP.run_pytest(ws.python, p, ids, 60)


def test_named_tests_must_be_collected_and_pass(ws, router):
    p = ws.create("rb-5", base(router))
    fix(p)
    write(p, "tests/test_skip.py", "import pytest\n\n\n@pytest.mark.skip\ndef test_s():\n"
                                   "    assert True\n")
    ws.commit(p, "m", scope=SCOPE + ["tests/test_skip.py"])
    # an id that names nothing is not a pass
    r = ws.run_tests(p, ["tests/test_regression.py::test_nope"], ["tests"], timeout=120)
    assert r["ok"] is False
    # a skipped node is not a pass
    r = ws.run_tests(p, ["tests/test_skip.py::test_s"], ["tests"], timeout=120)
    assert r["ok"] is False and any("skipped" in x for x in r["problems"]), r
    # the broader suite is never empty: the default is used when none is named
    r = ws.run_tests(p, ["tests/test_regression.py::test_add"], [], timeout=120)
    assert r["ok"], r["log"]
    assert r["broader_ids"] == ["tests"] and r["broader"]["ok"]
    ws2 = RepairWorkspace(ws.root, router, broader_default=["no/such/dir"])
    assert ws2.broader_for(p, []) == ["tests"]


def test_no_broader_suite_available_fails(router, tmp_path):
    import shutil
    rw = RepairWorkspace(str(tmp_path / "repairs2"), router, broader_default=[])
    p = rw.create("rb-5b", base(router))
    fix(p, regression=False)
    rw.commit(p, "m", scope=SCOPE)
    assert rw.broader_for(p, []) == ["tests"]
    shutil.rmtree(os.path.join(p, "tests"))
    r = rw.run_tests(p, ["calc.py"], [], timeout=60)
    assert r["ok"] is False and "no broader test suite" in r["log"]


def test_crash_between_worktree_add_and_meta_is_adopted(ws, router):
    b = base(router)
    path = os.path.join(ws.root, "c7")
    # a crash right after `git worktree add` (no meta written)
    RP._git(ws.router_repo, "worktree", "add", "-b", "lead/c7", path, b)
    assert ws.create("c7", b) == path
    assert ws.base_sha(path) == b
    assert ws.create("c7", b) == path                          # and stays idempotent
    # a crash after the intent, before the add: the add is redone on the branch
    other = ws.create("c8", b)
    RP._git(ws.router_repo, "worktree", "remove", "--force", other)
    with open(ws._meta_path("c8"), "w", encoding="utf-8") as fh:
        json.dump({"case_id": "c8", "base_sha": b, "intent": True}, fh)
    assert ws.create("c8", b) == other and os.path.isdir(other)
    assert g(other, "rev-parse", "--abbrev-ref", "HEAD") == "lead/c8"
    # a stray directory that is not a worktree on lead/<case> is still refused
    os.makedirs(os.path.join(ws.root, "c9"))
    with pytest.raises(RepairError, match="not a recorded repair worktree"):
        ws.create("c9", b)


def test_case_ids_are_case_insensitive(ws, router):
    b = base(router)
    p1 = ws.create("Case-Upper", b)
    assert os.path.basename(p1) == "case-upper"
    assert g(p1, "rev-parse", "--abbrev-ref", "HEAD") == "lead/case-upper"
    assert ws.create("case-UPPER", b) == p1
    assert ws._meta(p1)["case_id"] == "Case-Upper"


def test_repair_root_inside_game_workspace_refused(router, tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    with pytest.raises(RepairError, match="inside the protected"):
        RepairWorkspace(str(game / "repairs"), router, protected=[str(game)])


def test_repair_root_inside_running_checkout_refused(router, tmp_path):
    run = tmp_path / "running"
    run.mkdir()
    with pytest.raises(RepairError, match="inside the protected"):
        RepairWorkspace(str(run / "repairs"), router, running_checkout=str(run))
    with pytest.raises(RepairError):
        RepairWorkspace(os.path.join(RP._running_checkout(), "build", "repairs"), router)


def test_test_subprocess_env_is_scrubbed(ws, router, monkeypatch):
    for k, v in {"FOUNDRY_API_KEY": "fk", "FOUNDRY_URL": "http://f", "OPENAI_API_KEY": "sk",
                 "GITHUB_TOKEN": "gh", "DB_PASSWORD": "pw", "MY_SECRET": "s",
                 "AWS_SECRET_ACCESS_KEY": "a", "PYTEST_ADDOPTS": "-k nothing",
                 "HARMLESS_SETTING": "kept"}.items():
        monkeypatch.setenv(k, v)
    p = ws.create("rb-env", base(router))
    write(p, "tests/test_env.py", textwrap.dedent("""\
        import os, tempfile

        def test_env():
            leaked = [k for k in os.environ if k.upper().startswith("FOUNDRY_") or k.upper() in (
                "OPENAI_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "MY_SECRET",
                "AWS_SECRET_ACCESS_KEY", "PYTEST_ADDOPTS")]
            assert leaked == []
            assert os.environ.get("HARMLESS_SETTING") == "kept"
            assert os.environ.get("PATH")
            assert "foundry-pytest-" in os.environ["HOME"]
            assert "foundry-pytest-" in tempfile.gettempdir()
        """))
    ws.commit(p, "m", scope=["tests/test_env.py"])
    r = ws.run_tests(p, ["tests/test_env.py::test_env"], ["tests/test_env.py"], timeout=120)
    assert r["ok"], r["log"]
    env = RP.sandbox_env(str(ws.root), {"PATH": "p", "SYSTEMROOT": "C:/Windows", "X_KEY": "1",
                                        "VIRTUAL_ENV": "v", "FOUNDRY_COORD_STATE": "s"})
    assert env["PATH"] == "p" and env["SYSTEMROOT"] == "C:/Windows" and env["VIRTUAL_ENV"] == "v"
    assert "X_KEY" not in env and "FOUNDRY_COORD_STATE" not in env
    assert env["HOME"] == env["TEMP"] == str(ws.root)
    # 2026-10-30: the user's rokit tool storage (binaries only) stays reachable for tests that
    # run a tool such as Lune; without it every studio_gate candidate failed in the sandbox
    from pathlib import Path
    root = Path(ws.root)
    home = root / "realhome"
    (home / ".rokit" / "tool-storage").mkdir(parents=True)
    env = RP.sandbox_env(str(root / "sb"), {"USERPROFILE": str(home), "PATH": "p"})
    assert env["ROKIT_TOOL_STORAGE"] == os.path.join(str(home), ".rokit", "tool-storage")
    assert env["USERPROFILE"] == str(root / "sb")
    assert "ROKIT_TOOL_STORAGE" not in RP.sandbox_env(str(root / "sb2"), {"PATH": "p"})


def test_existing_test_dropped_at_runtime_refused(ws, router):
    """A modification the static check can't see (a module-level `del`) still
    loses an existing test: the base's collected set must still run."""
    path = _committed(ws, router, case="rb-6")
    write(path, "tests/test_calc.py", TEST_CALC + "\n\ndel test_mul_neg\n")
    ws.commit(path, "drop", scope=SCOPE + ["tests/test_calc.py"])
    ex = ws.existing_tests_hold(path, ["tests/test_calc.py"], timeout=120)
    assert not ex["ok"] and "tests/test_calc.py::test_mul_neg" in ex["base_ids"]
    assert any("test_mul_neg" in x and "not at the candidate" in x for x in ex["problems"])
    f = FakeFoundry("APPROVED", case="rb-6")
    res = ws.validate(P(f), path, case_id="rb-6",
                      regression_test="tests/test_regression.py::test_add",
                      focused=["tests/test_regression.py"], broader=["tests"],
                      scope=SCOPE + ["tests/test_calc.py"], lead_persona="Lead",
                      reviewer_persona="Reviewer", project_id="p1", timeout=120)
    assert not res["ok"] and "existing tests no longer hold" in res["reasons"][0]
    assert f.calls == []                                       # stopped before the review


def test_validate_refuses_declared_but_unapproved_test_change(ws, router):
    path = ws.create("rb-7", base(router))
    fix(path)
    write(path, "tests/test_calc.py", TEST_CALC.replace("== -3", "== 3"))
    write(path, "calc.py", CALC_FIX.replace("return a * b", "return abs(a * b)"))
    scope = SCOPE + ["tests/test_calc.py"]
    ws.commit(path, "m", scope=scope, declared_test_changes=["tests/test_calc.py"],
              approved_test_changes=["tests/test_calc.py"])
    kw = dict(case_id="rb-7", regression_test="tests/test_regression.py::test_add",
              focused=["tests/test_regression.py"], broader=["tests"],
              scope=scope, lead_persona="Lead", reviewer_persona="Reviewer", project_id="p1",
              timeout=120, declared_test_changes=["tests/test_calc.py"])
    f = FakeFoundry("APPROVED", case="rb-7")
    res = ws.validate(P(f), path, **kw)
    assert not res["ok"] and "operator approval" in res["reasons"][0] and f.calls == []
    res = ws.validate(P(f), path, **kw, approved_test_changes=["tests/test_calc.py"])
    assert res["ok"], res["reasons"]
    m = res["manifest"]
    assert m["approved_test_changes"] == ["tests/test_calc.py"] and m["lead_enabled"] is True
    assert m["tests"]["broader"] == ["tests"]
    # a model-supplied option as a test id never reaches pytest
    bad = ws.validate(P(f), path, **dict(kw, focused=["--co"]),
                      approved_test_changes=["tests/test_calc.py"])
    assert not bad["ok"] and "invalid test id" in bad["reasons"][0]
    bad = ws.validate(P(f), path, **dict(kw, regression_test="tests/test_regression.py"),
                      approved_test_changes=["tests/test_calc.py"])
    assert not bad["ok"] and "invalid test id" in bad["reasons"][0]

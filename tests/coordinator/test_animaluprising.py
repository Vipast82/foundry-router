"""AnimalUprising adapter against the real project (read-only).

Needs a clone of the AnimalUprising repository with history (AU_REPO, default
/home/user/animaluprising); skipped otherwise. Everything runs in a throwaway
`git clone --shared` so the real clone is never written to."""

import os
import shutil
import subprocess

import pytest

from foundry_coordinator.adapters.base import FAIL, PASS
from foundry_coordinator.profile import load
from tests.coordinator.helpers import GIT_ENV

AU = os.environ.get("AU_REPO", "/home/user/animaluprising")
_AU_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "foundry_coordinator", "projects",
                       "animaluprising")
# the local profile (git-ignored) when present, else the committed example
PROFILE = next(p for p in (os.path.join(_AU_DIR, "project.toml"),
                           os.path.join(_AU_DIR, "project.example.toml")) if os.path.exists(p))
pytestmark = pytest.mark.skipif(
    not os.path.isfile(os.path.join(AU, "tools", "guard", "done_check.py")),
    reason="AnimalUprising clone not available")


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True,
                          env=GIT_ENV, check=True).stdout.strip()


def _clone_at(tmp, rev):
    dst = tmp / "au"
    subprocess.run(["git", "clone", "-q", "--shared", AU, str(dst)], check=True)
    _git(dst, "checkout", "-q", "--detach", rev)
    return dst


def _adapter(tmp, workspace):
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    p = load(PROFILE)
    p.workspace = str(workspace)
    return AnimalUprisingAdapter(p, str(tmp / "state"))


def _closing_commit(task):
    out = _git(AU, "log", "--format=%H %s", "-n", "400")
    shas = [l.split()[0] for l in out.splitlines() if l.split(" ", 1)[1].startswith(task + ":")]
    if not shas:
        pytest.skip(f"{task} not in the clone's history (shallow clone?)")
    return shas[0]


def test_plan_is_the_task_source_and_scope(tmp_path):
    a = _adapter(tmp_path, AU)
    s = a.describe("UI-01")
    assert "src/client/UI/Kit/Theme.lua" in s.scope and "tests/UI_01_check.lua" in s.scope
    assert "tier:B" in s.tags and "visual" in s.tags
    assert {"AS-C16", "GF-UI1b"} <= set(s.deps)
    hk = a.describe("HK-G2")
    assert "visual" not in hk.tags                          # tier B isn't automatically visual
    with pytest.raises(Exception, match="does not exist"):
        a.describe("ZZ-99")


def test_dependencies_finished_before_the_coordinator_count(tmp_path):
    a = _adapter(tmp_path, AU)
    assert a.dependency_done("AS-C16") and a.dependency_done("GF-UI1b")
    assert not a.dependency_done("UI-01")                  # in progress, no ledger row


def test_sprints_refused_until_the_rule_amendment_exists(tmp_path):
    a = _adapter(tmp_path, AU)
    why = a.preflight_task(a.describe("UI-01"))
    assert why and "COORDINATOR-ADVANCEMENT" in why


@pytest.mark.parametrize("task", ["AS-C16", "HK-G2"])
def test_replay_completed_sprint_passes_at_its_own_commit(tmp_path, task):
    ws = _clone_at(tmp_path, _closing_commit(task))
    a = _adapter(tmp_path, ws)
    rev = _git(ws, "rev-parse", "HEAD")
    s = a.describe(task)
    rows = a._done_check(s, "closeout", rev)
    assert all(r.result == PASS for r in rows), [(r.check, r.result, r.detail) for r in rows]
    g = a._gate_binding(s, rev)
    assert g.result == PASS, g.detail


def test_negative_control_code_change_after_the_gate_fails_binding(tmp_path):
    task = "AS-C16"
    ws = _clone_at(tmp_path, _closing_commit(task))
    a = _adapter(tmp_path, ws)
    s = a.describe(task)
    target = os.path.join(ws, "src", "shared", "Util", "AssetImage.lua")
    with open(target, "a", encoding="utf-8") as f:
        f.write("\n-- changed after the gate\n")
    _git(ws, "commit", "-q", "--no-verify", "-am", f"{task}: change after gate (test)")
    g = a._gate_binding(s, _git(ws, "rev-parse", "HEAD"))
    assert g.result == FAIL and "src/shared/Util/AssetImage.lua" in g.detail


def test_active_sprint_is_not_done(tmp_path):
    a = _adapter(tmp_path, AU)
    rev = _git(AU, "rev-parse", "HEAD")
    rows = {r.check: r.result for r in a._done_check(a.describe("UI-01"), "closeout", rev)}
    assert rows["au.done_check"] == FAIL and rows["au.done_check.SUMMARY"] == FAIL


def test_kilo_transcript_is_audited_with_the_projects_checkpoint_rules(tmp_path):
    from foundry_coordinator.projects.animaluprising import kilo_checkpoint as K

    def turn(i, tool, inp, out, tokens=60_000, status="completed"):
        state = {"status": status, "input": inp, "output": out, "title": tool,
                 "metadata": {}, "time": {"start": 1, "end": 2}}
        if status == "error":
            state = {"status": "error", "input": inp, "error": out, "time": {"start": 1, "end": 2}}
        return {"info": {"role": "assistant", "id": f"m{i}", "time": {"created": 1792000000000 + i},
                         "tokens": {"input": tokens, "output": 10, "cache": {"read": 0}}},
                "parts": [{"type": "tool", "tool": tool, "callID": f"c{i}", "state": state}]}
    loop = [turn(i, "bash", {"command": "rojo --version"}, "rojo 7.4.0") for i in range(4)]
    text, level = K.audit(AU, "ses_test", loop)
    assert level == 2 and "LOOP" in text and text.strip().endswith("VERDICT STOP-AND-REPLAN")
    assert "run_commands: rojo --version" in text            # mapped to checkpoint's names
    fine = [turn(0, "read", {"filePath": "docs/progress/STATE.md", "offset": 1, "limit": 40},
                 "x"), turn(1, "edit", {"filePath": "src/a.lua", "oldString": "a",
                                       "newString": "b"}, "ok")]
    text, level = K.audit(AU, "ses_ok", fine)
    assert level in (0, 1) and "VERDICT" in text.splitlines()[-1]


def test_live_checkpoint_file_is_read_only_and_ignored(tmp_path):
    import stat
    ws = _clone_at(tmp_path, _git(AU, "rev-parse", "HEAD"))
    a = _adapter(tmp_path, ws)
    turn = {"info": {"role": "assistant", "id": "m0", "time": {"created": 1792000000000},
                     "tokens": {"input": 1000, "output": 10, "cache": {"read": 0}}},
            "parts": [{"type": "tool", "tool": "read", "callID": "c0", "state": {
                "status": "completed", "input": {"filePath": "docs/progress/STATE.md"},
                "output": "x", "title": "read", "metadata": {}, "time": {"start": 1, "end": 2}}}]}
    path = a.live_session_audit("ses_live", [turn], "abc123def4567890")
    path2 = a.live_session_audit("ses_live", [turn, turn], "abc123def4567890")   # refresh in place
    assert path == path2 and path.replace("\\", "/").endswith(".plan/tmp/kilo_checkpoint.txt")
    text = open(path, encoding="utf-8").read()
    assert "kilo:ses_live" in text and "HEAD abc123def456" in text
    assert "VERDICT" in text.strip().splitlines()[-1]
    assert not os.stat(path).st_mode & stat.S_IWRITE
    assert _git(ws, "status", "--porcelain", "--untracked-files=all") == ""   # git-ignored
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)                             # let tmp cleanup run
    assert "kilo_checkpoint.txt" in a.implement_prompt(a.describe("UI-02"), {"run_id": "r"})


def test_review_extra_quotes_the_gate_summary(tmp_path):
    ws = _clone_at(tmp_path, _closing_commit("HK-G2"))
    a = _adapter(tmp_path, ws)
    extra = a.review_extra(a.describe("HK-G2"), _git(ws, "rev-parse", "HEAD"))
    assert "=== GATE FINISH ===" in extra


@pytest.fixture(autouse=True)
def _cleanup(tmp_path):
    yield
    shutil.rmtree(tmp_path / "au", ignore_errors=True)

"""2026-11-03 PT-3: the owner's concept-art chat (Claude Code) committed docs/design files to
master while PT-3 ran ("docs: map concept order ...", "plans: MAP-5 ..."). PT-3's scope check
blamed it twice and the sprint went to a decision. A commit with a `Co-Authored-By: Claude`
trailer comes from an operator chat, never from a sprint session."""

import subprocess
from types import SimpleNamespace

from foundry_coordinator.adapters.git import Workspace as GitWorkspace
from foundry_coordinator.bookkeeping import agent_files

TRAILER = "\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def _ws(operator):
    return SimpleNamespace(
        commits=lambda b, h: [("a1", "PT-3: soak run 6"), ("b2", "docs: map concept brief"),
                              ("c3", "plans: MAP-5 longer road"), ("d4", "docs: unmarked")],
        commit_files=lambda b, h: [("a1", ["docs/progress/PT_REPORTS/r6.md"]),
                                   ("b2", ["docs/design/brief.md"]),
                                   ("c3", ["docs/progress/MAP-5_PLAN.md"]),
                                   ("d4", ["docs/other.md"])],
        operator_commits=lambda b, h: operator)


def test_operator_chat_commits_are_not_the_sprints_change():
    P = SimpleNamespace(ws=_ws({"b2", "c3"}))
    assert agent_files(P, {"task_id": "PT-3", "data": {}}, "b", "h") == {
        "docs/progress/PT_REPORTS/r6.md", "docs/other.md"}


def test_an_operator_commit_with_the_sprints_own_prefix_still_counts():
    P = SimpleNamespace(ws=_ws({"a1", "b2"}))
    assert "docs/progress/PT_REPORTS/r6.md" in agent_files(
        P, {"task_id": "PT-3", "data": {}}, "b", "h")


def test_a_workspace_without_the_method_behaves_as_before():
    ws = _ws(set())
    del ws.operator_commits
    assert agent_files(SimpleNamespace(ws=ws), {"task_id": "PT-3", "data": {}}, "b", "h") is None


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_git_reads_the_claude_trailer(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a").write_text("0")
    _git(tmp_path, "add", "a")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    for i, msg in enumerate(["PT-3: session work", "docs: brief" + TRAILER,
                             "docs: other\n\nCo-Authored-By: Someone <s@x>"]):
        (tmp_path / "a").write_text(str(i + 1))
        _git(tmp_path, "commit", "-q", "-am", msg)
    ws = GitWorkspace(str(tmp_path))
    ops = ws.operator_commits(base, "HEAD")
    subjects = dict(ws.commits(base, "HEAD"))
    assert [subjects[s] for s in ops] == ["docs: brief"]

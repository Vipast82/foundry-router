"""Immutable reads at a revision for review packets (v0.121.8): what a commit
contained, never the working tree."""

from __future__ import annotations

import subprocess

from foundry_coordinator.adapters.git import Workspace


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo(tmp_path):
    root = tmp_path / "r"
    (root / "src").mkdir(parents=True)
    (root / "docs" / "evidence").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "src" / "a.lua").write_text("return 1\n")
    (root / "docs" / "evidence" / "x.png").write_bytes(b"\x89PNG fake")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = Workspace(str(root)).head()
    (root / "src" / "a.lua").write_text("return 2\n")
    (root / "docs" / "big.log").write_text("line\n" * 5000)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "head")
    return root, Workspace(str(root)), base


def test_show_at_reads_the_commit_not_the_working_tree(tmp_path):
    root, ws, _ = _repo(tmp_path)
    head = ws.head()
    (root / "src" / "a.lua").write_text("return 999 -- uncommitted\n")
    assert ws.show_at(head, "src/a.lua") == "return 2\n"
    assert ws.show_at(head, "src/missing.lua") is None
    assert ws.blob_at(head, "src/a.lua") and ws.blob_at(head, "nope") is None


def test_ls_tree_and_per_file_diffs(tmp_path):
    root, ws, base = _repo(tmp_path)
    head = ws.head()
    assert [p for p, _ in ws.ls_tree_at(head, "docs/evidence")] == ["docs/evidence/x.png"]
    files = {f["path"]: f for f in ws.diff_files(base, head)}
    assert files["docs/big.log"]["added"] == 5000 and files["src/a.lua"]["added"] == 1
    d = ws.file_diff(base, head, "src/a.lua")
    assert "+return 2" in d and "big.log" not in d

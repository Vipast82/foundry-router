"""foundry-blender (docs/plans/SLOT_BLENDER_PLAN.md B1): Blender on a Studio slot."""

import io
import os
import subprocess
import tarfile

import pytest

from foundry_coordinator import slot_blender as SB
from foundry_coordinator import slots


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo)] + list(a), check=True, capture_output=True)


def _repo(tmp_path):
    r = tmp_path / "wt"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a", encoding="utf-8")
    (r / "gone.txt").write_text("g", encoding="utf-8")
    (r / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "c")
    return r


def test_local_changes_are_changed_new_and_deleted_files(tmp_path):
    r = _repo(tmp_path)
    (r / "a.txt").write_text("changed", encoding="utf-8")
    (r / "new").mkdir()
    (r / "new" / "b.py").write_text("b", encoding="utf-8")
    (r / "ignored").mkdir()
    (r / "ignored" / "x").write_text("x", encoding="utf-8")
    os.remove(r / "gone.txt")
    staged = r / "staged.txt"
    staged.write_text("s", encoding="utf-8")
    _git(r, "add", "staged.txt")
    files, deleted = SB.local_changes(str(r))
    assert sorted(files) == ["a.txt", "new/b.py", "staged.txt"]
    assert deleted == ["gone.txt"]


def test_remote_script_locks_uses_its_own_checkout_and_quotes_arguments():
    s = SB.remote_script("wt-1", 600, 0, ["-b", "--factory-startup", "-P", "tools/x.py", "--",
                                            "--name", "a b; rm -rf /"])
    assert 'flock -w 600 9' in s and f"exit {SB.BUSY_EXIT}" in s
    assert 'W="$HOME/work/blender"' in s and "refs/heads/blender/wt-1" in s
    assert "blender -t \"$T\" -b --factory-startup -P tools/x.py -- --name 'a b; rm -rf /' 1>&2" in s
    assert "-newer \"$M\"" in s and "-path ./.git" in s and "__pycache__" in s
    # threads given by the sprint are kept; a fixed --threads is used as given
    assert 'blender -t 2 ' in SB.remote_script("x", 5, 0, ["-t", "2", "-b"])
    assert "T=6\n" in SB.remote_script("x", 5, 6, ["-b"])


def _tar(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in entries:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    return buf


def test_receive_writes_only_inside_the_worktree(tmp_path):
    r = tmp_path / "wt"
    r.mkdir()
    back = SB.receive(_tar([("./assets/out.fbx", b"fbx"), ("../escape.txt", b"no"),
                            ("/abs.txt", b"no"), (".git/config", b"no"),
                            ("./sub/deep/x.png", b"png")]), str(r))
    assert back == ["assets/out.fbx", "sub/deep/x.png"]
    assert (r / "assets" / "out.fbx").read_bytes() == b"fbx"
    assert not (tmp_path / "escape.txt").exists() and not (r / ".git").exists()
    assert SB.receive(io.BytesIO(b""), str(r)) == []          # nothing back (a failed start)


def test_send_tar_carries_files_and_the_delete_list(tmp_path):
    r = _repo(tmp_path)
    (r / "a.txt").write_text("changed", encoding="utf-8")

    class Sink(io.BytesIO):
        def close(self):
            self.done = self.getvalue()
    sink = Sink()
    SB._send_tar(sink, str(r), ["a.txt"], ["gone.txt"])
    with tarfile.open(fileobj=io.BytesIO(sink.done)) as tf:
        names = tf.getnames()
        assert names == ["a.txt", SB._DELETE_LIST]
        assert tf.extractfile(SB._DELETE_LIST).read() == b"gone.txt"


def test_cli_keeps_everything_after_the_first_dash_dash_for_blender(monkeypatch):
    seen = {}

    def fake_run(a, args):
        seen.update(host=a.host, container=a.container, args=args)
        return 0
    monkeypatch.setattr(SB, "run", fake_run)
    rc = slots.main(["blender", "--host", "studio@h", "--container", "c", "--",
                     "-b", "-P", "tools/x.py", "--", "--selftest"])
    assert rc == 0 and seen == {"host": "studio@h", "container": "c",
                                "args": ["-b", "-P", "tools/x.py", "--", "--selftest"]}


def test_no_slot_means_no_run_and_never_a_local_blender(tmp_path, capsys):
    r = _repo(tmp_path)
    a = type("A", (), {"repo": str(r), "config": "", "host": "", "container": "",
                       "au_push_url": "", "ssh": "", "id": "", "threads": 0, "wait": 5})()
    assert SB.run(a, ["-b"]) == 2
    assert "no slot to run on" in capsys.readouterr().err

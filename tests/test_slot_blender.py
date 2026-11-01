"""foundry-blender (docs/plans/SLOT_BLENDER_PLAN.md B1): Blender on a Studio slot."""

import io
import json
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


# ----------------------------------------------------------------------------- B2: sprints

class _Prof:
    def __init__(self, workspace, raw, pid="animaluprising", slot=""):
        self.workspace, self.raw, self.id = str(workspace), raw, pid
        if slot:
            self.__dict__["slot"] = slot


def _raw(blender_slot="slot-1", host="studio@192.168.0.115"):
    return {"blender": {"slot": blender_slot},
            "slots": {"slot-1": {"container": "studio-slot", "host": host},
                      "slot-9": {"container": "x", "host": "studio@h9", "enabled": False}},
            "role_rules": {}}


def test_blender_slot_comes_from_the_track_then_the_project():
    assert SB.blender_slot(_Prof("w", _raw())) == "slot-1"
    assert SB.blender_slot(_Prof("w", _raw(""))) == ""
    assert SB.blender_slot(_Prof("w", _raw(""), slot="slot-2")) == "slot-2"   # a slot-bound track
    assert SB.deny_rules(_Prof("w", _raw(""))) == []
    assert {r["pattern"] for r in SB.deny_rules(_Prof("w", _raw()))} == set(SB.LOCAL_BLENDER)


def test_prepare_writes_a_drop_in_for_blender(tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_BLENDER_DIR", str(tmp_path / "fb"))
    ws = tmp_path / "AnimalUprising"
    ws.mkdir()
    b = SB.prepare(_Prof(ws, _raw()), str(tmp_path / "state"))
    assert b["slot"] == "slot-1" and os.path.isfile(b["command"])
    cfg = json.loads(open(b["config"], encoding="utf-8").read())
    assert cfg["host"] == "studio@192.168.0.115" and cfg["container"] == "studio-slot"
    assert cfg["workspace"] == os.path.realpath(str(ws))
    text = open(b["command"], encoding="utf-8").read()
    assert "-m foundry_coordinator.slots blender --config" in text and "--repo" in text
    assert ".foundry-coordinator" not in b["command"]
    assert ("%*" in text) if os.name == "nt" else ('"$@"' in text)
    assert SB.prepare(_Prof(ws, _raw("")), str(tmp_path / "state")) is None
    assert "no enabled slot slot-9" in SB.prepare(_Prof(ws, _raw("slot-9")), str(tmp_path))["error"]
    assert "local container slot" in SB.prepare(_Prof(ws, _raw(host="")), str(tmp_path))["error"]


def test_absolute_paths_inside_the_worktree_become_relative(tmp_path):
    r = tmp_path / "wt"
    (r / "tools" / "blender").mkdir(parents=True)
    tool = r / "tools" / "blender" / "x.py"
    tool.write_text("", encoding="utf-8")
    out, why = SB.relative_args(str(r), ["-b", "-P", str(tool), "--", "--selftest"])
    assert why == "" and out == ["-b", "-P", "tools/blender/x.py", "--", "--selftest"]
    other = tmp_path / "elsewhere.blend"
    other.write_text("", encoding="utf-8")
    out, why = SB.relative_args(str(r), ["-b", str(other)])
    assert out == [] and "outside the worktree" in why


def test_kilo_rules_deny_the_local_blender_while_it_runs_on_a_slot(tmp_path):
    from foundry_coordinator import kilo_config as KC
    rules = KC.with_extra({"implementer": {}}, SB.deny_rules(_Prof("w", _raw())))
    from foundry_automation import role_rules as RR
    for role in RR.RULE_ROLES:
        assert {"permission": "bash", "pattern": "*blender.exe*", "action": "deny"} \
            in rules[role]["extra"]
    assert KC.with_extra({"implementer": {}}, []) == {"implementer": {}}
    # what the GUI path tester says for the generated rules (it takes one role's rules)
    t = RR.test("implementer", rules["implementer"], "bash",
                '"C:/Program Files/Blender Foundation/Blender 4.5/blender.exe" -b')
    assert t["decision"] == "deny", t
    t = RR.test("implementer", rules["implementer"], "bash",
                'py -3 tools\\guard\\run.py --name blender --shell none -- '
                '"C:/Users/x/.foundry-blender/animaluprising-1.cmd" -b')
    # the drop-in itself is allowed: it lives in ~/.foundry-blender (bash rules deny every
    # command naming .foundry-coordinator, where it first lived)
    assert t["decision"] == "allow", t
    # the Blender tools themselves stay allowed
    t = RR.test("implementer", rules["implementer"], "bash", "py -3 tools/assets/selftest_all.py")
    assert t["decision"] == "allow", t


def test_the_config_pull_rewrites_kilo_when_the_blender_setting_changes():
    from foundry_automation import document as D
    from foundry_coordinator.config_pull import role_rules_changed
    a = D.empty("x")
    b = D.empty("x")
    b["settings"]["blender"] = {"slot": "slot-1"}
    assert role_rules_changed(a, b)


# ----------------------------------------------------------------------------- B2b: library

def test_library_manifest_and_diff(tmp_path):
    lib = tmp_path / "AssetLibrary"
    (lib / "K1_KenneyNatureKit" / "Models").mkdir(parents=True)
    (lib / "K1_KenneyNatureKit" / "Models" / "rock.glb").write_bytes(b"rock")
    (lib / "Q4b" ).mkdir()
    (lib / "Q4b" / "Lilypad.fbx").write_bytes(b"lily")
    (lib / ".cache").mkdir()
    (lib / ".cache" / "x").write_bytes(b"skip")
    m = SB.lib_manifest(str(lib))
    assert sorted(m) == ["K1_KenneyNatureKit/Models/rock.glb", "Q4b/Lilypad.fbx"]
    assert m["Q4b/Lilypad.fbx"][0] == 4
    # nothing on the slot yet: everything goes
    assert SB.lib_diff(m, {}) == (sorted(m), [])
    # the slot has an old rock and a file deleted here
    remote = {"K1_KenneyNatureKit/Models/rock.glb": [3, 1], "Q4b/Lilypad.fbx": m["Q4b/Lilypad.fbx"],
              "old/gone.fbx": [1, 1]}
    assert SB.lib_diff(m, remote) == (["K1_KenneyNatureKit/Models/rock.glb"], ["old/gone.fbx"])
    assert SB.lib_hash(m) == SB.lib_hash(dict(reversed(list(m.items()))))   # order-free


def test_library_sync_skips_a_missing_library(tmp_path):
    ok, msg = SB.lib_sync({"container": "c", "host": "h"}, str(tmp_path / "none"))
    assert ok and "skipped" in msg


def test_run_script_points_the_tools_at_the_slots_copy():
    s = SB.remote_script("x", 5, 0, ["-b"], library=True)
    assert 'export AU_ASSET_LIBRARY="$HOME/assetlib"' in s
    assert 'ln -sfn "$HOME/assetlib" "$W/C:/Roblox/AssetLibrary"' in s
    assert 'ln -sfn "$HOME/assetlib" "$W/C:\Roblox\AssetLibrary"' in s
    # links are made after `git clean` and before the marker, so they are never copied back
    assert s.index("clean -q -fd") < s.index("ln -sfn") < s.index('touch "$M"')
    assert "AU_ASSET_LIBRARY" not in SB.remote_script("x", 5, 0, ["-b"])


def test_prepare_records_the_library(tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_BLENDER_DIR", str(tmp_path / "fb"))
    ws = tmp_path / "w"
    ws.mkdir()
    raw = _raw()
    b = SB.prepare(_Prof(ws, raw), str(tmp_path))
    assert json.loads(open(b["config"], encoding="utf-8").read())["library"] == "C:/Roblox/AssetLibrary"
    raw["blender"]["library"] = ""
    b = SB.prepare(_Prof(ws, raw), str(tmp_path))
    assert json.loads(open(b["config"], encoding="utf-8").read())["library"] == ""

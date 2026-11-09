"""foundry-slot-fetch: files a Studio MCP tool saves on a slot come back into the worktree
(2026-11-09 ART-POI1: export_rbxm wrote inside the slot container and the sprint stopped)."""
import io
import os
import subprocess
import tarfile
from types import SimpleNamespace

from foundry_coordinator import slots

REPO = "/home/studio/work/AnimalUprising"


def _tar(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_paths_are_made_repo_relative(tmp_path):
    wt = str(tmp_path)
    assert slots.fetch_rel("assets/models/X.rbxm", REPO, wt) == ("assets/models/X.rbxm", "")
    assert slots.fetch_rel(REPO + "/assets/models/X.rbxm", REPO, wt)[0] == "assets/models/X.rbxm"
    assert slots.fetch_rel(os.path.join(wt, "assets", "X.rbxm"), REPO, wt)[0] == "assets/X.rbxm"
    assert slots.fetch_rel("./docs/evidence/A/", REPO, wt)[0] == "docs/evidence/A"


def test_paths_outside_the_repo_are_refused(tmp_path):
    wt = str(tmp_path)
    for bad in ("../x", "/etc/passwd", "C:\\Roblox\\x.rbxm", ".git/config", "", "a/../../b"):
        rel, why = slots.fetch_rel(bad, REPO, wt)
        assert rel == "" and why, bad


def test_fetch_copies_only_the_named_paths(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, _tar({
            "assets/models/X.rbxm": b"rbxm", "src/evil.lua": b"no", "../escape": b"no"}), b"")
    monkeypatch.setattr(slots.subprocess, "run", fake_run)
    got, why, rc = slots.fetch({"container": "studio-slot"}, REPO, str(tmp_path),
                               [REPO + "/assets/models/X.rbxm"])
    assert (got, why, rc) == (["assets/models/X.rbxm"], "", 0)
    assert (tmp_path / "assets" / "models" / "X.rbxm").read_bytes() == b"rbxm"
    assert not (tmp_path / "src").exists()
    assert "studio-slot" in seen["argv"] and "assets/models/X.rbxm" in seen["argv"][-1]


def test_a_missing_file_on_the_slot_is_exit_4(tmp_path, monkeypatch):
    monkeypatch.setattr(slots.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 4, b"", b"missing on the slot: assets/X.rbxm"))
    got, why, rc = slots.fetch({"container": "c"}, REPO, str(tmp_path), ["assets/X.rbxm"])
    assert got == [] and rc == 4 and "missing" in why


def test_the_drop_in_names_the_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_BLENDER_DIR", str(tmp_path / "dropins"))
    prof = SimpleNamespace(id="animaluprising", workspace=str(tmp_path / "wt"),
                           raw={"slots": {"slot-2": {"container": "studio-slot",
                                                     "host": "192.0.2.6"}}})
    f = slots.prepare_fetch(prof, "slot-2")
    assert os.path.isfile(f["command"]) and f["repo"] == REPO
    assert "slots fetch --config" in open(f["command"], encoding="utf-8").read()
    assert slots.prepare_fetch(prof, "slot-9").get("error")


def test_the_cli_reports_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FOUNDRY_BLENDER_DIR", str(tmp_path / "dropins"))
    prof = SimpleNamespace(id="animaluprising", workspace=str(tmp_path / "wt"),
                           raw={"slots": {"slot-1": {"container": "studio-slot"}}})
    f = slots.prepare_fetch(prof, "slot-1")
    monkeypatch.setattr(slots.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, _tar({"assets/X.rbxm": b"1"}), b""))
    assert slots.main(["fetch", "--config", f["config"], "assets/X.rbxm"]) == 0
    assert "SLOT-FETCH OK: 1 file(s): assets/X.rbxm" in capsys.readouterr().out
    assert (tmp_path / "wt" / "assets" / "X.rbxm").read_bytes() == b"1"

"""Slot upkeep (slotkeep.py): items 17, 18 and 19 of COORDINATOR_AUTOMATION_NEEDS. The
readings and the push run in a worker, never on the tick thread; the tick writes the events."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from foundry_coordinator import __version__, automation, board, slotkeep
from foundry_coordinator import slots as SL
from tests.coordinator.test_budget_policy import game

DF = """Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/sda2        229585956 181234532  36628208      84% /srv
"""


def world(tmp_path, monkeypatch, pct=84, version=__version__, push_rc=0):
    c, P, clk = game(tmp_path)
    P.profile.raw["slots"] = {
        "slot-1": {"container": "au-slot-1"},
        "slot-3": {"container": "au-slot-3", "host": "studio@192.168.0.117"}}
    ran = {"cmd": [], "push": []}

    def run(argv, timeout=60.0, stdout=None):
        line = " ".join(str(a) for a in argv)
        ran["cmd"].append(line)
        if "rev-parse --verify" in line:
            return 0, "a" * 40 + "\n"
        if " df " in f" {line} " or "df -P" in line:
            return 0, DF.replace("84%", f"{pct}%")
        if "__version__" in line:
            return 0, f'0123456789ab\n"{version}"\n'
        return 0, ""

    def git(cwd, *args, timeout=300, env=None):
        ran["push"].append(list(args))
        return subprocess.CompletedProcess(args, push_rc, stdout=b"" if push_rc == 0 else b"denied")
    monkeypatch.setattr(slotkeep, "_run", run)
    monkeypatch.setattr(SL, "_git", git)
    return c, P, clk, ran


def sync(target, args):
    target(*args)


def events(c, kind):
    return [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind=? ORDER BY id", (kind,))]


def rounds(c, P, clk, n=1):
    out = []
    for _ in range(n):
        slotkeep.tick(c, P, start=sync)      # starts the workers
        clk.advance(1)
        out += slotkeep.tick(c, P, start=sync)   # collects
    return out


def test_parse_df_and_the_bare_path():
    assert slotkeep.parse_df(DF) == {"pct": 84, "free_gb": 34.9, "total_gb": 219.0}
    assert slotkeep.parse_df("ssh: connect to host 192.168.0.117 port 22: timed out") is None
    assert slotkeep.bare_path({"au_push_url": "ssh://studio@h/srv/studio-slot/AnimalUprising.git"}) \
        == "/srv/studio-slot/AnimalUprising.git"
    assert slotkeep.bare_path({}) == ""


def test_replay_a_full_slot_disk_raises_one_alert_a_day(tmp_path, monkeypatch):
    """2026-11-21: the slots stood at 84% / 80% / 77% and nobody was told."""
    c, P, clk, ran = world(tmp_path, monkeypatch, pct=84)
    out = rounds(c, P, clk)
    alerts = [e for e in events(c, "operator_notice") if e.get("kind") == "slot_disk"]
    assert sorted(a["slot"] for a in alerts) == ["slot-1", "slot-3"]
    a = alerts[0]
    assert a["alert_class"] == "watch" and a["title"].endswith("disk 84% full")
    assert "34.9 GB free of 219.0 GB; alert at 80%" in a["text"]
    # read again every ten minutes, alerted once a day
    for _ in range(6):
        clk.advance(601)
        rounds(c, P, clk)
    assert len([e for e in events(c, "operator_notice") if e.get("kind") == "slot_disk"]) == 2
    clk.advance(24 * 3600)
    rounds(c, P, clk)
    assert len([e for e in events(c, "operator_notice") if e.get("kind") == "slot_disk"]) == 4
    # the limit is a setting; 0 = no alert
    c2, P2, clk2, _ = world(tmp_path / "b", monkeypatch, pct=84)
    automation.set_live(P2, "slot_disk_alert_pct", 90)
    rounds(c2, P2, clk2)
    automation.set_live(P2, "slot_disk_alert_pct", 0)
    clk2.advance(601)
    rounds(c2, P2, clk2)
    assert [e for e in events(c2, "operator_notice") if e.get("kind") == "slot_disk"] == []


def test_an_idle_remote_slot_gets_the_main_branch_every_few_hours(tmp_path, monkeypatch):
    """FIN-4a and BAL-2e3: a lane's first push to a slot took over 30 minutes because the
    slot's copy had nothing committed since its last lane."""
    c, P, clk, ran = world(tmp_path, monkeypatch, pct=40)
    rounds(c, P, clk)
    # the remote slot only: a local slot has no copy to push to
    assert len(ran["push"]) == 1
    args = ran["push"][0]
    assert args[:3] == ["push", "-q", "--force"] and args[-1] == "a" * 40 + ":refs/heads/slot/_keep"
    assert args[3] == "ssh://studio@192.168.0.117/srv/studio-slot/AnimalUprising.git"
    [e] = events(c, "slot_kept_fresh")
    assert e["slot"] == "slot-3" and e["head"] == "a" * 12 and "while it was idle" in e["text"]
    # not again until slot_keep_fresh_hours have passed
    for _ in range(5):
        clk.advance(601)
        rounds(c, P, clk)
    assert len(ran["push"]) == 1
    clk.advance(4 * 3600)
    rounds(c, P, clk)
    assert len(ran["push"]) == 2
    # 0 = never
    automation.set_live(P, "slot_keep_fresh_hours", 0)
    clk.advance(9 * 3600)
    rounds(c, P, clk)
    assert len(ran["push"]) == 2


def test_a_slot_with_a_lane_or_a_held_lock_is_not_pushed_to(tmp_path, monkeypatch):
    c, P, clk, ran = world(tmp_path, monkeypatch, pct=40)
    c.store.add_track("game", "fin4a", "parallel", str(tmp_path / "wt"), "track/fin4a", "a" * 40,
                      ["studio"], ["T1"], "victor", state="running", data={"slot": "slot-3"})
    assert not slotkeep.idle(c, P, "slot-3") and slotkeep.idle(c, P, "slot-1")
    rounds(c, P, clk)
    assert ran["push"] == []
    assert slotkeep.view(c, P)["slot-3"]["disk_pct"] == 40    # it is still read


def test_a_failed_push_is_an_event_and_is_tried_again(tmp_path, monkeypatch):
    c, P, clk, ran = world(tmp_path, monkeypatch, pct=40, push_rc=1)
    rounds(c, P, clk)
    [e] = events(c, "slot_keep_failed")
    assert e["slot"] == "slot-3" and e["error"] == "denied"
    assert c.store.meta("slot_fresh:game:slot-3") in (None, "")
    clk.advance(601)
    rounds(c, P, clk)
    assert len(ran["push"]) == 2


def test_nothing_runs_on_the_tick_thread_and_one_worker_per_slot(tmp_path, monkeypatch):
    c, P, clk, ran = world(tmp_path, monkeypatch)
    started = []
    slotkeep.tick(c, P, start=lambda target, args: started.append((target, args)))
    assert len(started) == 2 and ran["cmd"] == []             # nothing was run by the tick
    for _ in range(5):                                        # workers not done: none added
        clk.advance(700)
        slotkeep.tick(c, P, start=lambda target, args: started.append((target, args)))
    assert len(started) == 2
    for target, args in started:
        target(*args)
    assert slotkeep.tick(c, P, start=sync) != [] or True      # collected without raising
    # a command that blows up leaves a finished, empty result
    box: dict = {}
    monkeypatch.setattr(slotkeep, "read_disk", lambda slot: 1 / 0)
    slotkeep._work({"container": "x"}, {}, box)
    assert "ZeroDivisionError" in box["error"] and "done" in box


def test_the_board_shows_each_slots_disk_version_and_a_version_that_differs(tmp_path, monkeypatch):
    c, P, clk, ran = world(tmp_path, monkeypatch, pct=77, version="0.181.117")
    rounds(c, P, clk)
    v = slotkeep.view(c, P)
    assert v["slot-3"]["disk_pct"] == 77 and v["slot-3"]["foundry"] == "0.181.117"
    assert v["slot-3"]["behind"] == f"runs v0.181.117, this coordinator v{__version__}"
    assert v["slot-3"]["fresh_hours"] == 0.0 and "fresh_hours" not in v["slot-1"]
    b = board.build(c, P)
    seat = next(s for s in b["seats"] if s["seat"] == "slot-3")
    assert seat["upkeep"].startswith("disk 77%, Foundry v0.181.117 (differs: runs v0.181.117")
    assert "slot-3" in board.text(b) and "disk 77%" in board.text(b)
    # the same version: nothing "differs"
    c2, P2, clk2, _ = world(tmp_path / "b", monkeypatch, pct=10)
    rounds(c2, P2, clk2)
    assert "behind" not in slotkeep.view(c2, P2)["slot-1"]


def test_the_push_sends_the_files_that_are_running_not_only_the_last_commit(tmp_path):
    """Item 19: a deploy by copy left the deploy checkout's files ahead of its HEAD, and the
    slots ran the old commit until somebody committed a "deploy snapshot" by hand."""
    root = str(tmp_path / "foundry")
    os.makedirs(os.path.join(root, "pkg"))

    def git(*a):
        return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, check=True,
                              env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
                              ).stdout.strip()
    git("init", "-q")
    for name, text in (("pkg/__init__.py", "__version__ = '1'\n"), ("pkg/a.py", "A = 1\n"),
                       ("README.md", "x\n"), (".gitignore", "*.sqlite\n")):
        with open(os.path.join(root, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    head = git("rev-parse", "HEAD")
    assert SL.foundry_snapshot(root) == (head, "")            # clean: the commit itself
    # a deploy by copy: a changed file, a new module, a state file and a stray folder
    for name, text in (("pkg/a.py", "A = 2\n"), ("pkg/new.py", "N = 1\n"),
                       ("state.sqlite", "db"), ("notes/secret.txt", "s")):
        os.makedirs(os.path.dirname(os.path.join(root, name)), exist_ok=True)
        with open(os.path.join(root, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    before = git("status", "--porcelain")
    sha, note = SL.foundry_snapshot(root)
    assert sha != head and git("rev-parse", f"{sha}^") == head
    files = git("ls-tree", "-r", "--name-only", sha).splitlines()
    assert "pkg/new.py" in files and "state.sqlite" not in files
    assert "notes/secret.txt" not in files                     # only the code packages' new files
    assert git("show", f"{sha}:pkg/a.py") == "A = 2"
    assert "1 changed, 1 new" in note and "notes/secret.txt" in note
    # nothing about the checkout moved
    assert git("rev-parse", "HEAD") == head and git("status", "--porcelain") == before


def test_save_dirty_copies_the_list_and_the_files_and_changes_nothing(tmp_path, monkeypatch):
    calls = []

    def run(argv, timeout=60.0, stdout=None):
        line = " ".join(str(a) for a in argv)
        calls.append(line)
        if "tar --null" in line:
            stdout.write(b"TAR")
            return 0, ""
        if "ls-files" in line:
            return 0, "docs/evidence/X/a.png\nscratch.blend\n"
        return 0, ""
    monkeypatch.setattr(slotkeep, "_run", run)
    got = slotkeep.save_dirty({"container": "au-slot-1"}, "/home/studio/work/blender",
                              str(tmp_path / "save"))
    assert got["files"] == 2
    assert open(got["list"], encoding="utf-8").read() == "docs/evidence/X/a.png\nscratch.blend\n"
    assert open(got["tar"], "rb").read() == b"TAR"
    assert not any("checkout" in c and "sparse" in c or " clean" in c or " reset" in c for c in calls)
    # nothing dirty: a list, no tar
    monkeypatch.setattr(slotkeep, "_run", lambda argv, timeout=60.0, stdout=None: (0, ""))
    got = slotkeep.save_dirty({"container": "au-slot-1"}, "/x", str(tmp_path / "save2"))
    assert got == {"files": 0, "list": got["list"], "tar": ""}
    # a slot that cannot be reached is an error, never an empty save
    monkeypatch.setattr(slotkeep, "_run", lambda argv, timeout=60.0, stdout=None: (255, "no route"))
    with pytest.raises(RuntimeError):
        slotkeep.save_dirty({"container": "au-slot-1"}, "/x", str(tmp_path / "save3"))


def test_cli_sparse_saves_first_and_refuses_without_yes(tmp_path, monkeypatch, capsys):
    from foundry_coordinator import cli
    c, P, clk, ran = world(tmp_path, monkeypatch)
    did = []
    monkeypatch.setattr(slotkeep, "checkouts", lambda slot: ["/home/studio/work/AnimalUprising",
                                                             "/home/studio/work/blender"])
    monkeypatch.setattr(slotkeep, "save_dirty", lambda slot, co, dest: (
        did.append(("save", co)), {"files": 3, "list": dest + "/l.txt", "tar": dest + "/t.tar"})[1])
    monkeypatch.setattr(slotkeep, "sparse", lambda slot, co: (did.append(("sparse", co)), "1.2G")[1])
    args = type("A", (), {"project": "game", "kcmd": "sparse", "slot": "slot-1", "yes": False})()
    assert cli._slot_keep_cmd(c, args) == 2 and did == []
    assert "--yes" in capsys.readouterr().out
    args.yes = True
    assert cli._slot_keep_cmd(c, args) == 0
    # only the Blender checkouts are made sparse, each saved first
    assert did == [("save", "/home/studio/work/blender"), ("sparse", "/home/studio/work/blender")]
    out = capsys.readouterr().out
    assert "3 file(s) saved" in out and "1.2G" in out


def test_the_settings_are_in_the_schema():
    from foundry_automation import schema
    for k, d in (("slot_keep_fresh_hours", 4), ("slot_disk_alert_pct", 80)):
        assert schema.AUTOMATION_DEFAULTS[k] == d and schema._AUTO_DESC[k]
        assert schema.find_field(f"automation.{k}") is not None

"""Build replay R1 (docs/plans/BLENDER_BUILD_REPLAY_PLAN.md): the journal core, foundry-blender's
entries, the drop-in settings, pruning and the bridge's build_run observations."""

import io
import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from foundry_coordinator import build_journal as BJ
from foundry_coordinator import slot_blender as SB
from foundry_coordinator.store import Store
from foundry_coordinator.telemetry import CoordReader, TelemetryBridge


def _png(path, size=(600, 400), color=(200, 30, 30)):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path)


def _rec(state, repo, **kw):
    base = dict(project_id="game", task_id="AS-X1", source="slot", repo=str(repo),
                args=["-b", "--factory-startup", "-P", "tools/blender/build_x.py", "--", "--out",
                      "a"],
                exit_code=0, started_ts=time.time() - 5, ended_ts=time.time(),
                log_lines=["Blender 4.5", "mesh_report: PASS tris=1200", "noise"])
    base.update(kw)
    return BJ.record(str(state), **base)


@pytest.fixture
def ws(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tools" / "blender").mkdir(parents=True)
    (repo / "tools" / "blender" / "build_x.py").write_text("size = 1\nprint('x')\n")
    state = tmp_path / "state"
    state.mkdir()
    Store(str(state / "coordinator.sqlite"))
    return repo, state


def test_script_of_reads_the_builder_script_from_the_real_forms():
    assert BJ.script_of(["-b", "--factory-startup", "-P", "tools/blender/b.py", "--", "-P",
                         "x"]) == "tools/blender/b.py"
    assert BJ.script_of(["-b", "x.blend", "--python", '"tools\\blender\\b.py"']) == \
        "tools\\blender\\b.py"
    assert BJ.script_of(["--python=tools/a.py"]) == "tools/a.py"
    assert BJ.script_of(["-b", "--", "-P", "after_dashdash.py"]) == ""
    assert BJ.script_of(["-b", "--version"]) == ""


def test_render_views_come_first_and_are_capped():
    paths = ["out/zz.png", "out/model_front.png", "out/notes.txt", "out/model_back.png",
             "out/a.png"]
    assert BJ.pick_renders(paths, 3) == ["out/model_back.png", "out/model_front.png", "out/a.png"]
    assert BJ.pick_renders(paths, 0) == []


def test_settings_have_defaults_and_types():
    p = SimpleNamespace(raw={"blender": {"journal": 1, "journal_renders": "4",
                                         "journal_keep_days": "bad"}})
    o = BJ.settings(p)
    assert o["journal"] is True and o["journal_renders"] == 4
    assert o["journal_keep_days"] == 30 and o["journal_thumb_px"] == 256
    assert BJ.settings(SimpleNamespace(raw={}))["journal"] is False


def test_an_entry_keeps_script_log_files_and_thumbnails(ws):
    repo, state = ws
    _png(str(repo / "renders" / "x_front.png"))
    (repo / "assets").mkdir()
    (repo / "assets" / "x.blend").write_bytes(b"0" * 1000)
    e = _rec(state, repo, files_back=["renders/x_front.png", "assets/x.blend"],
             timings={"blender": 4.0}, where={"slot": "slot-1"})
    assert e and e["exit"] == 0 and e["where"]["slot"] == "slot-1"
    s = e["script"]
    assert s["path"] == "tools/blender/build_x.py" and len(s["sha256"]) == 64 and s["first"]
    assert ["assets/x.blend", 1000] in e["files_back"]
    assert e["key_lines"] == ["mesh_report: PASS tris=1200"]
    assert "noise" in e["log_tail"]
    (shot,) = e["renders"]
    assert shot["path"] == "renders/x_front.png" and shot["thumb"] == "thumbs/00.png"
    d = BJ.run_dir(str(state), "game", "AS-X1", e["run_id"])
    thumb = os.path.join(d, "thumbs", "00.png")
    assert os.path.getsize(thumb) <= BJ.THUMB_CAP
    from PIL import Image
    with Image.open(thumb) as im:
        assert max(im.size) == 256
    assert open(os.path.join(d, "script.txt")).read() == "size = 1\nprint('x')\n"
    assert json.load(open(os.path.join(d, "journal.json")))["run_id"] == e["run_id"]
    assert [r["run_id"] for r in BJ.runs(str(state), "game", "AS-X1")] == [e["run_id"]]


def test_the_next_run_of_the_same_script_has_a_diff(ws):
    repo, state = ws
    a = _rec(state, repo, started_ts=time.time() - 60, ended_ts=time.time() - 50)
    (repo / "tools" / "blender" / "build_x.py").write_text("size = 2\nprint('x')\n")
    b = _rec(state, repo)
    s = b["script"]
    assert s["previous_run"] == a["run_id"] and s["changed"] is True
    assert "-size = 1" in s["diff"] and "+size = 2" in s["diff"]
    c = _rec(state, repo, started_ts=time.time() + 1)
    assert c["script"]["changed"] is False and c["script"]["diff"] == ""


def test_edited_after_the_run_is_marked(ws):
    repo, state = ws
    e = _rec(state, repo, started_ts=time.time() - 100, ended_ts=time.time() - 90)
    assert e["script"]["edited_after_run"] is True


def test_long_logs_and_diffs_are_capped(ws):
    repo, state = ws
    _rec(state, repo, started_ts=time.time() - 60)
    (repo / "tools" / "blender" / "build_x.py").write_text(
        "\n".join(f"line {i}" for i in range(2000)))
    e = _rec(state, repo, log_lines=[f"l{i}" for i in range(1000)])
    assert len(e["log_tail"].splitlines()) == BJ.LOG_TAIL
    assert e["log_tail"].splitlines()[-1] == "l999"
    assert len(e["script"]["diff"].splitlines()) == BJ.DIFF_LINES + 1
    assert e["script"]["diff_lines"] > BJ.DIFF_LINES


def test_the_same_run_id_is_one_row(ws):
    repo, state = ws
    _rec(state, repo, run_id="r-1")
    _rec(state, repo, run_id="r-1", exit_code=1)
    rows = BJ.runs(str(state), "game", "AS-X1")
    assert len(rows) == 1 and rows[0]["exit"] == 1


def test_renders_off_and_no_pillow_keep_names_only(ws, monkeypatch):
    repo, state = ws
    _png(str(repo / "r" / "a_front.png"))
    assert _rec(state, repo, files_back=["r/a_front.png"],
                opts={"journal_renders": 0})["renders"] == []
    monkeypatch.setattr(BJ, "thumbnail", lambda *a: False)
    (shot,) = _rec(state, repo, files_back=["r/a_front.png"])["renders"]
    assert "thumb" not in shot and shot["bytes"] > 0


def test_a_journal_that_cannot_be_written_returns_none(tmp_path):
    bad = tmp_path / "file"
    bad.write_text("x")
    assert BJ.record(str(bad), project_id="g", task_id="t", source="slot", repo=str(tmp_path),
                     args=[], exit_code=0, started_ts=0, ended_ts=0, log_lines=[]) is None


# ----------------------------------------------------------------------------- foundry-blender

def test_remote_script_marks_the_end_of_blender():
    s = SB.remote_script("run-1", 600, 0, ["-b", "--version"])
    assert 'echo "foundry-blender: blender exit $rc" >&2' in s
    assert s.index("rc=$?") < s.index("blender exit $rc") < s.index("exit $rc\n")


def test_a_slot_blender_job_has_a_time_limit_that_frees_its_lock():
    s = SB.remote_script("run-1", 600, 0, ["-b", "--version"])
    assert f"timeout --kill-after=30 {SB.MAX_JOB_SECONDS} blender" in s
    assert "ran past" in s and s.index("rc=$?") < s.index("ran past")
    assert "timeout --kill-after" not in SB.remote_script("run-1", 600, 0, ["-b"], max_seconds=0)
    assert "timeout --kill-after=30 900 blender" in SB.remote_script("r", 600, 0, ["-b"],
                                                                      max_seconds=900)


def test_tee_streams_and_keeps_tail_keys_and_marks(capsys):
    stream = io.BytesIO(b"foundry-blender: running on slot with 14 threads: blender -b\n"
                        b"Blender 4.5\nmesh_report: FAIL tris\n"
                        b"foundry-blender: blender exit 0\n")
    from collections import deque
    tail, keys, marks = deque(maxlen=400), [], {}
    SB._tee(stream, tail, keys, marks)
    assert list(tail)[1] == "Blender 4.5"
    assert "mesh_report: FAIL tris" in keys
    assert {"blender_start", "blender_end"} <= set(marks)
    assert "mesh_report: FAIL tris" in capsys.readouterr().err


def test_a_slot_run_writes_one_entry_with_timings(ws, monkeypatch):
    repo, state = ws
    monkeypatch.setenv("FOUNDRY_COORD_STATE", str(state))
    _png(str(repo / "renders" / "x_front.png"))
    t0 = time.time() - 30
    marks = {"pushed": t0 + 2, "library": t0 + 3, "blender_start": t0 + 5,
             "blender_end": t0 + 25}
    cfg = {"project": "game", "task": "AS-X1", "slot": "slot-1", "journal": True,
           "journal_renders": 12}
    SB._journal(cfg, str(repo), ["-b", "-P", "tools/blender/build_x.py"], 0, t0, marks,
                ["mesh_report: PASS"], ["mesh_report: PASS"], ["renders/x_front.png"],
                {"slot": "slot-1", "commit": "abc"})
    (e,) = BJ.runs(str(state), "game", "AS-X1")
    assert e["timings"]["push"] == 2.0 and e["timings"]["blender"] == 20.0
    assert e["timings"]["prepare"] == 2.0 and "copy_back" in e["timings"]
    assert e["renders"][0]["thumb"] and e["where"]["commit"] == "abc"


def test_a_busy_slot_is_an_entry_without_renders(ws, monkeypatch):
    repo, state = ws
    monkeypatch.setenv("FOUNDRY_COORD_STATE", str(state))
    _png(str(repo / "renders" / "x_front.png"))
    SB._journal({"project": "game", "task": "AS-X1", "journal": True}, str(repo),
                ["-b", "-P", "tools/blender/build_x.py"], SB.BUSY_EXIT, time.time(), {}, [], [],
                ["renders/x_front.png"], {})
    (e,) = BJ.runs(str(state), "game", "AS-X1")
    assert e["exit"] == SB.BUSY_EXIT and e["renders"] == [] and e["files_back"] == []
    assert "busy" in e["notes"][0]


def test_a_failed_journal_never_raises(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "file"
    bad.write_text("x")
    monkeypatch.setenv("FOUNDRY_COORD_STATE", str(bad))
    SB._journal({"project": "g", "task": "t"}, str(tmp_path), [], 0, time.time(), {}, [], [], [],
                {})
    assert "could not be written" in capsys.readouterr().err


class _Prof:
    def __init__(self, ws, raw):
        self.id, self.workspace, self.raw, self.__dict__["slot"] = "game", ws, raw, ""


def test_prepare_writes_the_task_and_journal_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_BLENDER_DIR", str(tmp_path / "drop"))
    ws = tmp_path / "AnimalUprising"
    ws.mkdir()
    raw = {"blender": {"slot": "slot-1", "journal": True, "journal_renders": 6},
           "slots": {"slot-1": {"enabled": True, "host": "studio@h", "container": "c"}}}
    b = SB.prepare(_Prof(str(ws), raw), str(tmp_path / "state"), "AS-X1", "run-7")
    assert "error" not in b, b
    cfg = json.load(open(b["config"]))
    assert cfg["task"] == "AS-X1" and cfg["run_id"] == "run-7"
    assert cfg["journal"] is True and cfg["journal_renders"] == 6
    raw["blender"].pop("journal")
    cfg = json.load(open(SB.prepare(_Prof(str(ws), raw), str(tmp_path / "state"))["config"]))
    assert cfg["journal"] is False and cfg["task"] == ""


# ----------------------------------------------------------------------------- pruning

class _Engine:
    def __init__(self, state, tasks):
        self.state_dir = str(state)
        self.store = SimpleNamespace(task=lambda pid, tid: tasks.get(tid))


def test_sweep_prunes_only_finished_tasks(ws):
    repo, state = ws
    now = time.time()
    for tid in ("OLD", "DONE", "LIVE"):
        for i in range(3):
            _rec(state, repo, task_id=tid, started_ts=now - 100 + i, run_id=f"{tid}-{i}")
    tasks = {"OLD": {"state": "COMPLETE", "updated_ts": now - 40 * 86400},
             "DONE": {"state": "CANCELLED", "updated_ts": now - 86400},
             "LIVE": {"state": "BUILDING", "updated_ts": now - 90 * 86400}}
    P = SimpleNamespace(id="game", profile=SimpleNamespace(raw={"blender": {
        "journal_keep_days": 30, "journal_max_runs": 2}}))
    assert BJ.sweep(_Engine(state, tasks), P, force=True) == 4
    assert BJ.runs(str(state), "game", "OLD") == []
    assert [r["run_id"] for r in BJ.runs(str(state), "game", "DONE")] == ["DONE-1", "DONE-2"]
    assert len(BJ.runs(str(state), "game", "LIVE")) == 3
    assert not os.path.isdir(BJ.run_dir(str(state), "game", "OLD", "OLD-0"))
    assert BJ.sweep(_Engine(state, tasks), P) == 0                   # hourly


# ----------------------------------------------------------------------------- bridge

def test_the_bridge_sends_each_run_once_with_thumbnails_first(ws):
    repo, state = ws
    _png(str(repo / "renders" / "x_front.png"))
    e = _rec(state, repo, files_back=["renders/x_front.png"])
    br = TelemetryBridge.__new__(TelemetryBridge)
    br.reader = CoordReader(str(state / "coordinator.sqlite"))
    br.projects, br.fp = {"game": object()}, {}
    obs, commits = [], []
    br._build_facts(obs, commits, time.time())
    (o,) = obs
    assert o["type"] == "build_run" and o["run_id"] == e["run_id"] and o["task_id"] == "AS-X1"
    assert o["entry"]["script"]["path"] == "tools/blender/build_x.py"
    (t,) = o["thumbs"]
    assert t["name"] == "thumbs/00.png" and t["mime"] == "image/png" and t["b64"]
    for c in commits:
        c()
    obs = []
    br._build_facts(obs, [], time.time())
    assert obs == []
    br.projects = {"other": object()}
    br.fp = {}
    br._build_facts(obs, [], time.time())
    assert obs == []


def test_an_old_coordinator_without_the_table_sends_nothing(tmp_path):
    db = tmp_path / "c.sqlite"
    sqlite3.connect(str(db)).execute("CREATE TABLE meta (key TEXT, value TEXT)")
    assert CoordReader(str(db)).build_runs(0) == []


def test_a_restarted_bridge_catches_up_in_steps(ws, monkeypatch):
    repo, state = ws
    for i in range(5):
        _rec(state, repo, run_id=f"r-{i}", started_ts=time.time() - 50 + i)
    br = TelemetryBridge.__new__(TelemetryBridge)
    br.reader = CoordReader(str(state / "coordinator.sqlite"))
    br.projects, br.fp = {"game": object()}, {}
    monkeypatch.setattr(TelemetryBridge, "BUILD_FIRST_SENDS", 2)
    sent = []
    for _ in range(3):
        obs, commits = [], []
        br._build_facts(obs, commits, time.time())
        for c in commits:
            c()
        sent.append([o["run_id"] for o in obs])
    assert sent == [["r-0", "r-1"], ["r-2", "r-3"], ["r-4"]]


# ----------------------------------------------------------------------------- R3: this PC

B = r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe"
FORMS = [
    (rf'cd C:\Roblox\AU; $b="{B}"; py -3 tools\guard\run.py --name x -- "$b" -b --factory-startup '
     r'-P tools/blender/export_clip_json.py -- --all', "tools/blender/export_clip_json.py"),
    (rf'$b="{B}"; & $b -b --factory-startup -P tools/blender/build_bs_owl.py -- --save a.blend '
     r'2>&1 | Select -Last 4', "tools/blender/build_bs_owl.py"),
    (rf'& "{B}" -b assets/x.blend -P tools\blender\render_views.py -- --out tmp/r --name X',
     "tools/blender/render_views.py"),
    ("blender -b --python tools/blender/a.py", "tools/blender/a.py"),
]
NOT_RUNS = ["git add tools/blender/build_bs_owl.py", "py -3 tools/blender/build_bs_king.py -- --plan",
            r"Select-String -Path docs\ENVIRONMENT.md -Pattern blender",
            r'"C:\Users\me\.foundry-blender\au-1.cmd" -b -P tools/blender/x.py',
            "python -m foundry_coordinator.slots blender -- -b -P x.py"]


@pytest.mark.parametrize("cmd,script", FORMS)
def test_the_real_blender_forms_are_runs_with_their_script(cmd, script):
    assert BJ.is_local_blender(cmd, [])
    assert BJ.script_of(BJ.blender_args(cmd)) == script


@pytest.mark.parametrize("cmd", NOT_RUNS)
def test_commands_that_only_mention_blender_are_not_runs(cmd):
    assert not BJ.is_local_blender(cmd, ["selftest_all.py"])


def test_a_tool_that_starts_blender_itself_is_a_run():
    cmd = rf'py -3 tools/blender/selftest_all.py --blender "{B}"'
    assert BJ.is_local_blender(cmd, ["selftest_all.py"])
    assert not BJ.is_local_blender(cmd, [])


def _part(pid, cmd, start, end, status="completed", out="mesh_report: PASS\n", exit_code=0):
    return {"type": "tool", "id": pid, "callID": "c" + pid, "tool": "bash",
            "state": {"status": status, "input": {"command": cmd}, "output": out,
                      "metadata": {"exit": exit_code},
                      "time": {"start": int(start * 1000), "end": int(end * 1000)}}}


class _WS:
    def head(self):
        return "abc123"


def _proj(repo, raw=None):
    return SimpleNamespace(id="game", ws=_WS(), profile=SimpleNamespace(
        workspace=str(repo), raw=raw if raw is not None else {"blender": {"journal": True}}))


def test_a_transcript_gives_one_entry_per_blender_call_with_its_renders(ws):
    repo, state = ws
    now = time.time()
    _png(str(repo / "tmp" / "r" / "X_front.png"))
    os.utime(repo / "tmp" / "r" / "X_front.png", (now - 30, now - 30))
    _png(str(repo / "tmp" / "r" / "old_back.png"))
    os.utime(repo / "tmp" / "r" / "old_back.png", (now - 900, now - 900))
    run = (rf'$b="{B}"; & $b -b x.blend -P tools/blender/build_x.py -- --out tmp/r --name X')
    msgs = [{"info": {"role": "assistant"}, "parts": [
        _part("p1", run, now - 40, now - 29),
        _part("p2", "git status", now - 20, now - 19),
        _part("p3", run, now - 10, now, status="running"),
        _part("p4", r'"C:\Users\me\.foundry-blender\au.cmd" -b -P tools/blender/build_x.py',
              now - 9, now - 8)]}]
    P = _proj(repo)
    o = BJ.settings(P.profile)
    assert BJ.journal_transcript(str(state), P, "AS-X1", "ses_1", msgs, str(repo), o, now) == 1
    (e,) = BJ.runs(str(state), "game", "AS-X1")
    assert e["run_id"] == "local-p1" and e["source"] == "local" and e["session_id"] == "ses_1"
    assert e["script"]["path"] == "tools/blender/build_x.py"
    assert [r["path"] for r in e["renders"]] == ["tmp/r/X_front.png"]
    assert e["renders"][0]["thumb"] == "thumbs/00.png"
    assert e["where"]["machine"] == "this PC" and e["where"]["commit"] == "abc123"
    assert e["key_lines"] == ["mesh_report: PASS"] and e["timings"]["blender"] == 11.0
    # read again: nothing new
    assert BJ.journal_transcript(str(state), P, "AS-X1", "ses_1", msgs, str(repo), o, now) == 0


def test_a_late_read_says_so(ws):
    repo, state = ws
    now = time.time()
    msgs = [{"parts": [_part("p9", "blender -b -P tools/blender/build_x.py", now - 400,
                             now - 300)]}]
    P = _proj(repo)
    BJ.journal_transcript(str(state), P, "AS-X1", "ses_2", msgs, str(repo),
                          BJ.settings(P.profile), now)
    (e,) = BJ.runs(str(state), "game", "AS-X1")
    assert "after the run" in e["notes"][0]


class _Client:
    def __init__(self, msgs):
        self.msgs, self.calls = msgs, 0

    def transcript(self, sid, directory):
        self.calls += 1
        return self.msgs


def test_observe_is_off_with_the_switch_and_rate_limited(ws, monkeypatch):
    repo, state = ws
    now = time.time()
    msgs = [{"parts": [_part("q1", "blender -b -P tools/blender/build_x.py", now - 5, now - 4)]}]
    eng = SimpleNamespace(state_dir=str(state))
    t = {"task_id": "AS-X1"}
    monkeypatch.setattr(BJ, "_pending", {})
    monkeypatch.setattr(BJ, "_final", set())
    off = _proj(repo, {"blender": {"journal": False}})
    off.client = _Client(msgs)
    assert BJ.observe(eng, off, t, "s1") == 0 and off.client.calls == 0
    local_off = _proj(repo, {"blender": {"journal": True, "journal_local": False}})
    local_off.client = _Client(msgs)
    assert BJ.observe(eng, local_off, t, "s1") == 0 and local_off.client.calls == 0
    P = _proj(repo)
    P.client = _Client(msgs)
    assert BJ.observe(eng, P, t, "s1") == 1
    assert BJ.observe(eng, P, t, "s1") == 0 and P.client.calls == 1      # within 20 s
    assert BJ.observe(eng, P, t, "s1", final=True) == 0 and P.client.calls == 2
    assert BJ.observe(eng, P, t, "s1", final=True) == 0 and P.client.calls == 2   # once


def test_settings_lists_are_lists():
    o = BJ.settings(SimpleNamespace(raw={"blender": {"journal_tools": ["a.py", 3]}}))
    assert o["journal_tools"] == ["a.py", "3"]
    assert BJ.settings(SimpleNamespace(raw={"blender": {"journal_tools": "x"}}))[
        "journal_tools"] == ["selftest_all.py"]


def test_a_powershell_search_naming_blender_is_not_a_run():
    assert not BJ.is_local_blender(r"Select-String -Path docs\ENVIRONMENT.md -Pattern 'blender' "
                                   "-SimpleMatch | Select-Object -First 12", [])


def _guard(repo, name, body, exit_line="EXIT=0", end="2026-11-01 10:00:30"):
    d = repo / "tmp" / "guard" / "cmd"
    d.mkdir(parents=True, exist_ok=True)
    text = ("CMD: blender ...\nCWD: x\nSHELL: none\nSTART: 2026-11-01 10:00:00\nSTATUS RUNNING\n"
            "----\n" + body + "\n")
    if exit_line:
        text += f"----\n{exit_line}\nEND: {end}\n"
    (d / name).write_text(text)
    return f"LOG tmp/guard/cmd/{name}\nBG started (runner pid 1). Result: ..."


def test_a_background_guard_run_is_read_from_its_log_once_it_ended(ws):
    repo, state = ws
    now = time.time()
    full = str(repo / "tools" / "blender" / "build_x.py").replace("/", "\\")
    cmd = (rf'py -3 tools\guard\run.py --bg --name bx --shell none -- "{B}" -b --factory-startup '
           rf'-P "{full}" -- --out tmp/r')
    out = _guard(repo, "a_bx.log", "Blender 4.5\nmesh_report: FAIL budget", exit_line="")
    msgs = [{"parts": [_part("g1", cmd, now - 5, now - 4.6, out=out)]}]
    P = _proj(repo)
    o = BJ.settings(P.profile)
    assert BJ.journal_transcript(str(state), P, "AS-X1", "s", msgs, str(repo), o, now) == 0
    _guard(repo, "a_bx.log", "Blender 4.5\nmesh_report: FAIL budget", exit_line="EXIT=1")
    assert BJ.journal_transcript(str(state), P, "AS-X1", "s", msgs, str(repo), o, now) == 1
    (e,) = BJ.runs(str(state), "game", "AS-X1")
    assert e["exit"] == 1 and e["seconds"] == 30.0
    assert e["key_lines"] == ["mesh_report: FAIL budget"] and "Blender 4.5" in e["log_tail"]
    assert e["script"]["path"] == "tools/blender/build_x.py"          # made worktree-relative
    assert "guard log tmp/guard/cmd/a_bx.log" in e["notes"][0]


def test_guard_log_parsing():
    assert BJ.guard_log(".", "no log here") is None
    assert BJ.guard_log(".", "LOG ../../etc/x.log") is None

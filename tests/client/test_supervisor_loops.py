"""One upgrade supervisor per coordinator state (v0.181.255). 2026-11-23: every "Restart
client" left the old client's `supervisor --loop` running, because a restart keeps the
components for the new client to adopt and nothing could find a supervisor to adopt. So the
new client started another one, and five loops ran for one coordinator."""
import copy
import os
import subprocess
import sys

import pytest

from foundry_client import config, secrets, suploops
from foundry_client.manager import Manager
from foundry_coordinator import supervisor as S


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_CLIENT_CONFIG", str(tmp_path / "client.toml"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(secrets, "_kr", lambda: None)
    c = copy.deepcopy(config.DEFAULTS)
    for d in ("game", "profiles", "deploy/.git", "state"):
        (tmp_path / d).mkdir(parents=True)
    c["client"]["state_dir"] = str(tmp_path / "state")
    c["workspace"].update(game=str(tmp_path / "game"), profiles=str(tmp_path / "profiles"),
                          deploy=str(tmp_path / "deploy"))
    for n in c["components"]:
        c["components"][n]["enabled"] = n == "supervisor"
    return c


class FakeProc:
    def __init__(self, argv, pid=5000):
        self.argv, self.pid, self.code = argv, pid, None

    def poll(self):
        return self.code


@pytest.fixture()
def other_process():
    """A live process that is not this one (a loop of an earlier client)."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield p
    p.kill()
    p.wait(10)


def test_replay_a_restarted_client_adopts_the_running_loop_instead_of_starting_one(cfg):
    S.write_pid_file(os.path.join(cfg["client"]["state_dir"], S.LOOP_FILE))  # this process
    launched = []
    m = Manager(cfg, popen=lambda argv, **kw: launched.append(argv) or FakeProc(argv))
    m.start("supervisor")
    st = m.status("supervisor")
    assert launched == [] and st["state"] == "running"
    assert "outside the client" in st["detail"] and f"loop pid {os.getpid()}" in st["detail"]
    assert os.getpid() in m._component_pids()                 # the resource watch spares it


def test_with_no_loop_running_the_client_starts_one(cfg):
    launched = []
    m = Manager(cfg, popen=lambda argv, **kw: launched.append(argv) or FakeProc(argv))
    m.start("supervisor")
    assert len(launched) == 1 and "--loop" in launched[0] and "supervisor" in launched[0]
    assert m.status("supervisor")["state"] == "starting"


def test_a_loop_file_of_a_dead_process_is_not_adopted(cfg, other_process):
    path = os.path.join(cfg["client"]["state_dir"], S.LOOP_FILE)
    S.write_pid_file(path, pid=other_process.pid)
    other_process.kill()
    other_process.wait(10)
    assert S.loop_pid(cfg["client"]["state_dir"]) is None
    launched = []
    m = Manager(cfg, popen=lambda argv, **kw: launched.append(argv) or FakeProc(argv))
    m.start("supervisor")
    assert len(launched) == 1


def test_a_second_loop_for_the_same_state_exits_and_the_first_keeps_its_file(tmp_path,
                                                                             other_process):
    path = tmp_path / S.LOOP_FILE
    S.write_pid_file(str(path), pid=other_process.pid)
    with pytest.raises(S.LockHeld, match=f"pid {other_process.pid}"):
        with S._LoopFile(str(tmp_path)):
            pass
    assert S.read_pid_file(str(path))["pid"] == other_process.pid
    other_process.kill()
    other_process.wait(10)
    with S._LoopFile(str(tmp_path)):                             # its owner is gone: taken over
        assert S.loop_pid(str(tmp_path)) == os.getpid()
    assert not path.exists()                                     # removed when the loop ends


def test_main_with_loop_exits_at_once_when_another_loop_runs(tmp_path, other_process, capsys):
    S.write_pid_file(str(tmp_path / S.LOOP_FILE), pid=other_process.pid)
    rc = S.main(["--state", str(tmp_path), "--repo", str(tmp_path), "--coordinator-cmd", "x",
                 "--loop"])
    assert rc == 0 and "not starting a second one" in capsys.readouterr().err


def test_the_loop_waits_out_a_held_upgrade_lock_instead_of_dying(tmp_path, monkeypatch):
    sup = S.Supervisor.__new__(S.Supervisor)
    calls = []

    def run_once():
        calls.append(1)
        if len(calls) == 1:
            raise S.LockHeld("another supervisor (pid 9) holds supervisor.lock")
        return []
    sup.run_once = run_once
    sup.loop(0, sleep=lambda s: None, rounds=3)
    assert len(calls) == 3


class PS:
    """psutil.process_iter stand-in."""

    def __init__(self, rows):
        self.rows = rows

    def process_iter(self, attrs):
        for r in self.rows:
            yield type("P", (), {"info": r})()


def loop_cmd(state, exe="C:/venv/Scripts/python.exe"):
    return [exe, "-m", "foundry_client", "supervisor", "--state", state, "--repo", "C:/d",
            "--coordinator-cmd", "x", "--loop"]


def rows(state):
    other = "C:/elsewhere/state"
    return [  # the five pairs of 2026-11-23: a venv launcher and its python child each
        {"pid": 43568, "ppid": 1116, "create_time": 1.0, "cmdline": loop_cmd(state)},
        {"pid": 45488, "ppid": 43568, "create_time": 1.1, "cmdline": loop_cmd(state, "py.exe")},
        {"pid": 21536, "ppid": 24828, "create_time": 2.0, "cmdline": loop_cmd(state)},
        {"pid": 44568, "ppid": 21536, "create_time": 2.1, "cmdline": loop_cmd(state, "py.exe")},
        {"pid": 32792, "ppid": 20964, "create_time": 3.0, "cmdline": loop_cmd(state)},
        {"pid": 49500, "ppid": 32792, "create_time": 3.1, "cmdline": loop_cmd(state, "py.exe")},
        {"pid": 48824, "ppid": 39716, "create_time": 4.0, "cmdline": loop_cmd(state)},
        {"pid": 19012, "ppid": 48824, "create_time": 4.1, "cmdline": loop_cmd(state, "py.exe")},
        {"pid": 46312, "ppid": 7708, "create_time": 5.0, "cmdline": loop_cmd(state)},
        {"pid": 21632, "ppid": 46312, "create_time": 5.1, "cmdline": loop_cmd(state, "py.exe")},
        # not loops of this state: another state folder, a one-shot run, the coordinator
        {"pid": 600, "ppid": 1, "create_time": 6.0, "cmdline": loop_cmd(other)},
        {"pid": 601, "ppid": 1, "create_time": 6.0,
         "cmdline": ["py.exe", "-m", "foundry_client", "supervisor", "--state", state, "--once"]},
        {"pid": 29148, "ppid": 7708, "create_time": 6.0,
         "cmdline": ["py.exe", "-m", "foundry_client", "coordinator", "--state", state, "run"]},
    ]


def test_replay_the_five_loops_are_found_as_five_and_only_the_clients_own_is_kept(tmp_path):
    state = str(tmp_path)
    loops = suploops.find(state, PS(rows(state)))
    assert [lp["pid"] for lp in loops] == [43568, 21536, 32792, 48824, 46312]
    assert loops[0]["pids"] == {43568, 45488}
    killed = []
    res = suploops.sweep(state, {46312}, killed.append, ps=PS(rows(state)))
    assert killed == [43568, 21536, 32792, 48824] == res["stopped"]
    assert res["kept"] == [46312] and res["busy"] == []
    # adopted after a restart: the client knows only the python child from the loop file
    killed.clear()
    suploops.sweep(state, {21632}, killed.append, ps=PS(rows(state)))
    assert 46312 not in killed and len(killed) == 4


def test_a_loop_in_the_middle_of_an_upgrade_is_left_alone(tmp_path, monkeypatch):
    state = str(tmp_path)
    monkeypatch.setattr(S, "lock_pid", lambda d: 49500)            # 32792's child holds the lock
    killed = []
    res = suploops.sweep(state, {46312}, killed.append, ps=PS(rows(state)))
    assert 32792 not in killed and res["busy"] == [32792] and len(killed) == 3


def test_frozen_bundle_and_module_command_lines_count_as_loops(tmp_path):
    state = str(tmp_path)
    assert suploops.is_loop(["C:/FC/foundry-client.exe", "supervisor", "--state", state,
                             "--loop"], state)
    assert suploops.is_loop(["py", "-m", "foundry_coordinator.supervisor", "--state",
                             state + os.sep, "--loop"], state)
    assert not suploops.is_loop(["py", "-m", "pytest", "supervisor", "--state", state, "--loop"],
                                state)


def test_the_client_sweeps_only_while_its_own_supervisor_runs(cfg, monkeypatch):
    import foundry_client.manager as M
    state = cfg["client"]["state_dir"]
    monkeypatch.setattr(S, "lock_pid", lambda d: None)
    killed = []
    monkeypatch.setattr(M.Manager, "_kill_pid", staticmethod(lambda pid, p, tree=True:
                                                             killed.append((pid, tree))))
    m = Manager(cfg, popen=lambda argv, **kw: FakeProc(argv, pid=46312))
    assert m.sweep_supervisors(force=True, ps=PS(rows(state))) == {}    # not running: nothing
    m.start("supervisor")
    m.procs["supervisor"].state = "running"
    res = m.sweep_supervisors(force=True, ps=PS(rows(state)))
    assert res["stopped"] == [43568, 21536, 32792, 48824]
    assert killed == [(43568, True), (21536, True), (32792, True), (48824, True)]
    texts = [e["text"] for e in m.events if e["component"] == "supervisor"]
    assert sum("stopped an extra upgrade supervisor" in t for t in texts) == 4
    assert m.sweep_supervisors(ps=PS(rows(state))) == {}             # once a minute at most

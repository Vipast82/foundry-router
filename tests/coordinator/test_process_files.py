"""The coordinator's process files for the upgrade supervisor: pid file,
per-tick heartbeat, and a graceful stop honored only at a tick boundary and
only for a real supervisor request (a known upgrade id, or a configured
supervisor); garbage and stale stop files are ignored."""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from foundry_coordinator import cli, supervisor as SV


class _Engine:
    def __init__(self):
        self.projects = {}
        self.after_tick = []
        self.ticks = 0

    def run(self, max_ticks):
        while self.ticks < max_ticks:
            self.ticks += 1
            for h in self.after_tick:
                try:
                    h()
                except Exception:                                 # noqa: BLE001
                    pass                     # like Coordinator.run: hooks can't kill the loop


def _supervised(eng):
    """A lead manager configured with supervisor = "supervisor"."""
    eng.projects = {"game": SimpleNamespace(id="game")}
    eng.lead = SimpleNamespace(enabled=lambda p: True,
                               settings=lambda p: {"supervisor": "supervisor"},
                               readiness=lambda p: [])
    return eng


def _pid_of(path) -> int:
    rp = getattr(SV, "read_pid_file", None)
    if callable(rp):
        return int(rp(str(path))["pid"])
    return int(open(path).read())


def test_pid_heartbeat_and_graceful_stop(tmp_path):
    eng = _Engine()
    life = cli._ProcessFiles(str(tmp_path), eng)
    assert _pid_of(tmp_path / SV.PID_FILE) == os.getpid()
    life.tick()
    hb = json.loads((tmp_path / SV.HEARTBEAT_FILE).read_text())
    assert hb["pid"] == os.getpid() and hb["ok"] is True and hb["lead_ready"] is None
    from foundry_router import __version__ as release
    assert hb["release"] == release
    # a supervisor's stop request ends the loop at the next tick boundary
    # (BaseException passes through the engine's Exception-only hook guard)
    _supervised(eng)
    eng.after_tick.append(life.tick)
    (tmp_path / SV.STOP_FILE).write_text(json.dumps({
        "upgrade_id": "up-1", "supervisor_pid": os.getpid(), "pid": os.getpid(),
        "ts": time.time()}))
    with pytest.raises(cli._StopRequested):
        eng.run(max_ticks=5)
    assert eng.ticks == 1 and not (tmp_path / SV.STOP_FILE).exists()
    life.close()
    assert not (tmp_path / SV.PID_FILE).exists()


def test_heartbeat_reports_lead_readiness(tmp_path):
    eng = _Engine()
    P = SimpleNamespace(id="game")
    eng.projects = {"game": P}
    eng.lead = SimpleNamespace(
        enabled=lambda p: True,
        readiness=lambda p: [{"name": "route_chain", "ok": False, "blocking": True,
                              "detail": "served by a local model"}])
    life = cli._ProcessFiles(str(tmp_path), eng)
    life.tick()
    hb = json.loads((tmp_path / SV.HEARTBEAT_FILE).read_text())
    assert hb["lead_ready"] is False and "route_chain" in hb["lead_detail"]
    life.close()


def test_stop_file_ignored_without_a_supervisor_or_known_upgrade(tmp_path):
    """Lead off (no supervisor configured): a garbage or unknown stop file is
    removed and ignored; the loop keeps running."""
    eng = _Engine()
    life = cli._ProcessFiles(str(tmp_path), eng)
    eng.after_tick.append(life.tick)
    for body in ("stop", json.dumps({"upgrade_id": "up-unknown"}), str(os.getpid()), ""):
        (tmp_path / SV.STOP_FILE).write_text(body)
        eng.ticks = 0
        eng.run(max_ticks=2)
        assert eng.ticks == 2 and not (tmp_path / SV.STOP_FILE).exists(), body
    assert len(life.ignored) == 4
    # garbage is ignored even with a supervisor configured
    _supervised(eng)
    (tmp_path / SV.STOP_FILE).write_text("please stop")
    eng.ticks = 0
    eng.run(max_ticks=2)
    assert eng.ticks == 2
    life.close()


def test_stale_stop_file_is_dropped(tmp_path):
    eng = _supervised(_Engine())
    stop = tmp_path / SV.STOP_FILE
    stop.write_text(json.dumps({"upgrade_id": "up-1"}))
    old = time.time() - 700
    os.utime(stop, (old, old))
    life = cli._ProcessFiles(str(tmp_path), eng)
    assert not stop.exists() and life.ignored == ["stale"]
    stop.write_text(json.dumps({"upgrade_id": "up-1"}))
    os.utime(stop, (old, old))
    life.tick()                                   # stale at the tick: ignored too
    assert not stop.exists() and life.ignored == ["stale", "stale"]
    life.close()


def test_heartbeat_ok_reflects_tick_errors(tmp_path):
    from tests.coordinator.helpers import coordinator, make_repo, write_profile, write_ok
    from tests.coordinator.test_engine import T1
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": write_ok("src/a.txt")})
    life = cli._ProcessFiles(str(tmp_path / "state"), c)
    life.tick()
    assert json.loads((tmp_path / "state" / SV.HEARTBEAT_FILE).read_text())["ok"] is True
    c.store.event("game", "T1", "error", detail={"error": "boom"})
    life.tick()
    hb = json.loads((tmp_path / "state" / SV.HEARTBEAT_FILE).read_text())
    assert hb["ok"] is False and "error" in hb["errors"][0]
    life.tick()                                   # nothing new: ok again
    assert json.loads((tmp_path / "state" / SV.HEARTBEAT_FILE).read_text())["ok"] is True
    life.close()


def test_real_coordinator_run_stops_at_a_tick_boundary(tmp_path):
    from tests.coordinator.helpers import coordinator, make_repo, write_profile, write_ok
    from tests.coordinator.test_engine import T1
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": write_ok("src/a.txt")})
    uid = SV.request_upgrade(c.store, case_id="rc-1", project_id="game", task_id="T1",
                             candidate_sha="a" * 40, manifest={}, candidate_ref="lead/rc-1-c1")
    life = cli._ProcessFiles(str(tmp_path / "state"), c)
    c.after_tick.append(life.tick)
    (tmp_path / "state" / SV.STOP_FILE).write_text(json.dumps({"upgrade_id": uid}))
    with pytest.raises(cli._StopRequested):
        c.run(poll_seconds=0, max_ticks=10)
    life.close()


def test_garbage_stop_file_does_not_stop_a_real_coordinator(tmp_path):
    from tests.coordinator.helpers import coordinator, make_repo, write_profile, write_ok
    from tests.coordinator.test_engine import T1
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": write_ok("src/a.txt")})
    life = cli._ProcessFiles(str(tmp_path / "state"), c)
    c.after_tick.append(life.tick)
    (tmp_path / "state" / SV.STOP_FILE).write_text("stop")
    c.run(poll_seconds=0, max_ticks=3)                     # runs all ticks
    assert not (tmp_path / "state" / SV.STOP_FILE).exists()
    life.close()


def test_stop_file_of_a_dead_supervisor_or_for_another_pid_is_ignored(tmp_path):
    eng = _supervised(_Engine())
    stop = tmp_path / SV.STOP_FILE
    dead = 999999
    while SV.pid_alive(dead):
        dead += 1
    stop.write_text(json.dumps({"upgrade_id": "up-1", "supervisor_pid": dead,
                                "pid": os.getpid(), "ts": time.time()}))
    life = cli._ProcessFiles(str(tmp_path), eng)          # dropped at startup
    assert not stop.exists() and life.ignored == ["dead supervisor"]
    eng.after_tick.append(life.tick)
    stop.write_text(json.dumps({"upgrade_id": "up-1", "supervisor_pid": os.getpid(),
                                "pid": os.getpid() + 1, "ts": time.time()}))
    eng.run(max_ticks=2)                                    # another process's request
    assert eng.ticks == 2 and not stop.exists()
    life.close()

"""The Blender pool (docs/plans/BLENDER_POOL_PLAN.md P1): workers in order, several jobs per
slot, this PC as the last worker, the coordinator's capacity = the pool's jobs."""

import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from foundry_coordinator import slot_blender as SB
from foundry_coordinator.tracks import resources as RS


class _Prof:
    def __init__(self, workspace, raw, pid="animaluprising", slot=""):
        self.workspace, self.raw, self.id = str(workspace), raw, pid
        if slot:
            self.__dict__["slot"] = slot


def _raw(pool, **bl):
    return {"blender": {"pool": pool, **bl},
            "slots": {"slot-1": {"container": "studio-slot", "host": "studio@192.168.0.115"},
                      "slot-2": {"container": "studio-slot", "host": "studio@192.168.0.116"},
                      "slot-9": {"container": "x", "host": "studio@h9", "enabled": False},
                      "poc": {"container": "studio-slot-poc"}},
            "role_rules": {}}


def test_pool_reads_workers_jobs_and_puts_this_pc_last():
    p = SB.pool(_Prof("w", _raw(["local:1", "slot-1:2", "slot-2", {"id": "slot-1", "jobs": 5},
                                 "this-pc:3", "slot-3:99"])))
    assert p == [{"id": "slot-1", "jobs": 2}, {"id": "slot-2", "jobs": 1},
                 {"id": "slot-3", "jobs": 8}, {"id": "local", "jobs": 1}]
    assert SB.pool(_Prof("w", _raw([]))) == [] and SB.pool(_Prof("w", {})) == []


def test_a_track_bound_to_a_slot_gets_its_slot_first():
    p = SB.pool(_Prof("w", _raw(["slot-1:2", "slot-2:2", "local:1"]), slot="slot-2"))
    assert [w["id"] for w in p] == ["slot-2", "slot-1", "local"]
    p = SB.pool(_Prof("w", _raw(["slot-1:2"]), slot="slot-7"))
    assert p[0] == {"id": "slot-7", "jobs": 1}


def test_capacity_and_kilo_rules_follow_the_pool():
    pooled = _Prof("w", _raw(["slot-1:2", "slot-2:2", "local:1"]))
    assert SB.blender_capacity(pooled) == 5 and SB.pooled(pooled)
    assert {r["pattern"] for r in SB.deny_rules(pooled)} == set(SB.LOCAL_BLENDER)
    plain = _Prof("w", {"blender": {}})
    assert SB.blender_capacity(plain) == 1 and not SB.pooled(plain) and SB.deny_rules(plain) == []


def test_prepare_writes_every_worker_for_the_drop_in(tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_BLENDER_DIR", str(tmp_path / "fb"))
    ws = tmp_path / "AnimalUprising"
    ws.mkdir()
    prof = _Prof(ws, _raw(["slot-1:2", "slot-9:2", "poc:1", "slot-2:2", "local:1"],
                          pool_wait_seconds=120, local_exe="D:/B/blender.exe", journal=True))
    b = SB.prepare(prof, str(tmp_path / "state"), "AS-X1", "run-1")
    assert b["pool"] == ["slot-1", "slot-2", "local"]
    assert b["slot"] == "pool (slot-1 x2, slot-2 x2, this PC x1)"
    assert any("slot-9" in n for n in b["notes"]) and any("poc" in n for n in b["notes"])
    cfg = json.load(open(b["config"], encoding="utf-8"))
    assert [w["id"] for w in cfg["pool"]] == ["slot-1", "slot-2", "local"]
    assert cfg["pool"][1]["host"] == "studio@192.168.0.116" and cfg["pool"][1]["jobs"] == 2
    assert cfg["pool"][2]["exe"] == "D:/B/blender.exe" and cfg["pool_wait_seconds"] == 120
    assert cfg["task"] == "AS-X1" and cfg["journal"] is True
    assert os.path.isfile(b["command"])
    bad = SB.prepare(_Prof(ws, _raw(["slot-9:1"])), str(tmp_path / "state"))
    assert "no usable worker" in bad["error"]


def test_a_slot_job_has_its_own_lock_checkout_marker_and_cpu_share():
    s1 = SB.remote_script("wt", 600, 0, ["-b", "--version"])
    assert "for n in 1; do" in s1 and "T=$(( T / 1 ))" in s1
    s2 = SB.remote_script("wt", 600, 0, ["-b", "--version"], job=2, jobs=3)
    assert "for n in 2 1 3; do" in s2 and "J=2" in s2 and "T=$(( T / 3 ))" in s2
    assert 'W="$HOME/work/blender$SFX"' in s2 and 'M="$HOME/locks/blender$SFX.marker"' in s2
    assert 'exec 8>"$HOME/locks/library.lock"' in s2 and "flock -s -w 600 8" in s2
    assert s2.index("lockf") < s2.index("library.lock") < s2.index("git -C")
    # the bash itself: job 1 keeps the old names, job N gets -N
    import shutil
    import subprocess
    bash = shutil.which("bash")
    if bash:
        lf = s2.split("\n")[2]
        out = subprocess.run([bash, "-c", lf + '\nlockf 1; lockf 2'], capture_output=True,
                             text=True, env={**os.environ, "HOME": "/h"}).stdout.split()
        assert out == ["/h/locks/blender.lock", "/h/locks/blender-2.lock"]


def test_free_job_script_tries_each_job_lock():
    s = SB.free_job_script(3)
    assert "seq 1 3" in s and 'flock -n "$f" true' in s and "blender-$n.lock" in s
    assert s.rstrip().endswith("echo 0")


def test_the_library_copy_takes_the_library_lock_exclusive(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.txt").write_text("a")
    seen = {}
    monkeypatch.setattr(SB, "_slot_out", lambda slot, script, timeout=120: (0, b""))

    class P:
        def __init__(self, argv, **kw):
            seen["argv"] = argv
            import io
            self.stdin = io.BytesIO()
            self.stdin.close = lambda: None

        def wait(self):
            return 0

        def kill(self):
            pass
    monkeypatch.setattr(SB.subprocess, "Popen", P)
    ok, _ = SB.lib_sync({"host": "h", "container": "c"}, str(lib))
    script = seen["argv"][-1]
    assert ok and 'exec 9>"$HOME/locks/library.lock"' in script and "flock -x -w" in script


def test_choose_worker_prefers_the_last_worker_then_order_then_waits(tmp_path):
    cfg_path = str(tmp_path / "x.json")
    cfg = {"pool": [{"id": "slot-1", "jobs": 2}, {"id": "slot-2", "jobs": 2},
                    {"id": "local", "jobs": 1}]}
    free = {"slot-1": None, "slot-2": (2, "lock")}
    got = SB.choose_worker(cfg, cfg_path, 0, slot_probe=lambda w: free[w["id"]],
                           local_probe=lambda n: None)
    assert got[0] == "slot" and got[1]["id"] == "slot-2" and got[2] == (2, "lock")
    assert open(cfg_path + ".last").read() == "slot-2"
    free["slot-1"] = (1, "lock")             # slot-1 frees up, but slot-2 was used last
    got = SB.choose_worker(cfg, cfg_path, 0, slot_probe=lambda w: free[w["id"]],
                           local_probe=lambda n: None)
    assert got[1]["id"] == "slot-2"
    free.update({"slot-1": None, "slot-2": None})  # both slots full: this PC
    got = SB.choose_worker(cfg, cfg_path, 0, slot_probe=lambda w: free[w["id"]],
                           local_probe=lambda n: (1, "lock"))
    assert got == ("local", cfg["pool"][2], (1, "lock"))
    # everything busy: wait, then give up
    t = [0.0]
    naps = []
    got = SB.choose_worker(cfg, cfg_path, 12, clock=lambda: t[0],
                           sleep=lambda s: (naps.append(s), t.__setitem__(0, t[0] + s)),
                           slot_probe=lambda w: None, local_probe=lambda n: None)
    assert got is None and len(naps) == 3


def test_slot_jobs_are_reserved_on_this_pc_so_runs_started_together_spread(tmp_path,
                                                                           monkeypatch):
    monkeypatch.setenv("FOUNDRY_BLENDER_DIR", str(tmp_path / "fb"))
    cfg = {"pool": [{"id": "slot-1", "jobs": 2}, {"id": "slot-2", "jobs": 2},
                    {"id": "local", "jobs": 1}]}
    held = [SB.choose_worker(cfg, str(tmp_path / f"c{i}.json"), 0) for i in range(6)]
    assert [(h[1]["id"], h[2][0]) for h in held[:5]] == [
        ("slot-1", 1), ("slot-1", 2), ("slot-2", 1), ("slot-2", 2), ("local", 1)]
    assert held[5] is None                                  # all five jobs reserved
    held[2][2][1].close()                                   # slot-2 job 1 ends
    again = SB.choose_worker(cfg, str(tmp_path / "c9.json"), 0)
    assert (again[1]["id"], again[2][0]) == ("slot-2", 1)
    for h in held[:5] + [again]:
        h[2][1].close()


def test_local_jobs_are_file_locks(tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_BLENDER_DIR", str(tmp_path / "fb"))
    a = SB.local_try(2)
    b = SB.local_try(2)
    assert a[0] == 1 and b[0] == 2
    assert SB.local_try(2) is None
    a[1].close()
    c = SB.local_try(2)
    assert c[0] == 1
    b[1].close()
    c[1].close()


def test_this_pc_runs_blender_itself_with_a_journal(tmp_path, monkeypatch):
    from foundry_coordinator import build_journal as BJ
    from foundry_coordinator.store import Store
    state = tmp_path / "state"
    state.mkdir()
    Store(str(state / "coordinator.sqlite"))
    monkeypatch.setenv("FOUNDRY_COORD_STATE", str(state))
    repo = tmp_path / "wt"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "b.py").write_text("print('mesh_report: PASS tris=10')\n")
    cfg = {"project": "game", "task": "AS-X1", "journal": True}
    a = SimpleNamespace(wait=5)
    rc = SB._run_local(a, cfg, str(repo), ["tools/b.py"], {"id": "local", "exe": sys.executable},
                       (1, None))
    assert rc == 0
    (e,) = BJ.runs(str(state), "game", "AS-X1")
    assert e["source"] == "local" and e["where"]["machine"] == "this PC"
    assert e["key_lines"] == ["mesh_report: PASS tris=10"]
    assert SB._run_local(a, cfg, str(repo), [], {"id": "local", "exe": "C:/nope/blender.exe"},
                         (1, None)) == 2


# ----------------------------------------------------------------------------- capacity units

class _Store:
    def __init__(self, locks=()):
        self.rows = list(locks)

    def locks(self):
        return [{"resource": r, "project_id": p, "task_id": t} for r, p, t in self.rows]

    def holds(self, pid, tid):
        return [r for r, p, t in self.rows if (p, t) == (pid, tid)]


def test_blender_units_follow_the_pool():
    P = SimpleNamespace(id="game", profile=_Prof("w", _raw(["slot-1:2", "local:1"])))
    assert RS.capacity(P, "blender") == 3 and RS.capacity(P, "gpu") == 1
    assert RS.unit_names("blender", 3) == ["blender", "blender#2", "blender#3"]
    st = _Store([("blender", "game", "T1")])
    assert RS.pick_unit(st, P, "T1", "blender") == "blender"          # keeps its own unit
    assert RS.pick_unit(st, P, "T2", "blender") == "blender#2"        # first free
    st = _Store([("blender", "game", "T1"), ("blender#2", "game", "T2"),
                 ("blender#3", "game", "T3")])
    assert RS.pick_unit(st, P, "T4", "blender") == "blender"          # all taken: waits on #1
    plain = SimpleNamespace(id="game", profile=_Prof("w", {}))
    assert RS.pick_unit(_Store(), plain, "T1", "blender") == "blender"
    assert RS.base("blender#2") == "blender"


def test_the_planner_runs_as_many_blender_tracks_as_the_pool_has_jobs():
    from tests.coordinator.test_tracks_analysis import (BLENDER_PLAN, INFER_BLEND_PLAN,
                                                        UI_PLAN, plans)
    from foundry_coordinator.tracks import analysis as A
    out = A.analyze(plans(**{"UI-03a": UI_PLAN, "AS-P4a": BLENDER_PLAN,
                             "AS-Z": INFER_BLEND_PLAN}), main_sprint="UI-03a")
    one = {t["name"] for t in A.propose(out, 5)["tracks"]}
    assert not ({"as-p4a", "as-z"} <= one)
    two = {t["name"] for t in A.propose(out, 5, capacity={"blender": 2})["tracks"]}
    assert {"as-p4a", "as-z"} <= two


# ----------------------------------------------------------------------------- P4: Machines

def test_machines_lists_the_pool_with_the_last_day_of_runs():
    from foundry_router import machines as M
    now = 1_000_000.0
    runs = [(now - 100, {"where": {"slot": "slot-1"}, "timings": {"blender": 30}, "exit": 0}),
            (now - 50, {"where": {"slot": "slot-1"}, "timings": {"blender": 90}, "exit": 1}),
            (now - 10, {"where": {"machine": "this PC"}, "timings": {"blender": 5}, "exit": 0}),
            (now - 5, {"where": {"slot": "slot-3"}, "timings": {}, "exit": 0})]

    class DB:
        def query(self, sql, args):
            assert "cc_build_runs" in sql and args[0] == "game" and args[1] == now - 86400
            return [{"ts": ts, "data": json.dumps(e)} for ts, e in runs]
    svc = SimpleNamespace(
        automation=SimpleNamespace(
            projects=lambda: [{"project": "game"}],
            deployed=lambda p: {"doc": {"settings": {"blender": {
                "pool": ["slot-1:2", "slot-2:2", "local:1"]}}}}),
        control=SimpleNamespace(db=DB()))
    (p,) = M.blender_pool(svc, now)
    w = {x["id"]: x for x in p["workers"]}
    assert p["pool"] and list(w) == ["slot-1", "slot-2", "local", "slot-3"]
    assert w["slot-1"]["jobs"] == 2 and w["slot-1"]["runs_24h"] == 2
    assert w["slot-1"]["blender_s"] == 120 and w["slot-1"]["last_exit"] == 1
    assert w["slot-2"]["runs_24h"] == 0 and w["local"]["runs_24h"] == 1
    assert w["slot-3"]["jobs"] is None                       # ran, but not in the pool
    html = open(os.path.join(os.path.dirname(M.__file__), "ui", "static", "index.html"),
                encoding="utf-8").read()
    assert "<h3>Blender pool</h3>" in html and "d.blender_pool" in html


def test_a_pool_typed_on_one_line_still_reads_and_settings_refuse_bad_jobs():
    """2026-11-01: `slot-1:2,Slot-2:22,local:1,` deployed as one entry was read as slot-1 x1."""
    from foundry_automation import schema as S
    raw = _raw(["slot-1:2,Slot-2:2, local:1,"])
    assert SB.pool(_Prof("w", raw)) == [{"id": "slot-1", "jobs": 2}, {"id": "slot-2", "jobs": 2},
                                        {"id": "local", "jobs": 1}]
    f = next(x for x in S.FIELDS if x.key == "blender.pool")
    assert S.check_value(f, ["slot-1:2", "slot-2:2", "local:1"]) is None
    assert S.check_value(f, ["slot-1:2,slot-2:2,local:1"]) is None
    assert "jobs must be 1 to 8" in S.check_value(f, ["slot-1:2,Slot-2:22,local:1,"])
    assert "worker:jobs" in S.check_value(f, ["slot-1=2"])
    assert "listed twice" in S.check_value(f, ["slot-1:2", "SLOT-1:1"])

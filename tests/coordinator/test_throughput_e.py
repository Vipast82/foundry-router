"""Audit 2026-10-28, section E (throughput) and D's merge-gate tooling retry:
the review call and the Studio-free merge gate run off the tick; the review starts
beside Lune once done_check and the gate binding pass (dropped if the revision then
fails); a profile max_agents is not hidden by an older chat value; check processes
die with the coordinator, each run logs to its own file and finished done_check /
binding rows survive a restart; Kilo polling reads status first and the newest
messages only; a write-access Lead call keeps its studio.lock; a merge gate tooling
fault is retried before the track fails."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from foundry_coordinator import check_cache, check_progress, procjob
from foundry_coordinator import states as S
from foundry_coordinator.adapters.base import FAIL, PASS, UNKNOWN, VerificationResult
from foundry_coordinator.adapters.kilo import KiloClient
from foundry_coordinator.engine import Coordinator
from tests.coordinator import test_tracks_merge as TM
from tests.coordinator.test_au_review_evidence import T as AU_T, repo  # noqa: F401
from tests.coordinator.helpers import (FakeFoundry, approve_all, by_task, coordinator,
                                       make_repo, run_until, write_ok, write_profile)

NO_WAIT = "\n[automation]\ninline_check_wait_s = 0\n"


def _solo(tmp_path, foundry, extra=NO_WAIT):
    tasks = [{"id": "T1", "title": "main sprint", "spec": "Write src/a.txt.",
              "scope": ["src/a.txt"]}]
    root, _ = make_repo(tmp_path, "game", tasks)
    prof = write_profile(tmp_path, "game", root, extra=extra, publish="none", review=True)
    c = coordinator(tmp_path, [prof], {"game": by_task({"T1": write_ok("src/a.txt")})},
                    foundry={"game": foundry})
    c.approve("game", "T1", [], "victor")
    return c, c.projects["game"], root


def _state(c):
    return c.store.task("game", "T1")["state"]


def _timed_tick(c) -> float:
    t0 = time.perf_counter()
    c.tick()
    return time.perf_counter() - t0


# ------------------------------------------------------------------ 1. review off the tick

def test_the_review_call_runs_off_the_tick_with_a_note(tmp_path):
    gate = threading.Event()

    def slow_review(packet):
        gate.wait(6)
        return approve_all(packet)

    f = FakeFoundry(reviewer=slow_review)
    c, P, _ = _solo(tmp_path, f)
    assert run_until(c, lambda: _state(c) == S.REVIEWING or f.chats, limit=40)
    for _ in range(3):
        took = _timed_tick(c)
        assert took < 3.0, f"a tick waited {took:.1f} s on the review call"
        if f.chats:
            break
    assert f.chats and _state(c) == S.REVIEWING
    assert "code review in flight" in c.store.task("game", "T1")["next_action"]
    gate.set()
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=30)
    assert len(f.chats) == 1                                  # one paid call, applied once


def test_a_quick_review_is_still_applied_within_the_same_tick(tmp_path):
    f = FakeFoundry()
    c, P, _ = _solo(tmp_path, f, extra="")                    # inline_check_wait_s = 2
    assert run_until(c, lambda: _state(c) == S.REVIEWING, limit=40)
    c.tick()
    assert _state(c) != S.REVIEWING and len(f.chats) == 1


# ------------------------------------------------------------------ 2. review beside Lune

def _beside_lune(c, P, lune_result, lune_gate):
    """A verify whose done_check finishes at once and whose Lune waits on `lune_gate`."""
    P.adapter.EARLY_REVIEW_CHECKS = ("done_check",)

    def verify(spec, phase, revision, base, ws=None):
        tid = spec.task_id
        rows = check_progress.track(P.id, tid, "done_check", lambda: [VerificationResult(
            "au.done_check.TEST", PASS, True, "", "ok", revision)])
        if phase != "verify":
            return rows

        def lune():
            lune_gate.wait(10)
            return [VerificationResult("au.lune", lune_result[0], True, "", "LUNE", revision)]
        return rows + check_progress.track(P.id, tid, "Lune suite", lune)
    P.adapter.verify = verify


def test_the_review_starts_beside_lune_and_is_collected_not_paid_twice(tmp_path):
    f = FakeFoundry()
    c, P, _ = _solo(tmp_path, f)
    lune, result = threading.Event(), [PASS]
    _beside_lune(c, P, result, lune)
    assert run_until(c, lambda: bool(f.chats), limit=60)
    assert _state(c) == S.VERIFYING                          # Lune still runs
    ev = [e for e in c.store.events("game", 200) if e["kind"] == "review_beside_checks"]
    assert ev and ev[0]["detail"]["still_running"] == ["Lune suite"]
    lune.set()
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=30)
    assert len(f.chats) == 1


def test_a_lune_failure_drops_the_review_started_beside_it(tmp_path):
    gate = threading.Event()

    def held(packet):
        gate.wait(10)
        return approve_all(packet)

    f = FakeFoundry(reviewer=held)
    c, P, _ = _solo(tmp_path, f)
    lune, result = threading.Event(), [FAIL]
    _beside_lune(c, P, result, lune)
    assert run_until(c, lambda: bool(f.chats), limit=60)
    head = c.store.task("game", "T1")["head_sha"] or P.ws.head()
    lune.set()
    assert run_until(c, lambda: _state(c) != S.VERIFYING, limit=20)
    gate.set()
    assert _state(c) in (S.CHANGES_REQUIRED, S.IMPLEMENTING)
    [ev] = [e for e in c.store.events("game", 300) if e["kind"] == "early_review_discarded"]
    assert ev["detail"]["in_flight"].startswith("review:game:T1:")
    assert not [k for k in c._early_reviews if k[:2] == ("game", "T1")]
    assert c.store.op(ev["detail"]["in_flight"]) is None
    assert head


def test_review_beside_checks_can_be_turned_off(tmp_path):
    f = FakeFoundry()
    c, P, _ = _solo(tmp_path, f, extra=NO_WAIT + "review_beside_checks = false\n")
    lune, result = threading.Event(), [PASS]
    _beside_lune(c, P, result, lune)
    for _ in range(25):
        c.tick()
        if check_progress.finished_rows("game", "T1").get("done_check"):
            break
    c.tick()
    assert not f.chats
    lune.set()


# ------------------------------------------------------------------ 1b. merge gate off the tick

def test_the_studio_free_merge_gate_runs_in_bg_checks_and_holds_main_sessions(tmp_path):
    from foundry_coordinator.automation import set_live
    c, P, clock, root = TM.env(tmp_path)
    TM.finish_track(c, P, "blender", ["B1"])
    set_live(P, "inline_check_wait_s", 0)                     # ticks never wait from here
    gate = threading.Event()
    real = P.adapter.verify

    def slow(spec, phase, head, base, ws=None):
        gate.wait(8)
        return real(spec, phase, head, base)
    P.adapter.verify = slow
    for _ in range(6):
        took = _timed_tick(c)
        assert took < 4.0, f"a tick waited {took:.1f} s on the merge gate"
        if c.tracks.merger.checks_busy(P):
            break
    row = c.store.track("game", "blender")
    assert row["state"] == "merging" and row["data"]["merge_stage"] == "gate"
    assert c.tracks.merger.checks_busy(P) == "blender"
    assert any(j["key"][2] == "merge" for j in c.bg_checks.running())
    assert "in the background" in row["data"]["merge_wait"]
    # a main sprint does not start writing in the main checkout meanwhile
    c.approve("game", "T1", [], "victor")
    for _ in range(3):
        c.tick()
    t1 = c.store.task("game", "T1")
    assert t1["state"] in (S.QUEUED, S.READY) and t1["session_id"] in (None, "")
    assert "merge gate of track blender" in t1["next_action"]
    gate.set()
    set_live(P, "inline_check_wait_s", 2)                     # the rest at the normal pace
    assert run_until(c, lambda: c.store.track("game", "blender")["state"] == "merged", limit=20)
    assert not c.tracks.merger.checks_busy(P)
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE, limit=40)


# ------------------------------------------------------------------ 7. merge gate tooling retry

def test_unknown_merge_checks_are_retried_with_backoff_then_merge(tmp_path):
    c, P, clock, root = TM.env(tmp_path)
    TM.finish_track(c, P, "blender", ["B1"])
    real, n = P.adapter.verify, [0]

    def flaky(spec, phase, head, base, ws=None):
        n[0] += 1
        if n[0] <= 2:
            return [VerificationResult("project-check", UNKNOWN, True, "", "tool crashed", head)]
        return real(spec, phase, head, base)
    P.adapter.verify = flaky
    assert run_until(c, lambda: c.store.track("game", "blender")["data"].get("gate_retries")
                     == 1, limit=10)
    row = c.store.track("game", "blender")
    assert row["state"] == "merging" and row["data"]["merge_stage"] == "gate"
    c.tick()
    assert n[0] == 1                                          # waits out the backoff
    clock.t += 61
    assert run_until(c, lambda: c.store.track("game", "blender")["data"].get("gate_retries")
                     == 2, limit=5)
    clock.t += 121
    assert run_until(c, lambda: c.store.track("game", "blender")["state"] == "merged", limit=5)
    retries = [e for e in c.store.events("game", 300) if e["kind"] == "track_gate_retry"]
    assert [e["detail"]["in_s"] for e in reversed(retries)] == [60, 120]


def test_a_merge_gate_that_stays_unknown_fails_after_two_retries(tmp_path):
    c, P, clock, root = TM.env(tmp_path)
    TM.finish_track(c, P, "blender", ["B1"])
    P.adapter.verify = lambda spec, phase, head, base, ws=None: [VerificationResult(
        "project-check", UNKNOWN, True, "", "tool crashed", head)]
    for _ in range(6):
        c.tick()
        clock.t += 300
    row = c.store.track("game", "blender")
    assert row["state"] == "failed" and "after 2 retries" in row["data"]["failed"]["why"]


def test_a_runner_that_ended_without_a_verdict_clears_its_lock_and_retries(tmp_path):
    c, P, clock, root = TM.env(tmp_path)
    c.tracks.create(P, "blender", ["B1"])
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    head = P.ws.head()
    lock = os.path.join(root, ".plan", "tmp", "gate", "runner.lock")
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    with open(lock, "w", encoding="utf-8") as fh:
        json.dump({"pid": dead.pid, "sprint": "B1", "mode": "sprint"}, fh)
    c.store.update_track("game", "blender", state="merging")
    c.store.merge_track_data("game", "blender", merge_stage="gate_running", merge_head=head,
                             gate={"head": head, "sprint": "B1", "pid": dead.pid,
                                   "started": clock.t, "run_id": "r1", "studio": True})
    c.tracks.merger.step(P, c.store.track("game", "blender"))
    row = c.store.track("game", "blender")
    assert row["state"] == "merging", row["data"].get("failed")
    assert row["data"]["merge_stage"] == "gate" and row["data"]["gate_retries"] == 1
    assert not os.path.exists(lock)
    [ev] = [e for e in c.store.events("game", 50) if e["kind"] == "track_gate_retry"]
    assert ev["detail"]["runner_lock_cleared"] is True


# ------------------------------------------------------------------ 3. max_agents from the profile

def test_a_profile_max_agents_wins_over_an_older_chat_value(tmp_path):
    from tests.coordinator.test_tracks_manager import env
    c, P, _ = env(tmp_path)                                  # profile max_agents = 1
    c.tracks.set_max_agents(P, 1, "victor")                  # a chat value, profile says 1
    prof = tmp_path / "profiles" / "game" / "project.toml"
    prof.write_text(prof.read_text(encoding="utf-8").replace("max_agents = 1",
                                                             "max_agents = 2"),
                    encoding="utf-8")
    c2 = coordinator(tmp_path, [prof], {"game": by_task({})}, store_path=c.store.path,
                     max_writers=None)
    assert c2.max_writers == 2                               # the edited profile counts
    assert c2.store.meta("max_agents:game") in ("", None)
    # a chat value set while the profile says 2 still survives a restart
    c2.tracks.set_max_agents(c2.projects["game"], 3, "victor")
    c3 = coordinator(tmp_path, [prof], {"game": by_task({})}, store_path=c.store.path,
                     max_writers=None)
    assert c3.max_writers == 3


def test_max_agents_is_documented_for_studio_free_tracks():
    from foundry_automation.schema import AUTOMATION_DEFAULTS, _AUTO_DESC
    assert AUTOMATION_DEFAULTS["max_agents"] == 1
    assert "Studio-free" in _AUTO_DESC["max_agents"]


# ------------------------------------------------------------------ 4. restart safety

@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_a_check_process_dies_with_its_job_when_the_coordinator_goes(tmp_path):
    # a child "coordinator" starts a long check through procjob and is then killed: the
    # check (and its own child) must not outlive it
    marker = tmp_path / "pids.txt"
    check = tmp_path / "check.py"
    check.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n", encoding="utf-8")
    script = tmp_path / "coord.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {os.getcwd()!r})\n"
        "from foundry_coordinator import procjob\n"
        f"procjob.run([sys.executable, {str(check)!r}], timeout=120)\n", encoding="utf-8")
    coord = subprocess.Popen([sys.executable, str(script)])
    for _ in range(200):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.1)
    grandchild = int(marker.read_text())
    import psutil
    assert psutil.pid_exists(grandchild)
    coord.kill()
    coord.wait()
    for _ in range(50):
        if not psutil.pid_exists(grandchild):
            break
        time.sleep(0.1)
    assert not psutil.pid_exists(grandchild)


def test_procjob_run_matches_subprocess_run_and_kills_on_timeout(tmp_path):
    r = procjob.run([sys.executable, "-c", "print('hi')"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "hi"
    t0 = time.perf_counter()
    with pytest.raises(subprocess.TimeoutExpired):
        procjob.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert time.perf_counter() - t0 < 15


def test_reap_kills_a_run_left_by_a_dead_coordinator(tmp_path):
    import psutil
    leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    d = tmp_path / "procs"
    d.mkdir()
    (d / f"{leftover.pid}-x.json").write_text(json.dumps({
        "pid": leftover.pid, "create_time": psutil.Process(leftover.pid).create_time(),
        "owner": 999999, "owner_ct": 1.0, "cmd": ["lune"]}), encoding="utf-8")
    try:
        assert procjob.reap(str(tmp_path)) == [leftover.pid]
        leftover.wait(10)
        assert not list(d.iterdir())
    finally:
        if leftover.poll() is None:
            leftover.kill()


def test_each_lune_run_writes_its_own_log(tmp_path, monkeypatch):
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    a = AnimalUprisingAdapter.__new__(AnimalUprisingAdapter)
    a.p = SimpleNamespace(id="p", workspace=str(tmp_path))
    a.cfg, a.state_dir = {}, str(tmp_path)

    def fake(cmd, **kw):
        kw["stdout"].write("PASS: a :: 1\nLUNE 1/1 PASS (1 ms)\n")
        return subprocess.CompletedProcess(cmd, 0, None, None)
    monkeypatch.setattr(subprocess, "run", fake)
    r1 = a._lune_run("lune", "au.lune", [], r"^LUNE \d+/\d+ PASS", None, str(tmp_path), "rev")
    r2 = a._lune_run("lune", "au.lune", [], r"^LUNE \d+/\d+ PASS", None, str(tmp_path), "rev")
    assert r1.result == r2.result == PASS
    assert r1.evidence != r2.evidence and os.path.basename(r1.evidence).startswith("au.lune.")
    assert "LUNE 1/1 PASS" in open(r1.evidence, encoding="utf-8").read()


def test_finished_rows_survive_in_the_check_cache(tmp_path):
    rows = [VerificationResult("au.done_check.SCOPE", PASS, True, "log", "ok", "abc")]
    assert check_cache.put(str(tmp_path), "au", "T1", "done_check:verify", "abc", "k1", rows)
    got = check_cache.get(str(tmp_path), "au", "T1", "done_check:verify", "abc", "k1")
    assert [(r.check, r.result, r.detail) for r in got] == [("au.done_check.SCOPE", PASS, "ok")]
    assert check_cache.get(str(tmp_path), "au", "T1", "done_check:verify", "abc", "k2") is None
    bad = [VerificationResult("au.done_check", UNKNOWN, True, "", "timeout", "abc")]
    assert not check_cache.put(str(tmp_path), "au", "T1", "done_check:verify", "abc", "k3", bad)


def test_done_check_is_not_run_again_after_a_restart(repo, monkeypatch):
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    a, head = repo["a"], repo["head"]
    spec = a.describe(AU_T)
    real, calls = subprocess.run, []

    def fake(cmd, **kw):
        if cmd and cmd[0] == "git":
            return real(cmd, **kw)
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "PASS COMMITS ok\n", "")
    monkeypatch.setattr(subprocess, "run", fake)
    first = a._done_check(spec, "verify", head)
    b = AnimalUprisingAdapter(a.p, a.state_dir)              # "the restarted coordinator"
    again = b._done_check(spec, "verify", head)
    assert len(calls) == 1
    assert [(r.check, r.result) for r in again] == [(r.check, r.result) for r in first]
    # a changed working tree is another input: done_check runs again
    (repo["ws"] / "src" / "client" / "Kit.lua").write_text("return {changed = true}\n")
    b._done_check(spec, "verify", head)
    assert len(calls) == 2


def test_the_heartbeat_says_whether_checks_are_running(tmp_path):
    from foundry_coordinator import cli
    beats = []
    bg = SimpleNamespace(running=lambda: [{"label": "T1 verify", "seconds": 5, "key": ()}])
    hb = SimpleNamespace(c=SimpleNamespace(lead=None, bg_checks=bg, _early_reviews={}),
                         _tick_errors=lambda: 0, _last_ts=0, _beat=beats.append,
                         _stop_requested=lambda: False)
    cls = next(v for v in vars(cli).values() if isinstance(v, type)
               and "_tick_errors" in vars(v) and "tick" in vars(v))
    cls.tick(hb)
    assert beats[-1]["checks_running"] is True and beats[-1]["checks"] == ["T1 verify 5 s"]
    hb.c.bg_checks = SimpleNamespace(running=lambda: [])
    cls.tick(hb)
    assert beats[-1]["checks_running"] is False and beats[-1]["checks"] == []


# ------------------------------------------------------------------ 5. Kilo polling

class _Kilo:
    def __init__(self, msgs, status=None):
        self.msgs, self.status, self.calls = msgs, status or {}, []

    def __call__(self, method, path, body=None, query=None, ok404=False):
        self.calls.append((path, dict(query or {})))
        if path == "/session/s1":
            return {"id": "s1"}
        if path in ("/question", "/permission"):
            return []
        if path == "/session/status":
            return self.status
        if path == "/session/s1/message":
            lim = (query or {}).get("limit")
            return self.msgs[-lim:] if lim else list(self.msgs)
        raise AssertionError(path)

    def message_reads(self):
        return [q for p, q in self.calls if p == "/session/s1/message"]


def _msg(role, i, created, completed=None, tin=0, tout=0, cache_read=0, text=""):
    return {"info": {"id": f"m{i}", "role": role,
                     "time": {"created": created, "completed": completed},
                     "tokens": {"input": tin, "output": tout, "cache": {"read": cache_read}}},
            "parts": [{"type": "text", "text": text}]}


def test_kilo_state_reads_status_first_and_only_the_newest_messages():
    msgs = [_msg("user", 1, 1000), _msg("assistant", 2, 2000, 3000, 100, 10, 50),
            _msg("user", 3, 4000), _msg("assistant", 4, 5000, 9000, 200, 20, 70, "done")]
    k = KiloClient("http://unused")
    fake = _Kilo(msgs, status={"s1": {"type": "busy"}})
    k._req = fake
    assert k.state("s1", directory="d", since_ts=1).status == "busy"
    assert fake.message_reads() == []                         # busy: no transcript at all
    fake.status = {}
    s = k.state("s1", directory="d", since_ts=1)
    assert s.status == "idle" and s.final_text == "done"
    assert s.tokens == {"input": 300, "output": 30} and s.context == 270
    reads = fake.message_reads()
    assert reads[0].get("limit") == 2                         # the newest message first
    assert sum(1 for q in reads if "limit" not in q) == 1     # one full read for the totals
    fake.calls.clear()
    s2 = k.state("s1", directory="d", since_ts=1)
    assert s2.tokens == s.tokens
    assert all(q.get("limit") for q in fake.message_reads())  # polled again: no full read


def test_kilo_recent_keeps_the_tail_when_the_server_ignores_the_limit():
    msgs = [_msg("user", i, i) for i in range(10)]
    k = KiloClient("http://unused")
    k._req = lambda *a, **kw: list(msgs)
    assert [m["info"]["id"] for m in k.recent("s1", directory="d", n=3)] == ["m7", "m8", "m9"]


# ------------------------------------------------------------------ 6. orphan lock vs Lead

def test_a_studio_lock_is_kept_while_a_write_access_lead_call_runs(tmp_path, monkeypatch):
    from tests.coordinator import test_orphan_studio_lock as OL
    monkeypatch.setenv("AU_LOCK_DIR", str(tmp_path))
    p = OL._lock(tmp_path)
    lead = SimpleNamespace(active_case=lambda pid, tid: {"case_id": "rc-1"},
                           settings=lambda P: {"write_access": True},
                           _open_call=lambda cid, kind: {"op_key": "lead:rc-1:1"})
    eng, P = OL._engine({"GF-GATE1": S.BLOCKED})
    eng._lead_any = lambda: lead
    eng._lead_writer_open = lambda P_, tid: Coordinator._lead_writer_open(eng, P_, tid)
    Coordinator._orphan_studio_lock(eng, P)
    assert p.exists() and not eng.store.events
    # read-only Lead: the lock is released as before
    lead.settings = lambda P: {"write_access": False}
    eng.ORPHAN_LOCK_EVERY_S = 0
    Coordinator._orphan_studio_lock(eng, P)
    assert not p.exists()

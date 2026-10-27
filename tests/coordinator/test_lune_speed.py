"""AU verify speed: Lune PASS reuse by input content, and done_check / Lune /
negative control run side by side.

A synthetic AU-like repo (never the real game repository) and a fake
subprocess for done_check and lune; git runs for real."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import pytest

from foundry_coordinator.adapters.base import FAIL, PASS, UNKNOWN, TaskSpec
from foundry_coordinator.profile import Profile
from foundry_coordinator.projects.animaluprising import lune_cache
from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
from tests.coordinator.helpers import git

T = "GF-X1"
REAL_RUN = subprocess.run
OK_LUNE = "PASS: A :: a\nLUNE 1/1 PASS (5 ms)\n"
OK_NEG = "FAIL: N :: n\nNEGATIVE CONTROL PASS (1/1 failed as expected)\n"
DONE_OK = "PASS COMMITS 1 commit\nPASS CLEAN tree clean\nPASS SCOPE in scope\n"

SPEC = '''return function(T, L)
    local p = "docs/progress/" .. "GF-X1" .. "_gate.json"
    local ids = L.readFile("assets/models/model_ids.json")
end
'''
PROJECT = {"name": "au", "tree": {"$className": "DataModel",
                                  "Shared": {"$path": "src"}, "Pk": {"$path": "Packages"}}}


def _write(ws, rel, text):
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _commit(ws, msg="c"):
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "--no-verify", "-m", msg)
    return git(ws, "rev-parse", "HEAD")


class FakeRun:
    """subprocess.run for lune and done_check; everything else is real."""

    def __init__(self):
        self.calls = []                  # (kind, start, end, kwargs)
        self.out = {"normal": OK_LUNE, "negative": OK_NEG, "done": DONE_OK}
        self.raise_for = {}              # kind -> exception
        self.sleep = {}                  # kind -> seconds after the barrier
        self.barrier = None
        self.lock = threading.Lock()

    def __call__(self, cmd, *a, **kw):
        cmd = [str(c) for c in cmd]
        if cmd[0] != "fake-lune" and not any(c.endswith("done_check.py") for c in cmd):
            return REAL_RUN(cmd, *a, **kw)
        if cmd[0] == "fake-lune" and cmd[1:] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, "lune 0.10.5\n", "")
        kind = ("done" if cmd[0] != "fake-lune" else
                "negative" if "--negative" in cmd else "normal")
        t0 = time.perf_counter()
        if self.barrier is not None:
            self.barrier.wait()
        time.sleep(self.sleep.get(kind, 0))
        with self.lock:
            self.calls.append((kind, t0, time.perf_counter(), kw))
        if kind in self.raise_for:
            raise self.raise_for[kind]
        return subprocess.CompletedProcess(cmd, 0, self.out[kind], "")

    def kinds(self):
        return sorted(c[0] for c in self.calls)


@pytest.fixture
def env(tmp_path, monkeypatch):
    ws = tmp_path / "au-fake"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "master")
    _write(ws, ".gitignore", "Packages/\n")
    _write(ws, "default.project.json", json.dumps(PROJECT))
    _write(ws, "wally.lock", "lock 1\n")
    _write(ws, "rokit.toml", "lune = '0.10.5'\n")
    _write(ws, "src/A.lua", "return 1\n")
    _write(ws, "tests/lune/run.luau", "-- runner\n")
    _write(ws, "tests/lune/specs/A.spec.luau", SPEC)
    _write(ws, "docs/progress/GF-X1_gate.json", "{}\n")
    _write(ws, "docs/memory/NOTES.md", "notes\n")
    _write(ws, "assets/models/model_ids.json", "{}\n")
    _write(ws, "assets/other/big.txt", "unread\n")
    _write(ws, "Packages/pkg/init.lua", "return 'pkg v1'\n")
    rev = _commit(ws, "base")
    prof = Profile(id="animaluprising", name="AU", version="1", path="fixture.toml",
                   hash="fixture", workspace=str(ws), allowed_dirs=["src", "tests", "docs"],
                   branch="master", remote="origin", raw={})
    a = AnimalUprisingAdapter(prof, str(tmp_path / "state"))
    a.cfg = {"lune": "fake-lune", "python": ["fake-python"]}
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    spec = TaskSpec("animaluprising", T, "t", "", [], data={"tier": "A"})
    return {"ws": ws, "a": a, "fake": fake, "rev": rev, "spec": spec,
            "cache": lune_cache.cache_path(str(tmp_path / "state"), "animaluprising")}


def _cache(env):
    with open(env["cache"], encoding="utf-8") as f:
        return json.load(f)


def _by(rows):
    return {r.check: r for r in rows}


# -- reuse ---------------------------------------------------------------------------

def test_first_run_stores_passes_and_the_same_revision_reuses_them(env):
    a, fake, rev = env["a"], env["fake"], env["rev"]
    rows = a._lune(env["spec"], rev)
    assert [r.check for r in rows] == ["au.lune", "au.lune_negative"]
    assert [r.result for r in rows] == [PASS, PASS]
    assert fake.kinds() == ["negative", "normal"]
    assert len(_cache(env)) == 2                     # separate keys: normal, negative
    fake.calls.clear()
    again = a._lune(env["spec"], rev)
    assert fake.calls == []
    assert [r.result for r in again] == [PASS, PASS]
    assert "carried from a run at " + rev[:12] in again[0].detail
    assert "LUNE 1/1 PASS" in again[0].detail
    assert os.path.isfile(again[0].evidence)


def test_unread_change_reuses_and_copies_the_log(env):
    a, fake, ws, rev = env["a"], env["fake"], env["ws"], env["rev"]
    a._lune(env["spec"], rev)
    _write(ws, "docs/memory/NOTES.md", "more notes\n")
    _write(ws, "assets/other/big.txt", "still unread\n")
    rev2 = _commit(ws, "memory")
    fake.calls.clear()
    rows = a._lune(env["spec"], rev2)
    assert fake.calls == []
    assert [r.result for r in rows] == [PASS, PASS]
    lune = rows[0]
    assert rev2[:12] in lune.evidence and rev[:12] in lune.detail
    assert "key " in lune.detail and "original log" in lune.detail
    text = open(lune.evidence, encoding="utf-8").read()
    assert text.startswith("# carried from") and "LUNE 1/1 PASS" in text


@pytest.mark.parametrize("rel", ["src/A.lua", "tests/lune/specs/A.spec.luau",
                                 "default.project.json", "wally.lock",
                                 "docs/progress/GF-X1_gate.json",
                                 "assets/models/model_ids.json"])
def test_an_input_change_runs_again(env, rel):
    a, fake, ws = env["a"], env["fake"], env["ws"]
    a._lune(env["spec"], env["rev"])
    p = ws / rel
    p.write_text(p.read_text(encoding="utf-8") + "\n-- changed\n", encoding="utf-8")
    rev2 = _commit(ws, "change " + rel)
    fake.calls.clear()
    rows = a._lune(env["spec"], rev2)
    assert fake.kinds() == ["negative", "normal"]
    assert all("carried" not in r.detail for r in rows)


def test_a_package_change_on_disk_runs_again(env):
    a, fake, ws = env["a"], env["fake"], env["ws"]
    a._lune(env["spec"], env["rev"])
    _write(ws, "Packages/pkg/init.lua", "return 'pkg v2'\n")     # gitignored, same commit
    fake.calls.clear()
    a._lune(env["spec"], env["rev"])
    assert fake.kinds() == ["negative", "normal"]


def test_a_new_lune_version_runs_again(env, monkeypatch):
    a, fake = env["a"], env["fake"]
    a._lune(env["spec"], env["rev"])
    monkeypatch.setattr(a, "_lune_version", lambda lune: "lune 0.11.0")
    fake.calls.clear()
    a._lune(env["spec"], env["rev"])
    assert fake.kinds() == ["negative", "normal"]


def test_fail_is_never_cached(env):
    a, fake = env["a"], env["fake"]
    fake.out["normal"] = "FAIL: A :: a — boom\nLUNE 0/1 FAIL (5 ms)\n"
    rows = _by(a._lune(env["spec"], env["rev"]))
    assert rows["au.lune"].result == FAIL and rows["au.lune_negative"].result == PASS
    keys = _cache(env)
    assert len(keys) == 1 and all(e["result"] == PASS for e in keys.values())
    fake.calls.clear()
    fake.out["normal"] = OK_LUNE
    rows = _by(a._lune(env["spec"], env["rev"]))
    assert fake.kinds() == ["normal"]                 # only the failed run reruns
    assert rows["au.lune"].result == PASS
    assert "carried" in rows["au.lune_negative"].detail


def test_unknown_is_never_cached(env):
    a, fake = env["a"], env["fake"]
    fake.raise_for["normal"] = subprocess.TimeoutExpired("lune", 1800)
    a._lune(env["spec"], env["rev"])
    assert [e["check"] for e in _cache(env).values()] == ["au.lune_negative"]


def test_reuse_switch_off_always_runs_and_stores_nothing(env):
    a, fake = env["a"], env["fake"]
    a.cfg["lune_reuse"] = False
    a._lune(env["spec"], env["rev"])
    a._lune(env["spec"], env["rev"])
    assert fake.kinds() == ["negative", "negative", "normal", "normal"]
    assert not os.path.exists(env["cache"])
    assert a._lune_reuse() is False
    a.cfg["lune_reuse"] = "off"
    assert a._lune_reuse() is False
    a.cfg.pop("lune_reuse")
    assert a._lune_reuse() is True


@pytest.mark.parametrize("dirty", ["modified", "untracked"])
def test_a_dirty_tree_is_neither_reused_nor_stored(env, dirty):
    a, fake, ws = env["a"], env["fake"], env["ws"]
    a._lune(env["spec"], env["rev"])
    before = _cache(env)
    if dirty == "modified":
        _write(ws, "src/A.lua", "return 2\n")
    else:
        _write(ws, "tests/lune/specs/New.spec.luau", "return function() end\n")
    fake.calls.clear()
    a._lune(env["spec"], env["rev"])
    assert fake.kinds() == ["negative", "normal"]
    assert _cache(env) == before
    info = lune_cache.input_key(str(ws), env["rev"], "lune 0.10.5")
    assert info["key"] is None and "differs" in info["why"]


def test_head_elsewhere_is_not_reused(env):
    a, fake, ws, rev = env["a"], env["fake"], env["ws"], env["rev"]
    a._lune(env["spec"], rev)
    _write(ws, "docs/memory/NOTES.md", "next\n")
    _commit(ws, "next")
    fake.calls.clear()
    a._lune(env["spec"], rev)                         # verifying a revision HEAD isn't at
    assert fake.kinds() == ["negative", "normal"]


def test_a_missing_original_log_runs_again(env):
    a, fake = env["a"], env["fake"]
    rows = a._lune(env["spec"], env["rev"])
    os.remove(rows[0].evidence)
    fake.calls.clear()
    a._lune(env["spec"], env["rev"])
    assert fake.kinds() == ["normal"]


def test_input_key_lists_the_suite_inputs(env):
    info = lune_cache.input_key(str(env["ws"]), env["rev"], "lune 0.10.5")
    paths = info["paths"]
    for p in ("src", "tests", "default.project.json", "wally.lock", "rokit.toml",
              "docs/progress", "assets/models/model_ids.json"):
        assert p in paths, p
    assert paths["Packages"].startswith("disk:")
    assert "docs/memory" not in paths and "assets/other" not in paths
    assert lune_cache.mode_key(info, "normal") != lune_cache.mode_key(info, "negative")
    assert lune_cache.mode_key(None, "normal") is None


def test_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(lune_cache, "MAX_ENTRIES", 3)
    p = str(tmp_path / "c.json")
    for i in range(5):
        lune_cache.put(p, f"k{i}", {"result": PASS, "rev": str(i)})
        time.sleep(0.01)
    data = json.load(open(p, encoding="utf-8"))
    assert sorted(data) == ["k2", "k3", "k4"]
    lune_cache.put(p, "k4", {"result": FAIL})
    assert lune_cache.get(p, "k4") is None and lune_cache.get(p, "k3")


# -- concurrency -----------------------------------------------------------------------

def test_verify_runs_done_check_and_both_lune_runs_together(env):
    a, fake = env["a"], env["fake"]
    a.cfg["lune_reuse"] = False
    # all three must be inside subprocess.run at once to pass the barrier
    fake.barrier = threading.Barrier(3, timeout=20)
    fake.sleep = {"normal": 0.3, "done": 0.15, "negative": 0.05}
    rows = a.verify(env["spec"], "verify", env["rev"], env["rev"])
    names = [r.check for r in rows]
    assert names == ["au.done_check.COMMITS", "au.done_check.CLEAN", "au.done_check.SCOPE",
                     "au.gate_bound_to_head", "au.lune", "au.lune_negative"]
    assert _by(rows)["au.lune"].result == PASS
    assert _by(rows)["au.lune_negative"].result == PASS
    starts = [c[1] for c in fake.calls]
    ends = [c[2] for c in fake.calls]
    assert max(starts) < min(ends)                    # the three overlapped


def test_verify_track_runs_done_check_and_lune_together(env):
    a, fake = env["a"], env["fake"]
    a.cfg["lune_reuse"] = False
    fake.barrier = threading.Barrier(3, timeout=20)
    rows = a.verify_track(env["spec"], "verify", env["rev"], env["rev"])
    assert [r.check for r in rows] == ["au.done_check.COMMITS", "au.done_check.CLEAN",
                                       "au.done_check.SCOPE", "au.lune", "au.lune_negative"]


def test_timeouts_stay_unknown_and_keep_their_limits(env):
    a, fake = env["a"], env["fake"]
    a.cfg.update({"lune_reuse": False, "lune_timeout_s": 900})
    fake.raise_for = {"normal": subprocess.TimeoutExpired("lune", 900),
                      "done": subprocess.TimeoutExpired("python", 600)}
    rows = a.verify(env["spec"], "verify", env["rev"], env["rev"])
    by = _by(rows)
    assert by["au.done_check"].result == UNKNOWN
    assert by["au.lune"].result == UNKNOWN
    assert by["au.lune_negative"].result == PASS
    assert [r.check for r in rows][-2:] == ["au.lune", "au.lune_negative"]
    limits = {c[0]: c[3]["timeout"] for c in fake.calls}
    assert limits == {"normal": 900, "negative": 900, "done": 600}


def test_lune_workers_split_between_the_runs(env):
    a, fake = env["a"], env["fake"]
    a.cfg["lune_reuse"] = False
    a._lune(env["spec"], env["rev"])
    assert all(c[3].get("env") is None for c in fake.calls)   # unset: env untouched
    fake.calls.clear()
    a.cfg["lune_workers"] = 6
    a._lune(env["spec"], env["rev"])
    w = {c[0]: c[3]["env"]["AU_LUNE_WORKERS"] for c in fake.calls}
    assert w == {"normal": "5", "negative": "1"}


def test_lune_workers_all_to_the_one_run_left(env):
    a, fake = env["a"], env["fake"]
    fake.out["normal"] = "LUNE 0/1 FAIL\n"
    a._lune(env["spec"], env["rev"])                  # negative PASS cached
    fake.calls.clear()
    fake.out["normal"] = OK_LUNE
    a.cfg["lune_workers"] = 6
    a._lune(env["spec"], env["rev"])
    assert [(c[0], c[3]["env"]["AU_LUNE_WORKERS"]) for c in fake.calls] == [("normal", "6")]

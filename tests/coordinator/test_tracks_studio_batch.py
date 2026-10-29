"""tracks/merge.py studio_batch (2026-10-28, Victor): a track's assets are built first,
Studio-free; at its merge the coordinator runs the scripted Studio gate ([studio_calls],
studio_calls.py) in the main checkout for each of its covered sprints, in order, back to
back under ONE Studio lease. A red sprint fails the merge naming it; a tooling ERROR
retries just that sprint; a track [studio_calls] does not cover merges as before.

Replays: AS-D13, AS-V00..AS-V03 merged from a Blender track (needs blender, images,
audio) with no Studio test at all (the merge gate ran the adapter's verify only)."""

from __future__ import annotations

import json
import os
import textwrap
import time

import pytest

from foundry_coordinator import states as S
from tests.coordinator.helpers import by_task, coordinator, make_repo, run_until, write_ok, \
    write_profile
from tests.coordinator.test_studio_calls import FAKE_PIPELINE

TASKS = [
    {"id": "T1", "title": "main sprint", "spec": "Write src/a.txt.", "scope": ["src/a.txt"]},
    {"id": "B1", "title": "asset one", "spec": "Write art/b1.txt.", "scope": ["art/b1.txt"],
     "tags": ["needs:blender"]},
    {"id": "B2", "title": "asset two", "spec": "Write art/b2.txt.", "scope": ["art/b2.txt"],
     "tags": ["needs:blender"]},
    {"id": "B3", "title": "asset three", "spec": "Write art/b3.txt.", "scope": ["art/b3.txt"],
     "tags": ["needs:blender"]},
]
SPRINTS = ["B1", "B2", "B3"]


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AU_LOCK_DIR", str(tmp_path / "locks"))
    (tmp_path / "locks").mkdir()

    def build(plan, covered=("B1", "B2", "B3"), automation="", with_suites=()):
        files = {".gitignore": ".plan/\n"}
        for s in with_suites:          # a sprint with suites of its own (its last host half)
            files[f"docs/progress/{s}_EVIDENCE_RAW.log"] = (
                f"GATE RUN sprint={s} tier=A regression=none suites=[S51] files=[art/x]\n")
        root, _ = make_repo(tmp_path, "game", TASKS, extra_files=files)
        pipe = tmp_path / "fake_pipeline.py"
        pipe.write_text(FAKE_PIPELINE)
        pf = tmp_path / "calls_plan.json"
        pf.write_text(json.dumps(plan))
        monkeypatch.setenv("FAKE_CALLS_PLAN", str(pf))
        mod = str(pipe).replace(chr(92), "/")
        extra = textwrap.dedent(f'''\
            [automation]
            max_agents = 2
            decision_timeout_minutes = 30
            ''') + textwrap.dedent(automation) + textwrap.dedent(f'''\
            [studio_calls]
            enabled = true
            tasks = {json.dumps(list(covered))}
            pipeline_module = "{mod}"
            runner_module = "{mod}"
            ''')
        prof = write_profile(tmp_path, "game", root, extra=extra, publish="none", review=False)
        clock = Clock()
        scripts = {"game": by_task({s: write_ok(f"art/{s.lower()}.txt") for s in SPRINTS}
                                   | {"T1": write_ok("src/a.txt")})}
        c = coordinator(tmp_path, [prof], scripts, clock=clock, max_writers=None)
        return c, c.projects["game"], clock, root, pf
    return build


def events(c, kind):
    return [e for e in c.store.events("game", 500) if e["kind"] == kind]


def track(c):
    return c.store.track("game", "blender")


def build_track(c, P):
    c.tracks.create(P, "blender", SPRINTS)
    c.tracks.start(P, "blender", "victor")
    assert run_until(c, lambda: all(c.store.task("game", s)["state"] == S.COMPLETE
                                    for s in SPRINTS), limit=150)


def drive(c, clock, pred, limit=400, step=5.0):
    for _ in range(limit):
        c.tick()
        clock.t += step
        if pred():
            return True
        time.sleep(0.02)
    raise AssertionError(f"timed out: {track(c)['state']} {track(c)['data'].get('merge_stage')} "
                         f"{track(c)['data'].get('merge_wait')}")


def run_info(pf):
    """(sprint, regression, suites) of every pipeline run, in order."""
    out, n = [], 0
    while os.path.exists(f"{pf}.args.{n}"):
        a = json.loads(open(f"{pf}.args.{n}").read())
        out.append((a[a.index("--sprint") + 1], a[a.index("--regression") + 1],
                    a[a.index("--suites") + 1]))
        n += 1
    return out


def run_args(pf):
    return [x[0] for x in run_info(pf)]


def detail(d):
    return json.loads(d) if isinstance(d, str) else d


def leases(c):
    return [r for r in c.store.journal_rows("studio", "game", limit=200)
            if r["owner"] == "track:blender" and r["event"] == "acquire"]


def test_blender_track_gets_one_batched_full_regression_at_merge(env):
    """Replay AS-D13/AS-V00..V03: no sprint has suites of its own, so the merge runs ONE
    full regression on the merged result (as the last sprint) that counts for all."""
    c, P, clock, root, pf = env([{"result": "PASS"}])
    build_track(c, P)
    drive(c, clock, lambda: track(c)["state"] == "merged")
    assert run_info(pf) == [("B3", "full", "")]                 # one run, not three
    assert len(leases(c)) == 1
    gate = track(c)["data"]["gate"]
    assert gate["result"] == "PASS" and gate["studio"] is True
    [r] = gate["studio_batch"]
    assert r["sprint"] == "B3" and r["covers"] == SPRINTS and r["result"] == "PASS"
    assert events(c, "track_studio_batch_green") and events(c, "track_merged")
    assert c.tracks.resources.lease(P) is None                  # released at the end
    run_id = c.store.task("game", "B3")["data"]["studio_calls"]["last"]["run_id"]
    for s in SPRINTS:                                           # it counts for every asset
        last = c.store.task("game", s)["data"]["studio_calls"]["last"]
        assert last["result"] == "PASS" and last["track"] == "blender"
        assert last["run_id"] == run_id
        assert c.store.task("game", s)["state"] == S.COMPLETE
    args = json.loads(open(f"{pf}.args.0").read())
    assert args[args.index("--rerun") + 1] == "failed"
    assert args[args.index("--files") + 1] == "art/b1.txt,art/b2.txt,art/b3.txt"
    assert detail(leases(c)[0]["detail"])["lease_minutes"] == 45   # x runs executed (1)


def test_a_sprint_with_suites_runs_them_then_one_full_regression(env):
    c, P, clock, root, pf = env([{"result": "PASS"}], with_suites=("B2",))
    build_track(c, P)
    drive(c, clock, lambda: track(c)["state"] == "merged")
    assert run_info(pf) == [("B2", "none", "S51"), ("B3", "full", "")]
    gate = track(c)["data"]["gate"]
    assert [(r["sprint"], r["covers"]) for r in gate["studio_batch"]] == [
        ("B2", ["B2"]), ("B3", ["B1", "B3"])]
    assert len(leases(c)) == 1
    assert detail(leases(c)[0]["detail"])["lease_minutes"] == 45 * 2


def test_a_failing_sprint_fails_the_merge_naming_it(env):
    c, P, clock, root, pf = env([{"result": "FAIL"}, {"result": "PASS"}], with_suites=("B2",))
    build_track(c, P)
    drive(c, clock, lambda: track(c)["state"] == "failed")
    row = track(c)
    assert run_args(pf) == ["B2", "B3"]                         # the batch still ran them all
    why = row["data"]["failed"]["why"]
    assert "merge Studio batch red" in why and "B2 FAIL" in why and "row S46 FAIL" in why
    assert "B1" not in why and "B3" not in why
    assert row["data"]["gate"]["result"] == "FAIL"
    assert row["data"]["gate"]["findings"][0].startswith("B2 FAIL")
    assert git_head_has(root, row["data"]["merge_head"])        # landed, not reverted
    assert any("row S46 FAIL" in f for f in c.store.task("game", "B2")["data"]["findings"])
    assert not c.store.task("game", "B1")["data"].get("findings")
    assert c.tracks.resources.lease(P) is None


def test_a_red_full_regression_names_every_suiteless_sprint(env):
    c, P, clock, root, pf = env([{"result": "FAIL"}])
    build_track(c, P)
    drive(c, clock, lambda: track(c)["state"] == "failed")
    why = track(c)["data"]["failed"]["why"]
    assert "B1, B2, B3 (full regression, run as B3) FAIL" in why and "row S46 FAIL" in why
    # the failing rows go to the last sprint's findings
    assert any("row S46 FAIL" in f for f in c.store.task("game", "B3")["data"]["findings"])
    assert not c.store.task("game", "B1")["data"].get("findings")
    for s in SPRINTS:
        assert c.store.task("game", s)["data"]["studio_calls"]["last"]["result"] == "FAIL"
    assert detail(events(c, "track_studio_batch_red")[0]["detail"])["sprints"] == SPRINTS


def git_head_has(root, sha):
    from tests.coordinator.helpers import git
    return git(root, "merge-base", "--is-ancestor", sha, "main") == ""


def test_the_batch_waits_for_studio_while_the_main_sprint_holds_it(env):
    c, P, clock, root, pf = env([{"result": "PASS"}])
    c.store.acquire(["studio"], "game", "T1")                   # the main sprint's Studio lock
    build_track(c, P)
    for _ in range(6):
        c.tick()
        clock.t += 5
    row = track(c)
    assert row["state"] == "merging" and row["data"]["merge_stage"] == "studio_batch"
    assert "Studio: studio held by game/T1" in row["data"]["merge_wait"]
    assert not events(c, "studio_calls_started") and run_args(pf) == []
    c.store.release("game", "T1", ["studio"])
    drive(c, clock, lambda: track(c)["state"] == "merged")
    assert run_args(pf) == ["B3"] and len(leases(c)) == 1


def test_main_session_studio_use_also_holds_the_batch(env):
    c, P, clock, root, pf = env([{"result": "PASS"}])
    build_track(c, P)
    c.tracks.resources.note_studio_call(P, clock())             # main used Studio just now
    for _ in range(4):
        c.tick()
    assert "main session used Studio" in (track(c)["data"].get("merge_wait") or "")
    assert run_args(pf) == []
    drive(c, clock, lambda: track(c)["state"] == "merged", step=60)
    assert run_args(pf) == ["B3"]


def test_a_tooling_error_retries_that_run_only(env):
    c, P, clock, root, pf = env([{"result": "PASS"}, {"result": "ERROR", "error": "MCP gone"},
                                 {"result": "PASS"}], with_suites=("B2",))
    build_track(c, P)
    drive(c, clock, lambda: track(c)["state"] == "merged")
    assert run_args(pf) == ["B2", "B3", "B3"]                   # B2's passed run never re-run
    assert events(c, "track_gate_retry")
    assert not events(c, "track_failed")
    assert c.store.task("game", "B3")["state"] == S.COMPLETE    # never BLOCKED by it


@pytest.mark.parametrize("covered,automation", [
    (("T1",), ""),                                              # not covered
    (SPRINTS, "merge_studio_batch = false\n"),                   # switched off
])
def test_an_uncovered_track_keeps_todays_merge_gate(env, covered, automation):
    c, P, clock, root, pf = env([{"result": "PASS"}], covered=covered, automation=automation)
    build_track(c, P)
    assert run_until(c, lambda: track(c)["state"] == "merged", limit=10)
    row = track(c)
    assert row["data"]["gate"]["studio"] is False and "studio_batch" not in row["data"]["gate"]
    assert row["data"]["gate"]["checks"] == {"project-check": "PASS"}
    assert run_args(pf) == [] and not events(c, "studio_calls_started")
    assert not leases(c)

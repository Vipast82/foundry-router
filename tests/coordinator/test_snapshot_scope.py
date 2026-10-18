"""v0.119.1 review fix: source-snapshot identity and ownership are scoped to
project/task/run (+ workspace). Identical HEAD and bytes in two projects,
tasks or runs never share a row or a blob root; lookups refuse a record
owned by someone else; unchanged-content comparisons use content_fp; legacy
content-only rows stay readable by their recorded owner only."""

import json
import os

import pytest

from foundry_coordinator.artifacts import ArtifactStore
from foundry_coordinator.store import Store

HEAD = "a" * 40


class FakeWS:
    def __init__(self, head=HEAD, dirty=()):
        self._head, self._dirty = head, list(dirty)

    def head(self):
        return self._head

    def status(self):
        return [(" M", p) for p in self._dirty]


def mkws(tmp_path, name, text="return 1\n"):
    d = tmp_path / name
    d.mkdir()
    (d / "main.luau").write_bytes(text.encode())
    return d


@pytest.fixture()
def arts(tmp_path):
    return ArtifactStore(Store(str(tmp_path / "s.sqlite")), str(tmp_path / "state"))


def snap(a, ws, pid="project-a", tid="T1", run="run-1", wso=None):
    return a.source_snapshot(ws=wso or FakeWS(), project_id=pid, task_id=tid, run_id=run,
                             workspace=str(ws), patterns=["main.luau"])


def blob_root(a, pid):
    return os.path.join(a.state_dir, "snapshots", pid, "blobs")


def test_identical_content_in_two_projects_is_owned_separately(tmp_path, arts):
    """The reproduced case: same HEAD, same main.luau, two projects."""
    wa, wb = mkws(tmp_path, "a"), mkws(tmp_path, "b")
    s1 = snap(arts, wa, "project-a", run="run-project-a")
    s2 = snap(arts, wb, "project-b", run="run-project-b")
    assert s1["snapshot_id"] != s2["snapshot_id"]
    assert s1["content_fp"] == s2["content_fp"]                    # same code, comparable
    assert arts.snapshot(s2["snapshot_id"], "project-b")["project_id"] == "project-b"
    assert arts.snapshot(s1["snapshot_id"], "project-a")["run_id"] == "run-project-a"
    # each owner's blobs live under its own root, and its file reads from there
    h = s2["manifest"]["main.luau"][:32]
    assert os.path.isfile(os.path.join(blob_root(arts, "project-b"), h))
    assert os.path.isfile(os.path.join(blob_root(arts, "project-a"), h))
    os.remove(os.path.join(blob_root(arts, "project-a"), h))      # A's copy gone: B unaffected
    assert arts.snapshot_file(s2["snapshot_id"], "main.luau", "project-b") == "return 1\n"
    # a caller-supplied id never crosses the project boundary
    assert arts.snapshot(s2["snapshot_id"], "project-a") is None
    assert arts.snapshot_file(s2["snapshot_id"], "main.luau", "project-a") is None
    assert arts.drift(s2["snapshot_id"], str(wb), ["main.luau"], "project-a") == \
        {"main.luau": "new file"}
    assert arts.drift(s2["snapshot_id"], str(wb), ["main.luau"], "project-b") == \
        {"main.luau": "unchanged"}
    assert arts.snapshot(s2["snapshot_id"], "") is None


def test_two_tasks_and_two_runs_get_their_own_records(tmp_path, arts):
    w = mkws(tmp_path, "w")
    t1, t2 = snap(arts, w, tid="T1"), snap(arts, w, tid="T2")
    assert t1["snapshot_id"] != t2["snapshot_id"]
    assert arts.snapshot(t2["snapshot_id"], "project-a", "T2")["task_id"] == "T2"
    assert arts.snapshot(t2["snapshot_id"], "project-a", "T1") is None     # task-scoped lookup
    r1, r2 = snap(arts, w, run="run-1"), snap(arts, w, run="run-2")
    assert r1["snapshot_id"] != r2["snapshot_id"] and r1["content_fp"] == r2["content_fp"]
    assert arts.snapshot(r2["snapshot_id"], "project-a", "T1")["run_id"] == "run-2"


def test_same_scope_and_content_coalesces_and_dirty_trees_differ(tmp_path, arts):
    w = mkws(tmp_path, "w")
    a1, a2 = snap(arts, w), snap(arts, w)
    assert a1["snapshot_id"] == a2["snapshot_id"]                  # same run, same code: one row
    assert arts.store.q("SELECT COUNT(*) n FROM source_snapshots")[0]["n"] == 1
    (w / "main.luau").write_bytes(b"return 2\n")
    d = snap(arts, w, wso=FakeWS(dirty=["main.luau"]))
    assert d["snapshot_id"] != a1["snapshot_id"] and d["content_fp"] != a1["content_fp"]
    assert d["dirty"] is True


def test_restart_reuses_the_same_scoped_record(tmp_path):
    w = mkws(tmp_path, "w")
    a = ArtifactStore(Store(str(tmp_path / "s.sqlite")), str(tmp_path / "state"))
    s1 = snap(a, w)
    a2 = ArtifactStore(Store(str(tmp_path / "s.sqlite")), str(tmp_path / "state"))   # restart
    s2 = snap(a2, w)
    assert s1["snapshot_id"] == s2["snapshot_id"]
    assert a2.snapshot_file(s1["snapshot_id"], "main.luau", "project-a", "T1") == "return 1\n"


def test_legacy_rows_stay_readable_by_their_owner_only(tmp_path):
    """A v0.118.0 store: content-only id, no content_fp column. The migration
    only adds columns; the row's owner is never rewritten."""
    import sqlite3
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE source_snapshots (snapshot_id TEXT PRIMARY KEY,
      project_id TEXT, task_id TEXT, run_id TEXT, head TEXT, dirty INTEGER, manifest TEXT,
      status TEXT, reason TEXT, bytes INTEGER, created_ts REAL);""")
    import hashlib
    blob = b"return 1\n"
    h = hashlib.sha256(blob).hexdigest()
    con.execute("INSERT INTO source_snapshots VALUES ('ss-44ac1227fbe2a4db','project-a','T1',"
                "'run-project-a',?,0,?,'consistent','',9,1.0)",
                (HEAD, json.dumps({"main.luau": h})))
    con.commit()
    con.close()
    a = ArtifactStore(Store(str(db)), str(tmp_path / "state"))
    os.makedirs(blob_root(a, "project-a"), exist_ok=True)
    with open(os.path.join(blob_root(a, "project-a"), h[:32]), "wb") as f:
        f.write(blob)
    old = a.snapshot("ss-44ac1227fbe2a4db", "project-a", "T1")
    assert old["legacy"] and old["project_id"] == "project-a"
    assert a.snapshot_file("ss-44ac1227fbe2a4db", "main.luau", "project-a") == "return 1\n"
    assert a.snapshot("ss-44ac1227fbe2a4db", "project-b") is None
    # a new snapshot of the same content by project-b gets its own record
    s = snap(a, mkws(tmp_path, "b"), "project-b", run="run-project-b")
    assert s["snapshot_id"] != "ss-44ac1227fbe2a4db"
    assert a.store.q("SELECT project_id FROM source_snapshots WHERE snapshot_id="
                     "'ss-44ac1227fbe2a4db'")[0]["project_id"] == "project-a"


def test_incremental_review_does_not_repeat_unchanged_code_across_runs(tmp_path):
    """The no-repeat rule compares content_fp, so a new run's (new) snapshot id
    for the same code is still 'unchanged'."""
    from tests.coordinator.helpers import git
    from tests.coordinator.lane_env import CLAUDE, admit, setup
    env = setup(tmp_path, lanes="[lanes]\nenabled = true\ntranscript_poll_seconds = 0\n"
                                "[lanes.incremental_review]\nenabled = true\n"
                                "min_interval_seconds = 0\n")
    try:
        c, root = env["c"], env["root"]
        env["ff"].admission[CLAUDE] = admit("defer", "weekly 80% used")
        c.tick()
        (root / "src/menu.luau").write_text("local M = {a=1}\nreturn M\n")
        git(root, "commit", "-qam", "T2: step")
        c.tick()
        [u] = [x for x in c.helpers.units("game", "T2") if x["lane"] == "incremental_review"]
        # v0.121.5+: only a review that actually ran (done, not rejected/expired)
        # counts as "reviewed"; fixture state: it ran and was delivered
        c.store.x("UPDATE helper_units SET status='done', feedback='integrated' WHERE unit_id=?",
                  (u["unit_id"],))
        P = list(c.projects.values())[0]
        c.helpers.lanes._after_incremental_review(P, u, {"structured": True,
                                                         "base": u["snapshot_id"],
                                                         "assignment": u["unit_id"]})
        t = c.store.task("game", "T2")
        # simulate a new run of the same task with the same code
        c.store.update_task("game", "T2", run_id=t["run_id"] + "-r2")
        t2 = c.store.task("game", "T2")
        lanes = c.helpers.lanes
        s_new = lanes._snap(P, t2, P.adapter.describe("T2"))
        assert s_new["snapshot_id"] != u["snapshot_id"]
        c.store.x("INSERT INTO lane_events (event_key, project_id, task_id, kind, status, "
                  "signature, detail, tries) VALUES ('k2','game','T2','commit','new','',"
                  "'{}',0)")
        from foundry_coordinator import lanes as L
        lanes._plan_commit(P, t2, P.adapter.describe("T2"), L.settings(P),
                           {"event_key": "k2", "detail": {}, "tries": 0, "signature": ""})
        assert c.store.q("SELECT status FROM lane_events WHERE event_key='k2'")[0]["status"] \
            == "unchanged"
        assert len([x for x in c.helpers.units("game", "T2")
                    if x["lane"] == "incremental_review"]) == 1
    finally:
        env["fake"].stop()


def test_a_review_that_never_ran_does_not_block_the_next_runs_review(tmp_path):
    """The other side of the no-repeat rule (v0.121.5): an earlier run's review
    that never ran (deferred) can never be delivered to the new run, so the
    same code gets its own review in the new run."""
    from tests.coordinator.helpers import git
    from tests.coordinator.lane_env import CLAUDE, admit, setup
    env = setup(tmp_path, lanes="[lanes]\nenabled = true\ntranscript_poll_seconds = 0\n"
                                "[lanes.incremental_review]\nenabled = true\n"
                                "min_interval_seconds = 0\n")
    try:
        c, root = env["c"], env["root"]
        env["ff"].admission[CLAUDE] = admit("defer", "weekly 80% used")
        c.tick()
        (root / "src/menu.luau").write_text("local M = {a=1}\nreturn M\n")
        git(root, "commit", "-qam", "T2: step")
        c.tick()
        P = list(c.projects.values())[0]
        t = c.store.task("game", "T2")
        c.store.update_task("game", "T2", run_id=t["run_id"] + "-r2")
        t2 = c.store.task("game", "T2")
        c.store.x("INSERT INTO lane_events (event_key, project_id, task_id, kind, status, "
                  "signature, detail, tries) VALUES ('k3','game','T2','commit','new','',"
                  "'{}',0)")
        from foundry_coordinator import lanes as L
        c.helpers.lanes._plan_commit(P, t2, P.adapter.describe("T2"), L.settings(P),
                                     {"event_key": "k3", "detail": {}, "tries": 0,
                                      "signature": ""})
        runs = {x["run_id"] for x in c.helpers.units("game", "T2")
                if x["lane"] == "incremental_review"}
        assert t2["run_id"] in runs and len(runs) == 2
    finally:
        env["fake"].stop()

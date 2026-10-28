"""Repo changes (self-driving part 3, prep_queue.py): queued patches / branches / scripts
land on the main checkout at a safe point, in order, all or nothing through the repo's
hooks, then one push and the after-actions; holds are released after landing. Real git
repositories with a bare remote; no session ever starts."""

import json
import os
import subprocess

import pytest

from foundry_coordinator import client_requests
from foundry_coordinator import prep_queue as PQ
from foundry_coordinator import states as S
from foundry_coordinator.control import ControlChannel
from tests.coordinator.helpers import coordinator, git, make_repo, write_profile

T1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt containing OK.",
      "scope": ["src/a.txt"]}
T2 = {"id": "T2", "title": "Other thing", "spec": "Write src/b.txt containing OK.",
      "scope": ["src/b.txt"]}


@pytest.fixture()
def game(tmp_path):
    root, remote = make_repo(tmp_path, "game", [T1, T2], extra_files={"src/a.txt": "one\n"})
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": {}},
                    store_path=tmp_path / "state" / "coordinator.sqlite")   # the CLI's store
    P = c.projects["game"]
    pushes = []
    real = P.ws.push

    def counting(remote_, branch):
        pushes.append((remote_, branch))
        return real(remote_, branch)
    P.ws.push = counting
    return c, P, root, remote, pushes


def make_patch(root, tmp_path, name, edits: dict) -> str:
    """A patch of `edits` against the current HEAD (the tree is restored afterwards)."""
    for rel, text in edits.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(root, "add", "-A")
    out = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--binary"],
                         capture_output=True, text=True, check=True).stdout
    git(root, "reset", "-q", "--hard", "HEAD")
    path = tmp_path / f"{name}.patch"
    path.write_text(out, newline="")
    return str(path)


def chain_patches(root, tmp_path):
    """p1 then p2, where p2 only applies on top of p1."""
    p1 = make_patch(root, tmp_path, "p1", {"src/a.txt": "one\ntwo\n"})
    git(root, "apply", "--index", p1)
    git(root, "commit", "-q", "-m", "tmp")
    p2 = make_patch(root, tmp_path, "p2", {"src/a.txt": "one\ntwo\nthree\n"})
    git(root, "reset", "-q", "--hard", "HEAD~1")
    return p1, p2


def remote_head(remote):
    return subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "main"],
                          capture_output=True, text=True).stdout.strip()


def land(c, P):
    return PQ.tick_project(c, P, force=True)


def add_hook(root, script: str):
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\n" + script + "\n", newline="\n")
    os.chmod(hook, 0o755)


# ------------------------------------------------------------------ ordering + push

def test_entries_land_in_order_each_with_its_sha_and_one_push(game, tmp_path):
    c, P, root, remote, pushes = game
    p1, p2 = chain_patches(root, tmp_path)
    start = git(root, "rev-parse", "HEAD")
    e2 = PQ.add(c, "game", "patch", "prep: second", source=p2, body="why two\n\nTrailer: x")
    e1 = PQ.add(c, "game", "patch", "prep: first", source=p1, position=1)
    assert [e["id"] for e in PQ.entries(c.store, "game")] == [e1["id"], e2["id"]]
    res = land(c, P)
    assert res["result"] == "landed", res
    a, b = PQ.get(c.store, e1["id"]), PQ.get(c.store, e2["id"])
    assert a["status"] == b["status"] == "landed"
    log = git(root, "log", "--format=%H %s", f"{start}..HEAD").splitlines()
    assert log == [f"{b['sha']} prep: second", f"{a['sha']} prep: first"]
    assert "why two" in git(root, "log", "-1", "--format=%B")
    assert (root / "src" / "a.txt").read_text() == "one\ntwo\nthree\n"
    assert pushes == [("origin", "main")]
    assert remote_head(remote) == b["sha"]
    ev = [e for e in c.store.events("game") if e["kind"] == "prep_landed"]
    assert ev and [x["id"] for x in ev[0]["detail"]["entries"]] == [e1["id"], e2["id"]]
    assert land(c, P) is None                     # nothing pending: nothing happens
    assert len(pushes) == 1
    # GF-GATE1 15:11 replay: an open task's checks treat landed prep commits as the
    # coordinator's, never the session's (scope / memory hygiene)
    from foundry_coordinator import bookkeeping as BK
    c.approve("game", "T1", [], "victor")
    land(c, P)
    t = c.store.task("game", "T1")
    assert {a["sha"], b["sha"]} <= BK.commits(t)
    assert BK.only_coordinator(P, t, start, b["sha"], ["src/a.txt"]) == ["src/a.txt"]


def test_reorder_and_remove(game, tmp_path):
    c, P, root, *_ = game
    p = make_patch(root, tmp_path, "x", {"src/x.txt": "x\n"})
    ids = [PQ.add(c, "game", "patch", f"prep: {i}", source=p)["id"] for i in range(3)]
    PQ.move(c, "game", ids[2], 1)
    assert [e["id"] for e in PQ.entries(c.store, "game")] == [ids[2], ids[0], ids[1]]
    PQ.remove(c, "game", ids[0])
    assert [e["id"] for e in PQ.entries(c.store, "game")] == [ids[2], ids[1]]
    assert [e["ord"] for e in PQ.entries(c.store, "game")] == [1, 2]
    assert PQ.get(c.store, ids[0])["status"] == "removed"


def test_already_applied_patch_is_skipped_not_failed(game, tmp_path):
    c, P, root, remote, pushes = game
    p = make_patch(root, tmp_path, "x", {"src/x.txt": "x\n"})
    git(root, "apply", "--index", p)
    git(root, "commit", "-q", "-m", "already there")
    git(root, "push", "-q", "origin", "main")
    e = PQ.add(c, "game", "patch", "prep: x", source=p)
    res = land(c, P)
    assert res["result"] == "landed"
    assert PQ.get(c.store, e["id"])["status"] == "already_applied"
    assert pushes == []                            # nothing new: no push


# ------------------------------------------------------------------ conditions

def test_condition_waits_for_the_task_and_stops_later_entries(game, tmp_path):
    c, P, root, remote, pushes = game
    c.approve("game", "T1", [], "victor")
    p = make_patch(root, tmp_path, "x", {"src/x.txt": "x\n"})
    q = make_patch(root, tmp_path, "y", {"src/y.txt": "y\n"})
    e1 = PQ.add(c, "game", "patch", "prep: x", source=p,
                condition=PQ.parse_condition("T1", "COMPLETE"))
    e2 = PQ.add(c, "game", "patch", "prep: y", source=q)          # no condition of its own
    assert land(c, P) is None
    st = PQ.snapshot(c, P)["state"]
    assert "T1 COMPLETE" in st["stops_at"] and not st["batch"]
    assert PQ.get(c.store, e2["id"])["status"] == "pending"       # never ahead of e1
    c.store.update_task("game", "T1", state=S.COMPLETE)
    assert land(c, P)["result"] == "landed"
    assert PQ.get(c.store, e1["id"])["status"] == PQ.get(c.store, e2["id"])["status"] == "landed"
    with pytest.raises(PQ.PrepError):
        PQ.parse_condition("T1", "DONE")


# ------------------------------------------------------------------ safe point

def test_safe_point_detection(game, tmp_path):
    c, P, root, *_ = game
    assert PQ.safe_point(c, P) == (True, "")
    c.approve("game", "T1", [], "victor")
    c.store.update_task("game", "T1", state=S.IMPLEMENTING)
    ok, why = PQ.safe_point(c, P)
    assert not ok and "T1 is IMPLEMENTING" in why
    c.store.update_task("game", "T1", state=S.BLOCKED)
    assert PQ.safe_point(c, P)[0]
    # a workspace lock held by a task
    wl = f"workspace:{os.path.normcase(os.path.realpath(P.profile.workspace))}"
    c.store.acquire([wl], "game", "T1")
    ok, why = PQ.safe_point(c, P)
    assert not ok and "in use by game/T1" in why
    c.store.release("game", "T1")
    # an open session of a main-checkout task
    c.store.add_session("ses-1", "game", "T1", "run-1", "implementer", "IMPLEMENT", 1,
                        "kilo", "p") if hasattr(c.store, "add_session") else \
        c.store.x("INSERT INTO sessions (session_id, project_id, task_id, role, status, "
                  "created_ts) VALUES (?,?,?,?,?,?)",
                  ("ses-1", "game", "T1", "implementer", "prompted", c.clock()))
    ok, why = PQ.safe_point(c, P)
    assert not ok and "ses-1" in why
    # ... unless the task is PAUSED and its session is idle (busy still blocks)
    from types import SimpleNamespace
    status = {"v": "busy"}
    real_state = P.client.state
    P.client.state = lambda *a, **k: SimpleNamespace(status=status["v"])
    c.store.update_task("game", "T1", state=S.PAUSED, session_id="ses-1")
    assert not PQ.safe_point(c, P)[0]
    status["v"] = "idle"
    assert PQ.safe_point(c, P)[0]
    P.client.state = real_state
    c.store.update_task("game", "T1", state=S.BLOCKED)
    c.store.x("UPDATE sessions SET ended_ts=? WHERE session_id='ses-1'", (c.clock(),))
    # paused project
    c.store.set_project_paused("game", True, "upgrade")
    assert PQ.safe_point(c, P) == (False, "project paused")
    c.store.set_project_paused("game", False)
    # a tracked change in the main checkout
    (root / "src" / "a.txt").write_text("dirty\n")
    ok, why = PQ.safe_point(c, P)
    assert not ok and "uncommitted" in why
    git(root, "checkout", "--", "src/a.txt")
    (root / "untracked.txt").write_text("fine\n")               # untracked files are fine
    assert PQ.safe_point(c, P)[0]
    # the wrong branch
    git(root, "checkout", "-q", "-b", "side")
    ok, why = PQ.safe_point(c, P)
    assert not ok and "not main" in why
    git(root, "checkout", "-q", "main")
    # a background check of this project
    class BG:
        def running(self):
            return [{"key": ("game", "T1", "verify", "abc"), "label": "T1 verify"}]
    real, c.bg_checks = c.bg_checks, BG()
    try:
        ok, why = PQ.safe_point(c, P)
        assert not ok and "T1 verify" in why
    finally:
        c.bg_checks = real


def test_unsafe_point_lands_nothing_and_records_why(game, tmp_path):
    c, P, root, remote, pushes = game
    c.approve("game", "T1", [], "victor")
    c.store.update_task("game", "T1", state=S.VERIFYING)
    e = PQ.add(c, "game", "patch", "prep: x",
               source=make_patch(root, tmp_path, "x", {"src/x.txt": "x\n"}))
    assert land(c, P) is None
    assert PQ.get(c.store, e["id"])["status"] == "pending"
    st = PQ.snapshot(c, P)["state"]
    assert st["safe"] is False and "VERIFYING" in st["reason"]


# ------------------------------------------------------------------ all or nothing

def test_hook_refusal_rolls_back_the_whole_batch(game, tmp_path):
    c, P, root, remote, pushes = game
    good = make_patch(root, tmp_path, "good", {"src/x.txt": "fine\n"})
    bad = make_patch(root, tmp_path, "bad", {"src/y.txt": "BAD content\n"})
    later = make_patch(root, tmp_path, "later", {"src/z.txt": "z\n"})
    add_hook(root, 'if git diff --cached | grep -q BAD; then echo "hook: BAD is not allowed"; '
                   'exit 1; fi')
    start = git(root, "rev-parse", "HEAD")
    before_remote = remote_head(remote)
    e1 = PQ.add(c, "game", "patch", "prep: good", source=good)
    e2 = PQ.add(c, "game", "patch", "prep: bad", source=bad)
    e3 = PQ.add(c, "game", "patch", "prep: later", source=later)
    res = land(c, P)
    assert res["result"] == "failed" and res["failed"] == e2["id"]
    assert git(root, "rev-parse", "HEAD") == start
    assert git(root, "status", "--porcelain", "--untracked-files=no") == ""
    assert not (root / "src" / "x.txt").exists() and not (root / "src" / "y.txt").exists()
    assert pushes == [] and remote_head(remote) == before_remote
    for e in (e1, e2):
        assert PQ.get(c.store, e["id"])["status"] == "failed"
    assert PQ.get(c.store, e3["id"])["status"] == "pending"
    log2 = "\n".join(x["msg"] for x in PQ.get(c.store, e2["id"])["log"])
    assert "hook: BAD is not allowed" in log2 and "reset to" in log2
    ev = [e for e in c.store.events("game") if e["kind"] == "prep_failed"]
    assert ev and ev[0]["detail"]["alert_class"] == "prep_failed"
    assert ev[0]["detail"]["failed"] == e2["id"]
    assert "BAD is not allowed" in ev[0]["detail"]["error"]
    assert not c.store.locks()                    # the workspace lock is released
    # a failed entry blocks the queue (nothing lands out of order) until retried/removed
    assert land(c, P) is None
    assert "failed" in PQ.snapshot(c, P)["state"]["stops_at"]
    PQ.remove(c, "game", e2["id"])
    out = PQ.run_now(c, "game")
    assert out["reset"] == [e1["id"]]
    res = PQ.tick_project(c, P)                   # run-now skips the check interval
    assert res and res["result"] == "landed"
    assert PQ.get(c.store, e1["id"])["status"] == PQ.get(c.store, e3["id"])["status"] == "landed"
    assert len(pushes) == 1


def test_patch_that_does_not_apply_fails_the_batch(game, tmp_path):
    c, P, root, remote, pushes = game
    p = make_patch(root, tmp_path, "x", {"src/a.txt": "changed\n"})
    (root / "src" / "a.txt").write_text("conflict\n")
    git(root, "commit", "-qam", "moved on")
    e = PQ.add(c, "game", "patch", "prep: x", source=p)
    res = land(c, P)
    assert res["result"] == "failed"
    assert "git apply --check failed" in "\n".join(x["msg"] for x in
                                                   PQ.get(c.store, e["id"])["log"])


def test_commit_uses_the_adapter_commit_env(game, tmp_path):
    c, P, root, *_ = game
    seen = []
    P.adapter.commit_env = lambda paths: seen.append(paths) or {"PREP_HOOK_OK": "1"}
    add_hook(root, 'test "$PREP_HOOK_OK" = 1 || { echo "no env"; exit 1; }')
    e = PQ.add(c, "game", "patch", "prep: x",
               source=make_patch(root, tmp_path, "x", {"src/x.txt": "x\n"}))
    assert land(c, P)["result"] == "landed"
    assert seen == [["src/x.txt"]]
    assert PQ.get(c.store, e["id"])["status"] == "landed"


def test_branch_entry(game, tmp_path):
    c, P, root, *_ = game
    git(root, "checkout", "-q", "-b", "feat")
    (root / "src" / "f.txt").write_text("f\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "feat work")
    git(root, "checkout", "-q", "main")
    e = PQ.add(c, "game", "branch", "prep: feat as one commit", source="feat")
    assert land(c, P)["result"] == "landed"
    assert (root / "src" / "f.txt").read_text() == "f\n"
    assert git(root, "log", "-1", "--format=%s") == "prep: feat as one commit"
    assert PQ.get(c.store, e["id"])["sha"] == git(root, "rev-parse", "HEAD")


def test_script_entry_commits_the_named_paths(game, tmp_path):
    c, P, root, *_ = game
    sc = tmp_path / "amend.py"
    sc.write_text("import sys\nopen('src/a.txt', 'a').write('amended\\n')\nprint('amended')\n")
    e = PQ.add(c, "game", "script", "prep: amend", command=f'"{{python}}" "{sc}"',
               paths=["src/a.txt"])
    assert land(c, P)["result"] == "landed"
    assert git(root, "show", "--name-only", "--format=", "HEAD") == "src/a.txt"
    assert "amended" in (root / "src" / "a.txt").read_text()
    # a failing script refuses its batch (and nothing is committed)
    bad = tmp_path / "bad.py"
    bad.write_text("import sys\nopen('src/a.txt', 'a').write('half\\n')\nsys.exit(2)\n")
    head = git(root, "rev-parse", "HEAD")
    e2 = PQ.add(c, "game", "script", "prep: bad", command=f'"{{python}}" "{bad}"',
                paths=["src/a.txt"])
    assert land(c, P)["result"] == "failed"
    assert git(root, "rev-parse", "HEAD") == head
    assert "half" not in (root / "src" / "a.txt").read_text()
    assert "exited 2" in "\n".join(x["msg"] for x in PQ.get(c.store, e2["id"])["log"])
    assert PQ.get(c.store, e["id"])["status"] == "landed"


# ------------------------------------------------------------------ after-actions + holds

def test_after_actions_run_once_after_landing_and_holds_are_released(game, tmp_path):
    c, P, root, remote, pushes = game
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")
    c.pause("game", "T2", "short hold", actor="victor")
    src = tmp_path / "kilo-src"
    (src / "rules").mkdir(parents=True)
    (src / "rules" / "r.md").write_text("rule\n")
    (root / ".kilo").mkdir()
    (root / ".kilo" / "TRIAL.md").write_text("trial\n")
    marker = tmp_path / "ran.txt"
    acts = ["move_aside:.kilo/TRIAL.md", f"copy:{src}=>.kilo", "reload_kilo",
            f'run:"{{python}}" -c "open(r\'{marker}\', \'a\').write(\'x\')"',
            "resume:T2", "kilo_config_apply"]
    e1 = PQ.add(c, "game", "patch", "prep: 1",
                source=make_patch(root, tmp_path, "a", {"src/x.txt": "x\n"}),
                after_actions=acts, hold_tasks=["T1"], hold_minutes=30)
    e2 = PQ.add(c, "game", "patch", "prep: 2",
                source=make_patch(root, tmp_path, "b", {"src/y.txt": "y\n"}),
                after_actions=["reload_kilo"])
    t1 = c.store.task("game", "T1")
    assert t1["state"] == S.PAUSED                          # held (it was only QUEUED)
    h = t1["data"]["hold"]
    assert h["owner"] == f"prep_queue:{e1['id']}" and h["expires_ts"] > c.clock() + 29 * 60
    res = land(c, P)
    assert res["result"] == "landed"
    assert marker.read_text() == "x"                        # run: once, though deduped
    assert (root / ".kilo" / "rules" / "r.md").read_text() == "rule\n"
    assert not (root / ".kilo" / "TRIAL.md").exists()
    reqs = client_requests.pending(c.state_dir, "reload_kilo")
    assert len(reqs) == 1 and reqs[0]["count"] == 1         # deduplicated across entries
    assert c.store.task("game", "T2")["state"] != S.PAUSED  # resume:T2
    t1 = c.store.task("game", "T1")
    assert t1["state"] == S.QUEUED and "hold" not in t1["data"]
    after = dict(a.split(": ", 1) for a in res["after"])
    assert "FAILED" in after["kilo_config_apply"]           # no role rules: logged, not fatal
    assert [x for x in c.store.events("game") if x["kind"] == "prep_after_action_failed"]
    log = "\n".join(x["msg"] for x in PQ.get(c.store, e1["id"])["log"])
    assert "after-action resume:T2: resumed T2" in log and "released the hold on T1" in log
    assert len(pushes) == 1
    assert PQ.get(c.store, e2["id"])["status"] == "landed"


def test_removing_an_entry_releases_its_holds(game, tmp_path):
    c, P, root, *_ = game
    c.approve("game", "T1", [], "victor")
    e = PQ.add(c, "game", "patch", "prep: x",
               source=make_patch(root, tmp_path, "x", {"src/x.txt": "x\n"}), hold_tasks=["T1"])
    assert c.store.task("game", "T1")["state"] == S.PAUSED
    PQ.remove(c, "game", e["id"])
    t = c.store.task("game", "T1")
    assert t["state"] == S.QUEUED and "hold" not in t["data"]


def test_hold_uses_the_coordinator_api_when_present(game, tmp_path):
    c, P, root, *_ = game
    calls = []
    c.approve("game", "T1", [], "victor")            # a task the coordinator knows
    def fake_hold(*a):
        calls.append(("hold", *a))                   # like Coordinator.hold: records the owner
        c.store.merge_data(a[0], a[1], hold={"owner": a[2], "reason": a[3]})
    c.hold = fake_hold
    c.release_hold = lambda *a: calls.append(("release", *a))
    e = PQ.add(c, "game", "patch", "prep: x",
               source=make_patch(root, tmp_path, "x", {"src/x.txt": "x\n"}),
               hold_tasks=["T1"], hold_minutes=7)
    land(c, P)
    owner = f"prep_queue:{e['id']}"
    assert calls[0][:3] == ("hold", "game", "T1") and calls[0][3] == owner and calls[0][5] == 7
    assert calls[-1] == ("release", "game", "T1", owner)


def test_validation(game, tmp_path):
    c, P, root, *_ = game
    with pytest.raises(PQ.PrepError):
        PQ.add(c, "game", "patch", "m", source=str(tmp_path / "missing.patch"))
    with pytest.raises(PQ.PrepError):
        PQ.add(c, "game", "zip", "m", source="x")
    with pytest.raises(PQ.PrepError):
        PQ.add(c, "game", "script", "m")
    with pytest.raises(PQ.PrepError):
        PQ.add(c, "game", "branch", "", source="feat")
    with pytest.raises(PQ.PrepError):
        PQ.parse_after(["explode"])
    with pytest.raises(PQ.PrepError):
        PQ.parse_after(["copy:C:/x=>../outside"])
    assert PQ.parse_after(["reload_kilo", " resume:T1 "]) == ["reload_kilo", "resume:T1"]
    e = PQ.add(c, "game", "patch", "m", patch_text="diff --git a/x b/x\n")
    assert os.path.isfile(e["source"]) and e["source"].startswith(c.state_dir)


# ------------------------------------------------------------------ crash + engine + settings

def test_interrupted_batch_is_rolled_back_on_the_next_tick(game, tmp_path):
    c, P, root, *_ = game
    start = git(root, "rev-parse", "HEAD")
    e = PQ.add(c, "game", "patch", "prep: x",
               source=make_patch(root, tmp_path, "x", {"src/x.txt": "x\n"}))
    (root / "src" / "half.txt").write_text("h\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "half a batch")
    c.store.set_meta("prep_queue:inflight:game", json.dumps({"batch": "pb-dead", "start": start,
                                                            "ids": [e["id"]]}))
    res = land(c, P)
    assert res["result"] == "landed"
    assert not (root / "src" / "half.txt").exists()
    assert git(root, "rev-parse", "HEAD~1") == start
    assert [x for x in c.store.events("game") if x["kind"] == "prep_recovered"]


def test_engine_tick_lands_and_respects_the_interval_and_settings(game, tmp_path):
    c, P, root, remote, pushes = game
    e = PQ.add(c, "game", "patch", "prep: x",
               source=make_patch(root, tmp_path, "x", {"src/x.txt": "x\n"}))
    P.profile.raw["prep_queue"] = {"enabled": False}
    c.tick()
    assert PQ.get(c.store, e["id"])["status"] == "pending"
    P.profile.raw["prep_queue"] = {"enabled": True, "check_every_seconds": 3600}
    c.store.set_meta("prep_queue:state:game", json.dumps({"ts": c.clock()}))
    c.tick()
    assert PQ.get(c.store, e["id"])["status"] == "pending"   # checked recently: waits
    PQ.run_now(c, "game")
    c.tick()
    assert PQ.get(c.store, e["id"])["status"] == "landed"
    assert PQ.snapshot(c, P)["last"]["result"] == "landed"


def test_publish_none_lands_without_pushing(tmp_path):
    root, remote = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root, publish="none")
    c = coordinator(tmp_path, [prof], {"game": {}})
    P = c.projects["game"]
    e = PQ.add(c, "game", "patch", "prep: x",
               source=make_patch(root, tmp_path, "x", {"src/x.txt": "x\n"}))
    res = land(c, P)
    assert res["result"] == "landed" and "publish mode none" in res["detail"]
    assert remote_head(remote) != PQ.get(c.store, e["id"])["sha"]


# ------------------------------------------------------------------ control channel

class Ctl:
    def __init__(self):
        self.queue, self.reports, self.beats = [], [], []

    def heartbeat(self, p):
        self.beats.append(p)

    def claim_actions(self, source_id, projects, limit=20):
        q, self.queue = self.queue, []
        return q

    def action_status(self, action_id, source_id, status, detail, result=None):
        self.reports.append({"action_id": action_id, "status": status, "detail": detail,
                             "result": result or {}})


def test_control_actions_and_heartbeat_block(game, tmp_path):
    c, P, root, *_ = game
    ch = ControlChannel(c, foundry=Ctl(), source_id="coordinator:test", heartbeat_every_s=0)
    n = [0]

    def send(action, **payload):
        n[0] += 1
        aid = f"act-pq-{n[0]}"
        ch.foundry.queue.append({"action_id": aid, "kind": action, "project_id": "game",
                                 "task_id": "", "actor": "victor", "payload": payload,
                                 "expected": {}})
        ch.poll()
        return [r for r in ch.foundry.reports if r["action_id"] == aid][-1]

    p = make_patch(root, tmp_path, "x", {"src/x.txt": "x\n"})
    r1 = send("prep_add", kind="patch", source=p, message="prep: one",
              after_actions=["reload_kilo"])
    assert r1["status"] == "applied", r1
    r2 = send("prep_add", kind="patch", patch_text=open(p).read(), message="prep: two",
              after_task="T1", after_state="COMPLETE")
    assert r2["status"] == "applied", r2
    assert send("prep_add", kind="patch", message="no source")["status"] == "rejected"
    r = send("prep_reorder", id=r2["result"]["id"], position=1)
    assert r["status"] == "applied" and r["result"]["order"] == 1
    assert send("prep_remove", id=r1["result"]["id"])["status"] == "applied"
    assert send("prep_remove", id="pq-0000000000")["status"] == "rejected"
    assert send("prep_run_now")["status"] == "applied"
    beat = ch.foundry.beats[-1]
    assert "prep_queue" in beat["features"]
    for k in ("prep_add", "prep_remove", "prep_reorder", "prep_run_now"):
        assert k in beat["source"]["capabilities"]["actions"]
    block = beat["prep_queue"]["game"]
    assert [e["message"] for e in block["entries"]] == ["prep: two"]
    assert block["entries"][0]["condition"] == {"after_task": "T1", "state": "COMPLETE"}
    assert block["enabled"] is True and block["pending"] == 1


def test_cli_prep_commands(game, tmp_path, capsys):
    from foundry_coordinator import cli
    from foundry_coordinator.adapters.scripted import ScriptedClient
    c, P, root, *_ = game
    cl = {"game": ScriptedClient({}, project_id="game")}
    p = make_patch(root, tmp_path, "x", {"src/x.txt": "x\n"})
    base = ["--state", str(tmp_path / "state"), "--profiles", str(tmp_path / "profiles")]
    assert cli.main(base + ["prep", "add", "game", "--source", p, "-m", "prep: cli",
                            "--body", "para one", "--body", "Trailer: y",
                            "--after-task", "T1", "--after-action", "reload_kilo",
                            "--hold", "T2"], clients=cl) == 0
    out = capsys.readouterr().out
    eid = out.split()[1]
    e = PQ.get(c.store, eid)
    assert e["body"] == "para one\n\nTrailer: y" and e["hold_tasks"] == ["T2"]
    assert cli.main(base + ["prep", "list", "game", "--log"], clients=cl) == 0
    assert "prep: cli" in capsys.readouterr().out
    assert cli.main(base + ["prep", "status", "game"], clients=cl) == 0
    assert "waits for T1 COMPLETE" in capsys.readouterr().out
    assert cli.main(base + ["prep", "run-now", "game"], clients=cl) == 0
    assert cli.main(base + ["prep", "remove", "game", eid], clients=cl) == 0
    assert PQ.get(c.store, eid)["status"] == "removed"
    assert cli.main(base + ["prep", "add", "game", "--kind", "script", "-m", "x"],
                    clients=cl) == 2


def test_migration_script_queues_the_eleven_watcher_entries_in_order(game, tmp_path, capsys):
    import importlib.util
    from foundry_coordinator import cli
    from foundry_coordinator.adapters.scripted import ScriptedClient
    c, P, root, *_ = game
    spec = importlib.util.spec_from_file_location(
        "migrate_au_watcher", os.path.join(os.path.dirname(__file__), "..", "..", "tools",
                                           "migrate_au_watcher.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    dummy = tmp_path / "dummy.patch"
    dummy.write_text("x\n")
    m.ENTRIES = [(k, str(dummy) if k == "patch" else s, *rest) for k, s, *rest in m.ENTRIES]
    m.PROJECT = "game"
    cmds = m.commands(["x"])
    assert len(cmds) == 11
    cl = {"game": ScriptedClient({}, project_id="game")}
    base = ["--state", str(tmp_path / "state"), "--profiles", str(tmp_path / "profiles")]
    for cmd in cmds:
        assert cli.main(base + cmd[1:], clients=cl) == 0, cmd
    rows = PQ.entries(c.store, "game")
    assert rows[0]["message"].startswith("prep: ENVIRONMENT.md: Studio auto-reopen")
    assert rows[1]["message"].startswith("prep: parallel Lune runner")
    assert rows[0]["kind"] == "script" and rows[0]["paths"] == ["docs/ENVIRONMENT.md"]
    assert all(r["condition"] == {"after_task": "GF-GATE1", "state": "COMPLETE"} for r in rows)
    assert all(r["body"].endswith(m.CO) for r in rows)
    assert rows[-1]["message"] == "prep: automation audit fixes (owner-approved 2026-10-28)"
    assert rows[-1]["after_actions"][-2:] == ["reload_kilo", "resume:HK-CLOSE1"]
    assert rows[-1]["hold_tasks"] == ["HK-CLOSE1"]
    assert [r["message"] for r in rows[4:7]] == [
        f"prep: rules streamline phase {n} (owner-approved 2026-10-27)" for n in (1, 2, 3)]

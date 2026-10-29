"""A finished track whose integration of master conflicts on a real file gets a Lead repair
in its worktree (lead.py open_track_repair / track_repair_step, tracks/merge.py _conflict):
the repair session resolves and commits, the merge retries and completes; a Lead that cannot
run leaves the decision ("stop this track"); a timeout with the Lead unavailable stops the
track AND raises a notice naming the COMPLETE sprints whose work did not merge.

2026-10-28 replay: track backend-1 with sprint S57a was COMPLETE, its rebase conflicted,
LeadManager had no open_track_repair, and the decision timeout stopped the track: S57a's
work never reached master. Fakes only: the fake `kilo serve` through the real KiloClient,
a fake Foundry, temp git repos."""

import os
import time

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.adapters import build_project
from foundry_coordinator.devtools.fake_kilo import FakeKilo
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.lead import LeadManager, kilo_decision, track_repair_permissions
from foundry_coordinator.profile import load
from foundry_coordinator.store import Store
from foundry_coordinator.tracks.merge import DECISION_AFTER_REPAIR, DECISION_DEFAULT
from tests.coordinator.helpers import git, make_repo, write_profile
from tests.coordinator.test_lead_manager import LEAD, LEAD_TOML, LeadFoundry, RecKilo

TASKS = [
    {"id": "T1", "title": "main sprint", "spec": "Write src/a.txt.", "scope": ["src/a.txt"]},
    {"id": "S57a", "title": "backend sprint", "spec": "Write src/a.txt.",
     "scope": ["src/a.txt"]},
]
TRACK = "backend-1"
EXTRA = """
[automation]
decision_timeout_minutes = 30
"""


@pytest.fixture()
def env(tmp_path):
    fake = FakeKilo()
    url = fake.start()
    fake.config["provider"]["foundry"]["models"][LEAD] = {"limit": {"context": 200000,
                                                                    "output": 32000}}
    fake.config["agent"]["lead"] = {"model": f"foundry/{LEAD}", "mode": "primary"}
    root, _ = make_repo(tmp_path, "game", TASKS, extra_files={"src/a.txt": "start OK\n"})
    prof = load(str(write_profile(tmp_path, "game", root, publish="none", review=False,
                                  extra=LEAD_TOML.format(enabled="true", full="false")
                                  + EXTRA)))
    P = build_project(prof, str(tmp_path / "state"),
                      client=RecKilo(url, password=fake.password), foundry=LeadFoundry())
    c = Coordinator(Store(str(tmp_path / "state" / "c.sqlite")), [P], str(tmp_path / "state"))
    lm = LeadManager(c)
    c.lead = lm
    prompts = []
    fake.on_prompt = lambda f, sid, text: prompts.append((sid, text))
    e = {"fake": fake, "c": c, "P": P, "root": root, "lm": lm, "prompts": prompts}
    yield e
    fake.stop()


def conflicting_track(e):
    """backend-1 finished S57a (COMPLETE) on src/a.txt while master changed the same line;
    the track is merging at the rebase stage."""
    c, P, root = e["c"], e["P"], e["root"]
    row = c.tracks.create(P, TRACK, ["S57a"])
    wt = row["worktree"]
    with open(os.path.join(wt, "src", "a.txt"), "w") as fh:
        fh.write("track: fast path OK\n")
    git(wt, "commit", "-q", "-am", "S57a: fast path")
    with open(os.path.join(root, "src", "a.txt"), "w") as fh:
        fh.write("master: guard OK\n")
    git(root, "commit", "-q", "-am", "T0: guard")
    c.approve("game", "S57a", [], "victor", track=TRACK)
    c.store.update_task("game", "S57a", state=S.COMPLETE)
    c.store.update_track("game", TRACK, state="merging")
    c.store.merge_track_data("game", TRACK, merge_stage="rebase")
    return c.store.track("game", TRACK)


def tick(e, n=1):
    for _ in range(n):
        e["c"].tracks.tick(e["P"])


def track(e):
    return e["c"].store.track("game", TRACK)


def events(e, kind):
    return [x for x in e["c"].store.events("game", 400) if x["kind"] == kind]


def wait_for(e, cond, limit=60):
    for _ in range(limit):
        tick(e)
        if cond():
            return True
        time.sleep(0.05)
    return False


def resolve_and_commit(wt):
    with open(os.path.join(wt, "src", "a.txt"), "w") as fh:
        fh.write("master: guard OK\ntrack: fast path OK\n")
    git(wt, "add", "src/a.txt")
    git(wt, "commit", "-q", "--no-edit")


def test_replay_backend1_conflict_gets_a_lead_repair_and_the_merge_completes(env):
    e = env
    conflicting_track(e)
    tick(e)                                              # rebase -> conflict -> Lead case
    row = track(e)
    lr = row["data"]["conflict"]["lead_repair"]
    assert lr["status"] == "running" and lr["case_id"].startswith("rc-")
    d = row["data"]["decision"]
    assert d["status"] == "open" and d["default"] == DECISION_AFTER_REPAIR
    assert DECISION_AFTER_REPAIR in d["options"] and lr["case_id"] in d["question"]
    ev = events(e, "track_conflict")[0]["detail"]
    assert ev["lead_repair"] is True and ev["default"] == DECISION_AFTER_REPAIR
    case = e["lm"].case(lr["case_id"])
    assert case["task_id"] == "S57a" and case["state"] == "REPAIRING"
    assert case["trigger"]["track"] == TRACK and case["trigger"]["files"] == ["src/a.txt"]
    # the coordinator merges master into the track; ONE session in the worktree resolves it
    assert wait_for(e, lambda: e["prompts"])
    wt = row["worktree"]
    assert git(wt, "diff", "--name-only", "--diff-filter=U") == "src/a.txt"
    start = e["P"].client.starts[-1]
    assert os.path.realpath(start["directory"]) == os.path.realpath(wt)
    assert "src/a.txt" in e["prompts"][0][1] and "BOTH sides" in e["prompts"][0][1]
    # the decision times out while the repair runs: the default waits, nothing is dropped
    e["c"].store.merge_track_data("game", TRACK, decision={**d, "deadline_ts": 1.0})
    tick(e)
    row = track(e)
    assert row["state"] == "merging"
    assert row["data"]["decision"]["answer"] == DECISION_AFTER_REPAIR
    assert row["data"]["conflict"]["lead_repair"]["status"] == "running"
    # the (fake) Lead resolves keeping both sides and commits; the merge retries and lands
    resolve_and_commit(wt)
    e["fake"].finish(e["prompts"][0][0], "Resolved src/a.txt keeping both lines.")
    assert wait_for(e, lambda: track(e)["state"] == "merged", limit=120), track(e)["data"]
    text = open(os.path.join(e["root"], "src", "a.txt")).read()
    assert "master: guard" in text and "track: fast path" in text
    case = e["lm"].case(lr["case_id"])
    assert case["state"] == "RESOLVED"
    assert [a for a in e["lm"].actions(case["case_id"]) if a["kind"] == "track_repair"
            and a["status"] == "done"]
    assert events(e, "track_lead_repaired") and track(e)["data"]["lead_repaired"]["case_id"] \
        == case["case_id"]
    # the session counts toward the lead-call caps
    assert e["lm"].lifetime_calls("game", "S57a") == 1
    assert e["lm"].task_totals("game", "S57a")["lead_calls"] == 1
    assert not [x for x in events(e, "operator_notice")
                if x["detail"].get("category") == "track_work_unmerged"]


def test_a_repair_that_does_not_conclude_the_merge_leaves_the_decision(env):
    e = env
    row = conflicting_track(e)
    pre = git(row["worktree"], "rev-parse", "HEAD")
    tick(e)
    assert wait_for(e, lambda: e["prompts"])
    e["fake"].finish(e["prompts"][0][0], "I could not decide.")       # no commit
    assert wait_for(e, lambda: track(e)["data"]["conflict"]["lead_repair"]["status"]
                    == "failed")
    row = track(e)
    d = row["data"]["decision"]
    assert d["status"] == "open" and d["default"] == DECISION_DEFAULT
    assert DECISION_AFTER_REPAIR not in d["options"]
    assert "not concluded" in row["data"]["conflict"]["lead_repair"]["error"]
    # the worktree is back at the track's own head, no merge in progress
    assert git(row["worktree"], "rev-parse", "HEAD") == pre
    assert git(row["worktree"], "status", "--porcelain", "--untracked-files=no") == ""
    case = e["lm"].case(row["data"]["conflict"]["lead_repair"]["case_id"])
    assert case["state"] == "CANCELLED"


@pytest.mark.parametrize("why", ["readiness", "caps"])
def test_a_lead_that_cannot_run_leaves_the_decision(env, why):
    e = env
    lm = e["lm"]
    if why == "readiness":
        lm.readiness = lambda P, fresh=False: [{
            "name": "lead_route", "ok": False, "cls": "transient", "blocking": True,
            "detail": "quota window spent"}]
    else:
        lm.hard_cap = lambda P: 3
        lm.lifetime_calls = lambda pid, tid: 3
    conflicting_track(e)
    tick(e)
    row = track(e)
    lr = row["data"]["conflict"]["lead_repair"]
    assert isinstance(lr, dict) and lr.get("error") and not lr.get("case_id")
    d = row["data"]["decision"]
    assert d["default"] == DECISION_DEFAULT and DECISION_AFTER_REPAIR not in d["options"]
    assert "No Lead repair" in d["question"]
    assert events(e, "lead_track_repair_refused")
    assert not e["c"].store.q("SELECT 1 FROM recovery_cases")
    assert not e["prompts"]


def test_timeout_with_the_lead_unavailable_stops_the_track_and_names_unmerged_sprints(env):
    e = env
    e["lm"].readiness = lambda P, fresh=False: [{
        "name": "lead_route", "ok": False, "cls": "transient", "blocking": True,
        "detail": "route down"}]
    conflicting_track(e)
    tick(e)
    d = track(e)["data"]["decision"]
    e["c"].store.merge_track_data("game", TRACK, decision={**d, "deadline_ts": 1.0})
    tick(e)
    row = track(e)
    assert row["state"] == "stopped" and row["data"]["decision"]["answer"] == DECISION_DEFAULT
    notes = [x for x in events(e, "operator_notice")
             if x["detail"].get("category") == "track_work_unmerged"]
    assert len(notes) == 1
    n = notes[0]
    assert n["task_id"] == "S57a" and n["detail"]["unmerged_sprints"] == ["S57a"]
    assert n["detail"]["alert_class"] == "self_repair_exhausted"
    assert "S57a" in n["detail"]["text"] and "did NOT reach main" in n["detail"]["text"]
    from foundry_router.control_center import event_alert_class
    assert event_alert_class("operator_notice", n["detail"]) == "self_repair_exhausted"


def test_track_repair_session_is_confined_to_the_worktree_and_cannot_move_the_branch(env):
    rules = track_repair_permissions(env["P"])
    assert kilo_decision(rules, "external_directory", "C:/elsewhere/*") == "deny"
    for cmd in ("git push origin HEAD", "git rebase main", "git reset --hard HEAD~1",
                "git checkout -b other", "git merge main", "git switch main"):
        assert kilo_decision(rules, "bash", cmd) == "deny", cmd
    for cmd in ("git add src/a.txt", "git commit --no-edit", "git checkout --theirs src/a.txt"):
        assert kilo_decision(rules, "bash", cmd) == "allow", cmd

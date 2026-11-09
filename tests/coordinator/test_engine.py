"""Coordinator engine: lifecycle, evidence gates, enforcement and recovery.

Every test uses real git repositories (with a bare remote), the generic
profile/adapter, a scripted coding client and a fake Foundry, so the state
machine, the mechanical checks and the store are exercised exactly as in
production; only the model is replaced by a script."""

import foundry_coordinator.engine as engine_mod
import json
import os
import sys

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.adapters.scripted import ScriptedClient
from foundry_coordinator.profile import ProfileError, check_isolation, load
from foundry_coordinator.store import Store
from tests.coordinator.helpers import (FakeFoundry, by_task, coordinator, denial, git, make_repo,
                                       review_json, run_until, write_ok, write_profile)

T1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt containing OK.",
      "scope": ["src/a.txt"]}
T2 = {"id": "T2", "title": "Second thing", "spec": "Write src/b.txt containing OK.",
      "scope": ["src/b.txt"], "deps": ["T1"]}


def _state(c, pid, tid):
    return c.store.task(pid, tid)["state"]


def test_preapproved_followup_waits_for_parent(tmp_path):
    root, _ = make_repo(tmp_path, "game", [T1, T2])
    profile = write_profile(tmp_path, "game", root,
                            extra='[auto_queue_followups]\nT1 = ["T2"]\n')
    c = coordinator(tmp_path, [profile], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "Victor")
    assert c.store.task("game", "T2") is None
    c.tick()
    assert c.store.queue_entry("game", "T2")["deps"] == ["T1"]
    assert c.store.task("game", "T2")["state"] == S.QUEUED
    c.tick()
    assert len([q for q in c.store.queue("game") if q["task_id"] == "T2"]) == 1


@pytest.fixture()
def game(tmp_path):
    root, remote = make_repo(tmp_path, "game", [T1, T2])
    prof = write_profile(tmp_path, "game", root)
    return tmp_path, root, remote, prof


# -- the whole lifecycle -----------------------------------------------------------

def test_full_lifecycle_binds_every_step_to_the_exact_commit(game):
    tmp, root, remote, prof = game
    ff = FakeFoundry()
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")}, foundry={"game": ff})
    c.approve("game", "T1", [], "victor", "pilot")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)
    t = c.store.task("game", "T1")
    head = git(root, "rev-parse", "HEAD")
    assert t["head_sha"] == head == t["reviewed_sha"]
    assert git(root, "ls-remote", "origin", "refs/heads/main").split()[0] == head
    path = [h["to_state"] for h in c.store.history("game", "T1")]
    assert path == [S.QUEUED, S.READY, S.IMPLEMENTING, S.VERIFYING, S.REVIEWING, S.APPROVED,
                    S.CLOSING, S.COMPLETE]
    # verification evidence is bound to the revision and has raw output on disk
    ver = c.store.verification(t["verification_id"], "game", "T1")
    assert {v["revision"] for v in ver} == {head}
    pc = next(v for v in ver if v["check_name"] == "project-check")
    assert pc["result"] == "PASS" and os.path.isfile(pc["evidence"])
    # the review named the exact head; the session was bound to project/task/run/role
    rev, = c.store.reviews("game", "T1")
    assert rev["valid"] and rev["reviewed_sha"] == head and rev["persona"] == "Foundry-Kilo-Sonnet"
    impl = next(b for b in ff.binds if b["role"] == "implementer")
    assert (impl["project_id"], impl["task_id"], impl["persona"]) == \
        ("game", "T1", "Foundry-Kilo-Local") and impl["run_id"].startswith("T1-a1-")
    assert any(b["role"] == "reviewer" and b["run_id"] == impl["run_id"] for b in ff.binds)
    assert ff.tasks[("game", "T1")]["state"] == S.COMPLETE
    assert ff.tasks[("game", "T1")]["findings"] == []
    ended = {sid for sid, _ in ff.ended}
    assert {b["session_id"] for b in ff.binds} <= ended          # implement + review bindings closed
    assert c.store.locks() == []
    # the model's own report is recorded, but only as advice
    assert t["data"]["last_report"]["status"] == "DONE"


def test_model_saying_done_is_never_enough(game):
    tmp, root, _, prof = game
    calls = []

    def liar(ctx):
        calls.append(ctx["attempt"])
        if ctx["attempt"] == 1:      # claims DONE without committing anything
            return {"final": '```coordinator-report\n{"status": "DONE"}\n```'}
        if ctx["attempt"] == 2:      # commits, but the project check fails (no OK)
            return {"writes": {"src/a.txt": "nope"}, "commit": "T1: try",
                    "final": '```coordinator-report\n{"status": "DONE"}\n```'}
        assert "check project-check: FAIL" in ctx["prompt"]      # repair sees the real failure
        return {"writes": {"src/a.txt": "OK now"}, "commit": "T1: fix"}
    c = coordinator(tmp, [prof], {"game": liar})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)
    hist = [(h["from_state"], h["to_state"]) for h in c.store.history("game", "T1")]
    assert hist.count((S.VERIFYING, S.CHANGES_REQUIRED)) == 2
    assert calls == [1, 2, 3]
    first = c.store.history("game", "T1")[4]["evidence"]
    assert any("no commit since the task started" in f for f in first["findings"])


def test_scope_is_never_widened_and_attempts_run_out(game):
    tmp, root, _, prof = game

    def wanderer(ctx):
        return {"writes": {"src/a.txt": "OK", "src/other.txt": "OK"}, "commit": "T1: extra"}
    c = coordinator(tmp, [prof], {"game": wanderer})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.FAILED)
    t = c.store.task("game", "T1")
    assert t["attempt"] == 3 and "max_attempts" in c.store.history("game", "T1")[-1]["evidence"]["reason"]
    fails = [h for h in c.store.history("game", "T1") if h["to_state"] == S.CHANGES_REQUIRED]
    assert all(any("changed outside the task's files: src/other.txt" in f
                   for f in h["evidence"]["findings"]) for h in fails)
    assert c.store.locks() == []


def test_reviewer_changes_then_approval_of_the_new_commit(game):
    tmp, root, _, prof = game
    seen = []

    def reviewer(packet):
        seen.append(packet)
        if len(seen) == 1:
            return review_json(packet, "CHANGES_REQUIRED", findings=[
                {"severity": "major", "file": "src/a.txt", "line": 1, "message": "say OK twice"}])
        return review_json(packet)

    def agent(ctx):
        if "say OK twice" in ctx["prompt"]:
            assert "reviewer text is advice, not permission" in ctx["prompt"]
            return {"writes": {"src/a.txt": "OK OK"}, "commit": "T1: per review"}
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: first"}
    ff = FakeFoundry(reviewer)
    c = coordinator(tmp, [prof], {"game": agent}, foundry={"game": ff})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)
    revs = c.store.reviews("game", "T1")
    assert [r["verdict"] for r in revs] == ["CHANGES_REQUIRED", "APPROVED"]
    assert revs[0]["reviewed_sha"] != revs[1]["reviewed_sha"] == git(root, "rev-parse", "HEAD")


def test_invalid_reviews_are_rejected_then_block(game):
    tmp, _, _, prof = game
    bad = iter([lambda p: review_json(p, sha="0" * 40),             # wrong commit
                lambda p: review_json(p, task="T9")])               # wrong task
    ff = FakeFoundry(lambda p: next(bad)(p))
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")}, foundry={"game": ff})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.BLOCKED)
    errs = [r["error"] for r in c.store.reviews("game", "T1")]
    assert "not the head" in errs[0] and "not 'T1'" in errs[1]
    assert "invalid results" in c.store.task("game", "T1")["blocker"]


def test_approval_with_blocking_findings_is_not_an_approval(game):
    tmp, _, _, prof = game
    n = []

    def reviewer(p):
        n.append(1)
        if len(n) == 1:
            return review_json(p, "APPROVED", findings=[
                {"severity": "blocker", "file": "../etc/passwd", "message": "unsafe"}])
        return review_json(p)
    def agent(ctx):
        return {"writes": {"src/a.txt": f"OK {ctx['attempt']}"}, "commit": "T1: work"}
    c = coordinator(tmp, [prof], {"game": agent}, foundry={"game": FakeFoundry(reviewer)})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)
    first = c.store.reviews("game", "T1")[0]["result"]
    assert first["verdict"] == "CHANGES_REQUIRED"
    assert first["findings"][0]["file"] == ""                 # path outside the repo dropped


# -- budgets, denials and pauses ------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_review_budget_denial_pauses_and_resumes_after_reset(game):
    tmp, _, _, prof = game
    ff = FakeFoundry()
    ff.deny = denial("budget", retry=120)
    clk = Clock()
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")}, foundry={"game": ff}, clock=clk)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.PAUSED)
    t = c.store.task("game", "T1")
    assert t["resume_state"] == S.REVIEWING and t["pause_until"] == pytest.approx(clk.t + 120)
    c.tick()
    assert _state(c, "game", "T1") == S.PAUSED                # not before the reset
    clk.t += 121
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)
    assert c.store.task("game", "T1")["attempt"] == 1         # a budget stop isn't an attempt


def test_pause_switch_waits_for_an_operator(game):
    tmp, _, _, prof = game
    ff = FakeFoundry()
    ff.deny = denial("pause")
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")}, foundry={"game": ff})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.PAUSED)
    assert c.store.task("game", "T1")["pause_until"] is None
    for _ in range(3):
        c.tick()
    assert _state(c, "game", "T1") == S.PAUSED
    c.resume("game", "T1")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)


def test_coding_session_denied_by_foundry_pauses_then_continues_same_session(game):
    tmp, _, _, prof = game
    prompts = []

    def agent(ctx):
        prompts.append((ctx["session_id"], ctx["prompt"][:40]))
        if len(prompts) == 1:
            return {"error": 'APIError 429 {"error": {"code": "budget_denied", '
                             '"foundry_category": "budget", "reset_seconds": 30}}'}
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: done"}
    clk = Clock()
    client = ScriptedClient(agent)
    c = coordinator(tmp, [prof], {"game": client}, clock=clk)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.PAUSED)
    assert c.store.task("game", "T1")["resume_state"] == S.IMPLEMENTING
    clk.t += 31
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)
    assert prompts[0][0] == prompts[1][0]                     # same session continued
    assert prompts[1][1].startswith("Continue task T1")
    assert c.store.task("game", "T1")["attempt"] == 1


def test_config_refusal_blocks(game):
    tmp, _, _, prof = game
    ff = FakeFoundry()
    ff.deny = denial("config")
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")}, foundry={"game": ff})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.BLOCKED)
    assert "config" in c.store.task("game", "T1")["blocker"]


# -- dependencies, fresh sessions, one writer -------------------------------------------

def test_dependencies_and_a_fresh_session_per_task(game):
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": by_task({"T1": write_ok("src/a.txt"),
                                                   "T2": write_ok("src/b.txt")})})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", ["T1"], "victor")
    seen_t2_states = set()

    def done():
        seen_t2_states.add(_state(c, "game", "T2"))
        return _state(c, "game", "T2") == S.COMPLETE
    assert run_until(c, done)
    # T2 never started before T1 completed
    t1_done = next(h["ts"] for h in c.store.history("game", "T1") if h["to_state"] == S.COMPLETE)
    t2_start = next(h["ts"] for h in c.store.history("game", "T2") if h["to_state"] == S.READY)
    assert t2_start >= t1_done
    s1 = {s["session_id"] for s in c.store.sessions("game", "T1")}
    s2 = {s["session_id"] for s in c.store.sessions("game", "T2")}
    assert s1 and s2 and not (s1 & s2)


def test_undeclared_dependency_from_the_task_source_blocks(game):
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    c.approve("game", "T2", [], "victor")                      # T2 needs T1 per tasks.json
    assert run_until(c, lambda: _state(c, "game", "T2") == S.BLOCKED)
    assert "needs T1" in c.store.task("game", "T2")["blocker"]


def test_a_source_dependency_already_on_the_queue_is_waited_for(game):
    """2026-10-29 AS-R7: its plan named AS-P5 (approved, running) but its queue deps did
    not; it BLOCKED and opened a lead case. It now gets the dep and waits."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": by_task({"T1": write_ok("src/a.txt"),
                                                   "T2": write_ok("src/b.txt")})})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")                      # T2 needs T1 per tasks.json
    seen = set()

    def done():
        seen.add(_state(c, "game", "T2"))
        return _state(c, "game", "T2") == S.COMPLETE
    assert run_until(c, done)
    assert S.BLOCKED not in seen
    assert c.store.queue_entry("game", "T2")["deps"] == ["T1"]
    t1_done = next(h["ts"] for h in c.store.history("game", "T1") if h["to_state"] == S.COMPLETE)
    t2_ready = next(h["ts"] for h in c.store.history("game", "T2") if h["to_state"] == S.READY)
    assert t2_ready >= t1_done


def test_an_unblocked_sprint_still_waits_for_its_plan_dependency(game):
    """2026-10-29 AS-R7: unblocked to READY while AS-P5 (named in its plan, on the queue)
    was not done; it started at once. A first start now waits for such a dep."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": by_task({"T1": write_ok("src/a.txt"),
                                                   "T2": write_ok("src/b.txt")})})
    c.approve("game", "T2", [], "victor")                      # T2 needs T1 per tasks.json
    c.store.transition("game", "T2", S.READY, {"deps_satisfied": ["(none)"]}, actor="test")
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T2") == S.COMPLETE)
    t1_done = next(h["ts"] for h in c.store.history("game", "T1") if h["to_state"] == S.COMPLETE)
    t2_impl = next(h["ts"] for h in c.store.history("game", "T2")
                   if h["to_state"] == S.IMPLEMENTING)
    assert t2_impl >= t1_done


def test_a_deps_entry_that_is_not_a_sprint_is_ignored(game):
    """AS-D13 2026-10-28 replay: the spec's deps named Q1, a CC0 asset pack. An id the
    adapter says is not a sprint of the project never blocks; a known one still does."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    P.adapter.is_known_task = lambda d: False                  # T1 is 'not a sprint' here
    c.approve("game", "T2", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T2") != S.QUEUED)
    t = c.store.task("game", "T2")
    assert t["state"] != S.BLOCKED and t["data"]["deps_not_tasks"] == ["T1"]


def test_one_writer_across_projects_and_fair_turns(tmp_path):
    r1, _ = make_repo(tmp_path, "g1", [T1])
    r2, _ = make_repo(tmp_path, "g2", [T1])
    p1, p2 = write_profile(tmp_path, "g1", r1), write_profile(tmp_path, "g2", r2)

    class Slow(ScriptedClient):
        def state(self, sid, *, directory, since_ts):
            s = self.sessions.get(sid)
            if s and s.get("pending") and s.setdefault("polls", 0) < 2:
                s["polls"] += 1
                from foundry_coordinator.adapters.base import SessionState
                return SessionState(status="busy")
            return super().state(sid, directory=directory, since_ts=since_ts)
    c = coordinator(tmp_path, [p1, p2], {"g1": Slow(write_ok("src/a.txt")),
                                         "g2": Slow(write_ok("src/a.txt"))})
    c.approve("g1", "T1", [], "v")
    c.approve("g2", "T1", [], "v")
    writing_at_once = 0
    for _ in range(40):
        c.tick()
        w = sum(1 for p in ("g1", "g2") if _state(c, p, "T1") == S.IMPLEMENTING)
        writing_at_once = max(writing_at_once, w)
        if all(_state(c, p, "T1") == S.COMPLETE for p in ("g1", "g2")):
            break
    assert writing_at_once == 1
    assert all(_state(c, p, "T1") == S.COMPLETE for p in ("g1", "g2"))


# -- human approval -------------------------------------------------------------------

VISUAL = """
[approval]
deps_accept_pending = ["visual"]
[[approval.rules]]
match = "visual"
kind = "visual"
stage = "after_close"
artifacts = ["art/{task}_*.png"]
"""


def test_visual_approval_after_close_bound_to_revision_and_artifacts(tmp_path):
    tv = {"id": "V1", "title": "Screen", "spec": "x", "scope": ["src/a.txt", "art/V1_shot.png"],
          "tags": ["visual"]}
    t2 = dict(T2, deps=["V1"])
    root, _ = make_repo(tmp_path, "vis", [tv, t2])
    prof = write_profile(tmp_path, "vis", root, extra=VISUAL)

    def agent(ctx):
        if ctx["task_id"] == "V1":
            return {"writes": {"src/a.txt": "OK", "art/V1_shot.png": "PNG1 OK"},
                    "commit": "V1: ui"}
        return {"writes": {"src/b.txt": "OK"}, "commit": "T2: next"}
    c = coordinator(tmp_path, [prof], {"vis": agent})
    c.approve("vis", "V1", [], "victor")
    c.approve("vis", "T2", ["V1"], "victor")
    assert run_until(c, lambda: _state(c, "vis", "V1") == S.AWAITING_HUMAN)
    t = c.store.task("vis", "V1")
    assert t["data"]["stage"] == "after_close" and t["awaiting"] == "approval: visual"
    assert git(root, "ls-remote", "origin", "refs/heads/main").split()[0] == t["head_sha"]
    # pending visual approval satisfies the dependency (profile says so), so T2 proceeds
    assert run_until(c, lambda: _state(c, "vis", "T2") == S.COMPLETE)
    # approval is bound to the artifacts as they are now: changing the PNG voids it
    c.approve_human("vis", "V1", "visual", "victor")
    (root / "art" / "V1_shot.png").write_text("PNG2 OK")
    c.tick()
    assert _state(c, "vis", "V1") == S.AWAITING_HUMAN
    (root / "art" / "V1_shot.png").write_text("PNG1 OK")
    c.tick()
    assert _state(c, "vis", "V1") == S.COMPLETE
    with pytest.raises(ValueError):
        c.approve_human("vis", "T2", "visual", "victor")


def test_before_close_approval_is_invalidated_by_a_later_code_change(tmp_path):
    root, _ = make_repo(tmp_path, "pub", [dict(T1, tags=["publish"])])
    prof = write_profile(tmp_path, "pub", root, extra="""
[[approval.rules]]
match = "publish"
kind = "publish"
stage = "before_close"
""")
    c = coordinator(tmp_path, [prof], {"pub": write_ok("src/a.txt")})
    c.approve("pub", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "pub", "T1") == S.AWAITING_HUMAN)
    c.approve_human("pub", "T1", "publish", "victor")
    # someone commits a code change before the coordinator closes it
    (root / "src" / "a.txt").write_text("OK changed")
    git(root, "commit", "-qam", "T1: sneak")
    c.tick()
    assert _state(c, "pub", "T1") == S.VERIFYING
    assert c.store.approvals("pub", "T1") == []                   # revoked
    assert "files changed after the review: src/a.txt" in \
        c.store.history("pub", "T1")[-1]["evidence"]["invalidated"]


# -- questions, controls, recovery --------------------------------------------------------

def test_repair_that_changes_nothing_is_not_re_reviewed(game):
    tmp, _, _, prof = game
    ff = FakeFoundry(lambda p: review_json(p, "CHANGES_REQUIRED", findings=[
        {"severity": "major", "message": "wrong"}]))
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")}, foundry={"game": ff})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.FAILED)
    assert len(ff.chats) == 1                                 # no paid duplicate reviews
    later = [h for h in c.store.history("game", "T1") if h["from_state"] == S.VERIFYING
             and h["to_state"] == S.CHANGES_REQUIRED]
    assert later and all(any("committed nothing" in f for f in h["evidence"]["findings"])
                         for h in later)


def test_permission_ask_waits_for_a_human_answer(game):
    """A permission ask reaches a person only when Kilo refuses the coordinator's
    own reject (W1 follow-up: implementer permission asks are auto-rejected)."""
    tmp, _, _, prof = game

    def agent(ctx):
        if not ctx.get("answers"):
            return {"question": {"text": "edit src/a.txt?", "kind": "permission"}}
        assert ctx["answers"] == [["once"]]
        return {"writes": {"src/a.txt": "OK green"}, "commit": "T1: green"}
    from tests.coordinator.helpers import refuse_auto_reject
    c = coordinator(tmp, [prof], {"game": refuse_auto_reject(ScriptedClient(agent, "game"))})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.AWAITING_HUMAN)
    assert "edit src/a.txt" in c.store.task("game", "T1")["awaiting"]
    for _ in range(2):
        c.tick()
    assert _state(c, "game", "T1") == S.AWAITING_HUMAN
    c.answer("game", "T1", [["once"]])
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)


def test_implementer_question_is_answered_by_the_coordinator(game):
    """W1.1 (Victor 2026-10-22): an implementer never waits on a person. Kilo's
    question tool is denied in its session rules; if one still comes, the
    coordinator answers it with the standing instruction and the session goes on."""
    tmp, _, _, prof = game
    seen = []

    def agent(ctx):
        if not ctx.get("answers"):
            return {"question": {"text": "Use blue or green?"}}
        seen.append(ctx["answers"])
        return {"writes": {"src/a.txt": "OK green"}, "commit": "T1: green"}
    c = coordinator(tmp, [prof], {"game": agent})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)
    assert not any(h["to_state"] == S.AWAITING_HUMAN for h in c.store.history("game", "T1"))
    assert seen and "BLOCKED report" in seen[0][0][0] and "options" in seen[0][0][0]
    ev = c.store.q("SELECT detail FROM events WHERE kind='question_auto_answered'")
    assert len(ev) == 1 and "blue or green" in ev[0]["detail"]
    client = c.projects["game"].client
    rules = next(iter(client.sessions.values()))["permissions"]
    assert rules[-1] == {"permission": "question", "pattern": "*", "action": "deny"}


def test_permission_reply_without_new_kilo_turn_gets_one_continuation(game):
    tmp, _, _, prof = game
    from foundry_coordinator.adapters.base import SessionState

    def agent(ctx):
        if not ctx.get("answers"):
            return {"question": {"text": "May I write a scratch file?"}}
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: finish"}

    class RejectedWrite(ScriptedClient):
        def state(self, sid, *, directory, since_ts):
            result = super().state(sid, directory=directory, since_ts=since_ts)
            if result.status == "attention":
                result.question["kind"] = "permission"
            return result

        def answer(self, sid, request_id, answers, *, directory, kind="question"):
            self.answers[sid] = answers
            # Kilo may finish the rejected tool turn with no final report and
            # without starting a new assistant turn of its own.
            self.sessions[sid]["pending"] = False
            self.sessions[sid]["state"] = SessionState(status="idle")

    client = RejectedWrite(agent)
    c = coordinator(tmp, [prof], {"game": client})
    c.approve("game", "T1", [], "victor")
    # W1 follow-up: the coordinator rejects the ask itself; the rejected turn ended
    # without a report, so the same session is continued once (no person, no new session)
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)
    sid = c.store.task("game", "T1")["session_id"]
    assert client.answers[sid][0] == "reject"
    assert len(client.sessions[sid]["prompts"]) == 2
    assert not any(h["to_state"] == S.AWAITING_HUMAN for h in c.store.history("game", "T1"))
    assert c.store.task("game", "T1")["sessions"] == 1


def test_idle_unadmitted_prompt_recovers_before_session_timeout(game):
    tmp, _, _, prof = game
    from foundry_coordinator.adapters.base import SessionState

    class NeverAdmitted(ScriptedClient):
        def state(self, sid, *, directory, since_ts):
            return SessionState(status="busy", unanswered=True,
                                error="waiting for the prompt to be admitted")

    client = NeverAdmitted(write_ok("src/a.txt"))
    c = coordinator(tmp, [prof], {"game": client})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.IMPLEMENTING)
    sid = c.store.task("game", "T1")["session_id"]
    c.store.update_session(sid, prompt_ts=c.clock() - 200)
    c.tick()
    assert _state(c, "game", "T1") == S.CHANGES_REQUIRED
    assert c.store.session(sid)["outcome"] == "prompt_not_admitted"
    assert c.store.task("game", "T1")["data"]["free_retry"] is True


def test_a_stalled_turn_ends_the_session_after_the_infra_window(game):
    """2026-11-01: a forced Kilo restart cut 4 turns mid-reply; their unfinished replies read
    as busy and held the Strata unit and 3 Blender units until the 4-hour timeout."""
    tmp, _, _, prof = game
    from foundry_coordinator.adapters.base import SessionState

    class Cut(ScriptedClient):
        stalled = True

        def state(self, sid, *, directory, since_ts):
            if self.stalled:
                return SessionState(status="busy", stalled=True)
            return SessionState(status="busy")

    client = Cut(write_ok("src/a.txt"))
    c = coordinator(tmp, [prof], {"game": client})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.IMPLEMENTING)
    sid = c.store.task("game", "T1")["session_id"]
    c.tick()
    mark = c.store.task("game", "T1")["data"]["stall_since"]
    assert mark["session"] == sid and _state(c, "game", "T1") == S.IMPLEMENTING
    client.stalled = False                                       # a live turn again: cleared
    c.tick()
    assert not c.store.task("game", "T1")["data"].get("stall_since")
    client.stalled = True
    c.tick()
    c.store.merge_data("game", "T1", stall_since={"session": sid, "ts": c.clock() - 301})
    c.tick()
    assert _state(c, "game", "T1") == S.CHANGES_REQUIRED
    assert c.store.session(sid)["outcome"] == "stalled"
    assert c.store.task("game", "T1")["data"]["free_retry"] is True


def test_pause_resume_cancel(game):
    tmp, _, _, prof = game

    class Hang(ScriptedClient):
        def state(self, sid, *, directory, since_ts):
            from foundry_coordinator.adapters.base import SessionState
            return SessionState(status="busy")
    client = Hang(write_ok("src/a.txt"))
    aborted = []
    client.abort = lambda sid, directory: aborted.append(sid)
    c = coordinator(tmp, [prof], {"game": client})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.IMPLEMENTING)
    sid = c.store.task("game", "T1")["session_id"]
    c.pause("game", "T1", "lunch")
    assert aborted == [sid] and _state(c, "game", "T1") == S.PAUSED
    assert not any(l["resource"].startswith("writer:") for l in c.store.locks())
    c.cancel("game", "T1", "not needed")
    assert _state(c, "game", "T1") == S.CANCELLED and c.store.locks() == []
    with pytest.raises(S.TransitionError):
        c.store.transition("game", "T1", S.READY, {"resolution": "x"})


def test_restart_resumes_without_duplicate_sessions(game):
    tmp, _, _, prof = game
    starts = []

    class Slow(ScriptedClient):
        def start_session(self, **kw):
            sid = super().start_session(**kw)
            starts.append(sid)
            return sid

        def state(self, sid, *, directory, since_ts):
            from foundry_coordinator.adapters.base import SessionState
            s = self.sessions[sid]
            if s.get("pending") and not getattr(self, "go", False):
                return SessionState(status="busy")
            return super().state(sid, directory=directory, since_ts=since_ts)
    client = Slow(write_ok("src/a.txt"))          # Kilo keeps running across the restart
    c1 = coordinator(tmp, [prof], {"game": client})
    c1.approve("game", "T1", [], "victor")
    assert run_until(c1, lambda: _state(c1, "game", "T1") == S.IMPLEMENTING)
    del c1                                        # coordinator process "crashes"
    client.go = True
    c2 = coordinator(tmp, [prof], {"game": client})
    assert run_until(c2, lambda: _state(c2, "game", "T1") == S.COMPLETE)
    assert len(starts) == 1


def test_lost_session_after_kilo_restart_is_a_free_retry(game):
    tmp, _, _, prof = game

    class Busy(ScriptedClient):
        def state(self, sid, *, directory, since_ts):
            from foundry_coordinator.adapters.base import SessionState
            return SessionState(status="busy")
    c1 = coordinator(tmp, [prof], {"game": Busy(write_ok("src/a.txt"))})
    c1.approve("game", "T1", [], "victor")
    assert run_until(c1, lambda: _state(c1, "game", "T1") == S.IMPLEMENTING)
    c2 = coordinator(tmp, [prof], {"game": ScriptedClient(write_ok("src/a.txt"))})  # new Kilo
    assert run_until(c2, lambda: _state(c2, "game", "T1") == S.COMPLETE)
    t = c2.store.task("game", "T1")
    assert t["attempt"] == 1 and t["sessions"] == 2
    assert any("disappeared" in f for h in c2.store.history("game", "T1")
               for f in h["evidence"].get("findings", []))


def test_push_happens_once_per_commit(game):
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)
    head = git(root, "rev-parse", "HEAD")
    pushes = c.store.q("SELECT * FROM ops WHERE kind='push'")
    assert len(pushes) == 1 and pushes[0]["key"].endswith(head)


# -- workspace safety -------------------------------------------------------------------

def test_uncommitted_work_is_never_discarded(game):
    tmp, root, _, prof = game
    (root / "notes.md").write_text("my unrelated wip")         # unrelated: preserved
    (root / "src").mkdir(exist_ok=True)
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)
    assert (root / "notes.md").read_text() == "my unrelated wip"
    pres = [v for v in c.store.verification(c.store.task("game", "T1")["verification_id"],
                                            "game", "T1")
            if v["check_name"] == "coordinator.unrelated_preserved"]
    assert pres[0]["result"] == "PASS"
    # uncommitted work in the task's own files blocks before any session starts
    (root / "src" / "b.txt").write_text("half done")
    c.approve("game", "T2", ["T1"], "victor")
    assert run_until(c, lambda: _state(c, "game", "T2") == S.BLOCKED)
    assert "uncommitted changes in this task's files (src/b.txt)" in \
        c.store.task("game", "T2")["blocker"]
    assert c.store.sessions("game", "T2") == []


def test_task_s_own_uncommitted_work_goes_to_the_repair_session(game):
    tmp, root, _, prof = game

    def forgetful(ctx):
        if ctx["attempt"] == 1:                    # writes, forgets to commit, says DONE
            return {"writes": {"src/a.txt": "OK"},
                    "final": '```coordinator-report\n{"status": "DONE"}\n```'}
        assert "left these task files uncommitted: src/a.txt" in ctx["prompt"]
        return {"run": [["git", "add", "src/a.txt"], ["git", "commit", "-qm", "T1: commit it"]]}
    c = coordinator(tmp, [prof], {"game": forgetful})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)
    assert c.store.task("game", "T1")["attempt"] == 2


def test_explicit_handoff_preserves_original_and_finishes_existing_work(game):
    from pathlib import Path
    tmp, root, _, prof = game
    original = b"unfinished operator work\n"
    (root / 'src').mkdir(exist_ok=True)
    (root / 'src/a.txt').write_bytes(original)
    c = coordinator(tmp, [prof], {'game': write_ok('src/a.txt')})
    c.approve('game', 'T1', [], 'victor', adopt_existing=['src/a.txt'])
    inherited = c.store.task('game', 'T1')['data']['inherited_work']
    backup = Path(inherited['archive']) / 'files/src/a.txt'
    assert backup.read_bytes() == original
    assert run_until(c, lambda: _state(c, 'game', 'T1') == S.COMPLETE)
    assert backup.read_bytes() == original
    assert (root / 'src/a.txt').read_bytes() != original
    assert any(e['kind'] == 'work_adopted' for e in c.store.events('game'))


@pytest.mark.parametrize('change', ['workspace', 'backup', 'head', 'scope'])
def test_handoff_changes_block_before_any_session(game, change):
    from pathlib import Path
    tmp, root, _, prof = game
    (root / 'src').mkdir(exist_ok=True)
    (root / 'src/a.txt').write_text('unfinished original')
    c = coordinator(tmp, [prof], {'game': write_ok('src/a.txt')})
    c.approve('game', 'T1', [], 'victor', adopt_existing=['src/a.txt'])
    inherited = c.store.task('game', 'T1')['data']['inherited_work']
    if change == 'workspace':
        (root / 'src/a.txt').write_text('changed after approval')
    elif change == 'backup':
        (Path(inherited['archive']) / 'files/src/a.txt').write_text('changed backup')
    elif change == 'head':
        (root / 'other.md').write_text('another revision')
        git(root, 'add', 'other.md')
        git(root, 'commit', '-qm', 'other: changed revision')
    else:
        tasks = json.loads((root / 'tasks.json').read_text())
        tasks[0]['scope'].append('src/extra.txt')
        (root / 'tasks.json').write_text(json.dumps(tasks))
    assert run_until(c, lambda: _state(c, 'game', 'T1') == S.BLOCKED)
    assert 'handoff:' in c.store.task('game', 'T1')['blocker']
    assert c.store.sessions('game', 'T1') == []


def test_handoff_refuses_unrelated_file_and_keeps_queue_empty(game):
    tmp, root, _, prof = game
    (root / 'notes.md').write_text('unrelated')
    c = coordinator(tmp, [prof], {'game': write_ok('src/a.txt')})
    with pytest.raises(ValueError, match='dirty task-scope file'):
        c.approve('game', 'T1', [], 'victor', adopt_existing=['notes.md'])
    assert c.store.task('game', 'T1') is None


def test_partial_handoff_does_not_adopt_other_dirty_task_files(game):
    tmp, root, _, prof = game
    tasks = json.loads((root / 'tasks.json').read_text())
    tasks[0]['scope'].append('src/extra.txt')
    (root / 'tasks.json').write_text(json.dumps(tasks))
    (root / 'src').mkdir(exist_ok=True)
    (root / 'src/a.txt').write_text('approved handoff')
    (root / 'src/extra.txt').write_text('not handed off')
    c = coordinator(tmp, [prof], {'game': write_ok('src/a.txt')})
    c.approve('game', 'T1', [], 'victor', adopt_existing=['src/a.txt'])
    assert run_until(c, lambda: _state(c, 'game', 'T1') == S.BLOCKED)
    assert 'src/extra.txt' in c.store.task('game', 'T1')['blocker']
    assert c.store.sessions('game', 'T1') == []


def test_unchanged_shared_closeout_wip_is_preserved_not_claimed(game):
    tmp, root, _, prof = game
    text = prof.read_text()
    old = 'exempt_paths = ["docs/progress/{task}_*"]'
    assert old in text
    prof.write_text(text.replace(old, 'exempt_paths = ["docs/progress/{task}_*", "docs/progress/state/*"]'))
    (root / 'docs/progress/state').mkdir(parents=True)
    shared = root / 'docs/progress/state/DECISIONS.md'
    shared.write_text('another sprint WIP')
    c = coordinator(tmp, [prof], {'game': write_ok('src/a.txt')})
    c.approve('game', 'T1', [], 'victor')
    assert run_until(c, lambda: _state(c, 'game', 'T1') == S.COMPLETE)
    assert shared.read_text() == 'another sprint WIP'


def test_session_touching_unrelated_wip_fails_verification(game):
    tmp, root, _, prof = game
    (root / "notes.md").write_text("wip")

    def clumsy(ctx):
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: x",
                "run": [[sys.executable, "-c",
                         "from pathlib import Path; Path('notes.md').write_text('changed')"]]}
    c = coordinator(tmp, [prof], {"game": clumsy})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: c.store.task("game", "T1")["attempt"] >= 1 and
                     _state(c, "game", "T1") == S.CHANGES_REQUIRED)
    f = c.store.history("game", "T1")[-1]["evidence"]["findings"]
    assert any("pre-existing uncommitted changes were modified: notes.md" in x for x in f)


def test_coordinator_rewriting_kilo_config_mid_sprint_is_not_the_sessions(game):
    """AS-D13 2026-10-28 replay: `tracks set-needs` rewrote the worktree's (untracked)
    kilo.jsonc and left kilo.jsonc.bak-* beside it; unrelated_preserved blamed the session."""
    tmp, root, _, prof = game
    (root / "kilo.jsonc").write_text('{"mcp": {}}')

    def work(ctx):
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: x",
                "run": [[sys.executable, "-c",
                         "from pathlib import Path; Path('kilo.jsonc').write_text('{}'); "
                         "Path('kilo.jsonc.bak-20261028-181531').write_text('{}')"]]}
    c = coordinator(tmp, [prof], {"game": work})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)


def test_unknown_required_check_blocks(tmp_path):
    root, _ = make_repo(tmp_path, "u", [T1])
    prof = write_profile(tmp_path, "u", root, checks='''
[[verify.checks]]
name = "studio-gate"
command = ["definitely-not-a-real-binary-xyz"]
''')
    c = coordinator(tmp_path, [prof], {"u": write_ok("src/a.txt")})
    c.approve("u", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "u", "T1") == S.BLOCKED)
    assert "could not be run: studio-gate" in c.store.task("u", "T1")["blocker"]


# -- project isolation --------------------------------------------------------------------

def test_cross_project_isolation(tmp_path):
    r1, _ = make_repo(tmp_path, "a1", [T1])
    r2, _ = make_repo(tmp_path, "b2", [T1])
    p1, p2 = write_profile(tmp_path, "a1", r1), write_profile(tmp_path, "b2", r2)
    c = coordinator(tmp_path, [p1, p2], {"a1": write_ok("src/a.txt"), "b2": write_ok("src/a.txt")})
    with pytest.raises(ValueError, match="another project"):
        c.approve("a1", "T1", ["b2:T1"], "victor")
    c.approve("a1", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "a1", "T1") == S.COMPLETE)
    t = c.store.task("a1", "T1")
    # project b2's same-named task can't see a1's verification or review
    assert c.store.verification(t["verification_id"], "b2", "T1") == []
    assert c.store.review(t["review_id"], "b2", "T1") is None
    assert c.store.reviews("b2", "T1") == []
    # b2 runs on its own: its first review is its own, against its own repo
    c.approve("b2", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "b2", "T1") == S.COMPLETE)
    # (the two fixture repos are byte-identical, so the commit sha is the same:
    # a1's review of that sha still doesn't count for b2)
    assert c.store.task("b2", "T1")["head_sha"] == git(r2, "rev-parse", "HEAD")
    b2rev, = c.store.reviews("b2", "T1")
    assert b2rev["review_id"] != t["review_id"] and b2rev["project_id"] == "b2"
    # two profiles on one workspace are refused
    p3 = write_profile(tmp_path, "c3", r1)
    with pytest.raises(ProfileError, match="share a workspace"):
        check_isolation([load(str(p1)), load(str(p3))])


def test_profile_validation(tmp_path):
    root, _ = make_repo(tmp_path, "pv", [T1])
    p = write_profile(tmp_path, "pv", root)
    text = p.read_text()
    p.write_text(text.replace('url = "http://foundry.invalid"',
                              'url = "http://foundry.invalid"\napi_key = "sk-abcdefghijklmnop123"'))
    with pytest.raises(ProfileError, match="secrets must not be in a profile"):
        load(str(p))
    p.write_text(text.replace("reviewer = ", "wizard = "))
    with pytest.raises(ProfileError, match="unknown roles"):
        load(str(p))
    p.write_text(text.replace('mode = "push"', 'mode = "push"\nforce = true'))
    with pytest.raises(ProfileError, match="force"):
        load(str(p))


def test_every_transition_is_checked():
    for (frm, to), need in S.TRANSITIONS.items():
        with pytest.raises(S.TransitionError):
            S.check(frm, to, {})
    with pytest.raises(S.TransitionError):
        S.check(S.QUEUED, S.COMPLETE, {"anything": 1})
    with pytest.raises(S.TransitionError):
        S.check(S.VERIFYING, S.REVIEWING, {"verification_id": "v", "head_sha": "h",
                                          "all_required_pass": False})
    S.check(S.REVIEWING, S.APPROVED, {"review_id": "r", "reviewed_commit_sha": "abc"})
    # a lead assignment resumes as a repair round: the edge needs the findings
    S.check(S.BLOCKED, S.CHANGES_REQUIRED, {"resolution": "lead case", "findings": ["fix it"]})
    with pytest.raises(S.TransitionError):
        S.check(S.BLOCKED, S.CHANGES_REQUIRED, {"resolution": "lead case"})
    S.check(S.IMPLEMENTING, S.PAUSED, {"reason": "x", "resume_state": S.IMPLEMENTING})
    with pytest.raises(S.TransitionError):
        S.check(S.COMPLETE, S.PAUSED, {"reason": "x", "resume_state": S.READY})


def test_store_owner_lock(tmp_path):
    s = Store(str(tmp_path / "x.sqlite"))
    me = s.claim_owner()
    s2 = Store(str(tmp_path / "x.sqlite"))
    s2.set_meta("owner", json.dumps({"id": "otherhost:1", "beat": __import__("time").time()}))
    with pytest.raises(RuntimeError, match="another coordinator"):
        s.claim_owner()
    assert s.claim_owner(force=True)
    del me


def test_store_owner_lock_of_a_dead_process_on_this_host_is_taken_over(tmp_path):
    """2026-10-22: a coordinator killed hard left its owner row; the next one refused to
    start. A fresh beat from a pid that no longer exists on this host is stale."""
    import os
    import socket
    import time
    s = Store(str(tmp_path / "x.sqlite"))
    s.set_meta("owner", json.dumps({"id": f"{socket.gethostname()}:4000000", "beat": time.time()}))
    assert s.claim_owner()                                  # dead pid: taken over
    s.set_meta("owner", json.dumps({"id": f"{socket.gethostname()}:{os.getpid()}",
                                    "beat": time.time()}))
    s2 = Store(str(tmp_path / "x.sqlite"))
    assert s2.claim_owner()                                 # our own id: fine
    s.set_meta("owner", json.dumps({"id": f"{socket.gethostname()}:0", "beat": time.time()}))
    with pytest.raises(RuntimeError, match="another coordinator"):
        Store(str(tmp_path / "x.sqlite")).claim_owner()    # pid 0 (System Idle) counts as alive


def test_a_started_main_sprint_keeps_the_checkout_until_it_finishes(game):
    """2026-10-29 replay: AS-P5 and UI-SKIN2 took turns in the main checkout between
    sessions and staled each other's captures, gates and scope for hours. A second main
    sprint waits while the earlier-started one is in flight; BLOCKED/PAUSED releases it."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")
    c.store.update_task("game", "T1", state=S.CHANGES_REQUIRED, started_ts=100.0)
    c.store.update_task("game", "T2", state=S.READY, started_ts=None)
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == "T1"
    assert c._main_sprint_in_flight(P, c.store.task("game", "T1")) == ""
    c.store.update_task("game", "T1", state=S.BLOCKED)
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""


def test_a_long_waiting_landing_drains_new_main_sessions(game):
    """2026-10-29 replay: back-to-back main sprints starved the assets-local track merge and
    two prep landings from 21:50 on. A landing waiting longer than landing_drain_minutes
    stops new main sessions until it lands."""
    import json as _json
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    assert c._landing_drain(P) == ""
    old = c.clock() - 3600
    c.store.x("INSERT INTO tracks (project_id, name, kind, worktree, branch, base_sha, state, "
              "current_sprint, needs, sprints, created_by, created_ts, updated_ts, data) VALUES "
              "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("game", "assets", "parallel", "wt", "track/assets", "x", "merging", "", "[]",
               "[]", "t", old, old, _json.dumps({"merge_stage": "wait"})))
    assert "assets" in c._landing_drain(P)


def test_a_drained_sprint_gives_back_the_checkout_lock(game):
    """2026-10-29 replay: UI-SKIN2 (CHANGES_REQUIRED) kept the checkout lock while the drain
    held it, and the landing could not take the checkout."""
    from foundry_coordinator import prep_queue as PQ
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    PQ.ensure(c.store)
    c.approve("game", "T1", [], "victor")
    c.store.update_task("game", "T1", state=S.READY)
    c.store.acquire(c._workspace_locks(P), "game", "T1")
    old = c.clock() - 3600
    c.store.x("INSERT INTO prep_queue (id, project_id, ord, kind, source, message, condition, "
              "status, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("pq-test", "game", 1, "patch", "x.patch", "prep: x", "{}", "pending", old, old))
    assert "pq-test" in c._landing_drain(P)
    c._start_implementation(P, c.store.task("game", "T1"))
    assert not c.store.holds("game", "T1")
    assert "landing drain" in (c.store.task("game", "T1")["next_action"] or "")


def test_a_drained_sprint_commits_its_left_over_plan_history_first(game):
    """2026-11-01 AS-V04: its session wrote <ID>_PLAN_HISTORY.md after its last commit and the
    gate path went to CHANGES_REQUIRED without VERIFYING (where the auto-commit ran). The dirty
    checkout was no safe point, so pq-17d5c583ba and AS-V04 waited on each other."""
    import os as _os
    from foundry_coordinator import prep_queue as PQ
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    PQ.ensure(c.store)
    c.approve("game", "T1", [], "victor")
    c.store.update_task("game", "T1", state=S.CHANGES_REQUIRED)
    old = c.clock() - 3600
    c.store.x("INSERT INTO prep_queue (id, project_id, ord, kind, source, message, condition, "
              "status, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("pq-test", "game", 1, "patch", "x.patch", "prep: x", "{}", "pending", old, old))
    _os.makedirs(_os.path.join(root, "docs", "progress"), exist_ok=True)
    with open(_os.path.join(root, "docs", "progress", "T1_PLAN_HISTORY.md"), "w") as f:
        f.write("history" + chr(10))
    head = P.ws.head()
    c._start_implementation(P, c.store.task("game", "T1"))
    assert "landing drain" in (c.store.task("game", "T1")["next_action"] or "")
    assert P.ws.head() != head
    assert "docs/progress/T1_PLAN_HISTORY.md" not in [p for _, p in P.ws.status()]


def test_a_sprint_that_yields_to_an_earlier_one_gives_back_the_checkout_lock(game):
    """2026-10-29 07:00 replay: AS-P5 (restarted by an attempt reset) yielded to the earlier
    started HK-CLEAN1 but kept the checkout lock; HK-CLEAN1 waited for the lock: deadlock."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")
    c.store.update_task("game", "T1", state=S.READY, started_ts=100.0)
    c.store.update_task("game", "T2", state=S.CHANGES_REQUIRED, started_ts=200.0)
    c.store.acquire(c._workspace_locks(P), "game", "T2")
    c._start_implementation(P, c.store.task("game", "T2"))
    assert not c.store.holds("game", "T2")
    assert "waiting for T1" in (c.store.task("game", "T2")["next_action"] or "")


def test_a_left_over_plan_history_is_committed_by_the_coordinator(game):
    """2026-10-29 AS-R7 (and AS-D13, AS-V03): plan_new.py's PLAN_HISTORY was left
    uncommitted; the clean check failed and a new session started. The coordinator now
    commits the sprint's own tool-written files ([automation] auto_commit_task_files)."""
    import os as _os
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    P = c.projects["game"]
    t = c.store.task("game", "T1")
    _os.makedirs(_os.path.join(root, "docs", "progress"), exist_ok=True)
    for name in ("T1_PLAN_HISTORY.md", "T2_PLAN_HISTORY.md"):
        with open(_os.path.join(root, "docs", "progress", name), "w") as f:
            f.write("history\n")
    head = P.ws.head()
    assert c._auto_commit_task_files(P, t) is True
    assert P.ws.head() != head
    dirty = [p for _, p in P.ws.status()]
    assert "docs/progress/T1_PLAN_HISTORY.md" not in dirty
    assert "docs/progress/T2_PLAN_HISTORY.md" in dirty        # another sprint's: left alone
    assert c._auto_commit_task_files(P, t) is False             # nothing left to commit


def test_a_local_model_sprint_takes_the_gpu_lock(game):
    """2026-10-29: AS-R8 (main, local) and AS-D11 (assets-2 track, holding gpu) ran local
    sessions at once; the local server refused AS-D11 three times, an attempt each."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    P = c.projects["game"]
    t = c.store.task("game", "T1")
    import foundry_coordinator.fast_lane as FL
    orig = FL.is_local_primary
    try:
        FL.is_local_primary = lambda P, tid: True
        assert "gpu" in c._session_locks(P, t)
        FL.is_local_primary = lambda P, tid: False
        assert "gpu" not in c._session_locks(P, t)
    finally:
        FL.is_local_primary = orig
    assert "gpu" not in c._session_locks(P)


def test_local_streams_let_that_many_local_sprints_run_at_once(game):
    """2026-11-01 Victor: "run the audio sprints in parallel with Strata as the implementer":
    resources.local_streams = 2 gives two local sessions a gpu unit each; the third waits."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    P = c.projects["game"]
    P.profile.raw.setdefault("resources", {})["local_streams"] = 2
    import foundry_coordinator.fast_lane as FL
    orig = FL.is_local_primary
    try:
        FL.is_local_primary = lambda P, tid: True
        got = []
        for tid in ("A1", "A2", "A3"):
            t = {"task_id": tid}
            locks = [r for r in c._session_locks(P, t) if r.split("#")[0] == "gpu"]
            held = c.store.acquire(locks, "game", tid)
            got.append((locks, held))
        assert got[0] == (["gpu"], None) and got[1] == (["gpu#2"], None)
        assert got[2][1] is not None                         # both units taken: it waits
        c.store.release("game", "A1", ["gpu"])
        assert c._session_locks(P, {"task_id": "A3"})[-1] == "gpu"
        assert c._session_locks(P, {"task_id": "A2"})[-1] == "gpu#2"   # keeps its unit
    finally:
        FL.is_local_primary = orig


def test_a_session_end_gives_back_an_idle_studio_slot_lock(game):
    """2026-11-01: AS-D08's slot gate took `slot:slot-1`; no session end released it, so the
    slot stayed held through review and CLOSE while AS-F30 waited for it."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    P = c.projects["game"]
    t = c.store.task("game", "T1")
    assert c.store.acquire(["slot:slot-1", "writer:0"], "game", "T1") is None
    orig = c.studio_calls.active
    try:
        c.studio_calls.active = lambda P, tid: True             # its gate still runs: keep it
        c._release_session(P, t)
        assert "slot:slot-1" in c.store.holds("game", "T1")
        assert "writer:0" not in c.store.holds("game", "T1")
        c.studio_calls.active = lambda P, tid: False            # the gate is over: give it back
        c._release_session(P, t)
        assert "slot:slot-1" not in c.store.holds("game", "T1")
    finally:
        c.studio_calls.active = orig


def test_a_close_takes_no_blender_unit_and_lets_go_of_a_held_slot(game):
    """2026-11-01: AS-D08 (CLOSING, docs only) waited for a Blender unit held by AS-F30, while
    AS-F30 waited for the Studio slot AS-D08 still held."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    P = c.projects["game"]
    P.profile.resources.append("blender")
    P.adapter.closeout_mode = lambda: "session"                  # AU closes in a CLOSE session
    c.approve("game", "T1", [], "victor")
    seen = []
    orig = c._acquire

    def spy(P_, t_, resources, writer=False):
        cur = c.store.task(P_.id, t_["task_id"]) or t_
        if cur.get("state") == S.CLOSING:
            seen.append((list(resources), list(c.store.holds(P_.id, t_["task_id"]))))
        return orig(P_, t_, resources, writer=writer)
    c._acquire = spy
    c.store.acquire(["slot:slot-1"], "game", "T1")              # left over from its gate
    assert run_until(c, lambda: _state(c, "game", "T1") == S.COMPLETE)
    assert seen, "the CLOSE start was not observed"
    for wanted, held in seen:
        assert not [r for r in wanted if r.split("#")[0] == "blender"]
        assert "slot:slot-1" not in held                         # given back before CLOSE


def test_close_locks_drop_blender_and_give_back_held_units_and_idle_slots(game):
    """2026-11-01: the templated closeout asked for `_session_locks` (with a Blender unit) and
    AS-D08 waited for one while keeping slot-1."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    P = c.projects["game"]
    t = c.store.task("game", "T1")
    c.store.acquire(["blender#2", "slot:slot-1", "writer:0"], "game", "T1")
    orig = c.studio_calls.active
    try:
        c.studio_calls.active = lambda P, tid: False
        out = c._close_locks(P, t, ["workspace:x", "blender", "blender#3", "writer:0"])
    finally:
        c.studio_calls.active = orig
    assert out == ["workspace:x", "writer:0"]
    assert c.store.holds("game", "T1") == ["writer:0"]
    src = open(os.path.join(os.path.dirname(engine_mod.__file__), "engine.py"), encoding="utf-8").read()
    body = src[src.index("def _closeout_template"):src.index("def ", src.index("def _closeout_template") + 10)]
    assert "_close_locks(" in body                              # the templated closeout uses it
    assert 'split("#", 1)[0] != "gpu"' in body                  # and never waits for the model


def test_a_local_fallback_hold_clears_while_the_frontier_serves_the_same_persona(game):
    """2026-11-01 AS-F28: one "local busy" after a connection reset held a frontier sprint on
    Strata for an hour while 4 sprints with the same persona were served by Claude/ChatGPT."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")
    P = c.projects["game"]
    import foundry_coordinator.fast_lane as FL
    orig = FL.is_local_primary
    try:
        FL.is_local_primary = lambda P, tid: False
        c.store.merge_data("game", "T1", local_fallback_until=c.clock() + 3600)
        t1 = c.store.task("game", "T1")
        # nobody else is served: the hold stands (Oct 30 case: the frontier was out)
        assert any(r.split("#")[0] == "gpu" for r in c._session_locks(P, t1))
        # T2 (same persona) has a session working without a gpu unit: the frontier serves
        c.store.update_task("game", "T2", state=S.IMPLEMENTING, next_action="implement session working")
        locks = c._session_locks(P, c.store.task("game", "T1"))
        assert not any(r.split("#")[0] == "gpu" for r in locks)
        assert not (c.store.task("game", "T1")["data"].get("local_fallback_until"))
        # a T2 that itself holds gpu (it is the local stream) proves nothing
        c.store.merge_data("game", "T1", local_fallback_until=c.clock() + 3600)
        c.store.acquire(["gpu"], "game", "T2")
        assert any(r.split("#")[0] == "gpu" for r in c._session_locks(P, c.store.task("game", "T1")))
    finally:
        FL.is_local_primary = orig


def test_a_gpu_unit_held_past_the_local_fallback_window_is_released_for_a_waiter(game):
    """v0.181.60: BAL-1 (2026-11-05) began a session at 07:32 inside a local-fallback window.
    It held `gpu` after the window ended at 08:16 while Claude served it again, and VR-AS02's
    local closeout waited 75 min. A waiter now releases that stale hold; a live window or a
    local-primary holder keeps the unit."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")
    P = c.projects["game"]
    import foundry_coordinator.fast_lane as FL
    orig = FL.is_local_primary
    try:
        FL.is_local_primary = lambda P, tid: False
        c.store.acquire(["gpu"], "game", "T1")
        c.store.merge_data("game", "T1", local_fallback_until=c.clock() + 600)
        t2 = c.store.task("game", "T2")
        assert c._acquire(P, t2, ["gpu"]) is not None              # window still open: waits
        c.store.merge_data("game", "T1", local_fallback_until=c.clock() - 1)
        assert c._acquire(P, t2, ["gpu"]) is None                  # window over: T2 gets it
        assert "gpu" not in c.store.holds("game", "T1")
        assert c.store.q("SELECT 1 FROM events WHERE task_id='T1' AND "
                         "kind='local_fallback_gpu_released'")
        # a local-primary holder keeps its unit
        c.store.release("game", "T2", ["gpu"])
        c.store.acquire(["gpu"], "game", "T1")
        FL.is_local_primary = lambda P, tid: tid == "T1"
        assert c._acquire(P, c.store.task("game", "T2"), ["gpu"]) is not None
    finally:
        FL.is_local_primary = orig


def test_a_track_sprint_sent_back_by_review_goes_before_a_new_sprint(game):
    """v0.181.61 (Victor 2026-11-05 "Yes, fixes first"): on a track, a new sprint starts
    while the last one waits for its visual approval (deps_accept_pending); when review
    sends that one back, the new sprint no longer starts ahead of its fix round."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")
    P = c.projects["game"]
    t2 = c.store.task("game", "T2")
    # main checkout: no track, nothing changes
    c.store.update_task("game", "T1", state=S.CHANGES_REQUIRED)
    assert c._track_fix_round_pending(P, t2) == ""
    # both on track vr-a: T1's fix round goes first
    c.store.x("UPDATE queue SET track='vr-a' WHERE project_id='game'")
    assert c._track_fix_round_pending(P, t2) == "T1"
    c.store.update_task("game", "T2", state=S.READY)
    c.store.merge_data("game", "T2", deps_dropped=["T1"])     # not a plan dependency here
    c._start_implementation(P, c.store.task("game", "T2"))
    t2 = c.store.task("game", "T2")
    assert t2["state"] == S.READY and "T1's fix round" in (t2["next_action"] or "")
    # T1 back at work (or done): T2 may start
    c.store.update_task("game", "T1", state=S.IMPLEMENTING)
    assert c._track_fix_round_pending(P, t2) == ""
    # [automation] track_fixes_first = false turns it off
    from foundry_coordinator import automation
    c.store.update_task("game", "T1", state=S.CHANGES_REQUIRED)
    automation.set_live(P, "track_fixes_first", False)
    assert c._track_fix_round_pending(P, t2) == ""


def test_a_reset_sprint_that_ran_before_does_not_wait_for_fix_rounds(game):
    """2026-11-08: an operator reset cleared VR-AS16's attempt and started_ts, so it looked
    new and waited 5 hours on vr-d behind VR-AS17..19's fix rounds at round 8/8. A sprint
    that ran before (it has a run_id) starts in its turn."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")
    P = c.projects["game"]
    c.store.x("UPDATE queue SET track='vr-a' WHERE project_id='game'")
    c.store.update_task("game", "T1", state=S.CHANGES_REQUIRED)
    c.store.update_task("game", "T2", state=S.READY, run_id="T2-a15-old")
    c.store.merge_data("game", "T2", deps_dropped=["T1"])
    c._start_implementation(P, c.store.task("game", "T2"))
    assert "fix round" not in (c.store.task("game", "T2")["next_action"] or "")


def test_a_local_fallback_hold_clears_when_a_probe_finds_the_frontier_back(game):
    """v0.181.63 (Victor 2026-11-05 "Yes, fix it"): BAL-1 kept a one-hour local hold after
    Claude answered again. During a hold, a tiny probe through the implementer persona (at
    most every local_fallback_probe_seconds) clears it when a non-local route answers."""
    from types import SimpleNamespace
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    P = c.projects["game"]
    import foundry_coordinator.fast_lane as FL
    orig = FL.is_local_primary
    served = ["strata"]
    calls = []

    def chat(persona, messages, **kw):
        calls.append(persona)
        return "OK", {"served_backend": served[0]}
    orig_foundry = P.foundry
    P.foundry = SimpleNamespace(
        persona_info=lambda p: {"routes": [{"backend": "meridian", "source": "subscription"},
                                           {"backend": "strata", "source": "local"}]},
        chat=chat)
    try:
        FL.is_local_primary = lambda P, tid: False
        c.store.merge_data("game", "T1", local_fallback_until=c.clock() + 3600)
        # still served locally: the hold stands
        assert any(r.split("#")[0] == "gpu" for r in c._session_locks(P, c.store.task("game", "T1")))
        assert len(calls) == 1
        # within the probe interval: no second call
        served[0] = "meridian"
        assert any(r.split("#")[0] == "gpu" for r in c._session_locks(P, c.store.task("game", "T1")))
        assert len(calls) == 1
        # the interval passed and the frontier answers: the hold clears
        c.store.merge_data("game", "T1", local_fallback_probe_ts=c.clock() - 601)
        locks = c._session_locks(P, c.store.task("game", "T1"))
        assert not any(r.split("#")[0] == "gpu" for r in locks) and len(calls) == 2
        assert not c.store.task("game", "T1")["data"].get("local_fallback_until")
    finally:
        FL.is_local_primary = orig
        P.foundry = orig_foundry


def test_a_short_decision_wait_keeps_the_sprints_gpu_unit(game):
    """2026-11-01 AS-A02: it stopped 3 minutes for a decision with a timeout default, gave up
    Strata, and AS-A10 took it for a whole turn. A decision that answers itself soon keeps the
    local-model unit; a decision with no deadline releases it as before."""
    from foundry_coordinator import selfheal
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    P = c.projects["game"]
    c.obligations  # schema
    now = c.clock()

    def decision(did, deadline):
        c.store.x("INSERT INTO decisions (decision_id, project_id, task_id, kind, question, "
                  "status, opened_ts, deadline_ts) VALUES (?,?,?,?,?,?,?,?)",
                  (did, "game", "T1", "implementer_blocked", "q", "open", now, deadline))

    decision("d-soon", now + 180)
    c.store.acquire(["gpu", "writer:0"] + c._workspace_locks(P), "game", "T1")
    c._lc_wait(P, c.store.task("game", "T1"), ["d-soon"], "why", S.READY)
    assert c.store.holds("game", "T1") == ["gpu"]                # Strata kept, the rest given back
    # the stale-lock sweep leaves it alone while the decision answers itself soon
    c.store.x("UPDATE locks SET acquired_ts=? WHERE task_id='T1'", (now - 7200,))
    selfheal.stale_locks(c, P)
    assert c.store.holds("game", "T1") == ["gpu"]

    c.store.release("game", "T1")
    c.store.update_task("game", "T1", state=S.IMPLEMENTING)
    decision("d-person", None)
    c.store.acquire(["gpu", "writer:0"], "game", "T1")
    c._lc_wait(P, c.store.task("game", "T1"), ["d-person"], "why", S.READY)
    assert c.store.holds("game", "T1") == []                     # a person's answer: released


def test_a_frontier_sprint_that_fell_to_a_busy_local_model_waits_for_the_gpu(game):
    """2026-10-30: Claude weekly 100% + ChatGPT capped: GF-LOOP1 and MV2-9 fell to the
    local model AS-D14 held and failed a session every few minutes."""
    from foundry_coordinator import infra
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    P = c.projects["game"]
    t = c.store.task("game", "T1")
    import foundry_coordinator.fast_lane as FL
    orig = FL.is_local_primary
    try:
        FL.is_local_primary = lambda P, tid: False
        assert "gpu" not in c._session_locks(P, t)
        msg = ("session ended with an error: backend 'strata' serves the main stream only "
               "and 1 session(s) are active (Foundry-Kilo-Local). Free in about 48s")
        assert infra.session_failure(c, P, t, msg, False) is True
        assert "gpu" in c._session_locks(P, c.store.task("game", "T1"))
        c.store.merge_data("game", "T1", local_fallback_until=c.clock() - 1)   # expired
        assert "gpu" not in c._session_locks(P, c.store.task("game", "T1"))
        infra.session_failure(c, P, t, "session ended with an error: socket hang up", False)
        assert "gpu" not in c._session_locks(P, c.store.task("game", "T1"))
    finally:
        FL.is_local_primary = orig


def test_a_busy_local_server_is_an_infra_failure():
    from foundry_coordinator.autoheal import infra_signature
    msg = ("session ended with an error: backend 'strata' serves the main stream only and 1 "
           "session(s) are active (Foundry-Kilo-Local). Free in about 48s")
    assert infra_signature(msg) == "local_busy"


def test_a_png_frame_rule_also_takes_jpg_frames():
    """2026-10-30 GF-LOOP1: the plan's frame was a .jpg; the visual rule listed *.png only,
    so the closed, published sprint was sent back ("no required visual frames")."""
    from foundry_coordinator.profile import _frame_globs, matches_any
    g = _frame_globs(["docs/evidence/{task}/*.png", "art/x.png"])
    assert g[:2] == ["docs/evidence/{task}/*.png", "art/x.png"]
    assert "docs/evidence/{task}/*.jpg" in g and "docs/evidence/{task}/*.jpeg" in g
    assert matches_any("docs/evidence/T1/a.jpg", [p.replace("{task}", "T1") for p in g])
    assert _frame_globs(["docs/evidence/{task}/*.png", "docs/evidence/{task}/*.jpg"]).count(
        "docs/evidence/{task}/*.jpg") == 1


def test_an_attempt_reset_keeps_the_sprints_place_in_the_checkout_line(game):
    """2026-10-30 GF-LOOP1: unblock --reset-attempts cleared started_ts; the published sprint
    looked unstarted and waited while AS-P7 and AS-J08 traded the checkout at each handoff."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    for tid in ("T1", "T2"):
        c.approve("game", tid, [], "victor")
    c.store.update_task("game", "T1", state=S.BLOCKED, started_ts=100.0, blocker="x")
    c.store.update_task("game", "T2", state=S.CHANGES_REQUIRED, started_ts=200.0)
    c.unblock("game", "T1", "fixed", reset_attempts=True)
    t1 = c.store.task("game", "T1")
    assert not t1["started_ts"] and t1["data"]["queue_ts"] == 100.0
    assert c._main_sprint_in_flight(P, t1) == ""                # T1 goes first
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == "T1"


def test_a_review_cut_at_max_tokens_retries_with_more_room():
    """2026-10-30 AS-P7/AS-J08: the local fallback's verdict was cut at max_tokens 4000
    (finish_reason=length) three times each and both sprints were BLOCKED."""
    from types import SimpleNamespace as NS
    from foundry_coordinator.engine import Coordinator
    from foundry_coordinator.adapters.foundry import FoundryError
    seen = []

    def chat(persona, msgs, session_id, schema, max_tokens):
        seen.append(max_tokens)
        if max_tokens < 16000:
            raise FoundryError("HTTP 502: structured output was incomplete (finish_reason=length)")
        return "{}", {}
    P = NS(foundry=NS(chat=chat))
    text, meta, err = Coordinator._review_call(P, {"persona": "x", "packet": "p", "rsid": "r",
                                                   "max_tokens": 4000})
    assert err is None and seen == [4000, 8000, 16000]

    def chat2(persona, msgs, session_id, schema, max_tokens):
        seen.append(max_tokens)
        raise FoundryError("HTTP 500: other")
    seen.clear()
    _, _, err = Coordinator._review_call(NS(foundry=NS(chat=chat2)),
                                         {"persona": "x", "packet": "p", "rsid": "r",
                                          "max_tokens": 4000})
    assert err is not None and seen == [4000]


def test_an_automatic_pause_keeps_the_main_checkout(game):
    """2026-10-30: GF-LOOP1 waited seconds on the model visual approval and AS-E09 took the
    main checkout for 1.5 h; a scheduled auto-heal retry let AS-J08 in the same way."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    for tid in ("T1", "T2"):
        c.approve("game", tid, [], "victor")
    P.profile.raw.setdefault("approval", {})["visual_model"] = True
    c.store.update_task("game", "T1", state=S.AWAITING_HUMAN, started_ts=100.0,
                        awaiting="approval: visual")
    c.store.update_task("game", "T2", state=S.READY, started_ts=None)
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == "T1"
    c.store.update_task("game", "T1", awaiting="question: which color?")     # a person's call
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""
    c.store.update_task("game", "T1", state=S.BLOCKED, blocker="auto-heal 1/2 (rojo_stale): x",
                        data={"auto_heal": {"retry_at": 999.0}})
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == "T1"
    c.store.update_task("game", "T1", blocker="lead recovery: DIAGNOSING", data={})
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""


def test_a_sprint_with_a_working_lead_case_keeps_the_main_checkout(game, monkeypatch):
    """2026-10-30 20:55-21:12: MV2-11 and GF-RT1 blocked into lead cases and the next main
    sprint took the checkout in the same tick; the lead waits for the workspace lock, so no
    lead call ran for 2 h and the 3-min decision timeout parked MV2-11 as 'lead unavailable'."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    for tid in ("T1", "T2"):
        c.approve("game", tid, [], "victor")
    cases = {"rc-1": {"case_id": "rc-1", "state": "DIAGNOSING", "paused": 0}}
    from types import SimpleNamespace
    monkeypatch.setattr(c, "lead", SimpleNamespace(case=lambda cid: cases.get(cid)), raising=False)
    c.store.update_task("game", "T1", state=S.BLOCKED, started_ts=100.0,
                        blocker="lead recovery: DIAGNOSING (case rc-1)",
                        data={"recovery_case": "rc-1"})
    c.store.update_task("game", "T2", state=S.READY, started_ts=None)
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == "T1"
    cases["rc-1"]["state"] = "REPAIRING"
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == "T1"
    for state, paused in (("DEFERRED", 0), ("NEEDS_VICTOR", 0), ("DIAGNOSING", 1)):
        cases["rc-1"].update(state=state, paused=paused)        # waiting on quota or a person
        assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""
    # 2026-10-31 MV2-V1: a short infra backoff keeps the place (GF-RT3 took the checkout in a
    # 5-minute deferral and the lead then waited on its lock); a long wait still gives it up
    cases["rc-1"].update(state="DEFERRED", paused=0, next_check_ts=c.clock() + 300)
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == "T1"
    cases["rc-1"].update(next_check_ts=c.clock() + 3600)
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""
    cases["rc-1"].update(next_check_ts=c.clock() + 300, paused=1)
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""
    # 2026-11-01 MV2-CL1/AS-P8: the lead's own decision to wait for another sprint gives the
    # checkout away even when the recheck is minutes off (each sprint waited on the other)
    cases["rc-1"].update(paused=0, next_check_ts=c.clock() + 300, validation={
        "deferred_reason": "lead: environment wait (the only red row is AS-P8's A12)"})
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""
    cases["rc-1"].update(validation={"deferred_reason": "Kilo did not admit the prompt (503)"})
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == "T1"
    cases["rc-1"].update(validation={})
    # the blocker names what the lead waits on
    text = c._lead_blocker_text({"case_id": "rc-1", "state": "DIAGNOSING",
                                 "validation": {"lead_waiting": "workspace lock held by game/T2"},
                                 "trigger": {"state": "BLOCKED", "reason": "review budget"}})
    assert "[lead waits: workspace lock held by game/T2]" in text and "rc-1" in text
    c.store.update_task("game", "T1", data={"recovery_case": "rc-gone"})
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""


def test_a_sprint_waiting_on_a_decision_that_answers_itself_soon_keeps_the_main_checkout(game):
    """2026-11-01 05:28: MV2-V1 blocked on a 3-minute-default decision, GF-AFK1 took the
    checkout, and when MV2-V1 resumed (earlier in line) GF-AFK1 sat mid-review behind it."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    for tid in ("T1", "T2"):
        c.approve("game", tid, [], "victor")
    from foundry_coordinator import obligations
    obligations.migrate(c.store)
    c.store.x("INSERT INTO decisions (decision_id, project_id, task_id, kind, question, status, "
              "opened_ts, deadline_ts) VALUES ('dec-1', 'game', 'T1', 'implementer_blocked', "
              "'q', 'open', ?, ?)", (c.clock(), c.clock() + 180))
    c.store.update_task("game", "T1", state=S.BLOCKED, started_ts=100.0,
                        blocker="waiting for operator decision dec-1",
                        data={"decision_wait": {"ids": ["dec-1"], "resume": "READY"}})
    c.store.update_task("game", "T2", state=S.READY, started_ts=None)
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == "T1"
    # no deadline: it waits for a person, so the next sprint goes
    c.store.x("UPDATE decisions SET deadline_ts=NULL WHERE decision_id='dec-1'")
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""
    # a far deadline: the same
    c.store.x("UPDATE decisions SET deadline_ts=? WHERE decision_id='dec-1'", (c.clock() + 7200,))
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""
    # answered: nothing left to wait for here
    c.store.x("UPDATE decisions SET deadline_ts=?, status='answered' WHERE decision_id='dec-1'",
              (c.clock() + 60,))
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""


def test_a_track_worktree_goes_to_the_waiter_first_in_queue_order(game, monkeypatch):
    """2026-11-08: VR-AS16 (queue position 278) waited for vr-d's worktree while VR-AS19,
    stepped later in the same tick, took it when VR-AS18 released it. The worktree lock
    goes in queue order like the gpu unit, among sprints waiting to start."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")
    P = c.projects["game"]
    monkeypatch.setattr(c.tracks, "is_track", lambda _P: True)
    ws = "workspace:c:\roblox\tracks\vr-d"
    c.store.update_task("game", "T1", state=S.READY)
    c.store.update_task("game", "T2", state=S.CHANGES_REQUIRED)
    c.store.acquire([ws], "game", "HOLDER")
    assert c._acquire(P, c.store.task("game", "T1"), [ws]) is not None
    c.store.release("game", "HOLDER")
    w = c._acquire(P, c.store.task("game", "T2"), [ws])
    assert w and w["task_id"] == "T1" and c.store.holds("game", "T2") == []
    assert c._acquire(P, c.store.task("game", "T1"), [ws]) is None
    # a sprint mid-gate (IMPLEMENTING) with an old note does not hold the line
    c.store.release("game", "T1")
    c.store.update_task("game", "T1", state=S.IMPLEMENTING)
    assert c._acquire(P, c.store.task("game", "T2"), [ws]) is None


def test_the_gpu_unit_goes_to_the_waiter_first_in_queue_order(game):
    """2026-11-02 AS-A02 (queue position 119) waited 30 min for Strata: the sprint giving it
    up released it during its own step, and the sprint stepped right after it in the same
    tick (AS-A22, AS-A21, positions 134/137) took it before AS-A02 looked again. A later
    sprint now steps aside for an earlier one already waiting."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")
    P = c.projects["game"]
    c.store.update_task("game", "T1", state=S.CHANGES_REQUIRED)    # between sessions
    c.store.update_task("game", "T2", state=S.READY)
    c.store.acquire(["gpu"], "game", "HOLDER")
    assert c._acquire(P, c.store.task("game", "T1"), ["gpu"]) is not None
    assert c.store.task("game", "T1")["next_action"].startswith("waiting for gpu")
    c.store.release("game", "HOLDER")                      # freed mid-tick, T2 steps next
    w = c._acquire(P, c.store.task("game", "T2"), ["gpu"])
    assert w and w["task_id"] == "T1"
    assert "T1 is first in line" in c.store.task("game", "T2")["next_action"]
    assert c.store.holds("game", "T2") == []
    assert c._acquire(P, c.store.task("game", "T1"), ["gpu"]) is None     # T1 takes it
    assert c.store.holds("game", "T1") == ["gpu"]
    # an earlier task's stale note (it stopped) does not hold the line
    c.store.release("game", "T1")
    c.store.update_task("game", "T1", state=S.BLOCKED, next_action="waiting for gpu (old)")
    assert c._acquire(P, c.store.task("game", "T2"), ["gpu"]) is None


def test_claude_agents_are_capped_and_the_cap_drops_at_the_usage_threshold(game):
    """Victor 2026-11-02: "keep 4 or 5 [Claude agents] right now ... after 75% usage on Claude,
    then it's only 1 local LLM and 2 Claude agents". A sprint whose implementer persona is a
    Claude persona takes a `claude` unit; the units drop from claude_agents to
    claude_agents_over once the router reports Claude at or over claude_usage_pct."""
    from foundry_coordinator import engine as EN
    from foundry_coordinator.tracks import resources as R
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    P = c.projects["game"]
    persona = P.profile.persona("implementer", "T1")
    t1 = c.store.task("game", "T1")
    assert not any(r.startswith("claude") for r in c._session_locks(P, t1))   # off by default
    P.profile.raw.setdefault("resources", {}).update(
        claude_agents=3, claude_agents_over=1, claude_usage_pct=75)
    R.CLAUDE_USAGE.pop("game", None)
    assert R.claude_settings(P.profile)["personas"] == [persona]   # default: implementer role
    assert R.capacity(P, "claude") == 3
    assert "claude" in c._session_locks(P, t1)

    # the router's answer for a Claude persona: the highest window counts
    answer = {"routes": [
        {"backend": "meridian", "accounts": [{"windows": [{"name": "5h", "used": 41.0},
                                                          {"name": "7d", "used": 78.5}]}]},
        {"backend": "cliproxyapi", "accounts": [{"windows": [{"name": "5h", "used": 99.0}]}]}]}
    assert EN.claude_used_pct(answer) == 78.5
    assert EN.claude_used_pct({"routes": []}) is None

    class Router:
        calls = 0

        def helper_admission(self, persona_, **kw):
            Router.calls += 1
            assert persona_ == persona
            return answer
    P.foundry = Router()
    c._refresh_claude_usage(P)
    assert R.CLAUDE_USAGE["game"]["pct"] == 78.5 and R.capacity(P, "claude") == 1
    c._refresh_claude_usage(P)                     # within CLAUDE_USAGE_EVERY_S: no new read
    assert Router.calls == 1
    ev = [e for e in c.store.q("SELECT detail FROM events WHERE kind='claude_agent_cap'")]
    assert len(ev) == 1 and '"cap": 1' in ev[0]["detail"]
    # a read with no Claude windows is recorded once (why the cap cannot switch)
    R.CLAUDE_USAGE["game"]["ts"] = 0
    answer_saved = dict(answer)
    answer.clear()
    answer["routes"] = [{"backend": "meridian", "state": "unknown", "accounts": []}]
    c._refresh_claude_usage(P)
    R.CLAUDE_USAGE["game"]["ts"] = 0
    c._refresh_claude_usage(P)
    un = c.store.q("SELECT detail FROM events WHERE kind='claude_usage_unread'")
    assert len(un) == 1 and "meridian" in un[0]["detail"]
    assert R.CLAUDE_USAGE["game"]["pct"] == 78.5           # the last reading stands
    answer.clear()
    answer.update(answer_saved)
    # 2026-11-02: a coordinator restart (the in-memory reading gone) while reads fail keeps
    # the last good reading from the store while it is fresh, so the cap stays down
    R.CLAUDE_USAGE.pop("game", None)
    answer.clear()
    answer["routes"] = [{"backend": "meridian", "state": "unknown", "accounts": []}]
    c._refresh_claude_usage(P)
    assert R.CLAUDE_USAGE["game"]["pct"] == 78.5 and R.capacity(P, "claude") == 1
    R.CLAUDE_USAGE.pop("game", None)                 # a saved reading older than 6 h is not used
    c.store.set_meta("claude_usage:game", json.dumps({"pct": 78.5, "read_ts": 1.0}))
    c._refresh_claude_usage(P)
    assert R.CLAUDE_USAGE["game"].get("pct") is None and R.capacity(P, "claude") == 3
    answer.clear()
    answer.update(answer_saved)
    R.CLAUDE_USAGE["game"] = {"pct": 78.5, "ts": c.clock()}

    # over the threshold: one unit; a second Claude sprint waits
    c.store.acquire(["claude"], "game", "OTHER")
    held = c._acquire(P, t1, ["claude"])
    assert held and c.store.holds("game", "T1") == []
    # below it again: the base cap; it takes the next unit
    R.CLAUDE_USAGE["game"]["pct"] = 30.0
    unit = R.pick_unit(c.store, P, "T1", "claude")
    assert unit == "claude#2" and c._acquire(P, t1, [unit]) is None
    # a sprint whose persona is not a Claude persona never takes a unit
    P.profile.raw["resources"]["claude_personas"] = ["Some-Local-Persona"]
    assert not any(r.startswith("claude") for r in c._session_locks(P, t1))
    R.CLAUDE_USAGE.pop("game", None)


def test_a_checkout_freed_mid_tick_goes_to_the_earlier_waiting_sprint(game):
    """2026-11-02 MV2-13: GF-COS1 completed mid-tick after GF-QUEEN1 (earlier in the queue)
    had been told to wait for it; MV2-13, at the queue's end, stepped later in the same tick,
    found the checkout free and took it for hours. A never-started main sprint does not take
    a checkout freed this tick while an earlier sprint was told to wait; the queue order
    decides on the next tick. A started sprint keeps its own place in line."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")
    c.store.update_task("game", "T1", state=S.READY, started_ts=None)
    c.store.update_task("game", "T2", state=S.READY, started_ts=None)
    # (the fixture's T2 depends on T1, so T1 plays the later sprint here)
    c._lane_yielded["game"] = ["T2"]          # T2 was told to wait earlier in this tick
    c._start_implementation(P, c.store.task("game", "T1"))
    t1 = c.store.task("game", "T1")
    assert t1["state"] == S.READY and not c.store.holds("game", "T1")
    assert "waiting for T2 (earlier in the queue)" in (t1["next_action"] or "")
    assert c._main_sprint_in_flight(P, t1) == ""          # nothing else holds the checkout
    # a started sprint is not held back by it
    c.store.update_task("game", "T1", started_ts=50.0)
    c._lane_yielded["game"] = ["T2"]
    c._start_implementation(P, c.store.task("game", "T1"))
    assert "earlier in the queue" not in (c.store.task("game", "T1")["next_action"] or "")


def test_a_sprint_that_becomes_ready_this_tick_keeps_its_place_in_line(tmp_path):
    """2026-11-02 22:12: AS-V12 completed; MAP-1 (earlier in the queue) only went QUEUED ->
    READY in its step, and S53 (later, already READY) took the freed checkout in the same tick.
    A main sprint that became READY this tick goes first on the next tick."""
    a = {"id": "A", "title": "Earlier", "spec": "Write src/a.txt.", "scope": ["src/a.txt"]}
    b = {"id": "B", "title": "Later", "spec": "Write src/b.txt.", "scope": ["src/b.txt"]}
    root, _ = make_repo(tmp_path, "game", [a, b])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "A", [], "victor")
    c.approve("game", "B", [], "victor")
    c.store.update_task("game", "A", state=S.QUEUED, started_ts=None)   # its deps just finished
    c.store.update_task("game", "B", state=S.READY, started_ts=None)
    c.tick()
    ta, tb = c.store.task("game", "A"), c.store.task("game", "B")
    assert ta["state"] == S.READY, ta["next_action"]
    assert tb["state"] == S.READY and not c.store.holds("game", "B")
    assert "waiting for A (earlier in the queue)" in (tb["next_action"] or "")
    c.tick()                                                    # next tick: A takes the lane
    assert c.store.task("game", "A")["state"] != S.READY, c.store.task("game", "A")["next_action"]
    assert c.store.task("game", "B")["state"] == S.READY


def test_a_dependency_a_person_removes_is_not_put_back_from_the_task_source(game):
    """2026-11-02 S55/S57b: the owner moved them ahead of his review and the beta; the operator
    removed AS-VR1 / GF-BETA1 from their deps, and the readiness check put them straight back from
    the plan's "Depends on" line (the AS-R7 rule), which would have deadlocked the polish phase.
    A dependency a person removes stays removed; the coordinator's own additions still happen."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", ["T1"], "victor")                    # the source also names T1
    c.set_deps("game", "T2", [], "victor")
    assert c.store.task("game", "T2")["data"].get("deps_dropped") == ["T1"]
    c.store.update_task("game", "T1", state=S.IMPLEMENTING)      # T1 live, not done
    c.tick()
    t2 = c.store.task("game", "T2")
    assert c.store.queue_entry("game", "T2")["deps"] == []
    assert t2["state"] != S.QUEUED or "waiting for T1" not in (t2["next_action"] or "")
    # a person adding it back clears the mark
    c.store.update_task("game", "T2", state=S.QUEUED)
    c.set_deps("game", "T2", ["T1"], "victor")
    assert not c.store.task("game", "T2")["data"].get("deps_dropped")


def test_a_dropped_dependency_does_not_hold_back_a_ready_sprint_at_its_start(game):
    """2026-11-02 S55: AS-VR1 was dropped (it waits for POL-7, which needs S55); the QUEUED
    check honoured that, but the start check re-read the plan and S55 sat READY 'waiting for
    AS-VR1 (PAUSED)' - a deadlock of the whole polish phase."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", ["T1"], "victor")                    # the source also names T1
    c.set_deps("game", "T2", [], "victor")
    c.store.update_task("game", "T1", state=S.PAUSED)            # held until much later
    c.store.update_task("game", "T2", state=S.READY, started_ts=None)
    assert c._unmet_start_deps(P, c.store.task("game", "T2")) == []
    c._start_implementation(P, c.store.task("game", "T2"))
    assert c.store.task("game", "T2")["state"] == S.IMPLEMENTING


def test_owner_inbox_drops_mid_sprint_are_not_the_sessions(tmp_path):
    """PT-3 2026-11-03 replay: the owner's concept-art chat dropped images into
    assets/source/incoming/ of the main checkout while PT-3 verified; unrelated_preserved
    blamed the session. [automation] owner_inbox folders are the owner's."""
    root, _ = make_repo(tmp_path, "inb", [T1])
    prof = write_profile(tmp_path, "inb", root, extra='''
[automation]
owner_inbox = ["inbox/**"]
''')

    def work(ctx):
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: x",
                "run": [[sys.executable, "-c",
                         "from pathlib import Path; Path('inbox/map').mkdir(parents=True); "
                         "Path('inbox/map/MAP-3D-D.png').write_text('png')"]]}
    c = coordinator(tmp_path, [prof], {"inb": work})
    c.approve("inb", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "inb", "T1") == S.COMPLETE)


def test_a_new_file_outside_the_inbox_still_fails_unrelated_preserved(tmp_path):
    root, _ = make_repo(tmp_path, "inc", [T1])
    prof = write_profile(tmp_path, "inc", root, extra='''
[automation]
owner_inbox = ["inbox/**"]
''')

    def work(ctx):
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: x",
                "run": [[sys.executable, "-c",
                         "from pathlib import Path; Path('stray.txt').write_text('x')"]]}
    c = coordinator(tmp_path, [prof], {"inc": work})
    c.approve("inc", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "inc", "T1") == S.CHANGES_REQUIRED)
    f = c.store.history("inc", "T1")[-1]["evidence"]["findings"]
    assert any("new uncommitted files outside the task: stray.txt" in x for x in f)


def test_a_sprint_back_from_a_real_stop_rejoins_behind_the_one_that_took_the_checkout(game):
    """2026-11-03: AS-F24 (started 19:49) blocked on a Studio sync; MAP-5 started at 20:08 and
    committed work in progress; AS-F24 came back and, still the earliest started, took the
    checkout first. Its Lune leg ran red on MAP-5's half-done specs and the two blocked each
    other. A sprint out on a real stop when another takes the checkout rejoins behind it."""
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    for tid in ("T1", "T2"):
        c.approve("game", tid, [], "victor")
    c.clock = lambda: 500.0
    # T1 started first, then left the lane on a real stop (a Lead case waiting on Studio)
    c.store.update_task("game", "T1", state=S.BLOCKED, started_ts=100.0,
                        blocker="lead recovery: DEFERRED", data={})
    c.store.update_task("game", "T2", state=S.READY, started_ts=490.0)
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""
    c._mark_lane_yields(P, c.store.task("game", "T2"))           # T2 takes the checkout
    assert [e for e in c.store.events("game", 50) if e["kind"] == "lane_place_given"]
    # T1 comes back READY; T2 (between sessions, CHANGES_REQUIRED) keeps the checkout
    c.store.update_task("game", "T1", state=S.READY, blocker=None)
    c.store.update_task("game", "T2", state=S.CHANGES_REQUIRED)
    assert c._main_sprint_in_flight(P, c.store.task("game", "T1")) == "T2"
    assert c._main_sprint_in_flight(P, c.store.task("game", "T2")) == ""
    # an automatic stop (auto-heal retry) keeps the place: no yield is recorded
    c.store.update_task("game", "T1", state=S.BLOCKED, blocker="auto-heal 1/2 (rojo_stale): x",
                        data={"auto_heal": {"retry_at": 999.0}, "lane_line_ts": None})
    c.store.update_task("game", "T2", state=S.READY)
    c._mark_lane_yields(P, c.store.task("game", "T2"))
    assert not (c.store.task("game", "T1")["data"] or {}).get("lane_line_ts")


def test_only_open_plan_steps_after_a_gate_run_is_a_free_build_continuation(tmp_path):
    """v0.181.62: BAL-1 (2026-11-05) ran its Studio gate mid-sprint, as its plan says;
    verification then failed only au.plan_steps (steps still open by design) and each round
    cost an attempt (3 -> 6). Now it is the partial-BUILD continuation: free, with the stall
    guard (the same open steps, or no new commit, BUILD_STALL_LIMIT times -> normal repair)."""
    root, _ = make_repo(tmp_path, "u", [T1])
    prof = write_profile(tmp_path, "u", root, checks='''
[[verify.checks]]
name = "au.plan_steps"
command = ["python", "-c", "import sys; sys.exit(1)"]
''')
    c = coordinator(tmp_path, [prof], {"u": write_ok("src/a.txt")})
    c.approve("u", "T1", [], "victor")
    assert run_until(c, lambda: c.store.q(
        "SELECT 1 FROM events WHERE task_id='T1' AND kind='build_progress_verify'"))
    first = c.store.q("SELECT detail FROM events WHERE task_id='T1' AND "
                      "kind='build_progress_verify' ORDER BY id LIMIT 1")
    assert first
    hist = [h for h in c.store.history("u", "T1") if h["to_state"] == S.CHANGES_REQUIRED]
    assert hist and hist[0]["evidence"].get("build_progress") is True
    # the same head again and again is a stall: it ends in the normal repair path
    assert run_until(c, lambda: c.store.q(
        "SELECT 1 FROM events WHERE task_id='T1' AND kind='build_progress_stalled'"))


def test_a_lead_case_waiting_for_the_workspace_goes_before_a_new_sprint_start(game):
    """v0.181.65: VR-AS21's Lead case (2026-11-05) waited over an hour for the vr-b worktree
    while the VR-AS20 and VR-AS22 fix rounds took it in turn. A sprint that does not hold the
    workspace yet now yields to an open Lead case of another sprint on the same checkout that
    waits for it; a case on another track, or one not waiting for the workspace, does not."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")
    P = c.projects["game"]
    ws = "workspace:test-checkout"
    from foundry_coordinator.lead import _V1
    c.store.conn.executescript(_V1)
    cols = [r["name"] for r in c.store.q("PRAGMA table_info(recovery_cases)")]
    vals = {"case_id": "rc-ws", "project_id": "game", "task_id": "T1", "state": "DIAGNOSING",
            "created_ts": c.clock(), "deadline_ts": c.clock() + 3600,
            "validation": json.dumps({"lead_waiting": "workspace lock held by game/T3"})}
    keys = [k for k in vals if k in cols]
    c.store.x(f"INSERT INTO recovery_cases ({', '.join(keys)}) VALUES "
              f"({', '.join('?' for _ in keys)})", [vals[k] for k in keys])
    t2 = c.store.task("game", "T2")
    held = c._acquire(P, t2, [ws])
    assert held and held["task_id"] == "T1" and held["case_id"] == "rc-ws"
    assert "Lead case rc-ws" in (c.store.task("game", "T2")["next_action"] or "")
    assert ws not in c.store.holds("game", "T2")
    # a different track: no wait
    c.store.x("UPDATE queue SET track='vr-b' WHERE project_id='game' AND task_id='T2'")
    assert c._acquire(P, c.store.task("game", "T2"), [ws]) is None
    c.store.release("game", "T2", [ws])
    c.store.x("UPDATE queue SET track='' WHERE project_id='game' AND task_id='T2'")
    # the Lead is not waiting for the workspace (its session started): no wait
    c.store.x("UPDATE recovery_cases SET validation=? WHERE case_id='rc-ws'",
              (json.dumps({"lead_waiting": None}),))
    assert c._acquire(P, c.store.task("game", "T2"), [ws]) is None
    c.store.release("game", "T2", [ws])
    # a resolved case never blocks
    c.store.x("UPDATE recovery_cases SET state='RESOLVED', validation=? WHERE case_id='rc-ws'",
              (json.dumps({"lead_waiting": "workspace lock held by game/T3"}),))
    assert c._acquire(P, c.store.task("game", "T2"), [ws]) is None
    # a sprint already holding the workspace keeps going
    c.store.x("UPDATE recovery_cases SET state='DIAGNOSING' WHERE case_id='rc-ws'")
    assert c._acquire(P, c.store.task("game", "T2"), [ws, "studio:x"]) is None


def test_a_local_fallback_hold_ends_when_the_implementer_persona_changes(game):
    """v0.181.66: VR-AS22 (2026-11-05) got a local hold at 19:55 while it ran the Claude
    persona; router rev 25 moved it to ChatGPT (Foundry-Kilo-Sol) a minute earlier. The probe
    could not clear it (the coordinator client may not call Sol), so its closeout waited for
    the local GPU. A hold now records its persona and ends when the persona changes."""
    from types import SimpleNamespace
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    P = c.projects["game"]
    import foundry_coordinator.fast_lane as FL
    orig = FL.is_local_primary
    orig_foundry = P.foundry
    P.foundry = SimpleNamespace(persona_info=lambda p: {"routes": []},
                                chat=lambda *a, **k: (_ for _ in ()).throw(
                                    RuntimeError("model_not_allowed")))
    try:
        FL.is_local_primary = lambda P, tid: False
        same = P.profile.persona("implementer", "T1")
        c.store.merge_data("game", "T1", local_fallback_until=c.clock() + 3600,
                           local_fallback_persona=same)
        # same persona, probe refused: the hold stands
        assert any(r.split("#")[0] == "gpu" for r in c._session_locks(P, c.store.task("game", "T1")))
        # the hold belongs to another persona: it ends
        c.store.merge_data("game", "T1", local_fallback_persona="Foundry-Kilo-Old")
        locks = c._session_locks(P, c.store.task("game", "T1"))
        assert not any(r.split("#")[0] == "gpu" for r in locks)
        assert not c.store.task("game", "T1")["data"].get("local_fallback_until")
        assert c.store.q("SELECT 1 FROM events WHERE task_id='T1' AND kind='local_fallback_cleared' "
                         "AND detail LIKE '%persona changed%'")
    finally:
        FL.is_local_primary = orig
        P.foundry = orig_foundry


def test_a_fresh_blocked_report_answer_is_the_next_strategy_once(game):
    """v0.181.69: BAL-1 (2026-11-05 19:43, 22:36): the answer to its BLOCKED report applied,
    and the next assignment still asked 'findings repeat with no materially different
    strategy' and opened a Lead case. The latest answered implementer_blocked decision is
    the next attempt's (trusted) strategy, used once."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    P = c.projects["game"]
    assert c._fresh_blocked_answer(P, c.store.task("game", "T1")) == ""
    did = c.obligations.open_decision("game", "T1", "implementer_blocked", "towers win",
                                      options=["weaken the castle", "buff enemies"],
                                      blocks=["T1"])
    c.store.x("UPDATE decisions SET status='answered', answer=?, answered_ts=? "
              "WHERE decision_id=?", (json.dumps("weaken the castle"), c.clock(), did))
    assert c._fresh_blocked_answer(P, c.store.task("game", "T1")) == "weaken the castle"
    assert c.store.q("SELECT 1 FROM events WHERE kind='blocked_answer_strategy'")
    # used once
    assert c._fresh_blocked_answer(P, c.store.task("game", "T1")) == ""


def test_a_sprint_holding_the_workspace_yields_it_to_a_waiting_lead_at_a_new_attempt(game):
    """v0.181.72: VR-AS04's Lead case (2026-11-06) waited 45 min for vr-a while VR-AS02 held
    the worktree from VERIFYING back into a new attempt. A sprint that holds the workspace
    and starts a new attempt (READY / CHANGES_REQUIRED) gives it back to a Lead case on the
    same checkout that waits for it; mid-attempt (IMPLEMENTING) it keeps it."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")
    P = c.projects["game"]
    ws = "workspace:test-checkout"
    from foundry_coordinator.lead import _V1
    c.store.conn.executescript(_V1)
    cols = [r["name"] for r in c.store.q("PRAGMA table_info(recovery_cases)")]
    vals = {"case_id": "rc-ws2", "project_id": "game", "task_id": "T1", "state": "DIAGNOSING",
            "created_ts": c.clock(), "deadline_ts": c.clock() + 3600,
            "validation": json.dumps({"lead_waiting": "workspace lock held by game/T2"})}
    keys = [k for k in vals if k in cols]
    c.store.x(f"INSERT INTO recovery_cases ({', '.join(keys)}) VALUES "
              f"({', '.join('?' for _ in keys)})", [vals[k] for k in keys])
    c.store.acquire([ws], "game", "T2")
    # mid-attempt: T2 keeps it
    c.store.update_task("game", "T2", state=S.IMPLEMENTING)
    assert c._acquire(P, c.store.task("game", "T2"), [ws]) is None
    assert ws in c.store.holds("game", "T2")
    # a new attempt: T2 gives it back and waits
    c.store.update_task("game", "T2", state=S.CHANGES_REQUIRED)
    held = c._acquire(P, c.store.task("game", "T2"), [ws])
    assert held and held["case_id"] == "rc-ws2"
    assert ws not in c.store.holds("game", "T2")
    assert c.store.q("SELECT 1 FROM events WHERE kind='workspace_yielded_to_lead'")


def test_a_main_sprint_waits_until_its_dependencys_track_has_merged(game):
    """2026-11-08: VR-AS14 completed on vr-e at 19:59 and VR-AS-SYNC (main checkout,
    depending on it) started at once while vr-e still waited to merge, so it regenerated
    the asset ids without VR-AS11..14's models. A COMPLETE dependency on a live track
    satisfies only sprints on that same track until the track merges."""
    tmp, root, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", ["T1"], "victor")
    P = c.projects["game"]
    c.store.add_track("game", "vr-e", "sprint", str(tmp / "wt"), "track/vr-e", "abc", [], ["T1"],
                      "victor", state="merging")
    c.store.x("UPDATE queue SET track='vr-e' WHERE project_id='game' AND task_id='T1'")
    c.store.update_track("game", "vr-e", state="merging")
    c.store.update_task("game", "T1", state=S.COMPLETE)
    t1 = c.store.task("game", "T1")
    assert not c._dep_satisfied(P, t1, "T2")
    c._queued(P, c.store.task("game", "T2"))
    t2 = c.store.task("game", "T2")
    assert t2["state"] == S.QUEUED and "track vr-e not merged yet" in (t2["next_action"] or "")
    assert c._dep_satisfied(P, t1)                       # no dependent named: as before
    c.store.x("UPDATE queue SET track='vr-e' WHERE project_id='game' AND task_id='T2'")
    assert c._dep_satisfied(P, t1, "T2")                  # same track: satisfied
    c.store.x("UPDATE queue SET track='' WHERE project_id='game' AND task_id='T2'")
    c.store.update_track("game", "vr-e", state="merged")
    assert c._dep_satisfied(P, t1, "T2")                  # merged: satisfied

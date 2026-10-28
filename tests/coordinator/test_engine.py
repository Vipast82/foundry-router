"""Coordinator engine: lifecycle, evidence gates, enforcement and recovery.

Every test uses real git repositories (with a bare remote), the generic
profile/adapter, a scripted coding client and a fake Foundry, so the state
machine, the mechanical checks and the store are exercised exactly as in
production; only the model is replaced by a script."""

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

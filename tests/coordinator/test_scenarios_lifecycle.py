"""Scenario review of the coordinator lifecycle against its intended workflow.

FAKES ONLY. Every test drives the real engine, store, git workspace and
verification checks; the coding model is either the ScriptedClient or the
fake `kilo serve` (devtools/fake_kilo.py on localhost, reached through the
real KiloClient adapter), and Foundry is the FakeFoundry. Nothing here proves
behaviour against a live Kilo, Foundry or model: "passes" means "tested
against fakes", not "proven live".

Scenarios:
  1. full lifecycle, happy path
  2. failure -> repair -> re-verify
  3. retry exhaustion with frontier consultation (retry_guidance)
  4. questions / permission requests / operator instructions mid-task
  5. pause / coordinator restart / resume (op journal, in-flight delivery)
  6. Kilo context compaction / truncated transcript / replaced session

Tests that encode the CORRECT behaviour for a found defect or gap are marked
xfail(strict=True) with the reason prefixed DEFECT or GAP.
"""

from __future__ import annotations

import json
import time

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.adapters import build_project
from foundry_coordinator.adapters.base import SessionState
from foundry_coordinator.adapters.kilo import KiloClient
from foundry_coordinator.adapters.scripted import ScriptedClient
from foundry_coordinator.devtools.fake_kilo import FakeKilo
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.profile import load
from foundry_coordinator.store import Store
from tests.coordinator.helpers import (FakeFoundry, coordinator, git, make_repo,
                                       review_json, run_until, write_ok, write_profile)

T1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt containing OK.",
      "scope": ["src/a.txt"]}
REPORT_DONE = '```coordinator-report\n{"status": "DONE", "summary": "did it"}\n```'


class Crash(BaseException):
    """The coordinator process dying mid-tick (not caught like an Exception)."""


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Gated(ScriptedClient):
    """ScriptedClient that (a) can hold a prompted session busy until `open`,
    (b) records every session it starts, (c) can 'die' right after Kilo
    accepted a prompt (crash_after) or right before sending one (crash_before)."""

    def __init__(self, script):
        super().__init__(script)
        self.open = True
        self.starts: list[str] = []
        self.crash_after = None          # callable(text) -> bool
        self.crash_before = None
        self.fail_before = None          # callable(text) -> bool: raise RuntimeError
        self.answered: list[tuple] = []

    def start_session(self, **kw):
        sid = super().start_session(**kw)
        self.starts.append(sid)
        return sid

    def prompt(self, session_id, text, *, directory, persona, agent):
        if self.crash_before and self.crash_before(text):
            self.crash_before = None
            raise Crash("died before the prompt reached Kilo")
        if self.fail_before and self.fail_before(text):
            self.fail_before = None
            raise RuntimeError("Kilo refused the prompt")
        super().prompt(session_id, text, directory=directory, persona=persona, agent=agent)
        if self.crash_after and self.crash_after(text):
            self.crash_after = None
            raise Crash("died right after Kilo accepted the prompt")

    def state(self, session_id, *, directory, since_ts):
        s = self.sessions.get(session_id)
        if s is not None and s.get("pending") and not self.open:
            return SessionState(status="busy")
        return super().state(session_id, directory=directory, since_ts=since_ts)

    def answer(self, session_id, request_id, answers, *, directory, kind="question"):
        self.answered.append((session_id, request_id, answers, kind))
        super().answer(session_id, request_id, answers, directory=directory, kind=kind)

    def prompts(self, sid):
        return self.sessions[sid]["prompts"]

    def prompted(self):
        return [sid for sid, s in self.sessions.items() if s["prompts"]]


def _state(c, tid="T1"):
    return c.store.task("game", tid)["state"]


def _events(c, kind):
    return c.store.q("SELECT * FROM events WHERE task_id='T1' AND kind=?", (kind,))


@pytest.fixture()
def game(tmp_path):
    root, remote = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    return tmp_path, root, remote, prof


# ============================================================ fake-Kilo environment

class KiloEnv:
    """The real coordinator + KiloClient against the fake `kilo serve`."""

    def __init__(self, tmp_path, tasks=None, extra=""):
        self.fake = FakeKilo()
        self.url = self.fake.start()
        self.root, _ = make_repo(tmp_path, "game", tasks or [T1])
        self.prof = load(str(write_profile(tmp_path, "game", self.root, extra=extra)))
        self.ff = FakeFoundry()
        self.clock = [time.time() - 3600]   # coordinator clock behind the fake's timestamps
        self.state_dir = str(tmp_path / "state")
        self.store_path = str(tmp_path / "state" / "c.sqlite")
        self.c = self.make()

    def make(self):
        P = build_project(self.prof, self.state_dir,
                          client=KiloClient(self.url, password=self.fake.password), foundry=self.ff)
        return Coordinator(Store(self.store_path), [P], self.state_dir,
                           clock=lambda: self.clock[0])

    def start(self, tid="T1"):
        self.c.approve("game", tid, [], "victor")
        self.c.tick()
        self.c.tick()
        assert _state(self.c, tid) == S.IMPLEMENTING
        return self.c.store.task("game", tid)["session_id"]

    def commit(self, rel, text, msg="T1: work"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        git(self.root, "add", "--", rel)
        git(self.root, "commit", "-q", "-m", msg)

    def prompts_to(self, sid):
        return [p for p in self.fake.prompts if p["session_id"] == sid]


@pytest.fixture()
def kenv(tmp_path):
    env = KiloEnv(tmp_path)
    yield env
    env.fake.stop()


# ============================================================ 1. happy path

def test_s1_happy_path_every_step_once_and_bound_to_the_commit(game):
    tmp, root, remote, prof = game
    ff = FakeFoundry()
    client = Gated(write_ok("src/a.txt"))
    c = coordinator(tmp, [prof], {"game": client}, foundry={"game": ff})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    t = c.store.task("game", "T1")
    head = git(root, "rev-parse", "HEAD")
    assert [h["to_state"] for h in c.store.history("game", "T1")] == [
        S.QUEUED, S.READY, S.IMPLEMENTING, S.VERIFYING, S.REVIEWING, S.APPROVED, S.CLOSING,
        S.COMPLETE]
    assert t["head_sha"] == t["reviewed_sha"] == head
    assert git(root, "ls-remote", "origin", "refs/heads/main").split()[0] == head
    # exactly one session, prompted exactly once; one review call; one push
    assert len(client.starts) == 1 and len(client.prompts(client.starts[0])) == 1
    assert len(ff.chats) == 1
    ops = c.store.q("SELECT key, status FROM ops WHERE task_id='T1'")
    assert {o["status"] for o in ops} == {"done"}
    assert sorted(o["key"].split(":")[0] for o in ops) == ["push", "review", "session"]
    assert c.store.locks() == []
    # a restarted coordinator on the same store does nothing more for a COMPLETE task
    c2 = coordinator(tmp, [prof], {"game": client}, foundry={"game": ff})
    for _ in range(5):
        c2.tick()
    assert len(client.starts) == 1 and len(ff.chats) == 1
    assert len(c2.store.q("SELECT * FROM ops WHERE task_id='T1'")) == len(ops)


def test_s1_happy_path_through_the_kilo_adapter(kenv):
    sid = kenv.start()
    kenv.c.tick()
    assert _state(kenv.c) == S.IMPLEMENTING                   # Kilo busy: nothing advances
    meta = kenv.fake.sessions[sid]["info"]["metadata"]["coordinator"]
    assert (meta["project"], meta["task"], meta["phase"], meta["attempt"]) == \
        ("game", "T1", "implement", 1)
    kenv.commit("src/a.txt", "OK")
    kenv.fake.finish(sid, "Done.\n" + REPORT_DONE)
    assert run_until(kenv.c, lambda: _state(kenv.c) == S.COMPLETE, limit=10)
    assert len(kenv.prompts_to(sid)) == 1 and len(kenv.fake.prompts) == 1
    assert kenv.c.store.task("game", "T1")["data"]["last_report"]["status"] == "DONE"
    impl = next(b for b in kenv.ff.binds if b["role"] == "implementer")
    assert impl["session_id"] == sid and impl["purpose"] == "implementer"


# ============================================================ 2. failure -> repair -> re-verify

def test_s2_check_failure_is_repaired_and_reverified_on_the_new_commit(game):
    tmp, root, _, prof = game
    ff = FakeFoundry()

    def agent(ctx):
        if ctx["attempt"] == 1:
            return {"writes": {"src/a.txt": "nope"}, "commit": "T1: first try"}
        assert "check project-check: FAIL" in ctx["prompt"]       # repair sees the real failure
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: fix"}
    client = Gated(agent)
    c = coordinator(tmp, [prof], {"game": client}, foundry={"game": ff})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    path = [h["to_state"] for h in c.store.history("game", "T1")]
    assert path[:8] == [S.QUEUED, S.READY, S.IMPLEMENTING, S.VERIFYING, S.CHANGES_REQUIRED,
                        S.IMPLEMENTING, S.VERIFYING, S.REVIEWING]
    sets = c.store.q("SELECT set_id, revision, result FROM verifications WHERE task_id='T1' "
                     "AND phase='verify' AND check_name='project-check' ORDER BY executed_ts")
    assert [s["result"] for s in sets] == ["FAIL", "PASS"]
    head = git(root, "rev-parse", "HEAD")
    assert sets[0]["revision"] != sets[1]["revision"] == head
    # the failing revision was never sent for review; the fixed one exactly once
    revs = c.store.reviews("game", "T1")
    assert len(revs) == 1 == len(ff.chats) and revs[0]["reviewed_sha"] == head
    impl = [s for s in c.store.sessions("game", "T1") if s["role"] == "implementer"
            and s["phase"] == "implement"]
    assert len(impl) == 2 and impl[0]["run_id"] != impl[1]["run_id"]
    assert c.store.task("game", "T1")["attempt"] == 2
    assert c.snapshot(c.store.task("game", "T1"))["findings"] == []


def test_s2_reviewer_changes_force_a_fresh_verification_before_the_next_review(game):
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
            return {"writes": {"src/a.txt": "OK OK"}, "commit": "T1: per review"}
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: first"}
    c = coordinator(tmp, [prof], {"game": Gated(agent)}, foundry={"game": FakeFoundry(reviewer)})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    ver = c.store.q("SELECT DISTINCT set_id, revision FROM verifications WHERE task_id='T1' "
                    "AND phase='verify'")
    revs = c.store.reviews("game", "T1")
    assert len(ver) == 2 and {v["revision"] for v in ver} == {r["reviewed_sha"] for r in revs}
    assert [r["verdict"] for r in revs] == ["CHANGES_REQUIRED", "APPROVED"]


# ============================================================ 3. retry exhaustion + guidance

OPUS, SONNET = "Foundry-Kilo-Opus", "Foundry-Kilo-Sonnet"
REPAIR = json.dumps({"action": "repair", "summary": "Use the known correct output.",
                     "steps": ["Write the required OK result, then run the project check."]})


class Adviser(FakeFoundry):
    """Fake Foundry that answers retry-guidance calls (by session id) and
    records which persona reviewed which head."""

    def __init__(self, reviewer=None, answer=REPAIR):
        super().__init__(reviewer)
        self.answer, self.guidance_calls, self.review_personas = answer, [], []
        self.admission = {OPUS: {"admit": True}}

    def chat(self, persona, messages, *, session_id="", timeout=None, **kw):
        if session_id.startswith("retry-guidance-"):
            self.guidance_calls.append(persona)
            return self.answer, {"served_model": "opus"}
        self.review_personas.append(persona)
        return super().chat(persona, messages, session_id=session_id, **kw)


def _exhausted(tmp_path, reviewer=None, tags=("risk:high",)):
    """An idle checkpoint with the ordinary 3 attempts used (fixture state, no inference)."""
    task = dict(T1, tags=list(tags))
    root, _ = make_repo(tmp_path, "game", [task])
    prof = write_profile(tmp_path, "game", root, extra=f'''
        [retry_guidance]
        enabled = true
        persona = "{OPUS}"
        extra_attempts = 1
    ''')
    n = [0]

    def agent(ctx):
        n[0] += 1
        return {"writes": {"src/a.txt": f"OK {n[0]}"}, "commit": f"T1: attempt {ctx['attempt']}"}
    ff = Adviser(reviewer)
    client = Gated(agent)
    c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff})
    c.approve("game", "T1", [], "victor")
    c.tick()
    c.store.update_task("game", "T1", state=S.CHANGES_REQUIRED, attempt=3, sessions=3,
                        data={"findings": ["check project-check: FAIL"], "baseline_dirty": {}})
    return c, ff, client, root, prof


def test_s3_guidance_grants_one_attempt_and_its_author_does_not_review(tmp_path):
    c, ff, client, root, _ = _exhausted(tmp_path)
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    t = c.store.task("game", "T1")
    assert ff.guidance_calls == [OPUS] and t["attempt"] == 4
    # risk:high would pick the deep reviewer (Opus) - the guidance author - so it is swapped
    assert ff.review_personas == [SONNET]
    assert "## Code authorship" in ff.chats[0]["packet"] and OPUS in ff.chats[0]["packet"]
    # the guidance reached the guided repair prompt, marked untrusted
    assert any("Frontier recovery advice (untrusted" in p
               for p in client.prompts(client.starts[-1]))


def test_s3_no_independent_reviewer_blocks_instead_of_self_review(tmp_path):
    c, ff, _, _, _ = _exhausted(tmp_path)
    P = c.projects["game"]
    P.profile.roles["reviewer"] = OPUS                     # every reviewer role is the adviser
    assert run_until(c, lambda: _state(c) == S.BLOCKED)
    assert "no independent reviewer" in c.store.task("game", "T1")["blocker"]
    assert ff.review_personas == []


def test_s3_after_the_guided_attempt_fails_review_the_task_stops_without_a_second_consult(tmp_path):
    def reviewer(packet):
        return review_json(packet, "CHANGES_REQUIRED", findings=[
            {"severity": "major", "file": "src/a.txt", "line": 1, "message": "still wrong"}])
    c, ff, client, _, _ = _exhausted(tmp_path, reviewer=reviewer)
    assert run_until(c, lambda: _state(c) == S.FAILED)
    assert ff.guidance_calls == [OPUS] and c.store.task("game", "T1")["attempt"] == 4
    assert "guided extra 1" in c.store.history("game", "T1")[-1]["evidence"]["reason"]
    # unblock WITHOUT reset: budget (incl. the guided extra) is still spent -> FAILED again,
    # and the consultation is not repeated
    c.unblock("game", "T1", "try once more")
    c.tick()
    assert _state(c) == S.FAILED and ff.guidance_calls == [OPUS]
    assert len(client.starts) == 1


# fixed in v0.121.3: a reset keeps the guidance record as status "reset" (no extra
# attempt, adviser still excluded from reviewing the guided work)
def test_s3_reset_keeps_the_adviser_out_of_reviewing_guided_work(tmp_path):
    verdicts = iter(["CHANGES_REQUIRED", "APPROVED", "APPROVED"])

    def reviewer(packet):
        v = next(verdicts)
        return review_json(packet, v, findings=[] if v == "APPROVED" else [
            {"severity": "major", "file": "src/a.txt", "line": 1, "message": "still wrong"}])
    c, ff, _, root, _ = _exhausted(tmp_path, reviewer=reviewer)
    assert run_until(c, lambda: _state(c) == S.FAILED)
    guided_head = git(root, "rev-parse", "HEAD")
    c.unblock("game", "T1", "fresh budget", reset_attempts=True)
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    task_base = c.store.task("game", "T1")["data"]["task_base"]
    assert c.projects["game"].ws.is_ancestor(task_base, guided_head)   # guided work is reviewed
    assert OPUS not in ff.review_personas


# ============================================================ 4. questions / permissions / notes

def test_s4_question_via_kilo_is_answered_by_the_coordinator(kenv):
    """W1.1: an implementer's Kilo question never reaches a person; the engine
    answers it in place and the session keeps the writer slot."""
    sid = kenv.start()
    rid = kenv.fake.ask_question(sid, [{"question": "Use blue or green?",
                                        "options": [{"label": "blue"}, {"label": "green"}]}])
    kenv.c.tick()
    t = kenv.c.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING and not t["data"].get("question")
    assert kenv.fake.replies[-1]["kind"] == "question"
    assert kenv.fake.replies[-1]["request_id"] == rid
    assert "BLOCKED report" in kenv.fake.replies[-1]["body"]["answers"][0][0]
    assert any(l["resource"].startswith("writer:") for l in kenv.c.store.locks())  # still owned
    kenv.commit("src/a.txt", "OK green")
    kenv.fake.finish(sid, REPORT_DONE)
    assert run_until(kenv.c, lambda: _state(kenv.c) == S.COMPLETE, limit=10)
    assert len(kenv.fake.prompts) == 1                         # the answer is not a new prompt


def test_s4_permission_via_kilo_holds_the_writer_and_resumes_the_same_session(kenv):
    sid = kenv.start()
    rid = kenv.fake.ask_permission(sid, "edit", ["src/a.txt"])
    kenv.c.tick()
    t = kenv.c.store.task("game", "T1")
    assert t["state"] == S.AWAITING_HUMAN and "permission edit" in t["awaiting"]
    assert t["data"]["question"]["request_id"] == rid
    assert any(l["resource"].startswith("writer:") for l in kenv.c.store.locks())  # still owned
    for _ in range(3):
        kenv.c.tick()
    assert _state(kenv.c) == S.AWAITING_HUMAN                  # waits for a person, no timeout
    kenv.c.answer("game", "T1", ["once"])
    assert kenv.fake.replies[-1]["kind"] == "permission"
    assert kenv.fake.replies[-1]["body"] == {"reply": "once"}
    assert _state(kenv.c) == S.IMPLEMENTING
    kenv.commit("src/a.txt", "OK green")
    kenv.fake.finish(sid, REPORT_DONE)
    assert run_until(kenv.c, lambda: _state(kenv.c) == S.COMPLETE, limit=10)
    assert len(kenv.fake.prompts) == 1                         # the answer is not a new prompt


def test_s4_permission_request_is_answered_through_the_permission_endpoint(kenv):
    sid = kenv.start()
    kenv.fake.ask_permission(sid, "bash", ["rm -rf build"])
    kenv.c.tick()
    t = kenv.c.store.task("game", "T1")
    assert t["state"] == S.AWAITING_HUMAN and t["data"]["question"]["kind"] == "permission"
    assert "rm -rf build" in t["awaiting"]
    kenv.c.answer("game", "T1", ["reject"])
    assert kenv.fake.replies[-1]["kind"] == "permission"
    assert kenv.fake.replies[-1]["body"] == {"reply": "reject"}
    assert _state(kenv.c) == S.IMPLEMENTING
    with pytest.raises(ValueError):
        kenv.c.answer("game", "T1", ["once"])                  # no open question any more


def test_s4_task_note_waits_for_the_next_prompt_and_is_delivered_once(game):
    tmp, _, _, prof = game

    def agent(ctx):
        if ctx["attempt"] == 1:
            return {"writes": {"src/a.txt": "nope"}, "commit": "T1: try"}
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: fix"}
    client = Gated(agent)
    client.open = False
    c = coordinator(tmp, [prof], {"game": client})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.IMPLEMENTING)
    first = c.store.task("game", "T1")["session_id"]
    c.store.add_operator_message("act-1", "game", "T1", "", "task", "next_session", "victor",
                                 "Prefer uppercase OK.")
    c.tick()
    assert len(client.prompts(first)) == 1                     # the running worker isn't interrupted
    client.open = True
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    m = c.store.operator_message("act-1")
    second = client.starts[1]
    assert m["status"] == "delivered" and m["delivered_session"] == second
    assert "Prefer uppercase OK." in client.prompts(second)[0]
    assert not any("Prefer uppercase OK." in p for p in client.prompts(first))


# fixed in v0.121.7
def test_s4_session_note_for_a_finished_session_is_rejected_not_stranded(game):
    tmp, _, _, prof = game

    def agent(ctx):
        if ctx["attempt"] == 1:
            return {"writes": {"src/a.txt": "nope"}, "commit": "T1: try"}
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: fix"}
    client = Gated(agent)
    client.open = False
    c = coordinator(tmp, [prof], {"game": client})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.IMPLEMENTING)
    first = c.store.task("game", "T1")["session_id"]
    c.store.add_operator_message("act-2", "game", "T1", first, "session", "next_session",
                                 "victor", "Only for this session.")
    client.open = True
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    assert c.store.operator_message("act-2")["status"] == "rejected"


def _needs_human_then_ok(answers_seen):
    def agent(ctx):
        answers_seen.append(ctx["prompt"])
        if len(answers_seen) == 1:
            return {"final": '```coordinator-report\n{"status": "NEEDS_HUMAN", '
                             '"blocker": "which colour?"}\n```'}
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: done"}
    return agent


def test_s4_unblock_without_reset_keeps_the_attempt_count(game):
    tmp, _, _, prof = game
    seen = []
    c = coordinator(tmp, [prof], {"game": Gated(_needs_human_then_ok(seen))})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED)
    assert "which colour?" in c.store.task("game", "T1")["blocker"]
    assert c.store.locks() == []
    c.unblock("game", "T1", "use green")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    t = c.store.task("game", "T1")
    assert t["attempt"] == 2 and t["sessions"] == 2


def test_s4_unblock_at_the_attempt_limit_fails_unless_reset(game):
    tmp, _, _, prof = game

    n = [0]

    def agent(ctx):                                            # keyed on calls: a reset restarts attempts
        n[0] += 1
        if n[0] < 3:
            return {"writes": {"src/a.txt": f"nope {n[0]}"}, "commit": "T1: try"}
        if n[0] == 3:
            return {"final": '```coordinator-report\n{"status": "BLOCKED", '
                             '"blocker": "need the spec clarified"}\n```'}
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: done"}
    c = coordinator(tmp, [prof], {"game": Gated(agent)})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED)
    assert c.store.task("game", "T1")["attempt"] == 3
    c.unblock("game", "T1", "clarified")                       # no reset
    c.tick()
    assert _state(c) == S.FAILED                               # last attempt was the blocked one
    c.unblock("game", "T1", "clarified, fresh budget", reset_attempts=True)
    t = c.store.task("game", "T1")
    assert (t["state"], t["attempt"], t["sessions"], t["started_ts"]) == (S.READY, 0, 0, None)
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    assert c.store.task("game", "T1")["attempt"] == 1


# fixed in v0.121.7
def test_s4_unblock_resolution_reaches_the_next_session(game):
    tmp, _, _, prof = game
    seen = []
    c = coordinator(tmp, [prof], {"game": Gated(_needs_human_then_ok(seen))})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED)
    c.unblock("game", "T1", "use green")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    assert "use green" in seen[1]


# ============================================================ 5. pause / restart / resume

def test_s5_restart_during_a_question_then_answer(game):
    tmp, _, _, prof = game

    def agent(ctx):
        if not ctx.get("answers"):
            # a permission ask: the request that still reaches a person (W1.1)
            return {"question": {"text": "edit src/a.txt?", "kind": "permission"}}
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: green"}
    client = Gated(agent)
    c1 = coordinator(tmp, [prof], {"game": client})
    c1.approve("game", "T1", [], "victor")
    assert run_until(c1, lambda: _state(c1) == S.AWAITING_HUMAN)
    del c1
    c2 = coordinator(tmp, [prof], {"game": client})
    c2.answer("game", "T1", [["once"]])
    assert run_until(c2, lambda: _state(c2) == S.COMPLETE)
    assert len(client.starts) == 1 and client.answered[0][3] == "permission"


def test_s5_pause_restart_resume_continues_the_same_session(game):
    tmp, _, _, prof = game
    client = Gated(write_ok("src/a.txt"))
    client.open = False
    c1 = coordinator(tmp, [prof], {"game": client})
    c1.approve("game", "T1", [], "victor")
    assert run_until(c1, lambda: _state(c1) == S.IMPLEMENTING)
    sid = c1.store.task("game", "T1")["session_id"]
    c1.pause("game", "T1", "lunch")
    assert _state(c1) == S.PAUSED
    del c1
    c2 = coordinator(tmp, [prof], {"game": client})
    for _ in range(3):
        c2.tick()
    assert _state(c2) == S.PAUSED                              # an operator pause waits
    c2.resume("game", "T1")
    t = c2.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING and t["session_id"] == sid
    client.open = True
    assert run_until(c2, lambda: _state(c2) == S.COMPLETE)
    assert client.starts == [sid] and len(client.prompts(sid)) == 2
    assert client.prompts(sid)[1].startswith("Continue task T1")
    assert c2.store.task("game", "T1")["attempt"] == 1


# fixed in v0.121.4
def test_s5_crash_after_prompt_before_transition_does_not_start_a_second_session(game):
    tmp, _, _, prof = game
    client = Gated(write_ok("src/a.txt"))
    client.crash_after = lambda text: True
    c1 = coordinator(tmp, [prof], {"game": client})
    c1.approve("game", "T1", [], "victor")
    c1.tick()                                                  # QUEUED -> READY
    with pytest.raises(Crash):
        c1.tick()                                              # session started + prompted, then death
    assert len(client.prompted()) == 1
    del c1
    c2 = coordinator(tmp, [prof], {"game": client})
    assert run_until(c2, lambda: _state(c2) == S.COMPLETE)
    assert len(client.starts) == 1 and len(client.prompted()) == 1


# fixed in v0.121.4
def test_s5_crash_during_auto_resume_does_not_repeat_the_continue_prompt(game):
    tmp, _, _, prof = game
    calls = []

    def agent(ctx):
        calls.append(ctx["prompt"][:30])
        if len(calls) == 1:
            return {"error": 'APIError 429 {"error": {"code": "budget_denied", '
                             '"foundry_category": "budget", "reset_seconds": 30}}'}
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: done"}
    clk = Clock()
    client = Gated(agent)
    c1 = coordinator(tmp, [prof], {"game": client}, clock=clk)
    c1.approve("game", "T1", [], "victor")
    assert run_until(c1, lambda: _state(c1) == S.PAUSED)
    sid = c1.store.task("game", "T1")["session_id"]
    clk.t += 31
    client.crash_after = lambda text: text.startswith("Continue task")
    with pytest.raises(Crash):
        run_until(c1, lambda: False, limit=3)
    del c1
    c2 = coordinator(tmp, [prof], {"game": client}, clock=clk)
    assert run_until(c2, lambda: _state(c2) == S.COMPLETE)
    assert sum(p.startswith("Continue task") for p in client.prompts(sid)) == 1


def test_s5_safe_point_delivery_is_journaled_and_never_repeated_after_a_crash(game):
    tmp, _, _, prof = game
    client = Gated(write_ok("src/a.txt"))
    client.open = False
    c1 = coordinator(tmp, [prof], {"game": client})
    c1.approve("game", "T1", [], "victor")
    assert run_until(c1, lambda: _state(c1) == S.IMPLEMENTING)
    t = c1.store.task("game", "T1")
    sid = t["session_id"]
    # a plain failure before anything was sent forgets the journal entry -> resendable
    client.fail_before = lambda text: True
    with pytest.raises(RuntimeError):
        c1._prompt_primary(c1.projects["game"], t, "advice A", "kA")
    assert c1._prompt_primary(c1.projects["game"], t, "advice A", "kA") is True
    assert client.prompts(sid)[-1] == "advice A"
    # a process death between the journal write and the send: not repeated after restart
    client.crash_before = lambda text: True
    with pytest.raises(Crash):
        c1._prompt_primary(c1.projects["game"], t, "advice B", "kB")
    del c1
    c2 = coordinator(tmp, [prof], {"game": client})
    assert c2._prompt_primary(c2.projects["game"], t, "advice B", "kB") is False
    assert "advice B" not in client.prompts(sid)               # lost, by design, never doubled
    assert _events(c2, "helper_delivery_uncertain")
    assert c2._prompt_primary(c2.projects["game"], t, "advice A", "kA") is False  # done once
    assert client.prompts(sid).count("advice A") == 1


# ============================================================ 6. compaction / truncation / replaced

def _compaction_turn(kenv, sid):
    """Kilo ends a turn on a compaction summary message (opencode marks it
    summary=true, mode=compaction). Shape modelled, not observed live."""
    kenv.fake.add_message(sid, "assistant", [{"type": "text", "text":
                          "Summary of the conversation so far: edited nothing yet; next "
                          "step is to write src/a.txt."}], agent="compaction")
    kenv.fake.sessions[sid]["messages"][-1]["info"].update(summary=True, mode="compaction")
    kenv.fake.set_status(sid, None)


def test_s6_compaction_turn_is_never_mistaken_for_finished_work(kenv):
    """v0.121.7: a turn ending on Kilo's compaction summary is not verified and
    costs no attempt; the same session stays the writer."""
    sid = kenv.start()
    _compaction_turn(kenv, sid)
    for _ in range(3):
        kenv.c.tick()
    t = kenv.c.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING and t["session_id"] == sid and t["attempt"] == 1
    assert not t["data"].get("findings")
    assert len(kenv.prompts_to(sid)) == 1                      # within the grace: only waiting


# fixed in v0.121.7: Kilo stays the compaction manager; after compaction_grace_seconds
# without its own continuation the same session gets ONE journaled continue prompt
def test_s6_compaction_turn_continues_the_same_session(kenv):
    sid = kenv.start()
    _compaction_turn(kenv, sid)
    kenv.c.tick()                                              # first sighting: wait
    kenv.clock[0] += 31
    kenv.c.tick()
    t = kenv.c.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING and t["session_id"] == sid
    assert len(kenv.prompts_to(sid)) == 2 and t["attempt"] == 1
    assert "compacted" in kenv.prompts_to(sid)[1]["text"]
    for _ in range(3):                                         # never nudged twice
        kenv.clock[0] += 31
        kenv.c.tick()
    assert len(kenv.prompts_to(sid)) == 2
    kenv.commit("src/a.txt", "OK")
    kenv.fake.finish(sid, REPORT_DONE)
    assert run_until(kenv.c, lambda: _state(kenv.c) == S.COMPLETE, limit=10)
    assert kenv.c.store.task("game", "T1")["attempt"] == 1


def test_s6_kilo_continuing_by_itself_gets_no_extra_prompt(kenv):
    sid = kenv.start()
    _compaction_turn(kenv, sid)
    kenv.c.tick()
    # Kilo's own auto-continuation: a synthetic user turn, then the model works
    kenv.fake.add_message(sid, "user", [{"type": "text", "text": "Continue if you have next "
                                                                 "steps"}])
    kenv.fake.set_status(sid, "busy")
    for _ in range(3):
        kenv.clock[0] += 31
        kenv.c.tick()
    assert len(kenv.prompts_to(sid)) == 1
    assert _state(kenv.c) == S.IMPLEMENTING


def test_s6_compaction_part_shape_is_recognised(kenv):
    """Alternative shape: a compaction content part, no summary flag."""
    sid = kenv.start()
    kenv.fake.add_message(sid, "assistant", [{"type": "compaction", "text": "summary"}])
    kenv.fake.set_status(sid, None)
    kenv.c.tick()
    t = kenv.c.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING and t["attempt"] == 1


def test_s6_truncated_transcript_does_not_lose_committed_work(kenv):
    sid = kenv.start()
    kenv.commit("src/a.txt", "OK")
    with kenv.fake.lock:
        kenv.fake.sessions[sid]["messages"].clear()             # history pruned/truncated
    kenv.fake.finish(sid, REPORT_DONE)
    assert run_until(kenv.c, lambda: _state(kenv.c) == S.COMPLETE, limit=10)
    assert kenv.c.store.task("game", "T1")["attempt"] == 1


def test_s6_idle_with_no_assistant_reply_is_bounded_by_the_session_timeout(kenv):
    sid = kenv.start()
    with kenv.fake.lock:
        kenv.fake.sessions[sid]["messages"] = [m for m in kenv.fake.sessions[sid]["messages"]
                                               if m["info"]["role"] == "user"]
    kenv.fake.set_status(sid, None)                            # idle, but no reply survives
    for _ in range(3):
        kenv.c.tick()
    assert _state(kenv.c) == S.IMPLEMENTING                    # read as "not admitted yet"
    kenv.fake.set_status(sid, "busy")
    kenv.clock[0] += 181 * 60
    kenv.c.tick()
    t = kenv.c.store.task("game", "T1")
    assert t["state"] == S.CHANGES_REQUIRED and sid in kenv.fake.aborts
    assert any("ran over 180 minutes" in f for f in t["data"]["findings"])


def test_s6_replaced_session_is_a_free_retry_and_task_notes_follow(kenv):
    sid = kenv.start()
    kenv.c.store.add_operator_message("act-t", "game", "T1", "", "task", "next_session",
                                      "victor", "Remember the OK.")
    with kenv.fake.lock:
        kenv.fake.sessions.pop(sid)                            # Kilo restarted / session replaced
    kenv.c.tick()
    t = kenv.c.store.task("game", "T1")
    assert t["state"] == S.CHANGES_REQUIRED
    assert kenv.c.store.session(sid)["outcome"] == "lost"
    kenv.c.tick()
    t = kenv.c.store.task("game", "T1")
    new = t["session_id"]
    assert t["state"] == S.IMPLEMENTING and new != sid
    assert t["attempt"] == 1 and t["sessions"] == 2            # no attempt used
    text = kenv.prompts_to(new)[0]["text"]
    assert "disappeared" in text and "Remember the OK." in text
    assert kenv.c.store.operator_message("act-t")["delivered_session"] == new
    kenv.commit("src/a.txt", "OK")
    kenv.fake.finish(new, REPORT_DONE)
    assert run_until(kenv.c, lambda: _state(kenv.c) == S.COMPLETE, limit=10)


def test_s4_unblock_resolution_is_delivered_once_and_not_reported_as_an_action(game):
    tmp, _, _, prof = game
    seen = []
    c = coordinator(tmp, [prof], {"game": Gated(_needs_human_then_ok(seen))})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED)
    c.unblock("game", "T1", "use green")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    assert sum("use green" in p for p in seen) == 1
    [m] = [m for m in c.store.operator_messages() if m["action_id"].startswith("internal:")]
    assert m["status"] == "delivered" and m["scope"] == "task"


def test_s4_session_note_for_the_live_session_is_kept(game):
    tmp, _, _, prof = game
    client = Gated(write_ok("src/a.txt"))
    client.open = False
    c = coordinator(tmp, [prof], {"game": client})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.IMPLEMENTING)
    sid = c.store.task("game", "T1")["session_id"]
    c.store.add_operator_message("act-3", "game", "T1", sid, "session", "next_session",
                                 "victor", "For you.")
    for _ in range(3):
        c.tick()
    assert c.store.operator_message("act-3")["status"] == "queued"
